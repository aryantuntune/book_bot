"""detect_state is split into _classify_state (pure function of button labels
+ scroller text, trivially testable) and the thin Frame wrapper. We TDD the
pure helper with canned inputs that mirror what the live DOM would provide."""
import pytest

from booking_bot.chat import _classify_state


@pytest.mark.parametrize("buttons, scroller, expected", [
    (["Book for others", "Cancel"], "some prior bot text", "BOOK_FOR_OTHERS_MENU"),
    (["Booking Services", "Complaints"], "welcome to hpcl", "MAIN_MENU"),
    ([], "please enter the mobile number of the customer", "READY_FOR_CUSTOMER"),
    ([], "Customer Mobile Number:", "READY_FOR_CUSTOMER"),
    ([], "OTP sent to your registered mobile", "NEEDS_OPERATOR_OTP"),
    ([], "please enter otp", "NEEDS_OPERATOR_OTP"),
    ([], "Please enter your 10-digit Mobile number", "NEEDS_OPERATOR_AUTH"),
    (["Foo", "Bar"], "random text", "UNKNOWN"),
    ([], "", "UNKNOWN"),
])
def test_classify_state(buttons, scroller, expected):
    assert _classify_state(buttons, scroller) == expected


def test_button_takes_priority_over_text():
    """A 'Book for others' button wins even if the scroller text also matches
    a main-menu pattern."""
    assert _classify_state(
        ["Book for others"],
        "Please select one of: Booking Services, Complaints",
    ) == "BOOK_FOR_OTHERS_MENU"
