# Multi-Operator Orchestrator Design

**Date:** 2026-04-16
**Status:** Approved for implementation
**Motivation:** Tonight's 3,394-row lalji batch needs ~9 concurrent bots to finish in ~3 hours. Observed HPCL behavior limits a single operator account to ~3 concurrent sessions before server-side mass-kick. This design scales horizontally across K independent operator phone accounts, each hosting M (default 3) cloned sessions, giving K*M total parallelism while staying inside HPCL's per-account ceiling.

## Goal

Enable `python -m booking_bot.orchestrator start` to spawn **K × M parallel bot instances** backed by **K distinct HPCL operator phone accounts**, where each account owns at most M concurrent sessions. All existing single-operator orchestration semantics (chunk splitting, heartbeats, monitor, quiet-retry) continue to work; the multi-operator surface is additive.

**Tonight's target configuration:** K=3 operators × M=3 clones = 9 bots → 3,394 rows in ~3.3 hours at historical 1.9 rows/min per bot.

## Non-goals (explicit)

- **M > 3 clones per operator.** Unproven against HPCL's per-account limits. Keep M∈{1,2,3} for now.
- **Parallel OTP browser windows during `auth`.** Sequential is simpler; operator cost is ~90s total vs ~30s parallel.
- **Automatic failover between operators.** If operator X dies mid-run, its M bots quiet-retry until operator re-auths X manually. We do not move X's chunks to operator Y. Tonight's run is 3 hours and re-auth is a 60-second human action; dynamic rebalancing is not worth the complexity.
- **Changes to the booking flow, playbook, or chat state machine.** This is purely an orchestration-layer change.
- **Changes to orchestrator-less bare-bot mode** (`python -m booking_bot <input>`). That path runs unchanged; env-var overrides are opt-in and default to today's behavior.

## Architecture

Today the orchestrator has one operator dimension collapsed into each source:
- One `.chromium-profile-<source>-auth-seed` directory
- One `shared_auth.json` at repo root
- Every chunk's profile is a clone of the same seed
- Every bot reads/writes the same shared_auth.json

This design promotes **operator slot** (`op1`, `op2`, ..., `opK`) to a first-class dimension visible in:
- Auth-seed profile paths
- ChunkSpec fields
- Spawner env vars
- `_shared_auth_path()` in browser.py
- Heartbeat records (for monitor display)

### Key components

**1. Per-operator auth-seed profiles.**
`.chromium-profile-<source>-<slot>-auth-seed` where slot ∈ {op1, op2, ..., opK}. One interactive OTP per slot seeds its profile. Slots are assigned in the order operator phones were passed to the CLI.

**2. Chunk-to-operator bucketing.**
Splitter produces K×M contiguous chunks (not just M). Assignment is contiguous, not interleaved:
- Chunks 1..M → `op1`
- Chunks (M+1)..(2M) → `op2`
- ...
- Chunks ((K-1)M+1)..(KM) → `opK`

Each `ChunkSpec` carries two new fields: `operator_slot: str` and `operator_phone: str`. Chunks are still uniquely identified by per-chunk `chunk_id` (`<source>-001`, `-002`, ...). Each bot still gets its own browser profile dir cloned from *its operator's* seed.

**3. Per-operator `shared_auth.json`.**
`browser._shared_auth_path()` becomes slot-aware: reads env var `BOOKING_BOT_OPERATOR_SLOT`. When set, returns `config.ROOT / f"shared_auth-{slot}.json"`. When unset (orchestrator-less mode), returns legacy `config.ROOT / "shared_auth.json"`. A single file per operator slot means operator A's re-auth only touches `shared_auth-op1.json`; operator B's 3 bots continue untouched.

**4. Per-process `OPERATOR_PHONE` override.**
On bot import/startup, `config.py` checks env var `BOOKING_BOT_OPERATOR_PHONE`. If set, `config.OPERATOR_PHONE` is overwritten to that value before any auth call. Each bot thus has the correct phone to type if HPCL kicks its session and the operator does a manual re-OTP via the quiet-retry shared-auth mechanism.

