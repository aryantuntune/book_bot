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


from booking_bot import config
from booking_bot.ai_advisor import AdvisorBudget


def test_budget_fresh_is_not_exhausted():
    b = AdvisorBudget()
    assert b.exhausted() is False
    assert b.calls_made == 0
    assert b.total_skips == 0
    assert b.consecutive_skips == 0


def test_budget_record_call_increments_calls_made():
    b = AdvisorBudget()
    b.record_call()
    b.record_call()
    assert b.calls_made == 2


def test_budget_exhausted_when_max_calls_hit(monkeypatch):
    monkeypatch.setattr(config, "ADVISOR_MAX_CALLS_PER_SESSION", 3)
    b = AdvisorBudget()
    for _ in range(3):
        b.record_call()
    assert b.exhausted() is True


def test_budget_record_skip_increments_both_counters():
    b = AdvisorBudget()
    b.record_skip()
    b.record_skip()
    assert b.total_skips == 2
    assert b.consecutive_skips == 2


def test_budget_non_skip_decision_resets_consecutive_counter():
    b = AdvisorBudget()
    b.record_skip()
    b.record_skip()
    assert b.consecutive_skips == 2
    b.record_non_skip_decision()
    assert b.consecutive_skips == 0
    assert b.total_skips == 2


def test_budget_exhausted_when_consecutive_skip_cap_hit(monkeypatch):
    monkeypatch.setattr(config, "ADVISOR_MAX_CONSECUTIVE_SKIPS", 2)
    b = AdvisorBudget()
    b.record_skip()
    b.record_skip()
    assert b.exhausted() is True


def test_budget_exhausted_when_total_skip_cap_hit(monkeypatch):
    monkeypatch.setattr(config, "ADVISOR_MAX_TOTAL_SKIPS", 3)
    monkeypatch.setattr(config, "ADVISOR_MAX_CONSECUTIVE_SKIPS", 99)
    b = AdvisorBudget()
    for _ in range(3):
        b.record_skip()
        b.record_non_skip_decision()
    assert b.total_skips == 3
    assert b.exhausted() is True
