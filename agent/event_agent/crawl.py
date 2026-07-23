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
import re
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


# Route-noise denylist — path SEGMENTS that are NEVER an IR event page. WHY it matters: a `route` returned by the VLM
# becomes a RENDERED + EXTRACTED page next round (not just a hub-follow), so following legal / careers / account /
# SEC-filing-hub links only explodes the frontier with pages that cannot yield an event — NVDA over-crawled to 61 pages
# hitting terms-of-service + SEC filing lists. Only UNAMBIGUOUS non-event pages are listed; every event-adjacent hub
# (events / calendar / webcasts / presentations / earnings / quarterly / annual-report(s) / annual-meeting / dividend(s)
# / press / press-release / news / newsroom / media / results / financials) is DELIBERATELY absent so it stays
# crawlable. SEC filing lists are excluded because filings arrive via the SEC-API channel, not the crawl.
# {USER 2026-07-23 "route 过滤要挡掉 legal/hub/filing-list ... 页数从 20+ 降到 ~5-8 真事件页"}
# [CONFIDENCE: CONFIRMED 95% — user named terms-of-service + SEC filing lists as the noise; the event-adjacent
#  exclusions are my conservative call, validated by the first real NVDA crawl (blocked routes are logged, not silent)].
_NOISE_SEGMENTS = frozenset({
    # ── legal / policy ──
    "terms", "terms-of-use", "terms-of-service", "termsofuse", "tos", "privacy", "privacy-policy",
    "legal", "legal-notice", "legal-notices", "cookie", "cookies", "cookie-policy", "disclaimer",
    "disclaimers", "accessibility", "safe-harbor", "sitemap", "site-map",
    # ── corporate-info hubs (never an event) ──
    "careers", "career", "jobs", "about", "about-us", "aboutus", "who-we-are", "our-company",
    "company-overview", "contact", "contact-us", "contacts", "team", "our-team",
    "our-people", "history", "our-history",
    "mission", "values", "culture", "diversity", "supplier", "suppliers", "vendors",
    # ── account / utility / nav ──
    "login", "log-in", "signin", "sign-in", "register", "subscribe", "subscription", "newsletter",
    "search", "rss", "feed", "feeds", "print", "share", "email-alerts", "alerts", "faq", "faqs",
    "help", "support",
    # NOTE: sec-filings / edgar / regulatory-filings AND governance / board-of-directors / leadership /
    # management-team / executives were REMOVED from this hard denylist — they are NOT never-event pages: an
    # SEC-filings hub carries filing EVENTS (10-K/8-K/proxy dates) and a governance/board page links to the annual
    # MEETING + proxy. Now that routes carry a confidence SCORE, the model ranks these appropriately (low if they
    # dead-end, followed if they lead to events) instead of a blanket block. {USER 2026-07-23 "sec-filings ... board-
    # of-directors ... these should be kept"} [CONFIDENCE: CONFIRMED 100% — direct user instruction to keep them].
})


def _is_noise_route(url: str) -> bool:
    """True if this go-deeper url is a KNOWN non-event page (legal / careers / account / SEC-filing hub) → don't follow
    it into the frontier. Segment-EXACT match (not substring), so `/newsroom` is NOT caught by `news` and
    `/presentations` is caught by nothing — event-adjacent hubs stay crawlable. Applied ONLY to discovered routes; the
    seed url is never filtered. {USER 2026-07-23 "挡掉 legal/hub/filing-list"} [CONFIDENCE: CONFIRMED 95%]."""
    path = urlsplit(url if url.startswith("http") else "https://" + url).path.lower()   # scheme-safe path extract
    segs = [s for s in path.split("/") if s]                 # non-empty path segments (drops the leading/trailing //)
    return any(s in _NOISE_SEGMENTS for s in segs)           # any segment on the denylist → it's noise, skip it


# Binary / document / media file extensions. WHY a SEPARATE guard from _NONEVENT_URL_RE (extract.py): a url ending in
# one of these (a PDF earnings release, a PPTX deck, an MP3/MP4, a ZIP) is an event LEAF — a MATERIAL of an event, so it
# MUST stay in that event's urls[]. But it is NEVER a page to render + crawl: navigating a headless browser to a binary
# file fires "Download is starting" and Chromium BUFFERS the (often huge) file in memory; with concurrency the stacked
# download buffers exhausted the container cgroup (~46.5 GB) → OOM SIGKILL mid-run (fetch_10 EXIT=137) right after it
# tried to render Apple's FY26 financial-statement PDFs + the ~200-page Environmental Report. So this regex gates ONLY
# the crawl FRONTIER (go-deeper routes), never an event's material urls. {LOG 2026-07-23 "render_shot failed ... Download
# is starting ... FY26_Q1_Consolidated_Financial_Statements.pdf" → "50095 Killed" → "EXIT=137"} [CONFIDENCE: CONFIRMED
# 100% — the OOM immediately followed the PDF download-render attempts and exit 137 = 128+9 = SIGKILL, the OOM killer's signal].
_BINARY_ROUTE_RE = re.compile(
    r'\.(pdf|pptx?|docx?|xlsx?|csv|zip|rar|7z|gz|tgz|mp3|wav|m4a|aac|mp4|mov|avi|mkv|webm|ics|vcs|epub)(\?|#|$)', re.I)


