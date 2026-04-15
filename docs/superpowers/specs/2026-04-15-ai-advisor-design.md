# AI Recovery Advisor — Design

**Date:** 2026-04-15
**Author:** Claude + operator
**Status:** Draft, pending user review

## Problem

Even after the overnight batch survivability work (see `2026-04-15-overnight-batch-survivability-design.md`), the bot still has a residual failure mode: **it gets stuck on `UNKNOWN` states and dead-end dialogs that no deterministic rule covers**. Every incident we've patched recently (payment-pending dead-end, operator-auth misclassification, ERR_ABORTED, dead-end escape) was a one-off: the bot silently rode into a DOM shape it didn't recognise, `reset_to_customer_entry` raised `OptionNotFoundError`, and the engineer had to read logs, write a new `_choose_reset_target` branch, ship it, and restart.

We have ~130 log files capturing dozens of real-world stuck shapes. A human reading those logs can almost always tell what the next click should have been. The deterministic rule-base cannot grow fast enough to keep up with HPCL's UI variance.

**What we want:** a narrowly scoped AI advisor that sits **behind** the existing deterministic recovery as a last-resort fallback. When `_recover_with_playbook` runs `reset_to_customer_entry` and it genuinely exhausts — no matching reset target, no playbook branch — the advisor is consulted. It sees the stuck-state snapshot and a corpus of past successful recoveries, and returns a single constrained action: click an existing button, reload the page, or skip the current row. The bot executes that action, logs the outcome, and appends the incident to the corpus so next time this exact shape is unstuck without an API call.

## Goals

- **Unstick on novel UNKNOWN states without a code patch.** Operator doesn't need to ship a new reset-target branch every time HPCL shows a new dialog.
- **Learn from every successful recovery.** Episodic memory (`data/incidents.jsonl`) grows over time. Exact structural matches (same state + same button shape) become free — no API call on repeats.
- **Bootstrap from existing 130 logs.** A one-shot offline script seeds the incident store with confirmed "stuck → recovered" patterns from history, so the first overnight run isn't cold-started.
- **Zero new happy-path authority for the AI.** The AI never types text, never navigates URLs, never picks a button label that isn't already visible, and never touches auth/OTP flows (those are owned by the survivability spec's cooldown + quiet retry).
- **Hard safety caps.** Per-session call budget, consecutive skip cap, total skip cap. A confused AI cannot burn down the night.
- **Clean rollback.** Delete one module, revert two modified files, and the bot is exactly as it is today.

## Prerequisites

This feature is **independent of the overnight batch survivability spec** (`2026-04-15-overnight-batch-survivability-design.md`). The advisor can be implemented and shipped before, after, or in parallel with the survivability work. Two interactions to be aware of:

- When `skip_row` is chosen, the advisor locks the row via `excel.write_issue(row_idx)` directly. It does not touch the survivability spec's `attempt_count` column. The AI's judgment to skip is authoritative — the row is hopeless, not retriable.
- The advisor must refuse to act on `NEEDS_OPERATOR_AUTH` / `NEEDS_OPERATOR_OTP` states regardless of whether survivability's cooldown + quiet-retry path exists. This refusal is enforced in `consult` itself, not via an external guard.

The `ANTHROPIC_API_KEY` environment variable must be set on the machine that runs the bot. If unset, the advisor silently stays disabled for that session (logged at startup) and the bot behaves identically to current main.

## Non-goals

- **Not invoked on `NEEDS_OPERATOR_AUTH` / `NEEDS_OPERATOR_OTP`.** Those states are handled deterministically by `auth.login_if_needed` + the survivability spec's cooldown + quiet-retry loop. Advisor must explicitly refuse these states.
- **Not a happy-path supervisor.** No AI call during normal booking flow. Only after `_recover_with_playbook` would otherwise raise.
- **No free-text typing.** The advisor cannot propose typing customer phones, names, counts, or OTPs. Text typing always comes from the Excel queue or the auth flow.
- **No CSS selector / DOM-script actions.** The AI only picks from the labels HPCL is currently showing as enabled buttons.
- **No cross-vendor support.** Anthropic SDK only. No LangChain, no embeddings, no vector store.
- **No real-time confidence scoring.** LLM self-reported confidence is unreliable; hard caps are the only safety layer that matters.

## Design

Seven sections. Sections 1–4 are the runtime feature; section 5 is the offline bootstrap; section 6 is error handling and safety; section 7 is testing.

