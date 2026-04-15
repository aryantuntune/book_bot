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
