"""Playwright lifecycle, iframe drilling, gateway listener, and recover_session.

This file is split across three tasks: Task 11 adds start_browser +
get_chat_frame, Task 12 adds the gateway listener, Task 19 adds recover_session.
"""
from __future__ import annotations

import json
import logging
import os
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
)

log = logging.getLogger("browser")

# Module-level state for the gateway listener (Task 12). One-process bot.
_gateway_error_seen = False

PROFILE_DIR_NAME = ".chrome-profile"
CHROMIUM_PROFILE_DIR_NAME = ".chromium-profile"
_LAST_AUTH_FILENAME = "last_auth.json"


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
    config.ROOT is read-through and tests monkeypatch it."""
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
    profile_dir = config.ROOT / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
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
    try:
        ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
    except Exception as launch_err:
        msg = str(launch_err)
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
            try:
                pw.stop()
            except Exception:
                pass
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