### Section 1 — Architecture and file layout

**New files:**
- `booking_bot/ai_advisor.py` — the module. Exposes the single public entry point `consult(snapshot, store, budget) -> Decision | None`, plus the dataclasses `Snapshot` and `Decision` and the classes `IncidentStore` and `AdvisorBudget`.
- `data/incidents.jsonl` — append-only episodic corpus. One JSON object per line. Hand-editable.
- `scripts/bootstrap_incidents.py` — one-shot CLI that scans `logs/*.log` and seeds `incidents.jsonl` with confirmed stuck→recovered patterns. Run once before first overnight session.
- `tests/test_ai_advisor.py` — unit tests with a fake Anthropic client (no real API calls).
- `tests/test_bootstrap_incidents.py` — unit tests against a small canned log fixture.

**Modified files:**
- `booking_bot/cli.py` — `_recover_with_playbook` gets a fallback branch that calls `ai_advisor.consult` after existing deterministic recovery exhausts. The main run loop instantiates one `AdvisorBudget` and one `IncidentStore` per session and threads them through.
- `booking_bot/chat.py` — new helper `build_snapshot(frame, state) -> Snapshot` that packages the existing detect_state signals into the advisor's input shape. No change to state detection logic itself.
- `pyproject.toml` (or `requirements.txt`) — add `anthropic>=0.40` to dependencies.
- `booking_bot/config.py` — add three new constants (see Section 4).

**What stays untouched:** `playbook.py`, `auth.py`, `browser.py`, `excel.py`, and every existing state-detection path. Rollback is deletion of `ai_advisor.py` + revert of the two modified files + delete of `data/incidents.jsonl`.

### Section 2 — Data structures

**Snapshot** (advisor input, built by `chat.build_snapshot`):

```python
@dataclass(frozen=True)
class Snapshot:
    state: str                       # e.g. "UNKNOWN", "BOOK_FOR_OTHERS_MENU"
    enabled_buttons: tuple[str, ...] # exact labels HPCL shows, in DOM order
    last_bubble_text: str            # trimmed, max 500 chars
    recent_actions: tuple[str, ...]  # last 5 action log lines, trimmed
    empty_input_names: tuple[str, ...]  # safety classification input names
    row_hint: str | None             # "row 42/500, phone ending 1234" or None
```

**Decision** (advisor output, schema-validated):

```python
@dataclass(frozen=True)
class Decision:
    action: Literal["click", "reload", "skip_row"]
    button_label: str | None         # required iff action == "click"
    reason: str                      # short explanation, for logs
```

**Incident record** (one line of `incidents.jsonl`):

```json
{
  "key": "UNKNOWN|make payment|previous menu",
  "state": "UNKNOWN",
  "buttons_sorted": ["Make Payment", "Previous Menu"],
  "last_bubble_excerpt": "Your previous booking is payment pending. Please complete payment first.",
  "chosen_action": {"action": "click", "button_label": "Previous Menu", "reason": "Dead-end payment dialog; backing out to main flow"},
  "outcome": "recovered",
  "recovered_to_state": "BOOK_FOR_OTHERS_MENU",
  "source": "bootstrap",
  "timestamp": "2026-04-15T14:12:33Z",
  "occurrences": 7
}
```

**Matching key**: `(state, tuple(sorted(lower(b) for b in enabled_buttons)))`. Stored as a pipe-delimited string for JSON-friendly lookup. Exact structural match only — no fuzzy matching, no semantic search.

**Deduplication**: when a new successful incident matches the key of an existing record, `IncidentStore.record_success` increments `occurrences` and updates `timestamp` instead of appending a duplicate line. On flush, the store rewrites `incidents.jsonl` atomically (write-to-temp + rename).

### Section 3 — Runtime data flow

The advisor is called from one and only one place: `cli._recover_with_playbook`, as a fallback branch after the existing recovery logic has raised or exhausted. The sequence is:

1. Bot hits a stuck state (UNKNOWN after N polls, or `OptionNotFoundError` from `reset_to_customer_entry`).
2. Existing deterministic recovery runs to completion per current logic: `reset_to_customer_entry`, `_choose_reset_target` dispatch, reload+login cooldown path, etc.
3. **If still stuck** — the current `except OptionNotFoundError` branch that re-raises — we now intercept. Before re-raising:
   - Build snapshot: `snapshot = chat.build_snapshot(frame, current_state)`.
   - **Refusal check**: if `snapshot.state in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP")`, advisor is skipped — those are survivability-spec territory. Log and re-raise.
   - **Budget check**: if `budget.exhausted()`, advisor is skipped. Log and re-raise.
   - Call `decision = ai_advisor.consult(snapshot, store, budget)`.
