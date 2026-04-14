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


class RestartableFatalError(FatalError):
    """Circuit-breaker fatal that cli.main() handles by closing the browser,
    waiting briefly, and relaunching from scratch. The persistent profile dir
    retains the HPCL session cookies, so a relaunch usually lands on a live
    session — mimicking the operator's manual 'stop and restart the bot'
    workflow, which they historically used to escape the OTP-prompt loop."""


class ChromeNotInstalledError(FatalError):
    """The shareable .exe tried to launch via channel="chrome" but the target
    machine has no Google Chrome install. Carries a human-readable install
    link that the GUI bootstrap shows directly to the operator."""