**5. `_auth_fresh` bug fix (prerequisite).**
Today `auth_template._auth_fresh()` reads `data["timestamp"]` as float, but `browser.py` writes `{"auth_at_utc": <ISO-8601 string>}`. The KeyError is silently swallowed and returns False, so Path B (copy fresh main profile to seed) always fails and Path C (interactive OTP) fires even when fresh cookies exist on disk. Fix: parse `auth_at_utc` with `datetime.fromisoformat`, compute age against `datetime.now(timezone.utc)`. Keep the same return contract. This fix is a prerequisite — without it, the whole multi-operator feature falls through to interactive OTP immediately on every startup.

## Operator UX

Three concrete touchpoints:

### Step 1: `auth` (one time per batch)

```bash
python -m booking_bot.orchestrator auth \
    --source lalji \
    --operator-phones 9209114429,9xxxxxxxxx,9yyyyyyyyy
```

Behavior:
- Validates source name and phone-count.
- For each phone in order:
  1. Compute slot label `op1`, `op2`, ..., `opK`.
  2. Check if `_seed_path(source, slot)` exists with a fresh `last_auth.json` — if yes, skip this slot (log "slot op1: already fresh").
  3. Check if `.chromium-profile` (main profile) has a fresh `last_auth.json` — if yes AND this is slot `op1`, copy main profile to `op1` seed, scrub locks, continue. (This is the existing Path B fast path, now fixed by the `_auth_fresh` bug fix above.) For slots op2+, always go to the interactive path.
  4. Launch a headed Chromium against the slot's seed dir. Print "[auth_template] Auth seed op2: log in to HPCL as operator 9xxxxxxxxx." Poll `last_auth.json`, close window on success or raise `AuthSeedTimeout` after `ORCHESTRATOR_AUTH_TIMEOUT_S`.
- On success, prints "[orchestrator] 3 auth seeds ready".

### Step 2: `start`

```bash
python -m booking_bot.orchestrator start \
    --source lalji \
    --input Input/lalji-final-1604-52am.xlsx \
    --operator-phones 9209114429,9xxxxxxxxx,9yyyyyyyyy \
    --clones-per-operator 3
```

Behavior:
- Acquires source lock.
- Splits input into K*M contiguous chunks (9 chunks for K=3, M=3). Chunk size computed from total rows and K*M.
- Verifies all K auth seeds exist and are fresh. If any are missing/stale, aborts with a clear error pointing at `auth` subcommand.
- For each chunk: clones its operator's seed to the chunk's profile dir (`.chromium-profile-<chunk_id>`), scrubs lock files.
- Spawns K*M headless bot processes, each with env vars:
  - `BOOKING_BOT_HEARTBEAT_PATH` (existing)
  - `BOOKING_BOT_SOURCE` (existing)
  - `BOOKING_BOT_CHUNK_ID` (existing)
  - `BOOKING_BOT_OPERATOR_SLOT` (NEW)
  - `BOOKING_BOT_OPERATOR_PHONE` (NEW)
- Gentle 0.5s stagger between spawns (existing).
- Hands off to monitor.

### Step 3: Mid-run recovery (only if HPCL kicks an operator)

If operator X's session is killed server-side, all M of X's bots land in NEEDS_OPERATOR_AUTH → quiet-retry mode (watching `shared_auth-opX.json` for updates). The monitor shows M stuck chunks all with `operator_slot=opX`.

Recovery: operator opens one of opX's seed profiles in a headed window (CLI helper command or manual Chromium launch with `--user-data-dir`), types the OTP. That bot's auth.py writes `shared_auth-opX.json`. Within 3 seconds the other M-1 bots under opX detect the newer file, inject the cookies, reload, resume. Operators opY and opZ are untouched the entire time.

**No cross-operator failover.** If operator X never comes back, X's chunks never complete; operator reruns with a new `auth` call afterwards to finish them.

## File-by-file changes

### `booking_bot/config.py`
- Add `OPERATOR_PHONE_ENV = "BOOKING_BOT_OPERATOR_PHONE"` and `OPERATOR_SLOT_ENV = "BOOKING_BOT_OPERATOR_SLOT"` constants.
- At module import, if `os.environ.get("BOOKING_BOT_OPERATOR_PHONE")` is set and non-empty, overwrite `OPERATOR_PHONE` to that value. Log the override once at import time.
- No other changes to OPERATOR_PHONE default (`"9209114429"`).

