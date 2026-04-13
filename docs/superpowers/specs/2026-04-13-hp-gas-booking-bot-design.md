# HP Gas Booking Bot — Design

**Date:** 2026-04-13
**Author:** Aryan + Claude (brainstormed)
**Status:** Approved by user, ready for implementation planning

---

## 1. Goal

Automate booking of HP Gas LPG cylinder refills for many customer phone numbers
read from an Excel file, by driving the chatbot at `myhpgas.in` programmatically.
The bot must be reliable, resumable across crashes, must never produce a false
positive (i.e., never write a delivery confirmation code unless one was actually
received), and must keep the human in the loop only for OTP entry.

The first batch will be a single file with ~50 customer numbers. The system
should evolve gracefully toward processing hundreds of thousands of numbers and
running multiple instances in parallel.

## 2. Workflow context

- The operator (Aryan) has a single phone number that authenticates a session
  with HP Gas's chatbot via OTP. After auth, the chatbot offers a "Book for
  others" path that lets the operator submit any customer phone number and
  receive a 6-digit delivery confirmation code without any further OTP per
  customer.
- HP Gas's server is overloaded and frequently returns 502 Bad Gateway or causes
  the chatbot UI to hang on a loader. Reloading the page recovers the session
  every time. Per the operator, the *server-side session* survives the reload —
  only the client-side chat UI dies. This means recovery is usually navigation,
  not full re-auth.
- The success message looks like:
  > Your HP Gas Refill has been successfully booked with reference number
  > 1260669600118310 and your delivery confirmation code is **764260**

## 3. Reconnaissance findings

Performed via headless Playwright. Files are under `recon/`.

### 3.1 Page architecture
The chatbot is served from a separate domain and is double-iframed inside the
parent page:

```
myhpgas.in (#cbotform — slide-in panel, offscreen at right: -526px by default)
  └── iframe#webform → https://hpchatbot.hpcl.co.in/hpclpwa/myhpgas.html
        └── iframe[name="iframe"] → https://hpchatbot.hpcl.co.in/pwa/view?data=<base64>
```

The base64 `data` parameter encodes a tenant/campaign descriptor:
`{"eId":100,"gli":true,"campaignId":"645202e3a17e16acde93a28f","li":"<token>"}`.
The platform is Twixor (HPCL-branded).

### 3.2 Key insight: launcher click is unnecessary
The `#cbotform` panel is `display:block` and the iframe inside has its `src` set
on page load. Clicking the bottom-right `button.support` only animates the panel
into view — it does NOT initialize the chat session. The chatbot inside the
iframe is alive and ready as soon as the page loads. The bot can drive the
iframe directly without ever clicking the launcher.

### 3.3 The chat DOM (inner iframe)
- **Universal text input:** `<textarea class="replybox" placeholder="Type your
  message here">`. Same input is used for phone numbers, OTPs, and any free
  text.
- **Submit button:** `<button class="reply-submit pull-right">`.
- **Option buttons:** `<button class="dynamic-message-button type-N">` where
  `N` is a Twixor message-type id. The text content is the option label
  (e.g., "Book for others", "Yes", "Cancel"). Each button has a unique `id`.
- **Loader / processing indicator:** `.load-container`.
- **Process button container:** `.process-button`.
- **Messages container:** `#scroller` (the original `ul.list-group.chat`
  element appears in the DOM but is not where messages render in the current
  version — `#scroller` holds the live message stream).
- **Initial bot text:** "Offering you the flexibility and convenience of
  booking your refill cylinder ... Please enter your 10-digit Mobile number".

### 3.4 What we did NOT verify in recon (deferred to live walkthrough)
- Exact wording of the prompt that asks for the *customer* phone (post-auth).
- Exact wording of any "already booked" / "limit exceeded" / "invalid number"
  messages.
- Whether HP Gas serves a CAPTCHA at any point. (None observed.)
- The full list of menu options between "Booking Services" and "Book for
  others" — recon could not get past auth without a real OTP.

These will be discovered and refined during the Tier-3 live walkthrough.

## 4. Architecture overview

