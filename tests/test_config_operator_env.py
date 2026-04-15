"""Bot-child environment variable overrides for operator phone.

The orchestrator spawner passes BOOKING_BOT_OPERATOR_PHONE to each child
so cloned bots under different operator slots can re-auth with their own
operator phone during the quiet-retry mechanism. This test covers only
the config module's import-time behavior — the bot's actual auth path
is covered by auth.py tests."""
import importlib

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_module(monkeypatch):
    """Reset booking_bot.config module state around every test in this file.

    These tests mutate BOOKING_BOT_OPERATOR_PHONE and reload config to force
    the import-time env override to re-run. Without this fixture, the final
    reload in each test leaves config.OPERATOR_PHONE in whatever state the
    last test produced, and any downstream test that reads config in the
    same pytest session sees stale data."""
    monkeypatch.delenv("BOOKING_BOT_OPERATOR_PHONE", raising=False)
    from booking_bot import config
    importlib.reload(config)
    yield
    monkeypatch.delenv("BOOKING_BOT_OPERATOR_PHONE", raising=False)
    importlib.reload(config)


def test_operator_phone_env_override(monkeypatch):
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_PHONE", "9876543210")
    from booking_bot import config
    importlib.reload(config)
    assert config.OPERATOR_PHONE == "9876543210"


def test_operator_phone_env_empty_does_not_override(monkeypatch):
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_PHONE", "")
    from booking_bot import config
    importlib.reload(config)
    # Default remains the hardcoded one
    assert config.OPERATOR_PHONE == "9209114429"


def test_operator_phone_env_unset_does_not_override(monkeypatch):
    monkeypatch.delenv("BOOKING_BOT_OPERATOR_PHONE", raising=False)
    from booking_bot import config
    importlib.reload(config)
    assert config.OPERATOR_PHONE == "9209114429"


def test_operator_env_constants_exist():
    from booking_bot import config
    importlib.reload(config)
    assert config.OPERATOR_PHONE_ENV == "BOOKING_BOT_OPERATOR_PHONE"
    assert config.OPERATOR_SLOT_ENV == "BOOKING_BOT_OPERATOR_SLOT"
