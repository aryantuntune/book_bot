# Overnight Batch Survivability — Design

**Date:** 2026-04-15
**Author:** Claude + operator
**Status:** Draft, pending user review

## Problem

The operator needs to run a 3,500-customer batch overnight without being woken for OTP prompts. Three failure modes are currently breaking that:

1. **OTP SMS flood.** Observed: the bot sent ~150 OTP SMS requests to HPCL in a single day, despite the operator only typing 3–4 real OTPs. Root cause: every time `auth.login_if_needed` detects `NEEDS_OPERATOR_AUTH` (which happens on every gateway flap, every reload that comes back mid-auth, and every classifier flicker), the bot types the operator phone number into chat. HPCL responds by firing a fresh OTP SMS. The operator runs out of daily OTP SMS quota. The in-memory recent-auth guard doesn't survive process restarts, so each auto-restart adds another ~1 SMS burst.

2. **Session death with no silent recovery.** Observed: HPCL had a 502 storm at 02:41. From 02:57 onward every bot restart showed `NEEDS_OPERATOR_AUTH` and re-prompted for OTP. Once HPCL's server-side session is invalidated (likely by an idle TTL or by 502 responses from the API endpoints the session cookie is tied to), no cookie-jar trickery on our side can recover it. The bot currently has no way to wait and see if the session resurrects on its own — it immediately retypes the phone and prompts for OTP, which is the wrong response for a transient flap.

3. **ISSUE rows are final on the first failure.** Observed: a file of ~400 rows ends with 15–25 "ISSUE" rows that a simple retry would likely resolve. Today, any unrecognised failure in `book_one` marks col C = "ISSUE" and `pending_rows()` never yields that row again. A single glitch permanently loses the row.

## Goals

- **Zero bot-triggered OTP SMS** during steady-state operation. The only OTP SMS the operator sees should be the one they deliberately request in the morning.
- **Auto-restart is unattended.** Process crashes, circuit-breaker trips, and gateway storms are recovered from without operator action.
- **ISSUE is the last resort, capped at 3 attempts per row.** Rows get 3 chances across any number of restarts.
- **Session-dead cleanup is smart.** When HPCL really has killed the session, don't wake the operator if there's nothing useful to do — only alarm if fresh-OTP would unblock real work.
- **No new workbook columns visible to the operator.** (One new column, `attempt_count`, is added but not part of their normal view.)

## Non-goals

- Multi-instance parallelism (explicitly deferred by user).
- Remote OTP delivery (SMS relay, Telegram bot, etc.) — deferred.
- Preserving HPCL sessions indefinitely beyond what HPCL's server-side TTL allows.

## Design

Five sections. Sections 1, 3, 4, 5 stand alone and ship together. Section 2 is investigative — if the ~1-hour time-box doesn't yield a working keepalive, it's dropped without blocking the rest.

### Section 1 — Stop the OTP flood at the source

**Rule:** the bot is allowed to type the operator phone number **at most once per 3-hour window**, and only when no other path forward exists. Every other `NEEDS_OPERATOR_AUTH` detection becomes a silent wait-and-retry.

**Changes:**

- Add `AUTH_COOLDOWN_S = 10800` (3 hours) to `config.py`.
- Persist `_last_auth_at_monotonic` to disk as wall-clock UTC in `.chromium-profile/last_auth.json`. `browser.mark_auth_success()` writes the file; `browser.last_auth_age_s()` reads it on demand and converts back to "seconds since last auth". Wall-clock is fine because the only consumer is a cooldown check (monotonic would reset on reboot).
- `auth.login_if_needed` currently returns `None`. Change its return type to `Literal["authed", "cooldown_wait", "authed_freshly"]`:
  - `"authed"` — session was already active, no work done.
  - `"authed_freshly"` — we just typed phone + OTP. Caller should `mark_auth_success` and continue.
  - `"cooldown_wait"` — we detected `NEEDS_OPERATOR_AUTH`/`NEEDS_OPERATOR_OTP` within the cooldown window and refused to type anything. Caller enters Section 3's quiet retry loop.
