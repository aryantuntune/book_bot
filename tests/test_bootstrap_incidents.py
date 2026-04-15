"""Unit tests for scripts/bootstrap_incidents.py. Uses a small canned
log fixture under tests/fixtures/ so the test is fast and repeatable."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "bootstrap_log_sample.log"


def test_fixture_exists():
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"


def test_parse_log_extracts_stuck_to_recovered_pair():
    from scripts.bootstrap_incidents import parse_log_file
    incidents = parse_log_file(FIXTURE)
    assert len(incidents) == 2
    inc = incidents[0]
    assert inc["state"] == "UNKNOWN"
    assert sorted(inc["buttons_sorted"]) == ["Make Payment", "Previous Menu"]
    assert inc["chosen_action"]["action"] == "click"
    assert inc["chosen_action"]["button_label"] == "Previous Menu"
    assert inc["recovered_to_state"] == "BOOK_FOR_OTHERS_MENU"


def test_aggregate_incidents_dedupes_by_key():
    from scripts.bootstrap_incidents import aggregate_incidents, parse_log_file
    incidents = parse_log_file(FIXTURE)
    agg = aggregate_incidents(incidents)
    assert len(agg) == 1
    only = list(agg.values())[0]
    assert only["occurrences"] == 2


def test_scrub_phone_numbers_in_text():
    from scripts.bootstrap_incidents import scrub_pii
    assert scrub_pii("called 9876543210 today") == "called ****REDACTED**** today"
    assert scrub_pii("no phone here") == "no phone here"
    assert scrub_pii("two 1234567890 and 9999999999") == "two ****REDACTED**** and ****REDACTED****"


def test_parse_log_ignores_stuck_without_recovery(tmp_path):
    """A stuck marker with no subsequent known-state transition must
    NOT produce an incident."""
    log_path = tmp_path / "orphan.log"
    log_path.write_text(
        "2026-04-13 19:32:33 INFO chat: detect_state -> UNKNOWN\n"
        "2026-04-13 19:32:34 WARNING playbook: reset stuck on dead-end dialog (enabled=['A', 'B']); clicking 'A' to back out and retrying reset\n"
        "2026-04-13 19:32:35 INFO playbook: clicked 'A' (id=bA)\n"
    )
    from scripts.bootstrap_incidents import parse_log_file
    incidents = parse_log_file(log_path)
    assert incidents == []


def test_write_incidents_to_new_file(tmp_path):
    from scripts.bootstrap_incidents import write_incidents
    path = tmp_path / "data" / "incidents.jsonl"
    records = {
        "UNKNOWN|a|b": {
            "key": "UNKNOWN|a|b",
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "r"},
            "outcome": "recovered",
            "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap",
            "timestamp": "2026-04-15T00:00:00Z",
            "occurrences": 1,
        }
    }
    write_incidents(records, path)
    assert path.exists()
    loaded = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(loaded) == 1
    rec = json.loads(loaded[0])
    assert rec["key"] == "UNKNOWN|a|b"


def test_write_incidents_preserves_existing_runtime_records_on_key_collision(tmp_path):
    """When merging bootstrap output into an existing file, runtime-sourced
    records must not be overwritten by bootstrap records with the same key."""
    from scripts.bootstrap_incidents import write_incidents
    path = tmp_path / "incidents.jsonl"
    existing = {
        "key": "UNKNOWN|a|b",
        "state": "UNKNOWN",
        "buttons_sorted": ["a", "b"],
        "last_bubble_excerpt": "runtime",
        "chosen_action": {"action": "click", "button_label": "b", "reason": "runtime win"},
        "outcome": "recovered",
        "recovered_to_state": "MAIN_MENU",
        "source": "runtime",
        "timestamp": "2026-04-15T10:00:00Z",
        "occurrences": 7,
    }
    path.write_text(json.dumps(existing) + "\n")

    new_records = {
        "UNKNOWN|a|b": {
            "key": "UNKNOWN|a|b",
            "state": "UNKNOWN",
            "buttons_sorted": ["a", "b"],
            "last_bubble_excerpt": "bootstrap",
            "chosen_action": {"action": "click", "button_label": "a", "reason": "boot"},
            "outcome": "recovered",
            "recovered_to_state": "MAIN_MENU",
            "source": "bootstrap",
            "timestamp": "2026-04-15T00:00:00Z",
            "occurrences": 1,
        }
    }
    write_incidents(new_records, path)

    loaded = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(loaded) == 1
    rec = loaded[0]
    assert rec["source"] == "runtime"
    assert rec["chosen_action"]["button_label"] == "b"


def test_cli_dry_run_does_not_write_file(tmp_path, capsys):
    from scripts.bootstrap_incidents import run_cli
    out_path = tmp_path / "incidents.jsonl"
    exit_code = run_cli([
        "--logs-dir", str(FIXTURE.parent),
        "--output", str(out_path),
        "--dry-run",
    ])
    assert exit_code == 0
    assert not out_path.exists()
    captured = capsys.readouterr()
    assert "bootstrapped" in captured.out.lower()


def test_cli_writes_output_file(tmp_path):
    from scripts.bootstrap_incidents import run_cli
    out_path = tmp_path / "incidents.jsonl"
    exit_code = run_cli([
        "--logs-dir", str(FIXTURE.parent),
        "--output", str(out_path),
    ])
    assert exit_code == 0
    assert out_path.exists()
