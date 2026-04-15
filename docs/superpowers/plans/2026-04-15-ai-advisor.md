# AI Recovery Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrowly-scoped LLM advisor that sits behind existing deterministic recovery; when `_recover_with_playbook` exhausts on UNKNOWN/dead-end states, the advisor is consulted and may return a click-from-visible-buttons, a reload, or a skip_row — subject to hard per-session budget caps and validation that the chosen label exists in the current DOM.

**Architecture:** New module `booking_bot/ai_advisor.py` with `consult(snapshot, store, budget) -> Decision | None`, append-only episodic corpus at `data/incidents.jsonl`, and a one-shot `scripts/bootstrap_incidents.py` that seeds the corpus from the existing `logs/*.log` files. Hooked into `cli._recover_with_playbook` as a fallback after `reset_to_customer_entry` fails post-reload. Never invoked on `NEEDS_OPERATOR_AUTH`/`NEEDS_OPERATOR_OTP` (survivability-spec territory). Feature-flagged via `config.ADVISOR_ENABLED`; rollback is one module deletion plus a two-file revert.

**Tech Stack:** Python 3.12, Anthropic Python SDK (`anthropic >= 0.40`), Playwright sync API, pytest. No LangChain, no embeddings, no vector store, no new infra.

**Spec reference:** `docs/superpowers/specs/2026-04-15-ai-advisor-design.md`

---

## File Structure

**New files:**
- `booking_bot/ai_advisor.py` — module exposing `AdvisorSnapshot`, `Decision`, `IncidentStore`, `AdvisorBudget`, `validate_decision`, `build_snapshot`, `consult`, `apply_advisor_decision`, and the `AdvisorSkipRow` exception.
- `scripts/bootstrap_incidents.py` — one-shot CLI.
- `data/incidents.jsonl` — produced by bootstrap + appended by runtime. Starts empty; created by `IncidentStore.__init__` if missing.
- `tests/test_ai_advisor.py` — unit tests with a fake Anthropic client.
- `tests/test_bootstrap_incidents.py` — unit tests against a canned log fixture.
- `tests/fixtures/bootstrap_log_sample.log` — small canned log file for bootstrap tests.

**Modified files:**
- `booking_bot/config.py` — add 6 new constants + `data/` path.
- `booking_bot/cli.py` — add `_dispatch_advisor_fallback` helper called from `_recover_with_playbook` after the post-reload `reset_to_customer_entry` raises; add `AdvisorSkipRow` catch in row loop.
- `pyproject.toml` (or `requirements.txt`, whichever is the source of truth) — add `anthropic>=0.40`.

---

## Task 1: Dependency + config constants + `AdvisorSkipRow` exception

**Files:**
- Modify: `pyproject.toml` or `requirements.txt` (dependency bump)
- Modify: `booking_bot/config.py` (add constants)
- Modify: `booking_bot/exceptions.py` (add `AdvisorSkipRow`)

- [ ] **Step 1: Check which dependency file is the source of truth**

Run: `ls pyproject.toml requirements.txt 2>/dev/null`

If `pyproject.toml` has a `[project]` table with `dependencies`, add to that. Otherwise add a line to `requirements.txt`. Pick the one the repo already uses.

- [ ] **Step 2: Add `anthropic>=0.40` to the dependency file**

For `pyproject.toml`:
```toml
dependencies = [
    # ...existing deps...
    "anthropic>=0.40",
]
```

For `requirements.txt`:
```
anthropic>=0.40
```

- [ ] **Step 3: Install the new dependency**

Run: `pip install 'anthropic>=0.40'`
Expected: `Successfully installed anthropic-0.X.X ...`

- [ ] **Step 4: Add config constants at the end of `booking_bot/config.py`**

Append after the existing `GATEWAY_URL_RE` line:

```python
# ---- AI Recovery Advisor (see docs/superpowers/specs/2026-04-15-ai-advisor-design.md) ----
# Kill switch. When False, the advisor fallback branch in
# _recover_with_playbook is a no-op and the bot behaves exactly as current
# main. Flip to False for emergency rollback without redeploying.
ADVISOR_ENABLED               = True
ADVISOR_MODEL                 = "claude-sonnet-4-6"
ADVISOR_API_TIMEOUT_S         = 10.0
# Hard caps. An AI that got confused cannot burn down the night: the
# budget is capped and then the advisor goes silent for the rest of the
# session, falling back to existing crash-and-restart semantics.
ADVISOR_MAX_CALLS_PER_SESSION = 50
ADVISOR_MAX_CONSECUTIVE_SKIPS = 3
ADVISOR_MAX_TOTAL_SKIPS       = 15
# Episodic memory store. Append-only JSONL, one incident per line,
# hand-editable. Created on first use by IncidentStore.__init__ if missing.
ADVISOR_INCIDENTS_PATH        = ROOT / "data" / "incidents.jsonl"
```

- [ ] **Step 5: Add `AdvisorSkipRow` to `booking_bot/exceptions.py`**

Append after the existing `ChromeNotInstalledError` class:

```python
class AdvisorSkipRow(BookingBotError):
    """Raised by the AI advisor dispatcher when the advisor judged the
    current row as hopeless (e.g. payment pending, duplicate booking).
    The row loop catches it, writes the row as ISSUE via
    excel.write_issue, and advances the queue without any further
    retry attempts. This is an intentional short-circuit — the AI
    has explicitly said 'skip this one, don't retry.'

    Carries the reason string for logging and audit."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
```

- [ ] **Step 6: Run the full test suite to verify nothing regressed**