4. `consult` internals:
   - **Fast path**: look up `(state, sorted_buttons)` in `store.by_key`. If found with `outcome=recovered` and `occurrences >= 1`, return the stored `chosen_action` directly **without an API call**. This is where past logs make the AI free.
   - **Slow path**: if no exact match, gather top-5 similar incidents as few-shot examples. Similarity is computed as Jaccard overlap on the lowercased button-label sets, restricted to same-state incidents; ties broken by recency (newer first). Build the prompt (system + few-shots + current snapshot). Call Anthropic API via tool-use format (see Section 4 for the tool schema). Validate response.
   - **Validation**: `decision.action in {"click", "reload", "skip_row"}`; if `"click"`, `decision.button_label` must case-insensitive exact-match a label in `snapshot.enabled_buttons`. Failure → return `None`.
   - **Budget accounting**: `budget.record_call()` on API-path invocations only. `budget.record_skip()` on skip decisions.
   - Return `Decision` or `None`.
5. Caller dispatches on `Decision.action`:
   - `"click"` — `playbook._click_by_action(frame, Action(kind="click", button_text=decision.button_label, button_id=None))`, then `chat.wait_until_settled(frame)`, then re-detect state.
   - `"reload"` — `browser.get_chat_frame(page, kick=True)`, then `chat.wait_until_settled(frame)`, then re-detect state.
   - `"skip_row"` — raise `ai_advisor.AdvisorSkipRow(reason)` (new exception defined in the advisor module). `cli.book_row` catches it and calls `excel.write_issue(row_idx)` directly to lock the row immediately (the AI has explicitly judged this row hopeless, so it bypasses any retry budget), then advances the queue.
6. If the new post-action state is different from the stuck state, `store.record_success(snapshot, decision, new_state)` appends/updates the incident. If the state is unchanged or worse, no corpus update is made — we only learn from wins.
7. If `consult` returned `None`, caller re-raises the original exception and existing crash-and-restart semantics take over.

### Section 4 — Anthropic API integration

**Model:** `claude-sonnet-4-6`. Cheap, fast, structured output reliably.

**SDK:** `anthropic` Python package, sync client. No streaming (we need the full tool-use response before we can act).

**API key:** read from `ANTHROPIC_API_KEY` environment variable. If unset at startup, `AdvisorBudget.__init__` logs a warning and sets `advisor_disabled=True` — the bot still runs, the advisor just never fires. This matches the "clean rollback" goal.

**Tool schema** (forces JSON shape via Anthropic tool use):

```python
ADVISOR_TOOL = {
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
```

**System prompt** (fixed, short):

```
You are a recovery advisor for an HPCL gas booking bot. The bot is
stuck and its deterministic rules cannot decide the next action. You
must return exactly one action from a restricted action space.

Allowed actions:
  click     — click an existing button from the provided enabled list.
              button_label must EXACTLY match one enabled button.
  reload    — reload the chatbot page. Use when the DOM looks broken
              (duplicate inputs, missing buttons, stale dialog).
  skip_row  — mark the current customer row as failed and advance.
              Use ONLY when the stuck state is specific to this row
              (e.g. payment pending, duplicate booking, KYC issue).
              Never use skip_row to escape a menu or UI glitch.

Hard rules:
- You may NEVER invent a button label not in the enabled list.
- You may NEVER type text, fill inputs, or navigate URLs.
- You may NEVER act on NEEDS_OPERATOR_AUTH or NEEDS_OPERATOR_OTP
  states — those are handled deterministically.
- Return ONE decide() tool call. No prose outside the tool call.

Prefer click over reload. Prefer reload over skip_row. skip_row is
the last resort and is rate-limited.
```

**User prompt** (templated per call):

```
Past similar incidents (actions that worked before for stuck shapes like this one):

{few_shot_incidents_json}   # top 5 by same-state + button overlap

Current stuck state:
  state: {state}
  enabled_buttons: {buttons}
  last_bubble_text: "{last_bubble_text}"
  recent_actions:
{recent_actions_bulleted}
  row_hint: {row_hint}

What should the bot do next?
```

**Timeout:** 10 seconds per API call, wall-clock. `anthropic.Anthropic(..., timeout=10.0)`.

