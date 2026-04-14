# Overnight Batch Survivability Implementation Plan

> **For agentic workers:** This plan is executed inline by the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a 3,500-customer batch overnight without waking the operator for OTPs, losing rows to transient failures, or getting stuck on session flaps.

**Architecture:** Five self-contained changes gated behind config constants. Section 4 adds a persistent per-row attempt budget. Section 5 adds a session-dead cleanup that only alarms when real work remains. Section 1 adds a persistent last-auth timestamp + 3-hour cooldown that stops the bot typing the operator phone on every gateway flap. Section 3 replaces the existing rapid-reauth circuit breaker with a 30-minute quiet retry loop, bumps MAX_AUTO_RESTARTS to 200, and shields the second Ctrl-C so cookies flush cleanly. Section 2 (keepalive investigation) is deferred.

**Tech Stack:** Python 3.12, Playwright sync, openpyxl, pytest.

**Source spec:** `docs/superpowers/specs/2026-04-15-overnight-batch-survivability-design.md`

**Implementation order:** 4 → 5 → 1 → 3 (Section 2 deferred).

---

## Task 1: Per-row attempt budget (Section 4)

**Files:**
- Modify: `booking_bot/excel.py` (add get/increment_attempt_count, write to col D)
- Modify: `booking_bot/cli.py` (check count before each row, bump after Issue, flip to ISSUE at 3)
- Modify: `booking_bot/config.py` (MAX_ATTEMPTS_PER_ROW: 2 → 3)
- Test: `tests/test_excel_store.py` (attempt-count roundtrip, backwards compat)

- [ ] **Step 1.1: Write failing tests for attempt_count helpers**

Add to `tests/test_excel_store.py` at the end of the file:

```python
def test_attempt_count_defaults_to_zero_on_fresh_row(store_env):
    inp = _make_input(store_env, [("C1", "9876543210")])
    store = ExcelStore(inp)
    assert store.get_attempt_count(1) == 0


def test_attempt_count_increments_and_persists(store_env):
    inp = _make_input(store_env, [("C1", "9876543210")])
    store = ExcelStore(inp)
    assert store.increment_attempt_count(1) == 1
    assert store.increment_attempt_count(1) == 2
    assert store.get_attempt_count(1) == 2

    store2 = ExcelStore(inp)  # reload from disk
    assert store2.get_attempt_count(1) == 2


def test_attempt_count_missing_col_d_reads_as_zero(store_env):
    """Output workbooks written before this feature exist without col D.
    Reading them must not crash and must return 0 so existing batches
    resume cleanly."""
    inp = _make_input(store_env, [("C1", "9876543210")])
    store = ExcelStore(inp)
    assert store.get_attempt_count(1) == 0  # col D genuinely empty


def test_attempt_count_non_integer_col_d_reads_as_zero(store_env):
    """Defensive: if someone hand-edits col D to a string, treat as 0."""
    inp = _make_input(store_env, [("C1", "9876543210")])
    store = ExcelStore(inp)
    import openpyxl
    wb = openpyxl.load_workbook(store.output_path)
    wb.active.cell(row=1, column=4).value = "not a number"
    wb.save(store.output_path)
    store2 = ExcelStore(inp)
    assert store2.get_attempt_count(1) == 0
```

- [ ] **Step 1.2: Run the new tests and confirm they fail**

Run: `python -m pytest tests/test_excel_store.py::test_attempt_count_defaults_to_zero_on_fresh_row tests/test_excel_store.py::test_attempt_count_increments_and_persists tests/test_excel_store.py::test_attempt_count_missing_col_d_reads_as_zero tests/test_excel_store.py::test_attempt_count_non_integer_col_d_reads_as_zero -v`

Expected: 4 FAIL with `AttributeError: 'ExcelStore' object has no attribute 'get_attempt_count'`.

- [ ] **Step 1.3: Implement the helpers in excel.py**

Add two methods to `ExcelStore` between `clear_issue` and `_ensure_issues_workbook`:

```python
    def get_attempt_count(self, row_idx: int) -> int:
        """Read the per-row attempt count from col D. Returns 0 when the
        cell is empty, missing, or non-integer — old Output workbooks
        written before this feature have no col D at all, so a missing
        cell must degrade gracefully to zero."""
        raw = self._ws.cell(row=row_idx, column=4).value
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def increment_attempt_count(self, row_idx: int) -> int:
        """Bump col D by 1 and persist. Returns the new count so callers
        don't need a second read."""
        new = self.get_attempt_count(row_idx) + 1
        self._ws.cell(row=row_idx, column=4).value = new
        self._atomic_save(self._wb, self.output_path)
        return new
```

- [ ] **Step 1.4: Run the four new tests and confirm they pass**

