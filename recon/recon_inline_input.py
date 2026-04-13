"""Probe the live chatbot for the INLINE input field the operator phone
must be typed into (not the bottom textarea.replybox, which is a generic
chat box that triggers 'I am still learning' fallbacks).

Prints all inputs, all buttons, and the last <li> message's inner HTML.
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = (
    "https://hpchatbot.hpcl.co.in/pwa/view?data="
    "eyJlSWQiOjEwMCwiZ2xpIjp0cnVlLCJjYW1wYWlnbklkIjoiNjQ1MjAyZTNhMTdlMTZhY2RlOTNhMjhmIiwibGkiOiI4OWJiNzZlYTZlNmY0OTVjOTAwNTc3M2I1MGEzNDMyMSJ9"
)

OUT = Path(__file__).parent / "inline_input_probe.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 420, "height": 760})
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6_000)  # let the first bot message render

    info = page.evaluate(
        """
        () => {
          const describe = (el) => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {
              tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type'),
              id: el.id || null,
              name: el.getAttribute('name'),
              placeholder: el.getAttribute('placeholder'),
              cls: el.getAttribute('class'),
              visible: el.offsetParent !== null,
              disabled: el.hasAttribute('disabled'),
              readonly: el.hasAttribute('readonly'),
              x: r.x | 0, y: r.y | 0, w: r.width | 0, h: r.height | 0,
              value: el.value,
            };
          };

          const inputs = Array.from(
            document.querySelectorAll('input, textarea')
          ).map(describe);

          const buttons = Array.from(
            document.querySelectorAll('button, a.btn, .process-button button, input[type="submit"]')
          ).map((b) => {
            const r = b.getBoundingClientRect();
            return {
              tag: b.tagName.toLowerCase(),
              text: (b.innerText || b.value || '').trim().slice(0, 80),
              id: b.id || null,
              cls: b.getAttribute('class'),
              type: b.getAttribute('type'),
              visible: b.offsetParent !== null,
              x: r.x | 0, y: r.y | 0, w: r.width | 0, h: r.height | 0,
            };
          });

          // Inspect the last chat <li> — the bubble asking for mobile number.
          const lis = Array.from(document.querySelectorAll('ul.list-group.chat > li'));
          const lastLi = lis[lis.length - 1];
          const lastLiDetail = lastLi ? {
            cls: lastLi.getAttribute('class'),
            text: (lastLi.innerText || '').trim().slice(0, 400),
            innerHTML: lastLi.innerHTML.slice(0, 3000),
            childTags: Array.from(lastLi.querySelectorAll('*')).map(e => ({
              tag: e.tagName.toLowerCase(),
              cls: e.getAttribute('class'),
              id: e.id || null,
              type: e.getAttribute('type'),
              placeholder: e.getAttribute('placeholder'),
              text: e.tagName.toLowerCase() === 'input' || e.tagName.toLowerCase() === 'textarea'
                ? null
                : (e.innerText || '').trim().slice(0, 60),
            })).slice(0, 50),
          } : null;

          // Specifically look for any input/button INSIDE the chat <li>s.
          const formsInChat = [];
          for (const li of lis) {
            const ins = li.querySelectorAll('input, textarea');
            const btns = li.querySelectorAll('button, a.btn, input[type="submit"]');
            if (ins.length || btns.length) {
              formsInChat.push({
                liCls: li.getAttribute('class'),
                inputs: Array.from(ins).map(describe),
                buttons: Array.from(btns).map(b => ({
                  tag: b.tagName.toLowerCase(),
                  text: (b.innerText || b.value || '').trim().slice(0, 80),
                  id: b.id || null,
                  cls: b.getAttribute('class'),
                  type: b.getAttribute('type'),
                })),
              });
            }
          }

          return {
            title: document.title,
            inputCount: inputs.length,
            inputs,
            buttonCount: buttons.length,
            buttons,
            liCount: lis.length,
            lastLi: lastLiDetail,
            formsInChat,
          };
        }
        """
    )

    OUT.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"inputCount={info['inputCount']}  buttonCount={info['buttonCount']}  liCount={info['liCount']}")
    print(f"formsInChat={len(info['formsInChat'])}")
    for f in info["formsInChat"]:
        print(f"  liCls={f['liCls']!r}")
        for i in f["inputs"]:
            print(f"    input: {i}")
        for b in f["buttons"]:
            print(f"    button: {b}")
    print()
    print("=== all inputs ===")
    for i in info["inputs"]:
        print(f"  {i}")
    print()
    print("=== all buttons ===")
    for b in info["buttons"]:
        print(f"  {b}")
    print()
    print("=== last li snippet ===")
    if info["lastLi"]:
        print(f"  cls={info['lastLi']['cls']}")
        print(f"  text={info['lastLi']['text'][:300]!r}")
        print(f"  innerHTML[:800]={info['lastLi']['innerHTML'][:800]}")

    browser.close()
