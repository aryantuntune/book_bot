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


class ProfileInUseError(FatalError):
    """The Chromium user-data dir for this --profile-suffix is already owned
    by another live booking_bot instance (Chromium enforces single-writer on
    its profile). Surfaced as a loud, actionable error instead of the cryptic
    Playwright TargetClosedError that otherwise trips the auto-restart loop."""


class AuthSeedTimeout(BookingBotError):
    """orchestrator/auth_template.py: interactive auth seed poll loop hit
    ORCHESTRATOR_AUTH_TIMEOUT_S without seeing a fresh last_auth.json.
    Caller aborts `orchestrator start` with a clear operator message."""


class AuthCloneFailed(BookingBotError):
    """orchestrator/auth_template.py: one or more `shutil.copytree` calls
    raised while cloning the auth-seed profile to a chunk profile dir
    (disk full, permission denied, antivirus lock). Carries a list of
    (chunk_id, error_str) tuples so the CLI can print every failure."""

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        summary = ", ".join(f"{cid}: {err}" for cid, err in failures)
        super().__init__(f"profile clone failed for {len(failures)} chunks: {summary}")
