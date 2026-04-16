"""Orchestrator entry point. Subcommands: auth, start, monitor, stop,
status. Stateless — every command reads its state from the filesystem
(heartbeat JSONs, .start.lock)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from booking_bot import config, exceptions
from booking_bot.orchestrator import auth_template, heartbeat, monitor, spawner, splitter

log = logging.getLogger("orchestrator.cli")


def _parse_operator_phones(raw: str) -> list[str]:
    """Parse a comma-separated phone list: '9111111111,9222222222'.
    Validates each entry is exactly 10 digits and rejects duplicates.
    Raises argparse.ArgumentTypeError for any failure so argparse
    prints a clean error and exits with status 2."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("operator phone list is empty")
    if len(parts) > 10:
        raise argparse.ArgumentTypeError(
            f"at most 10 operator phones supported; got {len(parts)}"
        )
    for p in parts:
        if not (p.isascii() and p.isdigit() and len(p) == 10):
            raise argparse.ArgumentTypeError(
                f"operator phone must be exactly 10 digits; got {p!r}"
            )
    if len(set(parts)) != len(parts):
        raise argparse.ArgumentTypeError("duplicate operator phone in list")
    return parts


def _verify_operator_seeds(
    source: str, operator_phones: list[str],
) -> None:
    """For each operator slot, check that the seed exists, is fresh, and
    has a seed_phone.json that matches the passed phone. Raises
    exceptions.AuthSeedMissing with a list of failing slots."""
    max_age_s = float(
        config.AUTH_COOLDOWN_S - config.ORCHESTRATOR_AUTH_SEED_BUFFER_S
    )
    missing: list[str] = []
    for i, phone in enumerate(operator_phones):
        slot = f"op{i + 1}"
        seed = auth_template._seed_path(source, slot)
        if not seed.exists():
            missing.append(f"{slot} (no seed dir)")
            continue
        if not auth_template._auth_fresh(seed, max_age_s=max_age_s):
            missing.append(f"{slot} (seed stale or unparseable)")
            continue
        recorded = auth_template._read_seed_phone(source, slot)
        if recorded != phone:
            missing.append(
                f"{slot} (mismatch: seeded for {recorded}, passed {phone})"
            )
    if missing:
        raise exceptions.AuthSeedMissing(missing)


# ---- Internal seams (monkey-patched by tests) ----

def _ensure_auth_seed(source: str) -> Path:
    return auth_template.ensure_auth_seed(source)


def _clone_to_chunks(source: str, chunks: list) -> None:
    auth_template.clone_to_chunks(source, chunks)


def _spawn_chunk(spec, *, headed: bool):
    return spawner.spawn_chunk(spec, headed=headed)


# ---- Parser ----

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m booking_bot.orchestrator")
    sub = ap.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="split, clone auth, spawn chunks")
    start.add_argument("--source", required=True,
                       help="operator-chosen source name (alphanumeric, "
                            "1-28 chars)")
    start.add_argument("--input", required=True, type=Path,
                       help="path to the input xlsx to split")
    parallel = start.add_mutually_exclusive_group()
    parallel.add_argument("--chunk-size", type=int, default=None)
    parallel.add_argument("--instances",  type=int, default=None)
    start.add_argument(
        "--operator-phones", type=_parse_operator_phones, default=None,
        help="comma-separated HPCL operator phones; enables multi-operator "
             "mode. Total parallelism = len(phones) * --clones-per-operator. "
             "When set, --chunk-size/--instances are ignored.",
    )
    start.add_argument(
        "--clones-per-operator", type=int, default=3,
        help="cloned bot instances per operator phone (1-3, default 3)",
    )
    visibility = start.add_mutually_exclusive_group()
    visibility.add_argument("--headed", action="store_true")
    visibility.add_argument("--headless", dest="headed", action="store_false")
    start.set_defaults(headed=False)
    start.add_argument("--no-monitor", action="store_true",
                       help="skip the automatic monitor handoff after spawn")

    auth = sub.add_parser("auth", help="pre-authenticate operator auth-seed profiles")
    auth.add_argument("--source", required=True)
    phones_group = auth.add_mutually_exclusive_group(required=True)
    phones_group.add_argument(
        "--operator-phones", type=_parse_operator_phones, default=None,
        help="comma-separated HPCL operator phones; one auth-seed per phone "
             "(slots op1..opK)",
    )
    phones_group.add_argument(
        "--operator-phone", default=None,
        help="legacy single-phone form; implies slot op1",
    )

    mon = sub.add_parser("monitor", help="attach the live terminal UI")
    mon.add_argument("--source", default=None)

    stop_cmd = sub.add_parser("stop", help="stop all chunks of a source")
    stop_cmd.add_argument("--source", required=True)

    status = sub.add_parser("status", help="one-shot status dump")
    status.add_argument("--source", default=None)
    status.add_argument("--json", action="store_true", dest="as_json")

    return ap


