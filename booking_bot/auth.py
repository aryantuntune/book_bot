"""Operator authentication: type operator phone, prompt for OTP, walk the menu
to READY_FOR_CUSTOMER. Called once at startup by cli.main() and again by
browser.recover_session() if the chat is seen in NEEDS_OPERATOR_AUTH after
a reload."""
from __future__ import annotations

import logging
from typing import Callable

from playwright.sync_api import Frame

from booking_bot import chat, config
from booking_bot.exceptions import AuthFailedError, OptionNotFoundError

log = logging.getLogger("auth")


def full_auth(frame: Frame, operator_phone: str, get_otp: Callable[[], str]) -> None:
    """Complete operator auth: phone → OTP → walk AUTH_NAV_SEQUENCE until the
    chat is in READY_FOR_CUSTOMER. Raises AuthFailedError on any menu miss."""
    log.info(f"auth: typing operator phone {operator_phone[:3]}XXXXXXX")
    chat.send_text(frame, operator_phone)
    chat.wait_until_settled(frame)

    otp = get_otp()
    log.info("auth: typing OTP (not logged)")
    chat.send_text(frame, otp)
    chat.wait_until_settled(frame)

    navigate_to_book_for_others(frame)


def navigate_to_book_for_others(frame: Frame) -> None:
    """Walk config.AUTH_NAV_SEQUENCE. Each entry is a priority list of regex
    patterns; we click the first matching option and settle. If no option
    matches one of the groups, we raise AuthFailedError so the caller can
    decide whether to recover."""
    for step_idx, patterns in enumerate(config.AUTH_NAV_SEQUENCE):
        try:
            label = chat.click_option(frame, patterns)
            log.info(f"auth nav step {step_idx + 1}: clicked {label!r}")
        except OptionNotFoundError as e:
            raise AuthFailedError(
                f"auth nav step {step_idx + 1} failed: {e}"
            ) from e
        chat.wait_until_settled(frame)

    state = chat.detect_state(frame)
    if state != "READY_FOR_CUSTOMER":
        raise AuthFailedError(
            f"after AUTH_NAV_SEQUENCE, detect_state={state!r}; "
            f"visible: {chat.dump_visible_state(frame)}"
        )
    log.info("auth: landed on READY_FOR_CUSTOMER")