- Rewritten decision tree:
  - State is not an auth state → return `"authed"`.
  - State is an auth state AND `last_auth_age_s() < AUTH_COOLDOWN_S` → return `"cooldown_wait"` without typing anything.
  - State is `NEEDS_OPERATOR_AUTH` AND (`last_auth_age_s()` is `None` OR `>= AUTH_COOLDOWN_S`) → type phone, wait for OTP, return `"authed_freshly"`.
  - State is `NEEDS_OPERATOR_OTP` AND same cooldown condition as above → type OTP only, return `"authed_freshly"`.
- Delete the existing rapid-reauth counter logic (`note_rapid_reauth`, `rapid_reauth_count`, `reset_rapid_reauth_counter`, the `RestartableFatalError` branch in `login_if_needed`). It was a blast-radius cap that's superseded by the cooldown guard. The new Section 3 quiet retry is the blast-radius cap from here on.
- Remove `MAX_CONSECUTIVE_REAUTHS` from `config.py`.

**Net effect:** a full day of 100+ gateway flaps triggers at most **1** real phone-number submission (the morning one). Everything else is silent.

### Section 2 — Session keepalive (investigative, ~1 hour time-box)

**Hypothesis:** HPCL's server-side session has an idle TTL in the 10–15 minute range. When the bot goes quiet (waiting on operator, mid-Ctrl-C, paused for a reload), the TTL expires and subsequent requests return the login screen even though client-side cookies are valid.

**Investigation:**

1. During the first authenticated run, dump the names (not values) of every HPCL `localStorage` key and every `hpchatbot.hpcl.co.in` cookie to `logs/session_snapshot.json`. Look for obvious auth-token candidates (e.g. `session_id`, `token`, `jwt`, `auth_*`).
2. Add a debug command to print which keys change across a successful auth — the delta is the session state.
3. Once identified, add a **lightweight keepalive**: during idle periods in the main loop (no active row for > 120s), run a no-op `frame.evaluate("() => document.title")`. If that doesn't reset the TTL, try hitting a cheap HPCL endpoint (e.g. the loader image URL) via `fetch()` inside the page context.

**Time-box:** if after ~1 hour of work the keepalive doesn't measurably extend session life, drop Section 2 entirely and rely on Section 3's recovery. This section is a bonus, not a prerequisite.

**Deliverables (if kept):**
- `KEEPALIVE_INTERVAL_S = 120` in config.py
- `browser.keepalive_tick(page)` helper
- A call site in `cli.py`'s main loop that ticks the keepalive between rows when idle.

### Section 3 — Solid auto-restart with quiet retry

**Changes:**

- `MAX_AUTO_RESTARTS`: 5 → **200**. Overnight batches can genuinely hit 50+ restarts on a bad HPCL day; 5 is far too low.
- Add `SESSION_DEAD_QUIET_RETRY_S = 1800` (30 min) to config.py.
- New state in `cli.main`: **quiet retry mode**. When `auth.login_if_needed` returns `"cooldown_wait"` (see Section 1), the bot enters a loop that:
  - Reloads the page every 60 seconds.
  - Calls `chat.detect_state` after each reload.
  - If state becomes `READY_FOR_CUSTOMER`, `MAIN_MENU`, or `BOOK_FOR_OTHERS_MENU` → HPCL's flap resolved, exit quiet retry, resume the batch.
  - If state is still `NEEDS_OPERATOR_AUTH` / `NEEDS_OPERATOR_OTP` after `SESSION_DEAD_QUIET_RETRY_S` elapsed → exit quiet retry and trigger the session-dead cleanup check (Section 5).
- **Zero phone-number submissions during quiet retry.** No SMS is triggered. This is the key behavioural change from today's recovery path.
- **Fix the double-Ctrl-C cookie-flush race.** Today, second Ctrl-C restores the default SIGINT handler, so a rapid double-tap raises `KeyboardInterrupt` mid-`ctx.close()` and leaves Playwright's async close task dangling (observed in the 02:47:20 log: `Task was destroyed but it is pending!` + `Connection closed while reading from the driver`). Replace with a **shielded shutdown**: the second Ctrl-C no longer reraises immediately. Instead it arms a 10-second grace timer — during that window, `ctx.close()` + `pw.stop()` get to complete. Only after the grace window does the process exit with `os._exit(130)`. A third Ctrl-C within the grace window hard-exits immediately.