A small Python package, modular, single-process, single-browser. Driven by
Playwright (sync API, visible Chromium). No async, no DB, no web framework.
Excel is the durable I/O surface.

### 4.1 Layout

```
D:\workspace\booking_bot\
├── Input/                       # user drops file1.xlsx here (untouched by bot)
├── Output/                      # bot writes mirror + col C codes here
├── Issues/                      # bot writes failure rows + raw chatbot text here
├── logs/                        # one file per run, line-buffered, append-only
├── recon/                       # one-off reconnaissance scripts (existing)
├── docs/superpowers/specs/      # this file
├── booking_bot/                 # the package
│   ├── __init__.py
│   ├── __main__.py              # `python -m booking_bot Input/file1.xlsx`
│   ├── cli.py                   # arg parsing, OTP terminal prompts, top-level loop
│   ├── config.py                # constants, paths, knobs, regex patterns, selectors
│   ├── browser.py               # Playwright lifecycle, iframe drilling, recover_session
│   ├── auth.py                  # initial operator login → reach Book-for-others state
│   ├── chat.py                  # send_text, click_option, wait_until_settled,
│   │                            #   detect_state, book_one
│   ├── excel.py                 # ExcelStore: read input, write output + issues, resume
│   ├── logging_setup.py         # console + line-buffered file logger
│   └── exceptions.py            # GatewayError, ChatStuckError, AuthFailedError, ...
├── requirements.txt
└── README.md
```

Each file aims to stay under ~150 lines. The user has explicitly requested that
no single file become large.

### 4.2 Component responsibilities (one paragraph each)

- **`__main__.py`** — minimal; just `from booking_bot.cli import main; main()`.
- **`cli.py`** — parses the input file path argument, sets up logging, builds
  the `ExcelStore`, launches the browser, runs `auth.full_auth()` once, then
  iterates `excel.pending_rows()`. For each yielded `(row_idx, raw_cell)`, it
  calls `normalize_phone(raw_cell)`; on validation failure it writes an Issue
  immediately (no chatbot exchange). On valid phones it runs the per-row
  attempt loop with the `RECOVERABLE` exception set
  (`ChatStuckError | GatewayError | IframeLostError | OptionNotFoundError`),
  routing failures through `browser.recover_session()`. Catches
  `KeyboardInterrupt` for graceful shutdown. Catches `FatalError` at the top
  level to mark the in-flight row and exit cleanly (see §8.2a). Handles the
  OTP prompt callback (blocking `input()`).
- **`config.py`** — pure data: paths, timeouts, pacing, selectors, regex
  patterns, the operator phone number constant. No imports of other package
  modules.
- **`browser.py`** — owns the `Page` and the iframe handles. Exposes
  `start_browser()`, `get_chat_frame(page)`, and the recovery primitive
  `recover_session(page, operator_phone, get_otp)`. **`get_chat_frame` retries
  for up to 30 seconds** waiting for both `iframe#webform` and the inner
  `iframe[name='iframe']` to attach and reach `domcontentloaded`, raising
  `IframeLostError` if not. Registers `page.on("response", ...)` and
  `page.on("framenavigated", ...)` listeners that set a thread-local
  `gateway_error_seen` flag whenever the chatbot domain returns 502/503/504
  or navigates to a URL matching `/error|gateway|nginx/i`. The flag is read
  AND reset by `wait_until_settled` on each call (see §6.1).
  **Browser context lifetime:** one Chromium launch per `python -m booking_bot`
  run. The browser is created in `cli.main()` and closed on exit. Sessions
  survive `page.reload()` within a run because cookies/storage persist in the
  context. A new `python -m booking_bot` invocation starts a fresh context →
  fresh OTP at startup.
- **`auth.py`** — `full_auth(frame, phone, get_otp)`: types operator phone,
  waits, prompts for OTP, types OTP, then walks the option menu by clicking
  through `AUTH_NAV_SEQUENCE` until the chat is in `READY_FOR_CUSTOMER` state.
  Also exposes `navigate_to_book_for_others(frame)` for use by `recover_session`
  when the session is still alive but the chat needs forward navigation.
