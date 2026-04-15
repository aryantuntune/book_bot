"""Auth-seed profile management: get one Chrome profile authenticated,
then clone it to every chunk's profile dir so chunks can run headless
from the first row without each one demanding its own OTP.

The design treats the `.chromium-profile/` directory (cookies, local
storage, service workers) as the entire auth state. Cloning the dir
is equivalent to transferring an authenticated session, and HPCL's
long session lifetime (~15h) makes this practical for a day's work."""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from booking_bot import config, exceptions
from booking_bot.orchestrator.splitter import ChunkSpec

log = logging.getLogger("orchestrator.auth_template")

_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_DEFAULT_LOCK = ("Default", "LOCK")


def _seed_path(source: str) -> Path:
    return config.ROOT / f".chromium-profile-{source}-auth-seed"


def _chunk_profile_path(profile_suffix: str) -> Path:
    return config.ROOT / f".chromium-profile-{profile_suffix}"


def _auth_fresh(profile_dir: Path, *, max_age_s: float) -> bool:
    """True iff profile_dir/last_auth.json exists, parses, and is less than
    max_age_s old. Expects the browser.py write format:
    {"auth_at_utc": "<ISO-8601 UTC timestamp>"}. Any other shape (missing
    key, wrong type, malformed JSON) collapses to False — callers fall
    through to a fresh interactive auth in that case."""
    last_auth = profile_dir / "last_auth.json"
    if not last_auth.exists():
        return False
    try:
        data = json.loads(last_auth.read_text(encoding="utf-8"))
        written_at = datetime.fromisoformat(data["auth_at_utc"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    age_s = (datetime.now(timezone.utc) - written_at).total_seconds()
    return 0 <= age_s < max_age_s


def _scrub_lock_files(profile_dir: Path) -> None:
    for name in _SINGLETON_FILES:
        target = profile_dir / name
        if target.exists():
            try:
                target.unlink()
            except OSError as e:
                log.warning(f"could not unlink {target}: {e}")
    default_lock = profile_dir / _DEFAULT_LOCK[0] / _DEFAULT_LOCK[1]
    if default_lock.exists():
        try:
            default_lock.unlink()
        except OSError as e:
            log.warning(f"could not unlink {default_lock}: {e}")


def clone_to_chunks(source: str, chunks: list[ChunkSpec]) -> None:
    """Copy the source's auth-seed profile to each chunk's profile dir.
    Skips chunks whose target already has a fresh `last_auth.json`.
    Aggregates all failures and raises AuthCloneFailed at the end with
    the complete list so the operator can see every chunk that broke."""
    seed = _seed_path(source)
    if not seed.exists():
        raise FileNotFoundError(
            f"auth seed profile missing: {seed}. Run `orchestrator auth "
            f"--source {source}` first."
        )
    max_age_s = float(config.AUTH_COOLDOWN_S)
    failures: list[tuple[str, str]] = []
    for c in chunks:
        target = _chunk_profile_path(c.profile_suffix)
        if target.exists() and _auth_fresh(target, max_age_s=max_age_s):
            log.info(f"chunk {c.chunk_id}: profile already fresh, skipping clone")
            continue
        if target.exists():
            try:
                shutil.rmtree(target)
            except OSError as e:
                failures.append((c.chunk_id, f"rmtree failed: {e}"))
                continue
        try:
            shutil.copytree(seed, target)
        except OSError as e:
            failures.append((c.chunk_id, f"copytree failed: {e}"))
            continue
        _scrub_lock_files(target)
        log.info(f"chunk {c.chunk_id}: profile cloned from seed")
    if failures:
        raise exceptions.AuthCloneFailed(failures=failures)


def ensure_auth_seed(
    source: str, *, operator_phone: str | None = None,
) -> Path:
    """Return the path to an authenticated auth-seed profile for `source`.

    Three paths, tried in order:
      A) Seed already exists and its last_auth is fresher than
         (AUTH_COOLDOWN_S - ORCHESTRATOR_AUTH_SEED_BUFFER_S). Return it.
      B) Seed is missing/stale, but the main .chromium-profile has a
         fresh last_auth.json. Copy main profile to seed path, scrub
         locks, return.
      C) Neither above applies. Launch an interactive browser, block
         until the operator logs in (or timeout), then return.
    """
    seed = _seed_path(source)
    max_age_s = float(
        config.AUTH_COOLDOWN_S - config.ORCHESTRATOR_AUTH_SEED_BUFFER_S
    )

    # Path A.
    if seed.exists() and _auth_fresh(seed, max_age_s=max_age_s):
        log.info(f"auth seed for {source}: fresh ({seed})")
        return seed

    # Path B.
    main_profile = config.ROOT / ".chromium-profile"
    if main_profile.exists() and _auth_fresh(main_profile, max_age_s=max_age_s):
        log.info(
            f"auth seed for {source}: copying from main profile {main_profile}"
        )
        if seed.exists():
            shutil.rmtree(seed)
        shutil.copytree(main_profile, seed)
        _scrub_lock_files(seed)
        return seed

    # Path C.
    log.info(f"auth seed for {source}: launching interactive auth")
    return _interactive_auth_seed(source, operator_phone=operator_phone)


def _interactive_auth_seed(
    source: str, *, operator_phone: str | None = None,
) -> Path:
    """Interactive Path C. Launches a real Chromium window against
    `.chromium-profile-<source>-auth-seed/`, polls the profile's
    last_auth.json, and closes the browser once the operator logs in.
    Raises AuthSeedTimeout on timeout."""
    from booking_bot import browser  # lazy — keeps Playwright off the import graph for unit tests
    seed = _seed_path(source)
    seed.mkdir(parents=True, exist_ok=True)
    pw, _browser_obj, ctx, _page = browser.start_browser(
        headless=False,
        profile_suffix=f"{source}-auth-seed",
    )
    print(
        f"[auth_template] Auth seed: log in to HPCL in the browser window. "
        f"This window will close once authentication completes "
        f"(timeout: {config.ORCHESTRATOR_AUTH_TIMEOUT_S // 60} min).",
        flush=True,
    )
    deadline = time.monotonic() + config.ORCHESTRATOR_AUTH_TIMEOUT_S
    poll_start = time.monotonic()
    try:
        while time.monotonic() < deadline:
            if _auth_fresh(seed, max_age_s=60.0):
                last_mtime = (seed / "last_auth.json").stat().st_mtime
                if last_mtime >= poll_start:
                    time.sleep(5.0)  # let redirects settle
                    return seed
            time.sleep(2.0)
        raise exceptions.AuthSeedTimeout(
            f"auth seed for {source} timed out after "
            f"{config.ORCHESTRATOR_AUTH_TIMEOUT_S}s"
        )
    finally:
        try:
            ctx.close()
        except Exception as e:
            log.warning(f"auth seed ctx.close() failed: {e}")
        try:
            pw.stop()
        except Exception as e:
            log.warning(f"auth seed pw.stop() failed: {e}")
