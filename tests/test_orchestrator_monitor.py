"""Unit tests for orchestrator/monitor.py.

The monitor has three separable pieces (table builder, command parser,
stall detector). Each is tested independently; the run loop itself is
integration-tested in Task 17."""
from datetime import datetime, timedelta, timezone

from rich.console import Console

from booking_bot.orchestrator import monitor
from booking_bot.orchestrator.heartbeat import Heartbeat


def _hb(chunk_id: str, **overrides) -> Heartbeat:
    now = datetime.now(tz=timezone.utc)
    defaults = dict(
        source="TEST", chunk_id=chunk_id, pid=1234,
        input_file=f"Input/chunks/TEST/{chunk_id}.xlsx",
        profile_suffix=chunk_id, phase="booking",
        rows_total=100, rows_done=40, rows_issue=2, rows_pending=58,
        current_row_idx=43, current_phone="98xxxxxx10",
        started_at=(now - timedelta(minutes=5)).isoformat(),
        last_activity_at=now.isoformat(),
        command=["python", "-m", "booking_bot"],
        exit_code=None, last_error=None,
    )
    defaults.update(overrides)
    return Heartbeat(**defaults)


def test_build_table_has_chunk_row_per_heartbeat():
    hbs = [_hb("TEST-001"), _hb("TEST-002", rows_done=100, rows_pending=0, phase="completed")]
    table = monitor.build_table(hbs)
    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text()
    assert "TEST-001" in output
    assert "TEST-002" in output


def test_build_table_shows_percent_progress():
    hbs = [_hb("TEST-001", rows_done=50, rows_total=100, rows_pending=50, rows_issue=0)]
    table = monitor.build_table(hbs)
    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text()
    assert "50" in output  # either "50%" or "50/100" — exact formatting is free


def test_build_table_with_empty_list_renders_gracefully():
    table = monitor.build_table([])
    console = Console(record=True, width=160)
    console.print(table)  # must not raise


def test_build_totals_line_sums_across_chunks():
    hbs = [
        _hb("TEST-001", rows_done=10, rows_total=100, rows_pending=88, rows_issue=2),
        _hb("TEST-002", rows_done=50, rows_total=100, rows_pending=48, rows_issue=2),
    ]
    line = monitor.build_totals_line(hbs)
    assert "60" in line  # done
    assert "200" in line  # total


def test_parse_command_restart_short_form():
    cmd = monitor.parse_command("r TEST-001")
    assert cmd == ("restart", {"chunk_id": "TEST-001"})


def test_parse_command_restart_long_form():
    cmd = monitor.parse_command("restart TEST-001")
    assert cmd == ("restart", {"chunk_id": "TEST-001"})


def test_parse_command_kill():
    assert monitor.parse_command("k TEST-001") == ("kill", {"chunk_id": "TEST-001"})
    assert monitor.parse_command("kill TEST-001") == ("kill", {"chunk_id": "TEST-001"})


def test_parse_command_stop_source():
    assert monitor.parse_command("stop ASU") == ("stop", {"source": "ASU"})


def test_parse_command_quit_variants():
    assert monitor.parse_command("q") == ("detach", {})
    assert monitor.parse_command("qq") == ("stop_all", {})


def test_parse_command_help():
    assert monitor.parse_command("h") == ("help", {})
    assert monitor.parse_command("help") == ("help", {})


def test_parse_command_start_with_chunk_size():
    action, args = monitor.parse_command(
        "start ASU Input/ASU.xlsx --chunk-size 500"
    )
    assert action == "start"
    assert args["source"] == "ASU"
    assert args["input"] == "Input/ASU.xlsx"
    assert args["chunk_size"] == 500
    assert args.get("instances") is None


def test_parse_command_start_with_instances_and_headed():
    action, args = monitor.parse_command(
        "start ASU Input/ASU.xlsx --instances 20 --headed"
    )
    assert action == "start"
    assert args["instances"] == 20
    assert args["headed"] is True


def test_parse_command_start_defaults_to_chunk_size_500():
    action, args = monitor.parse_command("start ASU Input/ASU.xlsx")
    assert action == "start"
    assert args["chunk_size"] == 500
    assert args["headed"] is False


def test_parse_command_unknown_returns_error():
    assert monitor.parse_command("flibbertigibbet") == ("error", {"message": "unknown command"})


def test_parse_command_empty_returns_noop():
    assert monitor.parse_command("") == ("noop", {})
    assert monitor.parse_command("   ") == ("noop", {})
