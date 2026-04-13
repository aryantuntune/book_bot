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


def start_browser() -> tuple[Playwright, Browser, BrowserContext, Page]:
    """Launch a visible Chromium, return (pw, browser, ctx, page). Caller owns
    the handles and must call browser.close() / pw.stop() at shutdown."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 850},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    )
    page = ctx.new_page()
    log.info(f"browser launched; navigating to {config.URL}")
    page.goto(config.URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
    install_gateway_listener(page)
    return pw, browser, ctx, page


def get_chat_frame(page: Page) -> Frame:
    """Drill into iframe#webform → iframe[name='iframe'] and return the inner
    Frame. Retries internally for up to GET_FRAME_TIMEOUT_S seconds. Raises
    IframeLostError on failure."""
    deadline = time.monotonic() + config.GET_FRAME_TIMEOUT_S
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            outer_el = page.wait_for_selector(
                config.OUTER_IFRAME_SEL, timeout=5_000, state="attached",
            )
            outer_frame = outer_el.content_frame()
            if outer_frame is None:
                raise IframeLostError("outer frame has no content_frame")
            inner_el = outer_frame.wait_for_selector(
                config.INNER_IFRAME_SEL, timeout=5_000, state="attached",
            )
            inner_frame = inner_el.content_frame()
            if inner_frame is None:
                raise IframeLostError("inner frame has no content_frame")
            inner_frame.wait_for_load_state("domcontentloaded", timeout=10_000)
            return inner_frame
        except (PWTimeoutError, IframeLostError) as e:
            last_err = e
            time.sleep(0.5)
    raise IframeLostError(
        f"could not attach inner chat frame within {config.GET_FRAME_TIMEOUT_S}s: {last_err}"
    )


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
