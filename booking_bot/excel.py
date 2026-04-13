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
        """Implemented in Stage D."""
        raise NotImplementedError("xls conversion added in Stage D")

    # ---- Resume iteration ----

    def pending_rows(self) -> Iterator[tuple[int, object]]:
        """Yield (row_idx, raw_col_B_value) for rows where col B is not None AND
        col C is empty/whitespace. Operates on the Output workbook, starting at
        min_row=1 (no header row)."""
        for row in self._ws.iter_rows(min_row=1, values_only=False):
            row_idx = row[0].row
            col_b = row[1].value if len(row) > 1 else None
            col_c = row[2].value if len(row) > 2 else None
            if col_b is None:
                continue
            if col_c is not None and str(col_c).strip() != "":
                continue
            yield (row_idx, col_b)

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
        # Find the next empty row in the Issues worksheet.
        next_row = 1
        while self._issues_ws.cell(row=next_row, column=1).value is not None:
            next_row += 1
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
