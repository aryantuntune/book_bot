"""Record mode: open the HP Gas chatbot in a visible browser and log every
click, inline-form submission, and bot message to a JSONL file. Used to
discover the real menu wording and button labels so AUTH_NAV_SEQUENCE,
STATE_PATTERNS, and the book_one state machine can be configured against
real data instead of recon guesses.

Usage:
    python -m booking_bot.record

What gets recorded for each click:
  - clicked button (tag, text, id, class)
  - every OTHER button visible at that moment (so the alternatives are known)
  - every filled input/textarea on the page (so submitted phone/OTP values
    are captured)
  - the last 800 chars of #scroller text (what the bot was saying)

Bot-side messages (new <li> elements appended to ul.list-group.chat) are
captured via a MutationObserver and logged as chat_msg events with a
direction field (in/out) inferred from the <li>'s class.

Output: recordings/recording_YYYY-MM-DD_HH-MM-SS.jsonl — one JSON object
per line, written immediately so Ctrl-C or a browser crash never loses
data. Close the Chrome window or press Ctrl-C in the terminal to stop.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from booking_bot import config
from booking_bot.logging_setup import setup_logging

log = logging.getLogger("record")

RECORDINGS_DIR = config.ROOT / "recordings"


_INJECTED_JS = r"""
(function installRecorder() {
  if (window.__bbRecorderInstalled) return;
  window.__bbRecorderInstalled = true;

  const dumpButtons = () => Array.from(
    document.querySelectorAll('button, a.btn, .dynamic-message-button')
  )
    .filter(b => b.offsetParent !== null)
    .map(b => ({
      tag: b.tagName.toLowerCase(),
      text: (b.innerText || b.value || '').trim().slice(0, 160),
      id: b.id || null,
      cls: b.getAttribute('class'),
    }));

  const dumpInputs = () => Array.from(
    document.querySelectorAll('input, textarea')
  )
    .filter(i => i.offsetParent !== null && i.value)
    .map(i => ({
      tag: i.tagName.toLowerCase(),
      type: i.getAttribute('type'),
      id: i.id || null,
      name: i.getAttribute('name'),
      placeholder: i.getAttribute('placeholder'),
      cls: i.getAttribute('class'),
      value: i.value,
    }));

  const scrollerTail = () => {
    const s = document.querySelector('#scroller');
    return s ? (s.innerText || '').slice(-800) : '';
  };

  document.addEventListener('click', (ev) => {
    let t = ev.target;
    while (t && t !== document.body) {
      const isBtn = (
        t.tagName === 'BUTTON' ||
        t.tagName === 'A' ||
        (t.classList && (
          t.classList.contains('btn') ||
          t.classList.contains('dynamic-message-button')
        ))
      );
      if (isBtn) {
        try {
          window.pyRecord({
            kind: 'click',
            clicked: {
              tag: t.tagName.toLowerCase(),
              text: (t.innerText || t.value || '').trim().slice(0, 200),
              id: t.id || null,
              cls: t.getAttribute('class'),
            },
            visibleButtons: dumpButtons(),
            filledInputs: dumpInputs(),
            scrollerTail: scrollerTail(),
          });
        } catch (e) { /* swallow; JS must not break the real click */ }
        return;
      }
      t = t.parentElement;
    }
  }, true);

  // Capture Enter-key submissions on inline form inputs (many users hit
  // Enter instead of clicking Submit).
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    const el = ev.target;
    if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return;
    try {
      window.pyRecord({
        kind: 'enter_key',
        input: {
          tag: el.tagName.toLowerCase(),
          id: el.id || null,
          name: el.getAttribute('name'),
          placeholder: el.getAttribute('placeholder'),
          cls: el.getAttribute('class'),
          value: el.value,
        },
        scrollerTail: scrollerTail(),
      });
    } catch (e) { /* swallow */ }
  }, true);

  // Watch ul.list-group.chat for new <li> additions — these are the bot's
  // replies (and echo of our own messages). Retry until the chat ul exists.
  const tryAttach = () => {
    const ul = document.querySelector('ul.list-group.chat');
    if (!ul) { setTimeout(tryAttach, 500); return; }
    try {
      window.pyRecord({kind: 'info', text: 'recorder attached to chat ul'});
    } catch (e) {}
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== 1) continue;
          const cls = (node.getAttribute && node.getAttribute('class')) || '';
          const text = (node.innerText || '').trim();
          if (!text) continue;
          try {
            window.pyRecord({
              kind: 'chat_msg',
              direction: (cls.match(/sent|right|out|user/i)) ? 'out' : 'in',
              text: text.slice(0, 1200),
              cls,
            });
          } catch (e) {}
        }
      }
    }).observe(ul, {childList: true});
  };
  tryAttach();
})();
"""


def main() -> None:
    log_path = setup_logging(debug=False)
    log.info(f"log file: {log_path}")

    RECORDINGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = RECORDINGS_DIR / f"recording_{ts}.jsonl"
    event_count = 0

    # Seed file with a header so the operator knows which recording is which.
    with out_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "header",
            "started": ts,
            "url": config.URL,
        }, ensure_ascii=False) + "\n")

    def on_event(source, data: dict) -> None:
        nonlocal event_count
        event_count += 1
        event = {"t": round(time.time(), 3), **data}
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

        # Also log a human-friendly summary to the console.
        kind = event.get("kind", "?")
        if kind == "click":
            clicked = event.get("clicked", {})
            log.info(
                f"CLICK #{event_count}: {clicked.get('text')!r} "
                f"(id={clicked.get('id')}, cls={clicked.get('cls')})"
            )
            filled = event.get("filledInputs") or []
            if filled:
                for fi in filled:
                    log.info(
                        f"    filled: {fi.get('name') or fi.get('id') or fi.get('placeholder')}"
                        f" = {fi.get('value')!r}"
                    )
        elif kind == "enter_key":
            inp = event.get("input", {})
            log.info(
                f"ENTER #{event_count}: "
                f"{inp.get('name') or inp.get('id')} = {inp.get('value')!r}"
            )
        elif kind == "chat_msg":
            text = (event.get("text") or "").replace("\n", " ")[:120]
            log.info(f"MSG {event.get('direction', '?')}: {text}")
        elif kind == "info":
            log.info(f"[info] {event.get('text')}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 850},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # Order matters: expose_binding and add_init_script must be set up
        # BEFORE navigation so the injected JS finds window.pyRecord already
        # present when it runs.
        page.expose_binding("pyRecord", on_event)
        page.add_init_script(_INJECTED_JS)

        log.info(f"navigating to {config.URL}")
        page.goto(config.URL, wait_until="domcontentloaded", timeout=60_000)

        print()
        print("=" * 64)
        print("  RECORD MODE ACTIVE")
        print("=" * 64)
        print(f"  Output file: {out_path}")
        print()
        print("  1. A Chrome window has opened with the HP Gas chatbot.")
        print("  2. Complete a full booking manually — type your phone,")
        print("     the OTP, click through every menu, book for one")
        print("     customer, and wait until you see the 6-digit delivery")
        print("     confirmation code.")
        print("  3. When done, CLOSE THE CHROME WINDOW (not this terminal).")
        print("     Recording stops automatically when the tab closes.")
        print()
        print("  Press Ctrl-C here to stop early. Events are saved as they")
        print("  happen — nothing is lost on exit.")
        print("=" * 64)
        print(flush=True)

        try:
            # Block the main thread in Playwright so its event loop keeps
            # running and dispatches exposed-binding callbacks. Page.close
            # fires when the tab is closed by the operator.
            page.wait_for_event("close", timeout=0)
            log.info("page closed by operator")
        except KeyboardInterrupt:
            log.info("Ctrl-C received; stopping recording")
        except PWTimeoutError:
            log.info("wait_for_event timed out (shouldn't happen with timeout=0)")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    log.info(f"recording saved: {out_path}")
    log.info(f"total events captured: {event_count}")
    print()
    print("=" * 64)
    print(f"  Recording complete: {event_count} events")
    print(f"  File: {out_path}")
    print(f"  Paste this file's contents back to continue tuning.")
    print("=" * 64)


if __name__ == "__main__":
    main()
