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
from urllib.parse import urlsplit

from providers import watercrawl
from providers.qwen_llm import QwenClient

from .extract import extract_pages
from .trace import Tracer
from ..storage.urls import _canon, _event_key                        # _event_key=(title+date) identity dedup (matches db.py); _canon still used for frontier/route dedup

_USE_IMAGE = os.environ.get("EVENT_USE_IMAGE", "1") not in ("0", "false", "no")   # screenshot → needs a Qwen-VL model
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "60"))     # BFS page cap per company (a real IR tree is ~10-60 pages)
_BATCH = int(os.environ.get("EVENT_BATCH", "5"))            # pages rendered + sent to the LLM per round (parallel)
# Drop routes the model scored below this. The model scores a genuine IR event-section HIGH (0.8-1.0) and chrome
# (about / business-unit / account / legal / alerts) LOW — so a cheap threshold cleans up the bulk-routed nav junk
# that the exact-segment denylist misses (about_board, business_bank, ir_alert …). Root fix for "model dumps every nav
# link at 0.6 on a no-event page". {USER 2026-07-24 "smarter way ... chrome 低分"} [CONFIDENCE: CONFIRMED — 290.com.hk
# contact page bulk-routed 20 nav links; segment denylist leaked ~10 of them]. 0.35 keeps MID governance/hub routes.
_ROUTE_MIN_SCORE = float(os.environ.get("EVENT_ROUTE_MIN_SCORE", "0.35"))
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


# Deterministic EVENTS-PAGE BOOST. WHY: the dated events almost always live on the events/presentations/webcasts hub, but
# the VLM's route SCORE sometimes ranks a sec-filings/financials route ABOVE the events route — so under the per-company
# budget the crawl renders filings pages and hits the cap BEFORE it ever reaches the events page. Investigation of the
# 2668-run's zero-event companies found WRONG_SEED_PAGE (rendered SEC Form-4 lists, the events-calendar sitting in the
# footer never crawled) was the #2 root cause after bot-walls: nationalfuelgas/lla/gamestop all had a clearly-named
# events route one hop from the seed that was out-ranked. Bumping any events-pattern route to a high score forces the
# frontier to crawl it FIRST. {INVESTIGATION 2026-07-24 zero-event root-cause: WRONG_SEED_PAGE} [CONFIDENCE: CONFIRMED —
# 3 investigated companies rendered filings not events; the events URL was present in the page nav].
_EVENTS_BOOST_RE = re.compile(
    r'/(events?|events-and-presentations|events-calendar|ir-calendar|calendar|webcasts?|presentations?|'
    r'news-and-events|upcoming-events?|investor-events?)(/|\?|#|$|-|\.)', re.I)


def _events_boost(url: str, score: float) -> float:
    """Raise a route's frontier score to ≥0.95 when its URL clearly points at the events/presentations hub, so the
    frontier crawls it BEFORE sec-filings/financials under the per-company budget. Applied before the min-score gate so
    an events route the VLM under-scored still survives + jumps the queue. Non-events routes keep their VLM score."""
    return max(score, 0.95) if _EVENTS_BOOST_RE.search(urlsplit(url if url.startswith("http") else "https://" + url).path) else score


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

# EXTENSION-LESS document-server paths. WHY a SECOND regex: the ext regex above misses Q4/Sitecore document urls that
# serve a FILE at a UUID with NO extension — the server sets content-type=application/pdf, so the browser STILL downloads
# a huge doc. energytransfer.com/static-files/<uuid> rendered to 6,343,771 chars of extracted binary → the chunker split
# ONE such doc into 3172 blocks = ~3172 VLM calls, and a single company had 4 of them (~12k calls). These are document
# LEAVES, never IR pages — gate them from the FRONTIER (they stay valid event-material urls). {SMOKE 2026-07-24
# energytransfer /static-files/<uuid> 6.3M chars → 3172 blocks} [CONFIDENCE: CONFIRMED 100% — a served doc, not a page].
_DOC_PATH_RE = re.compile(r'/(static-files|content/dam|files/doc_financials|files/doc_downloads)/', re.I)


