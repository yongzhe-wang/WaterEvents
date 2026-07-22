"""event_agent.crawl — THE MAIN LOOP: company URL → all its events, via a close-loop BFS, FULLY TRACED.

用一句话讲完: 给一个 company URL → watercrawl.render_shot 开页+截图(并报告哪个 fetch 方法成功)→ extract_pages
并行喂 LLM 出 {events, routes} → **每页存全套 artifact(Tracer)** → events 收集去重, routes 里 go_deeper=true 的
入 frontier → 循环直到 frontier 干或撞 max_pages。event 是叶子(永不 go_deeper), 只有 route 往深走 = close-loop。

Flow (one round):
  frontier ──take a batch──▶ render_shot each (open + full-page screenshot + method)
                              │
                              ├──▶ Tracer.save_page: content / screenshot / html / links / result / method / go_deeper
                              ▼
                        extract_pages (parallel LLM)  ──▶ [{events, routes}]
                              │                                   │
             events → collect (dedup by url)          routes → go_deeper? → back into frontier
"""
from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlsplit, urlunsplit

from providers import watercrawl
from providers.qwen_llm import QwenClient

from .extract import extract_pages
from .trace import Tracer

_USE_IMAGE = os.environ.get("EVENT_USE_IMAGE", "1") not in ("0", "false", "no")   # screenshot → needs a Qwen-VL model
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "60"))     # BFS page cap per company (a real IR tree is ~10-60 pages)
_BATCH = int(os.environ.get("EVENT_BATCH", "16"))            # pages rendered + sent to the LLM per round (parallel)
_TRACE_ROOT = os.environ.get("EVENT_TRACE_DIR", os.path.join(os.path.dirname(__file__), "traces"))


def _canon(url: str) -> str:
    """Canonical dedup key: lowercase scheme+host, drop #fragment + trailing slash. Path case preserved."""
    try:
        p = urlsplit(url if url.startswith("http") else "https://" + url)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, "")) or url
    except Exception:                                        # noqa: BLE001 — unparseable → use the raw string as key
        return url


def _same_site(url: str, root: str) -> bool:
    """True if url is on the SAME registrable-ish host family as root — a cheap scope guard so a stray external
    go_deeper (a partner/social link the model mis-judged) can't send the crawl off-site."""
    def reg(u: str) -> str:
        h = (urlsplit(u if u.startswith("http") else "https://" + u).netloc or "").lower()
        return ".".join(h.split(".")[-2:]) if h.count(".") >= 1 else h
    return reg(url) == reg(root)


async def _render_one(url: str) -> dict | None:
    """Open ONE url with watercrawl (in a thread — render_shot is sync + marshals to the browser loop). Returns the
    render dict {url, text, links, html, shot_b64, method}, or None when the render came back empty (skip it)."""
    r = await asyncio.to_thread(watercrawl.render_shot, url)
    if not r.get("text") and not r.get("links"):            # walled / dead / empty → skip, don't feed the model junk
        return None
    r["url"] = url
    return r


def _to_page(render: dict) -> dict:
    """render dict → the page dict extract_pages wants (page_url, page_text, links_block, image_b64)."""
    return {
        "page_url": render["url"],
        "page_text": render.get("text", ""),
        "links_block": "\n".join((render.get("links") or [])[:400]),   # bare url list; context is inside page_text
        "image_b64": render["shot_b64"] if (_USE_IMAGE and render.get("shot_b64")) else None,
    }


async def crawl_company(start_url: str, max_pages: int = _MAX_PAGES, batch: int = _BATCH,
                        client: QwenClient | None = None, trace_dir: str | None = None) -> dict:
    """company URL → {"events":[...], "pages": N, "trace_dir": ...}. BFS close-loop, every page fully traced to disk.
    Events dedup by their first url; the frontier dedups by canonical url + stays on-site (routes only go_deeper)."""
    client = client or QwenClient()
    run_dir = trace_dir or os.path.join(_TRACE_ROOT, f"{_slug_host(start_url)}_{time.strftime('%Y%m%d_%H%M%S')}")
    tracer = Tracer(run_dir)
    print(f"[crawl] {start_url} → tracing to {run_dir}", flush=True)

    frontier: list[str] = [start_url]
    visited: set[str] = set()
    events: list[dict] = []
    seen_event: set[str] = set()

    while frontier and len(visited) < max_pages:
        round_urls: list[str] = []
        while frontier and len(round_urls) < batch and (len(visited) + len(round_urls)) < max_pages:
            u = frontier.pop(0)
            ck = _canon(u)
            if ck in visited:
                continue
            visited.add(ck)
            round_urls.append(u)
        if not round_urls:
            break

        # render all in parallel (threads), drop empties, then LLM-extract all in parallel
        renders = [r for r in await asyncio.gather(*(_render_one(u) for u in round_urls)) if r]
        if not renders:
            continue
        pages = [_to_page(r) for r in renders]
        results = await extract_pages(pages, client=client, use_image=_USE_IMAGE)

        new_events = new_routes = 0
        for render, res in zip(renders, results):
            tracer.save_page(render["url"], render, res)      # <-- full audit trail: content/shot/html/links/result/method
            for e in res["events"]:
                key = _canon(e["urls"][0])                    # dedup an event by its primary (first) url
                if key not in seen_event:
                    seen_event.add(key)
                    events.append(e)
                    new_events += 1
            for rt in res["routes"]:
                if rt["go_deeper"] and _same_site(rt["url"], start_url) and _canon(rt["url"]) not in visited:
                    frontier.append(rt["url"])
                    new_routes += 1
        print(f"[crawl] {start_url[:50]} | round: {len(renders)} pages → +{new_events} events, +{new_routes} to follow "
              f"| total events={len(events)} visited={len(visited)} frontier={len(frontier)}", flush=True)

    tracer.save_summary(events, len(visited))
    print(f"[crawl] DONE {start_url[:50]} — {len(events)} events over {len(visited)} pages. Trace: {run_dir}", flush=True)
    return {"events": events, "pages": len(visited), "trace_dir": run_dir}


def _slug_host(url: str) -> str:
    return (urlsplit(url if url.startswith("http") else "https://" + url).netloc or "run").replace(":", "_")


if __name__ == "__main__":                                  # manual: python3 -m agent.event_agent.crawl <url>
    import sys
    out = asyncio.run(crawl_company(sys.argv[1] if len(sys.argv) > 1 else "https://investors.example.com"))
    print(f"\n=== {len(out['events'])} events from {out['pages']} pages | trace: {out['trace_dir']} ===")
    for e in out["events"][:40]:
        print(" ", e["date"] or "—", "|", e["type"] or "—", "|", e["title"][:50], "|", e["urls"])
