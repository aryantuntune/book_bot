"""All tunables, paths, selectors, and compiled regex patterns. No imports of
other booking_bot modules — this module is a pure leaf."""
from __future__ import annotations

import re
from pathlib import Path

# ---- Paths ----
ROOT       = Path(__file__).resolve().parent.parent
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
PAGE_LOAD_WAIT_S      = 4
SETTLE_QUIET_MS       = 1500
STUCK_THRESHOLD_S     = 60
PACING_S              = 4.5
RETRY_PAUSE_S         = 2
GET_FRAME_TIMEOUT_S   = 30
MAX_NAV_HOPS          = 6
MAX_STEPS_PER_BOOKING = 5
MAX_ATTEMPTS_PER_ROW  = 2

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
    _compile_list([r"^main\s+menu$", r"main\s+menu"]),
    _compile_list([r"booking\s+services", r"refill", r"book\s+cylinder"]),
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
