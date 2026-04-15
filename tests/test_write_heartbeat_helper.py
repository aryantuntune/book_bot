"""Tests for the _write_heartbeat helper added to booking_bot/cli.py.
Critically: the helper is a NO-OP when BOOKING_BOT_HEARTBEAT_PATH is
unset — manual runs must not write any files."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booking_bot import cli


def _fake_store(summary: dict) -> MagicMock:
    s = MagicMock()
    s.summary.return_value = summary
    s.input_path = ""
    return s


def test_write_heartbeat_is_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOKING_BOT_HEARTBEAT_PATH", raising=False)
    store = _fake_store({
        "total": 10, "done": 3, "pending": 7, "issue": 0,
        "success": 3, "ekyc": 0, "not_registered": 0, "payment_pending": 0,
    })
    cli._write_heartbeat("booking", store, current_row_idx=4, current_phone="9876543210")
    # No heartbeat files anywhere.
    assert list(tmp_path.rglob("*.heartbeat.json")) == []


def test_write_heartbeat_writes_file_when_env_set(tmp_path, monkeypatch):
    hb_path = tmp_path / "runs" / "TEST" / "TEST-001.heartbeat.json"
    monkeypatch.setenv("BOOKING_BOT_HEARTBEAT_PATH", str(hb_path))
    monkeypatch.setenv("BOOKING_BOT_SOURCE", "TEST")
    monkeypatch.setenv("BOOKING_BOT_CHUNK_ID", "TEST-001")
    # Reset module-level _heartbeat_started_at so the test is deterministic.
    monkeypatch.setattr(cli, "_heartbeat_started_at", None, raising=False)

    store = _fake_store({
        "total": 10, "done": 3, "pending": 7, "issue": 0,
        "success": 3, "ekyc": 0, "not_registered": 0, "payment_pending": 0,
    })
    cli._write_heartbeat(
        "booking", store, current_row_idx=4, current_phone="9876543210",
    )
    assert hb_path.exists()
    data = json.loads(hb_path.read_text(encoding="utf-8"))
    assert data["source"] == "TEST"
    assert data["chunk_id"] == "TEST-001"
    assert data["phase"] == "booking"
    assert data["rows_total"] == 10
    assert data["rows_done"] == 3
    assert data["rows_pending"] == 7
    assert data["current_phone"] == "98xxxxxx10"  # masked


def test_write_heartbeat_disjoint_bucket_invariant(tmp_path, monkeypatch):
    # store.summary()["done"] INCLUDES issue-bucket rows; the heartbeat
    # wants done+issue+pending == total with disjoint buckets.
    hb_path = tmp_path / "hb.json"
    monkeypatch.setenv("BOOKING_BOT_HEARTBEAT_PATH", str(hb_path))
    monkeypatch.setenv("BOOKING_BOT_SOURCE", "TEST")
    monkeypatch.setenv("BOOKING_BOT_CHUNK_ID", "TEST-001")
    monkeypatch.setattr(cli, "_heartbeat_started_at", None, raising=False)

    store = _fake_store({
        "total": 10, "done": 5, "pending": 5, "issue": 2,
        "success": 3, "ekyc": 0, "not_registered": 0, "payment_pending": 0,
    })
    cli._write_heartbeat("booking", store)
    data = json.loads(hb_path.read_text(encoding="utf-8"))
    assert data["rows_done"] == 3     # 5 - 2
    assert data["rows_issue"] == 2
    assert data["rows_pending"] == 5
    assert data["rows_done"] + data["rows_issue"] + data["rows_pending"] == data["rows_total"]