Run: `python -m pytest -q`
Expected: All existing tests pass (`99 passed, 1 skipped` or equivalent).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt booking_bot/config.py booking_bot/exceptions.py
git commit -m "feat(advisor): add anthropic dep, advisor config, AdvisorSkipRow exception"
```

---

## Task 2: `AdvisorSnapshot` and `Decision` dataclasses

**Files:**
- Create: `booking_bot/ai_advisor.py`
- Create: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write the failing test for snapshot and decision dataclass construction**

Create `tests/test_ai_advisor.py`:

```python
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
    # Frozen: cannot mutate
    import dataclasses
    assert dataclasses.is_dataclass(s)
    # Hashable: tuples, not lists
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
```

- [ ] **Step 2: Run to confirm the test fails with ImportError**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: `ModuleNotFoundError: No module named 'booking_bot.ai_advisor'`

- [ ] **Step 3: Create `booking_bot/ai_advisor.py` with the dataclasses**

```python
"""AI Recovery Advisor — a narrow fallback layer that is consulted only
when deterministic recovery (reset_to_customer_entry, reload + login)
has exhausted on an UNKNOWN/dead-end state.

See docs/superpowers/specs/2026-04-15-ai-advisor-design.md for the
design and safety invariants.

Key safety properties:
  - Never invoked on NEEDS_OPERATOR_AUTH / NEEDS_OPERATOR_OTP states.
  - Action space is restricted to click-from-enabled-buttons, reload,
    skip_row. No free text, no CSS selectors, no URL navigation.
  - Every external failure path (API timeout, malformed JSON,
    hallucinated label, budget exhausted) returns None, which the
    caller treats as "advisor declined" and falls back to existing
    crash-and-restart semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AdvisorSnapshot:
    """The input to consult(). Built once per advisor call from the
    current frame state. Tuples (not lists) so the snapshot is
    hashable and cheap to log.

    Fields:
      state              — current detect_state result, e.g. 'UNKNOWN'
      enabled_buttons    — exact labels HPCL shows right now, DOM order
      last_bubble_text   — trimmed, at most 500 chars
      recent_actions     — last ~5 action log lines, for context
      empty_input_names  — input name attributes present for safety check
      row_hint           — 'row N/M, phone ****1234' or None (startup)
    """
    state: str
    enabled_buttons: tuple[str, ...]
    last_bubble_text: str
    recent_actions: tuple[str, ...]
    empty_input_names: tuple[str, ...]
    row_hint: str | None


@dataclass(frozen=True)
class Decision:
    """The output of consult(). One of three action types, plus the
    reason string for logging.

    For action='click', button_label must be non-None AND must exactly
    match (case-insensitive) one of the enabled_buttons from the
    snapshot that produced this decision. validate_decision enforces
    this invariant.
    """
    action: Literal["click", "reload", "skip_row"]
    button_label: str | None
    reason: str
```

- [ ] **Step 4: Run to confirm the test passes**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): AdvisorSnapshot and Decision dataclasses"
```

---

## Task 3: `AdvisorBudget` class — call and skip caps

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for AdvisorBudget**

Append to `tests/test_ai_advisor.py`:

```python
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
    # total_skips is NOT reset — only the streak is.
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
    # Interleave non-skips so consecutive cap doesn't trip first.
    for _ in range(3):
        b.record_skip()
        b.record_non_skip_decision()
    assert b.total_skips == 3
    assert b.exhausted() is True
```

- [ ] **Step 2: Run to confirm the tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 7 new failures with `ImportError` or `AttributeError` on `AdvisorBudget`.

- [ ] **Step 3: Add `AdvisorBudget` to `booking_bot/ai_advisor.py`**

Add to the imports at the top:

```python
from booking_bot import config
```

Append after the `Decision` class:

```python
class AdvisorBudget:
    """Per-session cost cap for the advisor. Reads its limits from
    config at construction time; monkeypatching config in tests works
    as expected.

    Semantics:
      - record_call() is called by consult() *only* when an API call
        is actually made. Fast-path hits (exact-match lookup in the
        incident store) do not increment calls_made — they are free.
      - record_skip() is called after a skip_row decision is acted on
        by the caller. Increments both total and consecutive counters.
      - record_non_skip_decision() is called after a click or reload
        decision is acted on. Resets the consecutive streak counter
        but leaves the total alone.
      - exhausted() is True if any of the three caps are hit. Once
        exhausted, consult() refuses further API calls and returns
        None, and the bot falls back to existing crash semantics.
    """

    def __init__(self):
        self.calls_made = 0
        self.total_skips = 0
        self.consecutive_skips = 0
        self.max_calls = config.ADVISOR_MAX_CALLS_PER_SESSION
        self.max_consecutive_skips = config.ADVISOR_MAX_CONSECUTIVE_SKIPS
        self.max_total_skips = config.ADVISOR_MAX_TOTAL_SKIPS

    def record_call(self) -> None:
        self.calls_made += 1

    def record_skip(self) -> None:
        self.total_skips += 1
        self.consecutive_skips += 1

    def record_non_skip_decision(self) -> None:
        self.consecutive_skips = 0

    def exhausted(self) -> bool:
        return (
            self.calls_made >= self.max_calls
            or self.consecutive_skips >= self.max_consecutive_skips
            or self.total_skips >= self.max_total_skips
        )
```

- [ ] **Step 4: Run to confirm the tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): AdvisorBudget call/skip caps"
```

---

## Task 4: `validate_decision` — the safety choke point

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for validate_decision**

Append to `tests/test_ai_advisor.py`:

```python
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
    # Decision dataclass allows any string because Literal is only
    # checked at type-check time; validate_decision is the runtime guard.
    d = Decision(action="typo_action", button_label=None, reason="x")  # type: ignore[arg-type]
    assert validate_decision(d, snap) is False


def test_validate_empty_reason_fails():
    snap = _snap(["A"])
    d = Decision(action="reload", button_label=None, reason="")
    assert validate_decision(d, snap) is False
```

- [ ] **Step 2: Run to confirm the tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 8 new `ImportError` failures on `validate_decision`.

- [ ] **Step 3: Add `validate_decision` to `booking_bot/ai_advisor.py`**

Append after the `AdvisorBudget` class:

```python
_ALLOWED_ACTIONS = frozenset({"click", "reload", "skip_row"})


def validate_decision(decision: Decision, snapshot: AdvisorSnapshot) -> bool:
    """Safety choke point. Every code path that produces a Decision —
    fast path, slow path, or any test fake — routes through this
    function before the decision is acted on.

    Rules enforced:
      1. action must be one of {click, reload, skip_row}.
      2. reason must be a non-empty string (a decision without a reason
         is almost certainly a malformed response we should reject).
      3. For action=='click': button_label must be non-None AND must
         case-insensitively exact-match one of snapshot.enabled_buttons.
         This is the invariant that makes label hallucination impossible
         to turn into a real click.

    Returns True if the decision is safe to act on, False otherwise.
    Never raises.
    """
    if decision.action not in _ALLOWED_ACTIONS:
        return False
    if not decision.reason or not decision.reason.strip():
        return False
    if decision.action == "click":
        if decision.button_label is None:
            return False
        label = decision.button_label.strip().lower()
        enabled_lower = {b.strip().lower() for b in snapshot.enabled_buttons}
        if label not in enabled_lower:
            return False
    return True
```

- [ ] **Step 4: Run to confirm the tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 19 tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): validate_decision safety choke point"
```

---

## Task 5: `IncidentStore` — load from file

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for IncidentStore.load**

Append to `tests/test_ai_advisor.py`:

```python
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
    # Mix of valid, blank, and malformed lines. The store must skip the
    # malformed ones and still load the valid one.
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
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 4 new `ImportError` failures on `IncidentStore`.

- [ ] **Step 3: Add IncidentStore.load logic to ai_advisor.py**

Add to imports at the top of `booking_bot/ai_advisor.py`:

```python
import json
import logging
from pathlib import Path

log = logging.getLogger("ai_advisor")
```

Append after `validate_decision`:

```python
class IncidentStore:
    """Episodic memory for the advisor — an append-only JSONL corpus
    of past stuck-state recoveries. Exact-match lookups by
    (state, sorted_buttons) are the fast path that makes repeat
    stucks free (no API call). Similarity lookups provide few-shot
    context for novel stucks.

    The backing file is hand-editable plain text. A malformed line
    is logged and skipped; the rest of the file loads normally.

    Thread-safety: not thread-safe. The bot is single-threaded for
    row processing; this store is only touched from that thread.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        # Keyed by (state, sorted_buttons_tuple) for O(1) exact lookup.
        self._by_key: dict[str, dict] = {}
        self._load()

    def __len__(self) -> int:
        return len(self._by_key)

    def _load(self) -> None:
        if not self.path.exists():
            log.info(
                f"IncidentStore: {self.path} does not exist; "
                f"starting with empty corpus"
            )
            return
        loaded = 0
        skipped = 0
        for lineno, raw in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning(
                    f"IncidentStore: skipping malformed line "
                    f"{self.path}:{lineno}: {e}"
                )
                skipped += 1
                continue
            key = record.get("key")
            if not key or not isinstance(key, str):
                log.warning(
                    f"IncidentStore: skipping line with missing key "
                    f"{self.path}:{lineno}"
                )
                skipped += 1
                continue
            self._by_key[key] = record
            loaded += 1
        log.info(
            f"IncidentStore: loaded {loaded} incidents from {self.path} "
            f"(skipped {skipped} malformed lines)"
        )

    @staticmethod
    def make_key(state: str, buttons: tuple[str, ...] | list[str]) -> str:
        """Compute the canonical dict key for a (state, buttons) pair.
        Public so the bootstrap script, tests, and runtime all agree on
        the exact canonicalization: lowercase, stripped, sorted, joined."""
        labels = sorted((b or "").strip().lower() for b in buttons)
        return f"{state}|{'|'.join(labels)}"
```

- [ ] **Step 4: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 23 tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): IncidentStore.load with malformed-line tolerance"
```

---

## Task 6: `IncidentStore` — lookup + similar_by_jaccard

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for lookup and similar_by_jaccard**

Append to `tests/test_ai_advisor.py`:

