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
