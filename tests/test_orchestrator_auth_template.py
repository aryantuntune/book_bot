"""Unit tests for orchestrator/auth_template.py.

clone_to_chunks is tested here with a synthetic seed profile and fake
ChunkSpecs. ensure_auth_seed's interactive path (Path C) is NOT tested
here — it needs a real browser; tests stub out _interactive_auth_seed
so paths A and B can be tested without touching Playwright."""
import json
import time
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


def _make_seed_profile(root: Path, source: str, *, fresh: bool = True) -> Path:
    """Create a .chromium-profile-<source>-auth-seed/ dir with a
    last_auth.json and one fake singleton lock file."""
    seed = root / f".chromium-profile-{source}-auth-seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "Default").mkdir(exist_ok=True)
    (seed / "Default" / "Cookies").write_bytes(b"fake-cookie-db")
    (seed / "SingletonLock").write_text("fake-lock")
    (seed / "SingletonCookie").write_text("fake-cookie-lock")
    ts = time.time() - 3600 if fresh else time.time() - 30 * 3600
    (seed / "last_auth.json").write_text(
        json.dumps({"timestamp": ts}), encoding="utf-8"
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
        json.dumps({"timestamp": time.time()}), encoding="utf-8"
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
    seed_dir = auth_env / ".chromium-profile-TEST-auth-seed"

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
    assert result == auth_env / ".chromium-profile-TEST-auth-seed"
    assert not launched["called"]


def test_ensure_auth_seed_path_b_copies_from_main_profile(auth_env, monkeypatch):
    # Seed missing, but main .chromium-profile has fresh auth.
    main_profile = auth_env / ".chromium-profile"
    main_profile.mkdir()
    (main_profile / "Default").mkdir()
    (main_profile / "Default" / "Cookies").write_bytes(b"main-cookies")
    (main_profile / "last_auth.json").write_text(
        json.dumps({"timestamp": time.time()}), encoding="utf-8"
    )

    def explode(*args, **kwargs):
        raise AssertionError("should not launch browser on Path B")

    monkeypatch.setattr(auth_template, "_interactive_auth_seed", explode)
    result = auth_template.ensure_auth_seed("TEST")
    assert result == auth_env / ".chromium-profile-TEST-auth-seed"
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