```python
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
    # Different case ordering — should still match because make_key
    # normalizes.
    hit = store.lookup_exact("UNKNOWN", ("PREVIOUS MENU", "make payment"))
    assert hit is not None


def test_incident_store_lookup_miss_returns_none(tmp_path):
    store = IncidentStore(tmp_path / "missing.jsonl")
    assert store.lookup_exact("UNKNOWN", ("a", "b")) is None


def test_incident_store_similar_ranks_by_jaccard(tmp_path):
    path = tmp_path / "incidents.jsonl"
    _write_incidents(path, [
        # Same state, buttons {a, b, c} — overlap with query {a, b} is 2/3.
        {
            "key": IncidentStore.make_key("UNKNOWN", ["a", "b", "c"]),
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b", "c"],
            "last_bubble_excerpt": "one",
            "chosen_action": {"action": "reload", "button_label": None, "reason": "one"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
        # Same state, buttons {a, b} — overlap 2/2 = 1.0 (perfect).
        {
            "key": IncidentStore.make_key("UNKNOWN", ["a", "b"]),
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "two",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "two"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
        # Different state — should be excluded.
        {
            "key": IncidentStore.make_key("BOOK_FOR_OTHERS_MENU", ["a", "b"]),
            "state": "BOOK_FOR_OTHERS_MENU",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "three",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "three"},
            "outcome": "recovered", "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap", "timestamp": "2026-04-15T00:00:00Z", "occurrences": 1,
        },
        # Same state, buttons {x, y} — overlap 0. Should still appear
        # but rank last.
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
    # BOOK_FOR_OTHERS_MENU record is excluded (different state).
    assert len(similar) == 3
    # Ranked: perfect match first, then 2/3 overlap, then 0 overlap.
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
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 5 new `AttributeError` failures on `lookup_exact` / `similar`.

- [ ] **Step 3: Add lookup_exact and similar to IncidentStore**

Append these methods to `IncidentStore` in `booking_bot/ai_advisor.py`:

```python
    def lookup_exact(
        self,
        state: str,
        buttons: tuple[str, ...],
    ) -> dict | None:
        """Exact-match lookup by (state, sorted canonicalized buttons).
        Returns the stored incident dict or None. This is the fast path
        that skips the API call entirely on repeat stucks."""
        key = self.make_key(state, buttons)
        return self._by_key.get(key)

    def similar(
        self,
        state: str,
        buttons: tuple[str, ...],
        top_k: int = 5,
    ) -> list[dict]:
        """Return up to top_k incidents from the same state, ranked by
        Jaccard similarity on the button-label sets. Used as few-shot
        context for the slow (API) path. Ties broken by timestamp
        (newer first) then by occurrences (higher first)."""
        query_set = {(b or "").strip().lower() for b in buttons}
        candidates = []
        for rec in self._by_key.values():
            if rec.get("state") != state:
                continue
            rec_buttons = rec.get("buttons_sorted") or []
            rec_set = {(b or "").strip().lower() for b in rec_buttons}
            union = query_set | rec_set
            if not union:
                jaccard = 0.0
            else:
                jaccard = len(query_set & rec_set) / len(union)
            candidates.append((jaccard, rec))
        # Sort by (jaccard desc, timestamp desc, occurrences desc).
        candidates.sort(
            key=lambda t: (
                -t[0],
                -(t[1].get("occurrences") or 0),
                t[1].get("timestamp") or "",
            ),
        )
        return [rec for (_score, rec) in candidates[:top_k]]
```

- [ ] **Step 4: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 28 tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): IncidentStore.lookup_exact and similar by Jaccard"
```

---

## Task 7: `IncidentStore.record_success` with atomic flush

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ai_advisor.py`:

```python
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
    # And the file was written.
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
    """After a flush, the file either contains the new content or the
    old content — never a half-written mess. Verified by checking that
    a fresh load sees a consistent state."""
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
    # The atomic rename leaves no `.tmp` file behind.
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 3 new `AttributeError` failures on `record_success`.

- [ ] **Step 3: Add record_success with atomic flush**

Add to imports at the top of `booking_bot/ai_advisor.py`:

```python
import os
import tempfile
from datetime import datetime, timezone
```

Append to `IncidentStore`:

```python
    def record_success(
        self,
        snapshot: AdvisorSnapshot,
        decision: Decision,
        recovered_to: str,
    ) -> None:
        """Append/update an incident for a successful advisor-driven
        recovery. If the exact (state, buttons) key already exists,
        increment occurrences and update the timestamp. Otherwise
        create a new record with occurrences=1 and source='runtime'.
        Flushes the whole file atomically after every write."""
        key = self.make_key(snapshot.state, snapshot.enabled_buttons)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = self._by_key.get(key)
        if existing is not None:
            existing["occurrences"] = int(existing.get("occurrences", 1)) + 1
            existing["timestamp"] = now
            existing["recovered_to_state"] = recovered_to
            # Update chosen_action only if this one is still the winning
            # action — if the recovery worked with a different click, we
            # keep the most recent successful choice.
            existing["chosen_action"] = {
                "action": decision.action,
                "button_label": decision.button_label,
                "reason": decision.reason,
            }
        else:
            record = {
                "key": key,
                "state": snapshot.state,
                "buttons_sorted": sorted(snapshot.enabled_buttons),
                "last_bubble_excerpt": (snapshot.last_bubble_text or "")[:500],
                "chosen_action": {
                    "action": decision.action,
                    "button_label": decision.button_label,
                    "reason": decision.reason,
                },
                "outcome": "recovered",
                "recovered_to_state": recovered_to,
                "source": "runtime",
                "timestamp": now,
                "occurrences": 1,
            }
            self._by_key[key] = record
        self._flush()

    def _flush(self) -> None:
        """Atomic write-to-temp + rename. Guarantees the jsonl file is
        either the old content or the new content, never a half-write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use a tempfile in the same directory so the rename is atomic
        # on the same filesystem.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".incidents.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for rec in self._by_key.values():
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp_name, self.path)
        except Exception:
            # Clean up the tempfile if rename failed.
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            raise
```

- [ ] **Step 4: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 31 tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): IncidentStore.record_success with atomic flush"
```

---

## Task 8: `build_snapshot` — pure helper + frame wrapper

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for the pure helper**

Append to `tests/test_ai_advisor.py`:

```python
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
    # Keeps the MOST RECENT 5, not the first.
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
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 4 new `ImportError` failures on `_build_snapshot_from_signals`.

- [ ] **Step 3: Add the pure helper and the frame wrapper**

Append to `booking_bot/ai_advisor.py`:

```python
_MAX_BUBBLE_CHARS = 500
_MAX_RECENT_ACTIONS = 5


def _build_snapshot_from_signals(
    signals: dict,
    state: str,
    recent_actions,
    row_hint: str | None,
) -> AdvisorSnapshot:
    """Pure helper — the thin adapter from a raw signals dict (produced
    by either detect_state's JS eval or a hand-crafted test fixture)
    to a typed AdvisorSnapshot. All coercion, truncation, and
    normalization lives here so it's trivially unit-testable without
    a Playwright frame."""
    buttons = tuple(signals.get("buttons") or [])
    bubble = (signals.get("lastBubbleText") or "")[:_MAX_BUBBLE_CHARS]
    empty_names = tuple(signals.get("emptyInputNames") or [])
    actions_list = list(recent_actions or [])
    if len(actions_list) > _MAX_RECENT_ACTIONS:
        actions_list = actions_list[-_MAX_RECENT_ACTIONS:]
    return AdvisorSnapshot(
        state=state,
        enabled_buttons=buttons,
        last_bubble_text=bubble,
        recent_actions=tuple(actions_list),
        empty_input_names=empty_names,
        row_hint=row_hint,
    )


def build_snapshot(
    frame,
    state: str,
    recent_actions,
    row_hint: str | None,
) -> AdvisorSnapshot:
    """Read the current frame state and construct an AdvisorSnapshot.
    Uses a single JS evaluate() call with the same selectors as
    chat.detect_state, so what the advisor sees is what detect_state
    saw. The JS is kept in sync with chat.py's detect_state JS by the
    test in test_ai_advisor.py::test_build_snapshot_reads_same_shape_as_detect_state."""
    from booking_bot import config  # local to avoid import cycles

    js = f"""
    () => {{
      const btns = Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
        .filter(b => b.offsetParent !== null && !b.disabled)
        .map(b => (b.innerText || '').trim());
      const inputs = Array.from(document.querySelectorAll(
        "input[type='text'], input[type='number'], input[type='tel'], input[type='password']"
      )).filter(el => {{
        if (el.offsetParent === null) return false;
        if (el.disabled || el.readOnly) return false;
        const cls = el.getAttribute('class') || '';
        if (cls.includes('replybox')) return false;
        if (el.value && el.value.trim().length > 0) return false;
        return true;
      }});
      const emptyInputNames = inputs.map(el =>
        (el.getAttribute('name') || el.id || '').trim()
      ).filter(n => n.length > 0);
      const s = document.querySelector('{config.SEL_SCROLLER}');
      let lastBubbleText = '';
      if (s) {{
        const kids = Array.from(s.children);
        for (let i = kids.length - 1; i >= 0; i--) {{
          const t = (kids[i].innerText || '').trim();
          if (t) {{ lastBubbleText = t; break; }}
        }}
        if (!lastBubbleText) lastBubbleText = (s.innerText || '').slice(-400);
      }}
      return {{buttons: btns, lastBubbleText: lastBubbleText, emptyInputNames: emptyInputNames}};
    }}
    """
    try:
        signals = frame.evaluate(js) or {}
    except Exception as e:
        log.warning(f"build_snapshot: frame.evaluate failed ({e}); using empty signals")
        signals = {}
    return _build_snapshot_from_signals(
        signals, state=state, recent_actions=recent_actions, row_hint=row_hint,
    )
```

- [ ] **Step 4: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 35 tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): build_snapshot pure helper + frame wrapper"
```

---

## Task 9: `FakeAnthropicClient` test harness

**Files:**
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Add the test-only fake client**

Append to `tests/test_ai_advisor.py`:

```python
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
```

- [ ] **Step 2: Run to confirm the new test passes and nothing regressed**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 36 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ai_advisor.py
git commit -m "test(advisor): FakeAnthropicClient harness"
```

