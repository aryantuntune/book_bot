"""Auth-seed profile management: get one Chrome profile authenticated,
then clone it to every chunk's profile dir so chunks can run headless
from the first row without each one demanding its own OTP.

The design treats the `.chromium-profile/` directory (cookies, local
storage, service workers) as the entire auth state. Cloning the dir
is equivalent to transferring an authenticated session, and HPCL's
long session lifetime (~15h) makes this practical for a day's work."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from booking_bot import config, exceptions
from booking_bot.orchestrator.splitter import ChunkSpec

log = logging.getLogger("orchestrator.auth_template")

_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_DEFAULT_LOCK = ("Default", "LOCK")


def _seed_path(source: str, slot: str = "op1") -> Path:
    """Auth-seed profile path for (source, slot). Slot is the operator
    bucket label: op1, op2, ..., opK. Single-operator callers use the
    default slot='op1'."""
    return config.ROOT / f".chromium-profile-{source}-{slot}-auth-seed"


def _seed_phone_meta_path(source: str, slot: str) -> Path:
    """Path to the small JSON file sitting alongside each auth seed that
    records which operator phone seeded it. Used by `start` to verify
    that the `--operator-phones` argument still matches the seeds on
    disk (guards against a reordered phone list between auth and start)."""
    return _seed_path(source, slot) / "seed_phone.json"


def _write_seed_phone(source: str, slot: str, phone: str) -> None:
    """Record the operator phone used to create this slot's seed. Written
    by `ensure_auth_seeds` after a successful interactive or Path-B seed.
    Never raises — metadata is advisory, not load-bearing."""
    path = _seed_phone_meta_path(source, slot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"operator_phone": phone}), encoding="utf-8",
        )
    except OSError as e:
        log.warning(f"could not write seed_phone.json for {source}/{slot}: {e}")


def _read_seed_phone(source: str, slot: str) -> str | None:
    """Return the phone recorded for this slot's seed, or None if missing
    or corrupt. Never raises."""
    path = _seed_phone_meta_path(source, slot)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    phone = data.get("operator_phone") if isinstance(data, dict) else None
    return str(phone) if phone else None


def _chunk_profile_path(profile_suffix: str) -> Path:
    return config.ROOT / f".chromium-profile-{profile_suffix}"


def _auth_fresh(profile_dir: Path, *, max_age_s: float) -> bool:
    """True iff profile_dir/last_auth.json exists, parses, and is less than
    max_age_s old. Expects the browser.py write format:
    {"auth_at_utc": "<ISO-8601 UTC timestamp>"}. Any other shape (missing
    key, wrong type, malformed JSON, naive datetime) collapses to False —
    callers fall through to a fresh interactive auth in that case."""
    last_auth = profile_dir / "last_auth.json"
    if not last_auth.exists():
        return False
    try:
        data = json.loads(last_auth.read_text(encoding="utf-8"))
        written_at = datetime.fromisoformat(data["auth_at_utc"])
        age_s = (datetime.now(timezone.utc) - written_at).total_seconds()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
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
    """Copy each chunk's operator-slot seed profile to the chunk's own
    profile dir. Skips chunks whose target already has a fresh
    `last_auth.json`. Aggregates all failures and raises AuthCloneFailed
    at the end with the complete list so the operator can see every
    chunk that broke.

    Raises FileNotFoundError early if any needed slot's seed is missing
    — fail before starting any clones so we don't leave half-populated
    state behind."""
    needed_slots = {c.operator_slot for c in chunks}
    for slot in needed_slots:
        seed = _seed_path(source, slot)
        if not seed.exists():
            raise FileNotFoundError(
                f"auth seed missing for {source}/{slot}: {seed}. Run "
                f"`orchestrator auth --source {source} --operator-phones "
                f"<list>` first."
            )

    max_age_s = float(config.AUTH_COOLDOWN_S)
    failures: list[tuple[str, str]] = []
    for c in chunks:
        seed = _seed_path(source, c.operator_slot)
        target = _chunk_profile_path(c.profile_suffix)
        if target.exists() and _auth_fresh(target, max_age_s=max_age_s):
            log.info(
                f"chunk {c.chunk_id}: profile already fresh, skipping clone"
            )
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
        log.info(
            f"chunk {c.chunk_id}: profile cloned from seed "
            f"{c.operator_slot}"
        )
    if failures:
        raise exceptions.AuthCloneFailed(failures=failures)


def ensure_auth_seeds(
    source: str, operator_phones: list[str],
) -> dict[str, Path]:
    """Return {slot: seed_path} for each operator phone in `operator_phones`.
    Slots are positional: op1, op2, ..., opK.

    For each slot, tries three paths:
      A) Seed already exists with fresh last_auth.json. Use as-is.
      B) Only for op1: main .chromium-profile is fresh → copytree to seed.
      C) Launch interactive Chromium against the slot's seed dir, block
         until the operator logs in (or timeout).

    Writes a seed_phone.json alongside each successful seed recording
    which phone seeded it. Raises AuthSeedTimeout on interactive timeout
    of any slot — previous slots' seeds are left on disk for a retry to
    pick up.
    """
    if not operator_phones:
        raise ValueError("operator_phones must be non-empty")
    if len(set(operator_phones)) != len(operator_phones):
        raise ValueError(
            f"operator_phones contains duplicates: {operator_phones}"
        )
    max_age_s = float(
        config.AUTH_COOLDOWN_S - config.ORCHESTRATOR_AUTH_SEED_BUFFER_S
    )
    seeds: dict[str, Path] = {}
    for i, phone in enumerate(operator_phones):
        slot = f"op{i + 1}"
        seed = _seed_path(source, slot)

        if seed.exists() and _auth_fresh(seed, max_age_s=max_age_s):
            recorded = _read_seed_phone(source, slot)
            if recorded is None or recorded == phone:
                log.info(f"auth seed {source}/{slot}: fresh ({seed})")
                seeds[slot] = seed
                if recorded is None:
                    _write_seed_phone(source, slot, phone)
                continue
            log.warning(
                f"auth seed {source}/{slot}: seed is fresh but was authenticated "
                f"as a different operator ({recorded}); re-seeding for {phone[:3]}XXXXXXX"
            )
            shutil.rmtree(seed)
            # fall through to Path B / Path C below

        if slot == "op1":
            main_profile = config.ROOT / ".chromium-profile"
            if main_profile.exists() and _auth_fresh(
                main_profile, max_age_s=max_age_s,
            ):
                log.info(
                    f"auth seed {source}/op1: copying from main profile "
                    f"{main_profile}"
                )
                if seed.exists():
                    shutil.rmtree(seed)
                shutil.copytree(main_profile, seed)
                _scrub_lock_files(seed)
                _write_seed_phone(source, slot, phone)
                seeds[slot] = seed
                continue

        log.info(
            f"auth seed {source}/{slot}: launching interactive auth for "
            f"operator {phone[:3]}XXXXXXX"
        )
        path = _interactive_auth_seed(source, slot=slot, operator_phone=phone)
        _write_seed_phone(source, slot, phone)
        seeds[slot] = path
    return seeds


def ensure_auth_seed(
    source: str, *, operator_phone: str | None = None,
) -> Path:
    """Legacy single-slot wrapper. Callers that don't know about multi-op
    (existing single-operator code paths) continue to work unchanged."""
    phone = operator_phone or config.OPERATOR_PHONE
    seeds = ensure_auth_seeds(source, [phone])
    return seeds["op1"]


_ALIVE_STATES = ("READY_FOR_CUSTOMER", "MAIN_MENU", "BOOK_FOR_OTHERS_MENU")


@contextlib.contextmanager
def _slot_env(slot: str):
    """Temporarily set BOOKING_BOT_OPERATOR_SLOT so functions that pick a
    slot-aware shared_auth path (browser._shared_auth_path) route the
    write to shared_auth-<slot>.json instead of the legacy file. Restores
    the prior value (or absence) on exit so the process-wide env isn't
    left polluted for subsequent slots in the same ensure_auth_seeds run."""
    key = config.OPERATOR_SLOT_ENV
    prev = os.environ.get(key)
    os.environ[key] = slot
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def _interactive_auth_seed(
    source: str, *, slot: str = "op1", operator_phone: str | None = None,
) -> Path:
    """Interactive Path C. Launches a real Chromium window against
    `.chromium-profile-<source>-<slot>-auth-seed/`, polls the live chat
    frame for a logged-in state, writes last_auth.json via
    browser.mark_auth_success, and closes the browser. Raises
    AuthSeedTimeout on timeout.

    Historical bug (fixed 2026-04-17): the original poll loop watched
    the seed dir for last_auth.json to appear. Nothing in the
    interactive flow ever writes that file — it's only written from
    auth.login_if_needed, which this path never calls — so the loop
    timed out on every fresh operator even when the login succeeded
    on-screen. Op1 happened to work by accident via Path B's copytree
    from the pre-existing main profile. This function now observes
    the login itself: poll chat.detect_state, and once the state is
    MAIN_MENU / READY_FOR_CUSTOMER / BOOK_FOR_OTHERS_MENU, mark auth
    success on the seed profile and return."""
    from booking_bot import browser, chat  # lazy — keeps Playwright off the import graph for unit tests
    seed = _seed_path(source, slot)
    seed.mkdir(parents=True, exist_ok=True)
    pw, _browser_obj, ctx, page = browser.start_browser(
        headless=False,
        profile_suffix=f"{source}-{slot}-auth-seed",
    )
    print(
        f"[auth_template] Auth seed {slot}: log in to HPCL in the browser "
        f"window as operator "
        f"{(operator_phone or '')[:3]}XXXXXXX. This window will close once "
        f"authentication completes "
        f"(timeout: {config.ORCHESTRATOR_AUTH_TIMEOUT_S // 60} min).",
        flush=True,
    )
    deadline = time.monotonic() + config.ORCHESTRATOR_AUTH_TIMEOUT_S
    try:
        frame = browser.get_chat_frame(page)
        while time.monotonic() < deadline:
            try:
                state = chat.detect_state(frame)
                if state in _ALIVE_STATES:
                    log.info(
                        f"auth seed {source}/{slot}: session alive "
                        f"(state={state!r}); capturing auth state"
                    )
                    browser.mark_auth_success()
                    # HPCL stores its auth token in JS sessionStorage,
                    # which Chromium does not persist to disk across a
                    # fresh launch_persistent_context. Capture cookies +
                    # localStorage + sessionStorage to shared_auth-<slot>.json
                    # while the live page is still open so chunks launched
                    # against cloned profiles can replay the storage via
                    # inject_shared_auth_cookies' add_init_script. Without
                    # this, the clone lands on a brand-new tab with an
                    # empty sessionStorage and HPCL shows the login screen.
                    with _slot_env(slot):
                        browser.write_shared_auth_state(page)
                    time.sleep(2.0)  # let any trailing JS settle
                    return seed
            except Exception as e:
                # Frame may have detached during login navigation
                # (HPCL sometimes redirects post-OTP). Re-acquire the
                # frame and keep polling — the next iteration will
                # usually land on the new post-login page.
                log.debug(
                    f"auth seed {source}/{slot}: poll error "
                    f"({type(e).__name__}: {e}); re-acquiring frame"
                )
                try:
                    frame = browser.get_chat_frame(page)
                except Exception:
                    pass
            time.sleep(3.0)
        raise exceptions.AuthSeedTimeout(
            f"auth seed for {source}/{slot} timed out after "
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