**Files touched:** `config.py`, `cli.py`, `browser.py`.

### Section 4 — Per-row attempt budget (3 strikes → ISSUE)

**Data model change:**

Add `attempt_count` as a new column (col D) to the Output workbook. Default 0 for newly-written rows, backwards-compatible with existing output files (read as 0 when col D is empty or missing).

**Behaviour change in `cli.book_row` / `excel.py`:**

When `book_one` returns an Issue:

```
attempt_count += 1
write attempt_count to col D
if attempt_count < 3:
    leave col C empty (row stays "pending")
else:
    write col C = "ISSUE" (row is locked, never retried again)
```

Successful outcomes (`Success`, ekyc, not_registered, payment_pending) finalize col C immediately regardless of attempt_count.

`ExcelStore.pending_rows()` needs no change — it already yields rows where col C is empty. The attempt_count mechanism piggybacks on that.

**New helpers:**

- `ExcelStore.get_attempt_count(row_idx) -> int`
- `ExcelStore.increment_attempt_count(row_idx) -> int` (returns the new count)
- `ExcelStore.write_issue()` stays for the final-locking case; new callers go through the attempt-count path first.

### Section 5 — Session-dead cleanup check

**When:** Section 3's 30-min quiet retry expires with the session still dead. Before firing the alarm and prompting the operator, we check whether prompting would actually help.

**The invariant that makes this simple:** rows with `attempt_count == 3` are always locked with col C = "ISSUE" (Section 4). So any row that's still in `pending_rows()` (col C empty) has `attempt_count < 3` — i.e. it's retriable by definition. "Pending but not retriable" is a state that doesn't exist.

**The check:**

```
pending = list(excel.pending_rows())

if not pending:
    # Batch naturally drained to zero during the retry loop.
    # Nothing a fresh OTP could help with.
    log "batch complete — no rows remain"
    finalize and exit 0
else:
    # Every pending row is retriable (attempt_count < 3, by the
    # Section 4 invariant). Fresh OTP would unblock real work.
    fire the loud idle alarm (existing feature)
    leave the browser window open
    log "SESSION DEAD — OPERATOR OTP REQUIRED — N retriable rows remain"
    prompt operator for OTP via GUI/stdin
    on OTP receipt: type phone, type OTP, mark_auth_success, resume
```

**Example:** file of 400. Main pass ends with 360 Success, 15 terminal rejects, 25 pending (all `attempt_count == 1`). Main loop restarts the pending iteration. Second pass resolves 10, leaving 15 pending (`attempt_count == 2`). Third pass resolves 5, and now the 10 that are still failing get col C = "ISSUE" (since `attempt_count` just hit 3). `pending_rows()` returns empty. Main loop exits normally. Final state: 375 Success, 15 terminal, 10 ISSUE. Operator interaction: the single morning OTP. Zero alarms, zero wake-ups.

**If HPCL kills the session during the retry loop:** quiet retry runs for 30 min. On expiry, check `pending`. Either the batch drained (exit clean) or rows remain (alarm + prompt). There's no middle case because of the Section 4 invariant above.

**Example:** file of 400. Main pass: 360 Success, 15 terminal rejects, 25 ISSUE (all attempt_count == 1). Main loop reaches end of `pending_rows()` but the 25 ISSUE rows are still pending (col C empty). Loop starts over, retries them. After second pass: 10 more resolved, 15 still ISSUE (attempt_count == 2). Third pass: 5 more resolved, 10 remain (attempt_count == 3) — NOW col C is set to ISSUE and they stop appearing in `pending_rows()`. Main loop exits because `pending_rows()` is empty. Final state: 375 Success, 15 terminal, 10 genuine ISSUE, no operator interaction needed.

**If mid-retry-loop HPCL kills the session:**
- Quiet retry runs for 30 min.
- If HPCL comes back → resume.
- If not → check retriable. If there are still retriable rows (attempt_count < 3), alarm the operator. If everything left is locked — no, that can't happen because locked rows are col C = ISSUE and don't appear in pending_rows(). So alarm fires whenever session death blocks progress, and is silent only when the batch already drained.

