"""Learned playbooks: replay a recording of a successful booking instead of
hardcoding menu regexes. Decouples bot behaviour from HPCL's current menu
wording — when HPCL changes the flow, the operator re-records and the bot
adapts with no code edits.

A playbook is parsed from the JSONL file produced by
`python -m booking_bot.record`. Its structure:

    auth_prefix  : actions run ONCE at bot startup (operator phone, OTP,
                   menu nav up to but not including customer-phone entry).
    booking_body : actions run PER ROW (customer phone, confirmations,
                   delivery-code read).

Splitting is automatic. Every typed value is classified:
    - equals config.OPERATOR_PHONE   → 'operator_phone'
    - 4-8 pure digits (not 10-digit) → 'otp'
    - 10 pure digits, not operator   → 'customer_phone'
    - anything else                  → 'literal'
The first 'customer_phone' TYPE action marks the start of booking_body.

Replay substitutes the runtime values into each slot:
    - operator_phone → config.OPERATOR_PHONE (at replay_auth)
    - otp            → get_otp() lazily (prompts fresh every time)
    - customer_phone → the row's phone
    - literal        → the recorded value verbatim

Click actions match by id first, then exact case-insensitive text, then
substring text. If nothing matches we raise OptionNotFoundError so the
caller knows the recording is out of date.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from playwright.sync_api import Frame, TimeoutError as PWTimeoutError

from booking_bot import chat, config
from booking_bot.exceptions import (
    ChatStuckError,
    GatewayError,
    IframeLostError,
    OptionNotFoundError,
)

log = logging.getLogger("playbook")


ActionKind = Literal["click", "type"]
ValueSlot = Literal["operator_phone", "otp", "customer_phone", "literal"]


@dataclass
class Action:
    kind: ActionKind
    # Click fields
    button_text: str | None = None
    button_id: str | None = None
    button_cls: str | None = None
    # Type fields
    input_id: str | None = None
    input_name: str | None = None
    input_placeholder: str | None = None
    value_slot: ValueSlot = "literal"
    literal_value: str | None = None

    def describe(self) -> str:
        if self.kind == "click":
            return f"CLICK {self.button_text!r} (id={self.button_id})"
        who = self.input_name or self.input_id or self.input_placeholder or "?"
        return f"TYPE [{self.value_slot}] -> input({who})"


@dataclass
class Playbook:
    auth_prefix: list[Action] = field(default_factory=list)
    booking_body: list[Action] = field(default_factory=list)
    source: str = ""

    def describe(self) -> str:
        out = [f"Playbook from {self.source}:"]
        out.append(f"  auth_prefix ({len(self.auth_prefix)} steps):")
        for a in self.auth_prefix:
            out.append(f"    - {a.describe()}")
        out.append(f"  booking_body ({len(self.booking_body)} steps):")
        for a in self.booking_body:
            out.append(f"    - {a.describe()}")
        return "\n".join(out)


# ---- Parsing (pure, TDD-able) ----

def classify_value(value: str, operator_phone: str) -> ValueSlot:
    """Classify a typed value into its replay slot. Pure function — tested
    in tests/test_playbook_classify.py."""
    v = (value or "").strip()
    if not v:
        return "literal"
    if v == operator_phone:
        return "operator_phone"
    if re.fullmatch(r"\d{10}", v):
        return "customer_phone"
    if re.fullmatch(r"\d{4,8}", v):
        return "otp"
    return "literal"


def _parse_events(jsonl_path: Path) -> list[dict]:
    events = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            log.warning(f"skipping malformed recording line: {e}")
    return events


def _is_submit_button(clicked: dict) -> bool:
    """Does this click target look like a form-submit button? We skip replaying
    these when they come paired with a TYPE action, because chat.send_text
    types+submits atomically — the button is gone by the time CLICK would run."""
    text = (clicked.get("text") or "").strip().lower()
    cls = (clicked.get("cls") or "").lower()
    if "submit" in cls:
        return True
    if text in ("submit", "send"):
        return True
    return False


def events_to_actions(events: list[dict], operator_phone: str) -> list[Action]:
    """Convert raw recording events into an Action sequence. Pure function.

    Strategy:
      - For each click event: append TYPE actions for each filledInput
        (deduped by (input_key, value) — the same filled value may persist
        across successive clicks that don't clear the form, but a NEW value
        in the same input is a fresh TYPE). Then append the CLICK action,
        EXCEPT when it's a Submit-like button and we emitted a TYPE in this
        same event — chat.send_text already submitted the form, so replaying
        the Submit click would fail on the now-vanished button.
      - For each enter_key event: append a TYPE action for the input.
      - Skip chat_msg, info, header events — they're diagnostic only.
    """
    actions: list[Action] = []
    seen: set[tuple] = set()  # (input_key, slot, value) to avoid dup type actions

    def _maybe_emit_type(inp: dict) -> bool:
        value = inp.get("value") or ""
        slot = classify_value(value, operator_phone)
        key = (
            inp.get("id") or inp.get("name") or inp.get("placeholder"),
            slot,
            value,
        )
        if key in seen:
            return False
        seen.add(key)
        actions.append(Action(
            kind="type",
            input_id=inp.get("id"),
            input_name=inp.get("name"),
            input_placeholder=inp.get("placeholder"),
            value_slot=slot,
            literal_value=value if slot == "literal" else None,
        ))
        return True

    for ev in events:
        kind = ev.get("kind")
        if kind == "click":
            emitted_type = False
            for inp in (ev.get("filledInputs") or []):
                if _maybe_emit_type(inp):
                    emitted_type = True
            clicked = ev.get("clicked") or {}
            if emitted_type and _is_submit_button(clicked):
                continue
            actions.append(Action(
                kind="click",
                button_text=(clicked.get("text") or "").strip() or None,
                button_id=clicked.get("id"),
                button_cls=clicked.get("cls"),
            ))
        elif kind == "enter_key":
            _maybe_emit_type(ev.get("input") or {})
    return actions


def split_playbook(actions: list[Action]) -> tuple[list[Action], list[Action]]:
    """Split the action list into (auth_prefix, booking_body).

    Single-booking recording (one customer_phone TYPE):
        auth_prefix  = actions before the customer_phone
        booking_body = from the customer_phone to the end

    Multi-booking recording (two or more customer_phone TYPEs):
        auth_prefix  = actions before the FIRST customer_phone
        booking_body = actions from the FIRST customer_phone up to but NOT
                       including the SECOND — this captures one full
                       iteration of the loop (type phone → submit → confirm
                       → navigate back to "enter customer phone" state),
                       and drops any trailing cleanup like Main Menu clicks.

    Recording two bookings is strongly preferred: the range between the two
    customer phones exactly describes what the bot must do per row.

    Raises ValueError if no customer_phone TYPE action was recorded.
    """
    cp_indices = [
        i for i, a in enumerate(actions)
        if a.kind == "type" and a.value_slot == "customer_phone"
    ]
    if not cp_indices:
        raise ValueError(
            "no customer_phone TYPE action found in recording. Re-record with "
            "`python -m booking_bot.record` and make sure you actually book a "
            "cylinder end-to-end for a 10-digit customer number."
        )
    first = cp_indices[0]
    if len(cp_indices) >= 2:
        second = cp_indices[1]
        return actions[:first], actions[first:second]
    return actions[:first], actions[first:]


# Backwards-compat alias for tests that imported the old name.
split_at_first_customer_phone = split_playbook


def load(jsonl_path: str | Path, operator_phone: str | None = None) -> Playbook:
    """Parse a recording JSONL into an executable Playbook."""
    if operator_phone is None:
        operator_phone = config.OPERATOR_PHONE
    path = Path(jsonl_path)
    events = _parse_events(path)
    actions = events_to_actions(events, operator_phone)
    auth, body = split_playbook(actions)
    return Playbook(auth_prefix=auth, booking_body=body, source=str(path))


# ---- Replay (touches the live frame) ----

def _click_by_action(frame: Frame, action: Action) -> None:
    """Find and click the button described by `action`. Uses a single
    in-page JS eval that:
      1. Collects every button/btn-link/dynamic-message-button.
      2. Filters to visible AND not-disabled elements.
      3. Matches against (id preferred) → (exact text) → (substring text).
      4. Picks the LAST match — HPCL reuses content-ids across repeated
         chat messages, so the most recent DOM node is almost always the
         active one. Older instances get `disabled` set and are filtered
         out anyway, but picking .last is an extra safety net.
      5. Calls .click() in-page, which dispatches a click event that
         HPCL's jQuery handlers pick up just like a real mouse click.

    Raises OptionNotFoundError if no enabled element matches."""
    target_text = (action.button_text or "").strip()
    target_id = action.button_id

    try:
        result = frame.evaluate(
            """
            ({targetId, targetText}) => {
              const all = Array.from(document.querySelectorAll(
                'button, a.btn, .dynamic-message-button'
              ));
              const clickable = all.filter(
                b => b.offsetParent !== null && !b.disabled
              );
              const target = (targetText || '').toLowerCase();

              // IMPORTANT: capture el.innerText BEFORE el.click(), because
              // HPCL's click handler synchronously flips the button to
              // disabled with text=data-loading-text ('processing...'),
              // and we'd log the loading text instead of the real label.

              // 1. id match
              if (targetId) {
                const byId = clickable.filter(b => b.id === targetId);
                if (byId.length) {
                  const el = byId[byId.length - 1];
                  const text = (el.innerText || '').trim();
                  el.click();
                  return {ok: true, how: 'by id', text: text};
                }
              }

              // 2. exact text (case-insensitive)
              if (target) {
                const byText = clickable.filter(
                  b => (b.innerText || '').trim().toLowerCase() === target
                );
                if (byText.length) {
                  const el = byText[byText.length - 1];
                  const text = (el.innerText || '').trim();
                  el.click();
                  return {ok: true, how: 'by text', text: text};
                }
                // 3. substring match
                const bySub = clickable.filter(
                  b => (b.innerText || '').trim().toLowerCase().includes(target)
                );
                if (bySub.length) {
                  const el = bySub[bySub.length - 1];
                  const text = (el.innerText || '').trim();
                  el.click();
                  return {ok: true, how: 'by substring', text: text};
                }
              }

              // No match — return diagnostic of all visible buttons
              const visibleAll = all.filter(b => b.offsetParent !== null);
              return {
                ok: false,
                visible: visibleAll.map(b => ({
                  text: (b.innerText || '').trim(),
                  id: b.id || null,
                  disabled: b.disabled,
                })),
              };
            }
            """,
            {"targetId": target_id, "targetText": target_text},
        )
    except Exception as e:
        raise IframeLostError(f"playbook click eval: {e}") from e

    if result.get("ok"):
        log.info(f"playbook click ({result['how']}): {result['text']!r}")
        return

    raise OptionNotFoundError(
        f"playbook click could not find enabled button {target_text!r} "
        f"(id={target_id!r}); visible: {result.get('visible', [])}"
    )


def _resolve_value(action: Action, context: dict) -> str:
    """Resolve a TYPE action's value from the replay context. OTP slots
    prompt lazily so the OTP is always fresh."""
    slot = action.value_slot
    if slot == "literal":
        return action.literal_value or ""
    if slot == "operator_phone":
        return context["operator_phone"]
    if slot == "customer_phone":
        return context["customer_phone"]
    if slot == "otp":
        get_otp: Callable[[], str] = context["get_otp"]
        return get_otp()
    raise ValueError(f"unknown value_slot: {slot}")


def _replay_step(frame: Frame, action: Action, context: dict) -> None:
    if action.kind == "click":
        _click_by_action(frame, action)
        return
    value = _resolve_value(action, context)
    # Structured values (operator phone, customer phone, OTP) are sent in
    # response to an inline form prompt. Forbid the textarea fallback so
    # we never silently dump a phone number into the chat bar — that's
    # what caused the "row-skipping by typing into the wrong field" bug
    # whenever post-recovery state detection landed somewhere other than
    # the customer-phone form.
    require_inline = action.value_slot in (
        "operator_phone", "customer_phone", "otp",
    )
    chat.send_text(frame, value, require_inline=require_inline)
    # Never log OTP values; always log customer phones (they're in Excel anyway).
    display = "***" if action.value_slot == "otp" else value
    log.info(f"playbook type [{action.value_slot}]: {display!r}")


def replay_actions(frame: Frame, actions: list[Action], context: dict) -> str:
    """Walk `actions` in order, calling chat.wait_until_settled after each.
    Returns the concatenation of every new-text diff, so callers can scan
    for SUCCESS_RE."""
    accumulated = ""
    total = len(actions)
    for i, action in enumerate(actions, start=1):
        log.info(f"playbook step {i}/{total}: {action.describe()}")
        _replay_step(frame, action, context)
        snap = chat.wait_until_settled(frame)
        accumulated += ("\n---\n" if accumulated else "") + snap.text
    return accumulated


def replay_auth(
    frame: Frame,
    playbook: Playbook,
) -> None:
    """Run the auth_prefix once at bot startup.

    COLD-START HANDLING: right after OTP acceptance, HPCL shows a welcome
    bubble whose only enabled button is 'Main Menu' — Booking Services does
    not exist yet. The recorded auth_prefix starts with 'Booking Services',
    so a naive replay would fail with OptionNotFoundError, triggering a
    page reload. Aggressive reloads on a fresh session kill the HPCL
    backend session, land us back in NEEDS_OPERATOR_AUTH, and spam OTP
    prompts in a tight loop (observed 10+ OTP prompts in under a minute
    when the bot was first started with no cached cookies).

    Fix: before running auth_prefix, inspect enabled buttons. If the first
    auth_prefix target isn't there but Main Menu is, click Main Menu first
    to transition from the welcome bubble.

    POST-WELCOME-STATE NAVIGATION: clicking 'Main Menu' from the welcome
    bubble does NOT always land on the actual main menu — HPCL sometimes
    jumps the operator straight back into the BOOK_FOR_OTHERS sub-menu
    (the operator's last-used context, persisted in the HPCL session).
    A blind replay_actions(auth_prefix) then tries to click the now-
    disabled 'Booking Services' button and fails. Use the adaptive
    reset_to_customer_entry instead, which inspects enabled buttons and
    picks the shortest path to READY_FOR_CUSTOMER from whatever state
    we actually landed on (fixed 2026-04-15)."""
    log.info(f"playbook auth: {len(playbook.auth_prefix)} steps")

    first_target = (playbook.auth_prefix[0].button_text or "").strip().lower() \
        if playbook.auth_prefix else ""
    if first_target:
        snap = _read_state_snapshot(frame)
        enabled = [(b or "").lower() for b in (snap.get("enabled") or [])]
        has_first = any(first_target in b for b in enabled)
        has_main_menu = any("main menu" in b for b in enabled)
        if not has_first and has_main_menu:
            log.info(
                f"playbook auth: post-OTP welcome state detected "
                f"({first_target!r} not yet visible, clicking Main Menu first)"
            )
            _click_by_action(
                frame,
                Action(kind="click", button_text="Main Menu", button_id=None),
            )
            chat.wait_until_settled(frame)

    reset_to_customer_entry(frame, playbook)
    log.info("playbook auth: done")


def _choose_reset_target(
    enabled: list[str],
    escape_tried: bool,
    prev_menu_tried: bool,
) -> str:
    """Pure decision helper for reset_to_customer_entry. Given the list of
    enabled button labels and the two escape-attempted flags, return the
    name of the path the caller should take. One of:

        'book_with_other_mobile' — alt-menu dead-end, click the one button
            that exits it.
        'book_for_others'        — already in the sub-menu, click direct.
        'booking_services'       — at main menu, click Booking Services then
            Book for Others.
        'main_menu'              — at some other sub-menu, click Main Menu
            then replay auth_prefix.
        'no_escape'              — dangling Yes/No bubble from a 502 during
            'Yes' click; dismiss with 'No' and retry reset.
        'previous_menu_escape'   — payment-pending / dead-end dialog whose
            only enabled buttons are terminal actions + 'Previous Menu'.
            Click 'Previous Menu' to back out and retry reset.
        'none'                   — no path available; caller should raise.

    Priority matters: nav buttons always beat escape hatches, and each
    escape hatch is gated by its own 'tried' flag so we can never loop.
    The 'Previous Menu' escape is the LAST resort — an earlier priority
    would have matched if the current dialog were a real menu we can
    navigate forward from."""
    lower = [(b or "").lower() for b in enabled]

    def _has(needle: str) -> bool:
        return any(needle in b for b in lower)

    if _has("book with other mobile"):
        return "book_with_other_mobile"
    if _has("book for others"):
        return "book_for_others"
    if _has("booking services"):
        return "booking_services"
    if _has("main menu"):
        return "main_menu"
    if not escape_tried and _has("no"):
        return "no_escape"
    if not prev_menu_tried and _has("previous menu"):
        return "previous_menu_escape"
    return "none"


def reset_to_customer_entry(
    frame: Frame,
    playbook: Playbook,
    _escape_tried: bool = False,
    _prev_menu_tried: bool = False,
) -> None:
    """Navigate the chat from ANY state back to 'enter customer phone' with
    the MINIMUM number of clicks. Previous incarnations blindly clicked Main
    Menu → Booking Services → Book for Others regardless of current state,
    which caused OptionNotFoundError storms (HPCL disables old button
    instances within ~50ms of a click, so by the time step 2 runs the older
    instance is gone) and triggered page reloads that eventually burned the
    operator session.

    The strategy here is: look at what's enabled NOW and pick the shortest
    path. In order of preference:

      0. READY_FOR_CUSTOMER already → no clicks needed (post-row happy path
         when the playbook's own tail leaves us on phone input).
      1. Book With Other Mobile enabled → click it (alt-menu escape, the
         only button that exits that dead-end).
      2. Book for Others enabled → click it (we're already in the sub-menu).
      3. Booking Services enabled → click it, wait, then click Book for
         Others (we're on the main menu).
      4. Main Menu enabled → click it, wait, replay full auth_prefix.

    ESCAPE HATCHES (tried only as last resort, one attempt each):
      - 'No' — dangling Yes/No confirmation bubble (a 502 during a 'Yes'
        click left HPCL with only the 'No' button enabled). Dismiss with
        'No' and HPCL redraws the parent menu with nav buttons enabled.
      - 'Previous Menu' — payment-pending / dead-end dialogs that show
        only a terminal action (e.g. 'Make Payment') plus 'Previous Menu'.
        Back out with 'Previous Menu' and HPCL redraws the parent menu.

    Without these, stuck states forced full page reloads that destroyed
    the chat session and dragged the bot through the OTP-flood loop.

    Only raises IframeLostError / OptionNotFoundError if NONE of the
    above paths work — in which case the caller falls back to full
    recovery."""
    try:
        state = chat.detect_state(frame)
    except IframeLostError:
        state = "UNKNOWN"
    if state == "READY_FOR_CUSTOMER":
        log.info("playbook: already at READY_FOR_CUSTOMER; skipping reset")
        return

    snap = _read_state_snapshot(frame)
    enabled = snap.get("enabled") or []
    target = _choose_reset_target(
        enabled=enabled,
        escape_tried=_escape_tried,
        prev_menu_tried=_prev_menu_tried,
    )

    if target == "book_with_other_mobile":
        log.info("playbook: alt menu detected; clicking 'Book With Other Mobile'")
        _click_by_action(
            frame,
            Action(kind="click", button_text="Book With Other Mobile", button_id=None),
        )
        chat.wait_until_settled(frame)
        log.info("playbook: reset done (alt menu)")
        return

    if target == "book_for_others":
        log.info("playbook: 'Book for Others' already enabled; clicking direct")
        _click_by_action(
            frame,
            Action(kind="click", button_text="Book for Others", button_id=None),
        )
        chat.wait_until_settled(frame)
        log.info("playbook: reset done (direct Book for Others)")
        return

    if target == "booking_services":
        log.info("playbook: at main menu; clicking Booking Services → Book for Others")
        _click_by_action(
            frame,
            Action(kind="click", button_text="Booking Services", button_id=None),
        )
        chat.wait_until_settled(frame)
        _click_by_action(
            frame,
            Action(kind="click", button_text="Book for Others", button_id=None),
        )
        chat.wait_until_settled(frame)
        log.info("playbook: reset done (Booking Services → Book for Others)")
        return

    if target == "main_menu":
        log.info("playbook: resetting via Main Menu → auth_prefix")
        _click_by_action(
            frame,
            Action(kind="click", button_text="Main Menu", button_id=None),
        )
        chat.wait_until_settled(frame)
        # After Main Menu click, the main menu bubble appears with Booking
        # Services etc. If it doesn't (e.g. we were on the post-OTP welcome
        # bubble whose only button is another Main Menu that just closes
        # the chat), fall through to auth_prefix and let the error bubble
        # up.
        snap2 = _read_state_snapshot(frame)
        enabled2 = [(b or "").lower() for b in (snap2.get("enabled") or [])]
        first_target = (playbook.auth_prefix[0].button_text or "").strip().lower() \
            if playbook.auth_prefix else ""
        if first_target and not any(first_target in b for b in enabled2) \
                and any("main menu" in b for b in enabled2):
            log.info(
                "playbook: still on welcome state after Main Menu click; "
                "clicking Main Menu again"
            )
            _click_by_action(
                frame,
                Action(kind="click", button_text="Main Menu", button_id=None),
            )
            chat.wait_until_settled(frame)
        replay_actions(frame, playbook.auth_prefix, {})
        log.info("playbook: reset done (Main Menu path)")
        return

    if target == "no_escape":
        log.warning(
            f"playbook: reset stuck on dangling confirmation "
            f"(enabled={enabled!r}); clicking 'No' to dismiss and retrying reset"
        )
        try:
            _click_by_action(
                frame,
                Action(kind="click", button_text="No", button_id=None),
            )
        except OptionNotFoundError:
            log.warning("playbook: 'No' click failed; falling through to raise")
        else:
            chat.wait_until_settled(frame)
            reset_to_customer_entry(
                frame, playbook,
                _escape_tried=True, _prev_menu_tried=_prev_menu_tried,
            )
            return

    if target == "previous_menu_escape":
        log.warning(
            f"playbook: reset stuck on dead-end dialog "
            f"(enabled={enabled!r}); clicking 'Previous Menu' to back out "
            f"and retrying reset"
        )
        try:
            _click_by_action(
                frame,
                Action(kind="click", button_text="Previous Menu", button_id=None),
            )
        except OptionNotFoundError:
            log.warning(
                "playbook: 'Previous Menu' click failed; falling through to raise"
            )
        else:
            chat.wait_until_settled(frame)
            reset_to_customer_entry(
                frame, playbook,
                _escape_tried=_escape_tried, _prev_menu_tried=True,
            )
            return

    raise OptionNotFoundError(
        f"reset_to_customer_entry: no usable nav button; enabled={enabled}"
    )


def _reset_after_salvage(frame: Frame, playbook: Playbook | None) -> None:
    """Best-effort UI reset after a salvaged Success. A salvage path means
    the booking succeeded but the trailing nav steps (Previous Menu → Book
    for Others) did NOT run, so the chat is stranded on a Make Payment /
    error bubble. If we don't reset here, cli.py's post-Success branch skips
    reset, and the NEXT row's baseline is captured from a stranded UI —
    that's how cross-row code contamination happens.

    Swallows all exceptions: we already have the salvaged code, data
    integrity is preserved; if the reset fails, the next row's replay will
    hit an error and cli.py will trigger full recovery.
    """
    if playbook is None:
        return
    try:
        reset_to_customer_entry(frame, playbook)
    except Exception as e:
        log.warning(
            f"post-salvage reset failed ({type(e).__name__}: {e}); "
            f"next row will need full recovery"
        )


# ---- Post-failure state classification ----
#
# When a click fails with OptionNotFoundError it usually means HPCL replied
# with something other than the expected booking flow — most commonly a
# pending-payment notice, an invalid-customer error, or a service-unavailable
# message. We inspect the enabled buttons + scroller tail and turn the raw
# OptionNotFoundError into a human-readable Issue reason. The raw field
# carries the post-baseline scroller slice so the operator can verify.

_PAYMENT_BTN_RE   = re.compile(r"make\s*payment", re.IGNORECASE)
_PAYMENT_TEXT_RE  = re.compile(
    r"pending\s+payment|outstanding\s+amount|please\s+clear\s+.*(dues|payment|amount)"
    r"|amount\s+due|pay.*before.*booking",
    re.IGNORECASE,
)
_INVALID_TEXT_RE  = re.compile(
    r"invalid\s+(mobile|lpg|customer|number|consumer)"
    r"|not\s+(found|registered|valid)"
    r"|does\s+not\s+(exist|match)"
    r"|no\s+(record|customer)\s+found"
    r"|customer\s+not\s+found",
    re.IGNORECASE,
)
_ALREADY_BOOKED_RE = re.compile(
    r"already\s+booked|cylinder\s+already|refill\s+already|booking\s+.*exists",
    re.IGNORECASE,
)
_EKYC_TEXT_RE = re.compile(
    r"aadhaar\s*(?:e-?kyc|ekyc)"
    r"|refill\s+booking\s+is\s+blocked.*ekyc"
    r"|ekyc\s+is\s+pending"
    r"|complete\s+(?:aadhaar\s+)?authentication",
    re.IGNORECASE,
)
_NOT_REGISTERED_TEXT_RE = re.compile(
    r"not\s+(?:a\s+)?registered\s+(?:with\s+)?(?:hpcl|hp\s*gas)?"
    r"|mobile\s+(?:number\s+)?is\s+not\s+registered"
    r"|number\s+is\s+not\s+registered"
    r"|no\s+(?:hp\s*gas\s+)?connection\s+(?:is\s+)?(?:found|linked|registered)"
    r"|not\s+linked\s+to\s+any\s+(?:hp\s*gas|hpcl)",
    re.IGNORECASE,
)
_NOT_REGISTERED_BTN_RE = re.compile(
    r"book\s+with\s+other\s+mobile",
    re.IGNORECASE,
)


def _read_state_snapshot(frame: Frame) -> dict:
    """Pull visible-enabled button labels + last 1500 chars of scroller in
    one in-page eval. Used by _wait_for_next_state and _classify_failure."""
    try:
        return frame.evaluate(
            f"""
            () => {{
              const btns = Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
                .filter(b => b.offsetParent !== null);
              const enabled = btns.filter(b => !b.disabled)
                .map(b => (b.innerText || '').trim())
                .filter(t => t.length > 0);
              const s = document.querySelector('{config.SEL_SCROLLER}');
              const text = s ? (s.innerText || '').slice(-1500) : '';
              return {{enabled: enabled, text: text}};
            }}
            """
        ) or {}
    except Exception as e:
        log.debug(f"_read_state_snapshot failed: {e}")
        return {}


def _classify_failure(
    frame: Frame,
    baseline: str,
    expected_click_text: str,
) -> chat.BookingResult:
    """Inspect the live chat state after a click failed, and return a
    Success if the booking actually went through, or an Issue otherwise.
    Reason precedence:
      0. success — scroller contains 'delivery confirmation code is NNNNNN'
         (checked FIRST because HPCL's post-booking menu shows a Make Payment
         button for the just-booked invoice and would otherwise be
         misclassified as pending_payment)
      1. ekyc_not_done — scroller mentions Aadhaar eKYC pending / blocked
      2. pending_payment — 'Make Payment' button visible OR scroller says
         pending/outstanding/due
      3. invalid_customer — scroller says invalid/not found/does not exist
      4. already_booked — scroller says already booked / refill exists
      5. unknown_state — none of the above matched; include visible buttons
    """
    snap = _read_state_snapshot(frame)
    enabled = snap.get("enabled") or []
    text = snap.get("text") or ""
    new_text = _post_baseline_text(text, baseline)
    # For STATE keyword checks (eKYC / payment / invalid / already) we accept
    # a fallback to the last-1500-chars tail, because those patterns are
    # qualitative state markers, not codes. Misclassifying a payment page
    # just means we won't retry a terminal row — not a data-integrity issue.
    text_for_search = new_text or text

    # Success detection, however, MUST use the safe baseline-relative salvage
    # helper — falling back to `text` here would pull a prior row's 6-digit
    # code out of the stale scroller tail and attribute it to THIS row.
    # Observed: recheck_30_46 run at 23:07 leaked row 1's 719275 into row 2.
    salvaged = _salvage_success_from_scroller(frame, baseline)
    if salvaged is not None:
        return chat.Success(
            code=salvaged,
            raw=f"classifier salvage (expected click {expected_click_text!r})",
        )

    if any(_NOT_REGISTERED_BTN_RE.search(b) for b in enabled) or \
            _NOT_REGISTERED_TEXT_RE.search(text_for_search):
        return chat.Issue(
            reason="not_registered",
            raw=f"enabled_buttons={enabled}; scroller_tail={text_for_search[-500:]!r}",
        )
    if _EKYC_TEXT_RE.search(text_for_search):
        return chat.Issue(
            reason="ekyc_not_done",
            raw=f"enabled_buttons={enabled}; scroller_tail={text_for_search[-500:]!r}",
        )
    if any(_PAYMENT_BTN_RE.search(b) for b in enabled) or \
            _PAYMENT_TEXT_RE.search(text_for_search):
        return chat.Issue(
            reason="pending_payment",
            raw=f"enabled_buttons={enabled}; scroller_tail={text_for_search[-500:]!r}",
        )
    if _INVALID_TEXT_RE.search(text_for_search):
        return chat.Issue(
            reason="invalid_customer",
            raw=f"enabled_buttons={enabled}; scroller_tail={text_for_search[-500:]!r}",
        )
    if _ALREADY_BOOKED_RE.search(text_for_search):
        return chat.Issue(
            reason="already_booked",
            raw=f"enabled_buttons={enabled}; scroller_tail={text_for_search[-500:]!r}",
        )
    return chat.Issue(
        reason=f"unknown_state (expected click {expected_click_text!r})",
        raw=f"enabled_buttons={enabled}; scroller_tail={text_for_search[-500:]!r}",
    )


def _wait_for_next_state(
    frame: Frame,
    target_text: str,
    timeout_s: float = 8.0,
) -> bool:
    """After a click failed with OptionNotFoundError, HPCL may still be
    streaming bubbles. Poll for up to `timeout_s` seconds looking for:
      - the target click text (enabled) — maybe the bot was just early;
        caller can retry the click
      - any definitive terminal state (Make Payment button, pending payment
        text, invalid-customer text) — caller should stop and classify

    Returns True if target_text became available (caller should retry click),
    False if any terminal state appeared or the timeout elapsed (caller
    should classify).
    """
    deadline = time.monotonic() + timeout_s
    target_re = re.compile(re.escape(target_text), re.IGNORECASE) if target_text else None
    while time.monotonic() < deadline:
        snap = _read_state_snapshot(frame)
        enabled = snap.get("enabled") or []
        text = snap.get("text") or ""

        if target_re is not None:
            for b in enabled:
                if target_re.search(b):
                    return True

        # Any terminal signal → stop polling, let classifier take over.
        if any(_PAYMENT_BTN_RE.search(b) for b in enabled):
            return False
        if _PAYMENT_TEXT_RE.search(text) or _INVALID_TEXT_RE.search(text) \
                or _ALREADY_BOOKED_RE.search(text):
            return False

        time.sleep(0.5)
    return False


def _post_baseline_text(full: str, baseline: str) -> str:
    """Return the portion of `full` that appeared AFTER `baseline` was captured.

    Handles two cases:
      1. Happy case: `full` is `baseline` with new content appended. Return
         the appended slice.
      2. HPCL trimmed old scroller bubbles after baseline was captured.
         `full.startswith(baseline)` is False because the head got clipped.
         Locate the TAIL of baseline inside `full` (rfind to handle repeated
         substrings) and return everything after it.

    Returns "" when we can't safely locate baseline inside full — in that
    case the caller must not claim any success code from this row. Returning
    the full scroller on fallback is what caused the repeated false-positive
    bug (rows 10/11 inheriting row 9's confirmation code), so we refuse to
    guess.

    EMPTY BASELINE is treated as UNSAFE. When chat.full_scroller_text fails
    at the start of a row it returns ""; returning `full` here would let the
    happy path match ANY prior row's 6-digit code and attribute it to this
    row. We return "" so the caller falls through to an Issue instead.
    """
    if not baseline:
        return ""
    if full.startswith(baseline):
        return full[len(baseline):]
    tail = baseline[-500:] if len(baseline) > 500 else baseline
    idx = full.rfind(tail)
    if idx >= 0:
        return full[idx + len(tail):]
    return ""


def _salvage_success_from_scroller(frame: Frame, baseline: str = "") -> str | None:
    """Scan the scroller text that appeared AFTER `baseline` for a SUCCESS_RE
    match. Used when a post-success navigation click fails: the booking already
    succeeded, we just can't walk back to the idle state.

    CRITICAL: must only attribute codes that actually belong to THIS row. The
    scroller contains every prior row's confirmation code too — naively
    searching the full scroller attributes an old code to the current failing
    row (observed in production: rows 5, 6, 7 all reported the same 719222
    from row 5; rows 9, 10, 11 all reported 719225).

    Strategy:
      1. Strict slice — take the text that appeared past `baseline`
         (startswith, or rfind on baseline's tail). If SUCCESS_RE matches
         that slice, return the code.
      2. Count-delta fallback — if HPCL trimmed the scroller so aggressively
         that baseline can't be located at all, compare SUCCESS_RE match
         counts in `baseline` vs `full`. If `full` has strictly more, the
         newest match is this row's code. If equal, we got nothing new
         and must not claim a code.

    Returns the 6-digit code or None. Never raises.
    """
    if not baseline:
        # No reference point — we cannot distinguish a new code from a stale
        # one. Refusing to guess is the only safe behaviour.
        log.warning("_salvage_success_from_scroller called with empty baseline — refusing to claim a code")
        return None
    try:
        full = chat.full_scroller_text(frame)
    except Exception as e:
        log.warning(f"_salvage_success_from_scroller read failed: {e}")
        return None
    baseline_codes_set = set(config.SUCCESS_RE.findall(baseline))
    new_text = _post_baseline_text(full, baseline)
    m = config.SUCCESS_RE.search(new_text)
    if m:
        code = m.group(1)
        # Hard defense against cross-row contamination: if this exact code
        # was already in baseline, it's the previous row's code leaking in
        # through a `_post_baseline_text` edge case (HPCL re-rendering,
        # duplicate scroller trims, etc). Observed in production: row 13
        # salvaged 719290 which belonged to row 11.
        if code in baseline_codes_set:
            log.warning(
                f"salvage: code {code} already present in baseline "
                f"(stale from prior row) — refusing to claim"
            )
        else:
            return code
    if new_text == "":
        full_codes = config.SUCCESS_RE.findall(full)
        # Claim only codes that are STRICTLY new (not already in baseline).
        # Previous implementation compared counts only, which allowed a
        # code to move position within the trimmed scroller and be re-
        # claimed as "the newest".
        new_codes = [c for c in full_codes if c not in baseline_codes_set]
        if new_codes:
            log.info(
                f"salvage count-delta: baseline had {len(baseline_codes_set)} "
                f"unique codes, scroller now has new codes {new_codes} — "
                f"claiming newest: {new_codes[-1]}"
            )
            return new_codes[-1]
    return None


def replay_booking(
    frame: Frame,
    playbook: Playbook,
    customer_phone: str,
) -> chat.BookingResult:
    """Run the booking_body for one customer. Returns Success on a
    SUCCESS_RE match, Issue otherwise.

    Success detection reads the FULL scroller after replay and searches only
    the post-baseline slice (text that appeared during THIS row). We do NOT
    search the accumulated per-step diffs from wait_until_settled: when HPCL
    trims old bubbles mid-row, wait_until_settled's startswith check silently
    returns the whole scroller as 'new text', which pulls the previous row's
    confirmation code into accumulated and causes false positives (observed:
    rows 9→10→11 all reporting 719225). Baseline-relative full-scroller read
    is the single source of truth.

    On any playbook-level failure the Issue raw includes the chat state so
    the operator can diagnose from the Issues workbook alone.
    """
    context = {"customer_phone": customer_phone}

    # Precondition: the chat MUST be at READY_FOR_CUSTOMER before we type the
    # phone. If a prior row's reset half-finished, we may be sitting on a
    # menu screen instead — typing the customer phone there would either fall
    # back to the textarea (now blocked by require_inline) or worse, type into
    # a stale inline input from an earlier bubble. In either case the row gets
    # marked failed silently and the next row inherits the broken state.
    #
    # Poll briefly: the customer-phone input bubble can render a beat after
    # auth's last settle returns, especially on the first row of a batch.
    # Only fall through to the in-place reset once we've given the page a
    # fair chance to render the input. If that still doesn't land us on
    # READY_FOR_CUSTOMER, raise so cli.py's recovery path takes over.
    pre_state = "UNKNOWN"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            pre_state = chat.detect_state(frame)
        except IframeLostError:
            pre_state = "UNKNOWN"
        if pre_state == "READY_FOR_CUSTOMER":
            break
        time.sleep(0.5)
    if pre_state != "READY_FOR_CUSTOMER":
        log.warning(
            f"replay_booking precondition: state is {pre_state!r}, not "
            f"READY_FOR_CUSTOMER — attempting one in-place reset before typing"
        )
        try:
            reset_to_customer_entry(frame, playbook)
        except (OptionNotFoundError, ChatStuckError) as e:
            log.warning(f"replay_booking precondition reset failed: {e}")
            raise OptionNotFoundError(
                f"replay_booking: cannot reach READY_FOR_CUSTOMER for "
                f"{customer_phone[:3]}XXXXXXX (state was {pre_state!r}); "
                f"reset failed: {e}"
            ) from e
        try:
            after = chat.detect_state(frame)
        except IframeLostError:
            after = "UNKNOWN"
        if after != "READY_FOR_CUSTOMER":
            raise OptionNotFoundError(
                f"replay_booking: post-reset state is {after!r}, expected "
                f"READY_FOR_CUSTOMER for {customer_phone[:3]}XXXXXXX"
            )

    # Baseline MUST be captured before any action runs. Every later success
    # search is restricted to text that appeared past this point, so earlier
    # rows' confirmation codes can never be attributed to this row.
    baseline = chat.full_scroller_text(frame)
    if not baseline:
        # Empty baseline = we cannot safely attribute any later code to THIS
        # row. Without a baseline, SUCCESS_RE would match any prior code still
        # sitting in the scroller and falsely credit it to this customer.
        # Fail fast so cli.py treats this as a transient Issue and retries.
        log.warning(
            f"baseline_capture_failed for {customer_phone}: "
            f"chat.full_scroller_text returned empty"
        )
        return chat.Issue(
            reason="baseline_capture_failed",
            raw="chat.full_scroller_text returned empty — cannot safely attribute success code",
        )
    try:
        accumulated = replay_actions(frame, playbook.booking_body, context)
    except OptionNotFoundError as e:
        # A click couldn't find its target button. Two reasons this happens:
        #   (a) HPCL is still streaming bubbles — the target will appear in a
        #       second or two. We poll for it before giving up.
        #   (b) HPCL responded with a different state entirely (pending
        #       payment, invalid LPG number, service error). We classify the
        #       current state into a human-readable Issue so the operator can
        #       see WHY the row failed in the Issues workbook.
        # Before either path, check if the booking already succeeded — the
        # confirmation code may be sitting in the scroller from a trailing
        # nav click that failed post-success.
        salvaged = _salvage_success_from_scroller(frame, baseline)
        if salvaged is not None:
            log.info(
                f"salvaged success code {salvaged} after "
                f"OptionNotFoundError: {e}"
            )
            full_now = chat.full_scroller_text(frame)
            new_text = _post_baseline_text(full_now, baseline)
            _reset_after_salvage(frame, playbook)
            return chat.Success(code=salvaged, raw=new_text[-2000:])

        target_text = ""
        msg = str(e)
        m = re.search(r"button\s+'([^']+)'", msg)
        if m:
            target_text = m.group(1)

        if target_text and _wait_for_next_state(frame, target_text):
            # The target appeared. Retry just the failed click by re-running
            # replay_actions on a fresh baseline — cleanest way to resume
            # without tracking step indices. Safer: retry the single click
            # directly rather than restarting the whole body.
            log.info(
                f"target button {target_text!r} appeared after retry wait; "
                f"clicking and finishing booking_body"
            )
            try:
                _click_by_action(
                    frame,
                    Action(kind="click", button_text=target_text, button_id=None),
                )
                chat.wait_until_settled(frame)
            except (OptionNotFoundError, ChatStuckError) as retry_e:
                log.warning(f"post-wait click retry failed: {retry_e}")
                classified = _classify_failure(frame, baseline, target_text)
                if isinstance(classified, chat.Success):
                    _reset_after_salvage(frame, playbook)
                return classified
            # After the delayed click, the recorded nav steps (Previous Menu,
            # Book for Others) did NOT run, so the UI is NOT at the
            # customer-phone input. Run reset_to_customer_entry after a
            # successful code detection to re-land there cleanly.
            full_now = chat.full_scroller_text(frame)
            new_text = _post_baseline_text(full_now, baseline)
            baseline_codes_set = set(config.SUCCESS_RE.findall(baseline))
            mm = config.SUCCESS_RE.search(new_text)
            if mm:
                code = mm.group(1)
                if code in baseline_codes_set:
                    log.warning(
                        f"post-retry: code {code} already in baseline "
                        f"(stale from prior row) — refusing to claim"
                    )
                else:
                    _reset_after_salvage(frame, playbook)
                    return chat.Success(code=code, raw=new_text[-2000:])
            classified = _classify_failure(frame, baseline, target_text)
            if isinstance(classified, chat.Success):
                _reset_after_salvage(frame, playbook)
            return classified

        classified = _classify_failure(frame, baseline, target_text)
        if isinstance(classified, chat.Success):
            _reset_after_salvage(frame, playbook)
        return classified
    except ChatStuckError as e:
        # Wait timed out with no scroller change. HPCL sometimes renders the
        # confirmation code AFTER wait_until_settled gives up, so poll the
        # scroller briefly before falling through to classification.
        deadline = time.monotonic() + 5.0
        salvaged: str | None = None
        while time.monotonic() < deadline:
            salvaged = _salvage_success_from_scroller(frame, baseline)
            if salvaged is not None:
                break
            time.sleep(0.5)
        if salvaged is not None:
            log.info(f"salvaged success after ChatStuckError: {e}")
            full_now = chat.full_scroller_text(frame)
            new_text = _post_baseline_text(full_now, baseline)
            _reset_after_salvage(frame, playbook)
            return chat.Success(code=salvaged, raw=new_text[-2000:])
        classified = _classify_failure(frame, baseline, "")
        if isinstance(classified, chat.Success):
            _reset_after_salvage(frame, playbook)
            return classified
        if classified.reason.startswith("unknown"):
            return chat.Issue(
                reason=f"playbook_stuck:{e}",
                raw=chat.dump_visible_state(frame),
            )
        return classified
    except (IframeLostError, GatewayError) as e:
        # Same salvage-first pattern — code may already be in the scroller.
        salvaged = _salvage_success_from_scroller(frame, baseline)
        if salvaged is not None:
            log.info(
                f"salvaged success code {salvaged} after "
                f"{type(e).__name__}: {e}"
            )
            full_now = chat.full_scroller_text(frame)
            new_text = _post_baseline_text(full_now, baseline)
            _reset_after_salvage(frame, playbook)
            return chat.Success(code=salvaged, raw=new_text[-2000:])
        # Transient — re-raise so cli.py can recover and retry.
        raise

    # Read the scroller after replay finishes and search only the slice that
    # appeared since baseline.
    full = chat.full_scroller_text(frame)
    new_text = _post_baseline_text(full, baseline)
    baseline_codes_set = set(config.SUCCESS_RE.findall(baseline))
    m = config.SUCCESS_RE.search(new_text)
    if m:
        code = m.group(1)
        # Final guard against cross-row contamination: the happy-path slice
        # should never contain a code that was already in baseline. If it
        # does, _post_baseline_text mis-sliced (HPCL trim edge case) and we
        # must not falsely credit this row with the prior row's code.
        if code not in baseline_codes_set:
            return chat.Success(code=code, raw=new_text[-2000:])
        log.warning(
            f"happy path: code {code} already in baseline "
            f"(stale from prior row) — refusing to claim"
        )

    # No success code: run the state classifier. For unregistered numbers, all
    # 4 replay clicks succeed (HPCL reuses Yes/Previous Menu/Book for Others ids
    # across rows and the JS click-by-id still finds enabled matches in the
    # scroller), but no booking is created — we must inspect the resulting
    # scroller state so rows like "not registered" get a terminal tag instead
    # of looping forever as transient playbook_no_success_code.
    classified = _classify_failure(frame, baseline, "")
    if isinstance(classified, chat.Success):
        _reset_after_salvage(frame, playbook)
        return classified
    if not classified.reason.startswith("unknown"):
        return classified

    return chat.Issue(
        reason="playbook_no_success_code",
        raw=accumulated + "\n---NEW-SCROLLER---\n" + new_text[-2000:]
        + "\n---VISIBLE---\n" + chat.dump_visible_state(frame),
    )