- **`chat.py`** — the heart of the bot. Functions:
  - `send_text(frame, text)` — **focuses** `textarea.replybox`, **selects all
    and clears existing content**, types `text`, then clicks
    `button.reply-submit`. The clear step is essential: a leftover from a
    prior interaction would otherwise be concatenated.
  - `click_option(frame, label_patterns)` — finds a *visible*
    `button.dynamic-message-button` whose text matches one of `label_patterns`
    in priority order. Raises `OptionNotFoundError` if none match.
  - `wait_until_settled(frame, timeout=STUCK_THRESHOLD_S)` — see §6.1.
  - `detect_state(frame) -> ChatState` — see §9.
  - `book_one(frame, phone) -> BookingResult` — the per-row state machine
    (see §6.2).
  - `dump_visible_state(frame) -> str` — diagnostic dump used in FatalError
    messages: visible button labels, last 500 chars of `#scroller`, loader
    visibility, current iframe URL.
- **`excel.py`** — the only Excel surface. The `ExcelStore` class hides
  openpyxl/xlrd. Public API is exactly four methods (see §7).
- **`logging_setup.py`** — configures the root logger with a colored console
  handler and a line-buffered file handler that flushes after every record.
- **`exceptions.py`** — `GatewayError`, `ChatStuckError`, `AuthFailedError`,
  `IframeLostError`, `OptionNotFoundError`, `FatalError`.

## 5. End-to-end flow

1. Operator drops `file1.xlsx` in `D:\workspace\booking_bot\Input\`.
2. Operator runs `python -m booking_bot Input/file1.xlsx`.
3. Bot creates `logs/booking_bot_<timestamp>.log`. All output goes to this
   file AND the console.
4. Bot opens `Output/file1.xlsx`. If it doesn't exist, it copies the input
   file there (preserving columns A and B). If it does exist, it opens it for
   resume — pending rows are those where column C is empty.
5. Bot launches **visible** Chromium, navigates to `https://myhpgas.in`,
   waits ~4s for JS, drills into the iframe chain, gets the inner chat frame.
6. Bot calls `auth.full_auth(frame, OPERATOR_PHONE, prompt_otp)`:
   - types operator phone, submits
   - settles, prompts operator in terminal: `Enter OTP for 9XXXXXXXXX: `
   - types OTP, submits
   - clicks through `AUTH_NAV_SEQUENCE` to land on "Book for others" → customer
     phone input prompt (`READY_FOR_CUSTOMER` state).
7. Bot iterates `excel.pending_rows()`:
   - For each `(row_idx, raw_cell)`, call `phone, err = normalize_phone(raw_cell)`.
   - If `err`: write Issue (`invalid_phone_format`, raw cell value), continue
     to next row. No chatbot exchange.
   - Otherwise run the per-row attempt loop (max 2 attempts):
     - `result = chat.book_one(frame, phone)`
     - On `Success(code)`: `excel.write_success(row_idx, code)`, log.
     - On `Issue(reason, raw)`: `excel.write_issue(row_idx, phone, reason, raw)`, log.
     - On any `RECOVERABLE` exception: if attempt 1, `frame =
       browser.recover_session(...)`, sleep `RETRY_PAUSE_S`, retry. If
       attempt 2, write Issue with reason
       `recovered_but_failed:<ExceptionName>` and move on.
   - After saving the row's result, attempt post-row navigation
     (`POST_ROW_NAV_LABELS`); a failure there triggers `recover_session` but
     the saved result stays on disk.
   - Sleep `PACING_S` (4.5s) between rows.
8. On completion (or Ctrl-C): close browser cleanly, write summary line
   (total / success / issue / pending), exit.

## 6. Chat interaction & no-false-positive guarantee

### 6.1 The settled-state primitive

`wait_until_settled(frame, timeout=STUCK_THRESHOLD_S)` is the synchronization
linchpin. **It auto-captures a "before" snapshot of `#scroller` immediately on
entry** — never accepts an external before. This is important: callers always
invoke it AFTER `send_text` / `click_option`, so the auto-captured before
already includes any user-bubble the chat may have rendered for the typed
input. The diff therefore contains only what the bot adds in response.