---

## Task 10: `consult` refusals + fast path

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for refusal cases and fast path**

Append to `tests/test_ai_advisor.py`:

```python
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
    # No API call should have happened.
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
    budget.record_call()   # exhaust
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
    # Fast path: no API call, no budget increment.
    assert client.last_kwargs is None
    assert budget.calls_made == 0


def test_consult_fast_path_skips_invalid_stored_incident(tmp_path):
    """If a stored incident has a button_label that is NOT in the current
    enabled_buttons (e.g. corpus is stale vs the current DOM), the fast
    path must fall through to the slow path rather than return an
    unsafe decision."""
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
    # Slow path will be reached; give it a valid canned response.
    client = FakeAnthropicClient(
        response=_fake_tool_use_response("click", "A", "fallback")
    )
    d = consult(snap, store, budget, client=client)
    # Slow path was used — API call was made.
    assert client.last_kwargs is not None
    assert d is not None
    assert d.button_label == "A"
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 5 new `ImportError` failures on `consult`.

- [ ] **Step 3: Add the refusal + fast-path portion of `consult`**

Append to `booking_bot/ai_advisor.py`:

```python
_REFUSED_STATES = frozenset({"NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"})


def consult(
    snapshot: AdvisorSnapshot,
    store: IncidentStore,
    budget: AdvisorBudget,
    *,
    client=None,
) -> Decision | None:
    """Ask the advisor what to do about the stuck state described by
    `snapshot`. Returns a validated Decision, or None if the advisor
    declined (refusal / budget exhausted / API error / invalid
    response).

    The caller is responsible for acting on the Decision and for
    calling budget.record_skip() / budget.record_non_skip_decision()
    after the action is dispatched.

    `client` is the Anthropic client (or a fake). If None, consult
    constructs a real anthropic.Anthropic client on demand — but only
    if ANTHROPIC_API_KEY is set AND the fast path misses.
    """
    if not config.ADVISOR_ENABLED:
        log.info("ai_advisor: disabled via config; advisor declined")
        return None

    if snapshot.state in _REFUSED_STATES:
        log.warning(
            f"ai_advisor: REFUSED state={snapshot.state!r} — "
            f"survivability spec owns auth/OTP recovery"
        )
        return None

    if budget.exhausted():
        log.warning(
            f"ai_advisor: budget exhausted "
            f"(calls={budget.calls_made}/{budget.max_calls} "
            f"skips={budget.total_skips}/{budget.max_total_skips} "
            f"consec={budget.consecutive_skips}/{budget.max_consecutive_skips})"
        )
        return None

    # --- Fast path: exact-match lookup, no API call ---
    hit = store.lookup_exact(snapshot.state, snapshot.enabled_buttons)
    if hit is not None:
        chosen = hit.get("chosen_action") or {}
        fast_decision = Decision(
            action=chosen.get("action", ""),
            button_label=chosen.get("button_label"),
            reason=f"fast_path: {chosen.get('reason', '')}",
        )
        if validate_decision(fast_decision, snapshot):
            log.info(
                f"ai_advisor: path=fast state={snapshot.state!r} "
                f"buttons={list(snapshot.enabled_buttons)!r} "
                f"decision={fast_decision.action}/"
                f"{fast_decision.button_label!r} "
                f"reason={fast_decision.reason!r} "
                f"budget={budget.calls_made}/{budget.max_calls} "
                f"skips={budget.total_skips}/{budget.max_total_skips}"
            )
            return fast_decision
        # Stale corpus entry — the stored button_label is no longer
        # in the enabled list. Log and fall through to the slow path.
        log.warning(
            f"ai_advisor: stale fast-path hit for state={snapshot.state!r} "
            f"(stored label {fast_decision.button_label!r} not in current "
            f"enabled buttons {list(snapshot.enabled_buttons)!r}); "
            f"falling through to slow path"
        )

    # Slow path is added in Task 11.
    return _consult_slow_path(snapshot, store, budget, client=client)


def _consult_slow_path(
    snapshot: AdvisorSnapshot,
    store: IncidentStore,
    budget: AdvisorBudget,
    *,
    client,
) -> Decision | None:
    """Placeholder stub; implemented in Task 11."""
    return None
```

- [ ] **Step 4: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 41 tests pass. (Note: `test_consult_fast_path_skips_invalid_stored_incident` asserts `last_kwargs is not None` which requires the slow path to actually call the client. Since `_consult_slow_path` is still a stub in this task, that one test will fail — move it to Task 11 by marking it `@pytest.mark.skip` for this task.)

Actually, re-order: remove the `_consult_slow_path` call from `consult` for this task's passing state. Replace the final `return _consult_slow_path(...)` with `return None` temporarily, and adjust the stale-fast-path test to assert `consult(...) is None` for now. Then in Task 11 re-wire it.

Concretely, for this task's Step 3, replace the last two lines of `consult` with:

```python
    # Slow path (Task 11) not yet wired.
    return None
```

And in this task's test file, temporarily change `test_consult_fast_path_skips_invalid_stored_incident` to:

```python
def test_consult_fast_path_skips_invalid_stored_incident(tmp_path):
    # ... setup same as before ...
    client = FakeAnthropicClient(response=None)
    d = consult(snap, store, budget, client=client)
    assert d is None  # Slow path stub returns None until Task 11.
```

- [ ] **Step 5: Re-run and confirm all tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All 41 tests pass.

- [ ] **Step 6: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): consult refusals + fast-path lookup"
```

---

## Task 11: `consult` slow path — API call + validation

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for slow-path behaviours**

Append to `tests/test_ai_advisor.py`:

```python
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
    # API call was made; budget incremented.
    assert client.last_kwargs is not None
    assert budget.calls_made == 1
    # Prompt includes the current state and the enabled buttons.
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
        enabled_buttons=("New Thing", "Previous Menu"),  # different shape, slow path
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
    # The prompt should contain the similar incident's excerpt.
    msgs = client.last_kwargs["messages"]
    combined = json.dumps(msgs)
    assert "an old incident" in combined
```

- [ ] **Step 2: Restore the slow-path stale-hit test from Task 10**

In `tests/test_ai_advisor.py`, find `test_consult_fast_path_skips_invalid_stored_incident` and change the assertion back to:

```python
    d = consult(snap, store, budget, client=client)
    # Slow path was used — API call was made.
    assert client.last_kwargs is not None
    assert d is not None
    assert d.button_label == "A"
```

- [ ] **Step 3: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 8+ new failures from the slow-path tests (stub returns None).

- [ ] **Step 4: Implement the slow path**

In `booking_bot/ai_advisor.py`, replace the `_consult_slow_path` stub with:

