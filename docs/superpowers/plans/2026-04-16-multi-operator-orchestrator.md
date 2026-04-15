# Multi-Operator Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the orchestrator to spawn K×M parallel bot instances across K distinct HPCL operator phones (each phone hosts at most M=3 concurrent sessions), so the 3,394-row lalji batch can finish in ~3.3 hours.

**Architecture:** Promote "operator slot" (`op1`..`opK`) to a first-class dimension carried through `ChunkSpec`, auth-seed paths, `shared_auth-<slot>.json`, and bot env vars. One interactive OTP per slot seeds its profile; each slot's seed is cloned to M chunk profile dirs; each bot reads/writes only its slot's shared_auth file. Prerequisite: fix the latent `_auth_fresh` key-mismatch bug that blocks Path B in the current single-operator code path.

**Tech Stack:** Python 3.11+, Playwright (existing), openpyxl (existing), argparse, pytest, rich (monitor UI).

**Spec:** [`docs/superpowers/specs/2026-04-16-multi-operator-orchestrator-design.md`](../specs/2026-04-16-multi-operator-orchestrator-design.md)

---

## File Structure (what each touched file is responsible for)

- `booking_bot/config.py` — reads `BOOKING_BOT_OPERATOR_PHONE` env var at import, overrides `OPERATOR_PHONE` for the child process lifetime. Exports `OPERATOR_PHONE_ENV` and `OPERATOR_SLOT_ENV` constants.
- `booking_bot/browser.py` — `_shared_auth_path()` becomes slot-aware; every caller (read/write/inject) automatically picks up per-slot files.
- `booking_bot/orchestrator/auth_template.py` — `_auth_fresh` bug fix; `_seed_path` takes a slot arg; `ensure_auth_seeds` (plural) handles K phones with per-slot Path A/B/C; seed metadata file records which phone seeded each slot; `clone_to_chunks` uses `chunk.operator_slot`.
- `booking_bot/orchestrator/splitter.py` — `ChunkSpec` gains `operator_slot`/`operator_phone` fields (with safe defaults); `split()` accepts `operator_phones` + `clones_per_operator` and buckets chunks contiguously across operator slots.
- `booking_bot/orchestrator/spawner.py` — exports `BOOKING_BOT_OPERATOR_SLOT` and `BOOKING_BOT_OPERATOR_PHONE` env vars per spawned child.
- `booking_bot/orchestrator/heartbeat.py` — `Heartbeat` dataclass gets optional `operator_slot` field; `read()` is backwards-compat with old files missing the field.
- `booking_bot/orchestrator/cli.py` — `auth` subcommand accepts `--operator-phones` (list); `start` subcommand accepts `--operator-phones` and `--clones-per-operator`; start-time verification that every slot's seed exists and matches the phone passed.
- `booking_bot/orchestrator/monitor.py` — table gets an "Op" column; render emits a visible re-auth banner when ≥2 chunks in the same operator slot are stuck in authenticating phase.
- New tests under `tests/` matching existing naming conventions.

---

## Task 1: Fix `_auth_fresh` key-mismatch bug

**Why first:** Every other task in the plan depends on `_auth_fresh` correctly recognizing a fresh `last_auth.json` written by `browser.py`. Today it reads `data["timestamp"]` (float) while `browser.py` writes `{"auth_at_utc": <ISO string>}`, so Path B (main profile → seed) silently never fires and every orchestrator run falls through to interactive OTP.

**Files:**
- Modify: `booking_bot/orchestrator/auth_template.py:34-43`
- Modify: `tests/test_orchestrator_auth_template.py` (update `_make_seed_profile` helper to write the real key, and add regression tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator_auth_template.py` near the top (after the `_make_chunk_spec` helper):

```python
def test_auth_fresh_accepts_real_browser_format(auth_env):
    """Regression: browser.py writes {'auth_at_utc': ISO}, not
    {'timestamp': float}. _auth_fresh must recognize the real format."""
    from datetime import datetime, timezone
    seed = auth_env / ".chromium-profile-REAL-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    assert auth_template._auth_fresh(seed, max_age_s=3600) is True


def test_auth_fresh_rejects_stale_auth_at_utc(auth_env):
    from datetime import datetime, timedelta, timezone
    seed = auth_env / ".chromium-profile-STALE-auth-seed"
    seed.mkdir(parents=True)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": old.isoformat()}), encoding="utf-8",
    )
    assert auth_template._auth_fresh(seed, max_age_s=24 * 3600) is False


def test_auth_fresh_rejects_missing_auth_at_utc_key(auth_env):
    seed = auth_env / ".chromium-profile-NOKEY-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text(
        json.dumps({"timestamp": 12345.0}), encoding="utf-8",
    )
    assert auth_template._auth_fresh(seed, max_age_s=3600) is False


def test_auth_fresh_rejects_corrupt_json(auth_env):
    seed = auth_env / ".chromium-profile-CORRUPT-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text("not-json", encoding="utf-8")
    assert auth_template._auth_fresh(seed, max_age_s=3600) is False
```

Also update the existing `_make_seed_profile` helper in the same file (line ~26) to use the real key. Replace:

```python
    ts = time.time() - 3600 if fresh else time.time() - 30 * 3600
    (seed / "last_auth.json").write_text(
        json.dumps({"timestamp": ts}), encoding="utf-8"
    )
```

with:

```python
    from datetime import datetime, timedelta, timezone
    delta = timedelta(hours=1) if fresh else timedelta(hours=30)
    written_at = datetime.now(timezone.utc) - delta
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": written_at.isoformat()}), encoding="utf-8"
    )
```

Also update `test_clone_to_chunks_skips_chunks_with_fresh_auth` in the same file — change the inline `{"timestamp": time.time()}` to `{"auth_at_utc": datetime.now(timezone.utc).isoformat()}` (and add the import at the top of the file). Same for `test_ensure_auth_seed_path_b_copies_from_main_profile` further down.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py -v
```

Expected: new tests FAIL with `KeyError: 'auth_at_utc'` (for the accept test) or `assert False != True` on the passing ones, plus the existing Path A/B tests now fail because `_auth_fresh` can't read the new format.

- [ ] **Step 3: Implement the fix**

In `booking_bot/orchestrator/auth_template.py`, replace the entire `_auth_fresh` function (currently lines 34–43):

```python
def _auth_fresh(profile_dir: Path, *, max_age_s: float) -> bool:
    """True iff profile_dir/last_auth.json exists, parses, and is less than
    max_age_s old. Expects the browser.py write format:
    {"auth_at_utc": "<ISO-8601 UTC timestamp>"}. Any other shape (missing
    key, wrong type, malformed JSON) collapses to False — callers fall
    through to a fresh interactive auth in that case."""
    last_auth = profile_dir / "last_auth.json"
    if not last_auth.exists():
        return False
    try:
        data = json.loads(last_auth.read_text(encoding="utf-8"))
        written_at = datetime.fromisoformat(data["auth_at_utc"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    age_s = (datetime.now(timezone.utc) - written_at).total_seconds()
    return 0 <= age_s < max_age_s
```

Add the import at the top of `auth_template.py` alongside the other imports (after `import time`):

```python
from datetime import datetime, timezone
```