def _is_binary_route(url: str) -> bool:
    """True if this go-deeper url points at a downloadable document/media FILE — either by extension (pdf/pptx/xlsx/zip/
    mp3/mp4/…) OR by an extension-less document-server PATH (Q4 /static-files/<uuid>, /content/dam/). Such a url is an event
    LEAF — never RENDER it (the browser buffers the download → OOM / a 6.3M-char doc → thousands of chunk blocks). It stays
    a valid EVENT material url via _clean_urls; this only excludes it from the crawl FRONTIER, never from an event."""
    path = urlsplit(url if url.startswith("http") else "https://" + url).path   # query/fragment stripped for the ext test
    return bool(_BINARY_ROUTE_RE.search(path) or _DOC_PATH_RE.search(path))      # ext file OR extension-less doc path


# How many times to (re)try a render before giving up on a url. WHY retry: under the concurrent multi-browser load a
# heavy SPA hub page (abc.xyz/investor/earnings|news|events) that renders fine ALONE can TIME OUT — GOOGL lost 19/19
# deep pages to TimeoutError in one run, so it under-crawled to 9 events despite routing to them. render_shot swallows
# the timeout and returns empty; a retry (after a short backoff, when the browser pool has freed up) usually succeeds,
# turning a transient timeout into a real page instead of a permanent coverage hole. {USER 2026-07-23 "we also want to
# retry"; DEBUG GOOGL deep pages TimeoutError under load, render fine standalone} [CONFIDENCE: CONFIRMED 100%].
_RENDER_TRIES = int(os.environ.get("EVENT_RENDER_TRIES", "3"))

# BACKSTOP char cap on a rendered page before it goes to the VLM/chunker. WHY high (2M): a real IR page is < ~100k chars,
# and even the biggest LEGIT archive we crawl (fbpinvestor SEC-filings, 9687 links) is ~1.4M — which the user confirmed is
# worth chunking. But an extension-less DOCUMENT that slips the _DOC_PATH_RE frontier gate (a 6.3M-char PDF served as text)
# is NEVER a page — chunking it = thousands of VLM calls for one doc. So a page ABOVE this ceiling is skipped (traced, 0
# events, fail-loud) instead of chunked into oblivion. Set below the 6.3M doc, above the 1.4M legit archive. {SMOKE 2026-07-24
# energytransfer 6.3M doc → 3172 blocks; fbpinvestor 1.4M archive = legit} [CONFIDENCE: CONFIRMED — doc vs page threshold].
_MAX_PAGE_CHARS = int(os.environ.get("EVENT_MAX_PAGE_CHARS", "2000000"))

# Per-company wall-clock SAFETY CAP (seconds). NOT a quality/depth compromise: it does NOT lower max_pages, tokens, or
# input — it only stops a company that has run pathologically long (a browser-event-loop-deadlocked seed that render_shot
# can't recover, or a frontier that exploded to hundreds of low-value routes). Such a company is either hung (0 progress)
# or already past its real events (a large cap's disclosures are all found in the first rounds; rounds 20+ are marketing
# tail). Hitting the cap returns what was collected so the WORKER is freed for the next company instead of wedging the
# whole fleet on one pathological site. {USER 2026-07-23 12h-1000-run "build infra for parallelization ... this can work";
# DEBUG abc.xyz seed deadlocked a worker for 8min+} [CONFIDENCE: CONFIRMED — the deadlock is real; the cap bounds fleet waste].
_COMPANY_BUDGET_S = float(os.environ.get("EVENT_COMPANY_BUDGET_S", "600"))


