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

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from booking_bot import config

log = logging.getLogger("ai_advisor")


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
        candidates.sort(
            key=lambda t: (
                -t[0],
                -(t[1].get("occurrences") or 0),
                t[1].get("timestamp") or "",
            ),
        )
        return [rec for (_score, rec) in candidates[:top_k]]

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
        fd, tmp_name = tempfile.mkstemp(
            prefix=".incidents.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for rec in self._by_key.values():
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp_name, self.path)
        except Exception:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            raise


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
    saw."""
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
