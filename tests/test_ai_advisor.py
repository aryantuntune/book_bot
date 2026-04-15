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


from booking_bot.ai_advisor import validate_decision


def _snap(buttons=()):
    return AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=tuple(buttons),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )


def test_validate_click_with_matching_button_ok():
    snap = _snap(["Make Payment", "Previous Menu"])
    d = Decision(action="click", button_label="Previous Menu", reason="x")
    assert validate_decision(d, snap) is True


def test_validate_click_case_insensitive_match_ok():
    snap = _snap(["Make Payment", "Previous Menu"])
    d = Decision(action="click", button_label="previous menu", reason="x")
    assert validate_decision(d, snap) is True


def test_validate_click_label_not_in_enabled_buttons_fails():
    snap = _snap(["Make Payment", "Previous Menu"])
    d = Decision(action="click", button_label="Main Menu", reason="x")
    assert validate_decision(d, snap) is False


def test_validate_click_with_none_button_label_fails():
    snap = _snap(["A", "B"])
    d = Decision(action="click", button_label=None, reason="x")
    assert validate_decision(d, snap) is False


def test_validate_reload_always_ok():
    snap = _snap([])
    d = Decision(action="reload", button_label=None, reason="x")
    assert validate_decision(d, snap) is True


def test_validate_skip_row_always_ok():
    snap = _snap([])
    d = Decision(action="skip_row", button_label=None, reason="x")
    assert validate_decision(d, snap) is True


def test_validate_invalid_action_fails():
    snap = _snap([])
    d = Decision(action="typo_action", button_label=None, reason="x")  # type: ignore[arg-type]
    assert validate_decision(d, snap) is False


def test_validate_empty_reason_fails():
    snap = _snap(["A"])
    d = Decision(action="reload", button_label=None, reason="")
    assert validate_decision(d, snap) is False


import json
from pathlib import Path

from booking_bot.ai_advisor import IncidentStore