async def _render_one(url: str) -> dict | None:
    """Open ONE url with watercrawl (in a thread — render_shot is sync + marshals to the browser loop). RETRIES an empty
    render up to _RENDER_TRIES times with a short backoff: a load-induced TimeoutError comes back empty, and a retry once
    the browser pool has freed up usually lands the page. Returns the render dict {url, text, links, html, shot_b64,
    method}, or None only after every attempt came back empty (genuinely walled / dead / persistently timing out)."""
    for attempt in range(_RENDER_TRIES):
        r = await asyncio.to_thread(watercrawl.render_shot, url)
        if r.get("text") or r.get("links"):                  # got real content → done (no wasted extra attempts)
            r["url"] = url
            # EVENTS PAGE → reveal the FULL history. A static render of an IR events page captures only the default-visible
            # upcoming+recent 3-5 events; the past-events archive sits behind a year-filter / "Load More" / pagination. For
            # events-page urls, drive those controls (year_bar + load_more, each self-skips if absent) and MERGE the expanded
            # inline so the extractor sees every year's dated events — not just the front page. Runs in a thread like
            # render_shot (the drivers marshal to the browser loop). {INVESTIGATION 2026-07-24: 51% of low-event-count
            # companies reached the events page but got only the default-visible few} [CONFIDENCE: CONFIRMED — airbnb events
            # page had 3 events in content, the historical earnings calls are behind the year filter].
            #
            # GATE = should_expand(url, r), NOT is_events_page(url). The url-only guess could not see whether a control
            # actually exists, so it expanded pages that had none (paying ~2 wasted navigations each) while skipping pages
            # that did. We already hold this page's post-JS DOM + reading-order text in `r`, so the control is a fact to
            # LOOK UP rather than a string to guess. {INVESTIGATION 2026-07-27 over the 14380 fetched pages: url gate =
            # 4501 expansions / 776 real controls / 3725 wasted navs (17.2% useful); artifact gate = 2944 expansions /
            # 2944 real controls / 0 wasted (100% useful)} [CONFIDENCE: CONFIRMED 100% — measured on the full pages table].
            if watercrawl.should_expand(url, r):
                try:
                    _expanded = await asyncio.to_thread(watercrawl.expand_events_page, url)
                except Exception:                            # noqa: BLE001 — expansion is best-effort, never sink the page
                    _expanded = ""
                if _expanded and len(_expanded) > len(r.get("inline") or ""):
                    r["inline"] = (r.get("inline") or "") + "\n" + _expanded   # static + expanded → extract sees ALL years
            return r
        # dim2: method=="walled" means a TRUE challenge body beat ALL 4 tiers (render→residential→impersonate→camoufox)
        # INSIDE this single render_shot — an outer retry re-runs the exact same known-failed chain with zero new tactics,
        # so exit now. STRICTLY method=="walled" only: a nav-timeout returns method=="" (indistinguishable from edrsilver's
        # first two attempts, which revive on the 3rd), so method=="" KEEPS the full _RENDER_TRIES loop. NEVER widen to
        # method=="" — that kills edrsilver on attempt 1. {AUDIT 2026-07-24 deadsite_retry; RENDER.PY:296 returns
        # method="walled" only after every tier failed} [CONFIDENCE: CONFIRMED — walled is terminal within one attempt].
        if r.get("method") == "walled":
            return None
        # A robots refusal is terminal for the same reason "walled" is — retrying re-runs a decision, not a chain — but
        # it must NOT be counted as a render failure, so it returns its own sentinel rather than None. See render.py's
        # gate for why: as an empty render it burned the full retry ladder and then parked live, event-producing hosts
        # in status='failed'. [CONFIDENCE: CONFIRMED 100% — sap.com and centrica.com were both parked this way.]
        # "url" IS REQUIRED. _to_page's first line is render["url"], so a sentinel without it raises KeyError, and the
        # page-task handler at the bottom of this file catches every Exception and sets render=None — which _harvest
        # then counts as failed_render. The robots refusal came back as a render failure by a completely different
        # route than before, and the production logs recorded 102 `page task error ... KeyError: 'url'` lines from
        # exactly the robots-disallowed hosts in the ~20 minutes this shipped.
        # {W*.LOG 2026-08-01 "[crawl] ⛔ page task error https://ir.prologis.com/news-events: KeyError: 'url'" ×102}
        # [CONFIDENCE: CONFIRMED 100% — my own bug, found by checking whether the fix had actually changed the numbers.]
        if r.get("method") == "robots-denied":
            return {"url": url, "method": "robots-denied", "text": "", "links": [], "html": "", "shot_b64": "",
                    "inline": ""}
        if attempt < _RENDER_TRIES - 1:                      # empty (timeout/walled/thin) → back off, then retry
            await asyncio.sleep(2.0 * (attempt + 1))         # 2s, 4s — let the browser pool drain before re-firing
    return None                                              # every attempt empty → real skip (counted fail-loud upstream)


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
                        client: QwenClient | None = None, trace_dir: str | None = None,
                        on_events=None, seeds: list[str] | None = None, gate=None) -> dict:
    """company URL → {"events":[...], "pages": N, "trace_dir": ...}. BFS close-loop, every page fully traced to disk.
    Events dedup by their first url; the frontier dedups by canonical url + stays on-site (routes only go_deeper).

    on_events: optional async callback (list[event]) -> awaitable, invoked with the NEW events found on EACH page as they
    are discovered — for INCREMENTAL persistence. WHY it matters: without it the worker only flushed after crawl_company
    RETURNED, so a company killed mid-crawl (a slow VLM page tripping the stall-watchdog) lost EVERY event it had already
    extracted (they were in the trace but never hit the DB) → 77% of companies showed 0 events + churned on re-crawl.
    Flushing per-page means a mid-crawl kill loses at most the one in-flight page; the idempotent ON CONFLICT flush makes
    the re-crawl merge, not duplicate. {USER 2026-07-23 "worker flush 只在最后一次性 ... 中途被杀 events 没落库 ... fix this"}."""
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
    # MULTI-FRONTIER seeds: start_url is the PRIMARY seed; `seeds` are EXTRA IR-entry URLs (a company's event_hubs —
    # its events / presentations / calendar sub-pages, found by ir_url_agent) crawled from round 0, all at max score 1.0.
    # WHY: an IR homepage often renders only 6-9 default-visible events; the full archive lives on a sibling events page
    # whose nav link a JS mega-menu never emitted as an <a href>, so the BFS never reached it (6-9-event root cause).
    # Seeding those sub-pages DIRECTLY guarantees they're rendered + year-dropdown-expanded regardless of nav extraction.
    # All seeds share ONE frontier → ONE dedup set (enqueued) + ONE max_pages budget (not N× budget — a single crawl,
    # one total page cap). {SCAN whn86f4f7 + USER 2026-07-25 "multiple url for one company so it start from multiple
    # frontier from beginning"} [CONFIDENCE: CONFIRMED — direct user directive to seed multiple frontiers].
    _all_seeds = [start_url] + [s for s in (seeds or []) if s]           # primary + extra IR-entry URLs (drop empties)
    frontier: list[tuple[str, float]] = [(s, 1.0) for s in _all_seeds]   # every seed max score → all crawled first, in parallel (batch-capped)
    visited: set[str] = set()
    # ANTI-REVISIT guard = every canonical url EVER queued OR visited. Gating frontier-adds on `visited` ALONE let the
    # SAME url (discovered on two different pages) get appended twice → rendered twice. `enqueued` makes a url enter the
    # frontier AT MOST ONCE, ever. Seeded from ALL seeds → _canon folds http/https + trailing-slash dup seeds into one.
    # {USER 2026-07-24 "make sure we dont revisit pages"} [CONFIDENCE: CONFIRMED 100%].
    enqueued: set[str] = {_canon(s) for s in _all_seeds}
    events: list[dict] = []
    seen_event: set[str] = set()
    # FAIL-LOUD counters — a page can silently drop out two ways: render came back empty (walled/dead) or the LLM call
    # HARD-failed (server down / GCP→RunPod network drop). Both otherwise look like "a page with 0 events". We count them
    # and shout at run end so a degraded run is NEVER mistaken for a complete one.
    # {USER 2026-07-23 "fail loudly is the core ... we dont want quality issue"} [CONFIDENCE: CONFIRMED 100% — directive].
    failed_render = 0
    skipped_robots = 0                                       # pages robots.txt told us not to fetch — declined, not failed
    failed_extract = 0
    # HARD extraction failures ONLY — the `_error` branch, where the page's events are LOST. Kept separate from
    # failed_extract, which ALSO counts `_partial` and `_route_error`; in those two the events were KEPT, so a caller
    # that treated failed_extract as "this scan produced nothing trustworthy" would be wrong. This counter is the one a
    # caller can act on: >0 means we rendered pages, called the VLM, and got nothing back for them.
    # WHY it had to be added: on 2026-07-28 the vLLM died and every extract returned
    # `APIConnectionError: Connection error.` The engine detected it correctly and shouted ~18,000 times, but the only
    # signal it exported was folded in with the harmless partial/route failures, so scan.py dropped the whole set and
    # worker.py called complete_work() — 6,304 queue units were re-armed as "scanned, nothing found" over 11h52m.
    # {W1.LOG 2026-07-29 "1628× ⛔ EXTRACT FAILED ... APIConnectionError: Connection error. — page's events LOST"}
    # {DB 2026-07-29 "6,304 of 6,310 units re-armed with last_event_count=0; 19,829 scan_log rows; 0 events"}
    # [CONFIDENCE: CONFIRMED 100% — both numbers measured live during the outage, not reconstructed.]
    extract_errors = 0
    # RESOURCE COUNTERS — feed the packing solver's C_R/C_V/hit_rate. vlm_calls = pages that actually hit the VLM;
    # vlm_skipped = pages the hash-gate short-circuited (unchanged). hit_rate = vlm_calls/(vlm_calls+vlm_skipped) is the
    # single knob that decides VLM demand → the incremental period T*. {USER 2026-07-26 "calculate render and vlm usage"}.
    vlm_calls = 0
    vlm_skipped = 0
    _t0 = time.time()                                         # per-company wall-clock start (for the safety cap below)

    # CONTINUOUS STREAMING (no round barrier). Keep `batch` page-tasks ALWAYS in flight per worker; the moment one
    # finishes, harvest it and immediately start the next frontier url. WHY not the old round model (render a batch of
    # `batch` → asyncio.gather WAITS for ALL of them render+VLM → next batch): ONE slow page (a heavy render or a slow
    # full-quality VLM call) stalled the whole batch and left the browser's render slots IDLE, starving the single GPU
    # (observed at 19% util). Streaming keeps the render pipeline FULL so it OVER-PRODUCES pages faster than the GPU
    # consumes them → with N CPU-isolated workers each holding `batch` in flight, total in-flight = N×batch ≫ the GPU's
    # ~8-12 concurrent full-quality VLM ceiling, so the GPU is the bottleneck (~100%), never the render. {USER 2026-07-23
    # "we have to actually be faster than the gpu to fully utilization ... 20 workers each 5 page parallel, browser-level
    # isolation"} [CONFIDENCE: CONFIRMED — the round-barrier gather was the GPU-starve].
    async def _render_then_extract(url: str):
        # render_shot self-times-out (run_on_loop total timeout = nav-timeout + settle + margin) and returns empty on
        # overrun — NO asyncio.wait_for budget here (an earlier one deadlocked: cancelling the to_thread coroutine can't
        # stop the underlying thread, which stayed blocked in render_shot; leaked threads drained the pool → frozen run).
        r = await _render_one(url)                             # browser render (self-times-out; retries inside)
        if r is None:                                          # walled/dead/empty → nothing to extract
            return url, None, None
        # Short-circuit BEFORE the page dict and the VLM. With only the "url" key restored this would still work, but
        # it would build an empty page and spend a VLM call proving that nothing is in it — on the binding resource.
        # _harvest reads the method and counts skipped_robots.
        # No _skipped marker: that flag means "the hash-gate saw identical content", and _harvest counts it as
        # vlm_skipped, which feeds the solver's hit_rate. A page we never fetched is not a hash-gate hit, and putting
        # it in that denominator would quietly bias T*. _harvest reads the method and returns before it looks at res.
        if r.get("method") == "robots-denied":
            return url, r, {"events": [], "routes": []}
        print(f"[crawl] · rendered {url[:60]} → VLM", flush=True)      # heartbeat: log advances so the stall-watchdog sees liveness
        _pg = _to_page(r)                                      # build the extract page dict once (need its size before the VLM)
        _n = len(_pg.get("page_text") or "")                  # rendered text length — a doc-sized page must NOT reach the chunker
        if _n > _MAX_PAGE_CHARS:                              # runaway document that slipped the frontier gate → skip, fail-loud
            print(f"[crawl] ⏭️ SKIP {url[:70]} — {_n} chars > {_MAX_PAGE_CHARS} cap (a served DOC, not an IR page) → 0 events", flush=True)
            return url, r, {"events": [], "routes": [], "_skipped": True}   # rendered but never sent to VLM → count as skip, not a call
        # HASH-GATE — the render→VLM decoupling point. VLM is ~7× costlier per op than render on one A5000 (~14.4s/call vs
        # ~2.1s/page) → it is the global binding bottleneck. gate(url, text) returns False when this page's sha256 matches
        # the stored pages.content_hash (unchanged since last scan) → SKIP the VLM extract entirely, the single biggest
        # VLM-saving lever (without it a weekly full sweep needs ~446 VLM-hr >> 168hr = infeasible). gate is wired ONLY for
        # incremental depth=1 (no routes to lose); full stays gate=None until route-caching lands (milestone 2). Fail-OPEN:
        # any gate error → extract (never skip real work on a bug). {USER 2026-07-26 "two bottleneck ... calculate usage for
        # parallel"; PLAN §hash-gate] [CONFIDENCE: CONFIRMED — VLM 7× costlier derived from measured C_R=1728, C_V=250].
        if gate is not None:
            try:
                _should_extract = await gate(url, _pg.get("page_text") or "")   # False → hash unchanged → skip VLM
            except Exception:                                # noqa: BLE001 — gate must never sink a page; fail-open to extract
                _should_extract = True
            if not _should_extract:
                print(f"[crawl] ⏭️ HASH-UNCHANGED → skip VLM {url[:60]} (content identical to last scan)", flush=True)
                return url, r, {"events": [], "routes": [], "_skipped": True}    # _skipped → counted vlm_skipped, not a call
        res = (await extract_pages([_pg], client=client, use_image=_USE_IMAGE))[0]   # VLM fires as the render lands
        print(f"[crawl] ✓ extracted {url[:55]} ({len(res.get('events') or [])} ev, {len(res.get('routes') or [])} rt)", flush=True)
        return url, r, res

    def _harvest(url, render, res) -> list:
        """Fold ONE finished page's events + routes into the shared state. RETURNS the NEW events found on this page so
        the caller can flush them to the DB incrementally (a mid-crawl kill then loses at most this one page)."""
        # nonlocal MUST include seen_event: `seen_event |= ekeys` is an augmented assignment that REBINDS the name, so
        # without this Python treats seen_event as a _harvest-local and every page raised UnboundLocalError → the crawl
        # crashed → the company was marked failed with 0 events (even when the VLM had extracted plenty). {DEBUG 2026-07-23}.
        nonlocal failed_render, failed_extract, extract_errors, seen_event, vlm_calls, vlm_skipped, skipped_robots
        if render is None:                                     # render failed (walled/dead/empty) — coverage loss, no VLM touched
            failed_render += 1
            return []
        # Counted apart from failed_render on purpose: this page was not attempted, so it is not coverage LOST, it is
        # coverage DECLINED. Folding it into failed_render is what made worker.py's `lost` predicate fire and park
        # robots-disallowed hosts in status='failed'. It still returns no events, so the scan reports 0 for this page —
        # it simply does not claim something broke. [CONFIDENCE: CONFIRMED 100% — see render.py's robots gate.]
        if render.get("method") == "robots-denied":
            skipped_robots += 1
            return []
        tracer.save_page(render["url"], render, res)           # full audit trail: content/shot/html/links/result/method
        if res.get("_skipped"):                                # hash-gate short-circuited this page → a SKIP, not a VLM call
            vlm_skipped += 1                                   # counts toward hit_rate denominator (VLM demand saved)
            return []
        if res.get("_error"):                                  # LLM hard-failed → NOT '0 events', it FAILED (fail-loud)
            failed_extract += 1
            extract_errors += 1                                # the ACTIONABLE count: this page's events are lost, not absent
            vlm_calls += 1                                     # the VLM WAS invoked (it errored) → still a call for C_V accounting
            print(f"[crawl] ⛔ EXTRACT FAILED {render['url'][:70]} — {res['_error']} — page's events LOST", flush=True)
            return []
        # PARTIAL / ROUTE-ONLY failures are NOT grounds to drop the page. `_partial` = some chunk blocks truncated but the
        # rest returned real events; `_route_error` = the (independent) routing call failed, which costs us depth from this
        # page but says nothing about the events already extracted. Both still count a failed_extract so the company ends
        # up status='incomplete' — we stay fail-loud about coverage without throwing away what we actually got.
        # {EXTRACT.PY _combine "THE RIGHT COST OF A ROUTING FAILURE IS "WE DON'T GO DEEPER FROM THIS PAGE", NEVER "THIS
        #  PAGE'S EVENTS ARE LOST""} [CONFIDENCE: CONFIRMED 100% — the two LLM jobs are gathered independently].
        if res.get("_partial"):
            failed_extract += 1
            print(f"[crawl] ⚠️ PARTIAL EXTRACT {render['url'][:70]} — {res['_partial']} — keeping "
                  f"{len(res.get('events') or [])} event(s) from the blocks that succeeded", flush=True)
        if res.get("_route_error"):
            failed_extract += 1
            print(f"[crawl] ⚠️ ROUTING FAILED {render['url'][:70]} — {res['_route_error']} — events kept, "
                  f"no frontier expansion from this page", flush=True)
        vlm_calls += 1                                         # a real extract fired on this rendered page → 1 VLM call
        new_ev: list = []
        for e in res["events"]:                                # dedup by (title+date) identity — MATCHES the DB dedup_key
            k = _event_key(e.get("title"), e.get("date"), e["urls"])   # so the same event across pages/chunks collapses in-memory too
            if not k or k in seen_event:                       # (was url-overlap → let same-event-different-url through as dups)
                continue
            seen_event.add(k)
            e["source_url"] = render["url"]                    # the PAGE this event was extracted from → events.source_url → frontend "Source page"
            events.append(e)
            new_ev.append(e)
        for rt in res["routes"]:                               # each route = {url, score}; gate then enqueue to frontier
            u, sc = rt["url"], rt.get("score", 0.5)
            ck = _canon(u)
            if not any(_same_site(u, s) for s in _all_seeds) or ck in enqueued:  # off ALL seed roots OR already seen → no revisit (multi-seed: a route on any seed's domain, e.g. off-host gcs-web events, stays in-scope)
                continue
            sc = _events_boost(u, sc)                          # events/presentations hub → force ≥0.95 so it crawls FIRST (before the gate, so a VLM-under-scored events route survives)
            if sc < _ROUTE_MIN_SCORE:                          # low-confidence chrome the model bulk-routed → drop it
                print(f"[crawl] 🚧 route BLOCKED (low score {sc:.2f}) {u[:80]}", flush=True)
                continue
            if _is_noise_route(u):                             # legal/careers/account hub → never an event page
                print(f"[crawl] 🚧 route BLOCKED (noise) {u[:90]}", flush=True)
                continue
            if _is_binary_route(u):                            # PDF/deck/audio FILE = event leaf, not a page to render
                print(f"[crawl] 🚧 route BLOCKED (binary file, not a page) {u[:90]}", flush=True)
                continue
            enqueued.add(ck)                                   # mark seen → this url can never be queued again
            frontier.append((u, sc))                           # (url, confidence) → picked highest-first on refill
        return new_ev                                          # this page's NEW events → caller flushes them incrementally

    in_flight: dict = {}                                       # asyncio.Task → url; kept FULL at `batch` per worker

    def _refill() -> None:
        # top up to `batch` in-flight page-tasks from the highest-confidence frontier urls (respecting max_pages)
        while frontier and len(in_flight) < batch and len(visited) < max_pages:
            frontier.sort(key=lambda t: -t[1])                 # PRIORITY: highest-confidence route first
            u, _score = frontier.pop(0)
            ck = _canon(u)
            if ck in visited:
                continue
            visited.add(ck)
            in_flight[asyncio.ensure_future(_render_then_extract(u))] = u

    _refill()
    while in_flight:
        if time.time() - _t0 > _COMPANY_BUDGET_S:              # pathological/hung company → stop, free the worker
            print(f"[crawl] ⏱ COMPANY-BUDGET {_COMPANY_BUDGET_S:.0f}s hit for {start_url[:60]} — stopping at "
                  f"{len(visited)} pages / {len(events)} events. Freeing worker.", flush=True)
            for t in in_flight:
                t.cancel()
            break
        # WAIT for the FIRST task to finish (NOT all — no barrier); 30s timeout keeps the budget check ticking if every
        # in-flight page is momentarily slow.
        done, _pending = await asyncio.wait(set(in_flight), return_when=asyncio.FIRST_COMPLETED, timeout=30)
        for t in done:
            url = in_flight.pop(t)
            try:
                _u, render, res = t.result()
            except asyncio.CancelledError:
                render, res = None, None
            except Exception as e:                             # noqa: BLE001 — one page task crashing must not sink the crawl
                print(f"[crawl] ⛔ page task error {url[:60]}: {type(e).__name__}: {e}", flush=True)
                render, res = None, None
            new_ev = _harvest(url, render, res)
            # Persist WHENEVER we rendered + ran the VLM (even with 0 events) — NOT only when new events appeared. WHY: the
            # hash-gate reads pages.content_hash to decide skip-vs-extract; if a rendered-but-0-event page never stored its
            # hash (the old `and new_ev` gate), the gate could never fire on it → it re-extracted EVERY cycle, burning the
            # binding VLM resource on an unchanged empty page — the exact waste the gate exists to kill. Gate-SKIPS
            # (_skipped) already have their hash from a prior scan → don't rewrite; render None → nothing rendered to store.
            # {AUDIT 2026-07-26: 0-event pages never cached content_hash → gate blind to them} [CONFIDENCE: CONFIRMED — the
            # old on_events was gated on new_ev, so a 0-event render skipped save_pages entirely].
            if on_events and render is not None and not (res or {}).get("_skipped"):
                try:                                           # flush per-page NOW (a mid-crawl kill loses ≤1 page) {USER "fix this"}
                    # pass the SOURCE PAGE (url + reading-order content) alongside → callback persists it (+content_hash) to
                    # the pages table: feeds the frontend "Source page" view AND the next scan's hash-gate. new_ev may be []
                    # → flush_events is a no-op, but save_pages STILL stores the content+hash. {USER 2026-07-24 "no source page"}
                    _page = {"url": render["url"], "content": render.get("inline") or render.get("text") or ""}
                    await on_events(new_ev, _page)
                except Exception as e:                         # noqa: BLE001 — a DB hiccup must not sink the crawl; events
                    print(f"[crawl] ⚠️ incremental flush failed ({type(e).__name__}: {e}) — kept for final flush", flush=True)
        _refill()                                              # immediately backfill the freed slot(s) — keep the pipe FULL

    tracer.save_summary(events, len(visited))
    # status = ok ONLY if nothing dropped. ANY render/extract failure → "incomplete" so the GCP caller can react
    # (retry the failed pages / alert) instead of trusting a partial event list as the whole truth.
    status = "ok" if (failed_render == 0 and failed_extract == 0) else "incomplete"   # skipped_robots deliberately absent: declining a page is a complete scan
    print(f"[crawl] DONE {start_url[:50]} — {len(events)} events over {len(visited)} pages. Trace: {run_dir}", flush=True)
    if status != "ok":                                        # LOUD run-level banner — a degraded run must be unmissable
        print(f"[crawl] ⚠️⚠️ INCOMPLETE RUN — {failed_extract} pages FAILED extraction (LLM/network), "
              f"{failed_render} pages FAILED render (walled/dead). Event list is PARTIAL — do NOT treat as complete.",
              flush=True)
    return {"events": events, "pages": len(visited), "trace_dir": run_dir,
            "status": status, "failed_extract": failed_extract, "extract_errors": extract_errors,
            "failed_render": failed_render,
            "skipped_robots": skipped_robots,
            "vlm_calls": vlm_calls, "vlm_skipped": vlm_skipped}   # → scan_log → C_R/C_V/hit_rate for the packing solver


def _slug_host(url: str) -> str:
    return (urlsplit(url if url.startswith("http") else "https://" + url).netloc or "run").replace(":", "_")


if __name__ == "__main__":                                  # manual: python3 -m agent.event_agent.crawl <url>
    import sys
    out = asyncio.run(crawl_company(sys.argv[1] if len(sys.argv) > 1 else "https://investors.example.com"))
    print(f"\n=== {len(out['events'])} events from {out['pages']} pages | trace: {out['trace_dir']} ===")
    for e in out["events"][:40]:
        print(" ", e["date"] or "—", "|", e["type"] or "—", "|", e["title"][:50], "|", e["urls"])
