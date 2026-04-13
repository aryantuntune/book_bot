"""Chat primitives for the inner Twixor frame. Split across tasks 13-17:
  Task 13: send_text, click_option, _scroller_snapshot
  Task 14: wait_until_settled
  Task 15: detect_state (+ testable pure helper)
  Task 16: dump_visible_state
  Task 17: book_one state machine
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

from playwright.sync_api import Frame, TimeoutError as PWTimeoutError

from booking_bot import config
from booking_bot.exceptions import (
    ChatStuckError,
    GatewayError,
    IframeLostError,
    OptionNotFoundError,
)

log = logging.getLogger("chat")


# ---- Result types used by book_one (Task 17) ----

@dataclass
class Success:
    code: str
    raw: str


@dataclass
class Issue:
    reason: str
    raw: str


BookingResult = Success | Issue


# ---- Snapshot dataclass used by wait_until_settled (Task 14) ----

@dataclass
class Snapshot:
    text: str           # the NEW text added (diff vs before), or full text
    child_count: int
    hash: str


# ---- Private helpers ----

def _scroller_snapshot(frame: Frame) -> Snapshot:
    """Capture a stable fingerprint of #scroller. Raises IframeLostError if the
    frame is detached (we swallow PW errors and translate them)."""
    try:
        data = frame.evaluate(
            """
            () => {
              const s = document.querySelector('#scroller');
              if (!s) return {text: '', children: 0};
              return {text: s.innerText || '', children: s.children.length};
            }
            """
        )
    except Exception as e:
        raise IframeLostError(f"scroller_snapshot: {e}") from e
    text = data["text"] or ""
    children = int(data["children"] or 0)
    h = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
    return Snapshot(text=text, child_count=children, hash=h)


def _loader_visible(frame: Frame) -> bool:
    try:
        return bool(frame.evaluate(
            f"""
            () => {{
              const el = document.querySelector('{config.SEL_LOADER}');
              if (!el) return false;
              const cs = getComputedStyle(el);
              if (cs.display === 'none' || cs.visibility === 'hidden') return false;
              return el.offsetParent !== null;
            }}
            """
        ))
    except Exception:
        return False


# ---- Public primitives ----

def send_text(frame: Frame, text: str) -> None:
    """Focus textarea.replybox, clear existing content, type the text, click
    submit. The clear step is essential — leftover content from a prior
    interaction would otherwise be concatenated."""
    try:
        frame.focus(config.SEL_TEXTAREA)
        frame.evaluate(
            f"() => {{ const t = document.querySelector('{config.SEL_TEXTAREA}'); "
            f"if (t) {{ t.value = ''; t.focus(); }} }}"
        )
        frame.fill(config.SEL_TEXTAREA, text)
        frame.click(config.SEL_SUBMIT)
        log.debug(f"sent text: {text!r}")
    except PWTimeoutError as e:
        raise IframeLostError(f"send_text timeout: {e}") from e


def click_option(frame: Frame, label_patterns: Iterable[re.Pattern[str]]) -> str:
    """Click the first *visible* button.dynamic-message-button whose text
    matches one of label_patterns (in priority order). Returns the matched
    button text. Raises OptionNotFoundError if none match."""
    try:
        buttons = frame.evaluate(
            f"""
            () => Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
                .filter(b => b.offsetParent !== null)
                .map(b => ({{ text: (b.innerText || '').trim(), id: b.id }}))
            """
        )
    except Exception as e:
        raise IframeLostError(f"click_option read buttons: {e}") from e

    for pat in label_patterns:
        for b in buttons:
            if pat.search(b["text"] or ""):
                sel = f"{config.SEL_OPTION}#{b['id']}" if b["id"] else \
                      f"{config.SEL_OPTION}:has-text('{b['text']}')"
                try:
                    frame.click(sel, timeout=5_000)
                    log.debug(f"clicked option: {b['text']!r} (pattern {pat.pattern})")
                    return b["text"]
                except PWTimeoutError as e:
                    raise IframeLostError(f"click_option click: {e}") from e
    raise OptionNotFoundError(
        f"no visible option matched {[p.pattern for p in label_patterns]}; "
        f"visible options were: {[b['text'] for b in buttons]}"
    )


# ---- Task 14: wait_until_settled ----

def wait_until_settled(frame: Frame, timeout: float | None = None) -> Snapshot:
    """Wait until the chatbot has fully processed the last interaction, then
    return a Snapshot whose .text contains ONLY the content added since entry.

    Algorithm (see spec §6.1):
      1. Reset the gateway-error flag (any flag raised now is from THIS call).
      2. Capture a 'before' snapshot of #scroller.
      3. Poll every 500ms:
         - if gateway flag set → raise GatewayError
         - if frame detached → raise IframeLostError
         - compute 'now' snapshot
      4. First-activity gate: require either (a) the loader has been seen
         visible at least once, or (b) the scroller hash has changed at least
         once. Without this, a caller that invokes us right after send_text()
         could return with an empty diff if the bot hasn't started yet.
      5. Settled = loader currently hidden AND scroller hash unchanged for
         SETTLE_QUIET_MS (1500ms).
      6. Timeout → ChatStuckError.
    """
    # Late import to avoid a cycle at module load (chat ← browser ← chat).
    from booking_bot import browser

    timeout_s = timeout if timeout is not None else config.STUCK_THRESHOLD_S
    deadline = time.monotonic() + timeout_s
    poll_ms = 500
    quiet_target_ms = config.SETTLE_QUIET_MS

    browser.reset_gateway_flag()
    before = _scroller_snapshot(frame)

    activity_seen = False
    last_change_time: float | None = None
    last_hash = before.hash

    while time.monotonic() < deadline:
        if browser.gateway_flag():
            raise GatewayError("gateway flag set during wait_until_settled")

        try:
            now = _scroller_snapshot(frame)
        except IframeLostError:
            raise

        loader = _loader_visible(frame)

        if loader:
            activity_seen = True

        if now.hash != last_hash:
            activity_seen = True
            last_change_time = time.monotonic()
            last_hash = now.hash

        if activity_seen and not loader and last_change_time is not None:
            quiet_ms = (time.monotonic() - last_change_time) * 1000
            if quiet_ms >= quiet_target_ms:
                # Settled. Return the diff.
                new_text = now.text[len(before.text):] if \
                    now.text.startswith(before.text) else now.text
                return Snapshot(
                    text=new_text,
                    child_count=now.child_count,
                    hash=now.hash,
                )

        time.sleep(poll_ms / 1000)

    raise ChatStuckError(
        f"wait_until_settled timeout after {timeout_s}s "
        f"(activity_seen={activity_seen}, last_hash_changed_at={last_change_time})"
    )
