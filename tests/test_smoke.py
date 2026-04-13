"""Tier-2 smoke: load myhpgas.in, drill the iframes, settle the chat, and
assert the initial welcome text. Canary against HP Gas changing the chatbot.

This test hits the real live site. Skipped if the BOOKING_BOT_SMOKE env var
is not set — keeps the default pytest run fully offline."""
import os

import pytest

from booking_bot import browser, chat

pytestmark = pytest.mark.skipif(
    os.environ.get("BOOKING_BOT_SMOKE") != "1",
    reason="set BOOKING_BOT_SMOKE=1 to enable live smoke test",
)


def test_welcome_text_visible():
    pw = brw = ctx = page = None
    try:
        pw, brw, ctx, page = browser.start_browser()
        frame = browser.get_chat_frame(page)
        chat.wait_until_settled(frame)
        snap = chat._scroller_snapshot(frame)
        assert "10-digit" in snap.text or "Mobile number" in snap.text, (
            f"unexpected welcome text: {snap.text[:200]!r}"
        )
    finally:
        if brw is not None:
            brw.close()
        if pw is not None:
            pw.stop()
