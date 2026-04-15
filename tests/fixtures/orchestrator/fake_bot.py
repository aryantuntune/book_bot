"""Fake bot: reads BOOKING_BOT_HEARTBEAT_PATH from env, writes 3
heartbeats with fake progress, then exits 0. Used by spawner tests so
we can exercise subprocess lifecycles without launching a real browser."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    hb_path = os.environ.get("BOOKING_BOT_HEARTBEAT_PATH")
    source  = os.environ.get("BOOKING_BOT_SOURCE", "FAKE")
    chunk   = os.environ.get("BOOKING_BOT_CHUNK_ID", "FAKE-001")
    if not hb_path:
        print("fake_bot: BOOKING_BOT_HEARTBEAT_PATH not set", file=sys.stderr)
        return 2
    path = Path(hb_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(tz=timezone.utc).isoformat()
    for i, (done, phase) in enumerate(((1, "booking"), (2, "booking"), (3, "completed"))):
        payload = {
            "source": source, "chunk_id": chunk, "pid": os.getpid(),
            "input_file": "fake.xlsx", "profile_suffix": chunk,
            "phase": phase, "rows_total": 3, "rows_done": done,
            "rows_issue": 0, "rows_pending": 3 - done,
            "current_row_idx": done if phase == "booking" else None,
            "current_phone": "98xxxxxx10" if phase == "booking" else None,
            "started_at": started,
            "last_activity_at": datetime.now(tz=timezone.utc).isoformat(),
            "command": ["python", "-m", "fake_bot"],
            "exit_code": 0 if phase == "completed" else None,
            "last_error": None,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        time.sleep(0.1)
    # Side file: record env var values so test_env_var_propagation can check them.
    side = path.parent / f"{chunk}.env.txt"
    with open(side, "w", encoding="utf-8") as f:
        for k, v in os.environ.items():
            if k.startswith("BOOKING_BOT_"):
                f.write(f"{k}={v}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
