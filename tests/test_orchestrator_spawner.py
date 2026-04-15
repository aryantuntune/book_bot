"""Unit tests for orchestrator/spawner.py. Uses tests/fixtures/orchestrator
/fake_bot.py as a stand-in for the real booking_bot child so we exercise
the subprocess lifecycle without launching a browser."""
import sys
import time
from pathlib import Path

import pytest

from booking_bot import config
from booking_bot.orchestrator import spawner
from booking_bot.orchestrator.splitter import ChunkSpec


FAKE_BOT = (
    Path(__file__).parent / "fixtures" / "orchestrator" / "fake_bot.py"
).resolve()


@pytest.fixture()
def spawner_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORCHESTRATOR_LOGS_DIR", tmp_path / "logs" / "orchestrator")
    return tmp_path


def _make_spec(tmp_path: Path, chunk_id: str = "FAKE-001") -> ChunkSpec:
    return ChunkSpec(
        source="FAKE",
        chunk_id=chunk_id,
        chunk_index=1,
        input_path=tmp_path / "fake.xlsx",
        profile_suffix=chunk_id,
        heartbeat_path=tmp_path / "runs" / "FAKE" / f"{chunk_id}.heartbeat.json",
        row_count=3,
    )


def test_spawn_chunk_runs_fake_bot_to_completion(spawner_env, monkeypatch):
    # Redirect spawner to use the fake bot script instead of booking_bot.
    monkeypatch.setenv("BOOKING_BOT_SPAWNER_CMD_OVERRIDE", f"{sys.executable}|{FAKE_BOT}")
    (spawner_env / "fake.xlsx").write_text("")
    spec = _make_spec(spawner_env)
    handle = spawner.spawn_chunk(spec, headed=False)
    try:
        rc = handle.popen.wait(timeout=10)
    finally:
        if handle.popen.poll() is None:
            handle.popen.kill()
    assert rc == 0
    assert spec.heartbeat_path.exists()


def test_spawn_chunk_sets_env_vars(spawner_env, monkeypatch):
    monkeypatch.setenv("BOOKING_BOT_SPAWNER_CMD_OVERRIDE", f"{sys.executable}|{FAKE_BOT}")
    (spawner_env / "fake.xlsx").write_text("")
    spec = _make_spec(spawner_env)
    handle = spawner.spawn_chunk(spec, headed=False)
    handle.popen.wait(timeout=10)
    env_file = spec.heartbeat_path.parent / f"{spec.chunk_id}.env.txt"
    assert env_file.exists()
    text = env_file.read_text(encoding="utf-8")
    assert f"BOOKING_BOT_SOURCE={spec.source}" in text
    assert f"BOOKING_BOT_CHUNK_ID={spec.chunk_id}" in text
    assert str(spec.heartbeat_path) in text


def test_spawn_chunk_writes_initial_heartbeat_immediately(spawner_env, monkeypatch):
    # Even before the child writes anything, the orchestrator's own
    # initial heartbeat should exist so the monitor sees the chunk.
    monkeypatch.setenv("BOOKING_BOT_SPAWNER_CMD_OVERRIDE", f"{sys.executable}|{FAKE_BOT}")
    (spawner_env / "fake.xlsx").write_text("")
    spec = _make_spec(spawner_env)
    handle = spawner.spawn_chunk(spec, headed=False)
    try:
        assert spec.heartbeat_path.exists()
    finally:
        handle.popen.wait(timeout=10)


def test_spawn_chunk_headless_flag_controls_cmd(spawner_env, monkeypatch):
    monkeypatch.setenv("BOOKING_BOT_SPAWNER_CMD_OVERRIDE", f"{sys.executable}|{FAKE_BOT}")
    (spawner_env / "fake.xlsx").write_text("")

    calls = []
    real_popen = spawner.subprocess.Popen

    class SpyPopen(real_popen):
        def __init__(self, cmd, *args, **kwargs):
            calls.append(list(cmd))
            super().__init__(cmd, *args, **kwargs)

    monkeypatch.setattr(spawner.subprocess, "Popen", SpyPopen)
    try:
        h1 = spawner.spawn_chunk(_make_spec(spawner_env, "FAKE-001"), headed=False)
        h1.popen.wait(timeout=10)
        h2 = spawner.spawn_chunk(_make_spec(spawner_env, "FAKE-002"), headed=True)
        h2.popen.wait(timeout=10)
    finally:
        pass
    # The fake bot doesn't care about --headless, but the spawner still
    # appends it for headed=False. Check the command list.
    assert any("--headless" in c for c in calls)
    assert any("--headless" not in c for c in calls)


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


def test_kill_chunk_terminates_a_long_running_child(spawner_env, monkeypatch):
    slow_script = spawner_env / "slow.py"
    slow_script.write_text("import time; time.sleep(30)\n")
    monkeypatch.setenv(
        "BOOKING_BOT_SPAWNER_CMD_OVERRIDE",
        f"{sys.executable}|{slow_script}",
    )
    (spawner_env / "fake.xlsx").write_text("")
    spec = _make_spec(spawner_env)
    handle = spawner.spawn_chunk(spec, headed=False)
    start = time.monotonic()
    rc = spawner.kill_chunk(handle, timeout_s=5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 8.0
    assert handle.popen.poll() is not None  # actually exited
    assert rc is not None
