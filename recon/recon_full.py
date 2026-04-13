"""
Recon part 3: full path through the parent site -> launcher -> chatbot iframe.
Goal: identify the launcher selector, the iframe chain, and the chatbot DOM
in its initial 'ask for mobile number' state.
"""
from playwright.sync_api import sync_playwright
from pathlib import Path
import re

OUT = Path(__file__).parent
URL = "https://myhpgas.in"


def block_fonts(route):
    if route.request.resource_type == "font":
        return route.abort()
    return route.continue_()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 850},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    )
    ctx.route("**/*", block_fonts)
    page = ctx.new_page()

    print(f"[1] navigating to {URL}")
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(4_000)
    print(f"    title={page.title()!r}")

    try:
        page.screenshot(path=str(OUT / "10_landing.png"), timeout=10_000)
    except Exception as e:
        print(f"    screenshot skipped: {type(e).__name__}")

    # Find the launcher: anything with onclick / role / image suggestive
    # of opening the chatbot. Look at images in the bottom-right corner.
    launcher_info = page.evaluate(
        """
        () => {
          const vw = window.innerWidth, vh = window.innerHeight;
          const els = Array.from(document.querySelectorAll('img,div,a,button'));
          const out = [];
          for (const el of els) {
            const r = el.getBoundingClientRect();
            // Bottom-right quadrant only.
            if (r.x < vw * 0.6 || r.y < vh * 0.5) continue;
            if (r.width === 0 || r.height === 0) continue;
            if (r.width > 250 || r.height > 250) continue;
            const id = el.id || '';
            const cls = el.getAttribute('class') || '';
            const src = el.getAttribute('src') || '';
            const onclick = el.getAttribute('onclick') || '';
            out.push({
              tag: el.tagName.toLowerCase(),
              id, cls, src, onclick,
              x: r.x|0, y: r.y|0, w: r.width|0, h: r.height|0,
            });
          }
          return out;
        }
        """
    )
    print(f"[2] {len(launcher_info)} bottom-right elements:")
    for item in launcher_info[:30]:
        print(f"    {item}")

    # Specifically inspect #cbotform (the offscreen chatbot wrapper).
    cbot = page.evaluate(
        """
        () => {
          const el = document.getElementById('cbotform');
          if (!el) return null;
          const cs = getComputedStyle(el);
          return {
            outerHTML: el.outerHTML.slice(0, 400),
            display: cs.display, visibility: cs.visibility,
            left: cs.left, right: cs.right, top: cs.top,
            transform: cs.transform,
            innerHTML_head: el.innerHTML.slice(0, 400),
          };
        }
        """
    )
    print(f"[3] #cbotform: {cbot}")

    # Find what triggers cbotform. Look for elements whose onclick references it.
    triggers = page.evaluate(
        """
        () => {
          const out = [];
          for (const el of document.querySelectorAll('[onclick]')) {
            const oc = el.getAttribute('onclick') || '';
            if (/cbotform|chatbot|openchat|openBot|togglechat/i.test(oc)) {
              const r = el.getBoundingClientRect();
              out.push({
                tag: el.tagName.toLowerCase(),
                id: el.id, cls: el.getAttribute('class'),
                onclick: oc.slice(0, 200),
                x: r.x|0, y: r.y|0,
              });
            }
          }
          return out;
        }
        """
    )
    print(f"[4] elements with chat-related onclick: {len(triggers)}")
    for t in triggers:
        print(f"    {t}")

    # Also dump a list of inline scripts that mention cbotform.
    scripts = page.evaluate(
        """
        () => {
          const out = [];
          for (const s of document.querySelectorAll('script')) {
            const txt = s.textContent || '';
            if (txt.includes('cbotform') || txt.toLowerCase().includes('chatbot')) {
              const i = txt.toLowerCase().indexOf('cbotform');
              const j = i >= 0 ? i : txt.toLowerCase().indexOf('chatbot');
              out.push(txt.slice(Math.max(0, j-80), j+220));
            }
          }
          return out.slice(0, 6);
        }
        """
    )
    print(f"[5] script snippets mentioning cbotform/chatbot: {len(scripts)}")
    for s in scripts:
        print(f"    --- {re.sub(chr(10),' ',s)[:300]}")

    browser.close()
print("done")