Algorithm:

1. **Reset the gateway-error flag** that `browser.py`'s network listener may
   have set during the previous interaction (so we only react to gateway
   errors observed during *this* settled wait).
2. Capture `before = (text, child_count, hash)` of `#scroller`.
3. Poll every 500ms. On each poll:
   a. If the gateway flag is set → raise `GatewayError`.
   b. If the iframe handle is detached → raise `IframeLostError`.
   c. Compute current `(text, child_count, hash)`.
4. **First-activity gate** (fixes a race): before we can declare settled, we
   require **at least one observed activity sign** since entry — either the
   loader has been seen visible at any point, OR the scroller hash has
   changed at least once. Without this gate, `wait_until_settled` could
   return instantly if the bot hasn't yet started processing the input,
   producing empty `new` content and a false ISSUE.
5. Once activity has been observed, settled = `loader is hidden` AND
   `scroller hash unchanged for SETTLE_QUIET_MS` (1500ms).
6. If `timeout` elapses without satisfying the activity gate AND settled
   condition → raise `ChatStuckError`.
7. On settle, return `Snapshot(new_text, new_child_count, hash)` where
   `new_text` is exactly the content added after `before` (string-diff or,
   simpler and good enough, `current_text[len(before.text):]` since the
   chat is append-only).

`scroller_snapshot(frame)` is a private helper inside `chat.py`. It is not
part of the public chat.py API — `wait_until_settled` is the only thing that
calls it.

### 6.2 The per-row state machine

```python
def book_one(frame, phone):
    send_text(frame, phone)                     # types into textarea, clears first
    new = wait_until_settled(frame)             # auto-captures before; may raise
    accumulated = new.text
    for step in range(MAX_STEPS_PER_BOOKING):   # 5
        m = SUCCESS_RE.search(new.text)
        if m:
            return Success(code=m.group(1), raw=accumulated)
        try:
            label = click_option(frame, AFFIRMATIVE_LABELS)
            log.debug(f"clicked affirmative option: {label}")
            new = wait_until_settled(frame)
            accumulated += "\n---\n" + new.text
            continue
        except OptionNotFoundError:
            return Issue(reason="unexpected_state", raw=accumulated)
    return Issue(reason="too_many_steps", raw=accumulated)
```

The `accumulated` text is the running concatenation of every `new.text`
returned by `wait_until_settled` during this row. We pass `accumulated` (not
just the latest `new.text`) into the Issue's `raw` field so the Issues file
captures the full bot response sequence — useful for pattern analysis.

After a `Success` or `Issue`, the orchestrator clicks "Book for others" and
calls `wait_until_settled` to land back at `READY_FOR_CUSTOMER` for the next
row. If that step fails, the row's result is already saved; the failure is
treated as a recoverable error and recovery proceeds normally.

### 6.3 Strict no-false-positive rules

- Column C only receives a 6-digit code if `SUCCESS_RE` matches inside
  `wait_until_settled`'s **new** snapshot (never against prior chat history).
- `SUCCESS_RE = re.compile(r"delivery\s+confirmation\s+code\s+is\s+(\d{6})", re.I)`.
  The literal phrase "delivery confirmation code is" is required. A stray
  6-digit number will not match.
- Any non-success outcome → column C gets the literal string `ISSUE`, AND a
  row is appended to `Issues/file1.xlsx` with the full new chatbot text in
  its column C, plus a label (e.g. `unexpected_state`, `too_many_steps`,
  `recovered_but_failed:GatewayError`).
- A `ChatStuckError` mid-row never marks the row as ISSUE on the first attempt
  — it triggers `recover_session` and one retry. Only on a second failure does
  the row get marked.

## 7. Excel I/O

### 7.1 File lifecycle

```
Input/file1.xlsx        (drop in; never modified)
Output/file1.xlsx       (created on first run as a copy; col C filled per row)
Issues/file1.xlsx       (created lazily on first issue; one row per failure)
```

- `.xls` legacy inputs are read via `xlrd==1.2.0` and converted to `.xlsx` on
  first open. Output is always `.xlsx`.