The `import time` line can be removed if no other usage exists — verify with grep before deleting. (The `_interactive_auth_seed` function uses `time.monotonic` and `time.sleep`, so keep `import time`.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py -v
```

Expected: all 8+ tests in the file pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/auth_template.py tests/test_orchestrator_auth_template.py
git commit -m "fix(orchestrator): _auth_fresh reads auth_at_utc, not timestamp"
```

---

## Task 2: Per-operator `_seed_path` + seed metadata

**Files:**
- Modify: `booking_bot/orchestrator/auth_template.py` — `_seed_path`, add metadata helpers
- Modify: `tests/test_orchestrator_auth_template.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator_auth_template.py`:

```python
def test_seed_path_includes_slot(auth_env):
    assert auth_template._seed_path("FOO", "op1") == (
        auth_env / ".chromium-profile-FOO-op1-auth-seed"
    )
    assert auth_template._seed_path("FOO", "op3") == (
        auth_env / ".chromium-profile-FOO-op3-auth-seed"
    )


def test_seed_path_defaults_to_op1(auth_env):
    # Back-compat: callers that don't know about slots get op1.
    assert auth_template._seed_path("BAR") == (
        auth_env / ".chromium-profile-BAR-op1-auth-seed"
    )


def test_write_and_read_seed_phone(auth_env):
    auth_template._write_seed_phone("FOO", "op2", "9876543210")
    assert auth_template._read_seed_phone("FOO", "op2") == "9876543210"


def test_read_seed_phone_missing_returns_none(auth_env):
    assert auth_template._read_seed_phone("FOO", "op2") is None


def test_read_seed_phone_corrupt_returns_none(auth_env):
    seed = auth_env / ".chromium-profile-FOO-op2-auth-seed"
    seed.mkdir(parents=True)
    (seed / "seed_phone.json").write_text("not-json", encoding="utf-8")
    assert auth_template._read_seed_phone("FOO", "op2") is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py::test_seed_path_includes_slot tests/test_orchestrator_auth_template.py::test_seed_path_defaults_to_op1 tests/test_orchestrator_auth_template.py::test_write_and_read_seed_phone tests/test_orchestrator_auth_template.py::test_read_seed_phone_missing_returns_none tests/test_orchestrator_auth_template.py::test_read_seed_phone_corrupt_returns_none -v
```

Expected: FAIL with `TypeError: _seed_path() takes 1 positional argument but 2 were given` and `AttributeError: module '...' has no attribute '_write_seed_phone'`.

- [ ] **Step 3: Implement**

In `booking_bot/orchestrator/auth_template.py`, replace `_seed_path` (currently line 26–27):

```python
def _seed_path(source: str, slot: str = "op1") -> Path:
    """Auth-seed profile path for (source, slot). Slot is the operator
    bucket label: op1, op2, ..., opK. Single-operator callers use the
    default slot='op1'."""
    return config.ROOT / f".chromium-profile-{source}-{slot}-auth-seed"


def _seed_phone_meta_path(source: str, slot: str) -> Path:
    """Path to the small JSON file sitting alongside each auth seed that
    records which operator phone seeded it. Used by `start` to verify
    that the `--operator-phones` argument still matches the seeds on
    disk (guards against a reordered phone list between auth and start)."""
    return _seed_path(source, slot) / "seed_phone.json"


def _write_seed_phone(source: str, slot: str, phone: str) -> None:
    """Record the operator phone used to create this slot's seed. Written
    by `ensure_auth_seeds` after a successful interactive or Path-B seed.
    Never raises — metadata is advisory, not load-bearing."""
    path = _seed_phone_meta_path(source, slot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"operator_phone": phone}), encoding="utf-8",
        )
    except OSError as e:
        log.warning(f"could not write seed_phone.json for {source}/{slot}: {e}")


def _read_seed_phone(source: str, slot: str) -> str | None:
    """Return the phone recorded for this slot's seed, or None if missing
    or corrupt. Never raises."""
    path = _seed_phone_meta_path(source, slot)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    phone = data.get("operator_phone") if isinstance(data, dict) else None
    return str(phone) if phone else None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py -v
```

Expected: all tests pass (existing plus new slot/metadata tests).

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/auth_template.py tests/test_orchestrator_auth_template.py
git commit -m "feat(orchestrator): per-operator seed paths + seed phone metadata"
```

---

## Task 3: `ensure_auth_seeds` (plural) function

**Files:**
- Modify: `booking_bot/orchestrator/auth_template.py` — add `ensure_auth_seeds`, keep `ensure_auth_seed` as single-slot wrapper
- Modify: `tests/test_orchestrator_auth_template.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_ensure_auth_seeds_path_a_all_fresh(auth_env, monkeypatch):
    """All K seeds already fresh on disk → no browser launch, returns
    {slot: path} for each."""
    from datetime import datetime, timezone
    for slot in ("op1", "op2"):
        seed = auth_env / f".chromium-profile-MULTI-{slot}-auth-seed"
        seed.mkdir(parents=True)
        (seed / "last_auth.json").write_text(
            json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        auth_template, "_interactive_auth_seed",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not launch browser"),
        ),
    )
    seeds = auth_template.ensure_auth_seeds("MULTI", ["9111111111", "9222222222"])
    assert set(seeds.keys()) == {"op1", "op2"}
    assert seeds["op1"] == auth_env / ".chromium-profile-MULTI-op1-auth-seed"
    assert seeds["op2"] == auth_env / ".chromium-profile-MULTI-op2-auth-seed"


def test_ensure_auth_seeds_path_b_copies_main_to_op1_only(auth_env, monkeypatch):
    """op1 can borrow from fresh main profile; op2 still goes interactive."""
    from datetime import datetime, timezone
    main_profile = auth_env / ".chromium-profile"
    main_profile.mkdir()
    (main_profile / "Default").mkdir()
    (main_profile / "Default" / "Cookies").write_bytes(b"main-cookies")
    (main_profile / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    interactive_calls = []

    def stub_interactive(source, *, slot, operator_phone=None):
        interactive_calls.append((source, slot, operator_phone))
        seed = auth_env / f".chromium-profile-{source}-{slot}-auth-seed"
        seed.mkdir(parents=True, exist_ok=True)
        (seed / "last_auth.json").write_text(
            json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        return seed

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", stub_interactive)
    seeds = auth_template.ensure_auth_seeds("MULTI", ["9111111111", "9222222222"])
    assert interactive_calls == [("MULTI", "op2", "9222222222")]
    assert (seeds["op1"] / "Default" / "Cookies").read_bytes() == b"main-cookies"


def test_ensure_auth_seeds_writes_seed_phone_metadata(auth_env, monkeypatch):
    def stub_interactive(source, *, slot, operator_phone=None):
        from datetime import datetime, timezone
        seed = auth_env / f".chromium-profile-{source}-{slot}-auth-seed"
        seed.mkdir(parents=True, exist_ok=True)
        (seed / "last_auth.json").write_text(
            json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        return seed

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", stub_interactive)
    auth_template.ensure_auth_seeds("MULTI", ["9111111111", "9222222222"])
    assert auth_template._read_seed_phone("MULTI", "op1") == "9111111111"
    assert auth_template._read_seed_phone("MULTI", "op2") == "9222222222"


def test_ensure_auth_seed_legacy_wrapper_still_works(auth_env, monkeypatch):
    """Back-compat: the original ensure_auth_seed(source) signature
    still returns op1's path so callers that don't know about multi-op
    don't break."""
    from datetime import datetime, timezone
    seed = auth_env / ".chromium-profile-LEGACY-op1-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        auth_template, "_interactive_auth_seed",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not launch")),
    )
    result = auth_template.ensure_auth_seed("LEGACY")
    assert result == seed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py::test_ensure_auth_seeds_path_a_all_fresh tests/test_orchestrator_auth_template.py::test_ensure_auth_seeds_path_b_copies_main_to_op1_only tests/test_orchestrator_auth_template.py::test_ensure_auth_seeds_writes_seed_phone_metadata tests/test_orchestrator_auth_template.py::test_ensure_auth_seed_legacy_wrapper_still_works -v
```

Expected: FAIL with `AttributeError: module '...' has no attribute 'ensure_auth_seeds'`.

- [ ] **Step 3: Implement**

Replace the `ensure_auth_seed` function in `booking_bot/orchestrator/auth_template.py` (currently lines 97–135) with:

```python
def ensure_auth_seeds(
    source: str, operator_phones: list[str],
) -> dict[str, Path]:
    """Return {slot: seed_path} for each operator phone in `operator_phones`.
    Slots are positional: op1, op2, ..., opK.

    For each slot, tries three paths:
      A) Seed already exists with fresh last_auth.json. Use as-is.
      B) Only for op1: main .chromium-profile is fresh → copytree to seed.
      C) Launch interactive Chromium against the slot's seed dir, block
         until the operator logs in (or timeout).

    Writes a seed_phone.json alongside each successful seed recording
    which phone seeded it. Raises AuthSeedTimeout on interactive timeout
    of any slot — previous slots' seeds are left on disk for a retry to
    pick up.
    """
    if not operator_phones:
        raise ValueError("operator_phones must be non-empty")
    max_age_s = float(
        config.AUTH_COOLDOWN_S - config.ORCHESTRATOR_AUTH_SEED_BUFFER_S
    )
    seeds: dict[str, Path] = {}
    for i, phone in enumerate(operator_phones):
        slot = f"op{i + 1}"
        seed = _seed_path(source, slot)

        if seed.exists() and _auth_fresh(seed, max_age_s=max_age_s):
            log.info(f"auth seed {source}/{slot}: fresh ({seed})")
            seeds[slot] = seed
            _write_seed_phone(source, slot, phone)
            continue

        if slot == "op1":
            main_profile = config.ROOT / ".chromium-profile"
            if main_profile.exists() and _auth_fresh(
                main_profile, max_age_s=max_age_s,
            ):
                log.info(
                    f"auth seed {source}/op1: copying from main profile "
                    f"{main_profile}"
                )
                if seed.exists():
                    shutil.rmtree(seed)
                shutil.copytree(main_profile, seed)
                _scrub_lock_files(seed)
                _write_seed_phone(source, slot, phone)
                seeds[slot] = seed
                continue

        log.info(
            f"auth seed {source}/{slot}: launching interactive auth for "
            f"operator {phone[:3]}XXXXXXX"
        )
        path = _interactive_auth_seed(source, slot=slot, operator_phone=phone)
        _write_seed_phone(source, slot, phone)
        seeds[slot] = path
    return seeds


def ensure_auth_seed(
    source: str, *, operator_phone: str | None = None,
) -> Path:
    """Legacy single-slot wrapper. Callers that don't know about multi-op
    (existing single-operator code paths) continue to work unchanged."""
    phone = operator_phone or config.OPERATOR_PHONE
    seeds = ensure_auth_seeds(source, [phone])
    return seeds["op1"]