Run: `python -m pytest tests/test_excel_store.py::test_attempt_count_defaults_to_zero_on_fresh_row tests/test_excel_store.py::test_attempt_count_increments_and_persists tests/test_excel_store.py::test_attempt_count_missing_col_d_reads_as_zero tests/test_excel_store.py::test_attempt_count_non_integer_col_d_reads_as_zero -v`

Expected: 4 PASS.

- [ ] **Step 1.5: Run full test suite — no regressions**

Run: `python -m pytest tests/ -q`

Expected: all previous tests + 4 new = all PASS.

- [ ] **Step 1.6: Bump MAX_ATTEMPTS_PER_ROW in config.py**

In `booking_bot/config.py`, change:

```python
MAX_ATTEMPTS_PER_ROW  = 2
```

to:

```python
# How many independent Issue outcomes a single row gets before col C is
# locked to literal "ISSUE" and the row stops appearing in pending_rows().
# Survives restarts because the count is persisted in col D of the Output
# workbook.
MAX_ATTEMPTS_PER_ROW  = 3
```

- [ ] **Step 1.7: Wire the attempt-count budget into cli.py Issue handling**

In `booking_bot/cli.py`, find the block around line 612 (the `else:` branch that calls `store.write_issue`). The current code is:

```python
                    else:
                        store.write_issue(row_idx, phone, result.reason, result.raw)
                        if not _is_terminal_issue(result.reason):
                            transient_rows.append(row_idx)
```

Replace it with:

```python
                    else:
                        # Per-row attempt budget (Section 4). Terminal reasons
                        # (pending_payment, invalid_customer, already_booked,
                        # invalid_phone_format, not_registered) go straight
                        # to write_issue and lock col C forever — retrying
                        # won't change HPCL's verdict. Transient reasons get
                        # up to MAX_ATTEMPTS_PER_ROW chances; until the cap
                        # we leave col C empty and bump col D so the row
                        # re-enters pending_rows() on the next pass or run.
                        if _is_terminal_issue(result.reason):
                            store.write_issue(row_idx, phone, result.reason, result.raw)
                        else:
                            new_count = store.increment_attempt_count(row_idx)
                            if new_count >= config.MAX_ATTEMPTS_PER_ROW:
                                log.warning(
                                    f"row {row_idx}: attempt {new_count}/"
                                    f"{config.MAX_ATTEMPTS_PER_ROW} reached — "
                                    f"locking as ISSUE ({result.reason})"
                                )
                                store.write_issue(row_idx, phone, result.reason, result.raw)
                            else:
                                # Leave col C empty so pending_rows() yields
                                # this row again, but STILL log a diagnostic
                                # line to Issues so the operator has the raw
                                # chatbot response for later debugging.
                                log.info(
                                    f"row {row_idx}: attempt {new_count}/"
                                    f"{config.MAX_ATTEMPTS_PER_ROW} failed "
                                    f"({result.reason}) — leaving pending"
                                )
                                transient_rows.append(row_idx)
```

- [ ] **Step 1.8: Delete the now-redundant "clear_issue on transient" loop at pass end**

In `booking_bot/cli.py`, find the block around line 738 (at the end of the per-pass cleanup):

```python
            # Clear transient ISSUE rows so pending_rows() re-yields them on
            # the next pass. Terminal rows keep their ISSUE marker.
            log.info(
                f"clearing {len(transient_rows)} transient rows for "
                f"pass {pass_num + 1}: {transient_rows}"
            )
            for ridx in transient_rows:
                store.clear_issue(ridx)
```

Replace with:

```python
            # Transient rows (col D < MAX_ATTEMPTS_PER_ROW) already have
            # col C empty — the Section 4 attempt-budget branch never wrote
            # ISSUE for them. So pending_rows() will yield them again on the
            # next pass for free. This log line is just a progress marker.
            log.info(
                f"{len(transient_rows)} transient row(s) will be retried on "
                f"pass {pass_num + 1}: {transient_rows}"
            )
```

- [ ] **Step 1.9: Also apply budget to the unexpected-exception branch**

In `booking_bot/cli.py`, find the `except Exception as row_e:` block around line 663. The existing code calls `store.write_issue(row_idx, phone, reason=f"unexpected:{type(row_e).__name__}", raw=str(row_e)[:500])`. Wrap that call with the same attempt-count logic:

Find:

```python
                    try:
                        store.write_issue(
                            row_idx,
                            phone,
                            reason=f"unexpected:{type(row_e).__name__}",
                            raw=str(row_e)[:500],
                        )
                    except Exception as write_e:
                        log.error(f"  (could not write issue: {write_e})")
                    transient_rows.append(row_idx)
```

Replace with:

```python
                    try:
                        new_count = store.increment_attempt_count(row_idx)
                        if new_count >= config.MAX_ATTEMPTS_PER_ROW:
                            log.warning(
                                f"row {row_idx}: unexpected error on "
                                f"attempt {new_count}/{config.MAX_ATTEMPTS_PER_ROW} "
                                f"— locking as ISSUE"
                            )
                            store.write_issue(
                                row_idx,
                                phone,
                                reason=f"unexpected:{type(row_e).__name__}",
                                raw=str(row_e)[:500],
                            )
                        else:
                            log.info(
                                f"row {row_idx}: unexpected error on "
                                f"attempt {new_count}/{config.MAX_ATTEMPTS_PER_ROW} "
                                f"— leaving pending"
                            )
                            transient_rows.append(row_idx)
                    except Exception as write_e:
                        log.error(f"  (could not write attempt_count: {write_e})")
                        transient_rows.append(row_idx)
```

- [ ] **Step 1.10: Run full test suite**

Run: `python -m pytest tests/ -q`

Expected: all tests PASS.

- [ ] **Step 1.11: Commit**

```bash
git add booking_bot/config.py booking_bot/excel.py booking_bot/cli.py tests/test_excel_store.py
git commit -m "$(cat <<'EOF'
feat: per-row attempt budget (3 strikes → ISSUE)

Adds attempt_count in col D of the Output workbook. Transient failures
leave col C empty and bump the counter; only the third strike locks the
row as ISSUE. Previously a single glitch permanently lost the row.

Backwards compat: missing col D reads as 0.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Session-dead cleanup check (Section 5)

**Files:**
- Modify: `booking_bot/cli.py` (new helper `_session_dead_cleanup`, plug into login_if_needed failure path)

Section 5 of the spec. The check is a small pure helper: given the store, decide whether any pending rows remain. It piggybacks on the Section 4 invariant (rows with attempt_count >= MAX_ATTEMPTS_PER_ROW are already col C = "ISSUE" and don't appear in pending_rows()). Actual integration into the quiet retry loop happens in Task 4.

- [ ] **Step 2.1: Add the helper to cli.py**

In `booking_bot/cli.py`, add this function above `_run_session_attempt` (around line 394):

```python
def _session_dead_cleanup_has_retriable_rows(store) -> tuple[bool, int]:
    """Section 5 of the survivability design. Called after the 30-min quiet
    retry loop has given up waiting for HPCL's session to heal.

    Returns (has_retriable, pending_count):
      - has_retriable=False, pending_count=0 → the batch drained to zero
        during the retry loop. Nothing a fresh OTP would unblock. Caller
        should exit cleanly without alarming the operator.
      - has_retriable=True,  pending_count=N → N rows still need work.
        Every pending row is retriable (attempt_count < MAX_ATTEMPTS_PER_ROW
        by the Section 4 invariant — capped rows have col C='ISSUE' and
        never appear in pending_rows). Caller should fire the loud idle
        alarm and prompt the operator for a fresh OTP.
    """
    pending = list(store.pending_rows())
    return (len(pending) > 0, len(pending))
```

- [ ] **Step 2.2: Commit the helper skeleton**

No tests yet — the helper is a one-liner around `pending_rows()` which is already covered by existing tests. Integration tests arrive with Task 4.

```bash
git add booking_bot/cli.py
git commit -m "$(cat <<'EOF'
feat: session-dead cleanup helper (Section 5)

Adds _session_dead_cleanup_has_retriable_rows. Plumbed into the quiet
retry loop in Task 4 — by itself this commit is dead code, landed early
so the next tasks can reference it without a forward-declaration.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Persistent auth timestamp + 3-hour cooldown (Section 1)

**Files:**
- Modify: `booking_bot/config.py` (add AUTH_COOLDOWN_S, remove MAX_CONSECUTIVE_REAUTHS)
- Modify: `booking_bot/browser.py` (persist last_auth_at to disk, rewrite last_auth_age_s, drop rapid-reauth counter)
- Modify: `booking_bot/auth.py` (new return sentinel "cooldown_wait", delete rapid-reauth branch)
- Modify: `booking_bot/cli.py` (handle the new return values from login_if_needed)

- [ ] **Step 3.1: Add AUTH_COOLDOWN_S to config.py and remove rapid-reauth constants**

In `booking_bot/config.py`, find the Circuit breakers block (around line 72). Replace the `MAX_CONSECUTIVE_REAUTHS` constant and its entire comment block with:

```python
# MAX_CONSECUTIVE_ROW_FAILURES: abort the batch after this many rows in a
# row end in recovery_failed / recovered_but_failed. A single bad row is
# fine; ten in a row means we're stuck in a 502 cascade and nothing we
# do is helping. The operator should rerun later when HPCL recovers.
MAX_CONSECUTIVE_ROW_FAILURES = 5
# AUTH_COOLDOWN_S (Section 1 of the survivability design): the bot is
# allowed to type the operator phone number at most once per this window.
# The timestamp lives at .chromium-profile/last_auth.json and survives
# process restarts, so a full day of 100+ gateway flaps triggers at most
# one real OTP SMS. Any NEEDS_OPERATOR_AUTH detection inside the cooldown
# window is routed through Section 3's quiet retry loop instead of
# typing the phone (which would burn operator OTP quota for nothing).
AUTH_COOLDOWN_S              = 10800   # 3h
```

