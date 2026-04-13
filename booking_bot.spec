# PyInstaller spec for booking_bot. Builds a single shareable .exe that
# drives the operator's installed Google Chrome via Playwright
# (channel="chrome") — so we do NOT bundle chromium-1134 or ffmpeg-1010.
# Only the ~78 MB Playwright node-based driver is embedded. The resulting
# one-file .exe is small enough to share via WhatsApp.
#
# Usage (from repo root, after `pip install -r requirements-build.txt`):
#
#     python -m PyInstaller booking_bot.spec --clean --noconfirm
#
# Output: dist/booking_bot.exe — a single file, ship it as-is.
#
# Runtime behavior:
#   - Double-click: headed Chrome + a new console window showing live logs.
#   - `booking_bot.exe --headless`: no Chrome window, no new console,
#     attaches to the parent cmd.exe only if the user is already in one.
#     Requires a previously-established session in .chrome-profile/.

from pathlib import Path

# ---- Locate the playwright driver ---------------------------------------
# The driver (node.exe + package/ dir) lives inside site-packages/playwright
# and must be shipped intact or playwright.sync_api won't start. This is the
# only piece of Playwright we still bundle — chromium itself comes from the
# operator's installed Chrome.
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
