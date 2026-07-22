"""smoke_render — the FULL vision pipeline on a REAL IR page: watercrawl render → screenshot → Qwen-VL → events JSON.

用一句话讲完: 给一个真实公司 IR events URL → watercrawl.render_shot 用 headless chromium 打开页面并截全页图 →
把这张真截图 + "只抽真事件、忽略 nav/footer/feed" 的 system prompt 打给本机 vLLM Qwen-VL → 打印抽出的事件。
这是 smoke_vision(合成图) 之后的真东西:验证 render→screenshot→VL 整条链在真实、含噪声的页面上 work。

WHY separate from smoke_vision: this stage adds the BROWSER as a second moving part (chromium must be installed +
reachable-network to the target site). Keeping it separate means a render failure is distinguishable from a model
failure — render_shot returns method="" / empty shot_b64 on total render failure, which we report explicitly.

Run ON THE VM:  python3 smoke_render.py [URL]
"""
from __future__ import annotations

import asyncio
import sys

from providers.qwen_llm import QwenClient
from providers.watercrawl import pool

# reuse the exact schema + anti-over-classification contract proven in smoke_vision (single source of truth)
from smoke_vision import EVENT_SCHEMA, SYSTEM

# a real IR events page as the default target (dated event rows in the body, plus the usual nav/footer chrome)
DEFAULT_URL = "https://investors.coca-colacompany.com/news-events/events"


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"rendering {url} ...")
    r = pool.render_shot(url, wait_ms=3500)                 # headless chromium → full-page JPEG (fallback chain inside)
    shot = r.get("shot_b64", "")
    print(f"render method={r.get('method')!r}  text_len={len(r.get('text',''))}  "
          f"links={len(r.get('links',[]))}  shot_bytes~{int(len(shot) * 0.75)}")
    if not shot:                                            # no screenshot → can't do vision; say so, don't fake a pass
        print("VERDICT: NO SCREENSHOT (render failed or impersonate-only path) — cannot run vision")
        return

    client = QwenClient()                                    # localhost:8000 vLLM Qwen-VL
    result = await client.send_one(
        system=SYSTEM,
        user=f"PAGE URL: {url}\nExtract the investor events from this page screenshot.",
        image_b64=shot,
        guided_json=EVENT_SCHEMA,
    )
    events = result.get("events", [])
    print(f"\nmodel returned {len(events)} event(s):")
    for e in events:                                         # each real event the VL read off the rendered layout
        print(f"  - {e.get('title')!r}  date={e.get('date')!r}  type={e.get('type')!r}")


if __name__ == "__main__":
    asyncio.run(main())