Delete the entire `MAX_CONSECUTIVE_REAUTHS` block (the comment and the constant). Keep `IN_PLACE_POLL_S` below it unchanged.

- [ ] **Step 3.2: Rewrite the auth-timestamp helpers in browser.py to persist to disk**

In `booking_bot/browser.py`, find the state block around line 39. Replace the four helpers (`mark_auth_success`, `last_auth_age_s`, `note_rapid_reauth`, `reset_rapid_reauth_counter`, `rapid_reauth_count`) and their docstrings. The new code:

```python
# Section 1 of the survivability design: we persist the last successful
# auth timestamp to disk at .chromium-profile/last_auth.json so that an
# auto-restart (or a manual rerun) doesn't forget we just typed an OTP.
# Wall-clock UTC is stored — monotonic would reset on reboot and the
# only consumer is a cooldown check where wall-clock is exactly right.
import json
from datetime import datetime, timezone

_LAST_AUTH_FILENAME = "last_auth.json"


def _last_auth_path() -> "Path":
    """Disk location of the auth timestamp file. Lives inside the same
    persistent profile dir as the chrome cookies so `rm -rf .chromium-profile`
    wipes both in one shot. Resolved lazily because config.ROOT is set at
    module import time and tests monkeypatch it."""
    from pathlib import Path
    return Path(config.ROOT) / CHROMIUM_PROFILE_DIR_NAME / _LAST_AUTH_FILENAME


def mark_auth_success() -> None:
    """Record that the operator is currently authenticated. Called by
    auth.login_if_needed whenever it confirms a logged-in state (whether
    it typed credentials or found the session already alive). Writes
    atomically via <path>.tmp + os.replace so a crash mid-write can't
    produce a corrupt JSON that breaks subsequent runs."""
    import os
    path = _last_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"auth_at_utc": datetime.now(timezone.utc).isoformat()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def last_auth_age_s() -> float | None:
    """Seconds since the last mark_auth_success() call, or None if we've
    never authed or the file is missing/unreadable. A corrupt or
    future-dated file is treated as 'never authed' — better to prompt
    once for OTP than to trust garbage."""
    path = _last_auth_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        then = datetime.fromisoformat(payload["auth_at_utc"])
        now = datetime.now(timezone.utc)
        age = (now - then).total_seconds()
        if age < 0:
            # Clock skew or someone edited the file. Treat as stale.
            return None
        return age
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
```

Then DELETE these four functions entirely (they are no longer needed — Section 1's cooldown supersedes the rapid-reauth counter, and Section 3's quiet retry handles the "bot keeps seeing the login screen" pattern without incrementing anything):

```python
def note_rapid_reauth() -> int:
    ...

def reset_rapid_reauth_counter() -> None:
    ...

def rapid_reauth_count() -> int:
    ...
```

Also delete the module-level `_consecutive_rapid_reauths: int = 0` declaration around line 47 and its comment block.

- [ ] **Step 3.3: Rewrite auth.login_if_needed with the three-state return and cooldown branch**

In `booking_bot/auth.py`, replace the entire `login_if_needed` function (lines 48-134) with:

```python
def login_if_needed(
    frame: Frame, operator_phone: str, get_otp: Callable[[], str],
) -> str:
    """Bring the chat to a logged-in state, doing as little work as possible.

    Return values (Section 1 of the survivability design):

      "authed"          — session was already active, no work done.
      "authed_freshly"  — we just typed phone + OTP. The caller has marked
                          auth success on its way out, so the persistent
                          timestamp is already written.
      "cooldown_wait"   — detected NEEDS_OPERATOR_AUTH or NEEDS_OPERATOR_OTP
                          less than AUTH_COOLDOWN_S after the last successful
                          auth. Refused to type anything. The caller's quiet
                          retry loop should reload and re-poll without
                          triggering a fresh OTP SMS.

    The cooldown is what stops the OTP-flood pattern: every gateway flap
    used to trigger phone-number entry, and HPCL was firing ~50 SMS per
    real OTP. With the cooldown, a full day of 100+ flaps produces at most
    one real phone-number submission.

    Unlike full_auth, this does NOT walk any menu. Use it when the rest of
    the navigation is handled by a playbook."""
    import time
    from booking_bot import browser  # late import to avoid cycle

    state = _wait_for_known_state(frame)
    log.info(f"login_if_needed: detected state={state!r}")

    if state in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"):
        age = browser.last_auth_age_s()
        if age is not None and age < config.AUTH_COOLDOWN_S:
            log.warning(
                f"{state} detected {age:.0f}s after last successful auth "
                f"(cooldown = {config.AUTH_COOLDOWN_S}s) — refusing to type "
                f"operator phone to avoid OTP SMS flood. Caller should enter "
                f"quiet retry mode."
            )
            return "cooldown_wait"

    if state == "UNKNOWN":
        log.warning(
            "state is UNKNOWN — cannot tell if session is active. Proceeding "
            "anyway; the first playbook click will fail loudly if the page "
            "isn't in the expected state. Visible snapshot for debugging:"
        )
        log.warning(chat.dump_visible_state(frame))

    if state == "NEEDS_OPERATOR_AUTH":
        log.info(f"typing operator phone {operator_phone[:3]}XXXXXXX")
        chat.send_text(frame, operator_phone)
        chat.wait_until_settled(frame)
        otp = get_otp()
        log.info("typing OTP (not logged)")
        chat.send_text(frame, otp)
        chat.wait_until_settled(frame)
        browser.mark_auth_success()
        return "authed_freshly"
    elif state == "NEEDS_OPERATOR_OTP":
        otp = get_otp()
        log.info("typing OTP (not logged)")
        chat.send_text(frame, otp)
        chat.wait_until_settled(frame)
        browser.mark_auth_success()
        return "authed_freshly"
    else:
        log.info("session already active; skipping operator auth")
        browser.mark_auth_success()
        return "authed"
```