```

Update `_interactive_auth_seed` (currently lines 138–181) to accept a `slot` kwarg so the seed path and profile-suffix it opens use the slotted path:

```python
def _interactive_auth_seed(
    source: str, *, slot: str = "op1", operator_phone: str | None = None,
) -> Path:
    """Interactive Path C. Launches a real Chromium window against
    `.chromium-profile-<source>-<slot>-auth-seed/`, polls the profile's
    last_auth.json, and closes the browser once the operator logs in.
    Raises AuthSeedTimeout on timeout."""
    from booking_bot import browser  # lazy — keeps Playwright off the import graph for unit tests
    seed = _seed_path(source, slot)
    seed.mkdir(parents=True, exist_ok=True)
    pw, _browser_obj, ctx, _page = browser.start_browser(
        headless=False,
        profile_suffix=f"{source}-{slot}-auth-seed",
    )
    print(
        f"[auth_template] Auth seed {slot}: log in to HPCL in the browser "
        f"window as operator "
        f"{(operator_phone or '')[:3]}XXXXXXX. This window will close once "
        f"authentication completes "
        f"(timeout: {config.ORCHESTRATOR_AUTH_TIMEOUT_S // 60} min).",
        flush=True,
    )
    deadline = time.monotonic() + config.ORCHESTRATOR_AUTH_TIMEOUT_S
    poll_start = time.monotonic()
    try:
        while time.monotonic() < deadline:
            if _auth_fresh(seed, max_age_s=60.0):
                last_mtime = (seed / "last_auth.json").stat().st_mtime
                if last_mtime >= poll_start:
                    time.sleep(5.0)  # let redirects settle
                    return seed
            time.sleep(2.0)
        raise exceptions.AuthSeedTimeout(
            f"auth seed for {source}/{slot} timed out after "
            f"{config.ORCHESTRATOR_AUTH_TIMEOUT_S}s"
        )
    finally:
        try:
            ctx.close()
        except Exception as e:
            log.warning(f"auth seed ctx.close() failed: {e}")
        try:
            pw.stop()
        except Exception as e:
            log.warning(f"auth seed pw.stop() failed: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py -v
```

Expected: all tests pass including the new plural-function tests and the legacy wrapper.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/auth_template.py tests/test_orchestrator_auth_template.py
git commit -m "feat(orchestrator): ensure_auth_seeds plural for multi-operator"
```

---

## Task 4: `ChunkSpec` operator fields + `splitter.split` multi-operator path

**Files:**
- Modify: `booking_bot/orchestrator/splitter.py`
- Modify: `tests/test_orchestrator_splitter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator_splitter.py`:

```python
def test_chunk_spec_has_operator_slot_and_phone_defaults():
    spec = splitter.ChunkSpec(
        source="TEST", chunk_id="TEST-001", chunk_index=1,
        input_path=Path("Input/chunks/TEST/TEST-001.xlsx"),
        profile_suffix="TEST-001",
        heartbeat_path=Path("data/runs/TEST/TEST-001.heartbeat.json"),
        row_count=5,
    )
    assert spec.operator_slot == "op1"
    assert spec.operator_phone == ""


def test_split_multi_operator_K3_M3(split_env):
    inp = _make_input_xlsx(split_env / "Input" / "file.xlsx", n_rows=27)
    chunks = splitter.split(
        "LALJI", inp,
        operator_phones=["9111111111", "9222222222", "9333333333"],
        clones_per_operator=3,
    )
    assert len(chunks) == 9
    # Chunks 1-3 → op1, 4-6 → op2, 7-9 → op3
    assert [c.operator_slot for c in chunks] == [
        "op1", "op1", "op1", "op2", "op2", "op2", "op3", "op3", "op3",
    ]
    assert [c.operator_phone for c in chunks] == [
        "9111111111", "9111111111", "9111111111",
        "9222222222", "9222222222", "9222222222",
        "9333333333", "9333333333", "9333333333",
    ]
    # Contiguous row buckets
    assert [c.row_count for c in chunks] == [3, 3, 3, 3, 3, 3, 3, 3, 3]


def test_split_multi_operator_uneven_rows(split_env):
    """N = 10 rows, K=3 M=3 → 9 chunks. Effective size = ceil(10/9) = 2.
    Chunks are not strictly balanced; the implementation uses existing
    equal-contiguous-split logic (trailing chunks smaller or empty)."""
    inp = _make_input_xlsx(split_env / "Input" / "file.xlsx", n_rows=10)
    chunks = splitter.split(
        "LALJI", inp,
        operator_phones=["9111111111", "9222222222", "9333333333"],
        clones_per_operator=3,
    )
    assert len(chunks) == 9
    assert sum(c.row_count for c in chunks) == 10


def test_split_multi_operator_M_too_high_rejected(split_env):
    inp = _make_input_xlsx(split_env / "Input" / "file.xlsx", n_rows=20)
    with pytest.raises(ValueError, match="clones_per_operator"):
        splitter.split(
            "T", inp,
            operator_phones=["9111111111"],
            clones_per_operator=4,
        )


def test_split_multi_operator_M_zero_rejected(split_env):
    inp = _make_input_xlsx(split_env / "Input" / "file.xlsx", n_rows=20)
    with pytest.raises(ValueError, match="clones_per_operator"):
        splitter.split(
            "T", inp,
            operator_phones=["9111111111"],
            clones_per_operator=0,
        )


def test_split_legacy_single_operator_still_works(split_env):
    """Without operator_phones, split() behaves exactly as before:
    all chunks get slot=op1, phone=''."""
    inp = _make_input_xlsx(split_env / "Input" / "file.xlsx", n_rows=20)
    chunks = splitter.split("TEST", inp, chunk_size=5)
    assert len(chunks) == 4
    assert all(c.operator_slot == "op1" for c in chunks)
    assert all(c.operator_phone == "" for c in chunks)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_splitter.py -v
```

Expected: new tests FAIL with `AttributeError: 'ChunkSpec' object has no attribute 'operator_slot'` and `TypeError: split() got an unexpected keyword argument 'operator_phones'`.

- [ ] **Step 3: Implement**

In `booking_bot/orchestrator/splitter.py`, update `ChunkSpec` (currently around line 23):

```python
@dataclass(frozen=True)
class ChunkSpec:
    source: str
    chunk_id: str
    chunk_index: int
    input_path: Path
    profile_suffix: str
    heartbeat_path: Path
    row_count: int
    operator_slot: str = "op1"
    operator_phone: str = ""
```

Update `split()` signature and body (currently lines 42–105):

```python
def split(
    source: str,
    input_file: Path,
    *,
    chunk_size: int | None = None,
    num_chunks: int | None = None,
    operator_phones: list[str] | None = None,
    clones_per_operator: int = 3,
    output_dir: Path | None = None,
) -> list[ChunkSpec]:
    """Split input_file into chunks.

    Two modes:
      - Single-operator (legacy): pass exactly one of chunk_size /
        num_chunks. All chunks get operator_slot='op1', operator_phone=''.
      - Multi-operator: pass operator_phones=[...]. Produces
        K*clones_per_operator contiguous chunks. First M chunks get
        operator_slot='op1' and operator_phone=phones[0], next M → op2,
        and so on. `chunk_size`/`num_chunks` are ignored in this mode.

    Writes chunks to output_dir/<source>/<chunk-id>.xlsx (default
    output_dir = config.CHUNKS_DIR). Idempotent: skips writing a chunk
    whose row count already matches what's on disk."""
    _validate_source(source)

    if operator_phones is not None:
        if not operator_phones:
            raise ValueError("operator_phones must be non-empty")
        if not (1 <= clones_per_operator <= 3):
            raise ValueError(
                f"clones_per_operator must be between 1 and 3 (per-account "
                f"session limit); got {clones_per_operator}"
            )
        n_chunks_override = len(operator_phones) * clones_per_operator
        # Ignore chunk_size/num_chunks in multi-operator mode.
        chunk_size = None
        num_chunks = n_chunks_override
    else:
        if (chunk_size is None) == (num_chunks is None):
            raise ValueError(
                "pass exactly one of chunk_size or num_chunks to split()"
            )
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive; got {chunk_size}")
        if num_chunks is not None and num_chunks <= 0:
            raise ValueError(f"num_chunks must be positive; got {num_chunks}")

    input_file = Path(input_file)
    out_root = Path(output_dir) if output_dir is not None else config.CHUNKS_DIR
    chunks_dir = out_root / source
    chunks_dir.mkdir(parents=True, exist_ok=True)

    header, data_rows = _read_input_rows(input_file)
    total_rows = len(data_rows)
    if total_rows == 0:
        raise ValueError(f"input file has no data rows: {input_file}")

    effective_size, n_chunks = _resolve_parallelism(
        total_rows, chunk_size=chunk_size, num_chunks=num_chunks,
    )
    pad_width = max(3, len(str(n_chunks)))
    if n_chunks > 50:
        print(f"[splitter] WARNING: num_chunks={n_chunks} is unusually high",
              file=sys.stderr)
    if effective_size < 10:
        print(f"[splitter] WARNING: chunk size={effective_size} is unusually low",
              file=sys.stderr)

    specs: list[ChunkSpec] = []
    for i in range(n_chunks):
        start = i * effective_size
        end = min(start + effective_size, total_rows)
        rows_slice = data_rows[start:end]
        chunk_index = i + 1
        chunk_id = f"{source}-{chunk_index:0{pad_width}d}"
        chunk_path = chunks_dir / f"{chunk_id}.xlsx"
        heartbeat_path = config.RUNS_DIR / source / f"{chunk_id}.heartbeat.json"
        _write_chunk_file(chunk_path, header, rows_slice)

        if operator_phones is not None:
            op_idx = i // clones_per_operator
            operator_slot = f"op{op_idx + 1}"
            operator_phone = operator_phones[op_idx]
        else:
            operator_slot = "op1"
            operator_phone = ""

        specs.append(ChunkSpec(
            source=source,
            chunk_id=chunk_id,
            chunk_index=chunk_index,
            input_path=chunk_path,
            profile_suffix=chunk_id,
            heartbeat_path=heartbeat_path,
            row_count=len(rows_slice),
            operator_slot=operator_slot,
            operator_phone=operator_phone,
        ))
    return specs
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_splitter.py -v
```

Expected: all splitter tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/splitter.py tests/test_orchestrator_splitter.py
git commit -m "feat(splitter): multi-operator chunk bucketing"
```

---

## Task 5: `clone_to_chunks` uses `chunk.operator_slot`

**Files:**
- Modify: `booking_bot/orchestrator/auth_template.py` — `clone_to_chunks`
- Modify: `tests/test_orchestrator_auth_template.py`

- [ ] **Step 1: Write the failing test**

```python
def test_clone_to_chunks_uses_per_chunk_operator_slot(auth_env, monkeypatch):
    """Each chunk's operator_slot picks which seed its profile is cloned
    from. Two seeds, six chunks (3 per seed)."""
    from datetime import datetime, timezone

    for slot, marker in (("op1", b"seed1"), ("op2", b"seed2")):
        seed = auth_env / f".chromium-profile-MULTI-{slot}-auth-seed"
        seed.mkdir(parents=True)
        (seed / "Default").mkdir()
        (seed / "Default" / "Cookies").write_bytes(marker)
        (seed / "last_auth.json").write_text(
            json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )

    def _spec(idx, slot, phone):
        return ChunkSpec(
            source="MULTI",
            chunk_id=f"MULTI-{idx:03d}",
            chunk_index=idx,
            input_path=auth_env / "Input" / "chunks" / "MULTI" / f"MULTI-{idx:03d}.xlsx",
            profile_suffix=f"MULTI-{idx:03d}",
            heartbeat_path=auth_env / "data" / "runs" / "MULTI" / f"MULTI-{idx:03d}.heartbeat.json",
            row_count=5,
            operator_slot=slot,
            operator_phone=phone,
        )

    chunks = [
        _spec(1, "op1", "9111111111"),
        _spec(2, "op1", "9111111111"),
        _spec(3, "op1", "9111111111"),
        _spec(4, "op2", "9222222222"),
        _spec(5, "op2", "9222222222"),
        _spec(6, "op2", "9222222222"),
    ]
    auth_template.clone_to_chunks("MULTI", chunks)
    for c in chunks:
        target = auth_env / f".chromium-profile-{c.profile_suffix}"
        expected = b"seed1" if c.operator_slot == "op1" else b"seed2"
        assert (target / "Default" / "Cookies").read_bytes() == expected


def test_clone_to_chunks_raises_when_slot_seed_missing(auth_env):
    """If a chunk's operator_slot has no seed on disk, raise FileNotFoundError
    naming the missing slot."""
    chunk = ChunkSpec(
        source="MULTI",
        chunk_id="MULTI-001",
        chunk_index=1,
        input_path=auth_env / "Input" / "chunks" / "MULTI" / "MULTI-001.xlsx",
        profile_suffix="MULTI-001",
        heartbeat_path=auth_env / "data" / "runs" / "MULTI" / "MULTI-001.heartbeat.json",
        row_count=5,
        operator_slot="op2",
        operator_phone="9222222222",
    )
    with pytest.raises(FileNotFoundError, match="op2"):
        auth_template.clone_to_chunks("MULTI", [chunk])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py::test_clone_to_chunks_uses_per_chunk_operator_slot tests/test_orchestrator_auth_template.py::test_clone_to_chunks_raises_when_slot_seed_missing -v
```

Expected: FAIL. The current `clone_to_chunks` uses `_seed_path(source)` (single-arg) which produces `.chromium-profile-MULTI-auth-seed` instead of `.chromium-profile-MULTI-op1-auth-seed`.

- [ ] **Step 3: Implement**

Replace `clone_to_chunks` in `booking_bot/orchestrator/auth_template.py` (currently lines 62–94):

```python
def clone_to_chunks(source: str, chunks: list[ChunkSpec]) -> None:
    """Copy each chunk's operator-slot seed profile to the chunk's own
    profile dir. Skips chunks whose target already has a fresh
    `last_auth.json`. Aggregates all failures and raises AuthCloneFailed
    at the end with the complete list so the operator can see every
    chunk that broke.

    Raises FileNotFoundError early if any needed slot's seed is missing
    — fail before starting any clones so we don't leave half-populated
    state behind."""
    needed_slots = {c.operator_slot for c in chunks}
    for slot in needed_slots:
        seed = _seed_path(source, slot)
        if not seed.exists():
            raise FileNotFoundError(
                f"auth seed missing for {source}/{slot}: {seed}. Run "
                f"`orchestrator auth --source {source} --operator-phones "
                f"<list>` first."
            )

    max_age_s = float(config.AUTH_COOLDOWN_S)
    failures: list[tuple[str, str]] = []
    for c in chunks:
        seed = _seed_path(source, c.operator_slot)
        target = _chunk_profile_path(c.profile_suffix)
        if target.exists() and _auth_fresh(target, max_age_s=max_age_s):
            log.info(
                f"chunk {c.chunk_id}: profile already fresh, skipping clone"
            )
            continue
        if target.exists():
            try:
                shutil.rmtree(target)
            except OSError as e:
                failures.append((c.chunk_id, f"rmtree failed: {e}"))
                continue
        try:
            shutil.copytree(seed, target)
        except OSError as e:
            failures.append((c.chunk_id, f"copytree failed: {e}"))
            continue
        _scrub_lock_files(target)
        log.info(
            f"chunk {c.chunk_id}: profile cloned from seed "
            f"{c.operator_slot}"
        )
    if failures:
        raise exceptions.AuthCloneFailed(failures=failures)
```

**Note on existing test `test_clone_to_chunks_copies_seed_to_each_chunk_profile`:** this test uses `_make_seed_profile` which creates `.chromium-profile-<source>-auth-seed` (no slot). The updated `_seed_path` defaults to `op1`, so that old path no longer matches. Update the helper to write the slotted path:

Replace `_make_seed_profile` in `tests/test_orchestrator_auth_template.py`:

```python
def _make_seed_profile(
    root: Path, source: str, *, fresh: bool = True, slot: str = "op1",
) -> Path:
    """Create a .chromium-profile-<source>-<slot>-auth-seed/ dir with a
    last_auth.json and one fake singleton lock file."""
    from datetime import datetime, timedelta, timezone
    seed = root / f".chromium-profile-{source}-{slot}-auth-seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "Default").mkdir(exist_ok=True)
    (seed / "Default" / "Cookies").write_bytes(b"fake-cookie-db")
    (seed / "SingletonLock").write_text("fake-lock")
    (seed / "SingletonCookie").write_text("fake-cookie-lock")
    delta = timedelta(hours=1) if fresh else timedelta(hours=30)
    written_at = datetime.now(timezone.utc) - delta
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": written_at.isoformat()}), encoding="utf-8"
    )
    return seed
