"""
Recon part 4: drill into the chat iframes and capture initial chat state.
"""
from playwright.sync_api import sync_playwright
from pathlib import Path
import json

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

    # Slide the chatbot into view first (in case the iframe inside waits for visibility).
    page.evaluate(
        """
        () => {
          const el = document.getElementById('cbotform');
          if (el) {
            el.style.right = '0px';
            el.style.left = 'auto';
          }
        }
        """
    )
    # Click the launcher button to fire any handlers.
    try:
        page.locator('button.support').first.click(timeout=5_000)
        print("    clicked button.support")
    except Exception as e:
        print(f"    launcher click skipped: {e}")
    page.wait_for_timeout(3_000)

    print(f"[2] page frames after open:")
    for i, f in enumerate(page.frames):
        print(f"    [{i}] name={f.name!r} url={f.url}")

    # Drill into outer iframe.
    outer = None
    for f in page.frames:
        if "hpclpwa/myhpgas.html" in f.url:
            outer = f
            break
    if outer is None:
        print("    !! outer iframe not found")
    else:
        print(f"[3] outer iframe loaded: {outer.url}")
        try:
            outer.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception as e:
            print(f"    outer wait error: {e}")

    # Wait additional time for inner iframe to finish.
    page.wait_for_timeout(5_000)

    # List frames again now that things may have settled.
    print(f"[4] frames after wait:")
    for i, f in enumerate(page.frames):
        print(f"    [{i}] name={f.name!r} url={f.url}")

    # Find the deepest frame (the actual chatbot UI).
    inner = None
    for f in page.frames:
        if "/pwa/view" in f.url:
            inner = f
            break
    if inner is None:
        print("    !! inner pwa frame not found, falling back to outer")
        inner = outer

    if inner:
        try:
            inner.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception as e:
            print(f"    inner wait error: {e}")
        page.wait_for_timeout(4_000)

        print(f"[5] inner frame DOM probe:")
        info = inner.evaluate(
            """
            () => {
              const pick = (sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                return {
                  found: true,
                  childCount: el.children.length,
                  text: (el.innerText || '').slice(0, 400),
                };
              };
              return {
                title: document.title,
                bodyTextHead: (document.body ? document.body.innerText : '').slice(0, 800),
                chat: pick('ul.list-group.chat'),
                wrapper: pick('#wrapper_enterprise_msg'),
                scroller: pick('#scroller'),
                loader: pick('.load-container'),
                processBtn: pick('.process-button'),
                inputs: Array.from(document.querySelectorAll('input,textarea')).map(i => ({
                  tag: i.tagName.toLowerCase(),
                  type: i.getAttribute('type'),
                  id: i.id,
                  name: i.getAttribute('name'),
                  placeholder: i.getAttribute('placeholder'),
                  cls: i.getAttribute('class'),
                  visible: i.offsetParent !== null,
                })),
                buttons: Array.from(document.querySelectorAll('button,a.btn,.process-button button')).slice(0,30).map(b => ({
                  tag: b.tagName.toLowerCase(),
                  text: (b.innerText || '').trim().slice(0,80),
                  id: b.id,
                  cls: b.getAttribute('class'),
                  visible: b.offsetParent !== null,
                })),
                li_count: document.querySelectorAll('ul.list-group.chat > li').length,
                li_items: Array.from(document.querySelectorAll('ul.list-group.chat > li')).map(li => ({
                  cls: li.getAttribute('class'),
                  text: (li.innerText || '').trim().slice(0, 200),
                })),
              };
            }
            """
        )
        Path(OUT / "iframe_probe.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(f"    wrote iframe_probe.json")
        print(f"    title={info['title']!r}")
        print(f"    li_count={info['li_count']}")
        print(f"    chat: {info['chat']}")
        print(f"    loader: {info['loader']}")
        print(f"    inputs ({len(info['inputs'])}):")
        for i in info["inputs"]:
            print(f"      {i}")
        print(f"    buttons ({len(info['buttons'])}):")
        for b in info["buttons"][:15]:
            print(f"      {b}")
        print(f"    body head: {info['bodyTextHead'][:400]!r}")
        print(f"    li_items:")
        for it in info["li_items"]:
            print(f"      cls={it['cls']!r}  text={it['text']!r}")

    try:
        page.screenshot(path=str(OUT / "20_full.png"), timeout=10_000)
    except Exception:
        print("    page screenshot skipped")

    browser.close()
print("done")
