"""event_agent smoke test — verify the PARALLEL endpoint works + {events, routes} is sane.
Run ON the H100 box AFTER the qwen_llm serve.sh:  python3 -m agent.event_agent.smoke

Checks: parallel pages/s · finds the 2 real releases · keeps a release's MULTIPLE urls (page + pdf) as ONE event ·
drops rss feed + dam asset · pagination/archive → go_deeper=true · /about → false · event url never in routes.
"""
from __future__ import annotations

import asyncio
import time

from providers.qwen_llm import QwenClient
from .extract import extract_pages

_SAMPLE = {
    "page_url": "https://investors.example.com/news",
    "page_text": ("Newsroom\nSkip to main navigation | About | Contact | Careers\n"
                  "Q3 2025 Results — Example Corp reported third-quarter revenue of $1.2B. (Oct 28, 2025) "
                  "[Press Release] [PDF Slides]\n"
                  "Example Corp to Present at the 2025 Investor Conference (Nov 15, 2025)\n"
                  "Older news: 2024 | 2023   Next page →   RSS feed | Cookie settings"),
    "links_block": (
        "https://investors.example.com/news/q3-2025-results — Q3 2025 Results\n"
        "https://investors.example.com/files/q3-2025-slides.pdf — Q3 2025 Slides (PDF)\n"
        "https://investors.example.com/events/investor-conference-2025 — 2025 Investor Conference\n"
        "https://investors.example.com/news?page=2 — Next page\n"
        "https://investors.example.com/news/archive/2023 — 2023 archive\n"
        "https://investors.example.com/rss/news.xml — RSS feed\n"
        "https://www.example.com/content/dam/logos/logo.png — logo\n"
        "https://investors.example.com/about — About Us"),
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
    for rt in routes:
        print("   ROUTE", "deeper" if rt["go_deeper"] else "skip  ", rt["url"])

    ev_urls = {u for e in events for u in e["urls"]}
    checks = {
        "results": any("q3-2025-results" in u for u in ev_urls),
        "conf": any("investor-conference" in u for u in ev_urls),
        "multi_url": any(len(e["urls"]) >= 2 and any(".pdf" in u for u in e["urls"]) for e in events),
        "no_junk": not any(("rss" in u or "/content/dam/" in u) for u in ev_urls),
        "route_deeper": any(rt["go_deeper"] and ("page=2" in rt["url"] or "/archive/" in rt["url"]) for rt in routes),
        "exclusive": ev_urls.isdisjoint({rt["url"] for rt in routes}),
    }
    print("[smoke]", checks, "→", "PASS ✅" if all(checks.values()) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