Also delete the now-unused `RestartableFatalError` import from the `from booking_bot.exceptions import (...)` block at the top of auth.py if it is no longer referenced anywhere else in the file.

- [ ] **Step 3.4: Update cli.py callers to handle "cooldown_wait" return**

In `booking_bot/cli.py`, find every `login_if_needed(frame, config.OPERATOR_PHONE, _prompt_otp)` call. There are two relevant ones:

1. Line ~450 inside `_run_session_attempt`, in the playbook startup path. Current:

```python
            login_if_needed(frame, config.OPERATOR_PHONE, _prompt_otp)
            last_err: Exception | None = None
```

Replace with:

```python
            auth_result = login_if_needed(frame, config.OPERATOR_PHONE, _prompt_otp)
            if auth_result == "cooldown_wait":
                # Cooldown said no — enter Section 3's quiet retry loop.
                # Task 4 replaces this placeholder with the real retry;
                # for now we raise RestartableFatalError so the outer
                # auto-restart handles it cleanly.
                raise RestartableFatalError(
                    "auth cooldown active at startup — session appears dead "
                    "but AUTH_COOLDOWN_S has not elapsed. Entering auto-restart."
                )
            last_err: Exception | None = None
```

2. The second call inside `_recover_with_playbook` at line 889. Current:

```python
    frame = browser.get_chat_frame(page)
    login_if_needed(frame, operator_phone, get_otp)
    try:
        playbook_mod.reset_to_customer_entry(frame, pb)
```

Replace with:

```python
    frame = browser.get_chat_frame(page)
    auth_result = login_if_needed(frame, operator_phone, get_otp)
    if auth_result == "cooldown_wait":
        # Cooldown said no — caller will surface this as a row failure
        # and the outer loop takes over (Task 4 wires the full quiet
        # retry into cli.main).
        raise RestartableFatalError(
            "auth cooldown active mid-recovery — refusing to type operator "
            "phone. Quiet retry will kick in at the outer loop."
        )
    try:
        playbook_mod.reset_to_customer_entry(frame, pb)
```

3. Remove the obsolete `browser.reset_rapid_reauth_counter()` call inside the main() auto-restart loop (around line 381) and inside the row-success branch (around line 598). Grep for `reset_rapid_reauth_counter` and delete every call. (The function is being removed in Step 3.2.)

- [ ] **Step 3.5: Delete the now-dead RECENT_AUTH constants and browser helper usage**

In `booking_bot/config.py`, delete these two constants and their comment block (around line 65):

```python
RECENT_AUTH_WINDOW_S  = 90
RECENT_AUTH_RECHECK_S = 15
```

The new AUTH_COOLDOWN_S supersedes both — the 90s window only existed to paper over "gateway flashes the login screen during a reload" and the cooldown now rejects that path outright without needing a recheck.

- [ ] **Step 3.6: Run the test suite — expect a few to break on import**

Run: `python -m pytest tests/ -q`

Expected: tests that import `note_rapid_reauth`, `reset_rapid_reauth_counter`, `rapid_reauth_count`, or `RECENT_AUTH_WINDOW_S` will fail with AttributeError/ImportError. Any other failure is a regression to investigate.

- [ ] **Step 3.7: Fix any test breakage from the symbol removals**

Grep for references and adjust:

```bash
grep -rn "note_rapid_reauth\|reset_rapid_reauth_counter\|rapid_reauth_count\|RECENT_AUTH_WINDOW_S\|RECENT_AUTH_RECHECK_S\|MAX_CONSECUTIVE_REAUTHS" tests/ booking_bot/
```

