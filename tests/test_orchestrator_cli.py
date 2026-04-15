"""Unit tests for orchestrator/cli.py argparse dispatch. Does not
actually spawn subprocesses — splitter/auth_template/spawner are
monkeypatched to spies so we exercise only the wiring."""
from pathlib import Path

import pytest

from booking_bot.orchestrator import cli as orch_cli


def test_parse_start_with_chunk_size():
    args = orch_cli.build_parser().parse_args([
        "start", "--source", "ASU", "--input", "Input/ASU.xlsx",
        "--chunk-size", "500",
    ])
    assert args.command == "start"
    assert args.source == "ASU"
    assert args.input == Path("Input/ASU.xlsx")
    assert args.chunk_size == 500
    assert args.instances is None
    assert args.headed is False
    assert args.no_monitor is False


def test_parse_start_with_instances_and_headed():
    args = orch_cli.build_parser().parse_args([
        "start", "--source", "ASU", "--input", "Input/ASU.xlsx",
        "--instances", "20", "--headed",
    ])
    assert args.instances == 20
    assert args.chunk_size is None
    assert args.headed is True


def test_parse_start_chunk_size_and_instances_mutex():
    with pytest.raises(SystemExit):
        orch_cli.build_parser().parse_args([
            "start", "--source", "ASU", "--input", "Input/ASU.xlsx",
            "--chunk-size", "500", "--instances", "20",
        ])


def test_parse_start_headed_and_headless_mutex():
    with pytest.raises(SystemExit):
        orch_cli.build_parser().parse_args([
            "start", "--source", "ASU", "--input", "Input/ASU.xlsx",
            "--chunk-size", "500", "--headed", "--headless",
        ])


def test_parse_start_defaults_chunk_size_when_neither_given():
    args = orch_cli.build_parser().parse_args([
        "start", "--source", "ASU", "--input", "Input/ASU.xlsx",
    ])
    assert args.chunk_size is None
    assert args.instances is None


def test_run_start_invokes_splitter_auth_and_spawner(tmp_path, monkeypatch):
    import openpyxl
    inp = tmp_path / "Input" / "file.xlsx"
    inp.parent.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["consumer_no", "phone"])
    for i in range(20):
        ws.append([f"C{i}", f"98765{i:05d}"])
    wb.save(inp)

    from booking_bot import config as cfg
    monkeypatch.setattr(cfg, "CHUNKS_DIR", tmp_path / "Input" / "chunks")
    monkeypatch.setattr(cfg, "RUNS_DIR",   tmp_path / "data" / "runs")
    monkeypatch.setattr(cfg, "ORCHESTRATOR_LOGS_DIR", tmp_path / "logs" / "orch")

    calls = {"ensure": 0, "clone": 0, "spawn": []}
    monkeypatch.setattr(orch_cli, "_ensure_auth_seed",
                        lambda source: calls.__setitem__("ensure", calls["ensure"] + 1))
    monkeypatch.setattr(orch_cli, "_clone_to_chunks",
                        lambda source, chunks: calls.__setitem__("clone", calls["clone"] + 1))
    monkeypatch.setattr(orch_cli, "_spawn_chunk",
                        lambda spec, *, headed: calls["spawn"].append(spec.chunk_id) or _StubHandle(spec.chunk_id))

    rc = orch_cli.run_start(
        source="TEST", input_file=inp, chunk_size=5, num_chunks=None,
        headed=False, no_monitor=True,
    )
    assert rc == 0
    assert calls["ensure"] == 1
    assert calls["clone"] == 1
    assert sorted(calls["spawn"]) == [f"TEST-00{i}" for i in range(1, 5)]


class _StubHandle:
    def __init__(self, chunk_id):
        self.chunk_id = chunk_id
        self.pid = 12345