```

Also update the failing-copytree test's seed-dir reference (currently line ~95) to `.chromium-profile-TEST-op1-auth-seed`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_auth_template.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/auth_template.py tests/test_orchestrator_auth_template.py
git commit -m "feat(orchestrator): clone_to_chunks routes chunks to per-slot seeds"
```

---

## Task 6: `config.py` OPERATOR_PHONE env override

**Files:**
- Modify: `booking_bot/config.py`
- Create: `tests/test_config_operator_env.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_operator_env.py`:

```python
"""Bot-child environment variable overrides for operator phone.

The orchestrator spawner passes BOOKING_BOT_OPERATOR_PHONE to each child
so cloned bots under different operator slots can re-auth with their own
operator phone during the quiet-retry mechanism. This test covers only
the config module's import-time behavior — the bot's actual auth path
is covered by auth.py tests."""
import importlib


def test_operator_phone_env_override(monkeypatch):
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_PHONE", "9876543210")
    from booking_bot import config
    importlib.reload(config)
    assert config.OPERATOR_PHONE == "9876543210"


def test_operator_phone_env_empty_does_not_override(monkeypatch):
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_PHONE", "")
    from booking_bot import config
    importlib.reload(config)
    # Default remains the hardcoded one
    assert config.OPERATOR_PHONE == "9209114429"


def test_operator_phone_env_unset_does_not_override(monkeypatch):
    monkeypatch.delenv("BOOKING_BOT_OPERATOR_PHONE", raising=False)
    from booking_bot import config
    importlib.reload(config)
    assert config.OPERATOR_PHONE == "9209114429"


def test_operator_env_constants_exist():
    from booking_bot import config
    importlib.reload(config)
    assert config.OPERATOR_PHONE_ENV == "BOOKING_BOT_OPERATOR_PHONE"
    assert config.OPERATOR_SLOT_ENV == "BOOKING_BOT_OPERATOR_SLOT"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_config_operator_env.py -v
```

Expected: FAIL. `config.OPERATOR_PHONE_ENV` does not exist; env-var override is not applied.

- [ ] **Step 3: Implement**

In `booking_bot/config.py`, near the `OPERATOR_PHONE` declaration (currently line 47), add:

```python
# Env-var names the orchestrator spawner uses to tell a bot child which
# operator slot/phone it owns. Defined here (not in orchestrator/) so
# both config's import-time override and browser._shared_auth_path can
# share a single authoritative constant.
OPERATOR_PHONE_ENV = "BOOKING_BOT_OPERATOR_PHONE"
OPERATOR_SLOT_ENV  = "BOOKING_BOT_OPERATOR_SLOT"

OPERATOR_PHONE = "9209114429"   # operator edits this to their own number

# If the child process was spawned by the orchestrator with
# BOOKING_BOT_OPERATOR_PHONE set, that value wins over the module
# default. This runs at import time so every subsequent read of
# config.OPERATOR_PHONE (there are many) sees the right value without
# plumbing a parameter through every call site.
import os as _os
_env_operator_phone = _os.environ.get(OPERATOR_PHONE_ENV, "").strip()
if _env_operator_phone:
    OPERATOR_PHONE = _env_operator_phone
del _os, _env_operator_phone
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_config_operator_env.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/config.py tests/test_config_operator_env.py
git commit -m "feat(config): OPERATOR_PHONE env override for multi-operator bots"
```

---

## Task 7: `browser._shared_auth_path()` slot-aware

**Files:**
- Modify: `booking_bot/browser.py`
- Modify: `tests/test_shared_auth.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shared_auth.py`:

```python
def test_shared_auth_path_without_slot_env(tmp_root, monkeypatch):
    """Bare bot mode: env var unset → legacy shared_auth.json."""
    monkeypatch.delenv("BOOKING_BOT_OPERATOR_SLOT", raising=False)
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"


def test_shared_auth_path_with_slot_env(tmp_root, monkeypatch):
    """Orchestrator mode: env var set → per-slot shared_auth-opN.json."""
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op2")
    assert browser._shared_auth_path() == tmp_root / "shared_auth-op2.json"


def test_shared_auth_path_invalid_slot_falls_back_to_default(
    tmp_root, monkeypatch,
):
    """Defensive: a malformed slot value (path traversal attempt,
    whitespace, etc.) falls back to the legacy path rather than writing
    to an attacker-chosen location."""
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "../evil")
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op")
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op1 ")
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"


def test_write_then_read_round_trip_with_slot(tmp_root, monkeypatch):
    """Full write/read cycle with a slot set — proves the per-slot file
    is actually used end-to-end."""
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op3")
    cookies = [
        {
            "name": "sessionid", "value": "xyz",
            "domain": ".hpchatbot.hpcl.co.in", "path": "/",
        }
    ]
    page = _make_page_with_cookies(cookies)
    browser.write_shared_auth_state(page)
    assert (tmp_root / "shared_auth-op3.json").exists()
    assert not (tmp_root / "shared_auth.json").exists()
    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["cookies"] == cookies
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_shared_auth.py -v
```

Expected: FAIL — `_shared_auth_path()` currently ignores the env var.

- [ ] **Step 3: Implement**

In `booking_bot/browser.py`, replace `_shared_auth_path` (currently lines 139–143):

```python
_SLOT_RE = re.compile(r"^op[1-9]\d*$")


def _shared_auth_path() -> Path:
    """Disk location of the shared auth JSON. Single file per operator
    slot when BOOKING_BOT_OPERATOR_SLOT is set (orchestrator-spawned
    bot); falls back to the legacy unslotted filename for bare-bot
    mode. Malformed slot values fall back to the legacy path rather
    than writing to an attacker-chosen location."""
    slot = os.environ.get(config.OPERATOR_SLOT_ENV, "").strip()
    if slot and _SLOT_RE.match(slot):
        return Path(config.ROOT) / f"shared_auth-{slot}.json"
    return Path(config.ROOT) / config.SHARED_AUTH_FILENAME
```

Add `import re` at the top of `browser.py` if not already present (most likely is — verify). Add `import os` similarly.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_shared_auth.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/browser.py tests/test_shared_auth.py
git commit -m "feat(browser): per-slot shared_auth.json path"
```

---

## Task 8: Spawner exports new env vars

**Files:**
- Modify: `booking_bot/orchestrator/spawner.py`
- Modify: `tests/test_orchestrator_spawner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator_spawner.py` (near the existing `test_spawn_chunk_sets_env_vars`):

```python
def test_spawn_chunk_sets_operator_env_vars(spawner_env, monkeypatch):
    """Multi-operator env vars reach the child process."""
    monkeypatch.setenv("BOOKING_BOT_SPAWNER_CMD_OVERRIDE", f"{sys.executable}|{FAKE_BOT}")
    (spawner_env / "fake.xlsx").write_text("")
    spec = ChunkSpec(
        source="FAKE",
        chunk_id="FAKE-004",
        chunk_index=4,
        input_path=spawner_env / "fake.xlsx",
        profile_suffix="FAKE-004",
        heartbeat_path=spawner_env / "runs" / "FAKE" / "FAKE-004.heartbeat.json",
        row_count=3,
        operator_slot="op2",
        operator_phone="9222222222",
    )
    handle = spawner.spawn_chunk(spec, headed=False)
    handle.popen.wait(timeout=10)
    env_file = spec.heartbeat_path.parent / f"{spec.chunk_id}.env.txt"
    assert env_file.exists()
    text = env_file.read_text(encoding="utf-8")
    assert "BOOKING_BOT_OPERATOR_SLOT=op2" in text
    assert "BOOKING_BOT_OPERATOR_PHONE=9222222222" in text
```

Verify (read) `tests/fixtures/orchestrator/fake_bot.py` writes `<chunk_id>.env.txt` containing the relevant env vars — it already does for the existing vars. If it filters to a whitelist, the whitelist may need extending. Read the file:

```bash
cat tests/fixtures/orchestrator/fake_bot.py
```

If the env-dump is filtered (e.g., only BOOKING_BOT_* prefixed), it will automatically include the new ones. If not, adjust the fake_bot script minimally to also dump the two new vars (prefer inclusive dump of all BOOKING_BOT_* vars).

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_spawner.py::test_spawn_chunk_sets_operator_env_vars -v
```

Expected: FAIL — the new env vars aren't set.

- [ ] **Step 3: Implement**

In `booking_bot/orchestrator/spawner.py`, extend the env-var block in `spawn_chunk` (currently lines 102–105):

```python
    env = os.environ.copy()
    env["BOOKING_BOT_HEARTBEAT_PATH"] = str(spec.heartbeat_path)
    env["BOOKING_BOT_SOURCE"]          = spec.source
    env["BOOKING_BOT_CHUNK_ID"]        = spec.chunk_id
    env["BOOKING_BOT_OPERATOR_SLOT"]   = spec.operator_slot
    env["BOOKING_BOT_OPERATOR_PHONE"]  = spec.operator_phone
```

If `tests/fixtures/orchestrator/fake_bot.py` filters the env dump, widen it to include all `BOOKING_BOT_*` keys. Typical form:

```python
with open(env_file, "w", encoding="utf-8") as f:
    for k, v in os.environ.items():
        if k.startswith("BOOKING_BOT_"):
            f.write(f"{k}={v}\n")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_spawner.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/spawner.py tests/test_orchestrator_spawner.py tests/fixtures/orchestrator/fake_bot.py
git commit -m "feat(spawner): export operator slot/phone env vars"
```

---

## Task 9: `Heartbeat` optional `operator_slot` field

**Files:**
- Modify: `booking_bot/orchestrator/heartbeat.py`
- Modify: `tests/test_orchestrator_heartbeat.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator_heartbeat.py` (read the file first to see existing patterns, then append):

```python
def test_heartbeat_round_trip_with_operator_slot(tmp_path):
    from booking_bot.orchestrator.heartbeat import Heartbeat, read, write
    hb = Heartbeat(
        source="S", chunk_id="S-001", pid=1234,
        input_file="in.xlsx", profile_suffix="S-001",
        phase="booking", rows_total=10, rows_done=5, rows_issue=0,
        rows_pending=5, current_row_idx=6, current_phone="9876543210",
        started_at="2026-04-16T00:00:00+00:00",
        last_activity_at="2026-04-16T00:01:00+00:00",
        command=["python"], exit_code=None, last_error=None,
        operator_slot="op2",
    )
    path = tmp_path / "hb.json"
    write(path, hb)
    got = read(path)
    assert got is not None
    assert got.operator_slot == "op2"


def test_heartbeat_read_old_file_without_operator_slot(tmp_path):
    """Back-compat: old heartbeat files on disk that lack the new field
    must still parse successfully (operator_slot defaults to None)."""
    import json
    path = tmp_path / "hb.json"
    path.write_text(json.dumps({
        "source": "S", "chunk_id": "S-001", "pid": 1234,
        "input_file": "in.xlsx", "profile_suffix": "S-001",
        "phase": "booking", "rows_total": 10, "rows_done": 5,
        "rows_issue": 0, "rows_pending": 5, "current_row_idx": 6,
        "current_phone": "9876543210",
        "started_at": "2026-04-16T00:00:00+00:00",
        "last_activity_at": "2026-04-16T00:01:00+00:00",
        "command": ["python"], "exit_code": None, "last_error": None,
    }), encoding="utf-8")
    from booking_bot.orchestrator.heartbeat import read
    got = read(path)
    assert got is not None
    assert got.operator_slot is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_heartbeat.py -v
```

Expected: FAIL with `TypeError: Heartbeat.__init__() got an unexpected keyword argument 'operator_slot'`.

- [ ] **Step 3: Implement**

In `booking_bot/orchestrator/heartbeat.py`, update the `Heartbeat` dataclass (currently around line 16) and the `_REQUIRED_FIELDS` set (currently line 55):

```python
from dataclasses import MISSING


@dataclass
class Heartbeat:
    source: str
    chunk_id: str
    pid: int
    input_file: str
    profile_suffix: str
    phase: str
    rows_total: int
    rows_done: int
    rows_issue: int
    rows_pending: int
    current_row_idx: int | None
    current_phone: str | None
    started_at: str          # ISO-8601 with +00:00
    last_activity_at: str
    command: list[str]
    exit_code: int | None
    last_error: str | None
    operator_slot: str | None = None   # NEW: orchestrator-multi-operator mode
```

And replace the existing `_REQUIRED_FIELDS` line (currently line 55) and the `read()` function body's parsing (currently around line 104) with:

```python
# Fields without a default value — every heartbeat file on disk must
# carry these. Fields with defaults (like operator_slot) are optional
# so old files written before the field existed still parse.
_REQUIRED_FIELDS = {f.name for f in fields(Heartbeat) if f.default is MISSING}
_ALL_FIELDS = {f.name for f in fields(Heartbeat)}
```

Update `read()` (the end of the function):

```python
    if not _REQUIRED_FIELDS.issubset(data.keys()):
        return None
    try:
        return Heartbeat(**{
            name: data[name] for name in _ALL_FIELDS if name in data
        })
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_heartbeat.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/heartbeat.py tests/test_orchestrator_heartbeat.py
git commit -m "feat(heartbeat): optional operator_slot field (backwards-compat)"
```

