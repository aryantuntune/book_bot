# Multi-Instance Orchestrator Design

**Date:** 2026-04-15
**Status:** Draft — pending user review
**Related specs:**
- `2026-04-15-overnight-batch-survivability-design.md` — owns cross-process OTP coordination. This spec deliberately defers mid-run OTP flood handling to that work.
- `2026-04-15-ai-advisor-design.md` — unrelated; orthogonal safety net inside each bot instance.

---

## 1. Problem

The booking bot runs one process at a time. Each run takes a single xlsx file, one Chrome profile, and plods through rows sequentially. To book 4 sources × 2–4k customers per source, the operator currently:

- splits each source file by hand into ~1 k chunks,
- copies chunks across 3 laptops,
- opens a new terminal on each laptop for each chunk,
- enters OTP separately for every fresh profile,
- eyeballs each terminal to see progress,
- and has no central way to see "how is ASU going overall" or to restart one stuck chunk without chasing down which terminal window it lives in.

This is labor-intensive, fragile, and wastes CPU headroom on the laptops. The laptops have 8–32 GB RAM sitting nearly idle.

## 2. Goals

1. **One command, one source, many concurrent chunks.** `orchestrator start --source <ANY_NAME> --input <ANY_FILE.xlsx> [--instances 25 | --chunk-size 500]` splits the file, spawns N headless bot instances, and begins booking in parallel. Source name is operator-chosen, free-form, and never hardcoded in the implementation. Any `ASU`/`BPCL`/etc. reference in this spec is illustrative only.
2. **Concurrent multi-source on a single laptop.** The same laptop can have multiple sources running simultaneously — each with its own chunk count — in the same monitor view or separate ones. Chunk counts are per-source and controlled at `start` time; no hardcoded limits.
3. **Single aggregate monitor.** `orchestrator monitor` shows a live table of every running chunk across every source with progress, phase, idle time, and totals. Detachable and re-attachable without killing chunks.
4. **Restart and kill from the monitor.** One-keystroke restart of a stalled or failed chunk. One-keystroke stop of a whole source. No tracking which cmd window belongs to which chunk.
5. **Browser visibility is a lever, not a constraint.** Chunks can launch with visible Chrome windows OR headless — it's a `start`-time flag (`--headed` / `--headless`, default headless). The monitor reads heartbeat files and is completely independent of browser visibility. If the operator wants 25 visible browsers tiled across the screen AND the monitor, they can. If they want 25 invisible ones AND just the monitor, they can.
5. **One OTP entry per source per day.** `orchestrator auth --source <NAME>` captures an authenticated Chrome profile once, and all chunks for that source clone from it. You pick how many clones — the auth-seed profile is cloned to exactly as many targets as you requested with `--instances`/`--chunk-size`.
6. **No new dependencies the operator can't install on Windows.** Must work on stock Python 3.12 + the existing project's deps + `rich`.

## 3. Non-Goals

- **Distributed orchestration across laptops.** Each laptop runs its own orchestrator. No network coordination. Operator manually assigns sources to laptops.
- **Replacing the existing manual CLI.** `python -m booking_bot <file>` keeps working exactly as today for single-instance use and for the auth-seed path. Orchestrator is additive.
- **Mid-run OTP flood prevention.** If all chunks of a source hit the 20 h cooldown simultaneously 20 h later, they will each demand OTP. This is the survivability spec's territory; orchestrator just surfaces the failures cleanly.
- **Graphical UI.** Terminal-only. No web dashboard, no tray app, no Electron.
- **Automatic output merging.** After all chunks of a source finish, the operator has per-chunk Output/Issues files tagged with the chunk id. Merging back into one file is a separate concern (can be added later as a 20-line utility).
- **Aesthetically clean output filenames.** The existing bot's `ExcelStore` tags output files with `<input-stem>-<profile-suffix>.xlsx`. Since each chunk's input is already `<chunk-id>.xlsx` and the profile suffix is also `<chunk-id>`, output files land as `Output/<chunk-id>-<chunk-id>.xlsx` (e.g. `Output/ASU-03-ASU-03.xlsx`). Functional but ugly. Renaming is a cosmetic v2 concern — possible fix is adding a new `--output-suffix ""` flag to the existing bot CLI that overrides the profile-suffix-based tagging.
- **Dynamic re-chunking.** Chunk size is fixed per `start` call. If a chunk stalls hard, operator restarts it as a whole chunk; we don't re-split mid-run.

## 4. Prerequisites

- Python 3.12 (already in use).
- Playwright with persistent context (already in use).
- `rich >= 13.0` — for `rich.Live`, `rich.Table`, `rich.Console`. Pin in `pyproject.toml`.
- Existing bot CLI supports `--profile-suffix`, `--headless`, and the env vars `BOOKING_BOT_HEARTBEAT_PATH` / `BOOKING_BOT_SOURCE` / `BOOKING_BOT_CHUNK_ID` (added in this spec, defaults to no-op if unset).
- Windows 10/11. Orchestrator MUST NOT break on POSIX but priority is Windows.

## 5. Architecture

