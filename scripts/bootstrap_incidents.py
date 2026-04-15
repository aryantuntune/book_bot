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
RESET_STUCK_RE = re.compile(
    r"reset stuck on dead-end dialog \(enabled=(\[.*?\])\); "
    r"clicking '([^']+)'"
)

# Real-log recovery signals. The first match after a reset-stuck line,
# within RECOVERY_WINDOW and before any subsequent reset-stuck, tells us
# which state the bot recovered to. Order matters only for the regex scan
# inside a single line; first-signal-wins across lines.
RECOVERY_SIGNALS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"playbook: reset done"), "BOOK_FOR_OTHERS_MENU"),
    (re.compile(r"playbook: at main menu"), "MAIN_MENU"),
    (re.compile(r"row \d+: success code="), "READY_FOR_CUSTOMER"),
    (re.compile(r"playbook step \d+/\d+: TYPE \[customer_phone\]"), "READY_FOR_CUSTOMER"),
]

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
            if RESET_STUCK_RE.search(lines[j]):
                break
            for pat, target in RECOVERY_SIGNALS:
                if pat.search(lines[j]):
                    recovered_to = target
                    break
            if recovered_to is not None:
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


def write_incidents(records: dict[str, dict], path: Path) -> None:
    """Atomic write of the aggregated records to `path`. If `path`
    already exists, merge the new records with the existing file,
    preferring runtime-sourced records over bootstrap-sourced ones
    on key collisions."""
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "key" in rec:
                existing[rec["key"]] = rec

    merged: dict[str, dict] = dict(existing)
    for key, new_rec in records.items():
        prior = existing.get(key)
        if prior is None:
            merged[key] = new_rec
        else:
            if prior.get("source") == "runtime" and new_rec.get("source") == "bootstrap":
                continue
            merged[key] = new_rec

    fd, tmp_name = tempfile.mkstemp(
        prefix=".incidents.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in merged.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        raise


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed data/incidents.jsonl from existing logs."
    )
    parser.add_argument(
        "--logs-dir",
        default="logs",
        help="Directory to scan for *.log files (default: logs)",
    )
    parser.add_argument(
        "--output",
        default="data/incidents.jsonl",
        help="Path to write the incidents.jsonl file (default: data/incidents.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary only; do not write the output file",
    )
    args = parser.parse_args(argv)

    logs_dir = Path(args.logs_dir)
    out_path = Path(args.output)

    all_incidents: list[dict] = []
    log_files: list[Path] = sorted(logs_dir.glob("*.log"))
    if not log_files:
        print(f"bootstrap: no *.log files in {logs_dir}", file=sys.stderr)
        return 1

    for lf in log_files:
        try:
            all_incidents.extend(parse_log_file(lf))
        except Exception as e:
            print(f"bootstrap: skipping {lf.name}: {e}", file=sys.stderr)

    agg = aggregate_incidents(all_incidents)

    print(
        f"bootstrapped {len(all_incidents)} incidents from "
        f"{len(log_files)} log files; {len(agg)} unique keys"
    )
    top = sorted(agg.values(), key=lambda r: -r["occurrences"])[:10]
    for rec in top:
        print(
            f"  {rec['occurrences']}x {rec['state']} buttons={rec['buttons_sorted']} "
            f"-> click {rec['chosen_action']['button_label']!r}"
        )

    if args.dry_run:
        print("dry-run: no file written")
        return 0

    write_incidents(agg, out_path)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
