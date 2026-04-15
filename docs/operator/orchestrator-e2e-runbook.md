# Orchestrator Manual E2E Runbook

Use this once per laptop per major change to confirm the orchestrator
stack works against real HPCL before you trust it with a production
batch. Allocate 20 minutes.

## Prerequisites

- Clean `.chromium-profile/` or a fresh auth seed (no stale cookies).
- A 20-row test xlsx at `tests/fixtures/orchestrator/e2e_20rows.xlsx`
  with real-looking but test-safe customer phones.
- Fresh operator OTP available (don't run this during the 20h cooldown).
- No other `booking_bot` or `orchestrator` processes running.

## Steps

1. **Pre-auth an auth seed.**
   Run: `python -m booking_bot.orchestrator auth --source E2E`
   A Chromium window opens. Log in with your operator phone and OTP.
   The window closes automatically after ~5s once auth completes.
   Expected: stdout prints `[orchestrator] auth seed ready: <path>`.

2. **Run a small parallel start.**
   Run:
   ```
   python -m booking_bot.orchestrator start \
     --source E2E \
     --input tests/fixtures/orchestrator/e2e_20rows.xlsx \
     --chunk-size 5
   ```
   Expected: four chunks spawn, the monitor attaches automatically,
   all four rows show `phase=starting` → `booking` within 15 seconds.
   No OTP prompts appear — the clones all inherit the seed's cookies.

3. **Exercise `k` (kill).**
   In the monitor input prompt, type `k E2E-002` and Enter.
   Expected: within 15 seconds, E2E-002 shows `phase=failed` (red) and
   the other three chunks keep running.

4. **Exercise `r` (restart).**
   Type `r E2E-002` and Enter.
   Expected: E2E-002 re-spawns with a new PID and resumes from wherever
   the chunk's Output xlsx left off (existing `pending_rows` logic
   handles this — no special wiring in the orchestrator).

5. **Exercise `q` (detach).**
   Type `q` and Enter.
   Expected: the monitor exits cleanly, you get your shell prompt
   back, and the four chunks keep running. Verify with
   `python -m booking_bot.orchestrator status --source E2E`.

6. **Re-attach from a new terminal.**
   Open a second terminal. Run:
   `python -m booking_bot.orchestrator monitor --source E2E`
   Expected: same chunks appear, same progress, same PIDs.

7. **Exercise `qq` (full stop).**
   Type `qq` and Enter.
   Expected: all four chunks exit within 10 seconds. Task Manager
   should show no stray chrome.exe or python.exe left from this run.

8. **Verify outputs.**
   Check `Output/E2E-001-E2E-001.xlsx` through
   `Output/E2E-004-E2E-004.xlsx` — each should have booking codes (or
   issue labels) written to col C for the rows that were processed
   before `qq`. This is the existing `ExcelStore` format tagged with
   the chunk's profile suffix.

## Pass/Fail Criteria

PASS if all 8 steps produced the expected behavior.

FAIL if any of these happened:
- An OTP prompt appeared after step 1 (auth seed wasn't cloned correctly)
- A chunk's Chromium window opened when `--headless` was in effect
- `qq` left chrome.exe orphaned after 10s
- Heartbeat files stopped updating while a chunk was still running (stall)