```python
_ADVISOR_TOOL = {
    "name": "decide",
    "description": "Choose a single recovery action for the stuck bot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["click", "reload", "skip_row"],
            },
            "button_label": {
                "type": "string",
                "description": "Required iff action=='click'. Must exactly match one of the enabled buttons.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining the choice.",
            },
        },
        "required": ["action", "reason"],
    },
}


_SYSTEM_PROMPT = """\
You are a recovery advisor for an HPCL gas booking bot. The bot is
stuck and its deterministic rules cannot decide the next action. You
must return exactly one action from a restricted action space.

Allowed actions:
  click     - click an existing button from the provided enabled list.
              button_label must EXACTLY match one enabled button.
  reload    - reload the chatbot page. Use when the DOM looks broken
              (duplicate inputs, missing buttons, stale dialog).
  skip_row  - mark the current customer row as failed and advance.
              Use ONLY when the stuck state is specific to this row
              (e.g. payment pending, duplicate booking, KYC issue).
              Never use skip_row to escape a menu or UI glitch.

Hard rules:
- You may NEVER invent a button label not in the enabled list.
- You may NEVER type text, fill inputs, or navigate URLs.
- You may NEVER act on NEEDS_OPERATOR_AUTH or NEEDS_OPERATOR_OTP
  states - those are handled deterministically.
- Return ONE decide() tool call. No prose outside the tool call.

Prefer click over reload. Prefer reload over skip_row. skip_row is
the last resort and is rate-limited.
"""


def _build_user_prompt(snapshot: AdvisorSnapshot, few_shots: list[dict]) -> str:
    """Render the user-side prompt. few_shots are the top-k similar
    incidents from the store."""
    lines = []
    if few_shots:
        lines.append("Past similar incidents (actions that worked before for stuck shapes like this one):")
        for rec in few_shots:
            lines.append(json.dumps({
                "state": rec.get("state"),
                "buttons": rec.get("buttons_sorted"),
                "last_bubble_excerpt": rec.get("last_bubble_excerpt"),
                "chosen_action": rec.get("chosen_action"),
                "recovered_to_state": rec.get("recovered_to_state"),
                "occurrences": rec.get("occurrences"),
            }, ensure_ascii=False))
        lines.append("")
    lines.append("Current stuck state:")
    lines.append(f"  state: {snapshot.state}")
    lines.append(f"  enabled_buttons: {list(snapshot.enabled_buttons)}")
    lines.append(f'  last_bubble_text: "{snapshot.last_bubble_text}"')
    lines.append("  recent_actions:")
    for a in snapshot.recent_actions:
        lines.append(f"    - {a}")
    lines.append(f"  row_hint: {snapshot.row_hint}")
    lines.append("")
    lines.append("What should the bot do next?")
    return "\n".join(lines)


def _extract_tool_call(message) -> dict | None:
    """Pull the first tool_use block's input dict out of an Anthropic
    Message. Returns None if no tool_use block is present or the
    message shape is unexpected."""
    content = getattr(message, "content", None)
    if not content:
        return None
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "tool_use":
            return getattr(block, "input", None) or {}
    return None


def _get_client(client):
    """Return the caller-supplied client, or construct a real
    anthropic.Anthropic if one wasn't provided. If the SDK import or
    construction fails (no API key, SDK missing), returns None."""
    if client is not None:
        return client
    try:
        import anthropic  # type: ignore
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("ai_advisor: ANTHROPIC_API_KEY unset; advisor disabled this call")
            return None
        return anthropic.Anthropic(timeout=config.ADVISOR_API_TIMEOUT_S)
    except Exception as e:
        log.warning(f"ai_advisor: could not construct Anthropic client ({e})")
        return None


def _consult_slow_path(
    snapshot: AdvisorSnapshot,
    store: IncidentStore,
    budget: AdvisorBudget,
    *,
    client,
) -> Decision | None:
    real_client = _get_client(client)
    if real_client is None:
        return None

    few_shots = store.similar(snapshot.state, snapshot.enabled_buttons, top_k=5)
    user_prompt = _build_user_prompt(snapshot, few_shots)

    budget.record_call()

    try:
        response = real_client.messages.create(
            model=config.ADVISOR_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            tools=[_ADVISOR_TOOL],
            tool_choice={"type": "tool", "name": "decide"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        log.warning(
            f"ai_advisor: API call failed ({type(e).__name__}: {e}); "
            f"advisor declined"
        )
        return None

    tool_input = _extract_tool_call(response)
    if tool_input is None:
        log.warning("ai_advisor: API response had no tool_use block; advisor declined")
        return None

    decision = Decision(
        action=tool_input.get("action", ""),
        button_label=tool_input.get("button_label"),
        reason=tool_input.get("reason", ""),
    )

    if not validate_decision(decision, snapshot):
        log.warning(
            f"ai_advisor: invalid decision rejected "
            f"(action={decision.action!r} label={decision.button_label!r} "
            f"enabled={list(snapshot.enabled_buttons)!r})"
        )
        return None

    log.info(
        f"ai_advisor: path=api state={snapshot.state!r} "
        f"buttons={list(snapshot.enabled_buttons)!r} "
        f"decision={decision.action}/{decision.button_label!r} "
        f"reason={decision.reason!r} "
        f"budget={budget.calls_made}/{budget.max_calls} "
        f"skips={budget.total_skips}/{budget.max_total_skips}"
    )
    return decision
```

Also update the `consult` function's last line so it actually calls `_consult_slow_path` again:

```python
    return _consult_slow_path(snapshot, store, budget, client=client)
```

- [ ] **Step 5: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All tests pass (~49 total).

- [ ] **Step 6: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): consult slow path with Anthropic tool-use validation"
```

---

## Task 12: `apply_advisor_decision` — dispatcher

**Files:**
- Modify: `booking_bot/ai_advisor.py`
- Modify: `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing tests for the dispatcher**

Append to `tests/test_ai_advisor.py`:

```python
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
    assert budget.consecutive_skips == 0  # click resets skip streak


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
    # Budget updated.
    assert budget.total_skips == 1
    assert budget.consecutive_skips == 1


def test_apply_decision_invalid_action_returns_declined(monkeypatch):
    frame = FakeFrame()
    page = FakePage()
    # Construct a bogus decision that bypasses validate_decision at the
    # call site — in practice validate_decision already ran, but the
    # dispatcher must be defensive too.
    decision = Decision(action="nonsense", button_label=None, reason="x")  # type: ignore[arg-type]
    budget = AdvisorBudget()

    result = apply_advisor_decision(decision, frame, page, budget=budget)
    assert result == "declined"
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: 4 new `ImportError` failures on `apply_advisor_decision`.

- [ ] **Step 3: Add `apply_advisor_decision` to ai_advisor.py**

Add to imports at the top:

```python
from booking_bot.exceptions import AdvisorSkipRow
```

Append:

```python
def apply_advisor_decision(
    decision: Decision,
    frame,
    page,
    *,
    budget: AdvisorBudget,
) -> str:
    """Dispatch on decision.action. Returns 'acted' if the action was
    executed, 'declined' if the action was not recognised.

    For action='click', delegates to playbook._click_by_action.
    For action='reload', calls page.reload + page.wait_for_timeout.
    For action='skip_row', raises AdvisorSkipRow(reason) — the row
    loop is responsible for catching it and writing the ISSUE row.

    Updates budget counters on successful dispatch: click/reload
    call record_non_skip_decision, skip_row calls record_skip.
    """
    if decision.action == "click":
        # Local import keeps the module's top-level imports slim and
        # avoids a circular import risk (playbook imports from chat,
        # chat is a leaf, ai_advisor is loaded from cli).
        from booking_bot.playbook import Action, _click_by_action
        _click_by_action(
            frame,
            Action(
                kind="click",
                button_text=decision.button_label,
                button_id=None,
            ),
        )
        budget.record_non_skip_decision()
        return "acted"

    if decision.action == "reload":
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
        budget.record_non_skip_decision()
        return "acted"

    if decision.action == "skip_row":
        budget.record_skip()
        raise AdvisorSkipRow(decision.reason)

    log.warning(f"apply_advisor_decision: unknown action {decision.action!r}; declining")
    return "declined"
```

- [ ] **Step 4: Run to confirm tests pass**

Run: `python -m pytest tests/test_ai_advisor.py -q`
Expected: All tests pass (~53 total).

- [ ] **Step 5: Commit**

```bash
git add booking_bot/ai_advisor.py tests/test_ai_advisor.py
git commit -m "feat(advisor): apply_advisor_decision dispatcher + AdvisorSkipRow"
```

---

## Task 13: Wire advisor into `_recover_with_playbook`

**Files:**
- Modify: `booking_bot/cli.py`

- [ ] **Step 1: Find the existing post-reload reset block**

Read `booking_bot/cli.py` lines 1104–1136. Target the block:

```python
    try:
        playbook_mod.reset_to_customer_entry(frame, pb)
    except (OptionNotFoundError, ChatStuckError, GatewayError) as e:
        log.warning(
            f"reset after reload failed ({type(e).__name__}: {e}); "
            f"falling back to replay_auth"
        )
        playbook_mod.replay_auth(frame, pb)
    return frame
```

- [ ] **Step 2: Replace with advisor-fallback-aware version**

Modify the block to:

```python
    try:
        playbook_mod.reset_to_customer_entry(frame, pb)
    except (OptionNotFoundError, ChatStuckError, GatewayError) as e:
        log.warning(
            f"reset after reload failed ({type(e).__name__}: {e})"
        )
        # Advisor fallback: when deterministic recovery has genuinely
        # exhausted, consult the AI advisor. The advisor may return a
        # click, a reload, or a skip_row decision — or decline (None).
        # On decline we fall through to the existing replay_auth path,
        # preserving prior behaviour.
        advisor_handled = _try_advisor_fallback(
            frame, page, pb,
            current_row_idx=getattr(_try_advisor_fallback, "_row_idx_hint", None),
        )
        if advisor_handled == "acted":
            return frame
        if advisor_handled == "declined":
            log.warning("advisor declined; falling back to replay_auth")
        playbook_mod.replay_auth(frame, pb)
    return frame