**Budget** (new class `AdvisorBudget`):

```python
class AdvisorBudget:
    def __init__(self):
        self.calls_made = 0
        self.consecutive_skips = 0
        self.total_skips = 0
        # limits pulled from config at construction time, not class-level
        self.max_calls = config.ADVISOR_MAX_CALLS_PER_SESSION
        self.max_consecutive_skips = config.ADVISOR_MAX_CONSECUTIVE_SKIPS
        self.max_total_skips = config.ADVISOR_MAX_TOTAL_SKIPS
```

- `exhausted()` returns True if any of `calls_made >= max_calls`, `consecutive_skips >= max_consecutive_skips`, or `total_skips >= max_total_skips`.
- `record_call()` increments `calls_made`. **Not called on fast-path hits** — fast-path is free because it does not invoke the API.
- `record_skip()` increments both `consecutive_skips` and `total_skips`.
- `record_non_skip_decision()` resets `consecutive_skips` to 0 (called after any click/reload decision that the caller acts on).
- When `exhausted()` is True, `consult` refuses further API calls and returns `None` immediately.

**New config constants** (in `booking_bot/config.py`):

```python
ADVISOR_ENABLED              = True
ADVISOR_MODEL                = "claude-sonnet-4-6"
ADVISOR_API_TIMEOUT_S        = 10.0
ADVISOR_MAX_CALLS_PER_SESSION = 50
ADVISOR_MAX_CONSECUTIVE_SKIPS = 3
ADVISOR_MAX_TOTAL_SKIPS       = 15
ADVISOR_INCIDENTS_PATH        = Path("data/incidents.jsonl")
```

`ADVISOR_ENABLED = False` disables the feature entirely — the fallback branch in `_recover_with_playbook` short-circuits to re-raise immediately. This is the kill switch.

### Section 5 — Offline bootstrap from existing logs

`scripts/bootstrap_incidents.py` is a one-shot script run by the operator before the first overnight session. It converts the 130+ existing log files into seed incidents.

**Algorithm:**

1. Iterate `logs/*.log` in chronological order.
2. Parse each log line-by-line, maintaining a sliding window of state-transition events. Events of interest:
   - `chat: detect_state -> STATE` (new state observation)
   - `playbook: clicked '{label}'` (click action)
   - `playbook: reset stuck on dead-end dialog (enabled=[...]); clicking '{label}'` (explicit recovery)
   - `OptionNotFoundError` / `UNKNOWN` markers (stuck markers)
3. For each stuck marker, look forward in the same log within a 30-second window for the next `detect_state -> KNOWN_STATE` line where `KNOWN_STATE` is in `{MAIN_MENU, BOOK_FOR_OTHERS_MENU, READY_FOR_CUSTOMER}`. If such a transition exists and was preceded by a click action, emit an incident:

```json
{
  "key": "UNKNOWN|{sorted_buttons}",
  "state": "UNKNOWN",
  "buttons_sorted": [...],
  "last_bubble_excerpt": "{from the nearest log bubble print, PII-scrubbed}",
  "chosen_action": {"action": "click", "button_label": "{the click label}", "reason": "bootstrapped from logs/{file}:{line}"},
  "outcome": "recovered",
  "recovered_to_state": "{KNOWN_STATE}",
  "source": "bootstrap",
  "timestamp": "{log-file timestamp}",
  "occurrences": 1
}
```

4. **PII scrub**: before writing any incident, regex-replace `\b\d{10}\b` (phone) with `****REDACTED****` and `\b\d{4,6}\b` in OTP contexts with `****`. Last-bubble excerpts are truncated to 500 chars.
5. **Deduplication**: group by `key`. If the same `(state, buttons_sorted)` appears multiple times, pick the most frequent `chosen_action` and set `occurrences` to the total count. Break ties by recency.
6. Write to `data/incidents.jsonl`, one object per line.
7. Print a summary to stdout: `bootstrapped N incidents from M log files; K unique keys; top 10 most frequent: ...`.

**CLI:**

```
python scripts/bootstrap_incidents.py [--logs-dir logs/] [--output data/incidents.jsonl] [--dry-run]
```

`--dry-run` prints the summary without writing the file. The operator reviews the summary, then re-runs without `--dry-run`.

**Idempotent behaviour**: if `data/incidents.jsonl` already exists when the script runs without `--dry-run`, it merges new bootstrap findings with existing records (preferring existing `source="runtime"` records on key collisions). The operator can safely re-run after acquiring more log data.

