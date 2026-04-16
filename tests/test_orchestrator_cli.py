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


def test_auth_subcommand_legacy_singular_phone_rejects_unicode_digits(monkeypatch):
    """Defensive: Devanagari digits look digit-ish to str.isdigit() but
    won't submit correctly to HPCL. Validator must reject them on both
    the plural and singular paths."""
    from booking_bot.orchestrator import cli
    with pytest.raises(SystemExit):
        cli.main([
            "auth", "--source", "T",
            "--operator-phone", "९111111111",  # leading Devanagari 9
        ])


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


def test_start_subcommand_fails_when_seed_missing(tmp_path, monkeypatch, capsys):
    from booking_bot.orchestrator import cli
    from booking_bot import config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")

    inp = tmp_path / "file.xlsx"
    inp.write_text("fake")

    rc = cli.main([
        "start", "--source", "NOSEED", "--input", str(inp),
        "--operator-phones", "9111111111,9222222222",
        "--clones-per-operator", "3",
        "--no-monitor",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no seed dir" in err
    assert "op1" in err and "op2" in err


def test_start_subcommand_fails_when_seed_phone_mismatches(
    tmp_path, monkeypatch, capsys,
):
    """If operator passes phones in a different order than auth was run,
    the seed_phone.json sidecars no longer match → loud error."""
    from booking_bot.orchestrator import cli
    from booking_bot import config
    from datetime import datetime, timezone

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "data" / "runs")
    monkeypatch.setattr(config, "CHUNKS_DIR", tmp_path / "Input" / "chunks")

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

    rc = cli.main([
        "start", "--source", "MULTI", "--input", str(inp),
        "--operator-phones", "9222222222,9111111111",
        "--clones-per-operator", "1",
        "--no-monitor",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "mismatch" in err