def _is_binary_route(url: str) -> bool:
    """True if this go-deeper url points at a downloadable document/media FILE (pdf/pptx/xlsx/zip/mp3/mp4/…). Such a url
    is an event LEAF — never RENDER it (the browser would buffer the download → the OOM that SIGKILLed the run). It stays
    a valid EVENT material url via _clean_urls; this only excludes it from the crawl FRONTIER, never from an event."""
    path = urlsplit(url if url.startswith("http") else "https://" + url).path   # query/fragment stripped for the ext test
    return bool(_BINARY_ROUTE_RE.search(path))


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

    # frontier is a PRIORITY QUEUE of (url, confidence-score): the seed gets max score 1.0; every discovered route
    # carries the VLM's confidence that it leads to real events. Each round we sort highest-first, so event-section
    # navs are crawled BEFORE low-score marketing / www-subdomain pages (which sink to the tail and get cut by
    # max_pages instead of flooding the frontier). {USER 2026-07-23 "add a confidence field to each route ... frontier
    # should always sort from highest to lowest" — fixes the investor.nvidia.com → www.nvidia.com marketing leak}
    # [CONFIDENCE: CONFIRMED 100% — direct user instruction to rank the frontier by a per-route score].
    frontier: list[tuple[str, float]] = [(start_url, 1.0)]
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
        frontier.sort(key=lambda t: -t[1])                    # PRIORITY: highest-confidence routes first each round
        round_urls: list[str] = []
        # visited already includes the urls added THIS round (added below), so the cap is len(visited) < max_pages —
        # NOT len(visited)+len(round_urls) which double-counts the round and trips the cap early. {AUDIT bug #4}.
        while frontier and len(round_urls) < batch and len(visited) < max_pages:
            u, _score = frontier.pop(0)                        # take the top-scored url (frontier is (url, score))
            ck = _canon(u)
            if ck in visited:
                continue
            visited.add(ck)
            round_urls.append(u)
        if not round_urls:
            break

        # PIPELINE each page: render → the MOMENT it has content, fire its VLM extract — all pages of the round run
        # concurrently. WHY not two-phase (gather ALL renders, THEN extract ALL): that serialises the whole ~11.5s
        # render batch BEFORE the VLM batch even starts, so a round = render_time + vlm_time. Pipelining OVERLAPS them
        # (page A's VLM decodes on the server's continuous batch while page B is still rendering) → round ≈
        # max(render_time, vlm_time). {USER 2026-07-23 "BFS 一轮把 N 页的 render + VLM 全并发发 → server continuous
        # batching → ~1 页/秒"} [CONFIDENCE: CONFIRMED 100% — direct user instruction to overlap the two stages].
        async def _render_then_extract(url: str):
            r = await _render_one(url)                         # browser render (resident-pool bounded)
            if r is None:                                     # walled/dead/empty → nothing to extract
                return url, None, None
            res = (await extract_pages([_to_page(r)], client=client, use_image=_USE_IMAGE))[0]  # VLM fires as soon as render lands
            return url, r, res
        pipelined = await asyncio.gather(*(_render_then_extract(u) for u in round_urls))

        new_events = new_routes = n_render_fail = n_route_blocked = 0
        for url, render, res in pipelined:
            if render is None:                                # render failed (walled/dead) — coverage loss, counted below
                n_render_fail += 1
                continue
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
            # routes is a FLAT list of go-deeper url strings — every url is a follow target. Gate order: (1) same-site
            # scope guard + dedup against visited, THEN (2) noise-route denylist (legal/careers/account/SEC-filing hub)
            # so we don't render+extract a page that cannot hold an event. Blocked routes are COUNTED + printed (not a
            # silent coverage cap) per the fail-loud directive. {USER 2026-07-23 "挡掉 legal/hub/filing-list"; "fail
            # loudly is the core"} [CONFIDENCE: CONFIRMED 100% — direct user instruction + fail-loud principle].
            for rt in res["routes"]:                          # routes are now {url, score}
                u, sc = rt["url"], rt.get("score", 0.5)
                if not (_same_site(u, start_url) and _canon(u) not in visited):
                    continue                                  # off-site or already-visited → normal skip (not "blocked")
                if _is_noise_route(u):                         # legal/careers/account/SEC-filing hub → never an event page
                    n_route_blocked += 1                       # fail-loud: count it, report below — never silently dropped
                    print(f"[crawl] 🚧 route BLOCKED (noise) {u[:90]}", flush=True)
                    continue
                if _is_binary_route(u):                        # PDF/deck/audio/video FILE = event leaf, NOT a page to render
                    n_route_blocked += 1                       # (rendering it buffers the download → the OOM that killed the run)
                    print(f"[crawl] 🚧 route BLOCKED (binary file, not a page) {u[:90]}", flush=True)
                    continue
                frontier.append((u, sc))                       # (url, confidence) → sorted highest-first next round
                new_routes += 1
        if n_render_fail:                                     # coverage loss — say it, don't swallow it
            failed_render += n_render_fail
            print(f"[crawl] ⚠️ {n_render_fail}/{len(round_urls)} pages FAILED to render (walled/dead/empty) — "
                  f"their events are UNSEEN this run", flush=True)
        print(f"[crawl] {start_url[:50]} | round: {len(round_urls) - n_render_fail} pages → +{new_events} events, "
              f"+{new_routes} to follow (blocked {n_route_blocked} noise) | total events={len(events)} "
              f"visited={len(visited)} frontier={len(frontier)}", flush=True)

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
