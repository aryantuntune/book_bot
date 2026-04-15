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


def full_scroller_text(frame: Frame) -> str:
    """Return the complete #scroller innerText. Used by callers that need to
    scan the entire chat history (e.g. playbook success detection — the
    confirmation code may land in the DOM slightly after wait_until_settled
    returns, so we fall back to the full scroller when a diff-based search
    comes up empty)."""
    try:
        return frame.evaluate(
            """() => {
              const s = document.querySelector('#scroller');
              return s ? (s.innerText || '') : '';
            }"""
        ) or ""
    except Exception as e:
        log.warning(f"full_scroller_text: {e}")
        return ""


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

def send_text(frame: Frame, text: str, require_inline: bool = False) -> None:
    """Type `text` and submit it to the chatbot.

    Priority order:
      1. Inline form input — when the chatbot asks for structured input
         (mobile number, OTP, etc.) it renders a dedicated
         `<input type='text'>` inside the chat bubble, paired with a
         `button.submit`. We must type into THAT input, not the generic
         bottom textarea; otherwise the chatbot replies with its "I am
         still learning" fallback.
      2. Fallback: the bottom `textarea.replybox` + `button.reply-submit`
         for free-text interactions with no inline form visible.

    require_inline: when True, refuse to use the textarea fallback. Used
    for structured values (operator phone, customer phone, OTP) where
    HPCL is in a form-input state — typing into the textarea instead
    sends the digits into chat, HPCL ignores them, and the bot moves on
    thinking the row was processed (the actual root cause of the
    "row-skipping by typing numbers into the chat bar" bug). Raises
    IframeLostError when the inline input isn't there so the caller can
    trigger recovery instead of silently corrupting the row.

    CRITICAL: HPCL leaves OLD inline inputs from previous chat bubbles in
    the DOM, sometimes still visible and still enabled. We must always
    pick the LAST matching element (the newest bubble), never the first
    one in DOM order — otherwise we type into the OLD bubble's input,
    which HPCL no longer monitors, and nothing happens.

    Everything runs in a single in-page eval so the input lookup, value
    set, event dispatch, and submit click are atomic — no chance of the
    DOM changing between steps."""
    try:
        result = frame.evaluate(
            """
            (value) => {
              // 1. Find all inline (non-replybox) text-ish inputs.
              const all = Array.from(document.querySelectorAll(
                "input[type='text'], input[type='number'], "
                + "input[type='tel'], input[type='password']"
              ));
              // Filter: visible, enabled, not the bottom replybox.
              const candidates = all.filter(el => {
                if (el.offsetParent === null) return false;
                if (el.disabled || el.readOnly) return false;
                const cls = el.getAttribute('class') || '';
                if (cls.includes('replybox')) return false;
                return true;
              });

              // Prefer empty inputs. HPCL leaves the OLD prompt's input in
              // the DOM after a click — visible AND enabled — and it's
              // already filled with the previous value. Picking the LAST
              // candidate blindly would type the new value into that stale
              // dead field. Empty inputs are always the fresh prompt.
              const empty = candidates.filter(el =>
                !el.value || el.value.trim().length === 0
              );
              const pool = empty.length > 0 ? empty : candidates;

              if (pool.length > 0) {
                // Pick the LAST — the newest chat bubble's input.
                const el = pool[pool.length - 1];
                el.focus();
                el.value = '';
                el.value = value;
                // Fire input+change so jQuery/framework listeners react.
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));

                // Find the newest enabled button.submit and click it.
                const submits = Array.from(
                  document.querySelectorAll('button.submit')
                ).filter(b => b.offsetParent !== null && !b.disabled);
                if (submits.length > 0) {
                  submits[submits.length - 1].click();
                  return {
                    ok: true, via: 'inline-submit',
                    id: el.id || null,
                    name: el.getAttribute('name'),
                  };
                }
                // No inline submit — dispatch Enter to the input.
                el.dispatchEvent(new KeyboardEvent('keydown', {
                  key: 'Enter', code: 'Enter', keyCode: 13,
                  which: 13, bubbles: true,
                }));
                return {
                  ok: true, via: 'inline-enter',
                  id: el.id || null,
                  name: el.getAttribute('name'),
                };
              }

              // 2. Fallback: bottom textarea.replybox.
              const ta = document.querySelector('textarea.replybox');
              if (ta && ta.offsetParent !== null && !ta.disabled) {
                ta.focus();
                ta.value = '';
                ta.value = value;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                const btn = document.querySelector('button.reply-submit');
                if (btn) {
                  btn.click();
                  return {ok: true, via: 'replybox'};
                }
              }
              return {ok: false};
            }
            """,
            text,
        )
    except Exception as e:
        raise IframeLostError(f"send_text eval: {e}") from e

    if not result.get("ok"):
        raise IframeLostError(
            "send_text: no visible enabled input found (neither inline "
            "nor replybox)"
        )

    via = result.get("via", "?")
    if require_inline and via == "replybox":
        raise IframeLostError(
            "send_text: structured value required inline form input but "
            "only the bottom replybox was available — chat is not in a "
            "form-input state. Refusing to send to chat as free text."
        )
    who = result.get("id") or result.get("name") or ""
    # Never log OTP/phone contents here — callers (playbook.replay_step
    # and auth.*) do their own masked logging.
    log.debug(f"send_text({via} {who!r})")


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


