"""event_agent.crawl — THE MAIN LOOP: company URL → all its events, via a close-loop BFS, FULLY TRACED.

用一句话讲完: 给一个 company URL → watercrawl.render_shot 开页+截图(并报告哪个 fetch 方法成功)→ extract_pages
并行喂 LLM 出 {events, routes} → **每页存全套 artifact(Tracer)** → events 收集去重, routes(一个纯 go-deeper
url 列表)入 frontier → 循环直到 frontier 干或撞 max_pages。event 是叶子(永不往深走), 只有 route 往深走 = close-loop。

Flow (one round):
  frontier ──take a batch──▶ render_shot each (open + full-page screenshot + method)
                              │
                              ├──▶ Tracer.save_page: content / screenshot / html / links / result / method / go_deeper
                              ▼
                        extract_pages (parallel LLM)  ──▶ [{events, routes}]
                              │                                   │
             events → collect (dedup by url)          routes (go-deeper urls) → back into frontier
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
from .urls import _canon                                    # shared canonical dedup key (also used by db.py; stdlib-only)

_USE_IMAGE = os.environ.get("EVENT_USE_IMAGE", "1") not in ("0", "false", "no")   # screenshot → needs a Qwen-VL model
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "60"))     # BFS page cap per company (a real IR tree is ~10-60 pages)
_BATCH = int(os.environ.get("EVENT_BATCH", "16"))            # pages rendered + sent to the LLM per round (parallel)
_TRACE_ROOT = os.environ.get("EVENT_TRACE_DIR", os.path.join(os.path.dirname(__file__), "traces"))


# _canon moved to .urls (shared with db.py, stdlib-only) — imported above.


# Common 2-part public suffixes — a host ending in one of these needs THREE labels for its registrable domain, not
# two (acme.co.uk → acme.co.uk, NOT co.uk). Without this, _same_site treats EVERY .co.uk / .com.tw / .co.jp company
# as the same site → the off-site guard leaks the crawl to unrelated UK/JP/TW hosts. {AUDIT 2026-07-22 bug #2}.
_TWO_PART_SUFFIXES = frozenset({
    "co.uk", "com.tw", "co.jp", "com.cn", "com.hk", "com.au", "co.kr", "com.br", "com.mx", "com.sg", "co.in",
    "co.za", "org.uk", "ne.jp", "or.jp", "com.tr", "co.nz", "com.my", "com.vn", "co.id", "com.ph"})


def _reg(host: str) -> str:
    """Registrable domain of host, public-suffix aware: 3 labels when the last two are a known 2-part ccTLD suffix
    (hotaimotor.com.tw), else 2 (pepsico.com)."""
    labels = (host or "").lower().split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else (labels[0] if labels else "")


def _same_site(url: str, root: str) -> bool:
    """True if url is on the SAME registrable domain as root — a cheap scope guard so a stray external go_deeper
    (a partner/social link the model mis-judged) can't send the crawl off-site. Public-suffix aware (see _reg)."""
    def host(u: str) -> str:
        return (urlsplit(u if u.startswith("http") else "https://" + u).netloc or "").lower()
    return _reg(host(url)) == _reg(host(root))


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
    # page_text = the INLINE-LINKED reading-order text (each link embedded in place as [anchor](url)) so the model
    # groups an event with its links by locality. Fall back to plain `text` for engines with no DOM (impersonate).
    # links_block stays EMPTY on purpose — the links now live INLINE in page_text, not in a separate links-first block.
    # {USER "you should embed the links into the context not links first"}
    return {
        "page_url": render["url"],
        "page_text": render.get("inline") or render.get("text", ""),
        "links_block": "",
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
    # FAIL-LOUD counters — a page can silently drop out two ways: render came back empty (walled/dead) or the LLM call
    # HARD-failed (server down / GCP→RunPod network drop). Both otherwise look like "a page with 0 events". We count them
    # and shout at run end so a degraded run is NEVER mistaken for a complete one.
    # {USER 2026-07-23 "fail loudly is the core ... we dont want quality issue"} [CONFIDENCE: CONFIRMED 100% — directive].
    failed_render = 0
    failed_extract = 0

    while frontier and len(visited) < max_pages:
        round_urls: list[str] = []
        # visited already includes the urls added THIS round (added below), so the cap is len(visited) < max_pages —
        # NOT len(visited)+len(round_urls) which double-counts the round and trips the cap early. {AUDIT bug #4}.
        while frontier and len(round_urls) < batch and len(visited) < max_pages:
            u = frontier.pop(0)
            ck = _canon(u)
            if ck in visited:
                continue
            visited.add(ck)
            round_urls.append(u)
        if not round_urls:
            break

        # render all in parallel (threads), drop empties, then LLM-extract all in parallel
        rendered = await asyncio.gather(*(_render_one(u) for u in round_urls))
        renders = [r for r in rendered if r]
        n_render_fail = len(round_urls) - len(renders)        # dropped = render came back empty (walled/dead)
        if n_render_fail:                                     # coverage loss — say it, don't swallow it
            failed_render += n_render_fail
            print(f"[crawl] ⚠️ {n_render_fail}/{len(round_urls)} pages FAILED to render (walled/dead/empty) — "
                  f"their events are UNSEEN this run", flush=True)
        if not renders:
            continue
        pages = [_to_page(r) for r in renders]
        results = await extract_pages(pages, client=client, use_image=_USE_IMAGE)

        new_events = new_routes = 0
        for render, res in zip(renders, results):
            tracer.save_page(render["url"], render, res)      # <-- full audit trail: content/shot/html/links/result/method
            if res.get("_error"):                             # LLM hard-failed on this page → NOT '0 events', it FAILED
                failed_extract += 1
                print(f"[crawl] ⛔ EXTRACT FAILED {render['url'][:70]} — {res['_error']} — this page's events are LOST "
                      f"(distinct from a genuine 0-event page)", flush=True)
                continue                                      # don't harvest events/routes from a failed page
            for e in res["events"]:
                # dedup by ANY overlapping url, not just urls[0] — the same event can surface on two pages with a
                # different primary url (one lists the detail first, another the pdf first), so first-url-only would
                # store it twice. If any of this event's urls was already seen, it's a duplicate. {AUDIT bug #3}.
                ekeys = {_canon(u) for u in e["urls"]}
                if ekeys & seen_event:
                    continue
                seen_event |= ekeys
                events.append(e)
                new_events += 1
            # routes is now a FLAT list of go-deeper url strings (no per-route go_deeper flag) — every url in it is a
            # follow target, so just scope-guard (same registrable site) + dedup against visited. {USER 2026-07-23
            # "just keep a list of urls go deeper"} [CONFIDENCE: CONFIRMED 100% — direct user instruction].
            for u in res["routes"]:
                if _same_site(u, start_url) and _canon(u) not in visited:
                    frontier.append(u)
                    new_routes += 1
        print(f"[crawl] {start_url[:50]} | round: {len(renders)} pages → +{new_events} events, +{new_routes} to follow "
              f"| total events={len(events)} visited={len(visited)} frontier={len(frontier)}", flush=True)

    tracer.save_summary(events, len(visited))
    # status = ok ONLY if nothing dropped. ANY render/extract failure → "incomplete" so the GCP caller can react
    # (retry the failed pages / alert) instead of trusting a partial event list as the whole truth.
    status = "ok" if (failed_render == 0 and failed_extract == 0) else "incomplete"
    print(f"[crawl] DONE {start_url[:50]} — {len(events)} events over {len(visited)} pages. Trace: {run_dir}", flush=True)
    if status != "ok":                                        # LOUD run-level banner — a degraded run must be unmissable
        print(f"[crawl] ⚠️⚠️ INCOMPLETE RUN — {failed_extract} pages FAILED extraction (LLM/network), "
              f"{failed_render} pages FAILED render (walled/dead). Event list is PARTIAL — do NOT treat as complete.",
              flush=True)
    return {"events": events, "pages": len(visited), "trace_dir": run_dir,
            "status": status, "failed_extract": failed_extract, "failed_render": failed_render}


def _slug_host(url: str) -> str:
    return (urlsplit(url if url.startswith("http") else "https://" + url).netloc or "run").replace(":", "_")


if __name__ == "__main__":                                  # manual: python3 -m agent.event_agent.crawl <url>
    import sys
    out = asyncio.run(crawl_company(sys.argv[1] if len(sys.argv) > 1 else "https://investors.example.com"))
    print(f"\n=== {len(out['events'])} events from {out['pages']} pages | trace: {out['trace_dir']} ===")
    for e in out["events"][:40]:
        print(" ", e["date"] or "—", "|", e["type"] or "—", "|", e["title"][:50], "|", e["urls"])
