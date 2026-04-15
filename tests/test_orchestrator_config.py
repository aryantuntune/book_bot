"""Smoke test for orchestrator config constants and new exceptions.
These are leaf additions to booking_bot/config.py and exceptions.py,
so a single import-and-check test catches typos and missing fields."""
from pathlib import Path

from booking_bot import config, exceptions


def test_runs_dir_is_under_root():
    assert config.RUNS_DIR == config.ROOT / "data" / "runs"


def test_chunks_dir_is_under_input():
    assert config.CHUNKS_DIR == config.ROOT / "Input" / "chunks"


def test_orchestrator_logs_dir_is_under_logs():
    assert config.ORCHESTRATOR_LOGS_DIR == config.ROOT / "logs" / "orchestrator"


def test_orchestrator_tuning_constants_have_sane_values():
    assert config.ORCHESTRATOR_AUTH_SEED_BUFFER_S == 7200
    assert config.ORCHESTRATOR_STALL_THRESHOLD_S == 600
    assert config.ORCHESTRATOR_IDLE_WARNING_S == 120
    assert config.ORCHESTRATOR_MAX_AUTO_RESTARTS == 3
    assert config.ORCHESTRATOR_KILL_TIMEOUT_S == 10.0
    assert config.ORCHESTRATOR_AUTH_TIMEOUT_S == 900


def test_auth_seed_buffer_leaves_headroom_before_auth_cooldown():
    assert config.ORCHESTRATOR_AUTH_SEED_BUFFER_S < config.AUTH_COOLDOWN_S


def test_new_exceptions_exist_and_subclass_booking_bot_error():
    assert issubclass(exceptions.AuthSeedTimeout, exceptions.BookingBotError)
    assert issubclass(exceptions.AuthCloneFailed, exceptions.BookingBotError)


def test_auth_clone_failed_carries_failure_list():
    exc = exceptions.AuthCloneFailed(failures=[("ASU-003", "disk full")])
    assert exc.failures == [("ASU-003", "disk full")]
    assert "ASU-003" in str(exc)
