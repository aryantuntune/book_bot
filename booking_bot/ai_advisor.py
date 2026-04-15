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

from booking_bot import config


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