- **Resume detection:** if `Output/file1.xlsx` exists at startup, it is opened
  in place; rows whose column C is empty/whitespace are pending; rows with any
  non-empty column C value are skipped (whether the value is a 6-digit code or
  `ISSUE`).
- **Atomic per-row save:** the workbook is written to `Output/file1.xlsx.tmp`
  then `os.replace()`d to `Output/file1.xlsx`. `os.replace` is atomic on Windows
  for same-filesystem renames. There is no half-written window.
- **Issues file structure:** column A = consumer number (mirrored from input),
  column B = phone number, column C = raw chatbot text + label, column D =
  `row N in Output/file1.xlsx` cross-reference.

### 7.2 Phone number coercion (validation lives in cli.py, not excel.py)

Excel often stores phone numbers as numeric cells, which `openpyxl` returns as
`float` (`9876543210.0`) or `int` (`9876543210`). `ExcelStore.pending_rows()`
**does not validate or transform** — it yields raw cell values. The only
thing it skips is rows where col B is `None` (truly empty cell). Validation
is the caller's job. This keeps `excel.py` side-effect-free and unit-testable.

`cli.py` validates each yielded row with a small helper:

```python
def normalize_phone(raw) -> tuple[str, str | None]:
    """Returns (cleaned_phone, error_reason). error_reason is None on success."""
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
    # Accept 10 digits, or +91 followed by 10 digits.
    m = re.fullmatch(r"(?:\+?91)?(\d{10})", s)
    if not m:
        return ("", "invalid_phone_format")
    return (m.group(1), None)
```

The cli.py per-row loop becomes:

```python
for row_idx, raw_phone in store.pending_rows():
    if should_stop: break
    phone, err = normalize_phone(raw_phone)
    if err:
        store.write_issue(row_idx, str(raw_phone), err, raw=f"input cell: {raw_phone!r}")
        continue                                # no chatbot exchange wasted
    # ... attempt loop, write_success / write_issue, post-row nav, pacing
```

So a malformed cell is logged as an ISSUE (in both Output and Issues) without
ever sending it to the chatbot.

### 7.3 ExcelStore API

```python
class ExcelStore:
    def __init__(self, input_path: Path):
        """Convert .xls→.xlsx if needed; create or resume Output; defer Issues."""

    def pending_rows(self) -> Iterator[tuple[int, object]]:
        """Yield (row_idx, raw_col_B_value) for rows where col C is empty AND
        col B is not None. Iterates the Output file (the source of truth for
        resume) starting at min_row=1 (no header). Performs no validation or
        coercion — callers do that."""

    def write_success(self, row_idx: int, code: str) -> None:
        """Write code to col C; atomic save."""

    def write_issue(self, row_idx: int, phone: str, reason: str, raw: str) -> None:
        """Write 'ISSUE' to Output col C + atomic save; append a row to Issues
        with raw text and reason; atomic save Issues."""

    def summary(self) -> dict:
        """{total, pending, success, issue}; for the end-of-run log."""
```

Nothing else in the codebase imports openpyxl or xlrd.

## 8. Recovery

### 8.1 Error taxonomy

```python
class GatewayError(Exception):       # 502/503/504 or nginx error page
class ChatStuckError(Exception):     # wait_until_settled timed out
class IframeLostError(Exception):    # frame detached
class AuthFailedError(Exception):    # auth navigation lost (option not found)
class OptionNotFoundError(Exception):# click_option couldn't match any pattern
class FatalError(Exception):         # unrecoverable; bot exits cleanly
```

`GatewayError`, `ChatStuckError`, `IframeLostError`, and
`OptionNotFoundError` are recoverable via `recover_session` — the per-row
loop's `RECOVERABLE` tuple is exactly these four. `AuthFailedError` is
recoverable too but escalates to `FatalError` if it happens twice
consecutively. `FatalError` is never caught by `RECOVERABLE`; it propagates
to the top-level handler in `cli.py` (see §8.2a).

### 8.2 Recovery primitive (`browser.recover_session`)

Important: the user has confirmed the **server-side session survives a
reload** — only the client UI dies. Recovery is therefore navigation-first,
re-auth only as a last resort.

