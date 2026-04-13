"""Playwright lifecycle, iframe drilling, gateway listener, and recover_session.

This file is split across three tasks: Task 11 adds start_browser +
get_chat_frame, Task 12 adds the gateway listener, Task 19 adds recover_session.
"""
from __future__ import annotations

import logging
import time

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
from booking_bot.exceptions import IframeLostError

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