def _write_incidents(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_incident_store_load_missing_file_is_empty(tmp_path):
    store = IncidentStore(tmp_path / "nope.jsonl")
    assert len(store) == 0


def test_incident_store_load_empty_file_is_empty(tmp_path):
    path = tmp_path / "incidents.jsonl"
    path.write_text("")
    store = IncidentStore(path)
    assert len(store) == 0


def test_incident_store_loads_single_record(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [{
        "key": "UNKNOWN|make payment|previous menu",
        "state": "UNKNOWN",
        "buttons_sorted": ["Make Payment", "Previous Menu"],
        "last_bubble_excerpt": "payment pending",
        "chosen_action": {"action": "click", "button_label": "Previous Menu", "reason": "escape"},
        "outcome": "recovered",
        "recovered_to_state": "BOOK_FOR_OTHERS_MENU",
        "source": "bootstrap",
        "timestamp": "2026-04-15T14:12:33Z",
        "occurrences": 1,
    }])
    store = IncidentStore(path)
    assert len(store) == 1


def test_incident_store_skips_malformed_lines(tmp_path):
    path = tmp_path / "incidents.jsonl"
    path.write_text(
        '{"key": "A", "state": "UNKNOWN", "buttons_sorted": ["x"], '
        '"last_bubble_excerpt": "", '
        '"chosen_action": {"action": "reload", "button_label": null, "reason": "r"}, '
        '"outcome": "recovered", "recovered_to_state": "MAIN_MENU", '
        '"source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1}\n'
        "\n"
        "not json at all\n"
        '{"broken": json}\n'
    )
    store = IncidentStore(path)
    assert len(store) == 1


def test_incident_store_lookup_exact_match(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [{
        "key": IncidentStore.make_key("UNKNOWN", ["Make Payment", "Previous Menu"]),
        "state": "UNKNOWN",
        "buttons_sorted": ["Make Payment", "Previous Menu"],
        "last_bubble_excerpt": "payment pending",
        "chosen_action": {"action": "click", "button_label": "Previous Menu", "reason": "escape"},
        "outcome": "recovered",
        "recovered_to_state": "BOOK_FOR_OTHERS_MENU",
        "source": "bootstrap",
        "timestamp": "2026-04-15T14:12:33Z",
        "occurrences": 1,
    }])
    store = IncidentStore(path)
    hit = store.lookup_exact("UNKNOWN", ("Make Payment", "Previous Menu"))
    assert hit is not None
    assert hit["chosen_action"]["button_label"] == "Previous Menu"


def test_incident_store_lookup_is_case_insensitive(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [{
        "key": IncidentStore.make_key("UNKNOWN", ["Make Payment", "Previous Menu"]),
        "state": "UNKNOWN",
        "buttons_sorted": ["Make Payment", "Previous Menu"],
        "last_bubble_excerpt": "",
        "chosen_action": {"action": "click", "button_label": "Previous Menu", "reason": "x"},
        "outcome": "recovered",
        "recovered_to_state": "MAIN_MENU",
        "source": "bootstrap",
        "timestamp": "2026-04-15T14:12:33Z",
        "occurrences": 1,
    }])
    store = IncidentStore(path)
    hit = store.lookup_exact("UNKNOWN", ("PREVIOUS MENU", "make payment"))
    assert hit is not None


def test_incident_store_lookup_miss_returns_none(tmp_path):
    store = IncidentStore(tmp_path / "missing.jsonl")
    assert store.lookup_exact("UNKNOWN", ("a", "b")) is None


def test_incident_store_similar_ranks_by_jaccard(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [
        {
            "key": IncidentStore.make_key("UNKNOWN", ["a", "b", "c"]),
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b", "c"],
            "last_bubble_excerpt": "one",
            "chosen_action": {"action": "reload", "button_label": None, "reason": "one"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
        {
            "key": IncidentStore.make_key("UNKNOWN", ["a", "b"]),
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "two",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "two"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
        {
            "key": IncidentStore.make_key("BOOK_FOR_OTHERS_MENU", ["a", "b"]),
            "state": "BOOK_FOR_OTHERS_MENU",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "three",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "three"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
        {
            "key": IncidentStore.make_key("UNKNOWN", ["x", "y"]),
            "state": "UNKNOWN",
            "buttons_sorted": ["x", "y"],
            "last_bubble_excerpt": "four",
            "chosen_action": {"action": "reload", "button_label": None, "reason": "four"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
    ])
    store = IncidentStore(path)
    similar = store.similar("UNKNOWN", ("a", "b"), top_k=5)
    assert len(similar) == 3
    assert similar[0]["last_bubble_excerpt"] == "two"
    assert similar[1]["last_bubble_excerpt"] == "one"
    assert similar[2]["last_bubble_excerpt"] == "four"


def test_incident_store_similar_respects_top_k(tmp_path):
    path = tmp_path / "incidents.jsonl"
    records = []
    for i in range(10):
        records.append({
            "key": IncidentStore.make_key("UNKNOWN", [f"b{i}"]),
            "state": "UNKNOWN",
            "buttons_sorted": [f"b{i}"],
            "last_bubble_excerpt": f"rec{i}",
            "chosen_action": {"action": "reload", "button_label": None, "reason": "x"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        })
    _write_incidents(path, records)
    store = IncidentStore(path)
    similar = store.similar("UNKNOWN", ("b0",), top_k=3)
    assert len(similar) == 3


def test_incident_store_record_success_new_incident(tmp_path):
    path = tmp_path / "incidents.jsonl"
    store = IncidentStore(path)
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("Make Payment", "Previous Menu"),
        last_bubble_text="payment pending",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    decision = Decision(
        action="click",
        button_label="Previous Menu",
        reason="dead-end payment dialog",
    )
    store.record_success(snap, decision, recovered_to="BOOK_FOR_OTHERS_MENU")
    assert len(store) == 1
    assert path.exists()
    reloaded = IncidentStore(path)
    assert len(reloaded) == 1
    hit = reloaded.lookup_exact("UNKNOWN", ("Make Payment", "Previous Menu"))
    assert hit is not None
    assert hit["chosen_action"]["button_label"] == "Previous Menu"
    assert hit["occurrences"] == 1
    assert hit["source"] == "runtime"


def test_incident_store_record_success_dedupes_and_increments(tmp_path):
    path = tmp_path / "incidents.jsonl"
    store = IncidentStore(path)
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A", "B"),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    d = Decision(action="click", button_label="A", reason="pick A")
    store.record_success(snap, d, recovered_to="MAIN_MENU")
    store.record_success(snap, d, recovered_to="MAIN_MENU")
    store.record_success(snap, d, recovered_to="MAIN_MENU")
    assert len(store) == 1
    hit = store.lookup_exact("UNKNOWN", ("A", "B"))
    assert hit["occurrences"] == 3


def test_incident_store_flush_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "incidents.jsonl"
    store = IncidentStore(path)
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A",),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store.record_success(
        snap,
        Decision(action="reload", button_label=None, reason="test"),
        recovered_to="MAIN_MENU",
    )
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


from booking_bot.ai_advisor import _build_snapshot_from_signals


def test_build_snapshot_from_signals_basic():
    signals = {
        "buttons": ["Make Payment", "Previous Menu"],
        "lastBubbleText": "Your booking is pending payment. Please complete payment first.",
        "emptyInputNames": [],
    }
    snap = _build_snapshot_from_signals(
        signals,
        state="UNKNOWN",
        recent_actions=["clicked Book for Others", "typed 9876543210"],
        row_hint="row 42/500",
    )
    assert snap.state == "UNKNOWN"
    assert snap.enabled_buttons == ("Make Payment", "Previous Menu")
    assert "pending payment" in snap.last_bubble_text
    assert snap.recent_actions == ("clicked Book for Others", "typed 9876543210")
    assert snap.empty_input_names == ()
    assert snap.row_hint == "row 42/500"


def test_build_snapshot_truncates_long_bubble_text():
    signals = {
        "buttons": [],
        "lastBubbleText": "x" * 900,
        "emptyInputNames": ["mobile"],
    }
    snap = _build_snapshot_from_signals(
        signals, state="UNKNOWN", recent_actions=[], row_hint=None,
    )
    assert len(snap.last_bubble_text) == 500


def test_build_snapshot_limits_recent_actions_to_5():
    signals = {"buttons": [], "lastBubbleText": "", "emptyInputNames": []}
    actions = [f"action {i}" for i in range(20)]
    snap = _build_snapshot_from_signals(
        signals, state="UNKNOWN", recent_actions=actions, row_hint=None,
    )
    assert len(snap.recent_actions) == 5
    assert snap.recent_actions[-1] == "action 19"


def test_build_snapshot_coerces_nulls_to_empty():
    signals = {
        "buttons": None,
        "lastBubbleText": None,
        "emptyInputNames": None,
    }
    snap = _build_snapshot_from_signals(
        signals, state="UNKNOWN", recent_actions=None, row_hint=None,
    )
    assert snap.enabled_buttons == ()
    assert snap.last_bubble_text == ""
    assert snap.empty_input_names == ()
    assert snap.recent_actions == ()


class FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic. Records the last messages.create
    kwargs and returns whatever response the test configured.

    The real Anthropic SDK's Message object has a .content attribute that
    is a list of content blocks, and tool_use blocks have .type == 'tool_use'
    and .input == <dict>. We mimic just enough of that shape.
    """

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.last_kwargs: dict | None = None
        self.messages = self  # so client.messages.create(...) works

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return self._response


class _Block:
    """Minimal content block with .type and .input. Used to build a fake
    messages.create response without importing the real SDK types."""
    def __init__(self, block_type: str, input_: dict | None = None, text: str = ""):
        self.type = block_type
        self.input = input_ or {}
        self.text = text


class _Msg:
    def __init__(self, blocks):
        self.content = blocks


def _fake_tool_use_response(action, button_label, reason):
    return _Msg([_Block("tool_use", input_={
        "action": action,
        "button_label": button_label,
        "reason": reason,
    })])


def _fake_text_only_response(text="sorry"):
    return _Msg([_Block("text", text=text)])


def test_fake_client_records_kwargs():
    client = FakeAnthropicClient(response=_fake_tool_use_response("reload", None, "r"))
    result = client.messages.create(model="x", messages=[{"role": "user", "content": "hi"}])
    assert client.last_kwargs["model"] == "x"
    assert result.content[0].type == "tool_use"


from booking_bot.ai_advisor import consult


def test_consult_refuses_operator_auth_state(tmp_path):
    snap = AdvisorSnapshot(
        state="NEEDS_OPERATOR_AUTH",
        enabled_buttons=(),
        last_bubble_text="please enter your 10 digit mobile",
        recent_actions=(),
        empty_input_names=("mobile",),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(response=_fake_tool_use_response("click", "x", "y"))
    d = consult(snap, store, budget, client=client)
    assert d is None
    assert client.last_kwargs is None


def test_consult_refuses_operator_otp_state(tmp_path):
    snap = AdvisorSnapshot(
        state="NEEDS_OPERATOR_OTP",
        enabled_buttons=(),
        last_bubble_text="otp sent",
        recent_actions=(),
        empty_input_names=("otp",),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(response=_fake_tool_use_response("click", "x", "y"))
    assert consult(snap, store, budget, client=client) is None
    assert client.last_kwargs is None


def test_consult_refuses_when_budget_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADVISOR_MAX_CALLS_PER_SESSION", 1)
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A",),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    budget.record_call()
    assert budget.exhausted()
    client = FakeAnthropicClient(response=_fake_tool_use_response("click", "A", "y"))
    assert consult(snap, store, budget, client=client) is None
    assert client.last_kwargs is None


def test_consult_fast_path_uses_stored_incident_without_api_call(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [{
        "key": IncidentStore.make_key("UNKNOWN", ["Make Payment", "Previous Menu"]),
        "state": "UNKNOWN",
        "buttons_sorted": ["Make Payment", "Previous Menu"],
        "last_bubble_excerpt": "payment pending",
        "chosen_action": {"action": "click", "button_label": "Previous Menu", "reason": "bootstrap: escape"},
        "outcome": "recovered",
        "recovered_to_state": "BOOK_FOR_OTHERS_MENU",
        "source": "bootstrap",
        "timestamp": "2026-04-15T14:12:33Z",
        "occurrences": 5,
    }])
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("Make Payment", "Previous Menu"),
        last_bubble_text="payment pending",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(path)
    budget = AdvisorBudget()
    client = FakeAnthropicClient(response=None)
    d = consult(snap, store, budget, client=client)
    assert d is not None
    assert d.action == "click"
    assert d.button_label == "Previous Menu"
    assert client.last_kwargs is None
    assert budget.calls_made == 0


def test_consult_fast_path_skips_invalid_stored_incident(tmp_path):
    """If a stored incident has a button_label that is NOT in the current
    enabled_buttons, the fast path must fall through. Task 10 stubs the
    slow path to return None; Task 11 wires it for real."""
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [{
        "key": IncidentStore.make_key("UNKNOWN", ["A", "B"]),
        "state": "UNKNOWN",
        "buttons_sorted": ["A", "B"],
        "last_bubble_excerpt": "",
        "chosen_action": {"action": "click", "button_label": "C", "reason": "stale"},
        "outcome": "recovered",
        "recovered_to_state": "MAIN_MENU",
        "source": "bootstrap",
        "timestamp": "2026-04-15T00:00:00Z",
        "occurrences": 1,
    }])
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A", "B"),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(path)
    budget = AdvisorBudget()
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("click", "A", "fallback")
    )
    d = consult(snap, store, budget, client=client)
    assert client.last_kwargs is not None
    assert d is not None
    assert d.button_label == "A"


def test_consult_slow_path_click_passes_validation(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("Make Payment", "Previous Menu"),
        last_bubble_text="payment pending",
        recent_actions=("typed 9876543210",),
        empty_input_names=(),
        row_hint="row 42/500",
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("click", "Previous Menu", "escape")
    )
    d = consult(snap, store, budget, client=client)
    assert d is not None
    assert d.action == "click"
    assert d.button_label == "Previous Menu"
    assert client.last_kwargs is not None
    assert budget.calls_made == 1
    messages = client.last_kwargs["messages"]
    user_content = "\n".join(
        block["text"] if isinstance(block, dict) else block
        for m in messages for block in (m["content"] if isinstance(m["content"], list) else [m["content"]])
    )
    assert "Make Payment" in user_content
    assert "Previous Menu" in user_content


def test_consult_slow_path_rejects_hallucinated_label(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A", "B"),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("click", "Ghost Button", "invented")
    )
    d = consult(snap, store, budget, client=client)
    assert d is None
    assert budget.calls_made == 1


def test_consult_slow_path_reload_passes(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=(),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("reload", None, "dom broken")
    )
    d = consult(snap, store, budget, client=client)
    assert d is not None
    assert d.action == "reload"


def test_consult_slow_path_skip_row_passes(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("Make Payment",),
        last_bubble_text="payment pending",
        recent_actions=(),
        empty_input_names=(),
        row_hint="row 42/500",
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("skip_row", None, "payment pending this row")
    )
    d = consult(snap, store, budget, client=client)
    assert d is not None
    assert d.action == "skip_row"


def test_consult_slow_path_api_timeout_returns_none(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A",),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(raise_exc=TimeoutError("fake timeout"))
    d = consult(snap, store, budget, client=client)
    assert d is None
    assert budget.calls_made == 1


def test_consult_slow_path_generic_exception_returns_none(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A",),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(raise_exc=RuntimeError("api exploded"))
    d = consult(snap, store, budget, client=client)
    assert d is None


def test_consult_slow_path_no_tool_use_block_returns_none(tmp_path):
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("A",),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(tmp_path / "i.jsonl")
    budget = AdvisorBudget()
    client = FakeAnthropicClient(response=_fake_text_only_response("I refuse"))
    d = consult(snap, store, budget, client=client)
    assert d is None


def test_consult_slow_path_passes_top_k_similar_as_few_shots(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [{
        "key": IncidentStore.make_key("UNKNOWN", ["Other", "Previous Menu"]),
        "state": "UNKNOWN",
        "buttons_sorted": ["Other", "Previous Menu"],
        "last_bubble_excerpt": "an old incident",
        "chosen_action": {"action": "click", "button_label": "Previous Menu", "reason": "escape"},
        "outcome": "recovered",
        "recovered_to_state": "MAIN_MENU",
        "source": "bootstrap",
        "timestamp": "2026-04-15T00:00:00Z",
        "occurrences": 1,
    }])
    snap = AdvisorSnapshot(
        state="UNKNOWN",
        enabled_buttons=("New Thing", "Previous Menu"),
        last_bubble_text="",
        recent_actions=(),
        empty_input_names=(),
        row_hint=None,
    )
    store = IncidentStore(path)
    budget = AdvisorBudget()
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("click", "Previous Menu", "similar")
    )
    d = consult(snap, store, budget, client=client)
    assert d is not None
    msgs = client.last_kwargs["messages"]
    combined = json.dumps(msgs)
    assert "an old incident" in combined


from booking_bot.ai_advisor import apply_advisor_decision
from booking_bot.exceptions import AdvisorSkipRow


class FakeFrame:
    """Captures click_by_action calls. Matches playbook._click_by_action's
    expected Frame shape only enough for the test."""
    def __init__(self):
        self.clicks = []


class FakePage:
    def __init__(self):
        self.reloaded = False


def test_apply_decision_click_invokes_click_by_action(monkeypatch):
    calls = []
    def fake_click_by_action(frame, action):
        calls.append((frame, action.kind, action.button_text))
    import booking_bot.playbook as pb_mod
    monkeypatch.setattr(pb_mod, "_click_by_action", fake_click_by_action)

    frame = FakeFrame()
    page = FakePage()
    decision = Decision(action="click", button_label="Previous Menu", reason="escape")
    budget = AdvisorBudget()

    result = apply_advisor_decision(decision, frame, page, budget=budget)
    assert result == "acted"
    assert calls == [(frame, "click", "Previous Menu")]
    assert budget.consecutive_skips == 0


def test_apply_decision_reload_calls_page_reload(monkeypatch):
    frame = FakeFrame()
    page = FakePage()
    def fake_reload(**kwargs):
        page.reloaded = True
    page.reload = fake_reload
    page.wait_for_timeout = lambda ms: None

    decision = Decision(action="reload", button_label=None, reason="dom broken")
    budget = AdvisorBudget()

    result = apply_advisor_decision(decision, frame, page, budget=budget)
    assert result == "acted"
    assert page.reloaded is True
    assert budget.consecutive_skips == 0


def test_apply_decision_skip_row_raises_advisor_skip_row(monkeypatch):
    frame = FakeFrame()
    page = FakePage()
    decision = Decision(action="skip_row", button_label=None, reason="payment pending")
    budget = AdvisorBudget()

    try:
        apply_advisor_decision(decision, frame, page, budget=budget)
    except AdvisorSkipRow as e:
        assert str(e) == "payment pending"
    else:
        raise AssertionError("expected AdvisorSkipRow to be raised")
    assert budget.total_skips == 1
    assert budget.consecutive_skips == 1


def test_apply_decision_invalid_action_returns_declined(monkeypatch):
    frame = FakeFrame()
    page = FakePage()
    decision = Decision(action="nonsense", button_label=None, reason="x")  # type: ignore[arg-type]
    budget = AdvisorBudget()

    result = apply_advisor_decision(decision, frame, page, budget=budget)
    assert result == "declined"