```
                     ┌───────────────────────────────────────┐
                     │  python -m booking_bot.orchestrator   │
                     │               cli.py                  │
                     └────────────┬──────────────────────────┘
                                  │
       ┌──────────────────┬───────┴───────┬─────────────────┬──────────────┐
       │                  │               │                 │              │
       ▼                  ▼               ▼                 ▼              ▼
  splitter.py       auth_template.py  spawner.py      heartbeat.py   monitor.py
  (stateless)       (one-shot)        (subprocess)    (json file     (rich.Live
                                                       read/write)    + stdin)
       │                  │               │                 ▲              ▲
       ▼                  ▼               ▼                 │              │
 Input/chunks/       .chromium-           ┌───────────┐      │              │
   ASU/              profile-             │ bot child │──────┘              │
     *.xlsx          ASU-01..             │ process   │                     │
                     ASU-07               │           │─ writes ─ data/runs/│
                                          │ (headless)│      ASU/           │
                                          └───────────┘      *.heartbeat.json
                                                                            │
                                                                  reads ◄───┘
```

**Principles:**

- **Stateless commands.** `start`, `monitor`, `stop`, and `status` all use the filesystem as their only source of truth. No daemons, no sockets, no database. Kill any orchestrator command at any time; chunks and state survive.
- **Child processes are fully independent.** They have no parent orchestrator process to die with. They read nothing from the orchestrator after spawn beyond env vars.
- **Heartbeat file is the contract.** The only protocol between bot and monitor. Any bot that writes a valid heartbeat is visible; any bot that doesn't is invisible (so manual `python -m booking_bot` runs don't pollute the monitor).
- **No IPC libraries.** File I/O + `subprocess.Popen` + `os.kill` cover everything.

### 5.1. File layout

**New:**
```
booking_bot/orchestrator/
├── __init__.py           # empty
├── __main__.py           # 5-line: from .cli import main; main()
├── cli.py                # argparse, subcommand dispatch (~150 LoC)
├── splitter.py           # ~80 LoC
├── auth_template.py      # ~120 LoC
├── spawner.py            # ~100 LoC
├── heartbeat.py          # ~120 LoC (read + write + masking)
└── monitor.py            # ~250 LoC (rich table, stdin reader, command parser)

data/runs/                # runtime only, created on demand
  <source>/
    <chunk-id>.heartbeat.json
    .start.lock           # advisory lock, PID + timestamp

Input/chunks/             # generated by splitter
  <source>/
    <chunk-id>.xlsx

logs/orchestrator/        # child stdout/stderr captures
  <chunk-id>.out.log
  <chunk-id>.err.log

tests/test_orchestrator_splitter.py
tests/test_orchestrator_heartbeat.py
tests/test_orchestrator_auth_template.py
tests/test_orchestrator_spawner.py
tests/test_orchestrator_monitor.py
tests/test_orchestrator_cli.py
tests/fixtures/orchestrator/
  tiny_source.xlsx         # ~20 rows, used for splitter tests
  fake_bot.py              # writes 3 heartbeats and exits, used for spawner tests
```

**Modified:**
```
booking_bot/config.py     # + 4 path constants
booking_bot/cli.py        # + _write_heartbeat() helper, + 8 call sites
pyproject.toml            # + rich>=13
```

## 6. Components

### 6.1. `splitter.py`

**Purpose:** Turn a big xlsx into N smaller xlsx files preserving headers and row order.

**Public API:**
```python
@dataclass(frozen=True)
class ChunkSpec:
    source: str            # operator-chosen, e.g. "ASU" or "indian-oil-feb"
    chunk_id: str          # "<source>-<NN>", e.g. "ASU-03"
    chunk_index: int       # 3  (1-based)
    input_path: Path       # Input/chunks/<source>/<chunk_id>.xlsx
    profile_suffix: str    # same as chunk_id
    heartbeat_path: Path   # data/runs/<source>/<chunk_id>.heartbeat.json
    row_count: int         # number of data rows in this chunk (header not counted)

def split(
    source: str,
    input_file: Path,
    *,
    chunk_size: int | None = None,
    num_chunks: int | None = None,
    output_dir: Path = Path("Input/chunks"),
) -> list[ChunkSpec]: ...
```

**Behavior:**
1. **Parallelism mode** — exactly one of `chunk_size` and `num_chunks` must be provided. Raises `ValueError("pass exactly one of chunk_size or num_chunks")` if both or neither are set.
   - `chunk_size=N` → every chunk has N rows (last one may be smaller); total chunk count is `ceil(total_rows / N)`.
   - `num_chunks=M` → exactly M chunks; size per chunk is `ceil(total_rows / M)` (last one may be smaller); if `M > total_rows`, raises `ValueError("num_chunks M exceeds row count R")`.
