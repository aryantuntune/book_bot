"""PyInstaller runtime hook. Runs before any booking_bot code imports.

Responsibilities:
1. Point PLAYWRIGHT_BROWSERS_PATH at the bundled ms-playwright/ dir so the
   driver finds the embedded chromium and ffmpeg binaries.
2. Point PLAYWRIGHT_DRIVER_PATH at the bundled playwright/driver/ so the
   driver's node.exe launches correctly.

This file is only referenced by booking_bot.spec (runtime_hooks=) and is a
no-op when executed from a normal Python interpreter — importing it from
source has no effect because sys.frozen won't be set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    base = Path(getattr(sys, "_MEIPASS", "."))
    browsers_path = base / "ms-playwright"
    driver_path   = base / "playwright" / "driver"

    if browsers_path.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path))
    if driver_path.exists():
        os.environ.setdefault("PLAYWRIGHT_DRIVER_PATH", str(driver_path))
