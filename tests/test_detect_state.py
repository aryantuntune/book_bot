"""detect_state is split into _classify_state (pure function of button labels
+ scroller text, trivially testable) and the thin Frame wrapper. We TDD the
pure helpers with canned inputs that mirror what the live DOM would provide."""
import pytest

from booking_bot.chat import _classify_state, _resolve_state


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


# ---- _resolve_state tests (priority pipeline) ----

def test_resolve_stale_auth_text_in_scrollback_does_not_trigger_auth():
    """Regression: after a fresh OTP login, the auth prompt bubble is still
    in the recent scrollback but the LATEST bubble is the customer-phone
    prompt with an empty input. detect_state must return READY_FOR_CUSTOMER,
    not NEEDS_OPERATOR_AUTH."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="Please enter customer's 10 digit mobile number",
        recent_text=(
            "Please enter your 10 digit mobile number\n"
            "OTP sent to your registered mobile\n"
            "Please enter customer's 10 digit mobile number"
        ),
        has_empty_input=True,
    )
    assert state == "READY_FOR_CUSTOMER"


def test_resolve_stale_otp_text_in_scrollback_does_not_trigger_otp():
    """Same idea for OTP: stale OTP bubble in scrollback must not
    re-trigger NEEDS_OPERATOR_OTP after we've moved past it."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="please enter customer mobile",
        recent_text=(
            "OTP sent to your registered mobile\n"
            "please enter customer mobile"
        ),
        has_empty_input=True,
    )
    assert state == "READY_FOR_CUSTOMER"


def test_resolve_real_auth_state_still_detected():
    """When the auth prompt IS the latest bubble (real auth state),
    NEEDS_OPERATOR_AUTH must still be returned."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="Please enter your 10 digit mobile number",
        recent_text="Please enter your 10 digit mobile number",
        has_empty_input=True,
    )
    assert state == "NEEDS_OPERATOR_AUTH"


def test_resolve_real_otp_state_still_detected():
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="OTP sent to your registered mobile, please enter otp",
        recent_text="OTP sent to your registered mobile, please enter otp",
        has_empty_input=True,
    )
    assert state == "NEEDS_OPERATOR_OTP"


def test_resolve_buttons_take_top_priority():
    """Enabled menu buttons always win over text-based detection."""
    state = _resolve_state(
        enabled_buttons=["Book for Others", "Book for Self"],
        last_bubble_text="Please enter your 10 digit mobile number",
        recent_text="anything",
        has_empty_input=True,
    )
    assert state == "BOOK_FOR_OTHERS_MENU"


def test_resolve_empty_input_falls_back_to_ready_for_customer():
    """When no buttons match and no auth text in latest bubble, an empty
    input means HPCL is waiting for the customer phone."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="something else entirely",
        recent_text="",
        has_empty_input=True,
    )
    assert state == "READY_FOR_CUSTOMER"


def test_resolve_unknown_when_nothing_matches():
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="totally unrelated bubble",
        recent_text="totally unrelated bubble",
        has_empty_input=False,
    )
    assert state == "UNKNOWN"
