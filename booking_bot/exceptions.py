"""Typed exception hierarchy. See spec §8.1."""


class BookingBotError(Exception):
    """Base class so callers can catch the whole hierarchy if they want."""


class GatewayError(BookingBotError):
    """502/503/504 or nginx error page observed on the chatbot domain."""


class ChatStuckError(BookingBotError):
    """wait_until_settled timed out (loader never cleared, or scroller never
    stabilized)."""


class IframeLostError(BookingBotError):
    """Inner chat frame detached or could not be found within the timeout."""


class AuthFailedError(BookingBotError):
    """Auth navigation lost — expected menu button not found."""


class OptionNotFoundError(BookingBotError):
    """click_option() could not match any of the requested label patterns
    against visible dynamic-message-button elements."""


class FatalError(BookingBotError):
    """Unrecoverable: the top-level cli loop writes an ISSUE row and exits."""
