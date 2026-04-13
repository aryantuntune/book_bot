"""Playwright lifecycle, iframe drilling, gateway listener, and recover_session.

This file is split across three tasks: Task 11 adds start_browser +
get_chat_frame, Task 12 adds the gateway listener, Task 19 adds recover_session.
"""
from __future__ import annotations

import logging
import time
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
from booking_bot.exceptions import GatewayError, IframeLostError

log = logging.getLogger("browser")

# Module-level state for the gateway listener (Task 12). One-process bot.
_gateway_error_seen = False


def reset_gateway_flag() -> None:
    global _gateway_error_seen
    _gateway_error_seen = False


def gateway_flag() -> bool:
    return _gateway_error_seen


PROFILE_DIR_NAME = ".chrome-profile"


def start_browser() -> tuple[Playwright, Browser | None, BrowserContext, Page]:
    """Launch a visible Chromium against a persistent user-data dir so that
    cookies, local storage, and service-worker caches survive across runs.
    This lets the bot skip operator-phone/OTP re-entry when HPCL still
    considers the session active from a previous launch.

    Returns (pw, None, ctx, page). The Browser slot is None because
    launch_persistent_context gives back a BrowserContext directly — there
    is no separate Browser handle to close. Callers must close ctx and
    stop pw at shutdown.

    Profile dir lives at config.ROOT / .chrome-profile, so it ends up next
    to the .exe in frozen mode and at the repo root when running from
    source. gitignored so we never commit cookies.

    Does NOT pre-reload. The reload-on-missing-chat logic lives in
    get_chat_frame — only reload once we've confirmed the chat hasn't
    rendered, instead of reloading eagerly before the initial load has
    finished (which just re-fetches from cache and lands us back in the
    same half-initialized state).
    """
    pw = sync_playwright().start()
    profile_dir = config.ROOT / PROFILE_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        viewport={"width": 1366, "height": 850},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    )
    # launch_persistent_context opens one blank tab by default — reuse it.
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    log.info(f"browser launched (persistent profile: {profile_dir.name})")
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


def recover_session(
    page: Page,
    operator_phone: str,
    get_otp: Callable[[], str],
) -> Frame:
    """Attempt to recover a wedged/erroring chat session. The server-side
    session typically survives the reload — we only re-run operator auth if
    detect_state sees NEEDS_OPERATOR_AUTH. Navigation-first, re-auth as a
    last resort.

    Raises:
      GatewayError if the reload itself times out.
      FatalError if detect_state returns UNKNOWN (unrecognized page).
      ChatStuckError if we exceed MAX_NAV_HOPS without reaching
        READY_FOR_CUSTOMER.
    """
    # Late imports to avoid module-load cycles.
    from booking_bot import auth, chat
    from booking_bot.exceptions import FatalError

    log.warning("recover_session: reloading page")
    try:
        page.reload(wait_until="domcontentloaded", timeout=60_000)
    except PWTimeoutError as e:
        raise GatewayError(f"reload timed out: {e}") from e
    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)

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
