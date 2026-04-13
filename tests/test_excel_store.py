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
    """Point config paths at tmp_path and hand back the tmp_path."""
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


def test_pending_rows_skips_already_filled(store_env):
    """Rows where col C already has a value (code or 'ISSUE') are not yielded."""
    inp = _make_input(store_env, [
        ("C1", "9876543210"),
        ("C2", "9123456789"),
        ("C3", "9000000000"),
    ])
    # First run: create Output then manually mark row 2 as filled.
    store = ExcelStore(inp)
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
