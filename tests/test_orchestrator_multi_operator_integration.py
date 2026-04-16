"""End-to-end orchestrator flow for multi-operator mode, K=2 M=2. Uses
openpyxl for input, monkey-patches out _interactive_auth_seed and the
spawner so no real Chromium launches. Ensures that all the surface
changes (splitter, auth_template, clone_to_chunks, spawner, cli) play
together correctly."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pytest

from booking_bot import config
from booking_bot.orchestrator import cli


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

    spawned: list = []

    class FakeHandle:
        def __init__(self):
            self.popen = None

    def fake_spawn(spec, *, headed=False):
        spawned.append(spec)
        return FakeHandle()

    monkeypatch.setattr(cli, "_spawn_chunk", fake_spawn)

    inp = e2e_env / "Input" / "lalji-test.xlsx"
    _make_xlsx(inp, n_rows=4)

    rc = cli.main([
        "start", "--source", "LALJI",
        "--input", str(inp),
        "--operator-phones", "9111111111,9222222222",
        "--clones-per-operator", "2",
        "--no-monitor",
    ])

    assert rc == 0
    assert len(spawned) == 4

    assert [s.operator_slot for s in spawned] == ["op1", "op1", "op2", "op2"]
    assert [s.operator_phone for s in spawned] == [
        "9111111111", "9111111111", "9222222222", "9222222222",
    ]

    for spec in spawned:
        target = e2e_env / f".chromium-profile-{spec.profile_suffix}"
        assert target.exists()
        expected = f"{spec.operator_slot}-cookies".encode()
        assert (target / "Default" / "Cookies").read_bytes() == expected

    for spec in spawned:
        assert spec.input_path.exists()
