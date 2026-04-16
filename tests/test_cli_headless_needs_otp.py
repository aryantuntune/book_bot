"""Regression tests for the headless session-dead escape hatch.

When an orchestrator chunk running --headless exits its quiet-retry loop
with outcome="needs_otp" (session is dead, fresh OTP would help), the
old behavior called login_if_needed again — which typed the operator
phone, triggered an OTP SMS with no listener, then hung in
wait_until_settled for up to STUCK_THRESHOLD_S. Across M parallel
same-account clones this produced M SMSes and M hung processes.

The fix: in headless mode, mark the chunk failed and raise FatalError
before re-calling login_if_needed. These tests lock that in."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from booking_bot import cli
from booking_bot.exceptions import FatalError


def _fake_store(pending: int = 5):
    s = MagicMock()
    s.summary.return_value = {
        "total": pending, "done": 0, "pending": pending, "issue": 0,
        "success": 0, "ekyc": 0, "not_registered": 0, "payment_pending": 0,
    }
    s.input_path = ""
    return s


def _mk_handles():
    pw = MagicMock(name="pw")
    browser_obj = MagicMock(name="browser")
    ctx = MagicMock(name="ctx")
    page = MagicMock(name="page")
    frame = MagicMock(name="frame")
    return (pw, browser_obj, ctx, page, frame)


def test_headless_needs_otp_raises_fatal_without_retyping_phone(monkeypatch, caplog):
    """Headless chunk that exhausts quiet retry must NOT re-call
    login_if_needed (which would type phone → OTP SMS flood + hang).
    _run_session_attempt catches FatalError and converts it to sys.exit(1),
    so the test asserts SystemExit and verifies the FATAL log + call counts."""
    import logging as _logging

    monkeypatch.setattr(cli, "_HEADLESS", True)

    login_if_needed_calls = []

    def fake_login_if_needed(frame, phone, get_otp):
        login_if_needed_calls.append(1)
        return "cooldown_wait"

    monkeypatch.setattr(cli, "login_if_needed", fake_login_if_needed)
    monkeypatch.setattr(
        cli, "_quiet_retry_until_alive_or_dead",
        lambda page, pb, store: "needs_otp",
    )
    clear_cooldown_calls = []
    monkeypatch.setattr(
        cli.browser, "clear_auth_cooldown",
        lambda: clear_cooldown_calls.append(1),
    )
    store = _fake_store(pending=5)
    store.write_issue = lambda *a, **kw: None
    pb = MagicMock(name="pb")
    args = MagicMock(name="args", keep_open=False)

    with caplog.at_level(_logging.ERROR, logger="cli"):
        with pytest.raises(SystemExit) as exc_info:
            cli._run_session_attempt(store, args, pb, _mk_handles())

    assert exc_info.value.code == 1
    assert any(
        "headless" in rec.message.lower() and "otp" in rec.message.lower()
        for rec in caplog.records
    ), f"expected FATAL log mentioning headless+OTP, got {[r.message for r in caplog.records]}"
    assert len(login_if_needed_calls) == 1, (
        "login_if_needed must NOT be called a second time in headless mode — "
        "that path types the operator phone and floods OTP SMS"
    )
    assert len(clear_cooldown_calls) == 0, (
        "clear_auth_cooldown must NOT be called in the headless needs_otp path"
    )


def test_headed_needs_otp_still_retries_with_fresh_auth(monkeypatch):
    """In headed mode (interactive operator present), needs_otp must still
    clear the cooldown and retry login_if_needed — that's how the operator's
    fresh OTP unblocks the stuck session."""
    monkeypatch.setattr(cli, "_HEADLESS", False)

    call_log = []

    def fake_login_if_needed(frame, phone, get_otp):
        call_log.append("login_if_needed")
        if len(call_log) == 1:
            return "cooldown_wait"
        return "authed_freshly"

    monkeypatch.setattr(cli, "login_if_needed", fake_login_if_needed)
    monkeypatch.setattr(
        cli, "_quiet_retry_until_alive_or_dead",
        lambda page, pb, store: "needs_otp",
    )
    monkeypatch.setattr(
        cli.browser, "clear_auth_cooldown",
        lambda: call_log.append("clear_cooldown"),
    )
    monkeypatch.setattr(
        cli.browser, "get_chat_frame",
        lambda page: MagicMock(name="new_frame"),
    )
    monkeypatch.setattr(cli, "_recover_with_playbook",
                        lambda *a, **kw: MagicMock(name="recovered_frame"))
    replay_log = []
    monkeypatch.setattr(
        cli.playbook_mod, "replay_auth",
        lambda frame, pb: replay_log.append("replay_auth"),
    )
    # Short-circuit the row loop so we don't need a real store + browser.
    store = _fake_store(pending=0)
    store.pending_rows = lambda: iter([])
    pb = MagicMock(name="pb")
    args = MagicMock(name="args", keep_open=False)

    # Run should complete cleanly (returns None) after the successful
    # re-auth. Any exception here would mean we broke the headed path.
    cli._run_session_attempt(store, args, pb, _mk_handles())

    assert call_log[:3] == [
        "login_if_needed", "clear_cooldown", "login_if_needed"
    ], f"expected ordered sequence but got {call_log}"
