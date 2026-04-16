# Multi-Operator Orchestrator — Operator Runbook

One-page reference for running the K-operator orchestrator.

## Pre-flight

- You have K HPCL operator phones enrolled (usually K=2 or 3).
- Your input file is under `Input/` (e.g. `Input/lalji-final-1604-52am.xlsx`).
- No other `orchestrator start` is currently running for the same source.

> **Important — HPCL per-account session limit.** HPCL allows ~1 active session per
> operator account. If you run the legacy path with `--instances N` (no
> `--operator-phones`) or clone a single operator across multiple parallel
> chunks, HPCL will invalidate all of those sessions within ~90 seconds of the
> second chunk connecting, every chunk will land on `NEEDS_OPERATOR_AUTH`, and
> the 30-min quiet-retry will drain without recovery. **Real parallelism
> requires K distinct HPCL operator phones** (one session per account). With
> one operator phone the orchestrator's only safe configuration is
> `--operator-phones <one-phone> --clones-per-operator 1` (i.e. sequential,
> same throughput as the single-bot path).

## Step 1 — Seed auth for all K operators (one OTP per operator)

```bash
python -m booking_bot.orchestrator auth \
    --source lalji \
    --operator-phones 9111111111,9222222222,9333333333
```

- K headed Chromium windows open **sequentially** (not parallel).
- Type the OTP when the HPCL login prompt appears in each window.
- Each window closes on its own when its login completes.
- Total time: ~60–90 seconds per operator.
- On success: `[orchestrator] auth seed op1 ready: ...` for each slot.

## Step 2 — Start the batch

```bash
python -m booking_bot.orchestrator start \
    --source lalji \
    --input Input/lalji-final-1604-52am.xlsx \
    --operator-phones 9111111111,9222222222,9333333333 \
    --clones-per-operator 3
```

Total parallelism = 3 × 3 = **9 bots**. The monitor attaches automatically.

## Step 3 — If HPCL kicks one operator mid-run

Monitor shows a red banner: `!! operator op2 NEEDS RE-AUTH (3 chunks waiting) !!`

Recovery:
1. Open a headed Chromium window pointed at `.chromium-profile-lalji-op2-auth-seed`.
   (Or one of op2's cloned profiles: `.chromium-profile-lalji-NNN` where NNN is one of op2's chunks.)
2. Navigate to HPCL, type OTP for operator 9222222222.
3. Within 3 seconds, op2's 3 bots detect the new `shared_auth-op2.json` and resume.
4. op1 and op3 are untouched — 6 bots keep going the whole time.

## Troubleshooting

- **`AuthSeedMissing`:** one or more slots' seeds are missing/stale. Run Step 1 again for the listed slots.
- **`seeded for X, passed Y` mismatch:** the phone list order changed between `auth` and `start`. Pass the phones in the same order, or re-run `auth` with the new order.
- **All 9 bots kicked simultaneously:** HPCL may have done an account-level session flush. Run Step 1 for all K operators again.
- **Every chunk immediately fails with "headless chunk: quiet retry exhausted while session is dead":** the auth-seed for that slot was invalidated server-side shortly after the chunks connected. This usually means >1 parallel chunk is sharing a single HPCL account (see the Pre-flight note above — use one operator phone per concurrent chunk). Re-auth the affected slot(s) in Step 1 and restart; if the failure is universal, reduce `--clones-per-operator` to 1.