```python
def recover_session(page, operator_phone, get_otp):
    log.warning("recovering session (reload)")
    try:
        page.reload(wait_until="domcontentloaded", timeout=60_000)
    except PWTimeoutError as e:
        raise GatewayError(f"reload timed out: {e}") from e
    page.wait_for_timeout(PAGE_LOAD_WAIT_S * 1000)
    frame = get_chat_frame(page)               # retries internally; see §4.2
    wait_until_settled(frame)

    for hop in range(MAX_NAV_HOPS):            # 6
        state = detect_state(frame)
        log.info(f"recovery state: {state}")
        if state == READY_FOR_CUSTOMER:
            return frame
        if state == BOOK_FOR_OTHERS_MENU:
            click_option(frame, [r"book\s+for\s+others"])
        elif state == MAIN_MENU:
            click_option(frame, [r"booking\s+services"])
        elif state == BOOKING_IN_PROGRESS:
            pass                                # wait_until_settled below will resettle
        elif state == NEEDS_OPERATOR_OTP:
            send_text(frame, get_otp())         # prompt user
        elif state == NEEDS_OPERATOR_AUTH:
            auth.full_auth(frame, operator_phone, get_otp)
        else:
            raise FatalError(
                f"unknown chat state during recovery; visible state: "
                f"{chat.dump_visible_state(frame)}"
            )
        wait_until_settled(frame)
    raise ChatStuckError("recovery exceeded MAX_NAV_HOPS")
```

`chat.dump_visible_state(frame)` is a helper that returns a JSON-ish string
with: list of visible button labels, last 500 chars of `#scroller` text,
loader visibility, and current iframe URL. Used only for diagnostics in
FatalError messages and DEBUG logs.

The OTP prompt is therefore asked **once at startup** and only re-asked if
the chat shows `NEEDS_OPERATOR_OTP` or `NEEDS_OPERATOR_AUTH` after a reload —
which, per the operator, should be rare.

### 8.2a FatalError handling (mid-row)

If `recover_session` (or anything else) raises `FatalError` mid-row, the
top-level handler in `cli.py` does the following before exiting:

```python
try:
    main_loop()
except FatalError as e:
    log.error(f"fatal: {e}")
    if current_row_idx is not None:
        store.write_issue(
            current_row_idx,
            str(current_phone or ""),
            reason=f"fatal_error:{e}",
            raw=chat.dump_visible_state(frame) if frame else "",
        )
    browser.close()
    write_summary()
    sys.exit(1)
```

This guarantees that a row in progress at the moment of fatal failure is
recorded as ISSUE — the operator is never left wondering whether row N was
attempted.

### 8.3 Per-row retry policy (in `cli.py`)

```python
RECOVERABLE = (ChatStuckError, GatewayError, IframeLostError, OptionNotFoundError)

for row_idx, phone in store.pending_rows():
    if should_stop:
        break

    # Attempt the booking, with one recovered retry.
    for attempt in (1, 2):
        try:
            result = chat.book_one(frame, phone)
            break
        except RECOVERABLE as e:
            log.warning(f"row {row_idx} ({phone}) attempt {attempt} failed: {e}")
            if attempt == 2:
                result = Issue(reason=f"recovered_but_failed:{type(e).__name__}", raw="")
                break
            frame = browser.recover_session(page, OPERATOR_PHONE, prompt_otp)
            time.sleep(RETRY_PAUSE_S)

    # Save the result before doing anything else.
    if isinstance(result, Success):
        store.write_success(row_idx, result.code)
    else:
        store.write_issue(row_idx, phone, result.reason, result.raw)

    # Set up the next row. If this fails, the current row's result is already
    # safely on disk, so we just recover and continue.
    try:
        chat.click_option(frame, POST_ROW_NAV_LABELS)
        chat.wait_until_settled(frame)
    except RECOVERABLE as e:
        log.warning(f"post-row navigation failed after row {row_idx}: {e}")
        frame = browser.recover_session(page, OPERATOR_PHONE, prompt_otp)

    time.sleep(PACING_S)
```

