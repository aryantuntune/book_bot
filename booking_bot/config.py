"""All tunables, paths, selectors, and compiled regex patterns. No imports of
other booking_bot modules — this module is a pure leaf."""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ---- Paths ----
# When running from source, ROOT is the repo root. When running from a
# PyInstaller bundle (single .exe), ROOT is the dir containing the .exe so
# Output/, Issues/, logs/, and .chrome-profile/ land next to the binary
# rather than inside the temp extraction dir (which PyInstaller wipes on
# exit and would lose all state). RESOURCES_ROOT points at the bundle's
# _MEIPASS extraction dir so we can still read bundled recordings/.
if getattr(sys, "frozen", False):
    ROOT           = Path(sys.executable).resolve().parent
    RESOURCES_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT           = Path(__file__).resolve().parent.parent
    RESOURCES_ROOT = ROOT
INPUT_DIR  = ROOT / "Input"
OUTPUT_DIR = ROOT / "Output"
ISSUES_DIR = ROOT / "Issues"
LOGS_DIR   = ROOT / "logs"

# ---- Target ----
# Direct chatbot URL. Navigating to https://myhpgas.in and clicking the
# launcher leads here via two nested iframes; going direct gives us the same
# DOM at document level with no iframe drilling. The ?data= token is the
# HPCL campaign id discovered in recon and is static across sessions.
URL = (
    "https://hpchatbot.hpcl.co.in/pwa/view?data="
    "eyJlSWQiOjEwMCwiZ2xpIjp0cnVlLCJjYW1wYWlnbklkIjoiNjQ1MjAyZTNhMTdlMTZhY2RlOTNhMjhmIiwibGkiOiI4OWJiNzZlYTZlNmY0OTVjOTAwNTc3M2I1MGEzNDMyMSJ9"
)
OPERATOR_PHONE = "9209114429"   # operator edits this to their own number

# ---- Timing (seconds unless suffixed _MS) ----
# Tuned 2026-04-14 for throughput: original values were PACING_S=4.5 and
# SETTLE_QUIET_MS=1500. wait_until_settled already blocks until the loader
# disappears, so PACING_S is pure inter-booking padding — dropped to 1.5s.
# SETTLE_QUIET_MS was dropped to 800ms; HPCL's trailing bubbles almost always
# arrive within the first 500ms after the loader goes away.
PAGE_LOAD_WAIT_S      = 3
SETTLE_QUIET_MS       = 800
STUCK_THRESHOLD_S     = 60
PACING_S              = 1.5
RETRY_PAUSE_S         = 2
GET_FRAME_TIMEOUT_S   = 30
MAX_NAV_HOPS          = 6
MAX_STEPS_PER_BOOKING = 5
# How many independent Issue outcomes a single row gets before col C is
# locked to literal "ISSUE" and the row stops appearing in pending_rows().
# Survives restarts because the count is persisted in col D of the Output
# workbook.
MAX_ATTEMPTS_PER_ROW  = 3

# ---- Gateway recovery backoff ----
# Wait this long after a GatewayError for HPCL's upstream to recover before
# attempting any recovery action. Reloading into an ongoing 502 burst
# destroys the session and forces a full operator re-auth (the OTP flood
# pattern). A 20s quiesce is enough for most transient gateway flaps.
GATEWAY_QUIESCE_S     = 20
# If we DO need to reload and the reload itself hits a gateway error, wait
# this much longer before the next reload attempt. Prevents tight reload
# loops through a flapping gateway.
GATEWAY_RELOAD_WAIT_S = 45
# When the chat state comes back as NEEDS_OPERATOR_AUTH within this many
# seconds of a successful auth, treat it as suspect — HPCL's PWA sometimes
# flashes the login screen briefly during a reload even though the session
# is still server-side valid. We re-check once before prompting the operator
# for an OTP they just typed.
RECENT_AUTH_WINDOW_S  = 90
RECENT_AUTH_RECHECK_S = 15