For each hit in `tests/`, delete the test (the behaviour is gone) or rewrite it against `last_auth_age_s`. For each hit in `booking_bot/` that isn't the removal itself, delete the call.

- [ ] **Step 3.8: Run full test suite**

Run: `python -m pytest tests/ -q`

Expected: all tests PASS.

- [ ] **Step 3.9: Commit**

```bash
git add booking_bot/config.py booking_bot/browser.py booking_bot/auth.py booking_bot/cli.py tests/
git commit -m "$(cat <<'EOF'
feat: persist auth timestamp + 3h cooldown (Section 1)

login_if_needed now refuses to type the operator phone within
AUTH_COOLDOWN_S of the last successful auth, returning "cooldown_wait"
for callers to route into quiet retry. Timestamp persists at
.chromium-profile/last_auth.json and survives process restarts.

Removes the rapid-reauth counter (superseded) and the RECENT_AUTH
recheck window (subsumed by the cooldown).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Quiet retry loop + shielded shutdown + restart budget (Section 3)

**Files:**
- Modify: `booking_bot/config.py` (MAX_AUTO_RESTARTS 5→200, add SESSION_DEAD_QUIET_RETRY_S, SHUTDOWN_GRACE_S)
- Modify: `booking_bot/cli.py` (`_quiet_retry` helper, shielded shutdown, plug Section 5)
- Modify: `booking_bot/browser.py` (expose a tiny helper to re-acquire frame after reload)

- [ ] **Step 4.1: Config bumps**

In `booking_bot/config.py`, change:

```python
MAX_AUTO_RESTARTS            = 5
AUTO_RESTART_WAIT_S          = 30
```

to:

```python
# 200 is far more than any single overnight batch should need — a bad
# HPCL night has historically produced 50+ restarts and the old cap of 5
# meant the bot stopped hours before the operator checked in. The only
# scenario where 200 isn't enough is a sustained hours-long outage, and
# in that case a higher cap wouldn't help anyway.
MAX_AUTO_RESTARTS            = 200
AUTO_RESTART_WAIT_S          = 30
# Section 3 of the survivability design: the quiet retry loop runs at most
# this long before declaring the session genuinely dead and handing off
# to Section 5's cleanup check. 30 min covers most HPCL flap windows
# without hiding a truly dead session for so long that the operator
# wakes up wondering why the batch stopped.
SESSION_DEAD_QUIET_RETRY_S   = 1800
# Grace window between the second Ctrl-C and the hard exit. ctx.close()
# needs a few seconds to flush the persistent chrome profile; killing
# Playwright mid-close leaves the async close task dangling and HPCL
# cookies in an undefined state. A third Ctrl-C inside this window
# hard-exits immediately for the operator who really needs out NOW.
SHUTDOWN_GRACE_S             = 10
```

- [ ] **Step 4.2: Add `_quiet_retry_until_alive_or_dead` to cli.py**

In `booking_bot/cli.py`, add this function above `_run_session_attempt`:

```python
def _quiet_retry_until_alive_or_dead(
    page,
    pb,
    store,
) -> str:
    """Section 3 of the survivability design. Enter a no-phone-number-typing
    loop that reloads the page every 60s and polls for a live chat state.
    Returns one of:

      "alive"       — state came back as READY_FOR_CUSTOMER / MAIN_MENU /
                      BOOK_FOR_OTHERS_MENU. Caller should resume the row
                      iteration. The returned frame is available via
                      page.main_frame.
      "drained"     — quiet retry deadline elapsed AND pending_rows() is
                      empty. Batch naturally completed during the wait.
                      Caller should exit 0 without alarming the operator.
      "needs_otp"   — quiet retry deadline elapsed AND pending_rows() is
                      non-empty. Fresh OTP would unblock real work.
                      Caller should fire the idle alarm and prompt for OTP.

    Crucially: this function NEVER calls login_if_needed. It never types
    the operator phone. Zero OTP SMS are triggered during quiet retry.
    That is the single behavioural difference from the old recovery path
    and the only reason the 3-hour cooldown is safe.
    """
    log.warning(
        f"quiet retry mode: reloading every 60s for up to "
        f"{config.SESSION_DEAD_QUIET_RETRY_S}s — NO phone/OTP typing. "
        f"Triggered because auth cooldown is still active."
    )
    deadline = time.monotonic() + config.SESSION_DEAD_QUIET_RETRY_S
    reload_interval_s = 60.0
    alive_states = ("READY_FOR_CUSTOMER", "MAIN_MENU", "BOOK_FOR_OTHERS_MENU")

    while time.monotonic() < deadline:
        try:
            browser.reset_gateway_flag()
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
            frame = browser.get_chat_frame(page)
            state = chat.detect_state(frame)
            log.info(f"quiet retry: state={state!r}")
            if state in alive_states:
                log.info("quiet retry: session alive — resuming")
                return "alive"
        except Exception as e:
            log.warning(
                f"quiet retry tick failed: {type(e).__name__}: {e} — "
                f"continuing to wait"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(reload_interval_s, remaining))

    log.warning(
        f"quiet retry: {config.SESSION_DEAD_QUIET_RETRY_S}s elapsed without "
        f"session recovery — running session-dead cleanup"
    )
    has_retriable, pending_count = _session_dead_cleanup_has_retriable_rows(store)
    if not has_retriable:
        log.info(
            "quiet retry: session dead BUT pending_rows() is empty — "
            "batch drained during the wait. Exiting cleanly with no "
            "operator alarm."
        )
        return "drained"
    log.error(
        f"SESSION DEAD — OPERATOR OTP REQUIRED — {pending_count} retriable "
        f"row(s) remain. Leaving browser open so the operator can paste "
        f"a fresh OTP once they arrive."
    )
    return "needs_otp"