## Data model

New column in the Output workbook:

| col | name           | type | default | meaning                                      |
|-----|----------------|------|---------|----------------------------------------------|
| A   | phone          | str  | —       | customer phone (from Input)                  |
| B   | code/raw       | str  | —       | confirmation code or raw response            |
| C   | outcome        | str  | ""      | Success / terminal tag / ISSUE / empty       |
| D   | attempt_count  | int  | 0       | incremented on each book_one Issue result    |

Backwards compatibility: when reading an existing Output workbook, missing col D is treated as `attempt_count = 0`.

New on-disk state:

- `.chromium-profile/last_auth.json` — `{"auth_at_utc": "2026-04-15T03:06:43Z"}`. Written on every successful auth, read on every `login_if_needed` call.
- `logs/session_snapshot.json` — debug dump of HPCL auth state keys (Section 2 only).

## Config changes

```python
# REMOVED
MAX_CONSECUTIVE_REAUTHS = 3         # superseded by AUTH_COOLDOWN_S

# ADDED
AUTH_COOLDOWN_S               = 10800    # 3h — no auto phone-type inside this window
SESSION_DEAD_QUIET_RETRY_S    = 1800     # 30min — quiet retry before alarming
MAX_ATTEMPTS_PER_ROW          = 3        # hard cap before col C = ISSUE
KEEPALIVE_INTERVAL_S          = 120      # Section 2 only, if kept

# CHANGED
MAX_AUTO_RESTARTS             = 5 → 200
```

## Testing strategy

- Unit tests for `ExcelStore.get_attempt_count`, `increment_attempt_count`, backwards-compat reading (missing col D).
- Unit test for the cooldown guard: mock `last_auth_age_s` return values, assert `login_if_needed` returns `"cooldown_wait"` vs types the phone vs prompts for OTP per the three branches.
- Unit test for the session-dead cleanup check logic (isolate the decision, not the Playwright interactions).
- Integration test: drive `book_one` to return an Issue 3 times against a mocked frame, assert col C transitions empty → empty → empty → "ISSUE" and col D increments 1 → 2 → 3.
- **Not tested in the suite:** the actual quiet-retry loop against HPCL (requires live network). Manual smoke test during development.

## Risks and rollback

- **Section 1 cooldown is too aggressive:** if `AUTH_COOLDOWN_S = 10800` is set too high and HPCL's real session dies in under 3 hours, the bot will quietly wait through the whole cooldown window before allowing a re-auth. Mitigation: Section 3's quiet retry is the fast path — it'll re-enter the logged-in state if HPCL heals. If HPCL really dies, Section 5 fires the alarm after 30 min, not 3 hours. Cooldown only blocks the phone-type, not the alarm.
- **attempt_count breaks existing Output workbooks:** mitigation is the backwards-compat "missing col D = 0" read path.
- **Shielded shutdown hangs on a genuinely broken Playwright:** mitigation is the 10s grace window + `os._exit(130)` fallback.
- **Rollback plan:** every section is gated on a config constant. Setting `AUTH_COOLDOWN_S = 0` disables the cooldown. Setting `MAX_ATTEMPTS_PER_ROW = 1` restores single-shot-and-ISSUE behaviour. Setting `MAX_AUTO_RESTARTS = 5` restores the old restart cap.

## Open questions

None — design is fully specified.

## Implementation order

1. Section 4 (per-row attempt budget) — self-contained, testable, unlocks Section 5.
2. Section 5 (session-dead cleanup check) — builds on Section 4's attempt_count.
3. Section 1 (persistent auth timestamp + cooldown) — self-contained, biggest win.
4. Section 3 (quiet retry + shielded shutdown + MAX_AUTO_RESTARTS bump) — builds on Section 1's `"cooldown_wait"` return and Section 5's cleanup check.
5. Section 2 (keepalive investigation) — last, time-boxed, droppable.

After each section: run `pytest tests/ -q`, commit, push. Each section is a clean commit so partial rollback is easy.