# ---- Task 15: _classify_state + detect_state ----

def _classify_state(button_labels: list[str], scroller_text: str) -> str:
    """Pure classifier — priority 1 is button labels, priority 2 is scroller
    text. Returns one of config.STATE_PATTERNS' keys or 'UNKNOWN'."""
    blob_buttons = " | ".join(button_labels)
    for state_name, patterns in config.STATE_PATTERNS.items():
        for p in patterns:
            if p.search(blob_buttons):
                return state_name
    for state_name, patterns in config.STATE_PATTERNS.items():
        for p in patterns:
            if p.search(scroller_text or ""):
                return state_name
    return "UNKNOWN"


# Input-name classification for the empty-input heuristic. These lists are
# the single source of truth for "which DOM input means which state" and
# are consulted by _resolve_state. The operator sets are intentionally
# broad and case-insensitive because getting them wrong has a real safety
# cost: a missed operator-auth classification sends actual customer phone
# numbers into HPCL's operator-phone field, triggering real OTP SMS to
# those customers (prod incident 2026-04-15).
_CUSTOMER_INPUT_NAMES = frozenset({"newmobile"})
_OPERATOR_AUTH_INPUT_NAMES = frozenset({"mobile"})
_OPERATOR_OTP_INPUT_NAMES = frozenset({"otp"})


def _resolve_state(
    enabled_buttons: list[str],
    last_bubble_text: str,
    recent_text: str,
    empty_input_names: list[str],
) -> str:
    """Pure priority pipeline used by detect_state. Kept separate from the
    Frame-reading wrapper so we can unit-test the resolution rules with
    canned DOM data.

    Priority order (see detect_state docstring for the WHY):
      1. Enabled menu buttons that match a state pattern.
      2. Auth/OTP text in the LAST BUBBLE ONLY — never in scrollback,
         because stale auth bubbles from a prior cycle would otherwise
         re-trigger NEEDS_OPERATOR_AUTH after a healthy login.
      3. Empty inline input, BUT classified by its `name` attribute:
           - 'mobile' (or any _OPERATOR_AUTH_INPUT_NAMES) -> NEEDS_OPERATOR_AUTH
           - 'otp'    (or any _OPERATOR_OTP_INPUT_NAMES)  -> NEEDS_OPERATOR_OTP
           - 'newmobile' (or any _CUSTOMER_INPUT_NAMES)   -> READY_FOR_CUSTOMER
           - Unknown name -> fall through to (4) instead of assuming
             READY_FOR_CUSTOMER. This is the hard invariant: we NEVER
             report READY_FOR_CUSTOMER unless the empty input is a
             KNOWN customer-phone field. Prior behavior assumed any
             empty input meant customer-phone, and after a reload onto
             HPCL's operator-auth screen the bot typed real customer
             numbers into the operator field, triggering SMS to those
             customers (fixed 2026-04-15).
         When BOTH a customer input and an operator input are visible
         (rare; transient double-render), the operator-auth classification
         wins — typing customer data into an operator field is the
         failure we're optimizing against.
      4. Last-resort recent-text classifier."""
    if enabled_buttons:
        button_state = _classify_state(enabled_buttons, "")
        if button_state != "UNKNOWN":
            return button_state

    for state_name in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"):
        for p in config.STATE_PATTERNS[state_name]:
            if p.search(last_bubble_text or ""):
                return state_name

    input_names_lower = {(n or "").strip().lower() for n in empty_input_names}
    if input_names_lower & _OPERATOR_AUTH_INPUT_NAMES:
        return "NEEDS_OPERATOR_AUTH"
    if input_names_lower & _OPERATOR_OTP_INPUT_NAMES:
        return "NEEDS_OPERATOR_OTP"
    if input_names_lower & _CUSTOMER_INPUT_NAMES:
        return "READY_FOR_CUSTOMER"

    return _classify_state([], recent_text)


