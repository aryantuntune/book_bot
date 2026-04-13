# PyInstaller spec for booking_bot. Builds a one-dir bundle with Chromium,
# ffmpeg, and the playwright driver embedded, so the .exe runs offline on
# any Windows machine without needing `playwright install`.
#
# Usage (from repo root, after `pip install -r requirements-build.txt`
# and `python -m playwright install chromium`):
#
#     pyinstaller booking_bot.spec --clean --noconfirm
#
# Output: dist/booking_bot/booking_bot.exe plus a _internal/ folder. Zip the
# whole dist/booking_bot/ folder to ship.

import os
import sys
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="booking_bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="booking_bot",
)
