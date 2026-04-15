"""Live terminal UI for the orchestrator. Reads heartbeat files, renders
a rich table, accepts interactive commands (restart/kill/stop/quit).

Structured in three pieces so each can be unit-tested independently:
  - build_table / build_totals_line: pure renderer (no I/O)
  - parse_command:                   pure input parser
  - run_monitor:                     the rich.Live loop that ties them
                                     together (integration-tested)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from rich.table import Table

from booking_bot.orchestrator.heartbeat import Heartbeat

log = logging.getLogger("orchestrator.monitor")

_PHASE_COLORS = {
    "starting":       "cyan",
    "authenticating": "cyan",
    "booking":        "white",
    "recovering":     "yellow",
    "idle":           "yellow",
    "completed":      "green",
    "failed":         "red",
}


def _fmt_idle_seconds(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        mm, ss = divmod(int(secs), 60)
        return f"{mm}m {ss:02d}s"
    hh, rest = divmod(int(secs), 3600)
    mm, _ = divmod(rest, 60)
    return f"{hh}h {mm:02d}m"


def _idle_seconds(hb: Heartbeat) -> float:
    try:
        last = datetime.fromisoformat(hb.last_activity_at)
    except ValueError:
        return 0.0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(tz=timezone.utc) - last).total_seconds())


def _progress_str(hb: Heartbeat) -> str:
    if hb.rows_total <= 0:
        return "—"
    pct = (hb.rows_done / hb.rows_total) * 100
    return f"{hb.rows_done}/{hb.rows_total} ({pct:.0f}%)"


def build_table(hbs: Iterable[Heartbeat]) -> Table:
    """Render a rich.Table for the monitor view. Pure — no I/O, no state."""
    table = Table(title="Orchestrator — chunk status", expand=True)
    table.add_column("Chunk", no_wrap=True)
    table.add_column("PID", justify="right")
    table.add_column("Phase")
    table.add_column("Done", justify="right")
    table.add_column("Issue", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("Progress")
    table.add_column("Idle", justify="right")

    for hb in hbs:
        color = _PHASE_COLORS.get(hb.phase, "white")
        phase_cell = f"[{color}]{hb.phase}[/{color}]"
        idle = _idle_seconds(hb)
        idle_cell = _fmt_idle_seconds(idle)
        if idle > 120:
            idle_cell = f"[yellow]{idle_cell}[/yellow]"
        table.add_row(
            hb.chunk_id,
            str(hb.pid if hb.pid > 0 else "-"),
            phase_cell,
            str(hb.rows_done),
            str(hb.rows_issue),
            str(hb.rows_pending),
            _progress_str(hb),
            idle_cell,
        )
    return table


def build_totals_line(hbs: Iterable[Heartbeat]) -> str:
    hbs = list(hbs)
    total = sum(h.rows_total for h in hbs)
    done  = sum(h.rows_done for h in hbs)
    issue = sum(h.rows_issue for h in hbs)
    failed = sum(1 for h in hbs if h.phase == "failed")
    pct = (done / total * 100) if total > 0 else 0.0
    return (
        f"Totals: done={done}/{total} ({pct:.0f}%)  "
        f"issue={issue}  failed_chunks={failed}"
    )