def detect_state(frame: Frame) -> str:
    """Read interactive DOM state and classify with a strict priority order:

      1. Enabled menu buttons — strongest signal, what the user can click NOW.
         HPCL leaves clicked buttons visible-but-disabled in the transcript;
         only ENABLED buttons drive classification. Filtering out disabled
         buttons is what stops a stale "Book for Others" bubble from
         re-classifying us as BOOK_FOR_OTHERS_MENU after we've moved on.

      2. Explicit auth/OTP patterns from the SINGLE LAST bubble — these
         strings are unique to HPCL's auth gate and never appear in normal
         booking flow, so checking them here is safe before the input-
         presence heuristic (which would otherwise misread an auth input
         as the customer-phone prompt).

         CRITICAL: we scan ONLY the LAST non-empty bubble of #scroller,
         not the last 5 and not `innerText.slice(-1000)`. Earlier
         iterations scanned the last 5 bubbles, but after a successful
         OTP the auth-prompt bubble was still inside that window —
         detect_state then returned NEEDS_OPERATOR_AUTH on the customer-
         phone prompt and the bot looped trying to navigate back to a
         menu that was already past. Auth/OTP prompts are always single-
         bubble events in HPCL, so the last-bubble-only check is both
         tighter and complete (fixed 2026-04-15).

      3. An EMPTY inline <input> that isn't the replybox — when buttons
         and auth-text don't match, an empty form field means HPCL is
         waiting for the next customer phone. This is what unblocks the
         post-click case where the customer-phone prompt is rendered but
         the scroller tail still contains "Book for Others" text from the
         just-dismissed menu bubble. The input MUST be empty so we don't
         confuse the OLD filled customer-phone input from the
         just-completed row with a fresh prompt.

      4. Last-resort bubble-text classifier — used only when nothing
         interactive is in view (chat mid-load).

    The bug this fixes (prod log 2026-04-14 16:25): after clicking
    "Book for Others", all menu buttons go disabled and HPCL renders an
    empty `<input name='newmobile'>` for the customer phone. The OLD
    code's `_classify_state` saw zero enabled buttons, fell through to
    the scroller text, matched `book\\s+for\\s+others` from the dismissed
    bubble (because BOOK_FOR_OTHERS_MENU is checked before
    READY_FOR_CUSTOMER in dict order), and reported BOOK_FOR_OTHERS_MENU
    forever. The bot kept clicking "Book for Others" → landing back on
    the same prompt → re-misclassifying → looping.
    """
    try:
        data = frame.evaluate(
            f"""
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
              // Collect the `name` (or `id` as fallback) of every empty
              // input. _resolve_state uses these to tell customer-phone
              // inputs (newmobile) apart from operator-auth inputs
              // (mobile, otp) — a mix-up here sends real SMS to customers.
              const emptyInputNames = inputs.map(el =>
                (el.getAttribute('name') || el.id || '').trim()
              ).filter(n => n.length > 0);
              const s = document.querySelector('{config.SEL_SCROLLER}');
              // Build TWO views of the recent scroller text:
              //  - lastBubbleText: ONLY the very last non-empty bubble. Used
              //    for the strict auth/OTP detection, which must NOT see
              //    stale auth bubbles still sitting in scrollback after a
              //    successful login (the false-positive that caused the
              //    post-OTP "stuck on main menu" loop, fixed 2026-04-15).
              //  - recentText: last 5 non-empty bubbles joined. Used as the
              //    weak fallback classifier when nothing more specific
              //    matches.
              let recentText = '';
              let lastBubbleText = '';
              if (s) {{
                const kids = Array.from(s.children);
                const recent = [];
                for (let i = kids.length - 1; i >= 0 && recent.length < 5; i--) {{
                  const t = (kids[i].innerText || '').trim();
                  if (t) {{
                    if (lastBubbleText === '') lastBubbleText = t;
                    recent.unshift(t);
                  }}
                }}
                recentText = recent.join('\\n');
                // Fallback for flat scroller layouts (no per-message children):
                // last 400 chars of innerText. Half the old 1000-char slice, so
                // it rarely spans more than one recent bubble.
                if (!recentText) {{
                  recentText = (s.innerText || '').slice(-400);
                  if (!lastBubbleText) lastBubbleText = recentText;
                }}
              }}
              return {{
                buttons: btns,
                text: recentText,
                lastBubbleText: lastBubbleText,
                emptyInputNames: emptyInputNames,
              }};
            }}
            """
        )
    except Exception as e:
        raise IframeLostError(f"detect_state: {e}") from e

    return _resolve_state(
        enabled_buttons=data["buttons"],
        last_bubble_text=data.get("lastBubbleText") or "",
        recent_text=data["text"],
        empty_input_names=list(data.get("emptyInputNames") or []),
    )


