"""TDD for the playbook parser's pure helpers. No browser, no IO."""
import pytest

from booking_bot.playbook import (
    Action,
    _choose_reset_target,
    classify_value,
    events_to_actions,
    split_playbook,
)


OP = "9209114429"


# ---- classify_value ----

@pytest.mark.parametrize("value,expected", [
    ("9209114429", "operator_phone"),       # exact match
    ("9876543210", "customer_phone"),       # 10 digits, not op
    ("123456", "otp"),                      # 6 digit
    ("1234", "otp"),                        # 4 digit boundary
    ("12345678", "otp"),                    # 8 digit upper boundary
    ("123456789", "literal"),               # 9 digit is out of OTP range
    ("Yes", "literal"),                     # non-numeric
    ("", "literal"),                        # empty
    ("  9209114429  ", "operator_phone"),   # trimmed
])
def test_classify_value(value, expected):
    assert classify_value(value, OP) == expected


# ---- events_to_actions ----

def test_events_to_actions_click_with_filled_input():
    """Submit-button click paired with a filled input becomes just a TYPE
    action — chat.send_text types+submits atomically at replay time, so the
    CLICK Submit would target a vanished button."""
    events = [
        {
            "kind": "click",
            "clicked": {"text": "Submit", "id": None, "cls": "btn btn-success submit"},
            "filledInputs": [
                {"id": "mobile", "name": "mobile", "placeholder": "e.g XXX", "value": OP}
            ],
            "visibleButtons": [],
        }
    ]
    actions = events_to_actions(events, OP)
    assert len(actions) == 1
    assert actions[0].kind == "type"
    assert actions[0].value_slot == "operator_phone"
    assert actions[0].input_name == "mobile"


def test_events_to_actions_non_submit_click_with_filled_input_kept():
    """A non-Submit click (e.g. a menu button) with a persistent filledInput
    keeps BOTH the TYPE and the CLICK — only Submit buttons are skipped."""
    events = [
        {
            "kind": "click",
            "clicked": {"text": "Main Menu", "id": "mm1", "cls": "btn dynamic-message-button"},
            "filledInputs": [
                {"id": "newmobile", "name": "newmobile", "value": "9876543210"}
            ],
        }
    ]
    actions = events_to_actions(events, OP)
    assert len(actions) == 2
    assert actions[0].kind == "type"
    assert actions[0].value_slot == "customer_phone"
    assert actions[1].kind == "click"
    assert actions[1].button_text == "Main Menu"


def test_events_to_actions_dedupes_persistent_filled_inputs():
    """If the same filledInput (same id, same value) appears across multiple
    clicks (the operator clicked a menu button that didn't clear the previous
    form), we emit the TYPE action only once. The first click is Submit which
    is skipped; the Main Menu click is kept."""
    events = [
        {
            "kind": "click",
            "clicked": {"text": "Submit", "id": None, "cls": "btn submit"},
            "filledInputs": [
                {"id": "mobile", "name": "mobile", "value": OP}
            ],
        },
        {
            "kind": "click",
            "clicked": {"text": "Main Menu", "id": "mm123"},
            "filledInputs": [
                {"id": "mobile", "name": "mobile", "value": OP}  # still there
            ],
        },
    ]
    actions = events_to_actions(events, OP)
    kinds = [(a.kind, a.value_slot if a.kind == "type" else a.button_text) for a in actions]
    assert kinds == [
        ("type", "operator_phone"),
        ("click", "Main Menu"),
    ]


def test_events_to_actions_distinct_values_in_same_input_both_emit():
    """Two different values in the same input element (same id/name) must
    BOTH produce TYPE actions. This is the multi-booking case: the operator
    books for customer A, then comes back to the same input and types
    customer B."""
    events = [
        {
            "kind": "click",
            "clicked": {"text": "Submit", "id": None, "cls": "btn submit"},
            "filledInputs": [{"id": "newmobile", "name": "newmobile", "value": "9226382081"}],
        },
        {
            "kind": "enter_key",
            "input": {"id": "newmobile", "name": "newmobile", "value": "7057274723"},
        },
    ]
    actions = events_to_actions(events, OP)
    types = [a for a in actions if a.kind == "type"]
    assert len(types) == 2
    assert types[0].value_slot == "customer_phone"
    assert types[1].value_slot == "customer_phone"