---

## Task 10: CLI `auth` subcommand accepts `--operator-phones`

**Files:**
- Modify: `booking_bot/orchestrator/cli.py`
- Modify: `tests/test_orchestrator_cli.py`

- [ ] **Step 1: Read existing CLI tests**

```bash
cd D:/workspace/booking_bot && head -100 tests/test_orchestrator_cli.py
```

Match the file's existing fixture/import patterns when writing the new tests.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_orchestrator_cli.py`:

```python
def test_auth_subcommand_parses_operator_phones_list(monkeypatch):
    from booking_bot.orchestrator import cli, auth_template
    captured = {}

    def fake_ensure(source, phones):
        captured["source"] = source
        captured["phones"] = phones
        return {f"op{i+1}": None for i in range(len(phones))}

    monkeypatch.setattr(auth_template, "ensure_auth_seeds", fake_ensure)
    rc = cli.main([
        "auth", "--source", "T",
        "--operator-phones", "9111111111,9222222222,9333333333",
    ])
    assert rc == 0
    assert captured["source"] == "T"
    assert captured["phones"] == ["9111111111", "9222222222", "9333333333"]


def test_auth_subcommand_legacy_singular_phone(monkeypatch):
    from booking_bot.orchestrator import cli, auth_template
    captured = {}

    def fake_ensure(source, phones):
        captured["phones"] = phones
        return {"op1": None}

    monkeypatch.setattr(auth_template, "ensure_auth_seeds", fake_ensure)
    rc = cli.main([
        "auth", "--source", "T", "--operator-phone", "9111111111",
    ])
    assert rc == 0
    assert captured["phones"] == ["9111111111"]


def test_auth_subcommand_rejects_malformed_phones(monkeypatch, capsys):
    from booking_bot.orchestrator import cli
    with pytest.raises(SystemExit):
        cli.main([
            "auth", "--source", "T",
            "--operator-phones", "abc,9111111111",
        ])


def test_auth_subcommand_rejects_duplicate_phones(monkeypatch):
    from booking_bot.orchestrator import cli
    with pytest.raises(SystemExit):
        cli.main([
            "auth", "--source", "T",
            "--operator-phones", "9111111111,9111111111",
        ])
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_cli.py::test_auth_subcommand_parses_operator_phones_list tests/test_orchestrator_cli.py::test_auth_subcommand_legacy_singular_phone tests/test_orchestrator_cli.py::test_auth_subcommand_rejects_malformed_phones tests/test_orchestrator_cli.py::test_auth_subcommand_rejects_duplicate_phones -v
```

Expected: FAIL with unknown-argument errors or because `ensure_auth_seeds` isn't called.

- [ ] **Step 4: Implement**

In `booking_bot/orchestrator/cli.py`, add a phone-list parser helper near the top (after imports):

```python
def _parse_operator_phones(raw: str) -> list[str]:
    """Parse a comma-separated phone list: '9111111111,9222222222'.
    Validates each entry is exactly 10 digits and rejects duplicates.
    Raises argparse.ArgumentTypeError for any failure so argparse
    prints a clean error and exits with status 2."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("operator phone list is empty")
    if len(parts) > 10:
        raise argparse.ArgumentTypeError(
            f"at most 10 operator phones supported; got {len(parts)}"
        )
    for p in parts:
        if not (p.isdigit() and len(p) == 10):
            raise argparse.ArgumentTypeError(
                f"operator phone must be exactly 10 digits; got {p!r}"
            )
    if len(set(parts)) != len(parts):
        raise argparse.ArgumentTypeError("duplicate operator phone in list")
    return parts
```

Update the `auth` subparser (currently lines 56–58 in `build_parser`):

```python
    auth = sub.add_parser("auth", help="pre-authenticate operator auth-seed profiles")
    auth.add_argument("--source", required=True)
    phones_group = auth.add_mutually_exclusive_group(required=True)
    phones_group.add_argument(
        "--operator-phones", type=_parse_operator_phones, default=None,
        help="comma-separated HPCL operator phones; one auth-seed per phone "
             "(slots op1..opK)",
    )
    phones_group.add_argument(
        "--operator-phone", default=None,
        help="legacy single-phone form; implies slot op1",
    )
```

Update the `main()` dispatch for the `auth` command (currently around line 160):

```python
    if args.command == "auth":
        if args.operator_phones is not None:
            phones = args.operator_phones
        else:
            if args.operator_phone is None:
                ap.error("auth requires --operator-phones or --operator-phone")
            # Single-phone form may also need basic validation.
            phone = args.operator_phone
            if not (phone.isdigit() and len(phone) == 10):
                ap.error(f"--operator-phone must be 10 digits; got {phone!r}")
            phones = [phone]
        seeds = auth_template.ensure_auth_seeds(args.source, phones)
        for slot, path in seeds.items():
            print(f"[orchestrator] auth seed {slot} ready: {path}")
        return 0
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_cli.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/cli.py tests/test_orchestrator_cli.py
git commit -m "feat(cli): orchestrator auth --operator-phones"
```

---

## Task 11: CLI `start` subcommand accepts `--operator-phones` + verifies seed metadata

**Files:**
- Modify: `booking_bot/orchestrator/cli.py`
- Modify: `tests/test_orchestrator_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_start_subcommand_multi_operator_plumbs_phones_to_splitter(
    tmp_path, monkeypatch,
):
    """start --operator-phones p1,p2,p3 --clones-per-operator 3 calls
    splitter.split with those args and then clone_to_chunks, then
    spawn_chunk."""
    from booking_bot.orchestrator import cli, splitter, auth_template, spawner
    from booking_bot.orchestrator.splitter import ChunkSpec
    from booking_bot import config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")

    # Seed all three op-slots on disk so the start-time verification passes.
    from datetime import datetime, timezone
    for slot, phone in (
        ("op1", "9111111111"),
        ("op2", "9222222222"),
        ("op3", "9333333333"),
    ):
        seed = tmp_path / f".chromium-profile-MULTI-{slot}-auth-seed"
        seed.mkdir(parents=True)
        (seed / "last_auth.json").write_text(
            '{"auth_at_utc": "' + datetime.now(timezone.utc).isoformat() + '"}',
            encoding="utf-8",
        )
        (seed / "seed_phone.json").write_text(
            '{"operator_phone": "' + phone + '"}',
            encoding="utf-8",
        )

    split_calls = {}

    def fake_split(source, input_file, **kwargs):
        split_calls["source"] = source
        split_calls["kwargs"] = kwargs
        return [
            ChunkSpec(
                source=source, chunk_id=f"{source}-{i:03d}", chunk_index=i,
                input_path=tmp_path / f"{i}.xlsx",
                profile_suffix=f"{source}-{i:03d}",
                heartbeat_path=tmp_path / f"{i}.heartbeat.json",
                row_count=3,
                operator_slot=f"op{((i - 1) // 3) + 1}",
                operator_phone=kwargs["operator_phones"][((i - 1) // 3)],
            )
            for i in range(1, 10)
        ]

    clone_calls = []

    def fake_clone(source, chunks):
        clone_calls.append((source, chunks))

    spawn_calls = []

    class FakeHandle:
        def __init__(self):
            self.popen = None

    def fake_spawn(spec, *, headed):
        spawn_calls.append(spec)
        return FakeHandle()

    monkeypatch.setattr(splitter, "split", fake_split)
    monkeypatch.setattr(auth_template, "clone_to_chunks", fake_clone)
    monkeypatch.setattr(spawner, "spawn_chunk", fake_spawn)
    monkeypatch.setattr(cli, "_spawn_chunk", fake_spawn)

    inp = tmp_path / "file.xlsx"
    inp.write_text("fake")

    rc = cli.main([
        "start", "--source", "MULTI", "--input", str(inp),
        "--operator-phones", "9111111111,9222222222,9333333333",
        "--clones-per-operator", "3",
        "--no-monitor",
    ])
    assert rc == 0
    assert split_calls["kwargs"]["operator_phones"] == [
        "9111111111", "9222222222", "9333333333",
    ]
    assert split_calls["kwargs"]["clones_per_operator"] == 3
    assert len(spawn_calls) == 9
    assert len(clone_calls) == 1


def test_start_subcommand_fails_when_seed_missing(tmp_path, monkeypatch):
    from booking_bot.orchestrator import cli
    from booking_bot import config, exceptions

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")

    inp = tmp_path / "file.xlsx"
    inp.write_text("fake")

    with pytest.raises(exceptions.AuthSeedMissing):
        cli.main([
            "start", "--source", "NOSEED", "--input", str(inp),
            "--operator-phones", "9111111111,9222222222",
            "--clones-per-operator", "3",
            "--no-monitor",
        ])


def test_start_subcommand_fails_when_seed_phone_mismatches(
    tmp_path, monkeypatch,
):
    """If operator passes phones in a different order than auth was run,
    the seed_phone.json sidecars no longer match → loud error."""
    from booking_bot.orchestrator import cli
    from booking_bot import config, exceptions
    from datetime import datetime, timezone

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")

    # Seed op1 with phone A, op2 with phone B.
    for slot, phone in (("op1", "9111111111"), ("op2", "9222222222")):
        seed = tmp_path / f".chromium-profile-MULTI-{slot}-auth-seed"
        seed.mkdir(parents=True)
        (seed / "last_auth.json").write_text(
            '{"auth_at_utc": "' + datetime.now(timezone.utc).isoformat() + '"}',
            encoding="utf-8",
        )
        (seed / "seed_phone.json").write_text(
            '{"operator_phone": "' + phone + '"}', encoding="utf-8",
        )

    inp = tmp_path / "file.xlsx"
    inp.write_text("fake")

    # Pass them in reverse order — should mismatch.
    with pytest.raises(exceptions.AuthSeedMissing, match="mismatch"):
        cli.main([
            "start", "--source", "MULTI", "--input", str(inp),
            "--operator-phones", "9222222222,9111111111",
            "--clones-per-operator", "1",
            "--no-monitor",
        ])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_cli.py -k "multi_operator or seed_missing or seed_phone_mismatches" -v
```