2. `source` becomes the stem of the chunks subdir AND the prefix of each chunk filename. Example: operator passes `--source ASU` and `--input Input/ASU-Feb2026.xlsx`, chunks are written to `Input/chunks/ASU/ASU-01.xlsx`..`ASU-NN.xlsx`. The source string is the sole identity — the original input filename never appears in chunk names, profile suffixes, or heartbeat paths. The source name is operator-chosen; any name is valid as long as it passes validation below.
3. Validates `source` matches `^[A-Za-z0-9_-]{1,32}$`. Raises `ValueError` otherwise.
4. Opens input xlsx with `openpyxl.load_workbook(read_only=True)`. Reads the header row (row 1) from the first sheet.
5. Iterates data rows. Every `effective_chunk_size` rows, closes the current chunk workbook and starts a new one. Chunk numbering is 1-based, zero-padded to `max(2, len(str(N)))` digits so sorting works for any chunk count.
6. Each chunk workbook has exactly one sheet (same name as source input's first sheet), header row + up to `effective_chunk_size` data rows.
7. Returns `list[ChunkSpec]` with all fields populated, in chunk-index order.
8. **Idempotent:** If `Input/chunks/<source>/<chunk_id>.xlsx` already exists and its row count matches what we're about to write, skip writing and just return the ChunkSpec. Lets the operator re-run `start` without wasting disk writes.

**Edge cases:**
- `chunk_size=500` on 3501 rows → 8 chunks: 500×7 + 1×1. Last chunk is allowed to be smaller (never zero).
- `num_chunks=25` on 5000 rows → 25 chunks of 200 rows each (exact). `num_chunks=25` on 5001 rows → 25 chunks of 201, 201, ..., and the last one with whatever remainder (may be smaller).
- `num_chunks=100` on 50 rows → `ValueError`. We never create empty chunks.
- Input has 0 data rows → raises `ValueError("input file has no data rows")`.
- `chunk_size` 0 or negative, or `num_chunks` 0 or negative → `ValueError`.
- Existing chunks dir with different effective chunk_size → the row-count check will mismatch → overwrites. Prints a warning.
- **No hardcoded upper bound on chunk count.** If the operator says `--instances 200` on a 5000-row file, they get 200 chunks. Sanity warnings (not errors) print for `num_chunks > 50` or `chunk_size < 10`, since both are unusual and often typos.

**Not handled:**
- Multi-sheet inputs — first sheet only. If the operator has multi-sheet books, preprocess externally.
- Column schema validation — delegate to the existing `ExcelStore` which raises clear errors when a chunk is opened by the bot.

### 6.2. `auth_template.py`

**Purpose:** Get one Chrome profile authenticated with HPCL, then fan it out to all chunk profiles so chunks can run `--headless` from the first row.

**Public API:**
```python
def ensure_auth_seed(source: str, *, operator_phone: str | None = None) -> Path:
    """Returns path to the auth-seed profile dir. Guaranteed authenticated
    within AUTH_COOLDOWN_S as of return time."""

def clone_to_chunks(source: str, chunks: list[ChunkSpec]) -> None:
    """Copies the auth-seed profile to each chunk's profile dir. Skips
    chunks whose profile already has a fresh last_auth.json."""
```

**`ensure_auth_seed` behavior:**

Try each path until one succeeds:

**Path A — seed already fresh:**
- If `.chromium-profile-<source>-auth-seed/last_auth.json` exists and `now - ts < AUTH_COOLDOWN_S - 2h`, return the path. (Buffer of 2h so clones still have time before the cooldown expires during the run.)

**Path B — reuse operator's main profile:**
- If `.chromium-profile/last_auth.json` exists and is fresh (same buffer), copy `.chromium-profile` → `.chromium-profile-<source>-auth-seed`, scrub lock files, return. This is the "operator already auth'd manually today" fast path.

**Path C — interactive auth:**
- Launch browser via `browser.start_browser(profile_suffix=f"{source}-auth-seed", headless=False)`.
- Print: `"Auth seed: waiting for you to log in to HPCL in the browser window. I'll close the window once auth completes."`
- Poll `.chromium-profile-<source>-auth-seed/last_auth.json` every 2 s. When its mtime is newer than the start of the poll, wait one additional 5 s (let any final redirects settle), then `browser.close()`, return the path.
- Timeout: 15 minutes. Raises `AuthSeedTimeout` (new exception in `booking_bot.exceptions`).

**`clone_to_chunks` behavior:**

For each chunk:
1. Target dir = `config.ROOT / f".chromium-profile-{chunk.profile_suffix}"`.
2. If target already exists and has fresh `last_auth.json`, skip.
3. Otherwise: if target exists, `shutil.rmtree(target)`. Then `shutil.copytree(seed_path, target)`.
4. After copy, delete these files inside target (they prevent Chromium from reopening the profile): `SingletonLock`, `SingletonCookie`, `SingletonSocket`, `Default/LOCK` (if present). Silently ignore "not found" errors.
5. If `copytree` raises (disk full, permission), log and collect the failure. After iterating all chunks, if any failed, raise `AuthCloneFailed(failures=[...])` listing each chunk_id + error. Caller aborts `start` cleanly.

**Idempotency:** Running `clone_to_chunks` twice in a row is a no-op the second time (the fresh-auth check skips everything).

### 6.3. `spawner.py`

**Purpose:** Start one bot child process per chunk, in a way that doesn't pop up a console window and doesn't die when the parent does.

**Public API:**
```python
@dataclass
class ChildHandle:
    chunk_id: str
    pid: int
    popen: subprocess.Popen
    stdout_log: Path
    stderr_log: Path

def spawn_chunk(spec: ChunkSpec, *, headed: bool = False) -> ChildHandle: ...
def kill_chunk(handle: ChildHandle, *, timeout_s: float = 10.0) -> int:
    """SIGTERM, wait, return exit code. SIGKILL if timeout exceeded."""
```

**`spawn_chunk` behavior:**
1. Ensure `spec.heartbeat_path.parent` exists.
2. Write an initial heartbeat with `phase="starting"`, `pid=-1` (to be overwritten by the bot itself), `rows_done=0`, `rows_total=spec.row_count`, `started_at=now`, `command=<cmd list>`. This guarantees the monitor sees the chunk immediately.
3. Build command:
   ```python
   cmd = [sys.executable, "-m", "booking_bot", str(spec.input_path),
          "--profile-suffix", spec.profile_suffix]
   if not headed:
       cmd.append("--headless")
   # headed=True omits the flag entirely; the existing bot CLI defaults
   # to launching a visible Chrome window when --headless is absent.
   ```
4. Env:
   ```python
   env = {**os.environ,
          "BOOKING_BOT_HEARTBEAT_PATH": str(spec.heartbeat_path),
          "BOOKING_BOT_SOURCE": spec.source,
          "BOOKING_BOT_CHUNK_ID": spec.chunk_id}
   ```
5. Open stdout/stderr log files under `logs/orchestrator/`.
6. Windows: `creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS` when `headed=False`; when `headed=True`, use `creationflags = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS` so each visible Chrome has its own parent console window that survives the orchestrator detaching. POSIX: `start_new_session=True` regardless of `headed`.
7. `subprocess.Popen(cmd, stdin=DEVNULL, stdout=out_f, stderr=err_f, env=env, creationflags=flags)`.
8. After Popen succeeds, overwrite the starting heartbeat's `pid` field with the real PID.
9. Return `ChildHandle`.

**Visibility is orthogonal to monitoring.** Whether the bot runs headed or headless, it writes the same heartbeat file in the same format to the same path. The monitor renders from heartbeat files only; it never touches browser state, stdin, or child stdout. The operator can freely mix modes across sources (ASU headless, BPCL headed) on the same laptop.

**`kill_chunk` behavior:**
1. `handle.popen.terminate()` (SIGTERM on POSIX, `TerminateProcess` on Windows).
2. `handle.popen.wait(timeout=timeout_s)`. On timeout, `.kill()` and wait again.
3. Return the exit code.
4. After return, overwrite the heartbeat with `phase="failed"` and `exit_code=<code>` ONLY IF the child's own heartbeat writer didn't already mark it completed. (Check mtime: if heartbeat was updated within the last 2 seconds, trust the child's version.)

### 6.4. `heartbeat.py`

**Purpose:** Read and write the JSON heartbeat contract.

**Public API:**
```python
@dataclass
class Heartbeat:
    source: str
    chunk_id: str
    pid: int
    input_file: str
    profile_suffix: str
    phase: str  # Literal["starting","authenticating","booking","recovering","idle","completed","failed"]
    rows_total: int
    rows_done: int
    rows_issue: int
    rows_pending: int
    current_row_idx: int | None
    current_phone: str | None  # masked
    started_at: str      # ISO-8601 with tz
    last_activity_at: str
    command: list[str]
    exit_code: int | None
    last_error: str | None

def write(path: Path, hb: Heartbeat) -> None:
    """Atomic temp-file + os.replace write."""

def read(path: Path) -> Heartbeat | None:
    """Returns None if file missing or malformed."""

def read_all(runs_dir: Path, source: str | None = None) -> list[Heartbeat]:
    """Glob + read all. If source given, filter to data/runs/<source>/*.json."""

def mask_phone(phone: str) -> str:
    """'9876543210' -> '98xxxxxx10'. Keeps first 2 + last 2, Xs middle.
    Returns the input unchanged if len < 4."""
```

**Write:** JSON with `indent=2`. Write to `path.with_suffix(".tmp")`, `os.replace` to final path. Swallow `PermissionError` if the target is briefly locked (retry once after 50 ms), otherwise log and continue.

**Read:** Catch `FileNotFoundError`, `json.JSONDecodeError`, `KeyError` — return `None`. Never raise. This is critical because the monitor reads many files per tick and one briefly-corrupt file (caught mid-write) must not crash the UI.

**Mask:** Pure function. Phone "9876543210" → "98xxxxxx10". The Heartbeat dataclass's `current_phone` field is written already-masked; the bot calls `mask_phone` before building the Heartbeat.

### 6.5. `monitor.py`

**Purpose:** Render live status and accept commands.

**Public API:**
```python
def run_monitor(
    source_filter: str | None = None,
    *,
    runs_dir: Path = Path("data/runs"),
    refresh_hz: float = 1.0,
) -> int: ...  # return code
```

**Structure:**

Three cooperating pieces inside `monitor.py`:
1. **Renderer:** owns a `rich.Live(table, refresh_per_second=refresh_hz)`. Every tick, calls `heartbeat.read_all(runs_dir, source_filter)`, builds a `rich.Table`, calls `live.update(table)`.
2. **Input thread:** daemon thread reads `stdin.readline()` in a loop, posts each line to a `queue.Queue`.
3. **Command handler:** main loop consumes the queue, dispatches to handlers. Handlers mutate the filesystem (spawn, kill) and the next render tick picks up the changes naturally.

**Command parser** (strict, no regex soup):

```
r <chunk-id>       | restart <chunk-id>    → restart
k <chunk-id>       | kill <chunk-id>       → kill
start <src> <path> [size]                  → new source (size defaults to 500)
stop <source>                              → stop all chunks of source
q                                          → detach, chunks keep running
qq                                         → stop all visible chunks, then exit
h                  | help                  → show inline help
```

Unknown commands print a red error and do nothing. Autocomplete is out of scope.

**Table columns:**
```
Chunk | PID | Phase | Done | Issue | Pending | Progress | Idle | ETA
```

- **Phase** is color-coded: `booking` white, `completed` green, `failed` red, `recovering`/`idle` yellow, `starting`/`authenticating` cyan.
- **Idle** = `now - last_activity_at` in human form (`2s`, `47s`, `3m 12s`, `1h 05m`). Shows ⚠ if > 2 min.
- **Progress** = unicode bar `▓▒░` + percent.
- **ETA** per chunk = `(rows_pending / rows_done_rate)` where the rate is computed from `(rows_done / (last_activity - started_at))`. If `rows_done == 0`, show `—`.

**Footer:**
```
Totals: done=X/Y (Z%)  issue=N  failed=M  elapsed=T  ETA ~T
> _user input_
```

**Auto-restart:** Default-on heuristic. If a heartbeat's `last_activity_at` is stale by > 10 min AND its `phase` is `booking`/`recovering`/`idle` (not `completed`/`failed`), monitor auto-restarts it. Auto-restart is rate-limited: at most 3 auto-restarts per chunk per monitor session. After 3, the chunk is marked `failed` with `last_error="auto-restart budget exhausted"` and requires manual intervention. This is a safety cap — not a replacement for the bot's own internal recovery.

**Detach (`q`):** stop the render loop, stop the input thread, return 0. Chunks keep running. No SIGTERM.

**Full stop (`qq`):** iterate all currently-live heartbeats (those without `exit_code`), SIGTERM each PID, wait up to 10 s each, return 0.

### 6.6. `cli.py` (orchestrator)

**Purpose:** Argparse entry point, subcommand dispatch, shared error handling.

**Commands** (exact forms):
```
python -m booking_bot.orchestrator auth --source <SRC> [--operator-phone PHONE]
python -m booking_bot.orchestrator start --source <SRC> --input <FILE>
                                         (--chunk-size N | --instances M)
                                         [--headed | --headless]
                                         [--no-monitor]
python -m booking_bot.orchestrator monitor [--source <SRC>]
python -m booking_bot.orchestrator stop --source <SRC>
python -m booking_bot.orchestrator status [--source <SRC>] [--json]
```

**`start` argument rules:**
- `--source` is required and free-form (any operator-chosen name matching `^[A-Za-z0-9_-]{1,32}$`).
- `--input` is required.
- **Parallelism:** exactly one of `--chunk-size` / `--instances` must be given. Passing both raises an argparse error. If neither is given, defaults to `--chunk-size 500`.
- **Visibility:** `--headed` (Chrome windows pop up, one per chunk) and `--headless` (hidden) are mutually exclusive. Default is `--headless`. The spawner passes the corresponding flag to each child `booking_bot` process. The monitor is unaffected by this choice — it reads heartbeat files.
- `--no-monitor` suppresses the automatic hand-off to the monitor after spawn; useful for scripting.

**`start` flow:**
1. Acquire `data/runs/<source>/.start.lock` (write `{pid, started_at}`). If lock exists and the lock's PID is still alive, refuse with a clear error. If the PID is dead (stale lock), overwrite.
2. Call `splitter.split(source, input_file, chunk_size=..., num_chunks=...)` → `chunks: list[ChunkSpec]`.
3. Call `auth_template.ensure_auth_seed(source)`.
4. Call `auth_template.clone_to_chunks(source, chunks)`. On failure: release lock, print which chunks failed, exit 2.
5. Call `spawner.spawn_chunk(spec, headed=<bool>)` for each chunk. Record handles.
6. Release the `.start.lock` (we're done; handles are on disk as heartbeats).
7. Unless `--no-monitor`, hand off to `monitor.run_monitor(source_filter=source)`.

**`stop` flow:** read all heartbeats for the source, SIGTERM each pid (skip completed/failed), wait, done.

**`status --json` flow:** print `json.dumps(read_all(...), indent=2)`. For scripting / grep / external tools.

## 7. Data Model

### 7.1. Heartbeat JSON schema (authoritative)

```json
{
  "source": "ASU",
  "chunk_id": "ASU-03",
  "pid": 12345,
  "input_file": "Input/chunks/ASU/ASU-03.xlsx",
  "profile_suffix": "ASU-03",
  "phase": "booking",
  "rows_total": 500,
  "rows_done": 127,
  "rows_issue": 3,
  "rows_pending": 370,
  "current_row_idx": 128,
  "current_phone": "99xxxxxx99",
  "started_at": "2026-04-15T13:10:00+00:00",
  "last_activity_at": "2026-04-15T14:32:05+00:00",
  "command": ["python","-m","booking_bot","Input/chunks/ASU/ASU-03.xlsx","--profile-suffix","ASU-03","--headless"],
  "exit_code": null,
  "last_error": null
}
```

**Field invariants:**
- `rows_done + rows_issue + rows_pending == rows_total` (always; the monitor can assert this).
- `phase == "completed"` iff `rows_pending == 0` and `exit_code == 0`.
- `phase == "failed"` iff `exit_code != 0` OR `exit_code == 0` with `rows_pending > 0` (crashed mid-run).
- `current_row_idx` is `None` when phase is `starting`/`completed`/`failed`, else the 1-based row being worked.
- `current_phone` is always pre-masked.

**Timestamps:** always ISO-8601 with explicit `+00:00` UTC. No naive datetimes.

### 7.2. Lock file schema (`data/runs/<source>/.start.lock`)

```json
{"pid": 12340, "started_at": "2026-04-15T13:09:55+00:00"}
```

Only used to prevent accidental double-start of the same source. Not used after spawn finishes.

## 8. Config changes

Add to `booking_bot/config.py` (after the existing `LOGS_DIR` block):

```python
# ---- Multi-instance orchestrator (see docs/superpowers/specs/2026-04-15-multi-instance-orchestrator-design.md) ----
RUNS_DIR                        = ROOT / "data" / "runs"
CHUNKS_DIR                      = ROOT / "Input" / "chunks"
ORCHESTRATOR_LOGS_DIR           = ROOT / "logs" / "orchestrator"
ORCHESTRATOR_AUTH_SEED_BUFFER_S = 7200          # 2h buffer before AUTH_COOLDOWN_S
ORCHESTRATOR_STALL_THRESHOLD_S  = 600           # 10 min → auto-restart eligible
ORCHESTRATOR_IDLE_WARNING_S     = 120           # 2 min → ⚠ in table
ORCHESTRATOR_MAX_AUTO_RESTARTS  = 3             # per chunk per monitor session
ORCHESTRATOR_KILL_TIMEOUT_S     = 10.0          # SIGTERM → SIGKILL fallback
ORCHESTRATOR_AUTH_TIMEOUT_S     = 900           # 15 min interactive auth wait
```

Create `RUNS_DIR`, `CHUNKS_DIR`, and `ORCHESTRATOR_LOGS_DIR` lazily (on first use); don't `mkdir` at import time.

## 9. Bot-side integration (`booking_bot/cli.py`)

**New helper:**
```python
def _write_heartbeat(
    phase: str,
    store: "ExcelStore",
    *,
    current_row_idx: int | None = None,
    current_phone: str | None = None,
    last_error: str | None = None,
) -> None:
    path_str = os.environ.get("BOOKING_BOT_HEARTBEAT_PATH")
    if not path_str:
        return  # Manual run — no heartbeat.
    s = store.summary()  # total/done/success/ekyc/not_registered/payment_pending/issue/pending
    # NB: store.summary()["done"] already includes issue-bucket rows.
    # Heartbeat schema wants done/issue/pending disjoint, so subtract.
    rows_done    = s["done"] - s["issue"]
    rows_issue   = s["issue"]
    rows_pending = s["pending"]
    rows_total   = s["total"]
    assert rows_done + rows_issue + rows_pending == rows_total, "summary bucket math mismatch"
    # Build Heartbeat using those fields, mask phone, use
    # _heartbeat_started_at for started_at, datetime.now(UTC) for last_activity_at,
    # os.environ for source/chunk_id, etc. Call heartbeat.write().
```

`_heartbeat_started_at` is a module-level `datetime` set to `datetime.now(tz=UTC)` the first time `_write_heartbeat` is called, then reused.

**Completion check:** Instead of adding a new `is_done()` method to `ExcelStore`, the finally-block heartbeat uses `store.summary()["pending"] == 0` inline. Minimizes churn to existing code.

**Call sites (8):**

1. Right after `store = ExcelStore(...)` is constructed: `_write_heartbeat("starting", store)`.
2. After the first successful `detect_state` that isn't an auth state: `_write_heartbeat("authenticating", store)` then on next loop `_write_heartbeat("booking", ...)`.
3. Top of row loop, per iteration, BEFORE doing any work on the row: `_write_heartbeat("booking", store, current_row_idx=row_idx, current_phone=phone)`.
4. After a row is marked done (inside the success branch, right after `store.mark_success(...)` or equivalent): `_write_heartbeat("booking", store, current_row_idx=row_idx, current_phone=phone)`.
5. Before calling `_recover_with_playbook` or any explicit reset path: `_write_heartbeat("recovering", store, current_row_idx=row_idx, current_phone=phone)`.
6. Inside `wait_until_settled`'s quiet loop if total elapsed exceeds `ORCHESTRATOR_IDLE_WARNING_S` while waiting: `_write_heartbeat("idle", store, current_row_idx=row_idx, current_phone=phone)`. This keeps the timestamp fresh during legitimate long waits.
7. Finally block at end of `main()`: `_write_heartbeat("completed" if store.summary()["pending"] == 0 else "failed", store)`.
8. Top-level exception handler: `_write_heartbeat("failed", store, last_error=str(e)[:500])`.

**Cost:** ~2 ms per call. Negligible compared to row processing time.

## 10. Error handling

| Scenario | Detection | Response |
|---|---|---|
| Chunk Python exception | Bot's top-level handler → writes `failed` heartbeat with `last_error`, exits nonzero | Monitor shows red row. Operator runs `r <chunk-id>`. |
| Chunk subprocess crashes without writing heartbeat (OS kill, SIGKILL, Python segfault) | Monitor's staleness check: `last_activity_at > 10 min ago` AND PID dead | Marked `failed`. Eligible for auto-restart if under budget. |
| Chunk stalls (still running, no progress) | `last_activity_at > 10 min ago` AND PID alive AND phase ∈ {booking, recovering, idle} | Auto-restart (if under budget) or mark for manual intervention. |
| Orchestrator `start` crashes mid-spawn | Some chunks spawned, some not. Lock file may be left behind. | Spawned chunks keep running. Operator sees them in `monitor`. `.start.lock` is stale, next `start` detects dead PID and overwrites. |
| Monitor crashes | Chunks don't know | Operator runs `monitor` again. State is filesystem. |
| Two operators both run `start --source ASU` | `.start.lock` with live PID | Second `start` refuses, prints `"source ASU is already starting (pid X, at TIME)"`. |
| Profile clone fails (disk full, ACL) | `shutil.copytree` exception | `start` aborts BEFORE spawning any chunk. Partial clones are left in place for debugging. Operator fixes disk and retries. |
| Auth-seed timeout (15 min without login) | Poll loop exceeded `ORCHESTRATOR_AUTH_TIMEOUT_S` | `ensure_auth_seed` raises `AuthSeedTimeout`. `start` prints error, exits 3. |
| `stop` on a source with no running chunks | Empty heartbeat list | Prints `"no running chunks for source ASU"`, exits 0. |
| Heartbeat file briefly corrupt (mid-write race) | `json.JSONDecodeError` in reader | `read()` returns `None`, chunk temporarily invisible in one tick, visible again on next tick. |
| `qq` with chunks in `recovering` phase | Bot is mid-recovery | SIGTERM interrupts it. Next `start`/`restart` resumes from the Output file (existing `pending_rows` logic). No data loss. |

## 11. Testing strategy

### 11.1. Unit tests (fast, no browser, no subprocess)

**`test_orchestrator_splitter.py`:**
- Tiny fixture xlsx (20 rows + header). `split("TEST", fixture, chunk_size=5)` → 4 chunks of 5 rows.
- Source name validation: bad chars (e.g., `"foo/bar"`, `"has space"`, empty) → `ValueError`. Arbitrary valid names (`"IOCL"`, `"indian-oil-feb"`, `"x"`) → succeed; confirming no hardcoded source names.
- Parallelism mode exclusivity: both `chunk_size` and `num_chunks` set → `ValueError`. Neither set → `ValueError`.
- `num_chunks=4` on 20 rows → 4 chunks of 5. `num_chunks=3` on 20 rows → 3 chunks of `ceil(20/3)=7, 7, 6`.
- `num_chunks=1` on 20 rows → one chunk with all 20 rows.
- `num_chunks > total_rows` → `ValueError`.
- `chunk_size=0` / `chunk_size=-1` / `num_chunks=0` → `ValueError`.
- Empty input → `ValueError`.
- Last-chunk-smaller case: 23 rows, size 5 → 5 chunks of 5,5,5,5,3.
- Idempotent: call `split` twice, assert no rewrites on second call (use file mtimes).
- Zero-padding: 12 chunks → `01..12`, 100 chunks → `001..100`, 7 chunks → `01..07`.
- High chunk count sanity warning: `num_chunks=100` on a 500-row file → succeeds but emits a warning to stderr (we don't want to block power users, but we do want them to notice typos).

**`test_orchestrator_heartbeat.py`:**
- Round-trip: build Heartbeat, write to tmp path, read back → equal.
- Malformed JSON: write garbage, `read()` returns None.
- Missing file: `read()` returns None.
- `mask_phone`: "9876543210" → "98xxxxxx10", "1234" → "12", "99" → "99", "" → "".
- Atomic write: write twice concurrently (threads), assert file is never half-written.

**`test_orchestrator_auth_template.py`:**
- `clone_to_chunks` with a mock seed dir (just some files + a fake `SingletonLock`). After clone, assert files present in targets and `SingletonLock` removed.
- `ensure_auth_seed` Path A: put a fresh `last_auth.json` in the seed, assert function returns without trying to launch browser (mock `browser.start_browser` to raise if called).
- `ensure_auth_seed` Path B: put a fresh `last_auth.json` in the main profile, seed missing, assert seed gets copied.
- Path C (interactive): skip in unit tests — covered by manual E2E only.

**`test_orchestrator_spawner.py`:**
- Uses `tests/fixtures/orchestrator/fake_bot.py`: a Python script that writes 3 heartbeats with fake progress then exits 0.
- Spawn a fake bot, wait for it to exit, assert heartbeat file has `exit_code=0` and `phase="completed"`.
- `kill_chunk` on a fake bot that sleeps for 60 s: assert exit code is non-zero, return within timeout.
- **Headless flag:** `spawn_chunk(spec, headed=False)` → command list contains `"--headless"`. `spawn_chunk(spec, headed=True)` → command list does NOT contain `"--headless"`.
- **Windows creationflags:** headed=False → `CREATE_NO_WINDOW` set. headed=True → `CREATE_NEW_CONSOLE` set. Both always OR `DETACHED_PROCESS`. (Check the Popen kwargs via a spy; skip the actual assertion on non-Windows.)
- Env var propagation: fake bot echoes its env vars to a side file, assert `BOOKING_BOT_HEARTBEAT_PATH`, `BOOKING_BOT_SOURCE`, `BOOKING_BOT_CHUNK_ID` are all set correctly.

**`test_orchestrator_monitor.py`:**
- Build a list of Heartbeat objects, call the table-builder, render to a `rich.Console(record=True)`, assert output contains expected chunk ids and percent strings.
- Command parser: dict of {input line → expected (action, args)}.
- Stall detection: heartbeat with `last_activity_at = now - 15 min`, assert auto-restart handler is invoked (with a spy on the restart function).
- Auto-restart budget: run the stall detector 4 times on the same chunk; 4th call does NOT restart.
- `q` vs `qq` behavior: with a spy on `kill_chunk`, assert `q` never kills, `qq` kills all live.

**`test_orchestrator_cli.py`:**
- Argparse happy-path: `start --source X --input Y --chunk-size 50` parses correctly.
- `start` with `--instances 20` parses correctly, passes `num_chunks=20` to splitter.
- `start --chunk-size 50 --instances 20` → argparse mutual-exclusion error.
- `start` with neither `--chunk-size` nor `--instances` → defaults to `chunk_size=500`, no error.
- `start` without `--source` fails.
- `start` without `--input` fails.
- `start --headed --headless` → argparse mutual-exclusion error.
- `start` with `--headed` alone → passes `headed=True` to spawner; without it, passes `headed=False`.
- Arbitrary source names accepted: parametrized over `["ASU", "IOCL", "BPCL-feb", "indian_oil", "x"]` → all succeed.
- Subcommand routing: each subcommand maps to the right function (spies).

### 11.2. Integration tests (medium, subprocess but no Chrome)

- `test_spawner_integration.py`: actually run `fake_bot.py` via `spawn_chunk`, assert real heartbeat file lifecycle.
- `test_cli_start_integration.py`: use `fake_bot.py` as a stand-in for the real bot (via env var `BOOKING_BOT_TEST_BIN_OVERRIDE`). Run `start --source TEST --input tests/fixtures/orchestrator/tiny_source.xlsx --chunk-size 5 --no-monitor`. Assert:
  - Chunks written to `Input/chunks/TEST/`.
  - 4 heartbeats appear under `data/runs/TEST/`.
  - All fake bots exit 0 within 10 s.
  - `.start.lock` is cleaned up.
- `test_cli_stop_integration.py`: start, then stop, assert all heartbeats show `failed` with negative exit code.

### 11.3. Manual E2E (one-time acceptance, with real browser)

Documented as a runbook in the spec, NOT automated:
1. Run `orchestrator auth --source TEST` manually, enter OTP once in the browser that appears.
2. Prepare `tests/fixtures/orchestrator/e2e_20rows.xlsx` with 20 real-looking rows.
3. Run `orchestrator start --source TEST --input tests/fixtures/orchestrator/e2e_20rows.xlsx --chunk-size 5`.
4. Verify: 4 chunks spawn, all auth successfully from the cloned profile (no OTP popups), begin booking.
5. Press `k TEST-02` → verify chunk 2 goes red within 15 s, others keep running.
6. Press `r TEST-02` → verify new PID, resume from where chunk 2 left off.
7. Press `q` → terminal returns, verify remaining chunks still in Task Manager.
8. Run `orchestrator monitor --source TEST` in a new terminal → verify same chunks visible.
9. Press `qq` → all chunks exit within 10 s.

## 12. Dependencies

**New:**
- `rich >= 13.0` — added to `pyproject.toml`. Already widely deployed; provides `Live`, `Table`, `Console`, ANSI color.

**No new infrastructure:** No Redis, no SQLite, no daemons, no systemd/nssm services.

## 13. Risks and rollback

| Risk | Mitigation |
|---|---|
| Chrome profile cloning turns out to be unreliable on Windows | Fallback to Path B (copy `.chromium-profile`) works well when operator auths manually. Worst case: a new sub-subcommand `orchestrator auth --force-interactive` that auths each chunk's profile sequentially (still one-time pain, then 20 h quiet). |
| Heartbeat file corruption under heavy write | `read()` treats missing/malformed as "not visible this tick". Single-tick glitches are invisible. Persistent corruption would be bug; covered by atomic-write tests. |
| Operator confusion with lots of chunks running | Totals row + color coding. If still a problem: add a `--brief` monitor mode that only shows non-green rows. |
| `rich.Live` performance with > 50 chunks | We don't expect > 30 chunks per laptop (4 sources × 7 chunks max). `rich` handles hundreds fine. If slow: cap refresh rate to 0.5 Hz. |
| Lock files inside Chrome profile dirs we don't know about | Clone then launch → Chrome complains. We scrub known lock files (`SingletonLock`, `SingletonCookie`, `SingletonSocket`, `Default/LOCK`). If unknown lock files surface in testing, add to the scrub list. |
| A chunk zombie (PID exists but bot is hung inside Playwright) | Monitor's 10-min stall detector catches it. Auto-restart SIGTERMs and respawns. Budget of 3 auto-restarts prevents infinite restart loops. |

**Rollback plan:** Orchestrator is purely additive. `booking_bot/orchestrator/` is one package — delete it and 4 lines from `pyproject.toml` and 10 lines from `booking_bot/cli.py` (`_write_heartbeat` calls) and the codebase returns to current behavior. Heartbeat writes in `cli.py` are env-gated no-ops when `BOOKING_BOT_HEARTBEAT_PATH` is unset, so manual runs are unaffected even if orchestrator files remain in place.

## 14. Implementation order (for the plan that comes next)

Logical dependency order — this is what the plan's TDD task breakdown should follow:

1. `config.py` constants + new exceptions (`AuthSeedTimeout`, `AuthCloneFailed`).
2. `heartbeat.py` — dataclass, read, write, mask. Pure, fully unit-testable.
3. `splitter.py` — openpyxl-based file splitting. Pure, fully unit-testable.
4. `booking_bot/cli.py` — add `_write_heartbeat` helper + 8 call sites. Manual runs still pass existing tests (env-gated no-op).
5. `auth_template.py` — clone logic first (unit-testable), interactive auth last (deferred to manual E2E).
6. `spawner.py` — using a fake bot script in tests.
7. `monitor.py` table builder + command parser (pure, unit-testable).
8. `monitor.py` run loop with `rich.Live` (integration-testable with fake heartbeats).
9. `orchestrator/cli.py` — argparse + dispatch. Integration tests with fake bot.
10. `orchestrator/__main__.py` — entry point.
11. Manual E2E runbook.

Each task from the implementation plan will follow TDD: failing test → minimal code → green → commit. See the separate implementation plan doc (to be created next via writing-plans).