```

- [ ] **Step 3: Add the helper `_try_advisor_fallback` above `_recover_with_playbook`**

Add imports near the top of `cli.py`:

```python
from booking_bot.ai_advisor import (
    AdvisorBudget,
    IncidentStore,
    apply_advisor_decision,
    build_snapshot,
    consult,
)
```

Insert above `_recover_with_playbook`:

```python
# Session-scoped advisor state. Initialized lazily on first use so the
# bot never touches the Anthropic SDK unless it actually needs to.
_advisor_budget: AdvisorBudget | None = None
_advisor_store: IncidentStore | None = None


def _get_advisor_state():
    global _advisor_budget, _advisor_store
    if _advisor_budget is None:
        _advisor_budget = AdvisorBudget()
    if _advisor_store is None:
        _advisor_store = IncidentStore(config.ADVISOR_INCIDENTS_PATH)
    return _advisor_budget, _advisor_store


def _try_advisor_fallback(frame, page, pb, current_row_idx: int | None) -> str:
    """Called from _recover_with_playbook after deterministic recovery
    has raised. Returns one of:
      - "acted":    advisor picked an action (click/reload) and the
                    bot should continue with the returned frame.
      - "declined": advisor refused or returned None; caller should
                    fall back to the existing replay_auth path.
      - "skip_row": advisor chose skip_row; AdvisorSkipRow has been
                    raised and will propagate out to the row loop.
                    This function never returns "skip_row" — it
                    always propagates the exception.
    """
    if not config.ADVISOR_ENABLED:
        return "declined"
    budget, store = _get_advisor_state()

    # Re-detect current state as the snapshot baseline.
    try:
        current_state = chat.detect_state(frame)
    except Exception as e:
        log.warning(f"advisor fallback: detect_state failed ({e}); declining")
        return "declined"

    row_hint = None
    if current_row_idx is not None:
        row_hint = f"row {current_row_idx}"
    snapshot = build_snapshot(
        frame,
        state=current_state,
        recent_actions=[],  # intentionally blank for now; future: recent log lines
        row_hint=row_hint,
    )

    pre_state = snapshot.state
    pre_buttons = snapshot.enabled_buttons

    decision = consult(snapshot, store, budget, client=None)
    if decision is None:
        return "declined"

    # Dispatch. AdvisorSkipRow propagates.
    try:
        outcome = apply_advisor_decision(decision, frame, page, budget=budget)
    except Exception as e:
        log.warning(
            f"advisor fallback: apply_advisor_decision raised "
            f"{type(e).__name__}: {e}"
        )
        raise

    if outcome != "acted":
        return "declined"

    # Allow the DOM to settle before re-detecting.
    try:
        chat.wait_until_settled(frame)
    except Exception as e:
        log.warning(f"advisor fallback: wait_until_settled failed ({e})")
        return "declined"

    try:
        new_state = chat.detect_state(frame)
    except Exception as e:
        log.warning(f"advisor fallback: post-action detect_state failed ({e})")
        return "declined"

    # Record success only if the state genuinely changed to a known
    # recovery state. We do NOT append an incident for UNKNOWN -> UNKNOWN
    # or for actions that left us in the same shape.
    if new_state != pre_state and new_state != "UNKNOWN":
        try:
            store.record_success(
                snapshot,
                decision,
                recovered_to=new_state,
            )
            log.info(
                f"advisor fallback: recorded success for "
                f"state={pre_state!r} buttons={list(pre_buttons)!r} "
                f"-> {new_state!r}"
            )
        except Exception as e:
            log.warning(f"advisor fallback: record_success failed ({e})")

    return "acted"
```

- [ ] **Step 4: Smoke-run the test suite**

Run: `python -m pytest -q`
Expected: all existing tests + advisor tests pass.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/cli.py
git commit -m "feat(advisor): wire advisor fallback into _recover_with_playbook"
```

---

## Task 14: Row loop catches `AdvisorSkipRow`

**Files:**
- Modify: `booking_bot/cli.py`

- [ ] **Step 1: Locate the row loop's except block**

Read `booking_bot/cli.py` lines 856–900. The relevant block is:

```python
                except (KeyboardInterrupt, FatalError):
                    raise
                except Exception as row_e:
                    log.error(
                        f"row {row_idx} ({phone}) unexpected error: "
                        f"{type(row_e).__name__}: {row_e}"
                    )
                    ...
```

- [ ] **Step 2: Add an `AdvisorSkipRow` catch before the generic Exception handler**

Modify to insert a new except block:

```python
                except (KeyboardInterrupt, FatalError):
                    raise
                except AdvisorSkipRow as skip_e:
                    log.warning(
                        f"row {row_idx} ({phone}): advisor chose skip_row "
                        f"(reason={skip_e.reason!r}); locking as ISSUE and advancing"
                    )
                    store.write_issue(
                        row_idx, phone,
                        f"advisor_skipped:{skip_e.reason}",
                        raw="",
                    )
                    # Skip does NOT count toward consecutive_row_failures —
                    # the advisor explicitly judged this row hopeless, so
                    # it's an intentional single-row drop, not a cascade.
                    continue
                except Exception as row_e:
                    ...
```

Also add the import at the top of `cli.py`:

```python
from booking_bot.exceptions import (
    # ...existing imports...
    AdvisorSkipRow,
)
```

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest -q`
Expected: all tests still pass.

- [ ] **Step 4: Commit**

```bash
git add booking_bot/cli.py
git commit -m "feat(advisor): row loop catches AdvisorSkipRow and locks row as ISSUE"
```

---

## Task 15: `scripts/bootstrap_incidents.py` — log parser core

**Files:**
- Create: `scripts/bootstrap_incidents.py`
- Create: `tests/test_bootstrap_incidents.py`
- Create: `tests/fixtures/bootstrap_log_sample.log`

- [ ] **Step 1: Create the canned log fixture**

Create `tests/fixtures/bootstrap_log_sample.log`:

```
2026-04-13 19:32:27 INFO chat: detect_state -> MAIN_MENU
2026-04-13 19:32:28 INFO playbook: clicked 'Booking Services' (id=b1)
2026-04-13 19:32:30 INFO chat: detect_state -> BOOK_FOR_OTHERS_MENU
2026-04-13 19:32:31 INFO playbook: clicked 'Book for Others' (id=b2)
2026-04-13 19:32:33 INFO chat: detect_state -> UNKNOWN
2026-04-13 19:32:34 WARNING playbook: reset stuck on dead-end dialog (enabled=['Make Payment', 'Previous Menu']); clicking 'Previous Menu' to back out and retrying reset
2026-04-13 19:32:35 INFO playbook: clicked 'Previous Menu' (id=bprev)
2026-04-13 19:32:37 INFO chat: detect_state -> BOOK_FOR_OTHERS_MENU
2026-04-13 19:32:39 INFO playbook: clicked 'Book for Others' (id=b2)
2026-04-13 19:32:41 INFO chat: detect_state -> READY_FOR_CUSTOMER
2026-04-13 19:32:42 INFO chat: typed customer phone 9876543210
2026-04-13 19:33:00 INFO chat: detect_state -> UNKNOWN
2026-04-13 19:33:01 WARNING playbook: reset stuck on dead-end dialog (enabled=['Make Payment', 'Previous Menu']); clicking 'Previous Menu' to back out and retrying reset
2026-04-13 19:33:02 INFO playbook: clicked 'Previous Menu' (id=bprev)
2026-04-13 19:33:04 INFO chat: detect_state -> BOOK_FOR_OTHERS_MENU
```

- [ ] **Step 2: Write failing tests for the log parser**

Create `tests/test_bootstrap_incidents.py`:

```python
"""Unit tests for scripts/bootstrap_incidents.py. Uses a small canned
log fixture under tests/fixtures/ so the test is fast and repeatable."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "bootstrap_log_sample.log"


def test_fixture_exists():
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"


def test_parse_log_extracts_stuck_to_recovered_pair():
    from scripts.bootstrap_incidents import parse_log_file
    incidents = parse_log_file(FIXTURE)
    # The fixture has 2 identical stuck->recovered pairs.
    assert len(incidents) == 2
    inc = incidents[0]
    assert inc["state"] == "UNKNOWN"
    assert sorted(inc["buttons_sorted"]) == ["Make Payment", "Previous Menu"]
    assert inc["chosen_action"]["action"] == "click"
    assert inc["chosen_action"]["button_label"] == "Previous Menu"
    assert inc["recovered_to_state"] == "BOOK_FOR_OTHERS_MENU"


def test_aggregate_incidents_dedupes_by_key():
    from scripts.bootstrap_incidents import aggregate_incidents, parse_log_file
    incidents = parse_log_file(FIXTURE)
    agg = aggregate_incidents(incidents)
    assert len(agg) == 1  # Two identical pairs aggregate to one.
    only = list(agg.values())[0]
    assert only["occurrences"] == 2


def test_scrub_phone_numbers_in_text():
    from scripts.bootstrap_incidents import scrub_pii
    assert scrub_pii("called 9876543210 today") == "called ****REDACTED**** today"
    assert scrub_pii("no phone here") == "no phone here"
    assert scrub_pii("two 1234567890 and 9999999999") == "two ****REDACTED**** and ****REDACTED****"


def test_parse_log_ignores_stuck_without_recovery(tmp_path):
    """A stuck marker with no subsequent known-state transition must
    NOT produce an incident."""
    log_path = tmp_path / "orphan.log"
    log_path.write_text(
        "2026-04-13 19:32:33 INFO chat: detect_state -> UNKNOWN\n"
        "2026-04-13 19:32:34 WARNING playbook: reset stuck on dead-end dialog (enabled=['A', 'B']); clicking 'A' to back out and retrying reset\n"
        "2026-04-13 19:32:35 INFO playbook: clicked 'A' (id=bA)\n"
        # No detect_state -> KNOWN_STATE within 30s.
    )
    from scripts.bootstrap_incidents import parse_log_file
    incidents = parse_log_file(log_path)
    assert incidents == []
```

- [ ] **Step 3: Run to confirm tests fail**

Run: `python -m pytest tests/test_bootstrap_incidents.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.bootstrap_incidents'` or similar.

- [ ] **Step 4: Create `scripts/bootstrap_incidents.py` core**

```python
"""One-shot log-mining script: scans booking_bot log files and extracts
confirmed stuck->recovered patterns into data/incidents.jsonl. Run this
once before the first overnight session to seed the advisor's episodic
memory with real historical wins.

