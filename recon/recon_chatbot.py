"""
Recon part 2: load the inner chatbot URL directly and capture its DOM.
Block fonts so screenshots don't hang waiting for font loads.
"""
from playwright.sync_api import sync_playwright
from pathlib import Path
import re

OUT = Path(__file__).parent
DIRECT_URL = (
    "https://hpchatbot.hpcl.co.in/pwa/view?data="
    "eyJlSWQiOjEwMCwiZ2xpIjp0cnVlLCJjYW1wYWlnbklkIjoiNjQ1MjAyZTNhMTdlMTZhY2RlOTNhMjhmIiwibGkiOiI4OWJiNzZlYTZlNmY0OTVjOTAwNTc3M2I1MGEzNDMyMSJ9"
)


def block_fonts(route):
    if route.request.resource_type in ("font",):
        return route.abort()
    return route.continue_()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        viewport={"width": 420, "height": 760},
        user_agent=(
            "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
        ),
    )
    ctx.route("**/*", block_fonts)
    page = ctx.new_page()

    print(f"[1] navigating directly to chatbot URL")
    page.goto(DIRECT_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(5_000)

    print(f"[2] title={page.title()!r}")
    print(f"    url={page.url}")
    try:
        page.screenshot(path=str(OUT / "02_chatbot_initial.png"), timeout=15_000)
        print("    saved 02_chatbot_initial.png")
    except Exception as e:
        print(f"    screenshot failed: {e}")

    # Dump the structure of the chat container.
    info = page.evaluate(
        """
        () => {
          const pick = (sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            return {
              outerHTML: el.outerHTML.slice(0, 600),
              childCount: el.children.length,
              text: (el.innerText || '').slice(0, 400),
            };
          };
          return {
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
            })),
            buttons: Array.from(document.querySelectorAll('button')).slice(0,20).map(b => ({
              text: (b.innerText || '').trim().slice(0,60),
              id: b.id,
              cls: b.getAttribute('class'),
            })),
            li_count: document.querySelectorAll('ul.list-group.chat > li').length,
          };
        }
        """
    )

    print("[3] DOM probe:")
    for k, v in info.items():
        if k in ("inputs", "buttons"):
            print(f"    {k}: ({len(v)})")
            for item in v:
                print(f"        {item}")
        elif k == "li_count":
            print(f"    {k}: {v}")
        else:
            if v is None:
                print(f"    {k}: <not found>")
            else:
                snippet = re.sub(r"\s+", " ", v["outerHTML"])[:200]
                print(f"    {k}: childCount={v['childCount']}")
                print(f"        text: {v['text'][:200]!r}")
                print(f"        html: {snippet}")

    # Wait a bit longer in case bot sends more messages.
    print("[4] waiting for additional bot messages...")
    page.wait_for_timeout(6_000)
    info2 = page.evaluate(
        """
        () => {
          const lis = Array.from(document.querySelectorAll('ul.list-group.chat > li'));
          return {
            count: lis.length,
            items: lis.map(li => ({
              cls: li.getAttribute('class'),
              text: (li.innerText || '').trim().slice(0, 300),
              hasInput: li.querySelector('input,textarea') ? true : false,
              hasButton: li.querySelector('button,a.btn,.process-button') ? true : false,
            })),
          };
        }
        """
    )
    print(f"    total <li> messages: {info2['count']}")
    for i, item in enumerate(info2["items"]):
        print(f"      [{i}] cls={item['cls']!r} input={item['hasInput']} btn={item['hasButton']}")
        print(f"          text={item['text']!r}")

    try:
        page.screenshot(path=str(OUT / "03_chatbot_after_wait.png"), timeout=15_000)
        print("    saved 03_chatbot_after_wait.png")
    except Exception as e:
        print(f"    screenshot failed: {e}")

    Path(OUT / "02_chatbot.html").write_text(page.content(), encoding="utf-8")
    print("[5] saved 02_chatbot.html")

    browser.close()
print("done")
