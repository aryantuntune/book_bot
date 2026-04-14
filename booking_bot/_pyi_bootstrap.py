"""PyInstaller runtime hook. Runs before any booking_bot code imports.

Responsibilities:

1. Allocate / attach a Windows console when running the frozen .exe with
   ``console=False``. Double-clicked (no parent terminal): AllocConsole()
   pops a new console window so the operator sees live logs. Launched from
   cmd.exe / powershell: AttachConsole(-1) attaches to the parent terminal.
   ``--headless`` in argv: skip console setup entirely so the bot runs
   fully in the background with no visible window.

2. Point PLAYWRIGHT_BROWSERS_PATH at the bundled ms-playwright/ so the
   driver finds the embedded chromium-1134 and ffmpeg-1010. Also point
   PLAYWRIGHT_DRIVER_PATH at the bundled playwright/driver/ so the driver's
   node.exe launches correctly. Bundled Chromium is used unconditionally
   in the frozen .exe — it's the only browser we can guarantee on a
   client's machine AND its persistent user-data dir actually survives
   restarts, unlike system Chrome which kept dropping the HPCL session
   and triggering OTP re-prompts.

This file is only referenced by booking_bot.spec (runtime_hooks=) and is a
no-op when executed from a normal Python interpreter.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # ---- Console setup (Windows, console=False build) ----
    if sys.platform == "win32" and "--headless" not in sys.argv:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            ATTACH_PARENT_PROCESS = -1
            # Try to attach to the parent terminal first (launched from cmd);
            # if that fails (ERROR_INVALID_HANDLE = 6, no parent console),
            # allocate a fresh console window for the double-click case.
            if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
                kernel32.AllocConsole()

            # Redirect Python stdio to the newly attached/allocated console.
            # PyInstaller's console=False build starts with stdout/stderr/stdin
            # pointing at NUL, so we have to re-open CONOUT$/CONIN$ explicitly.
            try:
                sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8")
            except OSError:
                pass
            try:
                sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8")
            except OSError:
                pass
            try:
                sys.stdin = open("CONIN$", "r", encoding="utf-8")
            except OSError:
                pass
        except Exception:
            # Console allocation is cosmetic — never let it kill startup.
            pass

    # ---- Playwright browsers + driver paths ----
    base = Path(getattr(sys, "_MEIPASS", "."))
    browsers_path = base / "ms-playwright"
    driver_path   = base / "playwright" / "driver"
    if browsers_path.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path))
    if driver_path.exists():
        os.environ.setdefault("PLAYWRIGHT_DRIVER_PATH", str(driver_path))
