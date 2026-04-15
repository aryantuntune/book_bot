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
