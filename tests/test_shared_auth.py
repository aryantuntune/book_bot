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


def _make_page_with_cookies(
    cookies: list[dict],
    local_storage: dict | None = None,
    session_storage: dict | None = None,
    origin: str = "https://hpchatbot.hpcl.co.in",
) -> MagicMock:
    """Build a mock Page whose .context.cookies() returns the given list
    and whose .evaluate() returns the given storage payloads, matching
    the shape produced by _dump_hpcl_storage."""
    ctx = MagicMock()
    ctx.cookies.return_value = cookies
    page = MagicMock()
    page.context = ctx
    page.evaluate.return_value = {
        "ls": local_storage or {},
        "ss": session_storage or {},
        "origin": origin,
    }
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


def test_read_rejects_all_surfaces_empty(tmp_root):
    """A payload with no cookies AND no localStorage AND no sessionStorage
    is useless — the transplant would be a no-op, so read_shared_auth_state
    returns None and the caller falls through to normal auth."""
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "cookies": [],
            "local_storage": {},
            "session_storage": {},
        })
    )
    assert browser.read_shared_auth_state() is None


def test_read_accepts_local_storage_only(tmp_root):
    """HPCL-style: no cookies, but a populated localStorage payload.
    This is the realistic case — HPCL stores its session token in
    localStorage, not as a Set-Cookie. The read must accept it."""
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "cookies": [],
            "local_storage": {"hpcl_session": "jwt-token-bytes"},
            "session_storage": {},
        })
    )
    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["local_storage"] == {"hpcl_session": "jwt-token-bytes"}
    assert payload["cookies"] == []


def test_read_accepts_session_storage_only(tmp_root):
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "cookies": [],
            "local_storage": {},
            "session_storage": {"ss_token": "xyz"},
        })
    )
    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["session_storage"] == {"ss_token": "xyz"}


def test_read_backwards_compat_with_old_cookie_only_format(tmp_root):
    """A shared_auth.json written by an older version of the bot only
    has 'cookies' with no local_storage/session_storage keys. Readers
    must normalize those to empty dicts and still return the payload."""
    (tmp_root / config.SHARED_AUTH_FILENAME).write_text(
        json.dumps({
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "cookies": [
                {"name": "s", "value": "v", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
            ],
        })
    )
    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["cookies"] and payload["cookies"][0]["name"] == "s"
    assert payload["local_storage"] == {}
    assert payload["session_storage"] == {}


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


def test_write_captures_local_storage_and_session_storage(tmp_root):
    """The killer test for the 'wrote shared_auth.json: 0 cookies' bug:
    HPCL stores its session in localStorage. write_shared_auth_state
    must dump localStorage + sessionStorage so the transplant is not
    a no-op even when there are zero cookies."""
    page = _make_page_with_cookies(
        cookies=[],
        local_storage={"hpcl_session_token": "eyJhbGc...", "user_id": "100"},
        session_storage={"csrf": "xyz"},
    )
    browser.write_shared_auth_state(page)

    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert payload["cookies"] == []
    assert payload["local_storage"] == {
        "hpcl_session_token": "eyJhbGc...",
        "user_id": "100",
    }
    assert payload["session_storage"] == {"csrf": "xyz"}


def test_write_zeroes_storage_on_non_hpcl_origin(tmp_root):
    """If the page is on about:blank or some redirect origin, any
    storage we read does not belong to HPCL and must not be stored.
    Cookies are still exported because they're already domain-filtered."""
    page = _make_page_with_cookies(
        cookies=[
            {"name": "s", "value": "v", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
        ],
        local_storage={"some": "other-origin-junk"},
        session_storage={},
        origin="about:blank",
    )
    browser.write_shared_auth_state(page)

    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert len(payload["cookies"]) == 1  # cookie still saved
    assert payload["local_storage"] == {}  # storage dropped
    assert payload["session_storage"] == {}


def test_write_survives_evaluate_exception(tmp_root):
    """If page.evaluate raises (frame detached, navigation in flight),
    the writer must still produce a valid cookies-only payload rather
    than crashing. The caller's successful auth is more important than
    a complete snapshot."""
    page = _make_page_with_cookies(
        cookies=[
            {"name": "s", "value": "v", "domain": ".hpchatbot.hpcl.co.in", "path": "/"},
        ],
    )
    page.evaluate.side_effect = RuntimeError("frame detached")
    browser.write_shared_auth_state(page)

    payload = browser.read_shared_auth_state()
    assert payload is not None
    assert len(payload["cookies"]) == 1
    assert payload["local_storage"] == {}
    assert payload["session_storage"] == {}
