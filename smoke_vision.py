"""smoke_vision — prove the Qwen2.5-VL vision path end-to-end BEFORE wiring the real crawl.

用一句话讲完: 用 PIL 画一张假的 IR 页截图(顶部 nav + 中间真事件表 + 底部 footer/RSS 陷阱)→ base64 → QwenClient
把它 + 一段"只抽真事件、忽略 nav/footer/feed"的 system prompt 打给本机 vLLM 的 Qwen-VL → 看返回的 JSON 是否
**只包含真事件行、把 nav/footer/RSS 全部丢掉**。这一步不碰浏览器,只验证 client→server→vision解码→guided_json 通路
以及模型的"看版面判断结构"能力 —— 这正是 text-only BERT over-classify 的地方。

WHY synthetic image (not a live render): the VM (h20-1039) may not have a browser installed yet, and a live render adds
a second failure mode. A synthetic image isolates the ONE thing we're validating here: the served VL model actually
decodes an image and returns schema-valid JSON that respects the anti-over-classification rules. Live-render smoke is
the next stage (needs patchright + chromium on the VM).

Run ON THE VM (localhost:8000 is the vLLM server):  python3 smoke_vision.py
"""
from __future__ import annotations

import asyncio
import base64
import io

from PIL import Image, ImageDraw  # pillow ships with vLLM (Qwen-VL image preprocessing needs it)

from providers.qwen_llm import QwenClient

# ── event schema: what the model MUST return. guided_json forces vLLM's sampler onto this shape on the first try,
# so we always get valid JSON (no prose, no truncation-then-parse-fail). {config MAX_TOKENS note} keeps it un-truncated.
EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "type": {"type": "string"},
                },
                "required": ["title", "date", "type"],
            },
        }
    },
    "required": ["events"],
}

# ── the anti-over-classification contract. This is the WHOLE reason we switched off BERT: a nav link / footer / RSS
# feed is NOT an event even though its text looks event-ish. The model must judge by VISUAL POSITION + role, not keywords.
SYSTEM = (
    "You extract INVESTOR-RELATIONS events from a rendered web page screenshot. "
    "Return ONLY genuine scheduled events (earnings calls, shareholder meetings, investor days, conference "
    "presentations) that appear in the page's MAIN content as dated list/table rows. "
    "STRICTLY IGNORE: navigation menus, headers, footers, cookie banners, and RSS/feed/subscribe links — these are "
    "site chrome, never events, no matter what their text says. If a row has no real date, drop it. "
    "Reply as JSON matching the schema; empty events array if the page has none."
)


def _make_fake_ir_page() -> str:
    """Draw a 900×640 'IR events page' with the exact traps BERT falls for: a top NAV bar with an 'Events' menu item,
    a REAL events table (3 dated rows) in the body, and a footer holding an 'RSS Feed' + 'Subscribe' link. A correct
    VL model returns the 3 body rows and drops the nav 'Events' item + the footer feed links. Returns JPEG base64."""
    img = Image.new("RGB", (900, 640), "white")            # white page canvas
    d = ImageDraw.Draw(img)                                # default bitmap font — no font file needed on the VM

    # top nav bar (grey strip) — the 'Events' menu item here is a TRAP (it's chrome, not an event)
    d.rectangle([0, 0, 900, 40], fill=(230, 230, 230))
    d.text((20, 14), "HOME    PRODUCTS    INVESTORS    Events    CONTACT", fill=(60, 60, 60))

    # main content: a real events table with dated rows (what the model SHOULD extract)
    d.text((20, 70), "Upcoming Investor Events", fill=(0, 0, 0))
    d.line([20, 92, 880, 92], fill=(0, 0, 0))
    rows = [
        "Q1 2026 Earnings Conference Call        Feb 05, 2026",
        "Annual Shareholder Meeting              Mar 18, 2026",
        "Barclays Investor Day Presentation      Apr 22, 2026",
    ]
    for i, r in enumerate(rows):                           # lay the 3 real event rows down the body
        d.text((30, 110 + i * 30), r, fill=(20, 20, 20))

    # footer (grey strip) with feed/subscribe TRAPS — event-ish words, but NOT events
    d.rectangle([0, 600, 900, 640], fill=(230, 230, 230))
    d.text((20, 612), "RSS Feed   |   Subscribe to Events Calendar   |   Sitemap   © 2026", fill=(60, 60, 60))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)               # match watercrawl's JPEG q70 so mime-sniff path is exercised
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def main() -> None:
    shot_b64 = _make_fake_ir_page()                        # the synthetic screenshot
    client = QwenClient()                                   # talks to config.BASE_URLS (localhost:8000) as SERVED_NAME
    result = await client.send_one(
        system=SYSTEM,
        user="PAGE URL: https://example.com/investors/events\nExtract the investor events from this page screenshot.",
        image_b64=shot_b64,
        guided_json=EVENT_SCHEMA,
    )
    events = result.get("events", [])
    print(f"model returned {len(events)} event(s):")
    for e in events:
        print(f"  - {e.get('title')!r}  date={e.get('date')!r}  type={e.get('type')!r}")

    # PASS criteria: 3 real rows kept, nav 'Events' + footer 'RSS/Subscribe' dropped. Report the verdict explicitly.
    titles = " ".join(str(e.get("title", "")).lower() for e in events)
    kept_real = sum(k in titles for k in ("earnings", "shareholder", "investor day"))
    leaked = any(k in titles for k in ("rss", "subscribe", "sitemap", "home", "contact"))
    print(f"\nVERDICT: kept_real={kept_real}/3  leaked_chrome={leaked}  "
          f"{'PASS' if kept_real >= 2 and not leaked else 'CHECK'}")


if __name__ == "__main__":
    asyncio.run(main())
