"""Top-level orchestration. Command:

    python -m booking_bot Input/file1.xlsx [--debug]

Flow: load Excel → launch browser → authenticate once → iterate pending rows
with a 2-attempt retry policy → write results → pace → summary.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from getpass import getpass
from pathlib import Path

from booking_bot import browser, chat, config, playbook as playbook_mod
from booking_bot.auth import full_auth, login_if_needed
from booking_bot.excel import ExcelStore
from booking_bot.ai_advisor import SAFE_RECOVERED_STATES  # noqa: F401
from booking_bot.ai_advisor import (
    AdvisorBudget,
    IncidentStore,
    apply_advisor_decision,
    build_snapshot,
    consult,
)
from booking_bot.exceptions import (
    AdvisorSkipRow,
    ChatStuckError,
    FatalError,
    GatewayError,
    IframeLostError,
    OptionNotFoundError,
    RestartableFatalError,
)
from booking_bot.logging_setup import setup_logging

log = logging.getLogger("cli")

RECOVERABLE = (ChatStuckError, GatewayError, IframeLostError, OptionNotFoundError)

MAX_PASSES = 3

# Issue reasons that are definitive — retrying won't help because the
# customer / payment / cylinder state is determined by HPCL, not by us.
# Anything NOT starting with one of these prefixes is considered transient
# and is retried on the next pass.
TERMINAL_ISSUE_PREFIXES = (
    "pending_payment",
    "invalid_customer",
    "already_booked",
    "invalid_phone_format",
    "not_registered",
)


def _is_terminal_issue(reason: str) -> bool:
    return reason.startswith(TERMINAL_ISSUE_PREFIXES)


# -------- Public helpers --------

def normalize_phone(raw: object) -> tuple[str, str | None]:
    """Coerce an Excel cell into a canonical 10-digit phone string. See spec
    §7.2. Returns (cleaned_phone, error_reason); error_reason is None on
    success and 'invalid_phone_format' otherwise."""
    if isinstance(raw, bool):
        return ("", "invalid_phone_format")
    if isinstance(raw, int):
        s = str(raw)
    elif isinstance(raw, float):
        if raw != int(raw):
            return ("", "invalid_phone_format")
        s = str(int(raw))
    elif isinstance(raw, str):
        s = re.sub(r"[^\d+]", "", raw.strip())
    else:
        return ("", "invalid_phone_format")
    m = re.fullmatch(r"(?:\+?91)?(\d{10})", s)
    if not m:
        return ("", "invalid_phone_format")
    return (m.group(1), None)


_USE_GUI_OTP = False
_HEADLESS = False
_PROFILE_SUFFIX: str | None = None


# ---- Orchestrator heartbeat integration (env-gated) ----
# These three env vars are set by orchestrator/spawner.py. When unset
# (manual runs) _write_heartbeat returns immediately and no files are
# written anywhere.

_heartbeat_started_at: str | None = None


def _operator_slot_from_env() -> str | None:
    """Return BOOKING_BOT_OPERATOR_SLOT from the environment, or None."""
    return os.environ.get("BOOKING_BOT_OPERATOR_SLOT") or None


def _write_heartbeat(
    phase: str,
    store,
    *,
    current_row_idx: int | None = None,
    current_phone: str | None = None,
    last_error: str | None = None,
) -> None:
    """Write one heartbeat tick for the orchestrator monitor. No-op when
    BOOKING_BOT_HEARTBEAT_PATH is unset (i.e., running manually)."""
    from datetime import datetime, timezone

    path_str = os.environ.get("BOOKING_BOT_HEARTBEAT_PATH")
    if not path_str:
        return

    from pathlib import Path as _Path

    from booking_bot.orchestrator import heartbeat as _hb

    global _heartbeat_started_at
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    if _heartbeat_started_at is None:
        _heartbeat_started_at = now_iso

    s = store.summary()
    # store.summary()["done"] includes the issue bucket — disjoint math:
    rows_total   = s["total"]
    rows_issue   = s["issue"]
    rows_done    = s["done"] - rows_issue
    rows_pending = s["pending"]
    assert rows_done + rows_issue + rows_pending == rows_total, (
        f"heartbeat bucket math off: done={rows_done} issue={rows_issue} "
        f"pending={rows_pending} total={rows_total}"
    )

    masked_phone: str | None = None
    if current_phone:
        masked_phone = _hb.mask_phone(current_phone)

    hb = _hb.Heartbeat(
        source=os.environ.get("BOOKING_BOT_SOURCE", ""),
        chunk_id=os.environ.get("BOOKING_BOT_CHUNK_ID", ""),
        pid=os.getpid(),
        input_file=str(getattr(store, "input_path", "") or ""),
        profile_suffix=os.environ.get("BOOKING_BOT_CHUNK_ID", ""),
        phase=phase,
        rows_total=rows_total,
        rows_done=rows_done,
        rows_issue=rows_issue,
        rows_pending=rows_pending,
        current_row_idx=current_row_idx,
        current_phone=masked_phone,
        started_at=_heartbeat_started_at,
        last_activity_at=now_iso,
        command=list(sys.argv),
        exit_code=None,
        last_error=last_error,
        operator_slot=_operator_slot_from_env(),
    )
    _hb.write(_Path(path_str), hb)


def _start_idle_alert() -> threading.Event:
    """Start a daemon watchdog thread that beeps the device when the bot has
    been waiting for manual input longer than config.IDLE_ALERT_AFTER_S.
    After the initial grace period the thread rings every
    config.IDLE_ALERT_INTERVAL_S until the returned Event is set.

    Use as::

        stop = _start_idle_alert()
        try:
            value = blocking_prompt()
        finally:
            stop.set()

    On Windows uses winsound.MessageBeep (the system "exclamation" sound, same
    alert the OS uses for warning dialogs — loud enough to notice across the
    room). On other platforms writes BEL to stdout as a best-effort fallback.
    """
    stop = threading.Event()

    def _beep_loop() -> None:
        if stop.wait(config.IDLE_ALERT_AFTER_S):
            return
        log.warning(
            f"idle alert: bot has been waiting for manual input for "
            f"{config.IDLE_ALERT_AFTER_S}s — ringing device every "
            f"{config.IDLE_ALERT_INTERVAL_S}s until resolved"
        )
        while not stop.is_set():
            try:
                if sys.platform == "win32":
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                else:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
            except Exception:
                pass
            if stop.wait(config.IDLE_ALERT_INTERVAL_S):
                return

    threading.Thread(target=_beep_loop, daemon=True).start()
    return stop


def _prompt_otp() -> str:
    """Blocking OTP prompt. Uses a tkinter popup when the startup dialog was
    shown (double-clicked .exe) and getpass() otherwise, so dev runs from a
    terminal keep their current behavior. In --headless mode there is no
    UI, no stdin, and no way to receive an SMS-delivered OTP — so we fail
    fast with a clear message instead of hanging."""
    if _HEADLESS:
        raise FatalError(
            "headless mode can't prompt for OTP. HP Gas requires an "
            "operator OTP the first time, so run the bot once without "
            "--headless (double-click the .exe or pass no flags) to "
            "establish the session, then subsequent --headless runs will "
            "use the cached cookies in .chrome-profile/."
        )
    stop = _start_idle_alert()
    try:
        if _USE_GUI_OTP:
            from booking_bot import ui
            return ui.prompt_otp(config.OPERATOR_PHONE)
        return getpass(f"Enter OTP for {config.OPERATOR_PHONE}: ").strip()
    finally:
        stop.set()


def _resolve_playbook_path(explicit: Path | None, no_playbook: bool) -> Path | None:
    """Figure out which playbook file to load.

    - If --no-playbook was passed, skip playbook mode entirely (legacy).
    - If --playbook was passed, use that exact path (fail loudly if missing).
    - Otherwise, auto-select the newest .jsonl in recordings/ (by mtime).
      This lets the operator just run `python -m booking_bot Input/file.xlsx`
      without remembering the recording filename. If there's no recording at
      all, fall back to legacy mode with a clear log line.
    """
    if no_playbook:
        return None
    if explicit is not None:
        if not explicit.exists():
            raise SystemExit(f"--playbook path does not exist: {explicit}")
        return explicit
    # Search two locations so the frozen .exe finds both operator-added
    # recordings (dropped next to the binary) AND the build-time fallback
    # PyInstaller extracts into _MEIPASS. config.ROOT = exe dir in frozen
    # mode, RESOURCES_ROOT = _MEIPASS. When running from source both point
    # at the repo root, so the second entry is harmless.
    search_dirs: list[Path] = []
    for d in (config.ROOT / "recordings", config.RESOURCES_ROOT / "recordings"):
        if d not in search_dirs:
            search_dirs.append(d)
    candidates: list[Path] = []
    for d in search_dirs:
        if d.exists():
            candidates.extend(d.glob("*.jsonl"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    newest = candidates[0]
    log.info(
        f"auto-selected newest recording: {newest.name} "
        f"({len(candidates)} available across {len(search_dirs)} location(s))"
    )
    return newest


# -------- Main loop --------

# Section 3 of the survivability design. The old handler restored the
# default SIGINT handler on first Ctrl-C, so a rapid double-tap raised
# KeyboardInterrupt mid-ctx.close() and left Playwright's async close
# task dangling (observed in the 02:47:20 log: "Task was destroyed but
# it is pending!" + "Connection closed while reading from the driver").
_should_stop = False
_ctrl_c_count = 0
_force_shutdown = False


def _install_signal_handler() -> None:
    """Shielded SIGINT handler:
      1st Ctrl-C — set _should_stop, keep the handler installed so a
                   fast second tap doesn't unwind mid-row.
      2nd Ctrl-C — arm _force_shutdown; the finally: clause will run
                   ctx.close() inside a SHUTDOWN_GRACE_S grace window.
      3rd Ctrl-C — hard os._exit for the operator who really needs out.
    """
    def _h(signum, frame):
        global _should_stop, _ctrl_c_count, _force_shutdown
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            log.warning(
                f"received signal {signum}; finishing current row then "
                f"stopping. Press Ctrl-C again for shielded shutdown "
                f"(waits {config.SHUTDOWN_GRACE_S}s for cookie flush). "
                f"A third Ctrl-C hard-exits."
            )
            _should_stop = True
        elif _ctrl_c_count == 2:
            log.warning(
                f"received second Ctrl-C; entering shielded shutdown "
                f"({config.SHUTDOWN_GRACE_S}s grace for ctx.close()). "
                f"Third Ctrl-C will hard-exit immediately."
            )
            _force_shutdown = True
        else:
            log.error("third Ctrl-C; hard-exiting")
            import os
            os._exit(130)
    signal.signal(signal.SIGINT, _h)


def main() -> None:
    global _USE_GUI_OTP

    # Handles populated by the GUI bootstrap when we pre-launch the browser
    # to detect session state. The main try/ block re-uses them instead of
    # calling start_browser() again.
    _pre_pw = _pre_browser = _pre_ctx = _pre_page = _pre_frame = None

    # No CLI args → show the startup GUI (this is how the bundled .exe runs
    # when an operator double-clicks it). With CLI args we keep the old
    # argparse behavior so dev workflows are unchanged.
    if len(sys.argv) == 1:
        from booking_bot import ui
        _USE_GUI_OTP = True

        # Minimal logging for the bootstrap phase so the operator sees
        # browser-launch progress in the console window while they wait
        # for the dialog. setup_logging() will replace these handlers
        # after the dialog returns.
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-7s  %(name)-12s  %(message)s",
            datefmt="%H:%M:%S",
        )

        # Pre-launch the browser to detect whether HPCL still thinks the
        # session is active. The persistent chrome profile preserves
        # cookies across runs, so subsequent launches can often skip
        # operator auth entirely — we only ask the operator for a phone
        # number when the chat lands in a NEEDS_OPERATOR_* state.
        try:
            _pre_pw, _pre_browser, _pre_ctx, _pre_page = browser.start_browser()
            _pre_frame = browser.get_chat_frame(_pre_page)
            chat.wait_until_settled(_pre_frame)
            startup_state = chat.detect_state(_pre_frame)
        except Exception as e:
            from booking_bot.exceptions import ChromeNotInstalledError, ProfileInUseError
            import tkinter as _tk
            import tkinter.messagebox as _mb
            _root = _tk.Tk(); _root.withdraw()
            if isinstance(e, ChromeNotInstalledError):
                _mb.showerror("Google Chrome not installed", str(e))
            elif isinstance(e, ProfileInUseError):
                _mb.showerror("HP Gas Booking Bot already running", str(e))
            else:
                _mb.showerror(
                    "Startup failed",
                    f"Could not launch the browser or load HP Gas chat:\n\n"
                    f"{type(e).__name__}: {e}\n\n"
                    f"Check that you're connected to the internet and try again.",
                )
            _root.destroy()
            sys.exit(1)

        session_active = startup_state not in (
            "NEEDS_OPERATOR_AUTH",
            "NEEDS_OPERATOR_OTP",
            "UNKNOWN",
        )
        log.info(
            f"startup detect_state={startup_state!r}; "
            f"session_active={session_active}"
        )

        try:
            values = ui.prompt_startup(ask_phone=not session_active)
        except Exception as e:
            log.error(f"GUI failed to launch: {type(e).__name__}: {e}")
            _close_browser_handles(_pre_ctx, _pre_pw)
            sys.exit(2)
        if values is None:
            log.info("cancelled by user")
            _close_browser_handles(_pre_ctx, _pre_pw)
            sys.exit(0)

        # Only overwrite OPERATOR_PHONE when the dialog actually collected
        # one — if the session was already active, the placeholder stays
        # and auth.login_if_needed will skip typing it anyway.
        if not session_active and values["operator_phone"]:
            config.OPERATOR_PHONE = values["operator_phone"]

        args = argparse.Namespace(
            input_file=values["input_file"],
            debug=values["debug"],
            keep_open=values["keep_open"],
            playbook=None,
            no_playbook=False,
            profile_suffix=None,
        )
    else:
        ap = argparse.ArgumentParser(prog="python -m booking_bot")
        ap.add_argument("input_file", type=Path, help="path to Input/*.xlsx")
        ap.add_argument("--debug", action="store_true", help="verbose file logging")
        ap.add_argument(
            "--keep-open",
            action="store_true",
            help="on any error, dump visible chat state and wait for Enter "
            "before closing the browser — useful for tuning menu regexes",
        )
        ap.add_argument(
            "--playbook",
            type=Path,
            default=None,
            help="path to a recording JSONL file from `python -m booking_bot.record`. "
            "When omitted, the bot auto-selects the newest .jsonl in recordings/. "
            "Pass --no-playbook to force legacy hardcoded-pattern mode.",
        )
        ap.add_argument(
            "--no-playbook",
            action="store_true",
            help="disable auto-playbook selection and run with hardcoded menu patterns.",
        )
        ap.add_argument(
            "--headless",
            action="store_true",
            help="run Chrome without a visible window (background mode). "
            "Requires a previously-established session in .chrome-profile/ "
            "so no OTP prompt is needed. Skips the startup GUI dialog and "
            "does not allocate a console window.",
        )
        ap.add_argument(
            "--profile-suffix",
            type=str,
            default=None,
            help="tag this run with its own Chromium profile directory so "
            "multiple booking_bot instances can run in parallel from the "
            "same folder without colliding on the browser user-data lock. "
            "Example: --profile-suffix 2 uses .chromium-profile-2/ instead "
            "of .chromium-profile/ and writes Output/<name>-2.xlsx so the "
            "two runs don't overwrite each other. Each suffix is a separate "
            "HPCL session — first run of each new suffix needs its own OTP.",
        )
        args = ap.parse_args()
        if args.profile_suffix is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", args.profile_suffix):
                ap.error(
                    "--profile-suffix must be alphanumeric (dashes and "
                    "underscores allowed); got: " + repr(args.profile_suffix)
                )
            if len(args.profile_suffix) > 32:
                ap.error(
                    "--profile-suffix must be 32 characters or fewer "
                    f"(got {len(args.profile_suffix)}); long suffixes risk "
                    "pushing the Chromium user-data path past Windows MAX_PATH."
                )
        global _HEADLESS, _PROFILE_SUFFIX
        _HEADLESS = args.headless
        _PROFILE_SUFFIX = args.profile_suffix

    log_path = setup_logging(debug=args.debug)
    log.info(f"booking_bot starting; log file: {log_path}")
    log.info(f"input file: {args.input_file}")

    store = ExcelStore(args.input_file, output_suffix=args.profile_suffix)
    _write_heartbeat("starting", store)
    log.info(f"initial summary: {store.summary()}")
    log.info(store.progress_line())

    pb_path = _resolve_playbook_path(args.playbook, args.no_playbook)
    pb = None
    if pb_path is not None:
        pb = playbook_mod.load(pb_path, config.OPERATOR_PHONE)
        log.info(f"loaded playbook: {pb_path}")
        for line in pb.describe().splitlines():
            log.info(line)
    else:
        log.info("no playbook in use; running in legacy hardcoded-pattern mode")

    _install_signal_handler()

    # Auto-restart loop: _run_session_attempt runs one full browser session +
    # row-processing pass and closes its browser handles in a finally block
    # before returning or propagating. RestartableFatalError (from the OTP-
    # flood, 502-cascade, and unexpected-error circuit breakers) signals that
    # a fresh relaunch has a good chance of recovering the session — the
    # persistent chrome profile retains HPCL session cookies so the new
    # browser often lands already logged in. Bounded by MAX_AUTO_RESTARTS so
    # a genuinely stuck state fails loudly instead of looping forever.
    pre_handles = (_pre_pw, _pre_browser, _pre_ctx, _pre_page, _pre_frame)
    restarts_used = 0
    while True:
        try:
            _run_session_attempt(store, args, pb, pre_handles)
            break
        except RestartableFatalError as e:
            log.error(f"FATAL (restartable): {e}")
            if restarts_used >= config.MAX_AUTO_RESTARTS:
                log.error(
                    f"auto-restart budget exhausted "
                    f"({restarts_used}/{config.MAX_AUTO_RESTARTS}); giving up. "
                    f"Pending rows remain pending and will resume on the next run."
                )
                _pause_if_keep_open(args.keep_open, None)
                sys.exit(1)
            restarts_used += 1
            pre_handles = (None, None, None, None, None)
            browser.reset_gateway_flag()
            log.warning(
                f"auto-restart {restarts_used}/{config.MAX_AUTO_RESTARTS}: "
                f"waiting {config.AUTO_RESTART_WAIT_S}s before relaunching browser"
            )
            time.sleep(config.AUTO_RESTART_WAIT_S)

    final_phase = "completed" if store.summary()["pending"] == 0 else "failed"
    _write_heartbeat(final_phase, store)
    log.info(f"final summary: {store.summary()}")
    log.info(store.progress_line())
    log.info("booking_bot done")


def _shutdown_browser_shielded(ctx, browser_obj, pw) -> None:
    """Close Playwright handles with a bounded grace window when
    _force_shutdown is armed (double Ctrl-C). Clean exits still get
    unbounded time — the whole point of the persistent profile is that
    cookies survive. A wedged Playwright inside a shielded shutdown
    hard-exits after SHUTDOWN_GRACE_S via os._exit(130)."""
    import os
    import threading

    def _do_close():
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        if browser_obj is not None:
            try:
                browser_obj.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    if not _force_shutdown:
        _do_close()
        return

    t = threading.Thread(target=_do_close, daemon=True)
    t.start()
    t.join(timeout=config.SHUTDOWN_GRACE_S)
    if t.is_alive():
        log.error(
            f"browser shutdown still running after "
            f"{config.SHUTDOWN_GRACE_S}s grace window — hard-exiting"
        )
    os._exit(130)


def _quiet_retry_until_alive_or_dead(page, pb, store) -> str:
    """Section 3 of the survivability design. Enter a no-phone-number-typing
    loop that reloads the page every 60s and polls for a live chat state.
    Returns one of:

      "alive"     — state came back as READY_FOR_CUSTOMER / MAIN_MENU /
                    BOOK_FOR_OTHERS_MENU. Caller should resume; a fresh
                    frame is available via page.main_frame.
      "drained"   — quiet retry deadline elapsed AND pending_rows() is
                    empty. Batch naturally completed during the wait.
                    Caller should exit 0 without alarming the operator.
      "needs_otp" — quiet retry deadline elapsed AND pending_rows() is
                    non-empty. Fresh OTP would unblock real work.
                    Caller should fire the idle alarm and prompt for OTP.

    Crucially: this function NEVER calls login_if_needed. It never types
    the operator phone. Zero OTP SMS are triggered during quiet retry.
    That is the single behavioural difference from the old recovery path
    and the only reason the 3-hour cooldown is safe.
    """
    # Poll-only quiet retry: NEVER call page.reload() during this loop. The
    # old 60s reload cadence was wiping manual operator re-auths — operator
    # typed an OTP, HPCL accepted, 60 seconds later our reload destroyed the
    # freshly-issued session cookie and the chat flipped back to the login
    # screen. Polling detect_state on the live frame lets a manual auth take
    # effect the moment it lands (MAIN_MENU / READY_FOR_CUSTOMER transitions
    # are visible in the existing DOM without any reload). We trade away
    # HPCL's rare server-side self-heal (which required a reload to observe)
    # in exchange for not sabotaging operator recovery, which is the far more
    # common outcome during a cooldown-gated quiet retry.
    log.warning(
        f"quiet retry mode: polling every 3s for up to "
        f"{config.SESSION_DEAD_QUIET_RETRY_S}s — NO phone/OTP typing, "
        f"NO page reload (unless a newer shared_auth.json appears). "
        f"Operator can manually authenticate in the browser window and "
        f"the bot will resume automatically; a parallel instance "
        f"finishing its own auth will also unblock this one."
    )
    deadline = time.monotonic() + config.SESSION_DEAD_QUIET_RETRY_S
    poll_interval_s = 3.0
    alive_states = ("READY_FOR_CUSTOMER", "MAIN_MENU", "BOOK_FOR_OTHERS_MENU")
    last_logged_state: str | None = None
    # Seed the shared-auth-seen marker with whatever the file has right now
    # so we don't immediately re-inject on the first poll (start_browser
    # already injected this version). We'll only re-inject when another
    # instance writes a NEWER shared_auth.json timestamp.
    initial_shared = browser.read_shared_auth_state()
    last_shared_written_at: str | None = (
        initial_shared.get("written_at_utc") if initial_shared else None
    )

    while time.monotonic() < deadline:
        if _should_stop:
            log.warning("quiet retry: Ctrl-C received; exiting early")
            return "needs_otp"
        # Option B: watch shared_auth.json for fresher cookies from another
        # parallel instance. When a newer write lands, inject + reload once
        # so this instance can piggyback on that auth without prompting the
        # operator. The reload here is justified because it's grabbing a
        # NEWLY AVAILABLE session, not wiping one — and the next poll
        # iteration will re-check state, so if the injection worked the
        # alive-state check fires immediately after.
        try:
            shared = browser.read_shared_auth_state()
            if shared and shared.get("written_at_utc") != last_shared_written_at:
                log.info(
                    f"quiet retry: newer shared_auth.json detected "
                    f"(written_at={shared.get('written_at_utc')}); "
                    f"injecting cookies and reloading once"
                )
                last_shared_written_at = shared.get("written_at_utc")
                try:
                    page.context.add_cookies(shared["cookies"])
                except Exception as e:
                    log.warning(
                        f"quiet retry: add_cookies failed "
                        f"({type(e).__name__}: {e})"
                    )
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
                except Exception as e:
                    log.warning(
                        f"quiet retry: reload after shared-auth inject "
                        f"failed ({type(e).__name__}: {e})"
                    )
        except Exception as e:
            log.debug(
                f"quiet retry: shared-auth check error: "
                f"{type(e).__name__}: {e}"
            )
        try:
            frame = page.main_frame
            state = chat.detect_state(frame)
            # Throttle state logging: only log when it changes, so a long
            # wait doesn't flood the log with identical NEEDS_OPERATOR_AUTH
            # lines but still shows every real transition.
            if state != last_logged_state:
                log.info(f"quiet retry: state={state!r}")
                last_logged_state = state
            if state in alive_states:
                log.info(
                    "quiet retry: session alive — resuming "
                    "(operator manual auth, shared-auth transplant, or "
                    "HPCL self-heal)"
                )
                # Persist our own auth timestamp so this instance
                # participates in the 20h cooldown protection going
                # forward, AND write shared_auth.json so other stuck
                # parallel instances pick up this manual auth on their
                # next quiet-retry tick. Without this propagation, a
                # manual re-auth would heal this instance but leave
                # every other stuck parallel instance looping — which
                # defeats the entire point of Option B.
                browser.mark_auth_success()
                browser.write_shared_auth_state(page)
                return "alive"
        except Exception as e:
            log.debug(
                f"quiet retry poll error: {type(e).__name__}: {e} — "
                f"continuing to wait"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_s, remaining))

    log.warning(
        f"quiet retry: {config.SESSION_DEAD_QUIET_RETRY_S}s elapsed without "
        f"session recovery — running session-dead cleanup"
    )
    has_retriable, pending_count = _session_dead_cleanup_has_retriable_rows(store)
    if not has_retriable:
        log.info(
            "quiet retry: session dead BUT pending_rows() is empty — "
            "batch drained during the wait. Exiting cleanly with no "
            "operator alarm."
        )
        return "drained"
    log.error(
        f"SESSION DEAD — OPERATOR OTP REQUIRED — {pending_count} retriable "
        f"row(s) remain. Leaving browser open so the operator can paste "
        f"a fresh OTP once they arrive."
    )
    return "needs_otp"


def _session_dead_cleanup_has_retriable_rows(store) -> tuple[bool, int]:
    """Section 5 of the survivability design. Called after the 30-min quiet
    retry loop has given up waiting for HPCL's session to heal.

    Returns (has_retriable, pending_count):
      - has_retriable=False, pending_count=0 -> the batch drained to zero
        during the retry loop. Nothing a fresh OTP would unblock. Caller
        should exit cleanly without alarming the operator.
      - has_retriable=True,  pending_count=N -> N rows still need work.
        Every pending row is retriable (attempt_count < MAX_ATTEMPTS_PER_ROW
        by the Section 4 invariant — capped rows have col C='ISSUE' and
        never appear in pending_rows). Caller should fire the loud idle
        alarm and prompt the operator for a fresh OTP.
    """
    pending = list(store.pending_rows())
    return (len(pending) > 0, len(pending))


def _run_session_attempt(store, args, pb, pre_handles) -> None:
    """Run one browser session: launch (or reuse pre-launched handles),
    authenticate, process pending rows, close the browser. Called by
    main() in a loop so RestartableFatalError can trigger a full relaunch
    without any operator intervention.

    Closes its browser handles in a finally block before returning or
    propagating exceptions. Raises RestartableFatalError when a circuit
    breaker trips (caller handles the restart decision). FatalError
    causes sys.exit(1). Other exceptions are re-raised after logging the
    visible chat state."""
    pw, browser_obj, ctx, page, frame = pre_handles
    current_row_idx: int | None = None
    current_phone: str | None = None

    try:
        if pw is not None:
            # GUI bootstrap already opened the browser and loaded the chat.
            log.info("re-using pre-launched browser from GUI bootstrap")
        else:
            pw, browser_obj, ctx, page = browser.start_browser(
                headless=_HEADLESS,
                profile_suffix=_PROFILE_SUFFIX,
            )
            # Initial frame acquisition + settle can fail when HPCL navigates
            # the page mid-wait (observed: frame destroyed ~50s after load,
            # likely a stale profile cookie triggering a redirect). Retry with
            # a page reload — get_chat_frame re-acquires the frame, and the
            # stale session gets replaced by a fresh one on reload.
            startup_err: Exception | None = None
            for startup_attempt in (1, 2, 3):
                try:
                    frame = browser.get_chat_frame(page)
                    chat.wait_until_settled(frame)
                    startup_err = None
                    break
                except RECOVERABLE as e:
                    startup_err = e
                    log.warning(
                        f"startup frame settle attempt {startup_attempt} "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
                    except Exception as reload_e:
                        log.warning(f"reload during startup retry failed: {reload_e}")
            if startup_err is not None:
                raise startup_err
        if pb is not None:
            # Playbook mode: do operator login if the session isn't already
            # active, then let the recording drive all menu navigation.
            # HPCL's `/execute_button/...` endpoint occasionally returns 502
            # on the very first click after a fresh page load. Retry the auth
            # replay a few times — reloading the page between attempts via
            # _recover_with_playbook (which already handles login + replay) —
            # so a transient 502 on startup doesn't kill the whole batch.
            auth_result = login_if_needed(frame, config.OPERATOR_PHONE, _prompt_otp)
            if auth_result == "cooldown_wait":
                # Section 1 + Section 3: cooldown refused to type phone.
                # Enter quiet retry — 30 min of silent poll-and-watch.
                # Write phase="authenticating" so the orchestrator's
                # re-auth banner (monitor.build_operator_reauth_banner,
                # which fires on phase=='authenticating' + idle > 60s)
                # triggers ~1 min into the wait instead of staying
                # invisible at phase='starting' for the entire 30 min.
                _write_heartbeat("authenticating", store)
                outcome = _quiet_retry_until_alive_or_dead(page, pb, store)
                if outcome == "drained":
                    log.info(
                        "batch drained during quiet retry; exiting "
                        "_run_session_attempt cleanly"
                    )
                    return
                if outcome == "needs_otp":
                    if _HEADLESS:
                        # Parallel orchestrator chunks run --headless: no
                        # GUI, no stdin, no way to receive an OTP. If we
                        # fell through to login_if_needed here we would
                        # (a) type the operator phone → SMS to the
                        # operator → repeated across every parallel
                        # chunk that simultaneously exited quiet retry
                        # (OTP flood), and (b) hang inside
                        # wait_until_settled for up to STUCK_THRESHOLD_S
                        # with no hope of progress. Fail fast instead so
                        # the chunk dies cleanly and the operator can
                        # manually re-auth the slot's auth-seed and
                        # restart the chunk via the orchestrator.
                        last_err = (
                            "headless chunk: quiet retry exhausted while "
                            "session is dead. Refusing to type operator "
                            "phone (would flood OTP SMS with no listener). "
                            "Operator must re-authenticate this slot's "
                            "auth-seed and restart the chunk."
                        )
                        _write_heartbeat("failed", store, last_error=last_err)
                        raise FatalError(last_err)
                    # Interactive/headed mode: the operator can type an
                    # OTP via the GUI popup or terminal. Clear the
                    # cooldown file so login_if_needed accepts a new
                    # phone submission, re-fetch the frame, and call it
                    # again. The operator hears the idle alarm from
                    # _prompt_otp's watchdog thread.
                    browser.clear_auth_cooldown()
                    frame = browser.get_chat_frame(page)
                    auth_result = login_if_needed(
                        frame, config.OPERATOR_PHONE, _prompt_otp,
                    )
                    if auth_result == "cooldown_wait":
                        raise RestartableFatalError(
                            "cooldown_wait persisted even after "
                            "clear_auth_cooldown — Section 1 state file "
                            "is not being reset correctly"
                        )
                else:  # "alive"
                    frame = browser.get_chat_frame(page)
            last_err: Exception | None = None
            for auth_attempt in (1, 2, 3):
                try:
                    if auth_attempt == 1:
                        playbook_mod.replay_auth(frame, pb)
                    else:
                        frame = _recover_with_playbook(
                            page, pb, config.OPERATOR_PHONE, _prompt_otp,
                        )
                    last_err = None
                    break
                except RECOVERABLE as e:
                    last_err = e
                    log.warning(
                        f"startup auth attempt {auth_attempt} failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    time.sleep(config.RETRY_PAUSE_S)
            if last_err is not None:
                raise last_err
        else:
            full_auth(frame, config.OPERATOR_PHONE, _prompt_otp)

        # Multi-pass processing. Pass 1 hits every pending row. After the pass
        # we re-attempt any row that ended in a transient failure (unknown
        # state, playbook_stuck, recovery_failed, or an unexpected exception).
        # Terminal reasons (pending_payment, invalid_customer, already_booked,
        # invalid_phone_format) are left alone — retrying them won't change
        # HPCL's answer.
        # Circuit breaker counter for the 502-cascade pattern. Incremented
        # each time a row ends in recovery_failed / recovered_but_failed,
        # reset on any successful row (Success or terminal Issue). When it
        # exceeds config.MAX_CONSECUTIVE_ROW_FAILURES we abort with a
        # FatalError so the bot stops chewing through rows during a
        # sustained HPCL outage instead of marking everything skipped.
        consecutive_row_failures = 0

        for pass_num in range(1, MAX_PASSES + 1):
            _write_heartbeat("booking", store)
            pass_start = store.summary()
            log.info(
                f"=== pass {pass_num}/{MAX_PASSES} starting; "
                f"pending={pass_start['pending']} ==="
            )
            transient_rows: list[int] = []

            for row_idx, raw_phone in store.pending_rows():
                if _should_stop:
                    break
                current_row_idx = row_idx
                phone, err = normalize_phone(raw_phone)
                current_phone = phone or str(raw_phone)
                _write_heartbeat(
                    "booking", store,
                    current_row_idx=row_idx, current_phone=current_phone,
                )

                if err:
                    store.write_issue(row_idx, str(raw_phone), err,
                                      raw=f"input cell: {raw_phone!r}")
                    current_row_idx = None
                    current_phone = None
                    continue

                try:
                    result = None
                    for attempt in (1, 2):
                        try:
                            if pb is not None:
                                result = playbook_mod.replay_booking(frame, pb, phone)
                            else:
                                result = chat.book_one(frame, phone)
                            break
                        except RECOVERABLE as e:
                            log.warning(
                                f"row {row_idx} ({phone}) attempt {attempt} "
                                f"failed: {type(e).__name__}: {e}"
                            )
                            if attempt == 2:
                                result = chat.Issue(
                                    reason=f"recovered_but_failed:{type(e).__name__}",
                                    raw="",
                                )
                                break
                            # Recovery can itself fail (page reload dies, auth
                            # re-prompt times out, etc.). When it does, mark
                            # ONLY this row as an issue and move on.
                            try:
                                _write_heartbeat(
                                    "recovering", store,
                                    current_row_idx=row_idx,
                                    current_phone=current_phone,
                                )
                                if pb is not None:
                                    frame = _recover_with_playbook(
                                        page, pb, config.OPERATOR_PHONE, _prompt_otp,
                                    )
                                else:
                                    frame = browser.recover_session(
                                        page, config.OPERATOR_PHONE, _prompt_otp,
                                    )
                            except FatalError:
                                # OTP-flood circuit breaker tripped inside
                                # login_if_needed during recovery. Must NOT
                                # be caught here — it has to escape the row
                                # loop and reach cli.main()'s FatalError
                                # handler so the batch aborts cleanly.
                                raise
                            except Exception as rec_e:
                                log.error(
                                    f"recovery after row {row_idx} failed: "
                                    f"{type(rec_e).__name__}: {rec_e}"
                                )
                                result = chat.Issue(
                                    reason=f"recovery_failed:{type(rec_e).__name__}",
                                    raw=str(rec_e),
                                )
                                break
                            time.sleep(config.RETRY_PAUSE_S)

                    assert result is not None
                    # Track consecutive recovery failures for the 502-cascade
                    # circuit breaker. A "row failure" is a row that ended in
                    # the cli.py-level recovery path (recovered_but_failed or
                    # recovery_failed). Anything that produced a real HPCL
                    # response — Success, terminal Issue, even an unknown
                    # state — proves the chat is talking back, so it resets
                    # the counter.
                    is_recovery_failure = (
                        isinstance(result, chat.Issue)
                        and (
                            result.reason.startswith("recovered_but_failed")
                            or result.reason.startswith("recovery_failed")
                        )
                    )
                    if is_recovery_failure:
                        consecutive_row_failures += 1
                        log.warning(
                            f"row failure #{consecutive_row_failures}/"
                            f"{config.MAX_CONSECUTIVE_ROW_FAILURES}: "
                            f"{result.reason}"
                        )
                        if consecutive_row_failures >= config.MAX_CONSECUTIVE_ROW_FAILURES:
                            raise RestartableFatalError(
                                f"502-cascade circuit breaker tripped: "
                                f"{consecutive_row_failures} rows in a row failed "
                                f"in the recovery path. HPCL may be in a sustained "
                                f"outage or the bot's session may be wedged. "
                                f"Triggering in-process browser restart — the "
                                f"persistent profile retains session cookies, so "
                                f"a fresh launch often recovers without operator "
                                f"intervention."
                            )
                    else:
                        consecutive_row_failures = 0

                    if isinstance(result, chat.Success):
                        store.write_success(row_idx, result.code)
                    elif result.reason == "ekyc_not_done":
                        # HPCL blocks this customer's booking until Aadhaar
                        # eKYC is completed — it's a terminal state but not a
                        # bot failure, so skip the Issues workbook and write
                        # a human-readable label directly to col C.
                        store.mark_terminal(row_idx, "ekyc not done")
                    elif result.reason == "not_registered":
                        store.mark_terminal(row_idx, "not registered with HPCL")
                    elif result.reason == "pending_payment":
                        store.mark_terminal(row_idx, "payment pending")
                    else:
                        # Section 4 per-row attempt budget. Terminal reasons
                        # lock col C immediately — retrying won't change
                        # HPCL's verdict. Transient reasons get
                        # MAX_ATTEMPTS_PER_ROW chances across ANY number of
                        # restarts because the count is persisted in col D.
                        if _is_terminal_issue(result.reason):
                            store.write_issue(row_idx, phone, result.reason, result.raw)
                        else:
                            new_count = store.increment_attempt_count(row_idx)
                            if new_count >= config.MAX_ATTEMPTS_PER_ROW:
                                log.warning(
                                    f"row {row_idx}: attempt {new_count}/"
                                    f"{config.MAX_ATTEMPTS_PER_ROW} reached — "
                                    f"locking as ISSUE ({result.reason})"
                                )
                                store.write_issue(row_idx, phone, result.reason, result.raw)
                            else:
                                log.info(
                                    f"row {row_idx}: attempt {new_count}/"
                                    f"{config.MAX_ATTEMPTS_PER_ROW} failed "
                                    f"({result.reason}) — leaving pending"
                                )
                                transient_rows.append(row_idx)

                    log.info(store.progress_line())
                    _write_heartbeat(
                        "booking", store,
                        current_row_idx=row_idx, current_phone=current_phone,
                    )

                    # Post-row navigation. Clean Success leaves us on the
                    # customer-phone input (booking_body's tail is Previous
                    # Menu → Book for Others). Issue and salvaged Success need
                    # an explicit reset — the salvage path in playbook.py
                    # handles its own reset before returning, so here we only
                    # reset on non-Success.
                    if pb is not None:
                        if not isinstance(result, chat.Success):
                            try:
                                playbook_mod.reset_to_customer_entry(frame, pb)
                            except Exception as reset_e:
                                log.warning(
                                    f"post-issue reset failed: "
                                    f"{type(reset_e).__name__}: {reset_e}; "
                                    f"triggering full recovery"
                                )
                                try:
                                    frame = _recover_with_playbook(
                                        page, pb, config.OPERATOR_PHONE, _prompt_otp,
                                    )
                                except FatalError:
                                    raise
                                except Exception as rec_e:
                                    log.error(
                                        f"recovery after post-issue reset failed: "
                                        f"{type(rec_e).__name__}: {rec_e}"
                                    )
                    else:
                        try:
                            chat.click_option(frame, config.POST_ROW_NAV_LABELS)
                            chat.wait_until_settled(frame)
                        except RECOVERABLE as e:
                            log.warning(f"post-row nav failed after row {row_idx}: {e}")
                            _write_heartbeat(
                                "recovering", store,
                                current_row_idx=row_idx, current_phone=current_phone,
                            )
                            frame = browser.recover_session(
                                page, config.OPERATOR_PHONE, _prompt_otp,
                            )
                except (KeyboardInterrupt, FatalError):
                    # FatalError must propagate out of the row loop —
                    # the circuit breakers (rapid reauth in auth.py and
                    # consecutive row failures above) raise it to abort
                    # the batch cleanly. Catching it here would defeat
                    # the abort and let the bot keep looping through the
                    # 502 cascade forever.
                    raise
                except AdvisorSkipRow as skip_e:
                    # The advisor explicitly judged this row hopeless
                    # (e.g. payment pending). Lock as ISSUE and advance.
                    # A single skip is an intentional row drop, not a
                    # cascade — but N consecutive skips is a cascade,
                    # and the budget already caps both cases. If the
                    # budget has now exhausted, escalate so the outer
                    # loop restarts the browser and the advisor stops
                    # being consulted for the rest of the session.
                    log.warning(
                        f"row {row_idx} ({phone}): advisor chose skip_row "
                        f"(reason={skip_e.reason!r}); locking as ISSUE and advancing"
                    )
                    store.write_issue(
                        row_idx, phone,
                        reason=f"advisor_skipped:{skip_e.reason}",
                        raw="",
                    )
                    current_row_idx = None
                    current_phone = None
                    budget_local, _ = _get_advisor_state()
                    if budget_local.exhausted():
                        raise RestartableFatalError(
                            f"advisor skip cascade: budget exhausted "
                            f"(consecutive_skips={budget_local.consecutive_skips}/"
                            f"{budget_local.max_consecutive_skips} "
                            f"total_skips={budget_local.total_skips}/"
                            f"{budget_local.max_total_skips}). "
                            f"The advisor has given up on too many rows in a row. "
                            f"Triggering in-process browser restart — a fresh "
                            f"relaunch lets deterministic recovery retry these "
                            f"rows cleanly."
                        )
                    continue
                except Exception as row_e:
                    # Catch-all so a single row's unexpected failure never
                    # kills the whole batch. Mark the row as a transient
                    # issue, best-effort recover the frame, and move on.
                    log.error(
                        f"row {row_idx} ({phone}) unexpected error: "
                        f"{type(row_e).__name__}: {row_e}"
                    )
                    consecutive_row_failures += 1
                    log.warning(
                        f"row failure #{consecutive_row_failures}/"
                        f"{config.MAX_CONSECUTIVE_ROW_FAILURES} "
                        f"(unexpected:{type(row_e).__name__})"
                    )
                    if consecutive_row_failures >= config.MAX_CONSECUTIVE_ROW_FAILURES:
                        raise RestartableFatalError(
                            f"unexpected-error circuit breaker tripped: "
                            f"{consecutive_row_failures} rows in a row failed "
                            f"with unhandled exceptions. Triggering in-process "
                            f"browser restart — a fresh relaunch often recovers "
                            f"the session. If the restart budget is exhausted "
                            f"the batch will exit cleanly."
                        ) from row_e
                    # Section 4: unexpected exceptions also count against
                    # the attempt budget. Three unexpected errors on the
                    # same row => lock as ISSUE.
                    try:
                        new_count = store.increment_attempt_count(row_idx)
                        if new_count >= config.MAX_ATTEMPTS_PER_ROW:
                            log.warning(
                                f"row {row_idx}: unexpected error on "
                                f"attempt {new_count}/{config.MAX_ATTEMPTS_PER_ROW} "
                                f"— locking as ISSUE"
                            )
                            store.write_issue(
                                row_idx,
                                phone,
                                reason=f"unexpected:{type(row_e).__name__}",
                                raw=str(row_e)[:500],
                            )
                        else:
                            log.info(
                                f"row {row_idx}: unexpected error on "
                                f"attempt {new_count}/{config.MAX_ATTEMPTS_PER_ROW} "
                                f"— leaving pending"
                            )
                            transient_rows.append(row_idx)
                    except Exception as write_e:
                        log.error(f"  (could not write attempt_count: {write_e})")
                        transient_rows.append(row_idx)
                    try:
                        if pb is not None:
                            frame = _recover_with_playbook(
                                page, pb, config.OPERATOR_PHONE, _prompt_otp,
                            )
                        else:
                            frame = browser.recover_session(
                                page, config.OPERATOR_PHONE, _prompt_otp,
                            )
                    except FatalError:
                        raise
                    except Exception as rec_e:
                        log.error(
                            f"  (recovery after unexpected error failed: "
                            f"{type(rec_e).__name__}: {rec_e})"
                        )

                current_row_idx = None
                current_phone = None
                time.sleep(config.PACING_S)

            if _should_stop:
                break

            log.info(
                f"=== pass {pass_num} complete; "
                f"transient={len(transient_rows)}; "
                f"summary={store.summary()} ==="
            )
            log.info(store.progress_line())

            if not transient_rows:
                log.info("no transient failures; done processing")
                break

            if pass_num >= MAX_PASSES:
                log.warning(
                    f"reached MAX_PASSES={MAX_PASSES} with "
                    f"{len(transient_rows)} transient failures still unresolved"
                )
                break

            # Transient rows already have col C empty — Section 4's attempt
            # budget branch left them alone when it bumped col D. So
            # pending_rows() will yield them again on the next pass for
            # free. This log line is just a progress marker.
            log.info(
                f"{len(transient_rows)} transient row(s) will be retried on "
                f"pass {pass_num + 1}: {transient_rows}"
            )

        log.info(f"final summary: {store.summary()}")

    except RestartableFatalError:
        # Leave the row/phone written as an issue on the way out so the
        # operator sees which row was in flight when the breaker tripped.
        # main()'s outer loop decides whether to actually restart.
        if current_row_idx is not None:
            try:
                store.write_issue(
                    current_row_idx,
                    str(current_phone or ""),
                    reason="restart_triggered",
                    raw=chat.dump_visible_state(frame) if frame else "<no-frame>",
                )
            except Exception as write_e:
                log.error(f"  (could not write restart issue: {write_e})")
        raise
    except FatalError as e:
        log.error(f"FATAL: {e}")
        if current_row_idx is not None:
            store.write_issue(
                current_row_idx,
                str(current_phone or ""),
                reason=f"fatal_error:{type(e).__name__}",
                raw=chat.dump_visible_state(frame) if frame else "<no-frame>",
            )
        _pause_if_keep_open(args.keep_open, frame)
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt; shutting down")
    except Exception as e:
        # Any unhandled exception — log the visible chat state so Tier-3
        # tuning doesn't require reproducing the bug to see the buttons.
        log.error(f"UNHANDLED: {type(e).__name__}: {e}")
        _write_heartbeat("failed", store, last_error=str(e)[:500])
        if frame is not None:
            try:
                log.error(f"visible state at failure:\n{chat.dump_visible_state(frame)}")
            except Exception as inner:
                log.error(f"  (could not dump visible state: {inner})")
        _pause_if_keep_open(args.keep_open, frame)
        raise
    finally:
        # Close the context first (persistent mode) so cookies are flushed,
        # then the legacy Browser handle, then stop Playwright. Under
        # _force_shutdown (double Ctrl-C) the close runs in a background
        # thread with a SHUTDOWN_GRACE_S cap so a wedged Playwright can't
        # hold the terminal hostage.
        _shutdown_browser_shielded(ctx, browser_obj, pw)


# Session-scoped advisor state. Initialized lazily on first use so the
# bot never touches the Anthropic SDK unless it actually needs to.
_advisor_budget: AdvisorBudget | None = None
_advisor_store: IncidentStore | None = None


def _get_advisor_state():
    global _advisor_budget, _advisor_store
    if _advisor_budget is None:
        _advisor_budget = AdvisorBudget()
    if _advisor_store is None:
        _advisor_store = IncidentStore(config.ADVISOR_INCIDENTS_PATH)
    return _advisor_budget, _advisor_store


def _try_advisor_fallback(frame, page, pb, current_row_idx: int | None) -> str:
    """Called from _recover_with_playbook after deterministic recovery
    has raised. Returns one of:
      - "acted":    advisor picked an action (click/reload) and the
                    bot should continue with the returned frame.
      - "declined": advisor refused or returned None; caller should
                    fall back to the existing replay_auth path.
    On skip_row the AdvisorSkipRow exception propagates out to the
    row loop — this function never returns "skip_row".
    """
    if not config.ADVISOR_ENABLED:
        return "declined"
    budget, store = _get_advisor_state()

    try:
        current_state = chat.detect_state(frame)
    except Exception as e:
        log.warning(f"advisor fallback: detect_state failed ({e}); declining")
        return "declined"

    row_hint = None
    if current_row_idx is not None:
        row_hint = f"row {current_row_idx}"
    snapshot = build_snapshot(
        frame,
        state=current_state,
        recent_actions=[],
        row_hint=row_hint,
    )

    pre_state = snapshot.state
    pre_buttons = snapshot.enabled_buttons

    decision = consult(snapshot, store, budget, client=None)
    if decision is None:
        return "declined"

    outcome = apply_advisor_decision(decision, frame, page, budget=budget)

    if outcome != "acted":
        return "declined"

    try:
        chat.wait_until_settled(frame)
    except Exception as e:
        log.warning(f"advisor fallback: wait_until_settled failed ({e})")
        return "declined"

    try:
        new_state = chat.detect_state(frame)
    except Exception as e:
        log.warning(f"advisor fallback: post-action detect_state failed ({e})")
        return "declined"

    if new_state != pre_state and new_state in SAFE_RECOVERED_STATES:
        try:
            store.record_success(
                snapshot,
                decision,
                recovered_to=new_state,
            )
            log.info(
                f"advisor fallback: recorded success for "
                f"state={pre_state!r} buttons={list(pre_buttons)!r} "
                f"-> {new_state!r}"
            )
        except Exception as e:
            log.warning(f"advisor fallback: record_success failed ({e})")
    elif new_state != pre_state:
        log.warning(
            f"advisor fallback: state changed {pre_state!r} -> {new_state!r} "
            f"but target is not in SAFE_RECOVERED_STATES; refusing to "
            f"memorize (would cache-poison the corpus)"
        )

    return "acted"


def _recover_with_playbook(page, pb, operator_phone, get_otp):
    """Playbook-aware recovery: gateway-aware, reload only as a last resort.

    The old flow was "reload + re-auth if needed" which caused the OTP flood
    during gateway flaps: the reload itself would hit a 502, the second 502
    would destroy the session, and the operator would be prompted for OTP
    on every row. The new flow is:

      1. Wait GATEWAY_QUIESCE_S for HPCL's upstream to recover.
      2. Try to use the CURRENT frame in-place. If scroller is still
         populated and detect_state returns a known state, run
         reset_to_customer_entry without reloading.
      3. Only if the in-place attempt raises or the state is UNKNOWN do we
         fall back to a page reload + full recovery.

    reset_to_customer_entry handles the normal path (Main Menu →
    auth_prefix) and the alt path (Book With Other Mobile) — the latter
    matters because HPCL's chat state persists across page reloads via the
    operator session cookie, so a reload landing on the "not registered"
    alt menu would dead-end on blind auth_prefix replay.
    """
    log.warning(
        f"playbook recover: waiting {config.GATEWAY_QUIESCE_S}s for gateway "
        f"to quiesce before touching the page"
    )
    browser.reset_gateway_flag()
    time.sleep(config.GATEWAY_QUIESCE_S)

    # Fast path: the frame is still alive and the gateway hiccup was a
    # single-request 502. Poll the in-place state for up to
    # config.IN_PLACE_POLL_S seconds — reloads are what destroys the HPCL
    # session and triggers the OTP flood, so we try VERY hard to recover
    # without one.
    poll_deadline = time.monotonic() + config.IN_PLACE_POLL_S
    last_state = "UNKNOWN"
    while time.monotonic() < poll_deadline:
        try:
            if not browser._scroller_populated(page):
                last_state = "<empty>"
            else:
                frame = page.main_frame
                state = chat.detect_state(frame)
                last_state = state
                # Fast-path operator-auth/OTP states: reset_to_customer_entry
                # cannot possibly recover from these (no nav buttons exist on
                # the operator-phone or OTP screen), so polling is wasted
                # time. Break out of the in-place loop and go straight to
                # the reload+login_if_needed path which has the cooldown
                # logic that prevents typing into the operator field.
                # CRITICAL: without this fast-path, the prior code would
                # leave the bot in detect_state's NEEDS_OPERATOR_AUTH state
                # for IN_PLACE_POLL_S seconds, and any concurrent row
                # processing happening in the background could keep
                # typing customer phones into the operator field — that
                # is the prod incident this whole fix is about.
                if state in ("NEEDS_OPERATOR_AUTH", "NEEDS_OPERATOR_OTP"):
                    log.warning(
                        f"playbook recover: in-place state={state!r} — "
                        f"operator auth surface visible. Skipping in-place "
                        f"reset (no nav buttons exist on this screen) and "
                        f"going straight to reload+login_if_needed which "
                        f"will honor the auth cooldown."
                    )
                    break
                if state != "UNKNOWN":
                    log.info(
                        f"playbook recover: in-place state={state!r}; "
                        f"resetting without reload"
                    )
                    try:
                        playbook_mod.reset_to_customer_entry(frame, pb)
                        return frame
                    except (OptionNotFoundError, ChatStuckError) as reset_e:
                        log.warning(
                            f"playbook recover: in-place reset failed "
                            f"({type(reset_e).__name__}: {reset_e}); "
                            f"continuing to poll"
                        )
        except Exception as e:
            log.debug(
                f"playbook recover: in-place poll error "
                f"({type(e).__name__}: {e})"
            )
        time.sleep(1.0)
    log.info(
        f"playbook recover: in-place gave up after {config.IN_PLACE_POLL_S}s "
        f"(last={last_state!r}); falling back to reload"
    )

    log.warning("playbook recover: reloading page")
    browser.reset_gateway_flag()
    page.reload(wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
    if browser.gateway_flag():
        log.warning(
            f"playbook recover: reload hit gateway error; waiting "
            f"{config.GATEWAY_RELOAD_WAIT_S}s before proceeding"
        )
        browser.reset_gateway_flag()
        time.sleep(config.GATEWAY_RELOAD_WAIT_S)
    frame = browser.get_chat_frame(page)
    auth_result = login_if_needed(frame, operator_phone, get_otp)
    if auth_result == "cooldown_wait":
        # Cooldown refused — surface as Restartable so the outer main
        # loop takes over. Task 4's startup quiet retry will catch it.
        raise RestartableFatalError(
            "auth cooldown active mid-recovery — refusing to type "
            "operator phone. Outer loop will handle quiet retry."
        )
    try:
        playbook_mod.reset_to_customer_entry(frame, pb)
    except (OptionNotFoundError, ChatStuckError, GatewayError) as e:
        log.warning(
            f"reset after reload failed ({type(e).__name__}: {e})"
        )
        # Advisor fallback: when deterministic recovery has genuinely
        # exhausted, consult the AI advisor. On decline we fall through
        # to the existing replay_auth path.
        advisor_handled = _try_advisor_fallback(
            frame, page, pb, current_row_idx=None,
        )
        if advisor_handled == "acted":
            return frame
        log.warning("advisor declined; falling back to replay_auth")
        playbook_mod.replay_auth(frame, pb)
    return frame


def _close_browser_handles(ctx, pw) -> None:
    """Best-effort shutdown for the GUI bootstrap path: close the Playwright
    context (flushes the persistent profile) then stop the driver. Used
    when the operator cancels the dialog after the browser has already
    been pre-launched."""
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def _pause_if_keep_open(keep_open: bool, frame) -> None:
    """When --keep-open is set, print a banner and block on input() so the
    operator can inspect the still-visible browser window before Playwright
    tears it down in the finally: clause."""
    if not keep_open:
        return
    print("\n" + "=" * 60)
    print("--keep-open: browser is paused. Inspect the Chrome window,")
    print("then press Enter here to close it and exit.")
    print("=" * 60, flush=True)
    stop = _start_idle_alert()
    try:
        input()
    except EOFError:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
