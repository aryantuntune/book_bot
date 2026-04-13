"""Test phone number coercion. Accepts str / int / float / None from Excel
cells; returns (cleaned_phone, error_or_None)."""
import pytest

from booking_bot.cli import normalize_phone


VALID_CASES = [
    ("9876543210",   "9876543210"),
    (9876543210,     "9876543210"),
    (9876543210.0,   "9876543210"),
    ("+919876543210","9876543210"),
    ("919876543210", "9876543210"),
    ("  9876543210 ","9876543210"),
    ("98765-43210",  "9876543210"),
    ("(987) 654-3210","9876543210"),
]

INVALID_CASES = [
    "",
    "12345",
    "98765432100",              # 11 digits
    "abc",
    None,
    9876543210.5,               # fractional
    ["9876543210"],             # wrong type
]


@pytest.mark.parametrize("raw, cleaned", VALID_CASES)
def test_normalize_phone_accepts(raw, cleaned):
    out, err = normalize_phone(raw)
    assert err is None
    assert out == cleaned


@pytest.mark.parametrize("raw", INVALID_CASES)
def test_normalize_phone_rejects(raw):
    out, err = normalize_phone(raw)
    assert err == "invalid_phone_format"
    assert out == ""