### Section 6 — Error handling and safety

Every external failure path returns `None` from `consult`, which the caller treats as "advisor declined" and falls back to existing crash-and-restart semantics. Concrete fallthrough cases:

| Failure mode | `consult` returns | Caller behaviour |
|---|---|---|
| `ADVISOR_ENABLED = False` | `None` (never called) | Re-raise original exception |
| `ANTHROPIC_API_KEY` unset | `None` | Re-raise |
| Budget exhausted | `None` | Re-raise |
| State is auth/OTP | `None` | Re-raise |
| API timeout (>10s) | `None` | Re-raise |
| API HTTP error (rate-limit, 500, network) | `None` | Re-raise |
| Response has no tool_use block | `None` | Re-raise |
| Response JSON malformed | `None` | Re-raise |
| `action` not in {click, reload, skip_row} | `None` | Re-raise |
| `button_label` not in `enabled_buttons` | `None` | Re-raise + log "hallucinated label" warning |
| `click` with `button_label == None` | `None` | Re-raise |

**Every consult call produces exactly one structured log line**, regardless of outcome:

```
ai_advisor: state=UNKNOWN buttons=['Make Payment','Previous Menu'] path=fast decision=click/'Previous Menu' reason="dead-end payment dialog" budget=3/50 skips=0/15 recovered_to=BOOK_FOR_OTHERS_MENU
```

or:

```
ai_advisor: state=UNKNOWN buttons=['Foo','Bar'] path=api decision=None reason="api_timeout" budget=12/50 skips=2/15
```

This is the operator's single source of truth for advisor behaviour. Parsing these lines is the natural input to a future "advisor audit" subcommand.

**Safety invariants** (enforced in code, not documentation):

- `validate_decision(decision, snapshot)` is the single validation function. It is called unconditionally on every code path that returns a `Decision` — fast-path, API-path, and any test fake.
- The click executor in `cli._recover_with_playbook`'s advisor branch re-asserts `decision.button_label in snapshot.enabled_buttons` immediately before calling `_click_by_action`. Double-check, because the cost of a wrong click is real customer SMS.
- The skip_row executor raises `AdvisorSkipRow(reason)` which `cli.book_row` must explicitly catch. There is no path for `skip_row` to slip through as a silent success.

### Section 7 — Testing strategy

**Unit tests (all fast, all offline):**

`tests/test_ai_advisor.py` — uses a `FakeAnthropicClient` that returns canned tool-use responses. Never hits the real API.

- `test_consult_click_from_enabled_buttons_passes_validation`
- `test_consult_hallucinated_label_returns_none`
- `test_consult_reload_action_passes`
- `test_consult_skip_row_passes_and_increments_budget`
- `test_consult_refuses_auth_state`
- `test_consult_refuses_otp_state`
- `test_consult_fast_path_uses_stored_incident_without_api_call`
- `test_consult_slow_path_passes_top_k_similar_incidents_as_few_shots`
- `test_consult_api_timeout_returns_none`
- `test_consult_api_exception_returns_none`
- `test_consult_malformed_tool_response_returns_none`
- `test_consult_missing_api_key_returns_none`
- `test_consult_budget_exhausted_returns_none`

`tests/test_ai_advisor.py::TestAdvisorBudget`:
- `test_budget_max_calls_per_session_enforced`
- `test_budget_consecutive_skip_cap_enforced`
- `test_budget_total_skip_cap_enforced`
- `test_budget_non_skip_decision_resets_consecutive_counter`

`tests/test_ai_advisor.py::TestIncidentStore`:
- `test_store_load_from_empty_file`
- `test_store_record_success_appends_new_incident`
- `test_store_record_success_increments_occurrences_on_dedupe`
- `test_store_lookup_by_key_exact_match`
- `test_store_similar_by_button_overlap_returns_top_k`
- `test_store_atomic_flush_to_jsonl`

`tests/test_bootstrap_incidents.py`:
- `test_bootstrap_extracts_stuck_to_recovered_pair_from_log`
- `test_bootstrap_scrubs_phone_numbers_in_bubble_text`
- `test_bootstrap_ignores_stuck_without_recovery_within_window`
- `test_bootstrap_dedupes_identical_keys_across_files`
- `test_bootstrap_dry_run_does_not_write_file`
- `test_bootstrap_merge_preserves_runtime_incidents_on_key_collision`

