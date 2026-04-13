# HP Gas Booking Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `booking_bot` Python package described in `docs/superpowers/specs/2026-04-13-hp-gas-booking-bot-design.md` — a Playwright-driven bot that reads customer phone numbers from `Input/*.xlsx`, drives the myhpgas.in chatbot, and writes 6-digit delivery confirmation codes back to `Output/*.xlsx`.

**Architecture:** Single-process, single-browser, modular Python package. Playwright sync API, visible Chromium. Each module < ~150 lines. Excel is the durable I/O surface (atomic per-row saves). TDD for all pure helpers; integration code (browser/chat/auth) is covered by a Tier-2 smoke test plus the operator-assisted Tier-3 live walkthrough that follows implementation.

**Tech Stack:** Python 3.12, Playwright 1.47, openpyxl 3.1.5, xlrd 1.2.0 (legacy .xls read), colorlog 6.8.2, pytest 8.x (dev).

---

## File Structure

Files created or modified by this plan:

| Path | Responsibility |
|---|---|
| `requirements.txt` | Runtime + dev deps |
| `pytest.ini` | Minimal pytest config |
| `booking_bot/__init__.py` | Marker file |
| `booking_bot/__main__.py` | `python -m booking_bot` entry → `cli.main()` |
| `booking_bot/config.py` | Paths, timing knobs, selectors, compiled regex patterns |
| `booking_bot/exceptions.py` | Typed exception hierarchy |
| `booking_bot/logging_setup.py` | Colored console + line-buffered file handler |
| `booking_bot/excel.py` | `ExcelStore` — the only module that touches openpyxl/xlrd |
| `booking_bot/browser.py` | Playwright lifecycle, iframe drilling, gateway listener, `recover_session` |
| `booking_bot/auth.py` | Operator phone + OTP → `READY_FOR_CUSTOMER` state |
| `booking_bot/chat.py` | `send_text`, `click_option`, `wait_until_settled`, `detect_state`, `book_one`, `dump_visible_state` |
| `booking_bot/cli.py` | `normalize_phone`, orchestration loop, retry policy, FatalError handler |
| `tests/test_success_regex.py` | TDD for `SUCCESS_RE` (no false positives) |
| `tests/test_normalize_phone.py` | TDD for phone coercion |
| `tests/test_excel_store.py` | TDD for ExcelStore resume / atomic save / Issues file |
| `tests/test_detect_state.py` | TDD for pure `_classify_state` helper |
| `tests/test_smoke.py` | Tier-2: loads myhpgas.in, drills iframes, asserts welcome text |
| `README.md` | Setup + run instructions |

Directories created (empty, committed via `.gitkeep`): `Input/`, `Output/`, `Issues/`, `logs/`.

---

## Task 1: Project scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `pytest.ini`
- Create: `booking_bot/__init__.py`
- Create: `booking_bot/__main__.py`
- Create: `Input/.gitkeep`, `Output/.gitkeep`, `Issues/.gitkeep`, `logs/.gitkeep`
- Create: `.gitignore`

- [ ] **Step 1: Initialize git repo (idempotent)**

Run: `git init && git status`
Expected: `On branch main` (or `master`) with the existing spec file visible as untracked/tracked depending on prior state.

- [ ] **Step 2: Create `requirements.txt`**

```
playwright==1.47.0
openpyxl==3.1.5
xlrd==1.2.0
colorlog==6.8.2
pytest==8.3.3
```

