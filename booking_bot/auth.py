"""Operator authentication: type operator phone, prompt for OTP, walk the menu
to READY_FOR_CUSTOMER. Called once at startup by cli.main() and again by
browser.recover_session() if the chat is seen in NEEDS_OPERATOR_AUTH after
a reload."""
from __future__ import annotations

import logging
from typing import Callable

from playwright.sync_api import Frame

from booking_bot import chat, config
from booking_bot.exceptions import (
    AuthFailedError,
    IframeLostError,
    OptionNotFoundError,
)

log = logging.getLogger("auth")


def _wait_for_known_state(frame: Frame, total_timeout_s: float = 20.0) -> str:
    """Poll detect_state until it returns something other than UNKNOWN, or
    until total_timeout_s elapses. On first page load, the chat renders
    asynchronously and UNKNOWN is temporary — it resolves once the welcome
    + menu messages arrive.

    This polls detect_state directly (a single fast JS evaluate) rather
    than calling wait_until_settled, because settling has its own 60s
    timeout that would dominate here when the scroller is already quiet.
    Short polls also let Ctrl-C break out within ~500ms.

    Transient IframeLostError during polling (HPCL sometimes navigates
    mid-eval on the first load — e.g. a stale profile cookie triggering
    a redirect) is swallowed as UNKNOWN so the next poll can re-try
    against the fresh execution context. Without this swallow, a single
    mid-poll navigation would propagate up as UNHANDLED and kill the run
    before we ever check whether the session was still alive."""
    import time
    deadline = time.monotonic() + total_timeout_s
    state = "UNKNOWN"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            state = chat.detect_state(frame)
        except IframeLostError as e:
            log.warning(
                f"_wait_for_known_state: transient IframeLostError on poll "
                f"{attempt} ({e}); treating as UNKNOWN and retrying"
            )
            state = "UNKNOWN"
        if state != "UNKNOWN":
            log.info(f"state resolved after {attempt} polls: {state!r}")
            return state
        time.sleep(0.5)
    log.warning(f"state still UNKNOWN after {total_timeout_s}s and {attempt} polls")
    return state


def login_if_needed(
    frame: Frame, operator_phone: str, get_otp: Callable[[], str],
) -> str:
    """Bring the chat to a logged-in state, doing as little work as possible.

    Return values (Section 1 of the survivability design):

      "authed"          — session was already active, no work done.
      "authed_freshly"  — we just typed phone + OTP. The persistent
                          timestamp is already written.
      "cooldown_wait"   — detected NEEDS_OPERATOR_AUTH or NEEDS_OPERATOR_OTP
                          less than AUTH_COOLDOWN_S after the last successful
                          auth. Refused to type anything. Caller should
                          enter the quiet retry loop.

    The cooldown is what stops the OTP-flood pattern: every gateway flap
    used to trigger phone-number entry, and HPCL was firing ~50 SMS per
    real OTP. With the cooldown, a full day of 100+ flaps produces at
    most one real phone-number submission.

    Unlike full_auth, this does NOT walk any menu. Use it when the rest
    of the navigation is handled by a playbook."""
    from booking_bot import browser  # late import to avoid cycle

    state = _wait_for_known_state(frame)
    log.info(f"login_if_needed: detected state={state!r}")

    if state in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"):
        age = browser.last_auth_age_s()
        if age is not None and age < config.AUTH_COOLDOWN_S:
            log.warning(
                f"{state} detected {age:.0f}s after last successful auth "
                f"(cooldown = {config.AUTH_COOLDOWN_S}s) — refusing to type "
                f"operator phone to avoid OTP SMS flood. Caller should "
                f"enter quiet retry mode."
            )
            return "cooldown_wait"

    if state == "UNKNOWN":
        log.warning(
            "state is UNKNOWN — cannot tell if session is active. Proceeding "
            "anyway; the first playbook click will fail loudly if the page "
            "isn't in the expected state. Visible snapshot for debugging:"
        )
        log.warning(chat.dump_visible_state(frame))

    if state == "NEEDS_OPERATOR_AUTH":
        log.info(f"typing operator phone {operator_phone[:3]}XXXXXXX")
        chat.send_text(frame, operator_phone)
        chat.wait_until_settled(frame)
        otp = get_otp()
        log.info("typing OTP (not logged)")
        chat.send_text(frame, otp)
        chat.wait_until_settled(frame)
        browser.mark_auth_success()
        return "authed_freshly"
    elif state == "NEEDS_OPERATOR_OTP":
        otp = get_otp()
        log.info("typing OTP (not logged)")
        chat.send_text(frame, otp)
        chat.wait_until_settled(frame)
        browser.mark_auth_success()
        return "authed_freshly"
    else:
        log.info("session already active; skipping operator auth")
        browser.mark_auth_success()
        return "authed"


def full_auth(frame: Frame, operator_phone: str, get_otp: Callable[[], str]) -> None:
    """Complete operator auth: phone → OTP → walk AUTH_NAV_SEQUENCE until the
    chat is in READY_FOR_CUSTOMER. Raises AuthFailedError on any menu miss."""
    from booking_bot import browser  # late import to avoid cycle

    log.info(f"auth: typing operator phone {operator_phone[:3]}XXXXXXX")
    chat.send_text(frame, operator_phone)
    chat.wait_until_settled(frame)

    otp = get_otp()
    log.info("auth: typing OTP (not logged)")
    chat.send_text(frame, otp)
    chat.wait_until_settled(frame)
    browser.mark_auth_success()

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
