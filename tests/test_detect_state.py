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
        empty_input_names=["newmobile"],
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
        empty_input_names=["newmobile"],
    )
    assert state == "READY_FOR_CUSTOMER"


def test_resolve_real_auth_state_still_detected():
    """When the auth prompt IS the latest bubble (real auth state),
    NEEDS_OPERATOR_AUTH must still be returned."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="Please enter your 10 digit mobile number",
        recent_text="Please enter your 10 digit mobile number",
        empty_input_names=["mobile"],
    )
    assert state == "NEEDS_OPERATOR_AUTH"


def test_resolve_real_otp_state_still_detected():
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="OTP sent to your registered mobile, please enter otp",
        recent_text="OTP sent to your registered mobile, please enter otp",
        empty_input_names=["otp"],
    )
    assert state == "NEEDS_OPERATOR_OTP"


def test_resolve_buttons_take_top_priority():
    """Enabled menu buttons always win over text-based detection."""
    state = _resolve_state(
        enabled_buttons=["Book for Others", "Book for Self"],
        last_bubble_text="Please enter your 10 digit mobile number",
        recent_text="anything",
        empty_input_names=["newmobile"],
    )
    assert state == "BOOK_FOR_OTHERS_MENU"


def test_resolve_customer_input_present_falls_back_to_ready_for_customer():
    """When no buttons match and no auth text in latest bubble, a
    'newmobile' input means HPCL is waiting for the customer phone."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="something else entirely",
        recent_text="",
        empty_input_names=["newmobile"],
    )
    assert state == "READY_FOR_CUSTOMER"


def test_resolve_unknown_when_nothing_matches():
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="totally unrelated bubble",
        recent_text="totally unrelated bubble",
        empty_input_names=[],
    )
    assert state == "UNKNOWN"


# ---- CRITICAL: operator-auth input must never be misclassified as
# READY_FOR_CUSTOMER. Prior bug: after a reload that landed on HPCL's
# operator-phone-entry screen, detect_state saw an empty input and
# returned READY_FOR_CUSTOMER without checking WHICH input it was.
# The bot then typed actual customer phone numbers into the operator
# auth field, triggering real OTP SMS to those customers. ----

def test_resolve_operator_mobile_input_is_needs_operator_auth():
    """An empty 'mobile' input (HPCL's operator phone entry field) must
    be classified as NEEDS_OPERATOR_AUTH even when the last bubble text
    is silent on auth — the presence of the input itself is the signal."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="",
        recent_text="",
        empty_input_names=["mobile"],
    )
    assert state == "NEEDS_OPERATOR_AUTH"


def test_resolve_operator_mobile_input_wins_over_silent_last_bubble():
    """Even with a menu-like recent_text in scrollback, an empty 'mobile'
    input still classifies as NEEDS_OPERATOR_AUTH. The bot must NOT type
    customer phones into this field."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="",
        recent_text="book for others\nsome menu\n",
        empty_input_names=["mobile"],
    )
    assert state == "NEEDS_OPERATOR_AUTH"


def test_resolve_otp_input_is_needs_operator_otp():
    """An empty 'otp' input (HPCL's OTP entry field) must be classified
    as NEEDS_OPERATOR_OTP — never READY_FOR_CUSTOMER."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="",
        recent_text="",
        empty_input_names=["otp"],
    )
    assert state == "NEEDS_OPERATOR_OTP"


def test_resolve_unknown_input_name_does_not_claim_ready_for_customer():
    """If the empty input has an UNKNOWN name (neither newmobile nor
    mobile/otp), we must NOT return READY_FOR_CUSTOMER — fall through to
    text classification or UNKNOWN. Typing a customer phone into an
    unknown field is the exact bug this prevents."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="totally unrelated",
        recent_text="totally unrelated",
        empty_input_names=["some_new_hpcl_field"],
    )
    assert state == "UNKNOWN"


def test_resolve_mobile_input_beats_stale_customer_text_in_scrollback():
    """After a reload onto the operator-auth screen, the scrollback
    may still contain 'customer mobile' text from before the reload.
    The empty 'mobile' input must still win — NEEDS_OPERATOR_AUTH."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="Please enter your mobile number",
        recent_text=(
            "please enter the mobile number of the customer\n"
            "Please enter your mobile number"
        ),
        empty_input_names=["mobile"],
    )
    assert state == "NEEDS_OPERATOR_AUTH"


def test_resolve_customer_and_operator_inputs_both_present_prefers_operator():
    """If HPCL momentarily renders both inputs (legacy + new), the
    operator-auth input is the more dangerous one to ignore — always
    classify as NEEDS_OPERATOR_AUTH so the bot refuses to type customer
    data into the wrong field."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="",
        recent_text="",
        empty_input_names=["newmobile", "mobile"],
    )
    assert state == "NEEDS_OPERATOR_AUTH"


def test_resolve_case_insensitive_input_names():
    """Input name matching must be case-insensitive — HPCL could ship
    'Mobile' or 'MOBILE' in a future markup change."""
    state = _resolve_state(
        enabled_buttons=[],
        last_bubble_text="",
        recent_text="",
        empty_input_names=["Mobile"],
    )
    assert state == "NEEDS_OPERATOR_AUTH"