`POST_ROW_NAV_LABELS` is a list of regex labels in priority order, defined in
`config.py` (see §10). It defaults to `[r"book\s+for\s+others", r"book\s+another", r"new\s+booking", r"for\s+others"]` so we don't fall over if the chatbot's wording varies after success.

- **Maximum 2 attempts per row.** No infinite loops.
- **Output is saved before any post-row navigation.** A failure setting up
  the next row never corrupts the current row's result.
- **Post-row navigation failures are recovered separately.** The current row
  is already safe; we just call `recover_session` and continue with the next
  row.
- **`OptionNotFoundError` is recoverable** (added to `RECOVERABLE`) — if the
  expected button isn't there, that's exactly the kind of state where a
  reload + state-detect is the right move.
- **Ctrl-C** sets `should_stop`, the loop exits gracefully through the same
  shutdown path as completion.

## 9. Detect-state patterns (live-tunable)

These regexes live in `config.STATE_PATTERNS` and will be refined during the
Tier-3 live walkthrough:

```python
STATE_PATTERNS = {
    "BOOK_FOR_OTHERS_MENU": [r"book\s+for\s+others"],
    "MAIN_MENU":            [r"booking\s+services"],
    "READY_FOR_CUSTOMER":   [r"customer.*mobile", r"mobile\s+number\s+of\s+the\s+customer"],
    "NEEDS_OPERATOR_OTP":   [r"otp.*sent", r"enter\s+otp"],
    "NEEDS_OPERATOR_AUTH":  [r"please\s+enter\s+your\s+10[- ]digit\s+mobile"],
}
```

Detection priority: visible `dynamic-message-button` text first, then last
bot-message text in `#scroller`. Returns `UNKNOWN` if nothing matches; the
caller dumps the visible state to the log and raises `FatalError` (the only
fatal recovery path).

## 10. Configuration (`config.py`)

```python
from pathlib import Path

ROOT       = Path(__file__).resolve().parent.parent
INPUT_DIR  = ROOT / "Input"
OUTPUT_DIR = ROOT / "Output"
ISSUES_DIR = ROOT / "Issues"
LOGS_DIR   = ROOT / "logs"

URL            = "https://myhpgas.in"
OPERATOR_PHONE = "9XXXXXXXXX"   # operator edits this once

PAGE_LOAD_WAIT_S      = 4
SETTLE_QUIET_MS       = 1500
STUCK_THRESHOLD_S     = 60
PACING_S              = 4.5
RETRY_PAUSE_S         = 2
MAX_NAV_HOPS          = 6
MAX_STEPS_PER_BOOKING = 5
MAX_ATTEMPTS_PER_ROW  = 2

OUTER_IFRAME_SEL = "iframe#webform"
INNER_IFRAME_SEL = "iframe[name='iframe']"
SEL_TEXTAREA     = "textarea.replybox"
SEL_SUBMIT       = "button.reply-submit"
SEL_OPTION       = "button.dynamic-message-button"
SEL_LOADER       = ".load-container"
SEL_SCROLLER     = "#scroller"

SUCCESS_RE = r"delivery\s+confirmation\s+code\s+is\s+(\d{6})"

AFFIRMATIVE_LABELS = [
    r"^yes", r"continue", r"confirm", r"proceed",
    r"go\s*on", r"book\s+now", r"^ok$",
]
AUTH_NAV_SEQUENCE = [
    [r"booking\s+services", r"refill"],
    [r"book\s+for\s+others", r"for\s+others"],
]
POST_ROW_NAV_LABELS = [
    r"book\s+for\s+others",
    r"book\s+another",
    r"new\s+booking",
    r"for\s+others",
]
STATE_PATTERNS = { ... }   # see §9
```

Operator phone is a constant (the operator uses the same number every time).
OTP is never stored — typed at runtime when prompted.

## 11. Logging

- Two handlers on the root logger:
  1. **Console** (colored via `colorlog`) at INFO+.
  2. **File** at INFO+ (DEBUG with `--debug`), `logs/booking_bot_<YYYY-MM-DD_HH-MM-SS>.log`,
     line-buffered, with explicit `flush()` after each record.