# ---- Circuit breakers (added 2026-04-14 after the OTP-flood incident) ----
# These hard limits stop the bot from repeating the same failure forever
# rather than skipping rows endlessly when HPCL is in a sustained outage.
#
# MAX_CONSECUTIVE_ROW_FAILURES: abort the batch after this many rows in a
# row end in recovery_failed / recovered_but_failed. A single bad row is
# fine; ten in a row means we're stuck in a 502 cascade and nothing we
# do is helping. The operator should rerun later when HPCL recovers.
MAX_CONSECUTIVE_ROW_FAILURES = 5
# MAX_CONSECUTIVE_REAUTHS: how many times in a row login_if_needed is
# allowed to detect NEEDS_OPERATOR_AUTH within RECENT_AUTH_WINDOW_S of a
# previous successful auth before we abort. The recent-auth recheck is
# our first line of defence; this is the second, in case the session
# really is being destroyed every reload and prompting more OTPs would
# burn through the operator's daily quota for nothing.
#
# 2026-04-15: briefly lowered to 1 during investigation of the OTP flood,
# then reverted to 3 after the operator reported that the aggressive
# setting made session recovery WORSE: at 1, the very first auth flash
# triggered a full browser restart, and the new process lost the
# in-memory `_last_auth_at_monotonic` timestamp — so the recent-auth
# guard no longer fired on the new process, and the bot fell through to
# prompting for a fresh OTP. Net effect: lost the live session on every
# gateway hiccup. With 3, the first 2 rapid re-auths stay in-process
# (type phone + OTP, session re-established, counter resets) and only a
# sustained cascade of 3 triggers the browser restart.
MAX_CONSECUTIVE_REAUTHS      = 3
# IN_PLACE_POLL_S: how long recover_session / _recover_with_playbook
# should keep polling the in-place frame for a non-UNKNOWN state before
# falling back to a full page reload. Larger values mean more chances to
# avoid a session-destroying reload during a transient flap.
IN_PLACE_POLL_S              = 30

# ---- Auto-restart (RestartableFatalError handling) ----
# When a circuit breaker trips with RestartableFatalError, cli.main() closes
# the browser, waits, and relaunches from scratch. The persistent chrome
# profile keeps HPCL's session cookies, so a relaunch usually lands on a live
# session without prompting the operator. MAX_AUTO_RESTARTS caps the number
# of times this can happen in a single run so a truly stuck state (e.g. real
# sustained outage) doesn't loop forever.
MAX_AUTO_RESTARTS            = 5
AUTO_RESTART_WAIT_S          = 30

# ---- Idle alert (manual-input watchdog) ----
# When the bot is blocked on a manual input (OTP prompt, --keep-open pause)
# for more than IDLE_ALERT_AFTER_S, start beeping the device every
# IDLE_ALERT_INTERVAL_S until the input is received. Prevents the operator
# from missing a stuck bot that's silently waiting on a dialog.
IDLE_ALERT_AFTER_S           = 120
IDLE_ALERT_INTERVAL_S        = 30

# ---- DOM selectors ----
OUTER_IFRAME_SEL = "iframe#webform"
INNER_IFRAME_SEL = "iframe[name='iframe']"
SEL_TEXTAREA     = "textarea.replybox"
SEL_SUBMIT       = "button.reply-submit"
SEL_OPTION       = "button.dynamic-message-button"
SEL_LOADER       = ".load-container"
SEL_SCROLLER     = "#scroller"

# ---- Success detection (strict; see spec §6.3) ----
SUCCESS_RE = re.compile(
    r"delivery\s+confirmation\s+code\s+is\s+(\d{6})(?!\d)",
    re.IGNORECASE,
)

# ---- Labels / state patterns. All compiled with re.IGNORECASE. ----
_FLAGS = re.IGNORECASE

def _compile_list(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, _FLAGS) for p in patterns]

AFFIRMATIVE_LABELS = _compile_list([
    r"^yes", r"continue", r"confirm", r"proceed",
    r"go\s*on", r"book\s+now", r"^ok$",
])

AUTH_NAV_SEQUENCE = [
    _compile_list([r"booking\s+services", r"refill"]),
    _compile_list([r"book\s+for\s+others", r"for\s+others"]),
]

POST_ROW_NAV_LABELS = _compile_list([
    r"book\s+for\s+others",
    r"book\s+another",
    r"new\s+booking",
    r"for\s+others",
])

STATE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "BOOK_FOR_OTHERS_MENU": _compile_list([r"book\s+for\s+others"]),
    "MAIN_MENU":            _compile_list([r"booking\s+services"]),
    "READY_FOR_CUSTOMER":   _compile_list([
        r"customer.*mobile",
        r"mobile\s+number\s+of\s+the\s+customer",
    ]),
    "NEEDS_OPERATOR_OTP":   _compile_list([r"otp.*sent", r"enter\s+otp"]),
    "NEEDS_OPERATOR_AUTH":  _compile_list([
        r"please\s+enter\s+your\s+10[- ]digit\s+mobile",
    ]),
}

# ---- Gateway error signatures ----
GATEWAY_STATUS_CODES = {502, 503, 504}
GATEWAY_URL_RE = re.compile(r"error|gateway|nginx", re.IGNORECASE)