Expected: FAIL with unknown-argument errors and missing `AuthSeedMissing` exception.

- [ ] **Step 3: Implement — new exception**

In `booking_bot/exceptions.py`, append:

```python
class AuthSeedMissing(BookingBotError):
    """orchestrator/cli.py: start-time verification found that one or
    more operator slots' auth seeds are missing, stale, or seeded with
    a different operator phone than the one passed to --operator-phones.
    Caller prints the list and exits; operator must rerun `orchestrator
    auth` before retrying."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            f"auth seeds missing or mismatched for: {', '.join(missing)}"
        )
```

- [ ] **Step 4: Implement — CLI wiring**

In `booking_bot/orchestrator/cli.py`, extend the `start` subparser (currently lines 40–54):

```python
    start = sub.add_parser("start", help="split, clone auth, spawn chunks")
    start.add_argument("--source", required=True,
                       help="operator-chosen source name (alphanumeric, "
                            "1-28 chars)")
    start.add_argument("--input", required=True, type=Path,
                       help="path to the input xlsx to split")
    parallel = start.add_mutually_exclusive_group()
    parallel.add_argument("--chunk-size", type=int, default=None)
    parallel.add_argument("--instances",  type=int, default=None)
    start.add_argument(
        "--operator-phones", type=_parse_operator_phones, default=None,
        help="comma-separated HPCL operator phones; enables multi-operator "
             "mode. Total parallelism = len(phones) * --clones-per-operator. "
             "When set, --chunk-size/--instances are ignored.",
    )
    start.add_argument(
        "--clones-per-operator", type=int, default=3,
        help="cloned bot instances per operator phone (1-3, default 3)",
    )
    visibility = start.add_mutually_exclusive_group()
    visibility.add_argument("--headed", action="store_true")
    visibility.add_argument("--headless", dest="headed", action="store_false")
    start.set_defaults(headed=False)
    start.add_argument("--no-monitor", action="store_true",
                       help="skip the automatic monitor handoff after spawn")
```

Add a seed-verification helper at module level:

```python
def _verify_operator_seeds(
    source: str, operator_phones: list[str],
) -> None:
    """For each operator slot, check that the seed exists, is fresh, and
    has a seed_phone.json that matches the passed phone. Raises
    exceptions.AuthSeedMissing with a list of failing slots."""
    max_age_s = float(
        config.AUTH_COOLDOWN_S - config.ORCHESTRATOR_AUTH_SEED_BUFFER_S
    )
    missing: list[str] = []
    for i, phone in enumerate(operator_phones):
        slot = f"op{i + 1}"
        seed = auth_template._seed_path(source, slot)
        if not seed.exists():
            missing.append(f"{slot} (no seed dir)")
            continue
        if not auth_template._auth_fresh(seed, max_age_s=max_age_s):
            missing.append(f"{slot} (seed stale or unparseable)")
            continue
        recorded = auth_template._read_seed_phone(source, slot)
        if recorded != phone:
            missing.append(
                f"{slot} (mismatch: seeded for {recorded}, passed {phone})"
            )
    if missing:
        raise exceptions.AuthSeedMissing(missing)
```

Add `from booking_bot import exceptions` at the top of `cli.py` if not already imported.

Update `run_start` (currently lines 112–148) to branch on `--operator-phones`:

```python
def run_start(
    *,
    source: str,
    input_file: Path,
    chunk_size: int | None,
    num_chunks: int | None,
    operator_phones: list[str] | None,
    clones_per_operator: int,
    headed: bool,
    no_monitor: bool,
) -> int:
    """Top-level start handler. Lock → split → auth seed verify → clone →
    spawn. Returns a shell exit code."""
    if operator_phones is None and chunk_size is None and num_chunks is None:
        chunk_size = 500  # default

    lock_path = _acquire_lock(source)
    try:
        if operator_phones is not None:
            # Multi-operator: verify seeds first so we fail loudly BEFORE
            # splitting any workbooks or creating any state.
            _verify_operator_seeds(source, operator_phones)
            chunks = splitter.split(
                source, input_file,
                operator_phones=operator_phones,
                clones_per_operator=clones_per_operator,
            )
            print(
                f"[orchestrator] multi-operator split into {len(chunks)} "
                f"chunks across {len(operator_phones)} operators "
                f"({clones_per_operator} per operator)",
                flush=True,
            )
        else:
            chunks = splitter.split(
                source, input_file,
                chunk_size=chunk_size, num_chunks=num_chunks,
            )
            print(
                f"[orchestrator] split into {len(chunks)} chunks", flush=True,
            )
            _ensure_auth_seed(source)

        _clone_to_chunks(source, chunks)

        handles = []
        for spec in chunks:
            handle = _spawn_chunk(spec, headed=headed)
            handles.append(handle)
            time.sleep(0.5)  # gentle stagger so HPCL isn't slammed by simultaneous SSL handshakes
        print(f"[orchestrator] spawned {len(handles)} chunks", flush=True)
    finally:
        _release_lock(lock_path)

    if no_monitor:
        return 0
    return monitor.run_monitor(source_filter=source)
```

Update the `main()` dispatch for `start` (currently lines 154–159):

```python
    if args.command == "start":
        return run_start(
            source=args.source, input_file=args.input,
            chunk_size=args.chunk_size, num_chunks=args.instances,
            operator_phones=args.operator_phones,
            clones_per_operator=args.clones_per_operator,
            headed=args.headed, no_monitor=args.no_monitor,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_cli.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/cli.py booking_bot/exceptions.py tests/test_orchestrator_cli.py
git commit -m "feat(cli): orchestrator start --operator-phones + seed verification"
```

---

## Task 12: Monitor — operator slot column + re-auth banner

**Files:**
- Modify: `booking_bot/orchestrator/monitor.py`
- Modify: `tests/test_orchestrator_monitor.py`

- [ ] **Step 1: Write the failing tests**

Read `tests/test_orchestrator_monitor.py` first to match existing fixture patterns, then append:

```python
def _make_hb(chunk_id, *, phase="booking", slot="op1",
             idle_secs=0.0, rows_done=0):
    from datetime import datetime, timedelta, timezone
    from booking_bot.orchestrator.heartbeat import Heartbeat
    last = (datetime.now(tz=timezone.utc) - timedelta(seconds=idle_secs)).isoformat()
    return Heartbeat(
        source="T", chunk_id=chunk_id, pid=123,
        input_file="in.xlsx", profile_suffix=chunk_id,
        phase=phase, rows_total=10, rows_done=rows_done, rows_issue=0,
        rows_pending=10 - rows_done, current_row_idx=None, current_phone=None,
        started_at="2026-04-16T00:00:00+00:00",
        last_activity_at=last,
        command=["python"], exit_code=None, last_error=None,
        operator_slot=slot,
    )


def test_build_table_shows_operator_slot_column():
    from booking_bot.orchestrator.monitor import build_table
    hbs = [_make_hb("T-001", slot="op1"), _make_hb("T-002", slot="op2")]
    table = build_table(hbs)
    # rich.Table.columns has .header attribute for each column.
    headers = [c.header for c in table.columns]
    assert "Op" in headers


def test_build_operator_reauth_banner_flags_stuck_slot():
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-001", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-002", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-003", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-004", slot="op2", phase="booking", idle_secs=2),
    ]
    banner = build_operator_reauth_banner(hbs)
    assert "op1" in banner
    assert "3 chunks" in banner
    assert "op2" not in banner


def test_build_operator_reauth_banner_empty_when_all_healthy():
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-001", slot="op1", phase="booking", idle_secs=0),
        _make_hb("T-002", slot="op2", phase="booking", idle_secs=0),
    ]
    assert build_operator_reauth_banner(hbs) == ""


def test_build_operator_reauth_banner_ignores_single_stuck_chunk():
    """One stuck chunk in a slot isn't an operator-wide problem — don't
    cry wolf."""
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-001", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-002", slot="op1", phase="booking", idle_secs=0),
    ]
    assert build_operator_reauth_banner(hbs) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_monitor.py -v
```

Expected: FAIL — no `Op` column and `build_operator_reauth_banner` doesn't exist.

- [ ] **Step 3: Implement**

In `booking_bot/orchestrator/monitor.py`, update `build_table` (currently around line 62) to add the column:

```python
def build_table(hbs: Iterable[Heartbeat]) -> Table:
    """Render a rich.Table for the monitor view. Pure — no I/O, no state."""
    table = Table(title="Orchestrator — chunk status", expand=True)
    table.add_column("Chunk", no_wrap=True)
    table.add_column("Op", no_wrap=True)
    table.add_column("PID", justify="right")
    table.add_column("Phase")
    table.add_column("Done", justify="right")
    table.add_column("Issue", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Progress")
    table.add_column("Idle", justify="right")

    for hb in hbs:
        color = _PHASE_COLORS.get(hb.phase, "white")
        phase_cell = f"[{color}]{hb.phase}[/{color}]"
        idle = _idle_seconds(hb)
        idle_cell = _fmt_idle_seconds(idle)
        if idle > 120:
            idle_cell = f"[yellow]{idle_cell}[/yellow]"
        table.add_row(
            hb.chunk_id,
            hb.operator_slot or "-",
            str(hb.pid if hb.pid > 0 else "-"),
            phase_cell,
            str(hb.rows_done),
            str(hb.rows_issue),
            str(hb.rows_pending),
            _progress_str(hb),
            idle_cell,
        )
    return table
```

