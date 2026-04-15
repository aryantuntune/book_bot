"""Live terminal UI for the orchestrator. Reads heartbeat files, renders
a rich table, accepts interactive commands (restart/kill/stop/quit).

Structured in three pieces so each can be unit-tested independently:
  - build_table / build_totals_line: pure renderer (no I/O)
  - parse_command:                   pure input parser
  - run_monitor:                     the rich.Live loop that ties them
                                     together (integration-tested)."""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
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


def parse_command(line: str) -> tuple[str, dict]:
    """Parse one line of user input into (action, args).

    Actions:
      - noop:      blank input
      - restart:   {chunk_id}
      - kill:      {chunk_id}
      - stop:      {source}
      - start:     {source, input, chunk_size|instances, headed}
      - detach:    {}
      - stop_all:  {}
      - help:      {}
      - error:     {message}
    """
    try:
        tokens = shlex.split(line.strip())
    except ValueError:
        return ("error", {"message": "unbalanced quotes"})
    if not tokens:
        return ("noop", {})
    head, *rest = tokens
    head = head.lower()

    if head in ("r", "restart"):
        if len(rest) != 1:
            return ("error", {"message": "usage: r <chunk-id>"})
        return ("restart", {"chunk_id": rest[0]})

    if head in ("k", "kill"):
        if len(rest) != 1:
            return ("error", {"message": "usage: k <chunk-id>"})
        return ("kill", {"chunk_id": rest[0]})

    if head == "stop":
        if len(rest) != 1:
            return ("error", {"message": "usage: stop <source>"})
        return ("stop", {"source": rest[0]})

    if head == "q":
        return ("detach", {})
    if head == "qq":
        return ("stop_all", {})
    if head in ("h", "help", "?"):
        return ("help", {})

    if head == "start":
        return _parse_start(rest)

    return ("error", {"message": "unknown command"})


def _parse_start(tokens: list[str]) -> tuple[str, dict]:
    if len(tokens) < 2:
        return ("error", {
            "message": "usage: start <source> <input> [--chunk-size N | --instances M] [--headed|--headless]"
        })
    source, input_path, *flags = tokens
    args: dict = {
        "source": source,
        "input":  input_path,
        "chunk_size": None,
        "instances":  None,
        "headed": False,
    }
    i = 0
    while i < len(flags):
        tok = flags[i]
        if tok == "--chunk-size" and i + 1 < len(flags):
            try:
                args["chunk_size"] = int(flags[i + 1])
            except ValueError:
                return ("error", {"message": f"--chunk-size expects integer, got {flags[i + 1]}"})
            i += 2
            continue
        if tok == "--instances" and i + 1 < len(flags):
            try:
                args["instances"] = int(flags[i + 1])
            except ValueError:
                return ("error", {"message": f"--instances expects integer, got {flags[i + 1]}"})
            i += 2
            continue
        if tok == "--headed":
            args["headed"] = True
            i += 1
            continue
        if tok == "--headless":
            args["headed"] = False
            i += 1
            continue
        return ("error", {"message": f"unknown flag: {tok}"})
    if args["chunk_size"] is not None and args["instances"] is not None:
        return ("error", {"message": "pass only one of --chunk-size / --instances"})
    if args["chunk_size"] is None and args["instances"] is None:
        args["chunk_size"] = 500
    return ("start", args)


_LIVE_PHASES = {"booking", "recovering", "idle", "starting", "authenticating"}


def is_stalled(hb: Heartbeat, *, threshold_s: float) -> bool:
    """True when hb.last_activity_at is older than threshold_s AND the
    heartbeat is still in a live phase. Completed and failed chunks are
    never stalled."""
    if hb.phase not in _LIVE_PHASES:
        return False
    return _idle_seconds(hb) > threshold_s


@dataclass
class RestartBudget:
    """Per-chunk auto-restart counter. consume() returns True if the
    chunk is allowed one more auto-restart, False once the budget is
    exhausted. A chunk that exhausts its budget is surfaced in the
    monitor with last_error='auto-restart budget exhausted'."""

    max_per_chunk: int
    _counts: dict[str, int] = field(default_factory=dict)

    def consume(self, chunk_id: str) -> bool:
        used = self._counts.get(chunk_id, 0)
        if used >= self.max_per_chunk:
            return False
        self._counts[chunk_id] = used + 1
        return True

    def reset(self, chunk_id: str) -> None:
        self._counts.pop(chunk_id, None)
