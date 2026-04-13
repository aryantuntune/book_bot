"""Verify SUCCESS_RE matches real bot success messages and rejects near-misses
and strings containing stray 6-digit numbers. Strict no-false-positives."""
import pytest

from booking_bot.config import SUCCESS_RE


# --- Must match: return the code ---
POSITIVE_CASES = [
    (
        "Your HP Gas Refill has been successfully booked with reference "
        "number 1260669600118310 and your delivery confirmation code is 764260",
        "764260",
    ),
    (
        "Your delivery confirmation code is 000001",
        "000001",
    ),
    (
        "DELIVERY CONFIRMATION CODE IS 123456 please keep it safe",
        "123456",
    ),
    (
        "...your delivery   confirmation\ncode is\t999888",  # whitespace variants
        "999888",
    ),
]

# --- Must NOT match: would be a false positive ---
NEGATIVE_CASES = [
    "Your reference number is 1260669600118310. Have a nice day!",
    "delivery confirmation code: 764260",              # colon, no "is"
    "delivery confirmation code is 7642",              # only 4 digits
    "delivery confirmation code is 76426099",          # 8 digits — must not capture 764260
    "delivery code is 764260",                         # missing "confirmation"
    "Please enter your 10-digit Mobile number",
    "Booking failed. Please try again later.",
    "",
]


@pytest.mark.parametrize("text, expected_code", POSITIVE_CASES)
def test_success_re_matches(text, expected_code):
    m = SUCCESS_RE.search(text)
    assert m is not None, f"expected match in: {text!r}"
    assert m.group(1) == expected_code


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_success_re_rejects(text):
    assert SUCCESS_RE.search(text) is None, f"false positive in: {text!r}"