# ---- Subcommand handlers ----

def _acquire_lock(source: str) -> Path:
    lock_dir = config.RUNS_DIR / source
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".start.lock"
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            if _pid_alive(pid):
                raise RuntimeError(
                    f"source {source} is already starting "
                    f"(pid {pid}, at {data.get('started_at')})"
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    lock_path.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
    }), encoding="utf-8")
    return lock_path


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_start(
    *,
    source: str,
    input_file: Path,
    chunk_size: int | None,
    num_chunks: int | None,
    operator_phones: list[str] | None = None,
    clones_per_operator: int = 3,
    headed: bool,
    no_monitor: bool,
) -> int:
    """Top-level start handler. Lock → split → auth seed verify → clone →
    spawn. Returns a shell exit code."""
    if operator_phones is None and chunk_size is None and num_chunks is None:
        chunk_size = 500  # preserve existing default

    lock_path = _acquire_lock(source)
    try:
        if operator_phones is not None:
            _verify_operator_seeds(source, operator_phones)
            chunks = splitter.split(
                source, input_file,
                operator_phones=operator_phones,
                clones_per_operator=clones_per_operator,
            )
            print(
                f"[orchestrator] multi-operator split into {len(chunks)} "
                f"chunks across {len(operator_phones)} operators "
                f"({clones_per_operator} per operator)",
                flush=True,
            )
        else:
            chunks = splitter.split(
                source, input_file,
                chunk_size=chunk_size, num_chunks=num_chunks,
            )
            print(
                f"[orchestrator] split into {len(chunks)} chunks", flush=True,
            )
            _ensure_auth_seed(source)

        _clone_to_chunks(source, chunks)

        handles = []
        for spec in chunks:
            handle = _spawn_chunk(spec, headed=headed)
            handles.append(handle)
            time.sleep(0.5)  # gentle stagger so HPCL isn't slammed by 25 simultaneous SSL handshakes
        print(f"[orchestrator] spawned {len(handles)} chunks", flush=True)
    finally:
        _release_lock(lock_path)

    if no_monitor:
        return 0
    return monitor.run_monitor(source_filter=source)


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "start":
        return run_start(
            source=args.source, input_file=args.input,
            chunk_size=args.chunk_size, num_chunks=args.instances,
            operator_phones=args.operator_phones,
            clones_per_operator=args.clones_per_operator,
            headed=args.headed, no_monitor=args.no_monitor,
        )
    if args.command == "auth":
        if args.operator_phones is not None:
            phones = args.operator_phones
        else:
            try:
                phones = _parse_operator_phones(args.operator_phone)
            except argparse.ArgumentTypeError as e:
                ap.error(str(e))
        seeds = auth_template.ensure_auth_seeds(args.source, phones)
        for slot, path in seeds.items():
            print(f"[orchestrator] auth seed {slot} ready: {path}")
        return 0
    if args.command == "monitor":
        return monitor.run_monitor(source_filter=args.source)
    if args.command == "stop":
        return run_stop(source=args.source)
    if args.command == "status":
        return run_status(source=args.source, as_json=args.as_json)
    ap.error(f"unknown command: {args.command}")
    return 2


def run_stop(*, source: str) -> int:
    hbs = heartbeat.read_all(config.RUNS_DIR, source=source)
    killed = 0
    for hb in hbs:
        if hb.exit_code is not None or hb.pid <= 0:
            continue
        try:
            import signal
            os.kill(hb.pid, signal.SIGTERM)
            killed += 1
        except OSError as e:
            log.warning(f"could not kill {hb.chunk_id} pid={hb.pid}: {e}")
    print(f"[orchestrator] stop: sent SIGTERM to {killed} chunks")
    return 0


def run_status(*, source: str | None, as_json: bool) -> int:
    hbs = heartbeat.read_all(config.RUNS_DIR, source=source)
    if as_json:
        from dataclasses import asdict
        print(json.dumps([asdict(h) for h in hbs], indent=2))
        return 0
    print(monitor.render_once(runs_dir=config.RUNS_DIR, source_filter=source))
    return 0
