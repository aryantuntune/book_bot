"""Integration test: full `orchestrator start --no-monitor` flow using
fake_bot.py as the child. Exercises splitter + spawner + a stubbed
auth_template together."""
import json
import sys
import time
from pathlib import Path

import openpyxl
import pytest

from booking_bot import config
from booking_bot.orchestrator import cli as orch_cli


FAKE_BOT = (
    Path(__file__).parent / "fixtures" / "orchestrator" / "fake_bot.py"
).resolve()


@pytest.fixture()
def orch_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")
    monkeypatch.setattr(config, "RUNS_DIR",   tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "ORCHESTRATOR_LOGS_DIR", tmp_path / "logs" / "orch")

    # Stub auth — fake_bot doesn't need a real Chromium profile.
    monkeypatch.setattr(orch_cli, "_ensure_auth_seed",
                        lambda source: tmp_path / ".chromium-profile-noop")
    monkeypatch.setattr(orch_cli, "_clone_to_chunks", lambda source, chunks: None)

    # Force spawner to use fake_bot.
    monkeypatch.setenv(
        "BOOKING_BOT_SPAWNER_CMD_OVERRIDE",
        f"{sys.executable}|{FAKE_BOT}",
    )
    return tmp_path


def _make_input(tmp_path: Path, n_rows: int = 20) -> Path:
    inp = tmp_path / "Input" / "file.xlsx"
    inp.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["consumer_no", "phone"])
    for i in range(n_rows):
        ws.append([f"C{i}", f"98765{i:05d}"])
    wb.save(inp)
    return inp


def test_start_spawns_four_fake_bots_and_all_complete(orch_env):
    inp = _make_input(orch_env, n_rows=20)
    rc = orch_cli.run_start(
        source="TEST", input_file=inp,
        chunk_size=5, num_chunks=None,
        headed=False, no_monitor=True,
    )
    assert rc == 0

    deadline = time.monotonic() + 30.0
    runs = orch_env / "data" / "runs" / "TEST"
    while time.monotonic() < deadline:
        hb_files = list(runs.glob("*.heartbeat.json"))
        if len(hb_files) == 4 and all(
            json.loads(p.read_text(encoding="utf-8")).get("phase") == "completed"
            for p in hb_files
        ):
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"not all chunks completed: {list(runs.glob('*'))}")

    chunks_dir = orch_env / "Input" / "chunks" / "TEST"
    assert len(list(chunks_dir.glob("TEST-*.xlsx"))) == 4

    assert not (runs / ".start.lock").exists()