The algorithm:
  1. Parse each log line-by-line.
  2. Maintain a sliding window of recent events.
  3. When we see a "reset stuck on dead-end dialog" line, capture the
     enabled buttons and the chosen click label.
  4. Look forward in the same file for the next "detect_state -> X"
     line where X is in {MAIN_MENU, BOOK_FOR_OTHERS_MENU, READY_FOR_CUSTOMER}.
     If found within 30 seconds of the stuck marker, emit an incident.
  5. PII-scrub phone numbers (10 digits) before writing.
  6. Deduplicate by (state, sorted_buttons) and aggregate occurrences.
  7. Write data/incidents.jsonl atomically.

Usage:
  python scripts/bootstrap_incidents.py [--logs-dir logs/] [--output data/incidents.jsonl] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Import the advisor module for its canonical key function. We add the
# repo root to sys.path so this script works when invoked from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booking_bot.ai_advisor import IncidentStore  # noqa: E402


LOG_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)
DETECT_STATE_RE = re.compile(r"detect_state\s*->\s*(\w+)")
RESET_STUCK_RE = re.compile(
    r"reset stuck on dead-end dialog \(enabled=(\[.*?\])\); "
    r"clicking '([^']+)'"
)
CLICKED_RE = re.compile(r"clicked '([^']+)'")

KNOWN_RECOVERED_STATES = {
    "MAIN_MENU",
    "BOOK_FOR_OTHERS_MENU",
    "READY_FOR_CUSTOMER",
}

RECOVERY_WINDOW = timedelta(seconds=30)

_PHONE_RE = re.compile(r"\b\d{10}\b")


def scrub_pii(text: str) -> str:
    """Replace 10-digit phone numbers with a REDACTED marker. Run on
    every string that goes into the corpus."""
    if not text:
        return text
    return _PHONE_RE.sub("****REDACTED****", text)


def _parse_ts(line: str) -> datetime | None:
    m = LOG_TIMESTAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_buttons_list(raw: str) -> list[str]:
    """Turn a Python-repr list-of-strings like "['A', 'B']" into
    ['A', 'B']. We ast.literal_eval would be cleaner but introduces
    an import; a regex-based parser is enough for the log format."""
    inner = raw.strip().strip("[]")
    if not inner:
        return []
    # Split on ', ' between quoted strings.
    items = re.findall(r"'((?:[^'\\]|\\.)*)'", inner)
    return items


