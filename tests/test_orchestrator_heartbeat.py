"""Unit tests for orchestrator/heartbeat.py. Pure data + file I/O, no
subprocess, no browser."""
import json

import pytest

from booking_bot.orchestrator import heartbeat
from booking_bot.orchestrator.heartbeat import Heartbeat


def test_mask_phone_standard_10_digit():
    assert heartbeat.mask_phone("9876543210") == "98xxxxxx10"


def test_mask_phone_5_digit():
    assert heartbeat.mask_phone("12345") == "12x45"


def test_mask_phone_exactly_4_digits_unchanged():
    # len == 4: first 2 + last 2 = whole string, no middle to mask.
    assert heartbeat.mask_phone("1234") == "1234"


def test_mask_phone_short_unchanged():
    assert heartbeat.mask_phone("99") == "99"
    assert heartbeat.mask_phone("") == ""


def test_heartbeat_dataclass_roundtrip_via_asdict():
    from dataclasses import asdict
    hb = heartbeat.Heartbeat(
        source="TEST",
        chunk_id="TEST-001",
        pid=1234,
        input_file="Input/chunks/TEST/TEST-001.xlsx",
        profile_suffix="TEST-001",
        phase="booking",
        rows_total=10,
        rows_done=3,
        rows_issue=0,
        rows_pending=7,
        current_row_idx=4,
        current_phone="98xxxxxx10",
        started_at="2026-04-15T13:10:00+00:00",
        last_activity_at="2026-04-15T13:12:00+00:00",
        command=["python", "-m", "booking_bot"],
        exit_code=None,
        last_error=None,
    )
    d = asdict(hb)
    assert d["chunk_id"] == "TEST-001"
    assert d["phase"] == "booking"
    assert d["exit_code"] is None


def _make_hb(**overrides) -> Heartbeat:
    defaults = dict(
        source="TEST", chunk_id="TEST-001", pid=1234,
        input_file="Input/chunks/TEST/TEST-001.xlsx",
        profile_suffix="TEST-001", phase="booking",
        rows_total=10, rows_done=3, rows_issue=0, rows_pending=7,
        current_row_idx=4, current_phone="98xxxxxx10",
        started_at="2026-04-15T13:10:00+00:00",
        last_activity_at="2026-04-15T13:12:00+00:00",
        command=["python", "-m", "booking_bot"],
        exit_code=None, last_error=None,
    )
    defaults.update(overrides)
    return Heartbeat(**defaults)


def test_write_then_read_round_trip(tmp_path):
    hb = _make_hb()
    path = tmp_path / "hb.json"
    heartbeat.write(path, hb)
    got = heartbeat.read(path)
    assert got == hb


def test_write_is_atomic_no_stale_tmp(tmp_path):
    hb = _make_hb()
    path = tmp_path / "hb.json"
    heartbeat.write(path, hb)
    tmp_file = path.with_suffix(path.suffix + ".tmp")
    assert not tmp_file.exists()
    assert path.exists()


def test_read_missing_file_returns_none(tmp_path):
    assert heartbeat.read(tmp_path / "nope.json") is None


def test_read_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "hb.json"
    path.write_text("{ not json }", encoding="utf-8")
    assert heartbeat.read(path) is None


def test_read_missing_required_field_returns_none(tmp_path):
    path = tmp_path / "hb.json"
    path.write_text(json.dumps({"source": "T"}), encoding="utf-8")
    assert heartbeat.read(path) is None


def test_write_overwrites_previous_heartbeat(tmp_path):
    path = tmp_path / "hb.json"
    heartbeat.write(path, _make_hb(rows_done=3))
    heartbeat.write(path, _make_hb(rows_done=7))
    got = heartbeat.read(path)
    assert got is not None
    assert got.rows_done == 7


def test_read_all_empty_runs_dir(tmp_path):
    assert heartbeat.read_all(tmp_path) == []


def test_read_all_reads_all_sources(tmp_path):
    (tmp_path / "ASU").mkdir()
    (tmp_path / "BPCL").mkdir()
    heartbeat.write(tmp_path / "ASU" / "ASU-001.heartbeat.json",
                    _make_hb(source="ASU", chunk_id="ASU-001"))
    heartbeat.write(tmp_path / "BPCL" / "BPCL-001.heartbeat.json",
                    _make_hb(source="BPCL", chunk_id="BPCL-001"))
    got = heartbeat.read_all(tmp_path)
    got_ids = sorted(hb.chunk_id for hb in got)
    assert got_ids == ["ASU-001", "BPCL-001"]


def test_read_all_filters_by_source(tmp_path):
    (tmp_path / "ASU").mkdir()
    (tmp_path / "BPCL").mkdir()
    heartbeat.write(tmp_path / "ASU" / "ASU-001.heartbeat.json",
                    _make_hb(source="ASU", chunk_id="ASU-001"))
    heartbeat.write(tmp_path / "BPCL" / "BPCL-001.heartbeat.json",
                    _make_hb(source="BPCL", chunk_id="BPCL-001"))
    got = heartbeat.read_all(tmp_path, source="ASU")
    assert [hb.chunk_id for hb in got] == ["ASU-001"]


def test_read_all_skips_corrupt_files(tmp_path):
    (tmp_path / "ASU").mkdir()
    heartbeat.write(tmp_path / "ASU" / "ASU-001.heartbeat.json",
                    _make_hb(source="ASU", chunk_id="ASU-001"))
    (tmp_path / "ASU" / "ASU-002.heartbeat.json").write_text("{ oops", encoding="utf-8")
    got = heartbeat.read_all(tmp_path)
    assert [hb.chunk_id for hb in got] == ["ASU-001"]


def test_read_all_ignores_non_heartbeat_files(tmp_path):
    (tmp_path / "ASU").mkdir()
    (tmp_path / "ASU" / ".start.lock").write_text("{}")
    (tmp_path / "ASU" / "notes.txt").write_text("hello")
    assert heartbeat.read_all(tmp_path) == []


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
