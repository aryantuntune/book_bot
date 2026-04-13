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
import time
from getpass import getpass
from pathlib import Path

from booking_bot import browser, chat, config
from booking_bot.auth import full_auth
from booking_bot.excel import ExcelStore
from booking_bot.exceptions import (
    ChatStuckError,
    FatalError,
    GatewayError,
    IframeLostError,
    OptionNotFoundError,
)
from booking_bot.logging_setup import setup_logging

log = logging.getLogger("cli")

RECOVERABLE = (ChatStuckError, GatewayError, IframeLostError, OptionNotFoundError)


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


def _prompt_otp() -> str:
    """Blocking prompt. getpass() so the OTP doesn't show on the console."""
    otp = getpass(f"Enter OTP for {config.OPERATOR_PHONE}: ").strip()
    return otp


# -------- Main loop --------

_should_stop = False


def _install_signal_handler() -> None:
    def _h(signum, frame):
        global _should_stop
        log.warning(f"received signal {signum}; will stop after current row")
        _should_stop = True
    signal.signal(signal.SIGINT, _h)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m booking_bot")
    ap.add_argument("input_file", type=Path, help="path to Input/*.xlsx")
    ap.add_argument("--debug", action="store_true", help="verbose file logging")
    ap.add_argument(
        "--keep-open",
        action="store_true",
        help="on any error, dump visible chat state and wait for Enter "
        "before closing the browser — useful for tuning menu regexes",
    )
    args = ap.parse_args()

    log_path = setup_logging(debug=args.debug)
    log.info(f"booking_bot starting; log file: {log_path}")
    log.info(f"input file: {args.input_file}")

    store = ExcelStore(args.input_file)
    log.info(f"initial summary: {store.summary()}")

    _install_signal_handler()

    pw = browser_obj = ctx = page = frame = None
    current_row_idx: int | None = None
    current_phone: str | None = None

    try:
        pw, browser_obj, ctx, page = browser.start_browser()
        frame = browser.get_chat_frame(page)
        chat.wait_until_settled(frame)
        full_auth(frame, config.OPERATOR_PHONE, _prompt_otp)

        for row_idx, raw_phone in store.pending_rows():
            if _should_stop:
                break
            current_row_idx = row_idx
            phone, err = normalize_phone(raw_phone)
            current_phone = phone or str(raw_phone)

            if err:
                store.write_issue(row_idx, str(raw_phone), err,
                                  raw=f"input cell: {raw_phone!r}")
                continue

            result = None
            for attempt in (1, 2):
                try:
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
                    frame = browser.recover_session(
                        page, config.OPERATOR_PHONE, _prompt_otp,
                    )
                    time.sleep(config.RETRY_PAUSE_S)

            assert result is not None  # one of the two branches always sets it
            if isinstance(result, chat.Success):
                store.write_success(row_idx, result.code)
            else:
                store.write_issue(row_idx, phone, result.reason, result.raw)

            # Post-row navigation: set up the chat for the next row. A failure
            # here never corrupts the already-saved current row.
            try:
                chat.click_option(frame, config.POST_ROW_NAV_LABELS)
                chat.wait_until_settled(frame)
            except RECOVERABLE as e:
                log.warning(f"post-row nav failed after row {row_idx}: {e}")
                frame = browser.recover_session(
                    page, config.OPERATOR_PHONE, _prompt_otp,
                )

            current_row_idx = None
            current_phone = None
            time.sleep(config.PACING_S)

        log.info(f"final summary: {store.summary()}")

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
        log.info(f"final summary: {store.summary()}")
        log.info("booking_bot done")


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
    try:
        input()
    except EOFError:
        pass


if __name__ == "__main__":
    main()
