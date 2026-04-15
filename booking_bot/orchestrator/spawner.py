"""Spawn one bot child process per chunk. Windows-first — uses
CREATE_NO_WINDOW (headless) / CREATE_NEW_CONSOLE (headed) and
DETACHED_PROCESS so children survive the parent orchestrator dying.

The spawner writes an initial 'starting' heartbeat BEFORE launching
the child, so the monitor sees the chunk immediately. The child's
own _write_heartbeat helper takes over from 'starting' onwards."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from booking_bot import config
from booking_bot.orchestrator import heartbeat
from booking_bot.orchestrator.splitter import ChunkSpec

log = logging.getLogger("orchestrator.spawner")


@dataclass
class ChildHandle:
    chunk_id: str
    pid: int
    popen: subprocess.Popen
    stdout_log: Path
    stderr_log: Path


def _resolve_cmd(spec: ChunkSpec, headed: bool) -> list[str]:
    """Build the child command. A test override env var replaces the
    booking_bot entry point with a fake bot script so the spawner's
    lifecycle can be exercised without launching a browser."""
    override = os.environ.get("BOOKING_BOT_SPAWNER_CMD_OVERRIDE")
    if override:
        # Format: "<python-exe>|<script-path>"
        py, script = override.split("|", 1)
        cmd = [py, script]
        if not headed:
            cmd.append("--headless")
    else:
        cmd = [sys.executable, "-m", "booking_bot", str(spec.input_path),
               "--profile-suffix", spec.profile_suffix]
        if not headed:
            cmd.append("--headless")
    return cmd


def _creation_flags(headed: bool) -> int:
    if sys.platform != "win32":
        return 0
    # Windows: CREATE_NO_WINDOW cannot be combined with DETACHED_PROCESS
    # or CREATE_NEW_CONSOLE. Children are not automatically killed when
    # the orchestrator exits on Windows, so we don't need DETACHED_PROCESS.
    if headed:
        return subprocess.CREATE_NEW_CONSOLE
    return subprocess.CREATE_NO_WINDOW


def _initial_heartbeat(spec: ChunkSpec, cmd: list[str]) -> heartbeat.Heartbeat:
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    return heartbeat.Heartbeat(
        source=spec.source,
        chunk_id=spec.chunk_id,
        pid=-1,  # overwritten below once Popen has been created
        input_file=str(spec.input_path),
        profile_suffix=spec.profile_suffix,
        phase="starting",
        rows_total=spec.row_count,
        rows_done=0,
        rows_issue=0,
        rows_pending=spec.row_count,
        current_row_idx=None,
        current_phone=None,
        started_at=now_iso,
        last_activity_at=now_iso,
        command=cmd,
        exit_code=None,
        last_error=None,
    )


def spawn_chunk(spec: ChunkSpec, *, headed: bool = False) -> ChildHandle:
    """Launch one bot child process. Writes an initial heartbeat,
    opens stdout/stderr log files under logs/orchestrator/, and returns
    a ChildHandle. Never blocks the caller."""
    spec.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = _resolve_cmd(spec, headed)
    heartbeat.write(spec.heartbeat_path, _initial_heartbeat(spec, cmd))

    logs_dir = config.ORCHESTRATOR_LOGS_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{spec.chunk_id}.out.log"
    stderr_log = logs_dir / f"{spec.chunk_id}.err.log"
    out_f = stdout_log.open("ab")
    err_f = stderr_log.open("ab")

    env = os.environ.copy()
    env["BOOKING_BOT_HEARTBEAT_PATH"]  = str(spec.heartbeat_path)
    env["BOOKING_BOT_SOURCE"]          = spec.source
    env["BOOKING_BOT_CHUNK_ID"]        = spec.chunk_id
    env["BOOKING_BOT_OPERATOR_SLOT"]   = spec.operator_slot
    env["BOOKING_BOT_OPERATOR_PHONE"]  = spec.operator_phone

    popen = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=out_f,
        stderr=err_f,
        env=env,
        creationflags=_creation_flags(headed),
        start_new_session=sys.platform != "win32",
        close_fds=True,
    )
    log.info(f"spawned chunk {spec.chunk_id} pid={popen.pid} headed={headed}")

    # Overwrite initial heartbeat with the real PID.
    hb = _initial_heartbeat(spec, cmd)
    hb.pid = popen.pid
    heartbeat.write(spec.heartbeat_path, hb)

    return ChildHandle(
        chunk_id=spec.chunk_id,
        pid=popen.pid,
        popen=popen,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )


def kill_chunk(
    handle: ChildHandle, *, timeout_s: float = config.ORCHESTRATOR_KILL_TIMEOUT_S,
) -> int | None:
    """Terminate a child. SIGTERM first, SIGKILL after timeout. Returns
    the exit code (None on POSIX if signaled)."""
    if handle.popen.poll() is not None:
        return handle.popen.returncode
    try:
        handle.popen.terminate()
    except ProcessLookupError:
        pass
    try:
        rc = handle.popen.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log.warning(
            f"kill_chunk: {handle.chunk_id} did not exit within "
            f"{timeout_s}s; escalating to SIGKILL"
        )
        try:
            handle.popen.kill()
        except ProcessLookupError:
            pass
        rc = handle.popen.wait(timeout=timeout_s)
    log.info(f"kill_chunk: {handle.chunk_id} exited rc={rc}")
    return rc
