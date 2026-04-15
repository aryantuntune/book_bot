"""JSON heartbeat contract between bot child processes and the orchestrator
monitor. Every write is atomic (temp file + os.replace); every read is
tolerant of missing/corrupt files so the monitor's per-tick scan never
crashes on a briefly half-written file."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Phase = Literal[
    "starting", "authenticating", "booking", "recovering",
    "idle", "completed", "failed",
]


@dataclass
class Heartbeat:
    source: str
    chunk_id: str
    pid: int
    input_file: str
    profile_suffix: str
    phase: str
    rows_total: int
    rows_done: int
    rows_issue: int
    rows_pending: int
    current_row_idx: int | None
    current_phone: str | None
    started_at: str          # ISO-8601 with +00:00
    last_activity_at: str
    command: list[str]
    exit_code: int | None
    last_error: str | None


def mask_phone(phone: str) -> str:
    """Keep the first 2 and last 2 digits; replace the middle with x's.
    Returns the input unchanged if len <= 4 (no middle to mask)."""
    if len(phone) <= 4:
        return phone
    middle_len = len(phone) - 4
    return phone[:2] + ("x" * middle_len) + phone[-2:]


import json
import logging
import os
import time
from dataclasses import asdict, fields
from pathlib import Path

log = logging.getLogger("orchestrator.heartbeat")

_REQUIRED_FIELDS = {f.name for f in fields(Heartbeat)}


def write(path: Path, hb: Heartbeat) -> None:
    """Atomic write. Serialize to <path>.tmp, then os.replace to the final
    path. On Windows a briefly-held file lock (antivirus scan, another
    reader) can raise PermissionError; retry once after 50 ms, then log
    and drop the write so the child doesn't crash over a missed tick."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(asdict(hb), indent=2)
    for attempt in (1, 2):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError as e:
            if attempt == 1:
                time.sleep(0.05)
                continue
            log.warning(f"heartbeat write dropped after retry: {path}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return


def read(path: Path) -> Heartbeat | None:
    """Return Heartbeat or None. Missing file, bad JSON, or missing keys
    all collapse to None; the monitor treats None as 'invisible this tick'
    and re-reads on the next refresh. Never raises."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError as e:
        log.debug(f"heartbeat read failed for {path}: {e}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not _REQUIRED_FIELDS.issubset(data.keys()):
        return None
    try:
        return Heartbeat(**{name: data[name] for name in _REQUIRED_FIELDS})
    except (TypeError, ValueError):
        return None


def read_all(runs_dir: Path, source: str | None = None) -> list[Heartbeat]:
    """Glob all *.heartbeat.json files under runs_dir (optionally filtered
    to a single source subdir) and return the successfully parsed ones,
    sorted by chunk_id. Corrupt or non-heartbeat files are silently
    skipped — never raises."""
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []
    if source is not None:
        paths = list((runs_dir / source).glob("*.heartbeat.json"))
    else:
        paths = list(runs_dir.glob("*/*.heartbeat.json"))
    hbs: list[Heartbeat] = []
    for p in paths:
        hb = read(p)
        if hb is not None:
            hbs.append(hb)
    hbs.sort(key=lambda h: h.chunk_id)
    return hbs