- [ ] **Step 3: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = -ra
```

- [ ] **Step 4: Create `.gitignore`**

```
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.venv/
venv/
logs/*.log
Output/*.xlsx
Issues/*.xlsx
*.xlsx.tmp
```

- [ ] **Step 5: Create package skeleton**

`booking_bot/__init__.py`:
```python
"""HP Gas booking bot package."""
```

`booking_bot/__main__.py`:
```python
from booking_bot.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Create empty directories with `.gitkeep` files**

Create four empty files: `Input/.gitkeep`, `Output/.gitkeep`, `Issues/.gitkeep`, `logs/.gitkeep`.

- [ ] **Step 7: Install dependencies**

Run: `pip install -r requirements.txt && python -m playwright install chromium`
Expected: `Successfully installed playwright-1.47.0 ...` then chromium downloads.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt pytest.ini .gitignore booking_bot/ Input/.gitkeep Output/.gitkeep Issues/.gitkeep logs/.gitkeep
git commit -m "chore: initial project scaffolding"
```

---

## Task 2: Config module

**Files:**
- Create: `booking_bot/config.py`

- [ ] **Step 1: Write `booking_bot/config.py`**

```python
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
URL            = "https://myhpgas.in"
OPERATOR_PHONE = "9XXXXXXXXX"   # operator edits this to their own number

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
    r"delivery\s+confirmation\s+code\s+is\s+(\d{6})",
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
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot import config; print(config.OPERATOR_PHONE)"`
Expected: `9XXXXXXXXX`

- [ ] **Step 3: Commit**

```bash
git add booking_bot/config.py
git commit -m "feat: add config module with paths, selectors, and compiled patterns"
```

---

## Task 3: Exception module

**Files:**
- Create: `booking_bot/exceptions.py`

- [ ] **Step 1: Write `booking_bot/exceptions.py`**

```python
"""Typed exception hierarchy. See spec §8.1."""


class BookingBotError(Exception):
    """Base class so callers can catch the whole hierarchy if they want."""


class GatewayError(BookingBotError):
    """502/503/504 or nginx error page observed on the chatbot domain."""


class ChatStuckError(BookingBotError):
    """wait_until_settled timed out (loader never cleared, or scroller never
    stabilized)."""


class IframeLostError(BookingBotError):
    """Inner chat frame detached or could not be found within the timeout."""


class AuthFailedError(BookingBotError):
    """Auth navigation lost — expected menu button not found."""


class OptionNotFoundError(BookingBotError):
    """click_option() could not match any of the requested label patterns
    against visible dynamic-message-button elements."""


class FatalError(BookingBotError):
    """Unrecoverable: the top-level cli loop writes an ISSUE row and exits."""
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.exceptions import FatalError; raise FatalError('x')" 2>&1 | head -1`
Expected: traceback ending in `FatalError: x`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/exceptions.py
git commit -m "feat: add exceptions module"
```

---

## Task 4: Logging setup

**Files:**
- Create: `booking_bot/logging_setup.py`

- [ ] **Step 1: Write `booking_bot/logging_setup.py`**

```python
"""Configure root logger: colored console handler (INFO+) and a line-buffered
file handler (INFO+, DEBUG with debug=True). The file handler flushes after
every record so the operator can tail the log in a second terminal."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import colorlog

from booking_bot import config


class FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after every record."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(debug: bool = False) -> Path:
    """Install console + file handlers on the root logger. Returns the file
    path so cli.main() can print it at startup."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = config.LOGS_DIR / f"booking_bot_{ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(name)-12s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    console = colorlog.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s" + fmt,
        datefmt=datefmt,
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "white",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    root.addHandler(console)

    file_handler = FlushingFileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_handler)

    return log_path
```

- [ ] **Step 2: Smoke-run the logger**

Run:
```bash
python -c "
from booking_bot.logging_setup import setup_logging
import logging
p = setup_logging()
logging.getLogger('test').info('hello')
print('LOG:', p)
"
```
Expected: a colored line `... INFO test hello` on the console, then `LOG: .../logs/booking_bot_<ts>.log`. Verify the file exists and contains the same line.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/logging_setup.py
git commit -m "feat: add logging setup with colored console and flushing file handler"
```

---

## Task 5: SUCCESS_RE tests (TDD the regex via fixtures)

**Files:**
- Create: `tests/test_success_regex.py`

- [ ] **Step 1: Write the failing test**

```python
"""Verify SUCCESS_RE matches real bot success messages and rejects near-misses
and strings containing stray 6-digit numbers. Strict no-false-positives."""
import pytest

from booking_bot.config import SUCCESS_RE


# --- Must match: return the code ---
POSITIVE_CASES = [
    (
        "Your HP Gas Refill has been successfully booked with reference "
        "number 1260669600118310 and your delivery confirmation code is 764260",
        "764260",
    ),
    (
        "Your delivery confirmation code is 000001",
        "000001",
    ),
    (
        "DELIVERY CONFIRMATION CODE IS 123456 please keep it safe",
        "123456",
    ),
    (
        "...your delivery   confirmation\ncode is\t999888",  # whitespace variants
        "999888",
    ),
]

# --- Must NOT match: would be a false positive ---
NEGATIVE_CASES = [
    "Your reference number is 1260669600118310. Have a nice day!",
    "delivery confirmation code: 764260",              # colon, no "is"
    "delivery confirmation code is 7642",              # only 4 digits
    "delivery confirmation code is 76426099",          # 8 digits — must not capture 764260
    "delivery code is 764260",                         # missing "confirmation"
    "Please enter your 10-digit Mobile number",
    "Booking failed. Please try again later.",
    "",
]


@pytest.mark.parametrize("text, expected_code", POSITIVE_CASES)
def test_success_re_matches(text, expected_code):
    m = SUCCESS_RE.search(text)
    assert m is not None, f"expected match in: {text!r}"
    assert m.group(1) == expected_code


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_success_re_rejects(text):
    assert SUCCESS_RE.search(text) is None, f"false positive in: {text!r}"
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_success_regex.py -v`
Expected: all PASS (Task 2 already compiled `SUCCESS_RE` correctly). If any NEGATIVE_CASES false-positive, the regex needs tightening — fix `config.py` and re-run. Especially watch the `76426099` case: the regex uses `(\d{6})` not `\b\d{6}\b`, but because `delivery confirmation code is ` is a literal anchor, `76426099` will *not* match via the prefix (since after the prefix there are 8 digits, and `\d{6}` will match the first 6 → `764260`). **If that test fails, update the regex to require a word boundary after the 6 digits:**

```python
SUCCESS_RE = re.compile(
    r"delivery\s+confirmation\s+code\s+is\s+(\d{6})(?!\d)",
    re.IGNORECASE,
)
```

Then re-run and confirm PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_success_regex.py booking_bot/config.py
git commit -m "test: validate SUCCESS_RE against fixture strings with strict negative cases"
```

---

## Task 6: normalize_phone helper (TDD)

**Files:**
- Create: `booking_bot/cli.py` (partial — just `normalize_phone` + imports for now)
- Create: `tests/test_normalize_phone.py`

- [ ] **Step 1: Write the failing test**

```python
"""Test phone number coercion. Accepts str / int / float / None from Excel
cells; returns (cleaned_phone, error_or_None)."""
import pytest

from booking_bot.cli import normalize_phone


VALID_CASES = [
    ("9876543210",   "9876543210"),
    (9876543210,     "9876543210"),
    (9876543210.0,   "9876543210"),
    ("+919876543210","9876543210"),
    ("919876543210", "9876543210"),
    ("  9876543210 ","9876543210"),
    ("98765-43210",  "9876543210"),
    ("(987) 654-3210","9876543210"),
]

INVALID_CASES = [
    "",
    "12345",
    "98765432100",              # 11 digits
    "abc",
    None,
    9876543210.5,               # fractional
    ["9876543210"],             # wrong type
]


@pytest.mark.parametrize("raw, cleaned", VALID_CASES)
def test_normalize_phone_accepts(raw, cleaned):
    out, err = normalize_phone(raw)
    assert err is None
    assert out == cleaned


@pytest.mark.parametrize("raw", INVALID_CASES)
def test_normalize_phone_rejects(raw):
    out, err = normalize_phone(raw)
    assert err == "invalid_phone_format"
    assert out == ""
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_normalize_phone.py -v`
Expected: `ImportError: cannot import name 'normalize_phone' from 'booking_bot.cli'` (module doesn't exist yet) — this counts as failing.

- [ ] **Step 3: Implement `normalize_phone`**

Create `booking_bot/cli.py`:
```python
"""Top-level orchestration. In this task, only normalize_phone is implemented —
the main() loop is added in Task 20."""
from __future__ import annotations

import re


def normalize_phone(raw: object) -> tuple[str, str | None]:
    """Coerce an Excel cell into a canonical 10-digit phone string.

    Returns (cleaned_phone, error_reason). error_reason is None on success and
    'invalid_phone_format' otherwise. Accepts:
      - 10-digit strings
      - +91 / 91 prefixed 12-digit strings
      - int cells (e.g. 9876543210)
      - whole-number float cells (e.g. 9876543210.0)
      - strings with spaces, dashes, parentheses
    """
    if isinstance(raw, bool):  # bool is a subclass of int; reject early
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_normalize_phone.py -v`
Expected: all 15 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/cli.py tests/test_normalize_phone.py
git commit -m "feat: add normalize_phone helper with TDD coverage"
```

---

## Task 7: ExcelStore — init and pending_rows (TDD)

**Files:**
- Create: `booking_bot/excel.py`
- Create: `tests/test_excel_store.py`

- [ ] **Step 1: Write the failing test**

```python
"""TDD for ExcelStore.__init__ (copy input→output on first run, reuse existing
output on resume) and pending_rows (yields (row_idx, raw_cell) for rows with
empty col C and non-empty col B)."""
from pathlib import Path

import openpyxl
import pytest

from booking_bot.excel import ExcelStore


def _make_input(tmp_path: Path, rows: list[tuple]) -> Path:
    """Create an Input xlsx with columns A (consumer no.), B (phone)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    p = tmp_path / "Input" / "file1.xlsx"
    p.parent.mkdir(parents=True)
    wb.save(p)
    return p


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """Point config paths at tmp_path and hand back (input_path, store_cls)."""
    from booking_bot import config
    monkeypatch.setattr(config, "INPUT_DIR",  tmp_path / "Input")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "Output")
    monkeypatch.setattr(config, "ISSUES_DIR", tmp_path / "Issues")
    (tmp_path / "Output").mkdir()
    (tmp_path / "Issues").mkdir()
    return tmp_path


def test_first_run_copies_input_to_output(store_env):
    inp = _make_input(store_env, [("C1", "9876543210"), ("C2", "9123456789")])
    store = ExcelStore(inp)
    out = store_env / "Output" / "file1.xlsx"
    assert out.exists()
    wb = openpyxl.load_workbook(out)
    ws = wb.active
    assert ws.cell(row=1, column=1).value == "C1"
    assert ws.cell(row=1, column=2).value == "9876543210"


def test_pending_rows_yields_unfilled_rows(store_env):
    inp = _make_input(store_env, [
        ("C1", "9876543210"),
        ("C2", 9123456789),
        ("C3", ""),            # empty phone → skipped
        ("C4", "9000000000"),
    ])
    store = ExcelStore(inp)
    rows = list(store.pending_rows())
    assert [r[0] for r in rows] == [1, 2, 4]
    assert rows[0][1] == "9876543210"
    assert rows[1][1] == 9123456789
    assert rows[2][1] == "9000000000"


def test_pending_rows_skips_already_filled(store_env, monkeypatch):
    """Rows where col C already has a value (code or 'ISSUE') are not yielded."""
    inp = _make_input(store_env, [
        ("C1", "9876543210"),
        ("C2", "9123456789"),
        ("C3", "9000000000"),
    ])
    # First run: mark row 2 as filled in Output, then create a new store.
    store = ExcelStore(inp)
    # Manually write to Output to simulate a prior partial run.
    out = store_env / "Output" / "file1.xlsx"
    wb = openpyxl.load_workbook(out)
    wb.active.cell(row=2, column=3).value = "764260"
    wb.save(out)

    store2 = ExcelStore(inp)
    pending = [r[0] for r in store2.pending_rows()]
    assert pending == [1, 3]


def test_pending_rows_skips_none_phone(store_env):
    inp = _make_input(store_env, [
        ("C1", None),
        ("C2", "9876543210"),
    ])
    store = ExcelStore(inp)
    assert [r[0] for r in store.pending_rows()] == [2]
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_excel_store.py -v`
Expected: `ModuleNotFoundError: No module named 'booking_bot.excel'`.

- [ ] **Step 3: Implement `ExcelStore.__init__` and `pending_rows`**

Create `booking_bot/excel.py`:
```python
"""ExcelStore — the only module in the package that touches openpyxl / xlrd.

Design notes (see spec §7):
- Output is the source of truth for resume. Pending = col C empty, col B not None.
- pending_rows() does NOT validate; it yields raw cell values. The cli layer
  calls normalize_phone() on each.
- All writes go through atomic save: write to <path>.tmp then os.replace().
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Iterator

import openpyxl

from booking_bot import config

log = logging.getLogger("excel")


class ExcelStore:
    def __init__(self, input_path: Path) -> None:
        self.input_path = Path(input_path)
        self.output_path = config.OUTPUT_DIR / self.input_path.name
        self.issues_path = config.ISSUES_DIR / self.input_path.name

        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        config.ISSUES_DIR.mkdir(parents=True, exist_ok=True)

        # Handle .xls legacy input (§7.1). Normalize self.input_path to .xlsx.
        if self.input_path.suffix.lower() == ".xls":
            self.input_path = self._convert_xls_to_xlsx(self.input_path)
            self.output_path = config.OUTPUT_DIR / self.input_path.name

        if not self.output_path.exists():
            shutil.copy2(self.input_path, self.output_path)
            log.info(f"created Output workbook: {self.output_path}")
        else:
            log.info(f"resuming existing Output workbook: {self.output_path}")

        self._wb = openpyxl.load_workbook(self.output_path)
        self._ws = self._wb.active
        self._issues_wb: openpyxl.Workbook | None = None
        self._issues_ws = None

    def _convert_xls_to_xlsx(self, xls_path: Path) -> Path:
        """Implemented in Task 10."""
        raise NotImplementedError("xls conversion added in Task 10")

    # ---- Resume iteration ----

    def pending_rows(self) -> Iterator[tuple[int, object]]:
        """Yield (row_idx, raw_col_B_value) for rows where col B is not None AND
        col C is empty/whitespace. Operates on the Output workbook, starting at
        min_row=1 (no header row)."""
        for row in self._ws.iter_rows(min_row=1, values_only=False):
            # row is a tuple of Cell objects for that row.
            row_idx = row[0].row
            col_b = row[1].value if len(row) > 1 else None
            col_c = row[2].value if len(row) > 2 else None
            if col_b is None:
                continue
            if col_c is not None and str(col_c).strip() != "":
                continue
            yield (row_idx, col_b)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_excel_store.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/excel.py tests/test_excel_store.py
git commit -m "feat: ExcelStore init and pending_rows with TDD coverage"
```

---

## Task 8: ExcelStore.write_success with atomic save (TDD)

**Files:**
- Modify: `booking_bot/excel.py`
- Modify: `tests/test_excel_store.py`

- [ ] **Step 1: Add failing tests for write_success**

Append to `tests/test_excel_store.py`:

```python
def test_write_success_writes_code(store_env):
    inp = _make_input(store_env, [("C1", "9876543210"), ("C2", "9123456789")])
    store = ExcelStore(inp)

    store.write_success(1, "764260")

    wb = openpyxl.load_workbook(store_env / "Output" / "file1.xlsx")
    assert wb.active.cell(row=1, column=3).value == "764260"
    assert wb.active.cell(row=2, column=3).value in (None, "")


def test_write_success_is_atomic(store_env):
    """After a successful write, the .tmp sibling must not exist."""
    inp = _make_input(store_env, [("C1", "9876543210")])
    store = ExcelStore(inp)
    store.write_success(1, "111111")
    tmp = store_env / "Output" / "file1.xlsx.tmp"
    assert not tmp.exists()


def test_write_success_updates_pending_iteration(store_env):
    inp = _make_input(store_env, [
        ("C1", "9876543210"),
        ("C2", "9123456789"),
    ])
    store = ExcelStore(inp)
    store.write_success(1, "222222")

    store2 = ExcelStore(inp)
    assert [r[0] for r in store2.pending_rows()] == [2]
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_excel_store.py -v -k write_success`
Expected: 3 failures with `AttributeError: 'ExcelStore' object has no attribute 'write_success'`.

- [ ] **Step 3: Implement `write_success` + atomic save helper**

Append to `booking_bot/excel.py`:
```python
    # ---- Writes ----

    def write_success(self, row_idx: int, code: str) -> None:
        """Write the 6-digit code to col C of row_idx, then atomically save."""
        self._ws.cell(row=row_idx, column=3).value = code
        self._atomic_save(self._wb, self.output_path)
        log.info(f"row {row_idx}: success code={code}")

    @staticmethod
    def _atomic_save(wb: openpyxl.Workbook, path: Path) -> None:
        """Save to <path>.tmp then os.replace — atomic on NTFS for same-FS
        renames. Never leaves a half-written .xlsx."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        wb.save(tmp)
        os.replace(tmp, path)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_excel_store.py -v`
Expected: all previous + 3 new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/excel.py tests/test_excel_store.py
git commit -m "feat: ExcelStore.write_success with atomic save"
```

---

## Task 9: ExcelStore.write_issue with lazy Issues file (TDD)

**Files:**
- Modify: `booking_bot/excel.py`
- Modify: `tests/test_excel_store.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_excel_store.py`:
```python
def test_write_issue_marks_output_and_creates_issues(store_env):
    inp = _make_input(store_env, [("C1", "9876543210")])
    store = ExcelStore(inp)

    store.write_issue(1, "9876543210", reason="unexpected_state",
                      raw="bot said: please try later")

    out = openpyxl.load_workbook(store_env / "Output" / "file1.xlsx")
    assert out.active.cell(row=1, column=3).value == "ISSUE"

    issues_path = store_env / "Issues" / "file1.xlsx"
    assert issues_path.exists()
    iss = openpyxl.load_workbook(issues_path)
    row = [iss.active.cell(row=1, column=c).value for c in range(1, 5)]
    assert row[0] == "C1"               # consumer no mirrored from input col A
    assert row[1] == "9876543210"
    assert "unexpected_state" in row[2]
    assert "please try later" in row[2]
    assert "row 1" in row[3]


def test_write_issue_appends_multiple(store_env):
    inp = _make_input(store_env, [("C1", "9876543210"), ("C2", "9123456789")])
    store = ExcelStore(inp)
    store.write_issue(1, "9876543210", reason="r1", raw="raw1")
    store.write_issue(2, "9123456789", reason="r2", raw="raw2")

    iss = openpyxl.load_workbook(store_env / "Issues" / "file1.xlsx")
    assert iss.active.max_row == 2
    assert iss.active.cell(row=2, column=2).value == "9123456789"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_excel_store.py -v -k write_issue`
Expected: 2 failures with `AttributeError: ... write_issue`.

- [ ] **Step 3: Implement `write_issue`**

Append to `booking_bot/excel.py`:
```python
    def write_issue(self, row_idx: int, phone: str, reason: str, raw: str) -> None:
        """Mark col C = 'ISSUE' in Output; append a row to Issues (create the
        workbook on first call). Issues columns:
          A: consumer number (mirrored from Output col A)
          B: phone (cleaned or raw cell, whatever the caller has)
          C: reason + raw chatbot text (joined with ' | ')
          D: cross-reference to Output row ('row N in Output/<file>')
        """
        self._ws.cell(row=row_idx, column=3).value = "ISSUE"
        self._atomic_save(self._wb, self.output_path)

        self._ensure_issues_workbook()
        consumer_no = self._ws.cell(row=row_idx, column=1).value
        next_row = self._issues_ws.max_row + 1 if self._issues_ws.max_row > 1 or \
            self._issues_ws.cell(row=1, column=1).value is not None else 1
        self._issues_ws.cell(row=next_row, column=1).value = consumer_no
        self._issues_ws.cell(row=next_row, column=2).value = phone
        self._issues_ws.cell(row=next_row, column=3).value = f"{reason} | {raw}"
        self._issues_ws.cell(row=next_row, column=4).value = (
            f"row {row_idx} in Output/{self.output_path.name}"
        )
        self._atomic_save(self._issues_wb, self.issues_path)
        log.warning(f"row {row_idx}: ISSUE ({reason})")

    def _ensure_issues_workbook(self) -> None:
        """Lazily create or open the Issues workbook."""
        if self._issues_wb is not None:
            return
        if self.issues_path.exists():
            self._issues_wb = openpyxl.load_workbook(self.issues_path)
        else:
            self._issues_wb = openpyxl.Workbook()
        self._issues_ws = self._issues_wb.active
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_excel_store.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/excel.py tests/test_excel_store.py
git commit -m "feat: ExcelStore.write_issue with lazy Issues workbook"
```

---

## Task 10: ExcelStore — .xls conversion and summary (TDD)

**Files:**
- Modify: `booking_bot/excel.py`
- Modify: `tests/test_excel_store.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_excel_store.py`:
```python
def test_summary_reports_counts(store_env):
    inp = _make_input(store_env, [
        ("C1", "9876543210"),
        ("C2", "9123456789"),
        ("C3", "9000000000"),
        ("C4", "9111111111"),
    ])
    store = ExcelStore(inp)
    store.write_success(1, "111111")
    store.write_issue(2, "9123456789", "unexpected_state", "raw")
    # rows 3, 4 still pending

    s = store.summary()
    assert s == {"total": 4, "success": 1, "issue": 1, "pending": 2}
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_excel_store.py -v -k summary`
Expected: `AttributeError: ... summary`.

- [ ] **Step 3: Implement `summary()` and `_convert_xls_to_xlsx()`**

Append `summary` to `booking_bot/excel.py`:
```python
    def summary(self) -> dict[str, int]:
        """Count total / success / issue / pending rows by inspecting col C."""
        total = success = issue = pending = 0
        for row in self._ws.iter_rows(min_row=1, values_only=True):
            phone = row[1] if len(row) > 1 else None
            code  = row[2] if len(row) > 2 else None
            if phone is None:
                continue
            total += 1
            if code is None or str(code).strip() == "":
                pending += 1
            elif str(code).strip().upper() == "ISSUE":
                issue += 1
            else:
                success += 1
        return {"total": total, "success": success, "issue": issue, "pending": pending}
```

Replace the `_convert_xls_to_xlsx` stub with the real implementation:
```python
    def _convert_xls_to_xlsx(self, xls_path: Path) -> Path:
        """Read legacy .xls via xlrd and write out a .xlsx copy next to it."""
        import xlrd                              # lazy import — heavy
        book = xlrd.open_workbook(str(xls_path))
        sheet = book.sheet_by_index(0)
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in range(sheet.nrows):
            for c in range(sheet.ncols):
                ws.cell(row=r + 1, column=c + 1).value = sheet.cell_value(r, c)
        new_path = xls_path.with_suffix(".xlsx")
        wb.save(new_path)
        log.info(f"converted {xls_path.name} -> {new_path.name}")
        return new_path
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_excel_store.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/excel.py tests/test_excel_store.py
git commit -m "feat: ExcelStore.summary and .xls→.xlsx legacy conversion"
```

---

## Task 11: browser.py — start_browser + get_chat_frame

**Files:**
- Create: `booking_bot/browser.py`

Integration code — no Tier-1 TDD; validated by the Tier-2 smoke test in Task 22.

- [ ] **Step 1: Write `booking_bot/browser.py` (partial)**

```python
"""Playwright lifecycle, iframe drilling, gateway listener, and recover_session.

This file is split across three tasks: Task 11 adds start_browser +
get_chat_frame, Task 12 adds the gateway listener, Task 19 adds recover_session.
"""
from __future__ import annotations

import logging
import time

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from booking_bot import config
from booking_bot.exceptions import IframeLostError

log = logging.getLogger("browser")

# Thread-local-ish state for the gateway listener (Task 12). One-process bot.
_gateway_error_seen = False


def reset_gateway_flag() -> None:
    global _gateway_error_seen
    _gateway_error_seen = False


def gateway_flag() -> bool:
    return _gateway_error_seen


def start_browser() -> tuple[Playwright, Browser, BrowserContext, Page]:
    """Launch a visible Chromium, return (pw, browser, ctx, page). Caller owns
    the handles and must call browser.close() / pw.stop() at shutdown."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 850},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    )
    page = ctx.new_page()
    log.info(f"browser launched; navigating to {config.URL}")
    page.goto(config.URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)
    return pw, browser, ctx, page


def get_chat_frame(page: Page) -> Frame:
    """Drill into iframe#webform → iframe[name='iframe'] and return the inner
    Frame. Retries internally for up to GET_FRAME_TIMEOUT_S seconds. Raises
    IframeLostError on failure."""
    deadline = time.monotonic() + config.GET_FRAME_TIMEOUT_S
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            outer_el = page.wait_for_selector(
                config.OUTER_IFRAME_SEL, timeout=5_000, state="attached",
            )
            outer_frame = outer_el.content_frame()
            if outer_frame is None:
                raise IframeLostError("outer frame has no content_frame")
            inner_el = outer_frame.wait_for_selector(
                config.INNER_IFRAME_SEL, timeout=5_000, state="attached",
            )
            inner_frame = inner_el.content_frame()
            if inner_frame is None:
                raise IframeLostError("inner frame has no content_frame")
            inner_frame.wait_for_load_state("domcontentloaded", timeout=10_000)
            return inner_frame
        except (PWTimeoutError, IframeLostError) as e:
            last_err = e
            time.sleep(0.5)
    raise IframeLostError(
        f"could not attach inner chat frame within {config.GET_FRAME_TIMEOUT_S}s: {last_err}"
    )
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.browser import start_browser, get_chat_frame; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/browser.py
git commit -m "feat: browser lifecycle and iframe drilling with retry"
```

---

## Task 12: browser.py — gateway-flag network listener

**Files:**
- Modify: `booking_bot/browser.py`

- [ ] **Step 1: Add listener installation function**

In `booking_bot/browser.py`, add this function (after `get_chat_frame`):

```python
def install_gateway_listener(page: Page) -> None:
    """Install page.on('response') and page.on('framenavigated') listeners that
    flip _gateway_error_seen True when:
      - any response from the hpchatbot.hpcl.co.in domain has status in
        GATEWAY_STATUS_CODES
      - any frame navigates to a URL whose path matches GATEWAY_URL_RE

    The flag is read AND reset by chat.wait_until_settled on every call."""

    def _on_response(response):
        global _gateway_error_seen
        try:
            url = response.url
            if "hpchatbot.hpcl.co.in" in url and response.status in config.GATEWAY_STATUS_CODES:
                log.warning(f"gateway error response: {response.status} {url}")
                _gateway_error_seen = True
        except Exception:
            pass  # ignore listener-thread errors

    def _on_framenav(frame):
        global _gateway_error_seen
        try:
            if config.GATEWAY_URL_RE.search(frame.url or ""):
                log.warning(f"frame navigated to gateway-ish url: {frame.url}")
                _gateway_error_seen = True
        except Exception:
            pass

    page.on("response", _on_response)
    page.on("framenavigated", _on_framenav)
```

Then at the bottom of `start_browser`, call it before returning:
```python
    install_gateway_listener(page)
    return pw, browser, ctx, page
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.browser import install_gateway_listener, reset_gateway_flag, gateway_flag; reset_gateway_flag(); assert gateway_flag() is False; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/browser.py
git commit -m "feat: gateway error network listener"
```

---

## Task 13: chat.py — send_text, click_option, _scroller_snapshot

**Files:**
- Create: `booking_bot/chat.py`

- [ ] **Step 1: Write `booking_bot/chat.py` (partial)**

```python
"""Chat primitives for the inner Twixor frame. Split across tasks 13-17:
  Task 13: send_text, click_option, _scroller_snapshot
  Task 14: wait_until_settled
  Task 15: detect_state (+ testable pure helper)
  Task 16: dump_visible_state
  Task 17: book_one state machine
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

from playwright.sync_api import Frame, TimeoutError as PWTimeoutError

from booking_bot import config
from booking_bot.exceptions import (
    ChatStuckError,
    GatewayError,
    IframeLostError,
    OptionNotFoundError,
)

log = logging.getLogger("chat")


# ---- Result types used by book_one (Task 17) ----

@dataclass
class Success:
    code: str
    raw: str


@dataclass
class Issue:
    reason: str
    raw: str


BookingResult = Success | Issue


# ---- Snapshot dataclass used by wait_until_settled (Task 14) ----

@dataclass
class Snapshot:
    text: str           # the NEW text added (diff vs before), or full text
    child_count: int
    hash: str


# ---- Private helpers ----

def _scroller_snapshot(frame: Frame) -> Snapshot:
    """Capture a stable fingerprint of #scroller. Raises IframeLostError if the
    frame is detached (we swallow PW errors and translate them)."""
    try:
        data = frame.evaluate(
            """
            () => {
              const s = document.querySelector('#scroller');
              if (!s) return {text: '', children: 0};
              return {text: s.innerText || '', children: s.children.length};
            }
            """
        )
    except Exception as e:
        raise IframeLostError(f"scroller_snapshot: {e}") from e
    text = data["text"] or ""
    children = int(data["children"] or 0)
    h = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
    return Snapshot(text=text, child_count=children, hash=h)


def _loader_visible(frame: Frame) -> bool:
    try:
        return bool(frame.evaluate(
            f"""
            () => {{
              const el = document.querySelector('{config.SEL_LOADER}');
              if (!el) return false;
              const cs = getComputedStyle(el);
              if (cs.display === 'none' || cs.visibility === 'hidden') return false;
              return el.offsetParent !== null;
            }}
            """
        ))
    except Exception:
        return False


# ---- Public primitives ----

def send_text(frame: Frame, text: str) -> None:
    """Focus textarea.replybox, clear existing content, type the text, click
    submit. The clear step is essential — leftover content from a prior
    interaction would otherwise be concatenated."""
    try:
        frame.focus(config.SEL_TEXTAREA)
        frame.evaluate(
            f"() => {{ const t = document.querySelector('{config.SEL_TEXTAREA}'); "
            f"if (t) {{ t.value = ''; t.focus(); }} }}"
        )
        frame.fill(config.SEL_TEXTAREA, text)
        frame.click(config.SEL_SUBMIT)
        log.debug(f"sent text: {text!r}")
    except PWTimeoutError as e:
        raise IframeLostError(f"send_text timeout: {e}") from e


def click_option(frame: Frame, label_patterns: Iterable[re.Pattern[str]]) -> str:
    """Click the first *visible* button.dynamic-message-button whose text
    matches one of label_patterns (in priority order). Returns the matched
    button text. Raises OptionNotFoundError if none match."""
    try:
        buttons = frame.evaluate(
            f"""
            () => Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
                .filter(b => b.offsetParent !== null)
                .map(b => ({{ text: (b.innerText || '').trim(), id: b.id }}))
            """
        )
    except Exception as e:
        raise IframeLostError(f"click_option read buttons: {e}") from e

    for pat in label_patterns:
        for b in buttons:
            if pat.search(b["text"] or ""):
                sel = f"{config.SEL_OPTION}#{b['id']}" if b["id"] else \
                      f"{config.SEL_OPTION}:has-text('{b['text']}')"
                try:
                    frame.click(sel, timeout=5_000)
                    log.debug(f"clicked option: {b['text']!r} (pattern {pat.pattern})")
                    return b["text"]
                except PWTimeoutError as e:
                    raise IframeLostError(f"click_option click: {e}") from e
    raise OptionNotFoundError(
        f"no visible option matched {[p.pattern for p in label_patterns]}; "
        f"visible options were: {[b['text'] for b in buttons]}"
    )
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.chat import send_text, click_option, Success, Issue, Snapshot; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/chat.py
git commit -m "feat: chat send_text, click_option, scroller_snapshot helper"
```

---

## Task 14: chat.py — wait_until_settled with first-activity gate

**Files:**
- Modify: `booking_bot/chat.py`

- [ ] **Step 1: Append `wait_until_settled` to `booking_bot/chat.py`**

```python
def wait_until_settled(frame: Frame, timeout: float | None = None) -> Snapshot:
    """Wait until the chatbot has fully processed the last interaction, then
    return a Snapshot whose .text contains ONLY the content added since entry.

    Algorithm (see spec §6.1):
      1. Reset the gateway-error flag (any flag raised now is from THIS call).
      2. Capture a 'before' snapshot of #scroller.
      3. Poll every 500ms:
         - if gateway flag set → raise GatewayError
         - if frame detached → raise IframeLostError
         - compute 'now' snapshot
      4. First-activity gate: require either (a) the loader has been seen
         visible at least once, or (b) the scroller hash has changed at least
         once. Without this, a caller that invokes us right after send_text()
         could return with an empty diff if the bot hasn't started yet.
      5. Settled = loader currently hidden AND scroller hash unchanged for
         SETTLE_QUIET_MS (1500ms).
      6. Timeout → ChatStuckError.
    """
    # Late import to avoid a cycle at module load (chat ← browser ← chat).
    from booking_bot import browser

    timeout_s = timeout if timeout is not None else config.STUCK_THRESHOLD_S
    deadline = time.monotonic() + timeout_s
    poll_ms = 500
    quiet_target_ms = config.SETTLE_QUIET_MS

    browser.reset_gateway_flag()
    before = _scroller_snapshot(frame)

    activity_seen = False
    last_change_time: float | None = None
    last_hash = before.hash

    while time.monotonic() < deadline:
        if browser.gateway_flag():
            raise GatewayError("gateway flag set during wait_until_settled")

        try:
            now = _scroller_snapshot(frame)
        except IframeLostError:
            raise

        loader = _loader_visible(frame)

        if loader:
            activity_seen = True

        if now.hash != last_hash:
            activity_seen = True
            last_change_time = time.monotonic()
            last_hash = now.hash

        if activity_seen and not loader and last_change_time is not None:
            quiet_ms = (time.monotonic() - last_change_time) * 1000
            if quiet_ms >= quiet_target_ms:
                # Settled. Return the diff.
                new_text = now.text[len(before.text):] if \
                    now.text.startswith(before.text) else now.text
                return Snapshot(
                    text=new_text,
                    child_count=now.child_count,
                    hash=now.hash,
                )

        time.sleep(poll_ms / 1000)

    raise ChatStuckError(
        f"wait_until_settled timeout after {timeout_s}s "
        f"(activity_seen={activity_seen}, last_hash_changed_at={last_change_time})"
    )
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.chat import wait_until_settled; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/chat.py
git commit -m "feat: wait_until_settled with first-activity gate"
```

---

## Task 15: chat.py — detect_state (TDD the pure helper)

**Files:**
- Modify: `booking_bot/chat.py`
- Create: `tests/test_detect_state.py`

- [ ] **Step 1: Write the failing test**

```python
"""detect_state is split into _classify_state (pure function of button labels
+ scroller text, trivially testable) and the thin Frame wrapper. We TDD the
pure helper with canned inputs that mirror what the live DOM would provide."""
import pytest

from booking_bot.chat import _classify_state


@pytest.mark.parametrize("buttons, scroller, expected", [
    # Book-for-others menu visible
    (["Book for others", "Cancel"], "some prior bot text", "BOOK_FOR_OTHERS_MENU"),
    # Main menu visible
    (["Booking Services", "Complaints"], "welcome to hpcl", "MAIN_MENU"),
    # Ready for customer phone
    ([], "please enter the mobile number of the customer", "READY_FOR_CUSTOMER"),
    ([], "Customer Mobile Number:", "READY_FOR_CUSTOMER"),
    # OTP wait
    ([], "OTP sent to your registered mobile", "NEEDS_OPERATOR_OTP"),
    ([], "please enter otp", "NEEDS_OPERATOR_OTP"),
    # Operator auth
    ([], "Please enter your 10-digit Mobile number", "NEEDS_OPERATOR_AUTH"),
    # Unknown — nothing matches
    (["Foo", "Bar"], "random text", "UNKNOWN"),
    ([], "", "UNKNOWN"),
])
def test_classify_state(buttons, scroller, expected):
    assert _classify_state(buttons, scroller) == expected


def test_button_takes_priority_over_text():
    """A 'Book for others' button wins even if the scroller text also matches
    a main-menu pattern."""
    assert _classify_state(
        ["Book for others"],
        "Please select one of: Booking Services, Complaints",
    ) == "BOOK_FOR_OTHERS_MENU"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_detect_state.py -v`
Expected: `ImportError: cannot import name '_classify_state' from 'booking_bot.chat'`.

- [ ] **Step 3: Implement `_classify_state` + `detect_state` wrapper**

Append to `booking_bot/chat.py`:
```python
def _classify_state(button_labels: list[str], scroller_text: str) -> str:
    """Pure classifier — priority 1 is button labels, priority 2 is scroller
    text. Returns one of config.STATE_PATTERNS' keys or 'UNKNOWN'."""
    blob_buttons = " | ".join(button_labels)
    for state_name, patterns in config.STATE_PATTERNS.items():
        for p in patterns:
            if p.search(blob_buttons):
                return state_name
    for state_name, patterns in config.STATE_PATTERNS.items():
        for p in patterns:
            if p.search(scroller_text or ""):
                return state_name
    return "UNKNOWN"


def detect_state(frame: Frame) -> str:
    """Thin wrapper: read visible button labels and last 1000 chars of
    #scroller text from the frame, then delegate to _classify_state."""
    try:
        data = frame.evaluate(
            f"""
            () => {{
              const btns = Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
                .filter(b => b.offsetParent !== null)
                .map(b => (b.innerText || '').trim());
              const s = document.querySelector('{config.SEL_SCROLLER}');
              const text = s ? (s.innerText || '').slice(-1000) : '';
              return {{buttons: btns, text: text}};
            }}
            """
        )
    except Exception as e:
        raise IframeLostError(f"detect_state: {e}") from e
    return _classify_state(data["buttons"], data["text"])
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_detect_state.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add booking_bot/chat.py tests/test_detect_state.py
git commit -m "feat: detect_state with tested pure classifier"
```

---

## Task 16: chat.py — dump_visible_state

**Files:**
- Modify: `booking_bot/chat.py`

- [ ] **Step 1: Append `dump_visible_state`**

```python
def dump_visible_state(frame: Frame) -> str:
    """Return a compact diagnostic string for FatalError messages and DEBUG
    logs. Never raises — returns a string even on failure."""
    try:
        data = frame.evaluate(
            f"""
            () => {{
              const btns = Array.from(document.querySelectorAll('{config.SEL_OPTION}'))
                .filter(b => b.offsetParent !== null)
                .map(b => (b.innerText || '').trim()).slice(0, 20);
              const s = document.querySelector('{config.SEL_SCROLLER}');
              const text = s ? (s.innerText || '').slice(-500) : '<no-scroller>';
              const loader = !!document.querySelector('{config.SEL_LOADER}');
              return {{
                buttons: btns, text: text, loader: loader,
                url: document.location ? document.location.href : '<no-url>'
              }};
            }}
            """
        )
        return (
            f"url={data['url']!r} loader_present={data['loader']} "
            f"visible_buttons={data['buttons']!r} "
            f"last_scroller_500={data['text']!r}"
        )
    except Exception as e:
        return f"<dump_visible_state failed: {e}>"
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.chat import dump_visible_state; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/chat.py
git commit -m "feat: dump_visible_state diagnostic helper"
```

---

## Task 17: chat.py — book_one state machine

**Files:**
- Modify: `booking_bot/chat.py`

- [ ] **Step 1: Append `book_one`**

```python
def book_one(frame: Frame, phone: str) -> BookingResult:
    """Drive one booking from READY_FOR_CUSTOMER to terminal state.

    Flow:
      1. Type the customer phone, submit.
      2. wait_until_settled → new message(s).
      3. If the new text contains a SUCCESS_RE match, return Success.
      4. Otherwise try clicking an affirmative option (Yes / Continue / ...).
         - If an affirmative matches, loop back to step 2 with the fresh
           settled snapshot. Accumulate the full bot response chain in
           `accumulated` for the Issue diagnostic field.
         - If no affirmative matches, the bot is in an unexpected state —
           return Issue('unexpected_state', accumulated).
      5. Bail out after MAX_STEPS_PER_BOOKING iterations with
         Issue('too_many_steps', accumulated).

    All recoverable exceptions (GatewayError, ChatStuckError, IframeLostError,
    OptionNotFoundError) propagate to the cli.py retry loop — book_one does
    not catch them. OptionNotFoundError is caught only when it comes from our
    own click_option call inside the affirmative step; that means 'the chat
    is not in an affirmative state', which is an unexpected_state Issue, not
    a recoverable error.
    """
    send_text(frame, phone)
    new = wait_until_settled(frame)
    accumulated = new.text

    for step in range(config.MAX_STEPS_PER_BOOKING):
        m = config.SUCCESS_RE.search(new.text)
        if m:
            log.info(f"book_one success: code={m.group(1)} (step {step})")
            return Success(code=m.group(1), raw=accumulated)

        try:
            label = click_option(frame, config.AFFIRMATIVE_LABELS)
        except OptionNotFoundError:
            log.info(f"book_one unexpected_state at step {step}")
            return Issue(reason="unexpected_state", raw=accumulated)

        log.debug(f"book_one clicked affirmative: {label!r}")
        new = wait_until_settled(frame)
        accumulated += "\n---\n" + new.text

    log.info("book_one too_many_steps")
    return Issue(reason="too_many_steps", raw=accumulated)
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.chat import book_one, Success, Issue; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/chat.py
git commit -m "feat: book_one per-row state machine"
```

---

## Task 18: auth.py — full_auth + navigate_to_book_for_others

**Files:**
- Create: `booking_bot/auth.py`

- [ ] **Step 1: Write `booking_bot/auth.py`**

```python
"""Operator authentication: type operator phone, prompt for OTP, walk the menu
to READY_FOR_CUSTOMER. Called once at startup by cli.main() and again by
browser.recover_session() if the chat is seen in NEEDS_OPERATOR_AUTH after
a reload."""
from __future__ import annotations

import logging
from typing import Callable

from playwright.sync_api import Frame

from booking_bot import chat, config
from booking_bot.exceptions import AuthFailedError, OptionNotFoundError

log = logging.getLogger("auth")


def full_auth(frame: Frame, operator_phone: str, get_otp: Callable[[], str]) -> None:
    """Complete operator auth: phone → OTP → walk AUTH_NAV_SEQUENCE until the
    chat is in READY_FOR_CUSTOMER. Raises AuthFailedError on any menu miss."""
    log.info(f"auth: typing operator phone {operator_phone[:3]}XXXXXXX")
    chat.send_text(frame, operator_phone)
    chat.wait_until_settled(frame)

    otp = get_otp()
    log.info("auth: typing OTP (not logged)")
    chat.send_text(frame, otp)
    chat.wait_until_settled(frame)

    navigate_to_book_for_others(frame)


def navigate_to_book_for_others(frame: Frame) -> None:
    """Walk config.AUTH_NAV_SEQUENCE. Each entry is a priority list of regex
    patterns; we click the first matching option and settle. If no option
    matches one of the groups, we raise AuthFailedError so the caller can
    decide whether to recover."""
    for step_idx, patterns in enumerate(config.AUTH_NAV_SEQUENCE):
        try:
            label = chat.click_option(frame, patterns)
            log.info(f"auth nav step {step_idx + 1}: clicked {label!r}")
        except OptionNotFoundError as e:
            raise AuthFailedError(
                f"auth nav step {step_idx + 1} failed: {e}"
            ) from e
        chat.wait_until_settled(frame)

    state = chat.detect_state(frame)
    if state != "READY_FOR_CUSTOMER":
        raise AuthFailedError(
            f"after AUTH_NAV_SEQUENCE, detect_state={state!r}; "
            f"visible: {chat.dump_visible_state(frame)}"
        )
    log.info("auth: landed on READY_FOR_CUSTOMER")
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.auth import full_auth, navigate_to_book_for_others; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/auth.py
git commit -m "feat: auth full_auth and navigate_to_book_for_others"
```

---

## Task 19: browser.py — recover_session

**Files:**
- Modify: `booking_bot/browser.py`

- [ ] **Step 1: Append `recover_session` to `booking_bot/browser.py`**

Add these imports at the top of the file (next to the existing ones):
```python
from typing import Callable
```

Then append:
```python
def recover_session(
    page: Page,
    operator_phone: str,
    get_otp: Callable[[], str],
) -> Frame:
    """Attempt to recover a wedged/erroring chat session. The server-side
    session typically survives the reload — we only re-run operator auth if
    detect_state sees NEEDS_OPERATOR_AUTH. Navigation-first, re-auth as a
    last resort.

    Raises:
      GatewayError if the reload itself times out.
      FatalError if detect_state returns UNKNOWN (unrecognized page).
      ChatStuckError if we exceed MAX_NAV_HOPS without reaching
        READY_FOR_CUSTOMER.
    """
    # Late imports to avoid module-load cycles.
    from booking_bot import auth, chat
    from booking_bot.exceptions import FatalError

    log.warning("recover_session: reloading page")
    try:
        page.reload(wait_until="domcontentloaded", timeout=60_000)
    except PWTimeoutError as e:
        raise GatewayError(f"reload timed out: {e}") from e
    page.wait_for_timeout(config.PAGE_LOAD_WAIT_S * 1000)

    frame = get_chat_frame(page)
    chat.wait_until_settled(frame)

    for hop in range(config.MAX_NAV_HOPS):
        state = chat.detect_state(frame)
        log.info(f"recover_session hop {hop + 1}: state={state}")
        if state == "READY_FOR_CUSTOMER":
            return frame
        if state == "BOOK_FOR_OTHERS_MENU":
            chat.click_option(frame, config.STATE_PATTERNS["BOOK_FOR_OTHERS_MENU"])
        elif state == "MAIN_MENU":
            chat.click_option(frame, config.STATE_PATTERNS["MAIN_MENU"])
        elif state == "BOOKING_IN_PROGRESS":
            pass  # the wait_until_settled below gives the bot time to finish
        elif state == "NEEDS_OPERATOR_OTP":
            chat.send_text(frame, get_otp())
        elif state == "NEEDS_OPERATOR_AUTH":
            auth.full_auth(frame, operator_phone, get_otp)
        else:  # UNKNOWN or any new state we haven't coded for
            raise FatalError(
                f"unknown chat state during recovery: {state}; visible: "
                f"{chat.dump_visible_state(frame)}"
            )
        chat.wait_until_settled(frame)

    from booking_bot.exceptions import ChatStuckError as _CSE
    raise _CSE(
        f"recovery exceeded MAX_NAV_HOPS={config.MAX_NAV_HOPS}; "
        f"visible: {chat.dump_visible_state(frame)}"
    )
```

- [ ] **Step 2: Sanity-import**

Run: `python -c "from booking_bot.browser import recover_session; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add booking_bot/browser.py
git commit -m "feat: recover_session state-driven navigator"
```

---

## Task 20: cli.py — main orchestration loop

**Files:**
- Modify: `booking_bot/cli.py` (keep `normalize_phone` from Task 6; add everything else)

- [ ] **Step 1: Replace `booking_bot/cli.py` with the full version**

```python
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
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt; shutting down")
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


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Sanity-check — re-run normalize_phone tests (cli.py changed)**

Run: `pytest tests/test_normalize_phone.py -v`
Expected: all tests still PASS.

- [ ] **Step 3: Sanity-import the whole module**

Run: `python -c "from booking_bot.cli import main; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add booking_bot/cli.py
git commit -m "feat: cli main orchestration loop with retry and fatal handling"
```

---

## Task 21: __main__.py entry point verification

**Files:**
- Verify: `booking_bot/__main__.py` (already created in Task 1)

- [ ] **Step 1: Verify the entry point dispatches correctly**

Run: `python -m booking_bot --help`
Expected: argparse usage:
```
usage: python -m booking_bot [-h] [--debug] input_file

positional arguments:
  input_file  path to Input/*.xlsx

options:
  -h, --help  show this help message and exit
  --debug     verbose file logging
```

- [ ] **Step 2: No commit needed unless __main__.py was edited.**

If __main__.py was modified, commit:
```bash
git add booking_bot/__main__.py
git commit -m "chore: verify __main__ entry point"
```

---

## Task 22: Tier-2 smoke test

**Files:**
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Write the smoke test**

```python
"""Tier-2 smoke: load myhpgas.in, drill the iframes, settle the chat, and
assert the initial welcome text. Canary against HP Gas changing the chatbot.

This test hits the real live site. Skipped if the BOOKING_BOT_SMOKE env var
is not set — keeps the default pytest run fully offline."""
import os

import pytest

from booking_bot import browser, chat

pytestmark = pytest.mark.skipif(
    os.environ.get("BOOKING_BOT_SMOKE") != "1",
    reason="set BOOKING_BOT_SMOKE=1 to enable live smoke test",
)


def test_welcome_text_visible():
    pw = brw = ctx = page = None
    try:
        pw, brw, ctx, page = browser.start_browser()
        frame = browser.get_chat_frame(page)
        chat.wait_until_settled(frame)
        snap = chat._scroller_snapshot(frame)
        assert "10-digit" in snap.text or "Mobile number" in snap.text, (
            f"unexpected welcome text: {snap.text[:200]!r}"
        )
    finally:
        if brw is not None:
            brw.close()
        if pw is not None:
            pw.stop()
```

- [ ] **Step 2: Run the default test suite (smoke skipped)**

Run: `pytest -v`
Expected: all Tier-1 tests PASS; the smoke test shows as SKIPPED.

- [ ] **Step 3: Run the smoke test explicitly**

On Windows PowerShell: `$env:BOOKING_BOT_SMOKE="1"; pytest tests/test_smoke.py -v -s`
On bash: `BOOKING_BOT_SMOKE=1 pytest tests/test_smoke.py -v -s`
Expected: a visible Chromium window opens, navigates to myhpgas.in, drills the iframes, and the test PASSes. The test closes the browser automatically.

If the text has changed, update the assertion or the `STATE_PATTERNS["NEEDS_OPERATOR_AUTH"]` regex accordingly.

- [ ] **Step 4: Commit**

```bash
git add tests/test_smoke.py
git commit -m "test: tier 2 smoke test against live myhpgas.in welcome screen"
```

---

## Task 23: README with setup + run instructions

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# HP Gas Booking Bot

Automates booking HP Gas LPG refill cylinders via the myhpgas.in chatbot.
Reads customer phone numbers from an Excel file, writes 6-digit delivery
confirmation codes back to the same file.

See `docs/superpowers/specs/2026-04-13-hp-gas-booking-bot-design.md` for the
full design.

## Setup

Windows, Python 3.12:

```
pip install -r requirements.txt
python -m playwright install chromium
```

Edit `booking_bot/config.py` and set `OPERATOR_PHONE` to your own 10-digit
number (the one registered with HP Gas for OTP auth).

## Run

1. Drop `file1.xlsx` in `Input/`. Column A = consumer number (untouched),
   Column B = customer phone number. No header row.
2. Start the bot:
   ```
   python -m booking_bot Input/file1.xlsx
   ```
3. When prompted, enter the OTP you receive on `OPERATOR_PHONE`.
4. The bot will iterate through pending rows (where column C is empty),
   booking each one. Column C is filled with the 6-digit code on success or
   `ISSUE` on failure.
5. Failures are written to `Issues/file1.xlsx` with the full chatbot text so
   you can inspect them.
6. Real-time logs live in `logs/booking_bot_<timestamp>.log`. Tail them in a
   second terminal:
   ```
   Get-Content -Path .\logs\booking_bot_*.log -Wait   # PowerShell
   ```

## Resume

If the bot crashes or you Ctrl-C it, just run it again with the same input
file. Rows that already have a value in column C (code or `ISSUE`) are
skipped; only pending rows are attempted.

## Tests

```
pytest                                    # offline Tier-1 unit tests
$env:BOOKING_BOT_SMOKE="1"; pytest tests/test_smoke.py   # live Tier-2 smoke
```

## Directory layout

| Dir | Purpose |
|---|---|
| `Input/`  | Drop input .xlsx files here. Never modified by the bot. |
| `Output/` | Bot writes the mirror + column C here. |
| `Issues/` | Bot appends one row per failure with diagnostic text. |
| `logs/`   | One log file per run. |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, run, and resume instructions"
```

---

## Post-Implementation: Tier-3 Live Walkthrough

This is **not** a task in this plan — it's the handoff. Once Tasks 1-23 are complete and the Tier-2 smoke test passes, invite the operator for a joint live walkthrough:

1. Operator confirms `OPERATOR_PHONE` is set in `config.py`.
2. Place a single trusted test number in `Input/test_one.xlsx` (1 row).
3. Run `python -m booking_bot Input/test_one.xlsx`.
4. Verify:
   - Operator phone typed, OTP prompt appears in terminal.
   - Auth walks to the customer-phone prompt.
   - Customer phone typed, success message received, 6-digit code written to column C.
   - Log file is readable, Issues file does not exist.
5. **Update `config.STATE_PATTERNS` and `config.AUTH_NAV_SEQUENCE`** if any wording differs from recon assumptions. These regexes are designed to be tuned at this stage.
6. Expand to a 3-5 row test xlsx and verify the post-row "Book for others" navigation loop.
7. Only after those pass, run the real 50-row `file1.xlsx`.

The open questions listed in spec §14 will be resolved here. Commit any pattern updates under `feat: tune patterns from tier-3 walkthrough`.

---

## Self-Review Notes

Cross-checked against the spec (2026-04-13-hp-gas-booking-bot-design.md):

- **§4 architecture / file layout** → Task 1 creates the directory structure, Tasks 2-20 populate each module described in §4.2.
- **§5 end-to-end flow** → Task 20 (`cli.main`) implements every bullet; resume detection lives in Task 7.
- **§6.1 wait_until_settled** → Task 14 with the first-activity gate and auto-captured before.
- **§6.2 book_one** → Task 17 with accumulated raw text and the `AFFIRMATIVE_LABELS` loop.
- **§6.3 no-false-positive** → Task 5 TDDs `SUCCESS_RE` including the 8-digit edge case. Column C only receives a code from a `SUCCESS_RE.search` match inside `book_one`.
- **§7.1 file lifecycle** → Task 7 (first-run copy, resume) and Task 10 (.xls conversion, summary).
- **§7.2 normalize_phone** → Task 6 TDD. Lives in `cli.py` as specified.
- **§7.3 ExcelStore API** → Tasks 7-10 cover all four public methods.
- **§8.1 exception taxonomy** → Task 3.
- **§8.2 recover_session** → Task 19 with navigation-first state walk.
- **§8.2a FatalError mid-row** → Task 20 `main()`'s `except FatalError` block writes the in-flight row as an ISSUE before exiting.
- **§8.3 per-row retry policy** → Task 20 with `RECOVERABLE` tuple matching spec exactly.
- **§9 detect_state patterns** → Task 15 TDDs the pure classifier with button-priority-then-text matching.
- **§10 configuration** → Task 2, with regex strings compiled at module load.
- **§11 logging** → Task 4 (colored console + flushing file handler, format matches spec).
- **§12 dependencies** → Task 1 requirements.txt.
- **§13 testing strategy** → Tier-1 (Tasks 5, 6, 7-10, 15), Tier-2 (Task 22), Tier-3 (post-implementation handoff section).
- **§14 open questions** → deferred to Tier-3 per spec, not blocking any task.
- **§15 out of scope** → nothing in the plan implements multi-instance, SQLite, CAPTCHA, etc. Confirmed.

**Placeholder scan:** no "TBD", "TODO", or "similar to task N" entries. All code steps show the actual code.

**Type consistency:** `ExcelStore` methods use the same signatures across tasks. `Snapshot`, `Success`, `Issue` dataclasses are defined once in Task 13 and referenced identically in Tasks 14 and 17. `_classify_state` signature in Task 15 matches both its test and its `detect_state` caller. `normalize_phone` lives in `cli.py` in both Task 6 and Task 20 (Task 20 re-includes it in the full file rewrite — verified identical).

**One point to watch during implementation:** Task 5 may reveal that `SUCCESS_RE` needs the `(?!\d)` trailing anchor to reject 8-digit numbers. The task explicitly calls this out and includes the fix in its instructions.
