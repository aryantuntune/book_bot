"""One-shot log-mining script: scans booking_bot log files and extracts
confirmed stuck->recovered patterns into data/incidents.jsonl. Run this
once before the first overnight session to seed the advisor's episodic
memory with real historical wins.

The algorithm:
  1. Parse each log line-by-line.
  2. When we see a "reset stuck on dead-end dialog" line, capture the
     enabled buttons and the chosen click label.
  3. Look forward in the same file for the next "detect_state -> X"
     line where X is in {MAIN_MENU, BOOK_FOR_OTHERS_MENU, READY_FOR_CUSTOMER}.
     If found within 30 seconds of the stuck marker, emit an incident.
  4. PII-scrub phone numbers (10 digits) before writing.
  5. Deduplicate by (state, sorted_buttons) and aggregate occurrences.
  6. Write data/incidents.jsonl atomically.

Usage:
  python scripts/bootstrap_incidents.py [--logs-dir logs/] [--output data/incidents.jsonl] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booking_bot.ai_advisor import IncidentStore  # noqa: E402


LOG_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)
DETECT_STATE_RE = re.compile(r"detect_state\s*->\s*(\w+)")
RESET_STUCK_RE = re.compile(
    r"reset stuck on dead-end dialog \(enabled=(\[.*?\])\); "
    r"clicking '([^']+)'"
)
CLICKED_RE = re.compile(r"clicked '([^']+)'")

KNOWN_RECOVERED_STATES = {
    "MAIN_MENU",
    "BOOK_FOR_OTHERS_MENU",
    "READY_FOR_CUSTOMER",
}

RECOVERY_WINDOW = timedelta(seconds=30)

_PHONE_RE = re.compile(r"\b\d{10}\b")


def scrub_pii(text: str) -> str:
    """Replace 10-digit phone numbers with a REDACTED marker. Run on
    every string that goes into the corpus."""
    if not text:
        return text
    return _PHONE_RE.sub("****REDACTED****", text)


def _parse_ts(line: str) -> datetime | None:
    m = LOG_TIMESTAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_buttons_list(raw: str) -> list[str]:
    """Turn a Python-repr list-of-strings like "['A', 'B']" into
    ['A', 'B']."""
    inner = raw.strip().strip("[]")
    if not inner:
        return []
    items = re.findall(r"'((?:[^'\\]|\\.)*)'", inner)
    return items


def parse_log_file(path: Path) -> list[dict]:
    """Return a list of incident dicts found in a single log file."""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    incidents: list[dict] = []

    for i, line in enumerate(lines):
        m = RESET_STUCK_RE.search(line)
        if not m:
            continue
        ts = _parse_ts(line)
        if ts is None:
            continue
        buttons_raw, click_label = m.group(1), m.group(2)
        buttons = _parse_buttons_list(buttons_raw)
        if not buttons:
            continue

        recovered_to: str | None = None
        for j in range(i + 1, min(i + 200, len(lines))):
            fwd_ts = _parse_ts(lines[j])
            if fwd_ts is not None and fwd_ts - ts > RECOVERY_WINDOW:
                break
            dm = DETECT_STATE_RE.search(lines[j])
            if dm and dm.group(1) in KNOWN_RECOVERED_STATES:
                recovered_to = dm.group(1)
                break
        if recovered_to is None:
            continue

        incidents.append({
            "key": IncidentStore.make_key("UNKNOWN", buttons),
            "state": "UNKNOWN",
            "buttons_sorted": sorted(buttons),
            "last_bubble_excerpt": scrub_pii(line.strip())[:500],
            "chosen_action": {
                "action": "click",
                "button_label": click_label,
                "reason": f"bootstrapped from {Path(path).name}:{i+1}",
            },
            "outcome": "recovered",
            "recovered_to_state": recovered_to,
            "source": "bootstrap",
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "occurrences": 1,
        })
    return incidents


def aggregate_incidents(incidents: list[dict]) -> dict[str, dict]:
    """Group by key and sum occurrences. Newest timestamp wins."""
    agg: dict[str, dict] = {}
    for inc in incidents:
        key = inc["key"]
        if key not in agg:
            agg[key] = dict(inc)
        else:
            agg[key]["occurrences"] += inc["occurrences"]
            if inc["timestamp"] > agg[key]["timestamp"]:
                agg[key]["timestamp"] = inc["timestamp"]
                agg[key]["chosen_action"] = inc["chosen_action"]
    return agg
