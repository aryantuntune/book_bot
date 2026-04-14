# PyInstaller spec for booking_bot. Builds a single shareable .exe with
# bundled Chromium 1134 + ffmpeg 1010 + the Playwright driver, so the bot
# does NOT depend on the operator having Google Chrome installed.
#
# Why bundled Chromium (not channel="chrome"):
#   1. Real client laptops have flaky Chrome installs that don't preserve
#      Playwright's persistent user-data dir across runs, which causes the
#      session cookie to drop and the bot to ask for an OTP on every row.
#      Bundled Chromium with .chromium-profile/ next to the .exe survives
#      restarts cleanly.
#   2. Removes the "install Google Chrome first" requirement entirely.
#
# Trade-off: the .exe grows from ~65 MB to ~450-500 MB. Too big for the
# old WhatsApp 100 MB cap but fits the modern 2 GB cap; otherwise share
# via Drive / USB.
#
# Usage (from repo root, after `pip install -r requirements-build.txt`
# and `python -m playwright install chromium`):
#
#     python -m PyInstaller booking_bot.spec --clean --noconfirm
#
# Output: dist/booking_bot.exe — a single self-extracting file.
#
# Runtime behavior:
#   - Double-click: headed Chromium + a new console window showing live logs.
#   - `booking_bot.exe --headless`: no Chromium window, no new console,
#     attaches to the parent cmd.exe only if the user is already in one.
#     Requires a previously-established session in .chromium-profile/.

import os
from pathlib import Path

# ---- Locate the playwright browser cache --------------------------------
# playwright 1.47 pins chromium-1134 and ffmpeg-1010. We bundle both into
# ms-playwright/ inside the bundle; at runtime, _pyi_bootstrap sets
# PLAYWRIGHT_BROWSERS_PATH to that dir so the driver finds them.
local_appdata = os.environ.get("LOCALAPPDATA", "")
pw_cache = Path(local_appdata) / "ms-playwright"
chromium_src = pw_cache / "chromium-1134"
ffmpeg_src   = pw_cache / "ffmpeg-1010"

for src in (chromium_src, ffmpeg_src):
    if not src.exists():
        raise SystemExit(
            f"Missing playwright browser cache: {src}\n"
            "Run `python -m playwright install chromium` first."
        )

# ---- Locate the playwright driver ---------------------------------------
# The driver (node.exe + package/ dir) lives inside site-packages/playwright
# and must be shipped intact or playwright.sync_api won't start.
import playwright
pw_pkg_dir = Path(playwright.__file__).resolve().parent
pw_driver_src = pw_pkg_dir / "driver"
if not pw_driver_src.exists():
    raise SystemExit(f"Missing playwright driver dir: {pw_driver_src}")

# ---- Bundle the newest recording, if any --------------------------------
# The bot auto-selects the newest .jsonl in recordings/ at runtime. We
# bundle whatever recordings/ currently holds so the .exe has a playbook.
repo_root = Path(SPECPATH).resolve()  # noqa: F821 — SPECPATH is PyInstaller-injected
recordings_dir = repo_root / "recordings"

datas = [
    (str(chromium_src), "ms-playwright/chromium-1134"),
    (str(ffmpeg_src),   "ms-playwright/ffmpeg-1010"),
    (str(pw_driver_src), "playwright/driver"),
]
if recordings_dir.exists():
    jsonl_files = sorted(
        recordings_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in jsonl_files:
        datas.append((str(p), "recordings"))

# ---- Analysis -----------------------------------------------------------
a = Analysis(
    ["booking_bot/__main__.py"],
    pathex=[str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "playwright",
        "playwright.sync_api",
        "playwright._impl._api_types",
        "openpyxl",
        "openpyxl.cell._writer",
        "colorlog",
        "booking_bot.ui",
        "booking_bot.playbook",
        "booking_bot.record",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["booking_bot/_pyi_bootstrap.py"],
    excludes=[
        "tkinter.test",
        "test",
        "unittest",
        "pytest",
        "_pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Single-file build: inline binaries + datas into EXE(...) with no COLLECT
# stage. PyInstaller writes one self-extracting booking_bot.exe to dist/.
# console=False suppresses the automatic terminal window; _pyi_bootstrap
# then decides at runtime whether to allocate one (double-click) or skip
# it entirely (--headless).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="booking_bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
