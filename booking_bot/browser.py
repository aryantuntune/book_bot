"""Playwright lifecycle, iframe drilling, gateway listener, and recover_session.

This file is split across three tasks: Task 11 adds start_browser +
get_chat_frame, Task 12 adds the gateway listener, Task 19 adds recover_session.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from booking_bot import config
from booking_bot.exceptions import (
    ChromeNotInstalledError,
    GatewayError,
    IframeLostError,
    ProfileInUseError,
)

log = logging.getLogger("browser")

# Module-level state for the gateway listener (Task 12). One-process bot.
_gateway_error_seen = False

PROFILE_DIR_NAME = ".chrome-profile"
CHROMIUM_PROFILE_DIR_NAME = ".chromium-profile"
_LAST_AUTH_FILENAME = "last_auth.json"

# Set by start_browser() to the profile dir actually in use. Read by
# _last_auth_path() so the auth-cooldown file follows the suffixed profile
# instead of leaking across parallel instances.
_active_profile_dir: Path | None = None


def reset_gateway_flag() -> None:
    global _gateway_error_seen
    _gateway_error_seen = False


def gateway_flag() -> bool:
    return _gateway_error_seen


# Section 1 of the survivability design: we persist the last successful
# auth timestamp to disk so an auto-restart (or a manual rerun) doesn't
# forget that we just typed an OTP. Wall-clock UTC — monotonic would
# reset on reboot and the only consumer is a cooldown check where
# wall-clock is exactly right.


def _last_auth_path() -> Path:
    """Disk location of the auth timestamp file. Lives inside the same
    persistent profile dir as the chrome cookies so wiping the profile
    also wipes the cooldown in one shot. Resolved lazily because
    config.ROOT is read-through and tests monkeypatch it.

    Prefers the active profile dir set by start_browser() so --profile-suffix
    runs read/write the cooldown file inside their own suffixed directory
    rather than sharing one file across parallel instances."""
    if _active_profile_dir is not None:
        return _active_profile_dir / _LAST_AUTH_FILENAME
    return Path(config.ROOT) / CHROMIUM_PROFILE_DIR_NAME / _LAST_AUTH_FILENAME


def mark_auth_success() -> None:
    """Record that the operator is currently authenticated. Writes
    atomically via <path>.tmp + os.replace so a crash mid-write can't
    produce a corrupt JSON that breaks subsequent runs."""
    path = _last_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"auth_at_utc": datetime.now(timezone.utc).isoformat()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def last_auth_age_s() -> float | None:
    """Seconds since the last mark_auth_success() call, or None if we've
    never authed or the file is missing/unreadable. A corrupt or
    future-dated file is treated as 'never authed' — better to prompt
    once for OTP than to trust garbage."""
    path = _last_auth_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        then = datetime.fromisoformat(payload["auth_at_utc"])
        now = datetime.now(timezone.utc)
        age = (now - then).total_seconds()
        if age < 0:
            return None
        return age
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


# ---- Shared auth (cross-instance cookie transplant) ---------------------
#
# The three functions below implement the shared_auth.json protocol that
# lets parallel booking_bot instances share a single operator OTP:
#
#   1. After any instance completes operator auth, `write_shared_auth_state`
#      snapshots the current page-context's HPCL cookies and writes them
#      atomically to config.ROOT / shared_auth.json with a UTC timestamp.
#   2. `read_shared_auth_state` parses the file, validates freshness
#      against SHARED_AUTH_MAX_AGE_S, and returns the payload — tolerating
#      missing, stale, corrupt, or mid-write files.
#   3. `inject_shared_auth_cookies` calls add_cookies() on a live
#      BrowserContext so the cookies apply to the next navigation.
#
# start_browser() calls inject() once between context creation and the
# first HPCL goto, so a fresh launch with a valid shared_auth.json skips
# operator auth entirely. The quiet-retry poll loop in cli.py also watches
# the file for newer writes and re-injects mid-run, so a single successful
# re-auth by any instance propagates to every other instance in ~3 seconds
# (Option B of the design).
#
# This is one-way trust: we never verify the cookie is still valid before
# injecting. If HPCL has invalidated it, the post-injection detect_state
# falls through to NEEDS_OPERATOR_AUTH and the normal login path runs,
# which will re-write shared_auth.json on success.


_SLOT_RE = re.compile(r"^op[1-9]\d*$")


def _shared_auth_path() -> Path:
    """Disk location of the shared auth JSON. Single file per operator
    slot when BOOKING_BOT_OPERATOR_SLOT is set (orchestrator-spawned
    bot); falls back to the legacy unslotted filename for bare-bot
    mode. Malformed slot values fall back to the legacy path rather
    than writing to an attacker-chosen location."""
    slot = os.environ.get(config.OPERATOR_SLOT_ENV, "")
    if slot and _SLOT_RE.match(slot):
        return Path(config.ROOT) / f"shared_auth-{slot}.json"
    return Path(config.ROOT) / config.SHARED_AUTH_FILENAME


def write_shared_auth_state(page: Page) -> None:
    """Snapshot the current page-context's HPCL cookies and write them to
    shared_auth.json atomically. Called by auth.py after every successful
    operator login so other parallel instances can skip their own OTP.

    Atomic write via <path>.tmp + os.replace — the NTFS rename is atomic
    for same-FS replaces, so a reader never sees a half-written file.

    Never raises: a failed write only costs us the cross-instance sync for
    this particular auth event, not the auth itself."""
    try:
        ctx = page.context
        all_cookies = ctx.cookies()
        # Filter to HPCL-origin cookies only. No point exporting unrelated
        # cookies (ads, analytics) — they're dead weight and bloat the file.
        hpcl_cookies = [
            c for c in all_cookies
            if "hpchatbot.hpcl.co.in" in (c.get("domain") or "")
            or "hpcl.co.in" in (c.get("domain") or "")
        ]
        payload = {
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "origin": "https://hpchatbot.hpcl.co.in",
            "cookies": hpcl_cookies,
        }
        path = _shared_auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
        log.info(
            f"wrote {config.SHARED_AUTH_FILENAME}: {len(hpcl_cookies)} HPCL "
            f"cookie(s) available for other parallel instances"
        )
    except Exception as e:
        log.warning(
            f"write_shared_auth_state failed: {type(e).__name__}: {e} — "
            f"parallel instances will still work but won't share this auth"
        )


def read_shared_auth_state() -> dict | None:
    """Read shared_auth.json. Returns the parsed dict if it exists, is
    well-formed, and is less than SHARED_AUTH_MAX_AGE_S old. Returns None
    otherwise. Safe to call from any instance at any time — retries on
    PermissionError (another instance may be mid os.replace on Windows)
    and swallows corruption as a None return so a bad file never crashes
    the bot."""
    path = _shared_auth_path()
    for attempt in range(3):
        try:
            if not path.exists():
                return None
            raw = path.read_text()
        except PermissionError:
            # Windows: os.replace from another writer briefly blocks us.
            time.sleep(0.1)
            continue
        except OSError:
            return None
        try:
            payload = json.loads(raw)
            written_at = datetime.fromisoformat(payload["written_at_utc"])
            age = (datetime.now(timezone.utc) - written_at).total_seconds()
            if age < 0 or age > config.SHARED_AUTH_MAX_AGE_S:
                return None
            cookies = payload.get("cookies")
            if not isinstance(cookies, list) or not cookies:
                return None
            return payload
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
    return None


def inject_shared_auth_cookies(context: BrowserContext) -> int:
    """Inject cookies from shared_auth.json into the given context if any
    are available and fresh. Returns the number of cookies actually
    injected (0 if the file is missing, stale, corrupt, or add_cookies
    itself failed). Prints a visible INFO line on success so the operator
    can confirm the cross-instance share is live."""
    shared = read_shared_auth_state()
    if not shared:
        return 0
    cookies = shared["cookies"]
    try:
        context.add_cookies(cookies)
    except Exception as e:
        log.warning(
            f"inject_shared_auth_cookies: add_cookies failed "
            f"({type(e).__name__}: {e}); falling back to normal auth"
        )
        return 0
    log.info(
        f"injected {len(cookies)} shared HPCL cookie(s) from "
        f"{config.SHARED_AUTH_FILENAME} "
        f"(written_at={shared.get('written_at_utc')}) — "
        f"attempting to skip operator auth this run"
    )
    return len(cookies)


def clear_auth_cooldown() -> None:
    """Delete the persisted auth timestamp so the next login_if_needed
    call will accept a phone/OTP submission. Called ONLY from the
    Section 5 session-dead path where the operator has explicitly been
    alarmed and the bot needs to accept a fresh OTP even though less
    than AUTH_COOLDOWN_S has elapsed since the last successful auth."""
    path = _last_auth_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"clear_auth_cooldown: could not delete {path}: {e}")


def start_browser(
    headless: bool = False,
    use_system_chrome: bool = False,
    profile_suffix: str | None = None,
) -> tuple[Playwright, Browser | None, BrowserContext, Page]:
    """Launch Chromium against a persistent user-data dir so that cookies,
    local storage, and service-worker caches survive across runs. This
    lets the bot skip operator-phone/OTP re-entry when HPCL still
    considers the session active from a previous launch.

    headless: when True, Chrome runs without a visible window. The
        persistent profile still works — cookies from previous headed
        runs are reused. Suitable for scripted / background execution
        via --headless.

    use_system_chrome: when False (default for the shareable .exe), the
        bundled Chromium 1134 is used. When True, Playwright launches the
        operator's installed Google Chrome via channel="chrome" instead.
        Bundled Chromium adds ~345 MB to the PyInstaller bundle but is
        the only browser whose persistent user-data dir reliably survives
        across runs on client machines, so we default to it.

    Returns (pw, None, ctx, page). The Browser slot is None because
    launch_persistent_context gives back a BrowserContext directly.
    Callers must close ctx and stop pw at shutdown.

    Profile dir lives at config.ROOT / <name> — next to the .exe in
    frozen mode, at the repo root when running from source.

    Does NOT pre-reload. The reload-on-missing-chat logic lives in
    get_chat_frame — only reload once we've confirmed the chat hasn't
    rendered, instead of reloading eagerly before the initial load has
    finished (which just re-fetches from cache and lands us back in the
    same half-initialized state).
    """
    pw = sync_playwright().start()
    profile_name = PROFILE_DIR_NAME if use_system_chrome else CHROMIUM_PROFILE_DIR_NAME
    if profile_suffix:
        profile_name = f"{profile_name}-{profile_suffix}"
    profile_dir = config.ROOT / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    global _active_profile_dir
    _active_profile_dir = profile_dir
    # Prominent log so operators running parallel instances can visually
    # confirm which profile this terminal is driving. Shown at INFO so it
    # appears in the console even without --debug.
    log.info(f"using profile dir: {profile_dir}")
    launch_kwargs: dict = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "viewport": {"width": 1366, "height": 850},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    }
    if use_system_chrome:
        launch_kwargs["channel"] = "chrome"
    else:
        # HPCL's PWA stalls on first load in bundled Chromium when Playwright's
        # default automation flags are present — navigator.webdriver=true and
        # --enable-automation trip the site's service worker, #scroller stays
        # empty, bot reloads forever. Strip them for bundled chromium runs.
        launch_kwargs["ignore_default_args"] = ["--enable-automation"]
        launch_kwargs["args"] = [
            "--disable-blink-features=AutomationControlled",
        ]
    try:
        ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
    except Exception as launch_err:
        msg = str(launch_err)
        # Failure path must unset _active_profile_dir so a later
        # _last_auth_path() caller doesn't read/write a cooldown file
        # inside a profile whose browser never actually came up.
        _active_profile_dir = None
        try:
            pw.stop()
        except Exception:
            pass
        # Chromium refuses to open a user-data-dir that another chrome.exe
        # already holds. It prints "Opening in existing browser session"
        # to stdout and exits, which Playwright surfaces as a generic
        # TargetClosedError. Detect that specific pattern and re-raise as
        # ProfileInUseError so the CLI can show an actionable message
        # instead of dumping a 40-line traceback.
        if "Opening in existing browser session" in msg or (
            "TargetClosedError" in type(launch_err).__name__
            and "launch_persistent_context" in msg
        ):
            raise ProfileInUseError(
                f"Chromium profile already in use: {profile_dir}\n\n"
                f"Another booking_bot instance is running against this same "
                f"profile directory (Chromium enforces a single-writer lock "
                f"on its user-data-dir).\n\n"
                f"To run multiple bots in parallel:\n"
                f"  1. Keep the existing terminal running, AND\n"
                f"  2. In this terminal, pick a different --profile-suffix:\n"
                f"       python -m booking_bot <input> --profile-suffix 2\n\n"
                f"If no other bot is running, a previous run may have crashed "
                f"without releasing its lock. Close any stray chrome.exe "
                f"processes that still have this profile open, then retry."
            ) from launch_err
        # Playwright's error when Chrome/Chromium can't be found mentions
        # "Executable doesn't exist" and a chromium-1134 path. Re-raise as
        # a ChromeNotInstalledError with a download link so the GUI bootstrap
        # can show the operator a clear, actionable dialog instead of a
        # cryptic Playwright traceback.
        if use_system_chrome and (
            "Executable doesn't exist" in msg
            or "chromium-1134" in msg
            or "channel" in msg.lower()
        ):
            raise ChromeNotInstalledError(
                "Google Chrome is not installed on this computer.\n\n"
                "The HP Gas Booking Bot uses your installed Chrome to talk\n"
                "to HPCL. Please install Chrome from:\n\n"
                "    https://www.google.com/chrome/\n\n"
                "then launch the bot again."
            ) from launch_err
        raise
    # launch_persistent_context opens one blank tab by default — reuse it.
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    mode = "headless" if headless else "headed"
    channel = "system-chrome" if use_system_chrome else "bundled-chromium"
    log.info(
        f"browser launched ({mode}, {channel}, profile={profile_dir.name})"
    )
    # Shared-auth cookie transplant: before the first HPCL navigation,
    # inject any cookies written by a previously-authed parallel instance.
    # If this is a fresh laptop (no shared_auth.json) or the file is stale,
    # this is a no-op and the normal OTP flow runs. If cookies are injected
    # and still valid on HPCL's side, the next detect_state lands straight
    # on MAIN_MENU / READY_FOR_CUSTOMER and login_if_needed becomes a nop.
    inject_shared_auth_cookies(ctx)
    log.info(f"navigating to {config.URL}")
    page.goto(config.URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
    install_gateway_listener(page)
    return pw, None, ctx, page


def get_chat_frame(page: Page) -> Frame:
    """Return the page's main frame after waiting for the chat DOM to render.

    The direct-URL approach (config.URL points straight to the hpchatbot.hpcl
    PWA) means all of the chat widgets — #scroller, textarea.replybox,
    button.reply-submit, button.dynamic-message-button — live on the
    top-level document. No iframe drilling.

    We wait for SEL_SCROLLER to be attached AND non-empty before returning.
    HPCL's PWA sometimes stalls on first load — the SPA's service worker
    and initial fetches complete, but the chat widgets stay blank until a
    page reload kicks them into rendering. Operators historically fixed
    this by hitting F5. We do it automatically: if the scroller doesn't
    show any children within a few seconds, call page.reload() and try
    again. Retry up to 3 times before giving up.

    Raises IframeLostError if the scroller never becomes populated."""
    max_reloads = 3
    per_attempt_timeout_s = 10.0
    poll_interval_s = 0.5

    for attempt in range(max_reloads + 1):
        if attempt > 0:
            log.warning(
                f"get_chat_frame: chat still empty after attempt {attempt}; "
                f"reloading page (kick #{attempt})"
            )
            try:
                page.reload(wait_until="domcontentloaded", timeout=60_000)
            except PWTimeoutError as e:
                log.warning(f"get_chat_frame: reload {attempt} timed out: {e}")
                continue
            except Exception as e:
                # Playwright can raise a plain Error (not PWTimeoutError) when
                # the reload is cancelled mid-flight — the classic is
                # `net::ERR_ABORTED; maybe frame was detached?` after a 502
                # response body. Treat those the same as a timeout: log,
                # skip the settle, and let the next kick try again. Without
                # this catch the raw Playwright Error escapes get_chat_frame
                # and crashes _run_session_attempt's startup retry loop.
                log.warning(
                    f"get_chat_frame: reload {attempt} failed "
                    f"({type(e).__name__}: {e}); continuing to next kick"
                )
                continue
            page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)

        deadline = time.monotonic() + per_attempt_timeout_s
        while time.monotonic() < deadline:
            try:
                if _scroller_populated(page):
                    log.info(
                        f"get_chat_frame: chat rendered "
                        f"(attempt {attempt + 1}/{max_reloads + 1})"
                    )
                    return page.main_frame
            except Exception as e:
                log.debug(f"get_chat_frame poll error: {e}")
            time.sleep(poll_interval_s)

    raise IframeLostError(
        f"chat scroller never populated after {max_reloads + 1} load attempts "
        f"(total {(max_reloads + 1) * per_attempt_timeout_s:.0f}s)"
    )


def _scroller_populated(page: Page) -> bool:
    """True when #scroller exists AND has at least one child element.

    An empty scroller means the PWA loaded its HTML shell but hasn't
    rendered any chat bubbles yet — either because it's still initialising
    or because it needs a reload to kick things."""
    return bool(page.evaluate(
        f"""() => {{
          const s = document.querySelector('{config.SEL_SCROLLER}');
          return !!(s && s.children && s.children.length > 0);
        }}"""
    ))


def install_gateway_listener(page: Page) -> None:
    """Install page.on('response') and page.on('framenavigated') listeners that
    flip _gateway_error_seen True when:
      - any response from the hpchatbot.hpcl.co.in domain has status in
        GATEWAY_STATUS_CODES
      - any frame navigates to a URL whose path matches GATEWAY_URL_RE

    The flag is read AND reset by chat.wait_until_settled on every call."""

    def _on_response(response):
        global _gateway_error_seen
        try:
            url = response.url
            if "hpchatbot.hpcl.co.in" in url and response.status in config.GATEWAY_STATUS_CODES:
                log.warning(f"gateway error response: {response.status} {url}")
                _gateway_error_seen = True
        except Exception:
            pass  # ignore listener-thread errors

    def _on_framenav(frame):
        global _gateway_error_seen
        try:
            if config.GATEWAY_URL_RE.search(frame.url or ""):
                log.warning(f"frame navigated to gateway-ish url: {frame.url}")
                _gateway_error_seen = True
        except Exception:
            pass

    page.on("response", _on_response)
    page.on("framenavigated", _on_framenav)


def _try_in_place_frame(page: Page) -> Frame | None:
    """Return the current main frame if the chat scroller is still rendered
    and detect_state reports a known state. Return None if the frame is
    broken or the state is UNKNOWN even after polling — the caller should
    reload in that case.

    Polls for up to config.IN_PLACE_POLL_S seconds before giving up.
    Reloads are session-destroying when HPCL is mid-flap; staying in place
    and waiting is almost always cheaper than a reload + re-auth, so we
    poll hard before falling back.

    Used by recover_session to avoid a full page.reload() on transient
    single-request 502s, which is what historically destroyed the session
    and triggered repeated OTP prompts.
    """
    from booking_bot import chat  # late import — chat depends on browser
    deadline = time.monotonic() + config.IN_PLACE_POLL_S
    poll_interval_s = 1.0
    last_state = "UNKNOWN"
    while time.monotonic() < deadline:
        try:
            if not _scroller_populated(page):
                last_state = "<empty>"
            else:
                frame = page.main_frame
                state = chat.detect_state(frame)
                last_state = state
                if state != "UNKNOWN":
                    log.info(
                        f"recover_session: in-place state={state!r}; "
                        f"skipping reload"
                    )
                    return frame
        except Exception as e:
            log.debug(
                f"recover_session: in-place poll error: "
                f"{type(e).__name__}: {e}"
            )
        time.sleep(poll_interval_s)
    log.info(
        f"recover_session: in-place gave up after "
        f"{config.IN_PLACE_POLL_S}s (last={last_state!r})"
    )
    return None


def recover_session(
    page: Page,
    operator_phone: str,
    get_otp: Callable[[], str],
) -> Frame:
    """Attempt to recover a wedged/erroring chat session.

    Recovery strategy (gateway-aware, added after the OTP-flood incident):

      1. Wait GATEWAY_QUIESCE_S for HPCL's upstream to recover. Reloading
         into an ongoing 502 burst is what caused the bot to reload, hit a
         second 502, lose its session, and prompt for OTP on every row.
      2. Try to detect state on the CURRENT page. If the frame is still
         alive and reports a recognised state (MAIN_MENU, BOOK_FOR_OTHERS_
         MENU, READY_FOR_CUSTOMER, etc.), skip the reload entirely. This
         is the fast path for transient single-request 502s.
      3. Only if the in-place read fails (IframeLostError / UNKNOWN /
         detection throws) do we fall back to page.reload(). If the reload
         itself trips the gateway flag, wait GATEWAY_RELOAD_WAIT_S before
         letting the caller retry.

    Raises:
      GatewayError if the reload itself times out or also hits a 502.
      FatalError if detect_state returns UNKNOWN (unrecognized page).
      ChatStuckError if we exceed MAX_NAV_HOPS without reaching
        READY_FOR_CUSTOMER.
    """
    # Late imports to avoid module-load cycles.
    from booking_bot import auth, chat
    from booking_bot.exceptions import FatalError

    log.warning(
        f"recover_session: waiting {config.GATEWAY_QUIESCE_S}s for gateway "
        f"to quiesce before attempting recovery"
    )
    reset_gateway_flag()
    time.sleep(config.GATEWAY_QUIESCE_S)

    frame = _try_in_place_frame(page)
    if frame is None:
        log.warning("recover_session: in-place frame unusable; reloading page")
        reset_gateway_flag()
        try:
            page.reload(wait_until="domcontentloaded", timeout=60_000)
        except PWTimeoutError as e:
            raise GatewayError(f"reload timed out: {e}") from e
        page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
        if gateway_flag():
            log.warning(
                f"recover_session: reload hit gateway error; waiting "
                f"{config.GATEWAY_RELOAD_WAIT_S}s before surfacing failure"
            )
            time.sleep(config.GATEWAY_RELOAD_WAIT_S)
        frame = get_chat_frame(page)
    chat.wait_until_settled(frame)

    for hop in range(config.MAX_NAV_HOPS):
        state = chat.detect_state(frame)
        log.info(f"recover_session hop {hop + 1}: state={state}")
        if state == "READY_FOR_CUSTOMER":
            return frame
        if state == "BOOK_FOR_OTHERS_MENU":
            chat.click_option(frame, config.STATE_PATTERNS["BOOK_FOR_OTHERS_MENU"])
        elif state == "MAIN_MENU":
            chat.click_option(frame, config.STATE_PATTERNS["MAIN_MENU"])
        elif state == "BOOKING_IN_PROGRESS":
            pass  # the wait_until_settled below gives the bot time to finish
        elif state == "NEEDS_OPERATOR_OTP":
            chat.send_text(frame, get_otp())
        elif state == "NEEDS_OPERATOR_AUTH":
            auth.full_auth(frame, operator_phone, get_otp)
        else:  # UNKNOWN or any new state we haven't coded for
            raise FatalError(
                f"unknown chat state during recovery: {state}; visible: "
                f"{chat.dump_visible_state(frame)}"
            )
        chat.wait_until_settled(frame)

    from booking_bot.exceptions import ChatStuckError as _CSE
    raise _CSE(
        f"recovery exceeded MAX_NAV_HOPS={config.MAX_NAV_HOPS}; "
        f"visible: {chat.dump_visible_state(frame)}"
    )