# ---- Task 16: dump_visible_state ----

def dump_visible_state(frame: Frame) -> str:
    """Return a compact diagnostic string for FatalError messages and DEBUG
    logs. Never raises — returns a string even on failure."""
    try:
        data = frame.evaluate(
            f"""
            () => {{
              const btns = Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
                .filter(b => b.offsetParent !== null)
                .map(b => (b.innerText || '').trim()).slice(0, 20);
              const s = document.querySelector('{config.SEL_SCROLLER}');
              const text = s ? (s.innerText || '').slice(-500) : '<no-scroller>';
              const loader = !!document.querySelector('{config.SEL_LOADER}');
              return {{
                buttons: btns, text: text, loader: loader,
                url: document.location ? document.location.href : '<no-url>'
              }};
            }}
            """
        )
        return (
            f"url={data['url']!r} loader_present={data['loader']} "
            f"visible_buttons={data['buttons']!r} "
            f"last_scroller_500={data['text']!r}"
        )
    except Exception as e:
        return f"<dump_visible_state failed: {e}>"


# ---- Task 17: book_one state machine ----

def book_one(frame: Frame, phone: str) -> BookingResult:
    """Drive one booking from READY_FOR_CUSTOMER to terminal state.

    Flow:
      1. Type the customer phone, submit.
      2. wait_until_settled → new message(s).
      3. If the new text contains a SUCCESS_RE match, return Success.
      4. Otherwise try clicking an affirmative option (Yes / Continue / ...).
         - If an affirmative matches, loop back to step 2 with the fresh
           settled snapshot. Accumulate the full bot response chain in
           `accumulated` for the Issue diagnostic field.
         - If no affirmative matches, the bot is in an unexpected state —
           return Issue('unexpected_state', accumulated).
      5. Bail out after MAX_STEPS_PER_BOOKING iterations with
         Issue('too_many_steps', accumulated).

    All recoverable exceptions (GatewayError, ChatStuckError, IframeLostError,
    OptionNotFoundError from wait_until_settled or earlier) propagate to the
    cli.py retry loop — book_one does not catch them. The ONE exception is
    OptionNotFoundError from our own click_option(AFFIRMATIVE_LABELS) call:
    that just means 'the chat isn't in an affirmative state', which is an
    unexpected_state Issue, not a recoverable error.
    """
    send_text(frame, phone)
    new = wait_until_settled(frame)
    accumulated = new.text

    for step in range(config.MAX_STEPS_PER_BOOKING):
        m = config.SUCCESS_RE.search(new.text)
        if m:
            log.info(f"book_one success: code={m.group(1)} (step {step})")
            return Success(code=m.group(1), raw=accumulated)

        try:
            label = click_option(frame, config.AFFIRMATIVE_LABELS)
        except OptionNotFoundError:
            log.info(f"book_one unexpected_state at step {step}")
            return Issue(reason="unexpected_state", raw=accumulated)

        log.debug(f"book_one clicked affirmative: {label!r}")
        new = wait_until_settled(frame)
        accumulated += "\n---\n" + new.text

    log.info("book_one too_many_steps")
    return Issue(reason="too_many_steps", raw=accumulated)