### `booking_bot/browser.py`
- `_shared_auth_path()` reads `os.environ.get(config.OPERATOR_SLOT_ENV)`.
  - If set to a non-empty string matching `^op\d+$`, returns `Path(config.ROOT) / f"shared_auth-{slot}.json"`.
  - Otherwise, returns `Path(config.ROOT) / config.SHARED_AUTH_FILENAME` (today's behavior).
- No other shared_auth changes. `read_shared_auth_state`, `write_shared_auth_state`, `inject_shared_auth_cookies` all go through `_shared_auth_path()` and automatically become per-slot.
- `last_auth.json` path is already per-profile-dir, so no change needed there — each cloned chunk's `.chromium-profile-<chunk_id>/last_auth.json` is already isolated.

### `booking_bot/orchestrator/auth_template.py`
- **Bug fix:** `_auth_fresh()` parses `data["auth_at_utc"]` with `datetime.fromisoformat`, falling back to return False on KeyError / ValueError / TypeError. Replace the `float(data["timestamp"])` line entirely. Use `datetime.now(timezone.utc)` for age computation.
- `_seed_path(source)` → `_seed_path(source, slot)` returning `config.ROOT / f".chromium-profile-{source}-{slot}-auth-seed"`. Single-slot callers pass `slot="op1"`.
- New function `ensure_auth_seeds(source, operator_phones: list[str]) -> dict[str, Path]`:
  - Returns a dict mapping slot → seed path.
  - For each phone, computes slot (`op1`..`opK`), checks Path A (existing fresh seed), Path B (copy from main profile, op1 only), Path C (interactive).
  - Path B only triggers for `op1`, since the main `.chromium-profile` is conceptually "the operator's primary profile" and is tied to `config.OPERATOR_PHONE` (which may or may not match phone 1 — we don't verify, but Path B is best-effort cache).
  - Raises `AuthSeedTimeout` on any slot's interactive timeout; message indicates which slot failed.
  - Legacy `ensure_auth_seed(source, operator_phone=None)` is kept as a thin wrapper calling `ensure_auth_seeds(source, [operator_phone or config.OPERATOR_PHONE])` and returning the single-path result, for backwards compat in tests.
- `clone_to_chunks(source, chunks)` — per chunk, look up `chunk.operator_slot`, use `_seed_path(source, chunk.operator_slot)` as the source. Same failure aggregation semantics.

### `booking_bot/orchestrator/splitter.py`
- `ChunkSpec` gains two fields: `operator_slot: str` and `operator_phone: str`.
- `split(...)` signature gains `operator_phones: list[str] | None = None` and `clones_per_operator: int | None = None`. When both are None, fall back to single-operator behavior (current behavior, slot="op1", phone=config.OPERATOR_PHONE).
- When provided:
  - `K = len(operator_phones)`, `M = clones_per_operator`, `n_chunks = K * M`.
  - Row ranges: equal contiguous split, same as today's `_resolve_parallelism(total_rows, num_chunks=K*M)`.
  - Chunk i (1-based) is assigned `operator_slot=f"op{((i-1)//M) + 1}"` and `operator_phone=operator_phones[((i-1)//M)]`.
  - Validation: M >= 1, M <= 3 (hard cap, raise ValueError on M > 3 with "per-account session limit"), K >= 1, K <= 10 (sanity cap).
- `chunk_id` format unchanged (`<source>-<zero-padded-index>`). Pad width is `max(3, len(str(n_chunks)))` as today.

### `booking_bot/orchestrator/cli.py`
- `start` subcommand new args:
  - `--operator-phones` (comma-separated string, parsed to list).
  - `--clones-per-operator` (int, default 3).
  - Keeps existing `--chunk-size` / `--instances` mutual-exclusion group. If `--operator-phones` is passed, `--chunk-size` and `--instances` are ignored (with a warning) — the parallelism is K*M, fully determined by the operator phones list.
- `auth` subcommand:
  - `--operator-phones` (comma-separated list, required). Replaces today's single `--operator-phone` — keep the singular form as a backwards-compat alias that maps to a 1-element list.
- Parse comma-separated phones with basic validation: 10 digits each, unique, 1..10 items.
- `run_start` plumbs `operator_phones` list into `splitter.split()` and `auth_template.clone_to_chunks()`.
- The auth subcommand handler calls `auth_template.ensure_auth_seeds(source, phones)` and prints each seed path on success.

### `booking_bot/orchestrator/spawner.py`
- `spawn_chunk()` adds two env vars to `env`:
  - `BOOKING_BOT_OPERATOR_SLOT = spec.operator_slot`
  - `BOOKING_BOT_OPERATOR_PHONE = spec.operator_phone`
- No other changes.

### `booking_bot/orchestrator/heartbeat.py`
- Add field `operator_slot: str | None = None` to `Heartbeat` dataclass (**optional**, with default None, because `_REQUIRED_FIELDS` is computed from dataclass fields and we cannot require this in existing heartbeat files on disk).
- `read()` is tolerant because `_REQUIRED_FIELDS` uses `issubset`; adding an optional field doesn't break old files. Verify: `Heartbeat(**data)` with data missing `operator_slot` will fail unless we give it a default. Give it a default.
- No API change needed — just the new optional field.

### `booking_bot/orchestrator/monitor.py`
- Render-once output shows `operator_slot` in a new column (or appended to chunk_id in compact modes).
- When >1 chunks in a single `operator_slot` are stuck in NEEDS_OPERATOR_AUTH phase, render a high-visibility line: `"!! operator op1 NEEDS RE-AUTH (3 chunks waiting) !!"`.
- Stuck detection: treat "phase=authenticating and last_activity_at older than 60s" OR "last_error contains 'cooldown_wait'/'NEEDS_OPERATOR_AUTH'" as the stuck signal. (Use whichever signals are already available in the heartbeat.)

### `booking_bot/orchestrator/spawner.py` (tests already expect env contents)
Update `test_orchestrator_spawner.py` to check new env vars.

## Testing strategy

Tests live under `tests/` with existing naming (`test_orchestrator_*.py`). All tests must pass; no existing tests should regress.

1. **`test_auth_template_fresh.py`** (new): `_auth_fresh` against:
   - Fresh `{"auth_at_utc": <now ISO>}` → True
   - Stale `{"auth_at_utc": <20h ago ISO>}` → False (with `max_age_s=72000`)
   - Legacy `{"timestamp": 123.456}` → False (no regression on malformed key, but the new format is the only valid one)
   - Missing file → False
   - Corrupt JSON → False
   - Regression: old test `test_auth_template.py::test_auth_fresh_*` updated to use `auth_at_utc` key.

2. **`test_orchestrator_splitter.py`** (extend):
   - `split()` with K=3 phones and M=3 → 9 chunks, bucketed contiguously (chunks 1–3 op1, 4–6 op2, 7–9 op3).
   - Chunk operator_phone matches the passed list at the right index.
   - M > 3 raises ValueError.
   - Empty `operator_phones` list falls back to single-operator (slot=op1, phone=config.OPERATOR_PHONE).
   - Chunk IDs still pad to 3 digits minimum.

3. **`test_orchestrator_auth_template.py`** (extend):
   - `ensure_auth_seeds` Path A: all K seeds exist and are fresh → returns dict, no interactive.
   - `ensure_auth_seeds` Path B: only op1 can use main profile copy; op2/op3 must go interactive. Verify op1's seed ends up a copy, op2/op3 raise AuthSeedTimeout when interactive times out (mocked).
   - `clone_to_chunks` with K=2 operators → each chunk is cloned from its operator's seed (verify copytree source path via monkeypatch).

4. **`test_orchestrator_spawner.py`** (extend):
   - Spawned child env contains `BOOKING_BOT_OPERATOR_SLOT` and `BOOKING_BOT_OPERATOR_PHONE` matching spec.

5. **`test_shared_auth.py`** (extend):
   - `_shared_auth_path()` with env var `BOOKING_BOT_OPERATOR_SLOT=op2` returns `shared_auth-op2.json`.
   - Without env var → returns `shared_auth.json` (regression check).
   - Invalid slot value (e.g., `"../evil"`) falls back to default (defensive).

6. **`test_config_operator_env.py`** (new):
   - Importing `config` with `BOOKING_BOT_OPERATOR_PHONE` set overrides `OPERATOR_PHONE`.
   - Empty env var does NOT override (uses default).
   - Invalid env var (non-numeric) ... we don't validate here; config just accepts it. Test that the override happens verbatim.

7. **`test_orchestrator_cli.py`** (extend):
   - `start --operator-phones 111,222,333 --clones-per-operator 3` → splitter called with these args.
   - `auth --operator-phones 111,222` → ensure_auth_seeds called with ['111', '222'].
   - `auth --operator-phone 111` (legacy singular) → still works, maps to list of one.
   - `start --operator-phones 111 --chunk-size 500` → warning logged that chunk-size is ignored.
   - Invalid phone list (non-10-digit) → argparse error.

8. **`test_orchestrator_heartbeat.py`** (extend):
   - New heartbeat file with `operator_slot="op1"` round-trips.
   - Old heartbeat file missing `operator_slot` parses with `operator_slot=None`.

9. **`test_orchestrator_monitor.py`** (extend):
   - Render-once with 3 stuck chunks same slot → renders `"!! operator op1 NEEDS RE-AUTH ..."` line.
   - Render-once with mixed healthy and stuck chunks → banner only for the stuck operator's slot.

## Error handling

- **Phone list parse errors:** argparse-level validation (digit-only, 10 digits, 1..10 items, no duplicates). Raise `argparse.ArgumentTypeError` with a clear message.
- **Auth seed missing at start time:** if `start` is called and any slot's seed is missing or stale, raise a new `exceptions.AuthSeedMissing` with the list of missing slots. Operator runs `auth --operator-phones <list>` to recreate.
- **Interactive auth timeout on slot N:** `AuthSeedTimeout` message now includes the slot name and the phone (masked). Previous slots' seeds are left on disk (not rolled back) so a retry picks up where it left off.
- **Clone failure for slot N:** Existing `AuthCloneFailed` aggregates failures; now each failure includes the slot. Operator can inspect which slot/chunk broke.
- **Bot-side: missing env var when slot expected:** not an error. If `BOOKING_BOT_OPERATOR_SLOT` is unset, browser uses legacy `shared_auth.json` (orchestrator-less mode). Treated as feature, not bug.

## Prerequisite bug fix: `_auth_fresh` key mismatch

Today (`auth_template.py:34-43`):
```python
def _auth_fresh(profile_dir: Path, *, max_age_s: float) -> bool:
    last_auth = profile_dir / "last_auth.json"
    if not last_auth.exists():
        return False
    try:
        data = json.loads(last_auth.read_text(encoding="utf-8"))
        ts = float(data["timestamp"])  # BUG: real key is "auth_at_utc"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    return (time.time() - ts) < max_age_s
```

Fixed:
```python
def _auth_fresh(profile_dir: Path, *, max_age_s: float) -> bool:
    last_auth = profile_dir / "last_auth.json"
    if not last_auth.exists():
        return False
    try:
        data = json.loads(last_auth.read_text(encoding="utf-8"))
        written_at = datetime.fromisoformat(data["auth_at_utc"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc) - written_at).total_seconds()
    return 0 <= age < max_age_s
```

This ships as Task 1 of the implementation plan — everything else depends on it working.

## Tradeoffs (captured for the record)

- **Blast radius.** `browser.py` and `config.py` are touched by every bot. Changes are additive and env-var-gated, so orchestrator-less mode is unaffected, but both files get a careful review.
- **Operator phone list is required config for multi-operator runs.** Coupling between CLI args and enrolled phones; validation is loud and early (before any chunks spawn). If a phone isn't enrolled, the interactive OTP for its slot fails and the whole start is aborted — no partial state.
- **No auto-failover between operators.** Kept out of scope. Re-auth is the human escape hatch.
- **Slot naming is positional (`op1`, `op2`, ...).** If the operator reorders the `--operator-phones` list between `auth` and `start`, seeds mis-align. Mitigation: phones are validated to match the order at `start` time — we store a small metadata file alongside each seed with the operator phone it was seeded for, and `start` verifies that each slot's stored phone matches the CLI arg. If mismatch, loud error.

## Out-of-scope follow-ups (post-tonight)

- Dynamic per-operator chunk rebalancing if one operator dies permanently.
- Per-operator heartbeat aggregation in the monitor ("op1: 3/3 running, 421/1132 done").
- Configurable M > 3 once per-account ceiling has been empirically probed.
- `auth` subcommand with `--parallel` flag opening K headed windows simultaneously.
