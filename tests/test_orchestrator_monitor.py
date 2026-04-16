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


def test_is_stalled_true_when_last_activity_exceeds_threshold():
    old = (datetime.now(tz=timezone.utc) - timedelta(minutes=15)).isoformat()
    hb = _hb("TEST-001", last_activity_at=old)
    assert monitor.is_stalled(hb, threshold_s=600) is True


def test_is_stalled_false_for_recent_heartbeat():
    hb = _hb("TEST-001")
    assert monitor.is_stalled(hb, threshold_s=600) is False


def test_is_stalled_false_for_completed_phase():
    old = (datetime.now(tz=timezone.utc) - timedelta(minutes=15)).isoformat()
    hb = _hb("TEST-001", phase="completed", last_activity_at=old)
    assert monitor.is_stalled(hb, threshold_s=600) is False


def test_is_stalled_false_for_failed_phase():
    old = (datetime.now(tz=timezone.utc) - timedelta(minutes=15)).isoformat()
    hb = _hb("TEST-001", phase="failed", last_activity_at=old)
    assert monitor.is_stalled(hb, threshold_s=600) is False


def test_restart_budget_allows_first_n_then_refuses():
    budget = monitor.RestartBudget(max_per_chunk=3)
    assert budget.consume("TEST-001") is True
    assert budget.consume("TEST-001") is True
    assert budget.consume("TEST-001") is True
    assert budget.consume("TEST-001") is False  # 4th refused


def test_restart_budget_is_per_chunk():
    budget = monitor.RestartBudget(max_per_chunk=2)
    assert budget.consume("TEST-001") is True
    assert budget.consume("TEST-001") is True
    assert budget.consume("TEST-002") is True  # different chunk — own budget
    assert budget.consume("TEST-001") is False


def test_run_monitor_once_renders_current_heartbeats(tmp_path):
    from booking_bot.orchestrator import heartbeat as hb_mod
    (tmp_path / "TEST").mkdir()
    hb_mod.write(
        tmp_path / "TEST" / "TEST-001.heartbeat.json",
        _hb("TEST-001", rows_done=7),
    )
    hb_mod.write(
        tmp_path / "TEST" / "TEST-002.heartbeat.json",
        _hb("TEST-002", rows_done=12, phase="completed"),
    )
    text = monitor.render_once(runs_dir=tmp_path, source_filter=None)
    assert "TEST-001" in text
    assert "TEST-002" in text
    assert "7" in text
    assert "12" in text


def test_run_monitor_once_filter_excludes_other_sources(tmp_path):
    from booking_bot.orchestrator import heartbeat as hb_mod
    (tmp_path / "ASU").mkdir()
    (tmp_path / "BPCL").mkdir()
    hb_mod.write(tmp_path / "ASU" / "ASU-001.heartbeat.json",
                 _hb("ASU-001", source="ASU"))
    hb_mod.write(tmp_path / "BPCL" / "BPCL-001.heartbeat.json",
                 _hb("BPCL-001", source="BPCL"))
    text = monitor.render_once(runs_dir=tmp_path, source_filter="ASU")
    assert "ASU-001" in text
    assert "BPCL-001" not in text


def _make_hb(chunk_id, *, phase="booking", slot="op1",
             idle_secs=0.0, rows_done=0):
    from datetime import datetime, timedelta, timezone
    from booking_bot.orchestrator.heartbeat import Heartbeat
    last = (datetime.now(tz=timezone.utc) - timedelta(seconds=idle_secs)).isoformat()
    return Heartbeat(
        source="T", chunk_id=chunk_id, pid=123,
        input_file="in.xlsx", profile_suffix=chunk_id,
        phase=phase, rows_total=10, rows_done=rows_done, rows_issue=0,
        rows_pending=10 - rows_done, current_row_idx=None, current_phone=None,
        started_at="2026-04-16T00:00:00+00:00",
        last_activity_at=last,
        command=["python"], exit_code=None, last_error=None,
        operator_slot=slot,
    )


def test_build_table_shows_operator_slot_column():
    from booking_bot.orchestrator.monitor import build_table
    hbs = [_make_hb("T-001", slot="op1"), _make_hb("T-002", slot="op2")]
    table = build_table(hbs)
    headers = [c.header for c in table.columns]
    assert "Op" in headers


def test_build_operator_reauth_banner_flags_stuck_slot():
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-001", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-002", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-003", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-004", slot="op2", phase="booking", idle_secs=2),
    ]
    banner = build_operator_reauth_banner(hbs)
    assert "op1" in banner
    assert "3 chunks" in banner
    assert "op2" not in banner


def test_build_operator_reauth_banner_empty_when_all_healthy():
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-001", slot="op1", phase="booking", idle_secs=0),
        _make_hb("T-002", slot="op2", phase="booking", idle_secs=0),
    ]
    assert build_operator_reauth_banner(hbs) == ""


def test_build_operator_reauth_banner_ignores_single_stuck_chunk():
    """One stuck chunk in a slot isn't an operator-wide problem — don't
    cry wolf."""
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-001", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-002", slot="op1", phase="booking", idle_secs=0),
    ]
    assert build_operator_reauth_banner(hbs) == ""


def test_build_operator_reauth_banner_ignores_none_slot():
    """Legacy pre-multi-op chunks carry operator_slot=None and must never
    poison the per-slot counters — even when they appear stuck alongside
    a genuinely stuck op1 cohort, the banner must attribute the alert to
    op1 only, never to None."""
    from booking_bot.orchestrator.monitor import build_operator_reauth_banner
    hbs = [
        _make_hb("T-legacy-1", slot=None, phase="authenticating", idle_secs=300),
        _make_hb("T-legacy-2", slot=None, phase="authenticating", idle_secs=300),
        _make_hb("T-001", slot="op1", phase="authenticating", idle_secs=300),
        _make_hb("T-002", slot="op1", phase="authenticating", idle_secs=300),
    ]
    banner = build_operator_reauth_banner(hbs)
    assert "op1" in banner
    assert "2 chunks" in banner
    assert "None" not in banner