Add the new banner function below `build_totals_line`:

```python
def build_operator_reauth_banner(hbs: Iterable[Heartbeat]) -> str:
    """Return a single high-visibility warning line if ≥2 chunks belonging
    to the same operator_slot are stuck in an auth-pending state for
    more than 60s. Returns '' when nothing is stuck — callers can check
    truthiness to decide whether to render.

    The detection heuristic: phase=='authenticating' and idle > 60s. This
    is the exact shape of the cooldown_wait quiet-retry loop when HPCL
    has killed the operator's sessions server-side; the operator's
    correct response is to re-auth *that* slot and let shared_auth
    propagate."""
    by_slot: dict[str, int] = {}
    for hb in hbs:
        if hb.operator_slot is None:
            continue
        if hb.phase != "authenticating":
            continue
        if _idle_seconds(hb) <= 60:
            continue
        by_slot[hb.operator_slot] = by_slot.get(hb.operator_slot, 0) + 1
    stuck_slots = sorted(
        (slot for slot, n in by_slot.items() if n >= 2)
    )
    if not stuck_slots:
        return ""
    parts = []
    for slot in stuck_slots:
        parts.append(f"operator {slot} NEEDS RE-AUTH ({by_slot[slot]} chunks waiting)")
    return "!! " + " | ".join(parts) + " !!"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_monitor.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd D:/workspace/booking_bot
git add booking_bot/orchestrator/monitor.py tests/test_orchestrator_monitor.py
git commit -m "feat(monitor): operator_slot column + re-auth banner"
```

---

## Task 13: Integration test — full K=2 orchestrator flow (mocked)

**Files:**
- Create: `tests/test_orchestrator_multi_operator_integration.py`

This integration test exercises the end-to-end split→verify→clone→spawn path with K=2 operators, using monkey-patches for the browser-touching pieces so no real Chromium launches. It's the safety net for the full workflow tonight.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_multi_operator_integration.py`:

```python
"""End-to-end orchestrator flow for multi-operator mode, K=2 M=2. Uses
openpyxl for input, monkey-patches out _interactive_auth_seed and the
spawner so no real Chromium launches. Ensures that all the surface
changes (splitter, auth_template, clone_to_chunks, spawner, cli) play
together correctly."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pytest

from booking_bot import config
from booking_bot.orchestrator import auth_template, cli, spawner


@pytest.fixture()
def e2e_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")
    monkeypatch.setattr(
        config, "ORCHESTRATOR_LOGS_DIR", tmp_path / "logs" / "orchestrator",
    )
    return tmp_path


def _make_xlsx(path: Path, n_rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["consumer_no", "phone"])
    for i in range(n_rows):
        ws.append([f"C{i+1}", f"98765{i:05d}"])
    wb.save(path)


def test_multi_operator_start_end_to_end(e2e_env, monkeypatch):
    # --- Pre-seed two auth slots on disk as if `orchestrator auth` just ran.
    for slot, phone in (("op1", "9111111111"), ("op2", "9222222222")):
        seed = e2e_env / f".chromium-profile-LALJI-{slot}-auth-seed"
        seed.mkdir(parents=True)
        (seed / "Default").mkdir()
        (seed / "Default" / "Cookies").write_bytes(f"{slot}-cookies".encode())
        (seed / "last_auth.json").write_text(
            json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        (seed / "seed_phone.json").write_text(
            json.dumps({"operator_phone": phone}), encoding="utf-8",
        )

    # --- Fake spawner to avoid launching real bot processes.
    spawned: list = []

    class FakeHandle:
        def __init__(self):
            self.popen = None

    def fake_spawn(spec, *, headed=False):
        spawned.append(spec)
        return FakeHandle()

    monkeypatch.setattr(cli, "_spawn_chunk", fake_spawn)

    # --- Build a 4-row input and run the CLI with K=2 M=2 = 4 chunks.
    inp = e2e_env / "Input" / "lalji-test.xlsx"
    _make_xlsx(inp, n_rows=4)

    rc = cli.main([
        "start", "--source", "LALJI",
        "--input", str(inp),
        "--operator-phones", "9111111111,9222222222",
        "--clones-per-operator", "2",
        "--no-monitor",
    ])

    # --- Assertions.
    assert rc == 0
    assert len(spawned) == 4

    # Chunks 1-2 belong to op1, chunks 3-4 belong to op2.
    assert [s.operator_slot for s in spawned] == ["op1", "op1", "op2", "op2"]
    assert [s.operator_phone for s in spawned] == [
        "9111111111", "9111111111", "9222222222", "9222222222",
    ]

    # Each chunk's cloned profile dir carries the right operator seed's
    # cookie marker.
    for spec in spawned:
        target = e2e_env / f".chromium-profile-{spec.profile_suffix}"
        assert target.exists()
        expected = f"{spec.operator_slot}-cookies".encode()
        assert (target / "Default" / "Cookies").read_bytes() == expected

    # Each chunk file was actually written.
    for spec in spawned:
        assert spec.input_path.exists()
```

- [ ] **Step 2: Run the test to verify it fails (if it fails) or passes (if the previous tasks were enough)**

```bash
cd D:/workspace/booking_bot && pytest tests/test_orchestrator_multi_operator_integration.py -v
```

Expected: This test should pass as-is once Tasks 1–12 are complete. If it fails, the failure reveals an integration seam that wasn't exercised by the unit tests — fix in place before committing.

- [ ] **Step 3: If it passes, commit**

```bash
cd D:/workspace/booking_bot
git add tests/test_orchestrator_multi_operator_integration.py
git commit -m "test(orchestrator): e2e multi-operator K=2 M=2 integration"
```

- [ ] **Step 4: Full test suite sanity check**

```bash
cd D:/workspace/booking_bot && pytest -x
```

Expected: every existing test plus every new test passes (≥ 282 + ~30 new tests, 0 failures, 0 errors).

---

## Task 14: Operator runbook stub

**Files:**
- Create: `docs/runbooks/multi-operator-orchestrator.md`

Minimal one-pager for the operator so they don't have to re-derive the commands at 3 AM.

- [ ] **Step 1: Create the runbook**

```bash
mkdir -p D:/workspace/booking_bot/docs/runbooks
```

Create `docs/runbooks/multi-operator-orchestrator.md`:

```markdown
# Multi-Operator Orchestrator — Operator Runbook

One-page reference for running the K-operator orchestrator.

## Pre-flight

- You have K HPCL operator phones enrolled (usually K=2 or 3).
- Your input file is under `Input/` (e.g. `Input/lalji-final-1604-52am.xlsx`).
- No other `orchestrator start` is currently running for the same source.

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
```

- [ ] **Step 2: Commit**

```bash
cd D:/workspace/booking_bot
git add docs/runbooks/multi-operator-orchestrator.md
git commit -m "docs: multi-operator orchestrator operator runbook"
```

---

## Final verification

- [ ] **Run full test suite one last time**

```bash
cd D:/workspace/booking_bot && pytest -q
```

Expected: all tests pass. If anything fails, the failure belongs to the most-recently-modified task's test set — open that task, review the relevant diff, re-run the task's own test file to narrow the failure, and patch.

- [ ] **Verify the orchestrator CLI help text reflects the new args**

```bash
cd D:/workspace/booking_bot && python -m booking_bot.orchestrator auth --help
cd D:/workspace/booking_bot && python -m booking_bot.orchestrator start --help
```

Expected: `--operator-phones` appears in both help outputs; `--clones-per-operator` appears in `start --help`.

- [ ] **Commit any follow-up fixes as separate `fix(multi-op): ...` commits.**

---

## Spec-coverage self-check

| Spec section | Task # |
|---|---|
| Prereq: `_auth_fresh` bug fix | Task 1 |
| Per-operator seed paths | Task 2 |
| Seed metadata (phone recording) | Task 2 |
| `ensure_auth_seeds` plural | Task 3 |
| Legacy `ensure_auth_seed` wrapper | Task 3 |
| `ChunkSpec.operator_slot` / `operator_phone` | Task 4 |
| `split()` multi-operator mode with contiguous bucketing | Task 4 |
| Clones-per-operator cap (M ≤ 3) | Task 4 |
| `clone_to_chunks` routes by `chunk.operator_slot` | Task 5 |
| `clone_to_chunks` fails loudly when seed missing | Task 5 |
| `config.OPERATOR_PHONE` env override | Task 6 |
| `OPERATOR_PHONE_ENV` / `OPERATOR_SLOT_ENV` constants | Task 6 |
| `browser._shared_auth_path` slot-aware | Task 7 |
| Defensive slot regex (path traversal) | Task 7 |
| Spawner env vars | Task 8 |
| `Heartbeat.operator_slot` optional field | Task 9 |
| Back-compat heartbeat parsing | Task 9 |
| `auth --operator-phones` | Task 10 |
| Legacy `--operator-phone` singular | Task 10 |
| Phone list validation | Task 10 |
| `start --operator-phones` / `--clones-per-operator` | Task 11 |
| Start-time seed verification (`AuthSeedMissing`) | Task 11 |
| Phone mismatch detection | Task 11 |
| Monitor slot column | Task 12 |
| Re-auth banner for stuck operators | Task 12 |
| Banner ignores lone stuck chunk | Task 12 |
| E2E K=2 integration test | Task 13 |
| Operator runbook | Task 14 |

All spec requirements covered.