```

- [ ] **Step 4.3: Replace the RestartableFatalError stub in the playbook startup path**

In `booking_bot/cli.py`, find the block added in Step 3.4:

```python
            auth_result = login_if_needed(frame, config.OPERATOR_PHONE, _prompt_otp)
            if auth_result == "cooldown_wait":
                raise RestartableFatalError(
                    "auth cooldown active at startup — session appears dead "
                    "but AUTH_COOLDOWN_S has not elapsed. Entering auto-restart."
                )
```

Replace the body of the `if` with a real quiet retry:

```python
            auth_result = login_if_needed(frame, config.OPERATOR_PHONE, _prompt_otp)
            if auth_result == "cooldown_wait":
                outcome = _quiet_retry_until_alive_or_dead(page, pb, store)
                if outcome == "drained":
                    log.info("batch drained during quiet retry; exiting _run_session_attempt")
                    return
                if outcome == "needs_otp":
                    # Operator presence required. Clear the cooldown file
                    # so login_if_needed will accept the next phone
                    # submission, then call it in a fresh cycle.
                    browser.clear_auth_cooldown()
                    frame = browser.get_chat_frame(page)
                    auth_result = login_if_needed(
                        frame, config.OPERATOR_PHONE, _prompt_otp,
                    )
                    if auth_result == "cooldown_wait":
                        raise RestartableFatalError(
                            "cooldown_wait persisted even after clear — "
                            "bug in Section 1 state file"
                        )
                else:  # "alive"
                    frame = browser.get_chat_frame(page)
```

- [ ] **Step 4.4: Add browser.clear_auth_cooldown helper**

In `booking_bot/browser.py`, below `last_auth_age_s()`, add:

```python
def clear_auth_cooldown() -> None:
    """Delete the persisted auth timestamp so the next login_if_needed call
    will accept a phone/OTP submission. Called ONLY from the Section 5
    session-dead path where the operator has explicitly been alarmed and
    the bot needs to accept a fresh OTP even though less than
    AUTH_COOLDOWN_S has elapsed since the last successful auth."""
    path = _last_auth_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"clear_auth_cooldown: could not delete {path}: {e}")
```

- [ ] **Step 4.5: Also plug the quiet retry into `_recover_with_playbook`**

In `booking_bot/cli.py`, find the block from Step 3.4 inside `_recover_with_playbook`:

```python
    auth_result = login_if_needed(frame, operator_phone, get_otp)
    if auth_result == "cooldown_wait":
        raise RestartableFatalError(
            "auth cooldown active mid-recovery — refusing to type operator "
            "phone. Quiet retry will kick in at the outer loop."
        )
```

Leave this one as-is — `_recover_with_playbook` is called during per-row recovery and surfacing the cooldown as RestartableFatalError is the right response there (the outer main loop's auto-restart picks it up and re-enters with a fresh process, which will hit the Step 4.3 quiet retry on startup). That avoids plumbing the store/page into the recovery path.

No code change in this step; just a reasoning note for the next maintainer.

- [ ] **Step 4.6: Shielded double-Ctrl-C shutdown**

In `booking_bot/cli.py`, replace `_install_signal_handler` (lines 198-211) with:

```python
# Section 3 of the survivability design. The old handler restored the
# default SIGINT handler on first Ctrl-C, so a rapid double-tap raised
# KeyboardInterrupt mid-ctx.close() and left Playwright's async close
# task dangling (observed in the 02:47:20 log: "Task was destroyed but
# it is pending!" + "Connection closed while reading from the driver").
#
# The new handler:
#   1st Ctrl-C — set _should_stop, keep the handler installed
#   2nd Ctrl-C — set _force_shutdown, let ctx.close() get its grace
#                window (SHUTDOWN_GRACE_S) before the finally: clause
#                os._exit's out with 130
#   3rd Ctrl-C — operator really wants out; hard os._exit immediately
_should_stop = False
_ctrl_c_count = 0
_force_shutdown = False


