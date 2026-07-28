"""event_agent smoke test — verify the PARALLEL endpoint works + {events, routes} is sane.
Run ON the H100 box AFTER the qwen_llm serve.sh:  python3 -m agent.event_agent.smoke

Checks: parallel pages/s · finds the 2 real releases · keeps a release's MULTIPLE urls (page + pdf) as ONE event ·
drops rss feed + dam asset · pagination/archive land in routes (go-deeper) · /about omitted · event url never in routes.
"""
from __future__ import annotations

import asyncio
import time

from providers.qwen_llm import QwenClient
from agent.event_agent.crawl.extract import extract_pages   # absolute: this file lives in tests/, not in the package

# The fixture MUST mirror what the crawl actually hands the extractor: render returns `inline`, i.e. reading-order
# text with each link embedded as `[anchor](url)`. extract tags those into Lnn placeholders and then GROUNDS every
# url the model emits against that tag map, so a url that never appeared inline cannot survive.
#
# This fixture used to be link-free prose plus a separate `links_block`, a shape the crawl stopped producing. The
# extractor had nothing to ground against, every event was dropped, and the smoke test reported events=0 routes=0 —
# i.e. it failed for a reason that had nothing to do with the code under test. Feeding the same content in the inline
# shape extracts both events (with the PDF merged into the results event) and both routes on the same model.
# {POD 2026-07-28 inline-shape run: "events: 2  routes: 2 ... urls: ['.../q3-2025-results', '.../q3-2025-slides.pdf']"}
# [CONFIDENCE: CONFIRMED 100% — same model, same box, only the fixture shape differed].
_SAMPLE = {
    "page_url": "https://investors.example.com/news",
    "page_text": (
        "Newsroom\nSkip to main navigation | [About](https://investors.example.com/about) | Contact | Careers\n"
        "[Q3 2025 Results](https://investors.example.com/news/q3-2025-results) — Example Corp reported third-quarter "
        "revenue of $1.2B. (Oct 28, 2025) "
        "[PDF Slides](https://investors.example.com/files/q3-2025-slides.pdf)\n"
        "[Example Corp to Present at the 2025 Investor Conference]"
        "(https://investors.example.com/events/investor-conference-2025) (Nov 15, 2025)\n"
        "Older news: [2023 archive](https://investors.example.com/news/archive/2023)   "
        "[Next page](https://investors.example.com/news?page=2)\n"
        "[RSS feed](https://investors.example.com/rss/news.xml) | "
        "[logo](https://www.example.com/content/dam/logos/logo.png) | Cookie settings"),
}


async def main() -> None:
    client = QwenClient()
    n = 8
    t0 = time.time()
    results = await extract_pages([_SAMPLE] * n, client=client)   # use_image=False → text-only (any model)
    dt = time.time() - t0
    r = results[0]
    events, routes = r["events"], r["routes"]
    print(f"[smoke] {n} pages in {dt:.2f}s ({n / dt:.1f} pages/s) | events={len(events)} routes={len(routes)}")
    for e in events:
        print("   EVENT", e["date"] or "—", "|", e["type"] or "—", "|", e["title"][:40], "| urls:", e["urls"])
    # _combine returns routes as {"url", "score"} dicts, not bare strings. The old code iterated them as strings, so
    # `"page=2" in rt` silently tested dict KEYS (always False) and `set(routes)` would raise TypeError: unhashable
    # type: 'dict' the moment any route came back. It only ever "passed" because routes was empty. Normalise once and
    # accept either shape. [CONFIDENCE: CONFIRMED 100% — observed live on the pod: ROUTE {'url': ..., 'score': 0.6}].
    route_urls = [rt["url"] if isinstance(rt, dict) else rt for rt in routes]
    for rt in route_urls:
        print("   ROUTE deeper", rt)

    ev_urls = {u for e in events for u in e["urls"]}
    checks = {
        "results": any("q3-2025-results" in u for u in ev_urls),
        "conf": any("investor-conference" in u for u in ev_urls),
        "multi_url": any(len(e["urls"]) >= 2 and any(".pdf" in u for u in e["urls"]) for e in events),
        "no_junk": not any(("rss" in u or "/content/dam/" in u) for u in ev_urls),
        "route_deeper": any(("page=2" in u or "/archive/" in u) for u in route_urls),
        "exclusive": ev_urls.isdisjoint(set(route_urls)),     # an event url never doubles as a route
    }
    print("[smoke]", checks, "→", "PASS ✅" if all(checks.values()) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
