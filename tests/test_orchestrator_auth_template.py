"""Unit tests for orchestrator/auth_template.py.

clone_to_chunks is tested here with a synthetic seed profile and fake
ChunkSpecs. ensure_auth_seed's interactive path (Path C) is NOT tested
here — it needs a real browser; tests stub out _interactive_auth_seed
so paths A and B can be tested without touching Playwright."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from booking_bot import config, exceptions
from booking_bot.orchestrator import auth_template
from booking_bot.orchestrator.splitter import ChunkSpec


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")
    return tmp_path


def _make_seed_profile(
    root: Path, source: str, *, fresh: bool = True, slot: str = "op1",
) -> Path:
    """Create a .chromium-profile-<source>-<slot>-auth-seed/ dir with a
    last_auth.json and one fake singleton lock file."""
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


def _make_chunk_spec(root: Path, source: str, idx: int) -> ChunkSpec:
    return ChunkSpec(
        source=source,
        chunk_id=f"{source}-{idx:03d}",
        chunk_index=idx,
        input_path=root / "Input" / "chunks" / source / f"{source}-{idx:03d}.xlsx",
        profile_suffix=f"{source}-{idx:03d}",
        heartbeat_path=root / "data" / "runs" / source / f"{source}-{idx:03d}.heartbeat.json",
        row_count=5,
    )


def test_clone_to_chunks_copies_seed_to_each_chunk_profile(auth_env):
    _make_seed_profile(auth_env, "TEST", fresh=True)
    chunks = [_make_chunk_spec(auth_env, "TEST", i) for i in (1, 2, 3)]
    auth_template.clone_to_chunks("TEST", chunks)
    for c in chunks:
        target = auth_env / f".chromium-profile-{c.profile_suffix}"
        assert target.exists()
        assert (target / "Default" / "Cookies").read_bytes() == b"fake-cookie-db"
        assert (target / "last_auth.json").exists()


def test_clone_to_chunks_scrubs_singleton_locks(auth_env):
    _make_seed_profile(auth_env, "TEST", fresh=True)
    chunks = [_make_chunk_spec(auth_env, "TEST", 1)]
    auth_template.clone_to_chunks("TEST", chunks)
    target = auth_env / ".chromium-profile-TEST-001"
    assert not (target / "SingletonLock").exists()
    assert not (target / "SingletonCookie").exists()


def test_clone_to_chunks_skips_chunks_with_fresh_auth(auth_env):
    _make_seed_profile(auth_env, "TEST", fresh=True)
    # Pre-create target with a newer last_auth.json.
    target = auth_env / ".chromium-profile-TEST-001"
    target.mkdir(parents=True)
    (target / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )
    (target / "marker.txt").write_text("do-not-overwrite")
    chunks = [_make_chunk_spec(auth_env, "TEST", 1)]
    auth_template.clone_to_chunks("TEST", chunks)
    # Marker should still be there — no copytree ran.
    assert (target / "marker.txt").read_text() == "do-not-overwrite"


def test_clone_to_chunks_raises_aggregate_on_failures(auth_env, monkeypatch):
    _make_seed_profile(auth_env, "TEST", fresh=True)
    chunks = [_make_chunk_spec(auth_env, "TEST", i) for i in (1, 2)]

    calls = {"count": 0}
    orig_copytree = auth_template.shutil.copytree
    seed_dir = auth_env / ".chromium-profile-TEST-op1-auth-seed"

    def flaky_copytree(src, dst, *args, **kwargs):
        # Only fail on top-level clone calls (src == seed); shutil.copytree
        # recurses into itself for subdirs, so we must not double-count.
        if Path(src) == seed_dir:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk full")
        return orig_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(auth_template.shutil, "copytree", flaky_copytree)
    with pytest.raises(exceptions.AuthCloneFailed) as exc_info:
        auth_template.clone_to_chunks("TEST", chunks)
    assert len(exc_info.value.failures) == 1
    assert exc_info.value.failures[0][0] == "TEST-002"


def test_ensure_auth_seed_path_a_returns_fresh_seed_without_browser(
    auth_env, monkeypatch,
):
    # Seed already exists and is fresh: no browser launch.
    _make_seed_profile(auth_env, "TEST", fresh=True)
    launched = {"called": False}

    def explode(*args, **kwargs):
        launched["called"] = True
        raise AssertionError("should not launch browser on Path A")

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", explode)
    result = auth_template.ensure_auth_seed("TEST")
    assert result == auth_env / ".chromium-profile-TEST-op1-auth-seed"
    assert not launched["called"]


def test_ensure_auth_seed_path_b_copies_from_main_profile(auth_env, monkeypatch):
    # Seed missing, but main .chromium-profile has fresh auth.
    main_profile = auth_env / ".chromium-profile"
    main_profile.mkdir()
    (main_profile / "Default").mkdir()
    (main_profile / "Default" / "Cookies").write_bytes(b"main-cookies")
    (main_profile / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )

    def explode(*args, **kwargs):
        raise AssertionError("should not launch browser on Path B")

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", explode)
    result = auth_template.ensure_auth_seed("TEST")
    assert result == auth_env / ".chromium-profile-TEST-op1-auth-seed"
    assert (result / "Default" / "Cookies").read_bytes() == b"main-cookies"


def test_ensure_auth_seed_path_a_stale_seed_falls_through(auth_env, monkeypatch):
    # Old seed (> AUTH_COOLDOWN_S - buffer) AND no main profile ->
    # interactive path should be called.
    _make_seed_profile(auth_env, "TEST", fresh=False)
    called = {"interactive": False}

    def stub_interactive(src, **kwargs):
        called["interactive"] = True
        return auth_env / f".chromium-profile-{src}-auth-seed"

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", stub_interactive)
    auth_template.ensure_auth_seed("TEST")
    assert called["interactive"] is True


def test_auth_fresh_accepts_real_browser_format(auth_env):
    """Regression: browser.py writes {'auth_at_utc': ISO}, not
    {'timestamp': float}. _auth_fresh must recognize the real format."""
    seed = auth_env / ".chromium-profile-REAL-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    assert auth_template._auth_fresh(seed, max_age_s=3600) is True


def test_auth_fresh_rejects_stale_auth_at_utc(auth_env):
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


def test_ensure_auth_seeds_path_a_all_fresh(auth_env, monkeypatch):
    """All K seeds already fresh on disk → no browser launch, returns
    {slot: path} for each."""
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
    assert auth_template._read_seed_phone("MULTI", "op1") == "9111111111"
    assert auth_template._read_seed_phone("MULTI", "op2") == "9222222222"


def test_ensure_auth_seeds_writes_seed_phone_metadata(auth_env, monkeypatch):
    def stub_interactive(source, *, slot, operator_phone=None):
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


def test_ensure_auth_seeds_path_a_phone_mismatch_forces_re_seed(auth_env, monkeypatch):
    """Path A: fresh seed on disk whose seed_phone.json records a different
    phone → log warning, rmtree the stale seed, fall through to interactive
    re-auth for the new phone."""
    seed = auth_env / ".chromium-profile-MULTI-op1-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    (seed / "seed_phone.json").write_text(
        json.dumps({"operator_phone": "9999999999"}), encoding="utf-8",
    )

    interactive_calls = []

    def stub_interactive(source, *, slot, operator_phone=None):
        interactive_calls.append((source, slot, operator_phone))
        seed2 = auth_env / f".chromium-profile-{source}-{slot}-auth-seed"
        seed2.mkdir(parents=True, exist_ok=True)
        (seed2 / "last_auth.json").write_text(
            json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        return seed2

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", stub_interactive)
    auth_template.ensure_auth_seeds("MULTI", ["9111111111"])
    assert interactive_calls == [("MULTI", "op1", "9111111111")]
    assert auth_template._read_seed_phone("MULTI", "op1") == "9111111111"


def test_ensure_auth_seeds_path_a_phone_match_is_idempotent(auth_env, monkeypatch):
    """Path A: fresh seed whose seed_phone.json matches the passed phone →
    no interactive launch, no metadata overwrite. Returns {op1: seed}."""
    seed = auth_env / ".chromium-profile-MULTI-op1-auth-seed"
    seed.mkdir(parents=True)
    (seed / "last_auth.json").write_text(
        json.dumps({"auth_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    (seed / "seed_phone.json").write_text(
        json.dumps({"operator_phone": "9111111111"}), encoding="utf-8",
    )
    monkeypatch.setattr(
        auth_template, "_interactive_auth_seed",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not launch browser"),
        ),
    )
    seeds = auth_template.ensure_auth_seeds("MULTI", ["9111111111"])
    assert seeds == {"op1": seed}
    assert auth_template._read_seed_phone("MULTI", "op1") == "9111111111"


def test_ensure_auth_seeds_rejects_duplicate_phones(auth_env):
    with pytest.raises(ValueError, match="duplicate"):
        auth_template.ensure_auth_seeds("MULTI", ["9111111111", "9111111111"])


def test_ensure_auth_seeds_rejects_empty_list(auth_env):
    with pytest.raises(ValueError, match="non-empty"):
        auth_template.ensure_auth_seeds("MULTI", [])


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


def test_interactive_auth_seed_writes_last_auth_on_alive_state(
    auth_env, monkeypatch,
):
    """Regression for the 2026-04-17 bug: _interactive_auth_seed used to
    poll for last_auth.json but nothing in that path ever wrote the
    file. The fix observes detect_state and calls mark_auth_success
    when the chat lands in an alive state after operator login.

    Also regressions the second 2026-04-17 bug: HPCL's auth token lives
    in JS sessionStorage, which doesn't survive a browser restart.
    write_shared_auth_state captures it into shared_auth-<slot>.json
    while the live page is still open so the cloned chunk profiles can
    replay it via inject_shared_auth_cookies."""
    from booking_bot import browser, chat as chat_mod

    fake_ctx = type("Ctx", (), {"close": lambda self: None})()
    fake_pw = type("Pw", (), {"stop": lambda self: None})()
    fake_page = object()
    fake_frame = object()

    monkeypatch.setattr(
        browser, "start_browser",
        lambda headless, profile_suffix: (fake_pw, None, fake_ctx, fake_page),
    )
    monkeypatch.setattr(
        browser, "get_chat_frame",
        lambda page: fake_frame,
    )
    # Simulate operator completing the OTP on the 2nd poll.
    state_sequence = iter(["NEEDS_OPERATOR_OTP", "MAIN_MENU"])
    monkeypatch.setattr(
        chat_mod, "detect_state",
        lambda frame: next(state_sequence),
    )
    mark_calls = []
    monkeypatch.setattr(
        browser, "mark_auth_success",
        lambda: mark_calls.append(True),
    )
    # Capture the slot env var as write_shared_auth_state sees it —
    # this is what proves the fix routes the snapshot to
    # shared_auth-<slot>.json instead of the legacy file.
    observed_slot = {"value": "__unset__"}

    def fake_write_shared(page_arg):
        observed_slot["value"] = os.environ.get(config.OPERATOR_SLOT_ENV)

    monkeypatch.setattr(
        browser, "write_shared_auth_state", fake_write_shared,
    )
    monkeypatch.setattr(auth_template.time, "sleep", lambda _s: None)

    # Start with the env var unset so we can prove the function sets it
    # to the right slot then restores it to absent.
    monkeypatch.delenv(config.OPERATOR_SLOT_ENV, raising=False)

    path = auth_template._interactive_auth_seed(
        "INT", slot="op2", operator_phone="9222222222",
    )

    assert path == auth_env / ".chromium-profile-INT-op2-auth-seed"
    assert len(mark_calls) == 1, (
        "mark_auth_success must be called exactly once when detect_state "
        "lands on an alive state — this is what writes last_auth.json"
    )
    assert observed_slot["value"] == "op2", (
        f"write_shared_auth_state must see BOOKING_BOT_OPERATOR_SLOT='op2' "
        f"so the snapshot lands at shared_auth-op2.json; got "
        f"{observed_slot['value']!r}"
    )
    assert os.environ.get(config.OPERATOR_SLOT_ENV) is None, (
        "slot env var must be restored to its prior state after the "
        "write; otherwise subsequent ensure_auth_seeds iterations would "
        "see a stale slot"
    )


def test_interactive_auth_seed_raises_timeout_if_never_alive(
    auth_env, monkeypatch,
):
    """If the operator never completes login within the timeout window,
    AuthSeedTimeout must fire — never silently hang or fabricate a
    last_auth.json that wasn't earned."""
    from booking_bot import browser, chat as chat_mod

    fake_ctx = type("Ctx", (), {"close": lambda self: None})()
    fake_pw = type("Pw", (), {"stop": lambda self: None})()
    monkeypatch.setattr(
        browser, "start_browser",
        lambda headless, profile_suffix: (fake_pw, None, fake_ctx, object()),
    )
    monkeypatch.setattr(
        browser, "get_chat_frame",
        lambda page: object(),
    )
    monkeypatch.setattr(
        chat_mod, "detect_state",
        lambda frame: "NEEDS_OPERATOR_AUTH",  # never moves off the login screen
    )
    mark_calls = []
    monkeypatch.setattr(
        browser, "mark_auth_success",
        lambda: mark_calls.append(True),
    )
    # Shrink the timeout so the test actually terminates.
    monkeypatch.setattr(config, "ORCHESTRATOR_AUTH_TIMEOUT_S", 0.1)
    monkeypatch.setattr(auth_template.time, "sleep", lambda _s: None)

    with pytest.raises(exceptions.AuthSeedTimeout, match="op1"):
        auth_template._interactive_auth_seed(
            "INT", slot="op1", operator_phone="9111111111",
        )
    assert mark_calls == [], (
        "mark_auth_success must NOT be called when detect_state never "
        "reaches an alive state"
    )