def _install_signal_handler() -> None:
    def _h(signum, frame):
        global _should_stop, _ctrl_c_count, _force_shutdown
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            log.warning(
                f"received signal {signum}; finishing current row then "
                f"stopping. Press Ctrl-C again for shielded shutdown "
                f"(waits {config.SHUTDOWN_GRACE_S}s for cookie flush). "
                f"A third Ctrl-C hard-exits."
            )
            _should_stop = True
        elif _ctrl_c_count == 2:
            log.warning(
                f"received second Ctrl-C; entering shielded shutdown "
                f"({config.SHUTDOWN_GRACE_S}s grace window for "
                f"ctx.close()). Third Ctrl-C will hard-exit immediately."
            )
            _force_shutdown = True
        else:
            log.error("third Ctrl-C; hard-exiting")
            import os
            os._exit(130)
    signal.signal(signal.SIGINT, _h)
```

- [ ] **Step 4.7: Honour _force_shutdown in the finally block**

In `booking_bot/cli.py`, find the finally block in `_run_session_attempt` (around line 788) that closes ctx/browser_obj/pw. Wrap the close calls so a running `_force_shutdown` caps them at SHUTDOWN_GRACE_S using a background thread.

Current:

```python
    finally:
        # Close the context first (persistent mode) so cookies are flushed
        # to .chrome-profile/, then close the legacy Browser handle if
        # present (non-persistent mode), then stop Playwright.
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        if browser_obj is not None:
            try:
                browser_obj.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
```

Replace with:

```python
    finally:
        _shutdown_browser_shielded(ctx, browser_obj, pw)
```

And add the helper above `_run_session_attempt`:

```python
def _shutdown_browser_shielded(ctx, browser_obj, pw) -> None:
    """Close Playwright handles with a bounded grace window. If the
    operator has hit Ctrl-C twice (_force_shutdown True) we run the
    close in a daemon thread and os._exit after SHUTDOWN_GRACE_S so a
    wedged Playwright doesn't hold the terminal hostage.

    The grace window only triggers on _force_shutdown. Clean exits still
    get unbounded time to close cookies (which is what we want — the
    whole point of the persistent profile is that cookies survive)."""
    import os
    import threading

    def _do_close():
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        if browser_obj is not None:
            try:
                browser_obj.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    if not _force_shutdown:
        _do_close()
        return

    t = threading.Thread(target=_do_close, daemon=True)
    t.start()
    t.join(timeout=config.SHUTDOWN_GRACE_S)
    if t.is_alive():
        log.error(
            f"browser shutdown still running after {config.SHUTDOWN_GRACE_S}s "
            f"grace window — hard-exiting"
        )
        os._exit(130)
    os._exit(130)
```

- [ ] **Step 4.8: Run the full test suite**

Run: `python -m pytest tests/ -q`

Expected: all tests PASS. The retry loop is not unit-tested (it requires a live browser), but all imports and symbol changes from Task 3 must remain green.

- [ ] **Step 4.9: Commit**

```bash
git add booking_bot/config.py booking_bot/cli.py booking_bot/browser.py
git commit -m "$(cat <<'EOF'
feat: quiet retry + shielded shutdown + restart budget bump (Section 3)

- MAX_AUTO_RESTARTS: 5 -> 200 so overnight batches survive HPCL flaps.
- New _quiet_retry_until_alive_or_dead: reload-and-poll for 30 min
  without typing phone/OTP when auth cooldown is active. Returns
  "alive" / "drained" / "needs_otp".
- Session-dead cleanup (Section 5) wired in: on quiet retry timeout,
  if pending_rows() is empty, exit 0 silently; else alarm the operator
  exactly once.
- Shielded double-Ctrl-C: 1st = graceful stop, 2nd = 10s grace close,
  3rd = hard exit. Fixes the cookie-flush race that killed sessions.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review gate

After Task 4 is committed, grep for leftover dead symbols and re-run tests:

```bash
grep -rn "rapid_reauth\|RECENT_AUTH\|MAX_CONSECUTIVE_REAUTHS" booking_bot/ tests/
python -m pytest tests/ -q
```

Expected: zero grep hits, all tests pass.

If either fails, fix inline before claiming the plan done. Do NOT leave half-finished cleanup for the next session.

---

## What's explicitly deferred

- **Section 2 (session keepalive):** investigative, time-boxed, only useful if Section 1's cooldown turns out to be insufficient. Drop-in as a follow-up once a week of production data shows whether the cooldown alone is enough.
- **Multi-instance parallelism:** user deferred.
- **Remote OTP delivery:** user deferred.

---

## Notes for the executing session

- Every task is a clean commit. Partial rollback is `git revert <sha>`.
- Between tasks, re-run `python -m pytest tests/ -q`. Any regression is a bug in the current task, not a pre-existing issue.
- Do NOT push to `origin` without explicit user approval — this plan only covers local commits.