def test_events_to_actions_enter_key():
    events = [
        {
            "kind": "enter_key",
            "input": {"id": "otp", "name": "otp", "value": "654321"},
        }
    ]
    actions = events_to_actions(events, OP)
    assert len(actions) == 1
    assert actions[0].kind == "type"
    assert actions[0].value_slot == "otp"


def test_events_to_actions_skips_chat_msg_and_info():
    events = [
        {"kind": "info", "text": "recorder attached"},
        {"kind": "chat_msg", "direction": "in", "text": "Welcome to HPCL"},
        {"kind": "chat_msg", "direction": "out", "text": "9209114429"},
        {
            "kind": "click",
            "clicked": {"text": "OK", "id": None},
            "filledInputs": [],
        },
    ]
    actions = events_to_actions(events, OP)
    assert len(actions) == 1
    assert actions[0].kind == "click"
    assert actions[0].button_text == "OK"


def test_events_to_actions_full_booking_shape():
    """End-to-end shape: operator phone + OTP + 2 menu clicks + customer phone + Yes.
    Submit clicks paired with TYPEs are skipped (send_text submits atomically)."""
    events = [
        # 1. operator types 9209114429, clicks Submit
        {
            "kind": "click",
            "clicked": {"text": "Submit", "id": None, "cls": "btn submit"},
            "filledInputs": [{"id": "mobile", "name": "mobile", "value": OP}],
        },
        # 2. operator types OTP 555666, clicks Submit
        {
            "kind": "click",
            "clicked": {"text": "Submit", "id": None, "cls": "btn submit"},
            "filledInputs": [{"id": "otp", "name": "otp", "value": "555666"}],
        },
        # 3. clicks Main Menu (no filled input)
        {
            "kind": "click",
            "clicked": {"text": "Main Menu", "id": "mm1"},
            "filledInputs": [],
        },
        # 4. clicks Book Refill
        {
            "kind": "click",
            "clicked": {"text": "Book Refill Cylinder", "id": "br1"},
            "filledInputs": [],
        },
        # 5. types customer phone 9876543210, clicks Submit
        {
            "kind": "click",
            "clicked": {"text": "Submit", "id": None, "cls": "btn submit"},
            "filledInputs": [{"id": "mobile", "name": "mobile", "value": "9876543210"}],
        },
        # 6. clicks Yes
        {
            "kind": "click",
            "clicked": {"text": "Yes", "id": "y1"},
            "filledInputs": [],
        },
    ]
    actions = events_to_actions(events, OP)
    kinds = [(a.kind, a.value_slot if a.kind == "type" else a.button_text) for a in actions]
    assert kinds == [
        ("type", "operator_phone"),
        ("type", "otp"),
        ("click", "Main Menu"),
        ("click", "Book Refill Cylinder"),
        ("type", "customer_phone"),
        ("click", "Yes"),
    ]


# ---- split_playbook ----

def test_split_basic_single_customer_phone():
    """One customer_phone recorded: auth = everything before it,
    body = from it to the end."""
    actions = [
        Action(kind="type", value_slot="operator_phone"),
        Action(kind="type", value_slot="otp"),
        Action(kind="click", button_text="Main Menu"),
        Action(kind="type", value_slot="customer_phone"),
        Action(kind="click", button_text="Yes"),
    ]
    auth, body = split_playbook(actions)
    assert len(auth) == 3
    assert len(body) == 2
    assert body[0].kind == "type"
    assert body[0].value_slot == "customer_phone"
    assert body[-1].button_text == "Yes"


