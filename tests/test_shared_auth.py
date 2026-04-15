"""Tests for the shared_auth.json cross-instance cookie transplant.

Covers read_shared_auth_state and write_shared_auth_state — the pure
file-I/O helpers that let parallel instances share a single operator OTP.
inject_shared_auth_cookies and the write hook in auth.py rely on a live
Playwright BrowserContext / Page and are exercised end-to-end by smoke
tests, not here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booking_bot import browser, config


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """Redirect config.ROOT at tmp_path so each test gets a fresh
    shared_auth.json location that won't touch the real repo root."""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path


def _make_page_with_cookies(cookies: list[dict]) -> MagicMock:
    """Build a mock Page whose .context.cookies() returns the given list."""
    ctx = MagicMock()
    ctx.cookies.return_value = cookies
    page = MagicMock()
    page.context = ctx
    return page


def test_read_returns_none_when_file_missing(tmp_root):
    assert browser.read_shared_auth_state() is None


def test_write_then_read_round_trip(tmp_root):
    cookies = [
        {
            "name": "sessionid",
            "value": "abc123",
            "domain": ".hpchatbot.hpcl.co.in",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }
    ]
    page = _make_page_with_cookies(cookies)
    browser.write_shared_auth_state(page)

    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["cookies"] == cookies
    assert "written_at_utc" in payload


def test_write_filters_non_hpcl_cookies(tmp_root):
    cookies = [
        {"name": "sessionid", "value": "x", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
        {"name": "ga", "value": "y", "domain": ".google-analytics.com", "path": "/"},
        {"name": "foo", "value": "z", "domain": ".otherunrelated.com", "path": "/"},
    ]
    page = _make_page_with_cookies(cookies)
    browser.write_shared_auth_state(page)

    payload = browser.read_shared_auth_state()
    assert payload is not None
    names = [c["name"] for c in payload["cookies"]]
    assert "sessionid" in names
    assert "ga" not in names
    assert "foo" not in names


def test_read_rejects_file_older_than_max_age(tmp_root):
    old_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=config.SHARED_AUTH_MAX_AGE_S + 60)
    ).isoformat()
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": old_ts,
            "cookies": [{"name": "x", "value": "y", "domain": ".hpchatbot.hpcl.co.in", "path": "/"}],
        })
    )
    assert browser.read_shared_auth_state() is None


def test_read_rejects_future_dated_file(tmp_root):
    """A future timestamp means clock skew or tampering — treat as invalid
    rather than trust it indefinitely."""
    future_ts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": future_ts,
            "cookies": [{"name": "x", "value": "y", "domain": ".hpchatbot.hpcl.co.in", "path": "/"}],
        })
    )
    assert browser.read_shared_auth_state() is None


def test_read_rejects_corrupt_json(tmp_root):
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text("{ not: valid json ]]")
    assert browser.read_shared_auth_state() is None


def test_read_rejects_missing_timestamp(tmp_root):
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({"cookies": [{"name": "x", "value": "y"}]})
    )
    assert browser.read_shared_auth_state() is None


def test_read_rejects_empty_cookie_list(tmp_root):
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "cookies": [],
        })
    )
    assert browser.read_shared_auth_state() is None


def test_write_survives_playwright_cookies_exception(tmp_root):
    """write_shared_auth_state must never raise — a failed snapshot
    shouldn't kill the run that just successfully auth'd."""
    page = MagicMock()
    page.context.cookies.side_effect = RuntimeError("playwright went away")
    browser.write_shared_auth_state(page)  # must not raise
    # And no file written.
    assert not (tmp_root / config.SHARED_AUTH_FILENAME).exists()


def test_write_atomic_tmp_replaced(tmp_root):
    """The write is done via <name>.tmp + os.replace. After a successful
    write there should be no .tmp file left behind."""
    page = _make_page_with_cookies([
        {"name": "s", "value": "v", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
    ])
    browser.write_shared_auth_state(page)
    assert (tmp_root / config.SHARED_AUTH_FILENAME).exists()
    assert not (tmp_root / (config.SHARED_AUTH_FILENAME + ".tmp")).exists()


def test_write_overwrites_previous_snapshot(tmp_root):
    page1 = _make_page_with_cookies([
        {"name": "first", "value": "v1", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
    ])
    browser.write_shared_auth_state(page1)

    page2 = _make_page_with_cookies([
        {"name": "second", "value": "v2", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
    ])
    browser.write_shared_auth_state(page2)

    payload = browser.read_shared_auth_state()
    assert payload is not None
    names = [c["name"] for c in payload["cookies"]]
    assert names == ["second"]


def test_shared_auth_path_without_slot_env(tmp_root, monkeypatch):
    """Bare bot mode: env var unset → legacy shared_auth.json."""
    monkeypatch.delenv("BOOKING_BOT_OPERATOR_SLOT", raising=False)
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"


def test_shared_auth_path_with_slot_env(tmp_root, monkeypatch):
    """Orchestrator mode: env var set → per-slot shared_auth-opN.json."""
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op2")
    assert browser._shared_auth_path() == tmp_root / "shared_auth-op2.json"


def test_shared_auth_path_invalid_slot_falls_back_to_default(
    tmp_root, monkeypatch,
):
    """Defensive: a malformed slot value (path traversal attempt,
    whitespace, etc.) falls back to the legacy path rather than writing
    to an attacker-chosen location."""
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "../evil")
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op")
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op1 ")
    assert browser._shared_auth_path() == tmp_root / "shared_auth.json"


def test_write_then_read_round_trip_with_slot(tmp_root, monkeypatch):
    """Full write/read cycle with a slot set — proves the per-slot file
    is actually used end-to-end."""
    monkeypatch.setenv("BOOKING_BOT_OPERATOR_SLOT", "op3")
    cookies = [
        {
            "name": "sessionid", "value": "xyz",
            "domain": ".hpchatbot.hpcl.co.in", "path": "/",
        }
    ]
    page = _make_page_with_cookies(cookies)
    browser.write_shared_auth_state(page)
    assert (tmp_root / "shared_auth-op3.json").exists()
    assert not (tmp_root / "shared_auth.json").exists()
    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["cookies"] == cookies