def parse_log_file(path: Path) -> list[dict]:
    """Return a list of incident dicts found in a single log file.
    Each dict is ready to pass to aggregate_incidents."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    incidents: list[dict] = []

    # Scan for reset-stuck markers, then look forward for a detect_state
    # -> KNOWN transition within RECOVERY_WINDOW.
    for i, line in enumerate(lines):
        m = RESET_STUCK_RE.search(line)
        if not m:
            continue
        ts = _parse_ts(line)
        if ts is None:
            continue
        buttons_raw, click_label = m.group(1), m.group(2)
        buttons = _parse_buttons_list(buttons_raw)
        if not buttons:
            continue

        # Look forward for a recovered state within the window.
        recovered_to: str | None = None
        for j in range(i + 1, min(i + 200, len(lines))):
            fwd_ts = _parse_ts(lines[j])
            if fwd_ts is not None and fwd_ts - ts > RECOVERY_WINDOW:
                break
            dm = DETECT_STATE_RE.search(lines[j])
            if dm and dm.group(1) in KNOWN_RECOVERED_STATES:
                recovered_to = dm.group(1)
                break
        if recovered_to is None:
            continue

        incidents.append({
            "key": IncidentStore.make_key("UNKNOWN", buttons),
            "state": "UNKNOWN",
            "buttons_sorted": sorted(buttons),
            "last_bubble_excerpt": scrub_pii(line.strip())[:500],
            "chosen_action": {
                "action": "click",
                "button_label": click_label,
                "reason": f"bootstrapped from {path.name}:{i+1}",
            },
            "outcome": "recovered",
            "recovered_to_state": recovered_to,
            "source": "bootstrap",
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "occurrences": 1,
        })
    return incidents


def aggregate_incidents(incidents: list[dict]) -> dict[str, dict]:
    """Group by key and sum occurrences. Newest timestamp wins."""
    agg: dict[str, dict] = {}
    for inc in incidents:
        key = inc["key"]
        if key not in agg:
            agg[key] = dict(inc)
        else:
            agg[key]["occurrences"] += inc["occurrences"]
            if inc["timestamp"] > agg[key]["timestamp"]:
                agg[key]["timestamp"] = inc["timestamp"]
                agg[key]["chosen_action"] = inc["chosen_action"]
    return agg
```

- [ ] **Step 5: Run tests to verify parser works**

Run: `python -m pytest tests/test_bootstrap_incidents.py -q`
Expected: All 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/bootstrap_incidents.py tests/test_bootstrap_incidents.py tests/fixtures/bootstrap_log_sample.log
git commit -m "feat(advisor): bootstrap_incidents log parser core + fixture"
```

---

## Task 16: `scripts/bootstrap_incidents.py` — CLI wrapper + atomic write

**Files:**
- Modify: `scripts/bootstrap_incidents.py`
- Modify: `tests/test_bootstrap_incidents.py`

- [ ] **Step 1: Write failing tests for the CLI and merge behaviour**

Append to `tests/test_bootstrap_incidents.py`:

```python
def test_write_incidents_to_new_file(tmp_path):
    from scripts.bootstrap_incidents import write_incidents
    path = tmp_path / "data" / "incidents.jsonl"
    records = {
        "UNKNOWN|a|b": {
            "key": "UNKNOWN|a|b",
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "r"},
            "outcome": "recovered",
            "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap",
            "timestamp": "2026-04-15T00:00:00Z",
            "occurrences": 1,
        }
    }
    write_incidents(records, path)
    assert path.exists()
    loaded = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(loaded) == 1
    rec = json.loads(loaded[0])
    assert rec["key"] == "UNKNOWN|a|b"


def test_write_incidents_preserves_existing_runtime_records_on_key_collision(tmp_path):
    """When merging bootstrap output into an existing file, runtime-sourced
    records must not be overwritten by bootstrap records with the same key."""
    from scripts.bootstrap_incidents import write_incidents
    path = tmp_path / "incidents.jsonl"
    # Pre-existing runtime record.
    existing = {
        "key": "UNKNOWN|a|b",
        "state": "UNKNOWN",
        "buttons_sorted": ["a", "b"],
        "last_bubble_excerpt": "runtime",
        "chosen_action": {"action": "click", "button_label": "b", "reason": "runtime win"},
        "outcome": "recovered",
        "recovered_to_state": "MAIN_MENU",
        "source": "runtime",
        "timestamp": "2026-04-15T10:00:00Z",
        "occurrences": 7,
    }
    path.write_text(json.dumps(existing) + "\n")

    # Bootstrap tries to overwrite with a different chosen_action.
    new_records = {
        "UNKNOWN|a|b": {
            "key": "UNKNOWN|a|b",
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "bootstrap",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "boot"},
            "outcome": "recovered",
            "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap",
            "timestamp": "2026-04-15T00:00:00Z",
            "occurrences": 1,
        }
    }
    write_incidents(new_records, path)

    loaded = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(loaded) == 1
    rec = loaded[0]
    # Runtime record was preserved.
    assert rec["source"] == "runtime"
    assert rec["chosen_action"]["button_label"] == "b"


def test_cli_dry_run_does_not_write_file(tmp_path, capsys):
    from scripts.bootstrap_incidents import run_cli
    out_path = tmp_path / "incidents.jsonl"
    exit_code = run_cli([
        "--logs-dir", str(FIXTURE.parent),
        "--output", str(out_path),
        "--dry-run",
    ])
    assert exit_code == 0
    assert not out_path.exists()
    captured = capsys.readouterr()
    assert "bootstrapped" in captured.out.lower()


def test_cli_writes_output_file(tmp_path):
    from scripts.bootstrap_incidents import run_cli
    out_path = tmp_path / "incidents.jsonl"
    exit_code = run_cli([
        "--logs-dir", str(FIXTURE.parent),
        "--output", str(out_path),
    ])
    assert exit_code == 0
    assert out_path.exists()
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `python -m pytest tests/test_bootstrap_incidents.py -q`
Expected: 4 new failures.

- [ ] **Step 3: Add `write_incidents` and `run_cli` to the script**

Append to `scripts/bootstrap_incidents.py`:

```python
def write_incidents(records: dict[str, dict], path: Path) -> None:
    """Atomic write of the aggregated records to `path`. If `path`
    already exists, merge the new records with the existing file,
    preferring runtime-sourced records over bootstrap-sourced ones
    on key collisions."""
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "key" in rec:
                existing[rec["key"]] = rec

    merged: dict[str, dict] = dict(existing)
    for key, new_rec in records.items():
        prior = existing.get(key)
        if prior is None:
            merged[key] = new_rec
        else:
            # Runtime wins on collision.
            if prior.get("source") == "runtime" and new_rec.get("source") == "bootstrap":
                continue
            merged[key] = new_rec

    # Atomic write-to-temp + rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".incidents.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in merged.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        raise


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed data/incidents.jsonl from existing logs."
    )
    parser.add_argument(
        "--logs-dir",
        default="logs",
        help="Directory to scan for *.log files (default: logs)",
    )
    parser.add_argument(
        "--output",
        default="data/incidents.jsonl",
        help="Path to write the incidents.jsonl file (default: data/incidents.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary only; do not write the output file",
    )
    args = parser.parse_args(argv)

    logs_dir = Path(args.logs_dir)
    out_path = Path(args.output)

    all_incidents: list[dict] = []
    log_files: list[Path] = sorted(logs_dir.glob("*.log"))
    if not log_files:
        print(f"bootstrap: no *.log files in {logs_dir}", file=sys.stderr)
        return 1

    for lf in log_files:
        try:
            all_incidents.extend(parse_log_file(lf))
        except Exception as e:
            print(f"bootstrap: skipping {lf.name}: {e}", file=sys.stderr)

    agg = aggregate_incidents(all_incidents)

    print(
        f"bootstrapped {len(all_incidents)} incidents from "
        f"{len(log_files)} log files; {len(agg)} unique keys"
    )
    # Top 10 by occurrences.
    top = sorted(agg.values(), key=lambda r: -r["occurrences"])[:10]
    for rec in top:
        print(
            f"  {rec['occurrences']}x {rec['state']} buttons={rec['buttons_sorted']} "
            f"-> click {rec['chosen_action']['button_label']!r}"
        )

    if args.dry_run:
        print("dry-run: no file written")
        return 0

    write_incidents(agg, out_path)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_bootstrap_incidents.py -q`
Expected: All 9 tests pass.

- [ ] **Step 5: Full test suite sanity check**

Run: `python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/bootstrap_incidents.py tests/test_bootstrap_incidents.py
git commit -m "feat(advisor): bootstrap_incidents CLI with atomic merge"
```

---

## Task 17: End-to-end sanity — bootstrap against real logs + smoke test

**Files:**
- None (operational task)

- [ ] **Step 1: Dry-run bootstrap against real logs**

Run: `python scripts/bootstrap_incidents.py --dry-run`
Expected: summary line like `bootstrapped N incidents from 132 log files; K unique keys`. Top-10 list shows most-frequent stuck shapes. No file written.

- [ ] **Step 2: Review the dry-run output**

Read the summary. If any top-10 entry is obviously wrong (e.g. a button label that looks like a customer phone number), investigate the source log before proceeding — phone numbers should have been scrubbed; a raw number means the regex missed something.

- [ ] **Step 3: Write the real incidents.jsonl**

Run: `python scripts/bootstrap_incidents.py`
Expected: `wrote data/incidents.jsonl`. File exists at `data/incidents.jsonl`.

- [ ] **Step 4: Spot-check the written file**

Read the first few lines of `data/incidents.jsonl`. Each line must:
- Be valid JSON
- Have `source: "bootstrap"`
- Have `outcome: "recovered"`
- Have a `button_label` that is NOT a 10-digit number (PII scrub check)

- [ ] **Step 5: Commit the seeded corpus**

```bash
git add data/incidents.jsonl
git commit -m "feat(advisor): seed incidents corpus from 132 existing log files"
```

- [ ] **Step 6: Optional — run the live-API smoke test**

Only if `ANTHROPIC_API_KEY` is set in the environment. This makes one real API call and costs a few cents.

Create a temporary file `smoke_advisor.py` at the repo root:

```python
"""Live smoke test: one real Anthropic API call with a canned stuck
state. Not run in CI. Asserts the advisor returns a valid Decision."""
from booking_bot.ai_advisor import (
    AdvisorBudget, AdvisorSnapshot, IncidentStore, consult,
)
from booking_bot import config

snap = AdvisorSnapshot(
    state="UNKNOWN",
    enabled_buttons=("Make Payment", "Previous Menu"),
    last_bubble_text="Your booking is payment pending. Please complete payment first.",
    recent_actions=("clicked Book for Others", "typed ****REDACTED****"),
    empty_input_names=(),
    row_hint="row 42/500",
)
store = IncidentStore(config.ADVISOR_INCIDENTS_PATH)
budget = AdvisorBudget()
decision = consult(snap, store, budget)
print("decision:", decision)
assert decision is not None, "advisor returned None on canned stuck state"
assert decision.action in {"click", "reload", "skip_row"}
if decision.action == "click":
    assert decision.button_label in snap.enabled_buttons
print("smoke test PASS")
```

Run: `python smoke_advisor.py`
Expected: one `decision: Decision(...)` line, then `smoke test PASS`. Delete `smoke_advisor.py` afterwards.

- [ ] **Step 7: Remove the smoke test file**

```bash
rm smoke_advisor.py
```

- [ ] **Step 8: Full suite final check**

Run: `python -m pytest -q`
Expected: all tests pass. Count should be ~118 (99 existing + ~19 advisor + ~9 bootstrap).

---

## Self-review of this plan

**Spec coverage check:**
- Architecture and file layout → Tasks 1–16 create all listed files.
- Snapshot/Decision/IncidentStore/AdvisorBudget dataclasses → Tasks 2, 3, 5, 6, 7.
- validate_decision choke point → Task 4.
- build_snapshot + pure helper → Task 8.
- Fast path → Task 10.
- Slow path with tool-use, system prompt, user prompt, few-shots → Task 11.
- Budget enforcement → Tasks 3 and 10 and 11.
- Refusal on NEEDS_OPERATOR_AUTH/OTP → Task 10.
- apply_advisor_decision dispatcher → Task 12.
- AdvisorSkipRow exception → Task 1; caught in row loop → Task 14.
- cli._recover_with_playbook fallback branch → Task 13.
- Bootstrap script (parser + PII scrub + CLI + atomic merge) → Tasks 15, 16.
- Config constants → Task 1.
- Dependency add → Task 1.
- Tests: unit + bootstrap + live-API opt-in → Tasks 2–16 + 17.
- Rollback path (`ADVISOR_ENABLED = False`) → Task 1 adds the constant, Task 10 respects it.

**Placeholder scan:** no TBD / TODO / "fill in details" text. Every code block is complete. Every test assertion is concrete.

**Type consistency:** `AdvisorSnapshot` uses tuples throughout; `Decision.action` is `Literal["click","reload","skip_row"]`; `IncidentStore.make_key` is used in tests, runtime, and bootstrap; `consult` signature `(snapshot, store, budget, *, client=None)` is identical across Tasks 10/11.
