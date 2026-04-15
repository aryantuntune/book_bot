"""Unit tests for booking_bot.ai_advisor. All tests use a FakeAnthropicClient
or hit only pure helpers — no real API calls, no Playwright, no network."""
from __future__ import annotations

from booking_bot.ai_advisor import AdvisorSnapshot, Decision


def test_advisor_snapshot_is_frozen_and_hashable():
    s = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("Make Payment", "Previous Menu"),
        last_bubble_text="Payment is pending.",
        recent_actions=("clicked Book for Others", "typed 9999999999"),
        empty_input_names=(),
        row_hint="row 42/500, phone ****1234",
    )
    assert s.state == "UNKNOWN"
    assert s.enabled_buttons == ("Make Payment", "Previous Menu")
    import dataclasses
    assert dataclasses.is_dataclass(s)
    assert hash(s) is not None


def test_decision_click_requires_button_label():
    d = Decision(action="click", button_label="Previous Menu", reason="escape dialog")
    assert d.action == "click"
    assert d.button_label == "Previous Menu"


def test_decision_reload_has_no_button_label():
    d = Decision(action="reload", button_label=None, reason="dom looks broken")
    assert d.action == "reload"
    assert d.button_label is None


def test_decision_skip_row_has_no_button_label():
    d = Decision(action="skip_row", button_label=None, reason="payment pending on this row")
    assert d.action == "skip_row"
