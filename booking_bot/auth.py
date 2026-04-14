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
    FatalError,
    OptionNotFoundError,
    RestartableFatalError,
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
    Short polls also let Ctrl-C break out within ~500ms."""
    import time
    deadline = time.monotonic() + total_timeout_s
    state = "UNKNOWN"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        state = chat.detect_state(frame)
        if state != "UNKNOWN":
            log.info(f"state resolved after {attempt} polls: {state!r}")
            return state
        time.sleep(0.5)
    log.warning(f"state still UNKNOWN after {total_timeout_s}s and {attempt} polls")
    return state


def login_if_needed(
    frame: Frame, operator_phone: str, get_otp: Callable[[], str],
) -> None:
    """Bring the chat to a logged-in state, doing as little work as possible.

    - NEEDS_OPERATOR_AUTH → type phone, then OTP
    - NEEDS_OPERATOR_OTP  → type OTP only (phone already accepted)
    - anything else       → session already active, no-op

    Unlike full_auth, this does NOT walk any menu. Use it when the rest of
    the navigation is handled by a playbook."""
    import time
    from booking_bot import browser  # late import to avoid cycle

    state = _wait_for_known_state(frame)
    log.info(f"login_if_needed: detected state={state!r}")

    # Recent-auth guard: HPCL's PWA occasionally flashes the login OR
    # OTP-entry screen briefly after a reload that followed a gateway hiccup,
    # even though the server-side session is still valid. If we authed
    # successfully less than RECENT_AUTH_WINDOW_S ago, wait once and re-read
    # state before prompting the operator for credentials they just typed.
    #
    # Both NEEDS_OPERATOR_AUTH and NEEDS_OPERATOR_OTP are covered: the OTP
    # state is sometimes matched from stale "OTP sent to..." chat bubbles
    # left in the scroller from a prior auth cycle, which is a classifier
    # false positive and should NOT prompt the operator for another OTP.
    if state in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"):
        age = browser.last_auth_age_s()
        if age is not None and age < config.RECENT_AUTH_WINDOW_S:
            log.warning(
                f"{state} detected {age:.0f}s after a successful auth — "
                f"waiting {config.RECENT_AUTH_RECHECK_S}s to see if HPCL "
                f"settles back into the logged-in state"
            )
            time.sleep(config.RECENT_AUTH_RECHECK_S)
            state = _wait_for_known_state(frame)
            log.info(f"login_if_needed: after re-check, state={state!r}")
            if state in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"):
                # The recheck didn't help — HPCL really is back on the login
                # screen. This is the OTP-flood pattern. Increment the
                # rapid-reauth counter; if we've done this too many times in
                # a row, raise a RestartableFatalError so cli.main() can
                # close-and-relaunch the browser (which has historically
                # been the operator's manual fix for this stuck state).
                count = browser.note_rapid_reauth()
                log.warning(
                    f"rapid re-auth detected (#{count}/"
                    f"{config.MAX_CONSECUTIVE_REAUTHS}): {state} persisted "
                    f"past the recheck"
                )
                if count >= config.MAX_CONSECUTIVE_REAUTHS:
                    raise RestartableFatalError(
                        f"OTP-flood circuit breaker tripped: HPCL kept "
                        f"flashing the login/OTP screen within "
                        f"{config.RECENT_AUTH_WINDOW_S}s of the last "
                        f"successful auth {count} times in a row. Triggering "
                        f"in-process browser restart — the persistent "
                        f"profile retains the session cookies, so a fresh "
                        f"launch usually lands on a live session."
                    )

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
    elif state == "NEEDS_OPERATOR_OTP":
        otp = get_otp()
        log.info("typing OTP (not logged)")
        chat.send_text(frame, otp)
        chat.wait_until_settled(frame)
        browser.mark_auth_success()
    else:
        log.info("session already active; skipping operator auth")
        browser.mark_auth_success()


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
