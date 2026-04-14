"""Top-level orchestration. Command:

    python -m booking_bot Input/file1.xlsx [--debug]

Flow: load Excel → launch browser → authenticate once → iterate pending rows
with a 2-attempt retry policy → write results → pace → summary.
"""
from __future__ import annotations

import argparse
import logging
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
from booking_bot.exceptions import (
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

_should_stop = False


def _install_signal_handler() -> None:
    """First Ctrl-C: set the stop flag and restore the default SIGINT
    handler. The bot will finish the current row and exit cleanly. Second
    Ctrl-C (now handled by the default) raises KeyboardInterrupt and
    unwinds immediately — the finally clause still closes the browser."""
    def _h(signum, frame):
        global _should_stop
        log.warning(
            f"received signal {signum}; finishing current row then stopping. "
            "Press Ctrl-C again to force immediate exit."
        )
        _should_stop = True
        signal.signal(signal.SIGINT, signal.default_int_handler)
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
            from booking_bot.exceptions import ChromeNotInstalledError
            import tkinter as _tk
            import tkinter.messagebox as _mb
            _root = _tk.Tk(); _root.withdraw()
            if isinstance(e, ChromeNotInstalledError):
                _mb.showerror("Google Chrome not installed", str(e))
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
        args = ap.parse_args()
        global _HEADLESS
        _HEADLESS = args.headless

    log_path = setup_logging(debug=args.debug)
    log.info(f"booking_bot starting; log file: {log_path}")
    log.info(f"input file: {args.input_file}")

    store = ExcelStore(args.input_file)
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

    log.info(f"final summary: {store.summary()}")
    log.info(store.progress_line())
    log.info("booking_bot done")


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
                # Enter quiet retry (Task 4 replaces this placeholder body
                # with the real loop). For now, raise Restartable so the
                # outer auto-restart re-enters a fresh process.
                raise RestartableFatalError(
                    "auth cooldown active at startup — session appears "
                    "dead but AUTH_COOLDOWN_S has not elapsed. "
                    "Entering auto-restart."
                )
            last_err: Exception | None = None
            for auth_attempt in (1, 2, 3):
                try:
                    if auth_attempt == 1:
                        playbook_mod.replay_auth(
                            frame, pb, config.OPERATOR_PHONE, _prompt_otp,
                        )
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
        if frame is not None:
            try:
                log.error(f"visible state at failure:\n{chat.dump_visible_state(frame)}")
            except Exception as inner:
                log.error(f"  (could not dump visible state: {inner})")
        _pause_if_keep_open(args.keep_open, frame)
        raise
    finally:
        # Close the context first (persistent mode) so cookies are flushed
        # to .chrome-profile/, then close the legacy Browser handle if
        # present (non-persistent mode), then stop Playwright.
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
        # reset couldn't find a usable nav target — fall back to the legacy
        # replay_auth path (blindly clicks Booking Services → Book for Others)
        # so we still have a chance of landing on phone entry for sessions
        # that came up fresh on the main menu.
        log.warning(
            f"reset after reload failed ({type(e).__name__}: {e}); "
            f"falling back to replay_auth"
        )
        playbook_mod.replay_auth(frame, pb, operator_phone, get_otp)
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