def test_split_multiple_customer_phones_extracts_loop():
    """Two customer_phones recorded: body is the slice between them
    (captures exactly one loop iteration), and trailing cleanup after the
    2nd customer_phone is dropped."""
    actions = [
        Action(kind="click", button_text="Booking Services"),
        Action(kind="click", button_text="Book for Others"),
        # --- first iteration ---
        Action(kind="type", value_slot="customer_phone"),  # 9226382081
        Action(kind="click", button_text="Yes"),
        Action(kind="click", button_text="Previous Menu"),
        Action(kind="click", button_text="Book for Others"),
        # --- second iteration starts ---
        Action(kind="type", value_slot="customer_phone"),  # 7057274723
        Action(kind="click", button_text="Yes"),
        Action(kind="click", button_text="Main Menu"),     # cleanup, dropped
    ]
    auth, body = split_playbook(actions)
    assert [a.button_text for a in auth] == ["Booking Services", "Book for Others"]
    assert body[0].kind == "type" and body[0].value_slot == "customer_phone"
    assert [
        (a.kind, a.value_slot if a.kind == "type" else a.button_text) for a in body
    ] == [
        ("type", "customer_phone"),
        ("click", "Yes"),
        ("click", "Previous Menu"),
        ("click", "Book for Others"),
    ]


def test_split_missing_customer_phone_raises():
    actions = [
        Action(kind="type", value_slot="operator_phone"),
        Action(kind="click", button_text="Submit"),
    ]
    with pytest.raises(ValueError, match="customer_phone"):
        split_playbook(actions)


# ---- _choose_reset_target ----
#
# Pure decision helper for reset_to_customer_entry. Takes the enabled-button
# list plus two escape-attempted flags and decides which button path the
# caller should take to get back to the customer-phone entry state.

def test_reset_target_book_with_other_mobile_wins():
    assert _choose_reset_target(
        enabled=["Book With Other Mobile", "Previous Menu"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "book_with_other_mobile"


def test_reset_target_book_for_others_direct():
    assert _choose_reset_target(
        enabled=["Book for Others", "Previous Menu"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "book_for_others"


def test_reset_target_booking_services_path():
    assert _choose_reset_target(
        enabled=["Booking Services", "Other Services"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "booking_services"


def test_reset_target_main_menu_path():
    assert _choose_reset_target(
        enabled=["Main Menu"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "main_menu"


def test_reset_target_no_escape_hatch():
    # Dangling Yes/No bubble after a 502 during a 'Yes' click.
    assert _choose_reset_target(
        enabled=["No"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "no_escape"


def test_reset_target_no_escape_not_retried():
    assert _choose_reset_target(
        enabled=["No"],
        escape_tried=True,
        prev_menu_tried=False,
    ) == "none"


def test_reset_target_previous_menu_escape_for_payment_pending():
    # HPCL presents a payment-pending dead-end dialog with only
    # 'Make Payment' and 'Previous Menu' enabled. Must click
    # Previous Menu to back out, not raise.
    assert _choose_reset_target(
        enabled=["Make Payment", "Previous Menu"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "previous_menu_escape"


def test_reset_target_previous_menu_escape_not_retried():
    assert _choose_reset_target(
        enabled=["Make Payment", "Previous Menu"],
        escape_tried=False,
        prev_menu_tried=True,
    ) == "none"


def test_reset_target_nav_buttons_beat_previous_menu():
    # If both a nav button and 'Previous Menu' are enabled, the nav
    # button takes precedence — Previous Menu is the last-resort escape.
    assert _choose_reset_target(
        enabled=["Book for Others", "Previous Menu"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "book_for_others"


def test_reset_target_empty_returns_none():
    assert _choose_reset_target(
        enabled=[],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "none"


def test_reset_target_case_insensitive():
    # Button labels from the DOM may arrive in any case.
    assert _choose_reset_target(
        enabled=["MAKE PAYMENT", "previous menu"],
        escape_tried=False,
        prev_menu_tried=False,
    ) == "previous_menu_escape"
