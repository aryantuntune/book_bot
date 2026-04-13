"""
Recon: open myhpgas.in, find the chatbot launcher, open it, capture the
welcome screen + DOM. No auth attempted.
"""
from playwright.sync_api import sync_playwright
from pathlib import Path

OUT = Path(__file__).parent
URL = "https://myhpgas.in"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 850},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    )
    page = ctx.new_page()

    print(f"[1] navigating to {URL}")
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(4_000)  # let JS settle; networkidle never fires (long-poll)

    print(f"[2] title={page.title()!r}  url={page.url}")
    try:
        page.screenshot(path=str(OUT / "01_landing.png"), full_page=False, timeout=10_000)
        print("    saved viewport screenshot")
    except Exception as e:
        print(f"    screenshot failed (continuing): {e}")

    # Look for things that smell like a chatbot launcher.
    candidates = page.evaluate(
        """
        () => {
          const out = [];
          const all = document.querySelectorAll('*');
          for (const el of all) {
            const id = el.id || '';
            const cls = (el.getAttribute('class') || '');
            const tag = el.tagName.toLowerCase();
            const hay = (id + ' ' + cls).toLowerCase();
            if (/chat|bot|twixor|enterprise|launcher|widget/.test(hay)) {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) {
                out.push({tag, id, cls, x: r.x|0, y: r.y|0, w: r.width|0, h: r.height|0});
              }
            }
          }
          return out.slice(0, 40);
        }
        """
    )
    print(f"[3] {len(candidates)} chat-ish elements found:")
    for c in candidates:
        print(f"    {c['tag']}#{c['id']!r}.{c['cls'][:80]!r}  @ ({c['x']},{c['y']}) {c['w']}x{c['h']}")

    # Also list iframes — the chatbot might be inside one.
    frames = page.frames
    print(f"[4] {len(frames)} frame(s):")
    for f in frames:
        print(f"    name={f.name!r}  url={f.url}")

    Path(OUT / "01_landing.html").write_text(page.content(), encoding="utf-8")
    print(f"[5] saved 01_landing.png and 01_landing.html")

    browser.close()
print("done")