**Not tested in the suite:** the live Anthropic API call. There is one opt-in smoke test behind an env flag (`BOOKING_BOT_ADVISOR_SMOKE=1`) that calls the real API once with a canned snapshot and asserts the response validates. Not run in CI.

**TDD ordering is enforced by the implementation plan** (`writing-plans` output): each component ships as a failing test first, then minimal implementation, then commit.

## Data model

```
data/
├── incidents.jsonl          (new — append-only episodic corpus)
```

No database. No embeddings. No migrations. `incidents.jsonl` is plain text, one JSON object per line, hand-editable with any text editor. On startup `IncidentStore.__init__` reads the file line-by-line into an in-memory dict keyed by `(state, sorted_buttons)`. On every `record_success` the store writes the new/updated record to disk with a write-to-temp + atomic rename.

## Config changes

```python
# ADDED (booking_bot/config.py)
ADVISOR_ENABLED               = True
ADVISOR_MODEL                 = "claude-sonnet-4-6"
ADVISOR_API_TIMEOUT_S         = 10.0
ADVISOR_MAX_CALLS_PER_SESSION = 50
ADVISOR_MAX_CONSECUTIVE_SKIPS = 3
ADVISOR_MAX_TOTAL_SKIPS       = 15
ADVISOR_INCIDENTS_PATH        = Path("data/incidents.jsonl")
```

## Dependencies

```
# pyproject.toml / requirements.txt — ADDED
anthropic >= 0.40
```

No other new dependencies. The tests use a hand-rolled `FakeAnthropicClient`; no mocking library beyond what's already in the project.

## Risks and rollback

- **Advisor hallucinates a button and the double-check fails to catch it.** Mitigated by `validate_decision` being the single choke point and the re-assertion in the click executor. Unit tests specifically cover hallucinated labels. If this happens in production, the fallback is the same as any other `None` — re-raise and crash-and-restart.
- **API rate limits during a storm of UNKNOWN states.** Mitigated by the 50-call-per-session cap. If we hit it, the advisor goes silent for the rest of the session and the bot falls back to existing crash semantics. The operator can bump the cap in config or wait for the next auto-restart to reset.
- **Bootstrap misidentifies a stuck→recovered pair and seeds a bad incident.** Mitigated by:
  (a) The exact-match key means the bad incident only fires on the exact same button shape.
  (b) Bootstrap output is reviewable — `incidents.jsonl` is plain text.
  (c) Operator can delete any line to evict a bad incident.
  (d) On the next successful API-path recovery for the same key, the runtime path overwrites the bad entry.
- **`data/incidents.jsonl` corruption on crash during flush.** Mitigated by atomic write-to-temp + rename. Store reads are line-tolerant: a malformed line is logged and skipped, the rest of the file loads normally.
- **Anthropic SDK breaking change.** Pinned to `>= 0.40`. If a future SDK version breaks `tool_use` handling, tests catch it before merge.

**Rollback plan:**
1. `ADVISOR_ENABLED = False` — soft disable. The fallback branch short-circuits immediately; no API calls; no risk. This is a one-line revert.
2. Full rollback: delete `booking_bot/ai_advisor.py`, revert the two modifications in `cli.py` and `chat.py`, delete `data/incidents.jsonl`, remove the `anthropic` dependency. Bot returns to the exact shape it had before this feature.

## Implementation order

1. **Data structures** — `Snapshot`, `Decision`, `IncidentStore`, `AdvisorBudget`. Pure, testable, no dependencies.
2. **`chat.build_snapshot`** — helper that packages existing detect_state signals. No behaviour change.
3. **`ai_advisor.validate_decision`** — the safety choke point. Tested in isolation.
4. **`ai_advisor.consult` fast path** — exact-match lookup, no API. Unit-tested against a canned IncidentStore.
5. **`ai_advisor.consult` slow path + FakeAnthropicClient** — templated prompt, tool-use response, validation. Fully tested without real API.
6. **`cli._recover_with_playbook` fallback branch** — wires advisor into recovery, dispatches on Decision. Tested against a fake frame.
7. **`scripts/bootstrap_incidents.py`** — offline log parser. Tested against a canned log fixture.
8. **Run bootstrap against real `logs/`** — operator reviews the output, commits the seeded `incidents.jsonl`.
9. **Live smoke test** — opt-in smoke test against real Anthropic API with a single canned snapshot.
10. **Merge + first overnight run.**

After each step: `pytest -q`, commit, push. Each step is a clean commit so partial rollback is easy.

## Open questions

None — design is fully specified.