- Format: `YYYY-MM-DD HH:MM:SS.mmm  LEVEL  module  message`.
- The operator watches the file in a second window with
  `Get-Content -Path .\logs\booking_bot_<latest>.log -Wait`.
- Logged events: every row attempt, every state transition, every recovery,
  every save, every option-button match. Issues are logged at WARN/ERROR.
- **Not logged:** OTP value (security). Phone numbers are logged in full
  (single-user environment).
- One log file per run; manual cleanup; no auto-rotation.

## 12. Dependencies

```
playwright==1.47.0
openpyxl==3.1.5
xlrd==1.2.0
colorlog==6.8.2
```

Setup:
```
pip install -r requirements.txt
python -m playwright install chromium
```

## 13. Testing strategy

**Tier 1 — Offline unit tests** (no network, no browser):
- `excel.py`: fake input xlsx → confirm Output/Issues files created correctly,
  resume skips filled rows, atomic save survives simulated mid-write crash,
  `.xls → .xlsx` conversion works.
- `SUCCESS_RE`: battery of strings — real success messages, near-misses
  ("delivery confirmation code: 12345" with wrong digit count), text containing
  stray 6-digit numbers without the trigger phrase. Confirm zero false
  positives.
- `detect_state`: canned `#scroller` HTML fixtures for each `ChatState`.

**Tier 2 — Headless smoke** (network, no auth):
- One test that loads `myhpgas.in`, drills the iframes, settles, and asserts
  the welcome text contains `"10-digit Mobile number"`. Canary against HP Gas
  changing the chatbot.

**Tier 3 — Live walkthrough** (one real booking, with the operator):
- Operator + assistant sit together. `OPERATOR_PHONE` is set in `config.py`.
  A single test customer phone the operator trusts is placed in a 1-row test
  xlsx in `Input/`. Bot runs end-to-end, books one cylinder, writes the code
  to column C, exits.
- Verify: code matches the SMS; Output file is correct; log file is readable;
  Issues file does not exist (no false positives on a successful run).
- Then expand to 3-5 numbers and verify the inter-row "Book for others" loop.
- Only after Tier 3 passes do we run the real 50-row file.

## 14. Open questions for the live walkthrough

These are unknowns that recon couldn't answer; they will be resolved during
Tier 3 and the relevant patterns/regexes in `config.py` will be updated:

1. Exact wording of the prompt that asks for the *customer* phone (drives
   `READY_FOR_CUSTOMER` pattern).
2. Exact menu sequence and button labels between "Booking Services" and
   "Book for others" (drives `AUTH_NAV_SEQUENCE`).
3. Wording of the "already booked" / "limit exceeded" / "invalid number"
   messages (informs nothing in the bot — they all become `ISSUE` — but useful
   to know for log clarity).
4. Whether the chatbot ever requires a confirmation click ("Yes go on") between
   typing the customer phone and receiving the success message (drives whether
   `book_one`'s loop iterates 1 or 2 times for a happy-path booking).
5. What a 502 / stuck state actually looks like in the DOM (drives the
   `GatewayError` detection signature).

## 15. Out of scope for v1

- Multi-instance / parallel runs (deferred to v2; `excel.py` is designed so
  that each instance owning its own file is the natural way to scale).
- SQLite history / pattern analytics (Issues file is enough for v1).
- CAPTCHA solving (we crash with `FatalError` if encountered).
- Persistent storage state / session cookie reuse (defer until we know
  whether re-auth fatigue is real).
- Auto-discovery of files in `Input/` (operator passes the path explicitly).
- Auto-rotation of log files.
- Scheduling / unattended operation (operator is at the machine).
- Telegram or other remote OTP forwarding (operator is at the machine).

## 16. Approval

Brainstormed and approved by the user (Aryan) on 2026-04-13 across five design
sections: layout & flow, chat interaction & no-false-positive guarantee,
recovery (revised after the operator clarified that sessions survive reloads),
Excel I/O & logging, and config & testing strategy.

The next step is to invoke the `superpowers:writing-plans` skill to produce a
detailed implementation plan from this design.
