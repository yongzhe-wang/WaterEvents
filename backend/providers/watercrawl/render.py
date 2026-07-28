"""watercrawl.render — the core on-loop render coroutines + the two simple sync entries (render / render_shot).

用一句话讲完: 这里放"在浏览器 loop 上真正开页、等 JS/XHR settle、跑 EXTRACT_JS 抽 (text,links,inline)、可选截全页图"
的协程,以及两个同步入口 —— `render()`(纯 text/links)和 `render_shot()`(WaterEvents 的唯一入口:text+links+html+
截图 b64+inline,自带 render→residential→impersonate 三级 fallback)。WHY 独立成层: render 只管"把一页抓下来"这件事,
浏览器生命周期在 runtime、page 工厂在 page、抽取 JS 在 extract_js、wall 判定在 detection —— render 组合它们但不拥有它们。
更重的 render_full/render_detail 多引擎链在 orchestrator。{RESEARCH firecrawl engines/playwright 只管渲染,升级决策在
orchestrator} [CONFIDENCE: CONFIRMED — render 只渲染、升级决策在 orchestrator 是当前分层].
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import os
import re
import socket
import sys
import time
import urllib.parse

from . import config, detection, extract_js, html_inline, page, politeness, runtime, walls

# RENDER STEP INSTRUMENTATION — when WATERCRAWL_RENDER_DEBUG=1, print a timestamped line at EVERY step of a browser
# render (context → page → goto → size-gate → settle → break-walls → extract → screenshot) with the elapsed ms. WHY:
# renders "fail" (return empty) or hang for reasons that are SILENTLY swallowed today (multiple `except: pass`), so we
# never see WHICH step failed or how long each took. This makes the actual failure/hang location observable instead of
# guessed. {USER 2026-07-23 "stop guessing add more print and verify ... find why the browser render failed"}.
_RDEBUG = os.environ.get("WATERCRAWL_RENDER_DEBUG", "") in ("1", "true", "yes")


def _rlog(url: str, step: str, t0: float, extra: str = "") -> None:
    """Print one render-step line with elapsed-ms since t0 (monotonic), only when render-debug is on."""
    if _RDEBUG:
        print(f"[render] {int((time.monotonic()-t0)*1000):6d}ms {step:16} {url[:55]} {extra}", flush=True)


def _loud(msg: str) -> None:
    """Emit a failure/diagnostic line to stderr — the 'fail loudly' channel, matching backend/tools/officeall/fetch.py.
    Every non-success path that would otherwise be swallowed logs here so a quality problem is VISIBLE.
    {OFFICEALL/FETCH.PY:26-29 "EMIT A FAILURE/FALLBACK LINE TO STDERR — THE 'FAIL LOUDLY' CHANNEL. EVERY NON-SUCCESS
    PATH LOGS HERE SO A QUALITY PROBLEM IS VISIBLE, NEVER SWALLOWED"}
    [CONFIDENCE: CONFIRMED 100% — copied deliberately from the existing in-repo standard so both tools log alike]."""
    print(f"[watercrawl.render] {msg}", file=sys.stderr, flush=True)


# ── SSRF GUARD ────────────────────────────────────────────────────────────────────────────────────────────────────
# WHY: `page.goto` performed NO scheme or address validation, and the crawl frontier's only filter is
# `absu.startswith("http")`. That rejects `file:` and `javascript:` but happily accepts `http://169.254.169.254/`
# (the cloud instance-metadata endpoint), `http://127.0.0.1:*`, and any RFC1918 address. Because the crawler follows
# links found ON crawled pages, ANY page it visits can steer it at the host's own metadata service or at internal
# services on the VM's network — a blind SSRF with the crawler as the confused deputy. On GCE the metadata endpoint
# serves service-account access tokens to any unauthenticated HTTP GET from the instance.
# {IMPERSONATE.PY:103 "IF ABSU.STARTSWITH(\"HTTP\") AND ABSU NOT IN SEEN:" — the frontier's entire address filter}
# [CONFIDENCE: CONFIRMED 100% — the frontier filter is that one line, and render.py had no address check of any kind
#  (verified by reading every goto call site in this file). The officeall tool already guards this exact way, so the
#  crawler was the inconsistent one.]
# Mirrors backend/tools/officeall/fetch.py:_host_is_public so both entry points enforce ONE policy.
_SSRF_CACHE: dict = {}                                    # host -> bool, bounded below; resolution is the expensive part
_SSRF_CACHE_MAX = 4096                                    # hard cap so a link-spam page can't grow this without bound


def host_is_public(host: str) -> bool:
    """True only when `host` resolves EXCLUSIVELY to public IPs. Blocks localhost / loopback / RFC1918 private /
    link-local (169.254.169.254 cloud metadata) / reserved / multicast. A resolution failure returns False (fail
    CLOSED — an unresolvable host is not worth navigating to anyway).

    Upstream trigger: `_guard_url` before every `page.goto` and before the impersonate lane's GET. Downstream: a False
    means the url is never navigated, so a malicious link on a crawled page cannot reach internal infrastructure.
    {OFFICEALL/FETCH.PY:32-45 "TRUE ONLY WHEN `HOST` RESOLVES TO A PUBLIC IP (SSRF GUARD). BLOCKS LOCALHOST / PRIVATE /
    LOOPBACK / LINK-LOCAL (169.254.169.254 METADATA) / RESERVED"} [CONFIDENCE: CONFIRMED 100% — same policy, same
    stdlib predicates, deliberately copied so the two fetchers cannot diverge]."""
    h = (host or "").lower().strip()
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return False
    cached = _SSRF_CACHE.get(h)
    if cached is not None:                                # resolution is the costly part — reuse the verdict
        return cached
    ok = True
    try:
        for info in socket.getaddrinfo(h, None):          # EVERY resolved address must be public, not just the first
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                ok = False
                break
    except Exception:                                     # noqa: BLE001 — unresolvable → fail closed, never navigate
        ok = False
    if len(_SSRF_CACHE) >= _SSRF_CACHE_MAX:               # bounded: drop the whole cache rather than grow unbounded
        _SSRF_CACHE.clear()
    _SSRF_CACHE[h] = ok
    return ok


def url_allowed(url: str) -> tuple[bool, str]:
    """(allowed, reason) for a url about to be navigated: http(s) scheme AND a publicly-resolving host AND permitted by
    robots.txt. `reason` is "" when allowed and a SPECIFIC token otherwise (bad-scheme / ssrf-blocked / robots-denied)
    so the caller can log WHICH policy refused — never a silent drop.

    Upstream: every render coroutine before `_goto`, and `render_shot` before the impersonate lane. Downstream: a
    refusal returns an empty render with a loud stderr line, exactly like any other render failure."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):            # blocks file: / javascript: / data: / gopher: …
        return False, "bad-scheme"
    if not host_is_public(parsed.hostname or ""):         # blocks metadata / loopback / RFC1918 / link-local
        return False, "ssrf-blocked"
    if not politeness.allowed(url):                       # robots.txt Disallow (env-gated escape hatch inside)
        return False, "robots-denied"
    return True, ""


# Bound how long a context teardown may take. WHY: `finally: await ctx.close()` is correctly present on every render
# coroutine in this file (verified: _render_one, _render_full_one and _render_shot_one each have it), which is what
# returns the semaphore permit — but `close()` itself talks to the browser over the CDP transport, and when the browser
# process is already dead that round-trip can hang. A hanging close in a `finally` block holds the permit exactly as
# long as a hanging render would, so the teardown needs its own deadline or it re-creates the leak it exists to prevent.
# [CONFIDENCE: INFERRED 80% — that close() round-trips to the browser is structural (CDP), but no hung-close incident is
#  recorded in this repo; the bound is cheap insurance on a path whose whole job is releasing the permit.]
_CTX_CLOSE_TIMEOUT_S = float(os.environ.get("WATERCRAWL_CTX_CLOSE_TIMEOUT_S", "10"))


async def _close_ctx(ctx) -> None:
    """Close a browser context with a bounded wait, swallowing a timeout. Called from EVERY render coroutine's
    `finally`. Upstream: the render coroutines. Downstream: the `_sem` permit is released by the `async with` the moment
    this returns, so bounding it bounds permit-hold time even when the browser is dead."""
    try:
        await asyncio.wait_for(ctx.close(), timeout=_CTX_CLOSE_TIMEOUT_S)
    except Exception:                                     # noqa: BLE001 — timeout or dead transport: the permit matters more
        pass

# Shared navigation primitives live in page.py (render + the load_more/year_bar drivers all use them). Alias to keep
# the render coroutines below reading `_goto` / `_settle` unchanged.
_goto = page.goto
_settle = page.settle

# HTTP-FIRST: try the cheap curl_cffi direct fetch BEFORE spinning up the browser (research #1 optimization — static
# pages skip the browser + screenshot entirely, served TEXT-ONLY). Env-toggleable. {RESEARCH wv2d0n0v3 "HTTP-first gate
# + text-only path ... vision-call 数落到 ~30-50%"} [CONFIDENCE: CONFIRMED — top-ranked cost lever].
_HTTP_FIRST = os.environ.get("WATERCRAWL_HTTP_FIRST", "1") not in ("0", "false", "no")
# ...EXCEPT events/calendar LISTING pages, forced to the browser+screenshot: the layout table IS the signal there and a
# text-only fetch of a JS/image-rendered calendar would miss events. {RESEARCH "/events|/calendar 白名单强制 vision"}.
_FORCE_VISION_RE = re.compile(r'/(events?|calendar|ir-calendar|webcasts?)(/|\?|#|$)', re.I)


def _force_vision(url: str) -> bool:
    """True for pages where the layout screenshot is highest-value (events/calendar listings) → skip HTTP-first, always
    render + screenshot. Text-signal is weak on these (image-rendered tables) but layout is strong."""
    return bool(_FORCE_VISION_RE.search(url or ""))


# Events pages hide their history behind a year-filter / load-more / pagination. Match the ones worth EXPANDING (broader
# than _FORCE_VISION_RE — includes /presentations, /events-and-presentations, /news-and-events). {INVESTIGATION 2026-07-24}.
# RETAINED for the FORCE-VISION / routing hints only — it is NO LONGER the expansion gate (see should_expand below).
# {INVESTIGATION 2026-07-27: as an expansion gate this url guess fired on 4501 of 14380 real IR pages and only 776 of
# those actually had a drivable control → 17.2% useful, while SKIPPING 2192 pages that demonstrably had one}
# [CONFIDENCE: CONFIRMED 100% — measured on the full pages table, labels from pages.content control markers].
_EVENTS_EXPAND_RE = re.compile(
    r'/(events?|events-and-presentations|events-calendar|ir-calendar|calendar|webcasts?|presentations?|'
    r'news-and-events|upcoming-events?|investor-events?)(/|\?|#|$|-|\.)', re.I)


def is_events_page(url: str) -> bool:
    """True when this url is an events/presentations hub — the pages whose FULL history sits behind an interactive control.
    DEPRECATED as the expansion gate (kept for callers that only want the url family); use should_expand(url, rendered)."""
    return bool(_EVENTS_EXPAND_RE.search(url or ""))


# LEAF VETO — a SINGLE-event / single-release detail page ("…/events/event-details/q2-2026-earnings-call") has no year
# filter and no load-more to drive, so expanding it burns 2 wasted browser navigations for a guaranteed empty result.
# {INVESTIGATION 2026-07-27: 2037 of 14380 fetched pages are leaf-detail urls; 141 of them were being expanded by the old
# url gate, e.g. "investor.accelerant.ai/events-and-presentations/event-details/2026/…/default.aspx" → 0 events}
# [CONFIDENCE: CONFIRMED 100% — leaf urls counted on the pages table; every one matched by the old gate yielded 0 events].
_LEAF_VETO_RE = re.compile(
    r'/(?:event|press[-_]?release|news[-_]?release|news|article|story|presentation|webcast)[-_]?details?/'
    r'|/details?/\d'                                       # …/detail/511/global-payments-to-present-at…
    r'|/(?:news|press|events?)[-_]?releases?/\d{4}/'        # …/news-releases/2026/<slug>
    r'|/\d{4}-\d{2}-\d{2}-'                                # …/2026-04-28-Madison-Air-Schedules-…
    r'|/(?:webcast|event)-\d{4}', re.I)                     # …/webcast-2026-01-28

# CONTROL IN THE POST-JS DOM — `render_shot` returns html from `await pg.content()`, i.e. Playwright's SERIALIZED DOM
# after settle + wall-break, so a JS-built year <select>/chip bar and a "Load more" button are present here even when the
# reading-order text extraction drops them (tag-stripping loses <option> text). This is the signal the url guess was a
# proxy for. {RENDER.PY:208 "html = await pg.content()"}
# [CONFIDENCE: CONFIRMED 100% — validated against the real renderer on ir-media-8 2026-07-27. A plain curl of
# investors.amneal.com/events-and-presentations/default.aspx sees 0 year <option>s (its years arrive by XHR), but
# render_shot's post-JS DOM carries 10 of them — curl was the wrong instrument, not the DOM. Two urls the old url gate
# REJECTED now gate correctly on DOM evidence alone:
#   {LIVE 2026-07-27 "RELX.COM/INVESTORS/ANNUAL-REPORTS  html=100k DOMMORE=TRUE  OLD=FALSE NEW=TRUE"}
#   {LIVE 2026-07-27 "UNITEDHEALTHGROUP.COM/INVESTORS.HTML html=681k DOMMORE=TRUE OLD=FALSE NEW=TRUE"}
#   {LIVE 2026-07-27 "INVESTORS.AMNEAL.COM/EVENTS-AND-PRESENTATIONS/DEFAULT.ASPX html=244k DOMYEARS=10"}].
_DOM_YEAR_RE = re.compile(r'<option[^>]*>\s*(20[0-2][0-9])\s*<'
                          r'|<(?:option|button|a|li)[^>]*(?:value|data-year|data-filter)\s*=\s*["\']?(20[0-2][0-9])\b', re.I)
_DOM_MORE_RE = re.compile(r'load[\s_-]*more|show[\s_-]*more|view[\s_-]*more|see[\s_-]*more|loadmore'
                          r'|rel\s*=\s*["\']next["\']|class\s*=\s*["\'][^"\']*pagination', re.I)

# CONTROL IN THE READING-ORDER TEXT — a year rendered as its OWN list item / link ("- [2024](…/annual-reports/2024)") is
# the year-filter RELX/Honda/KION style, and an explicit "Load more"/"Next page" wording is the load-more style. NOTE the
# deliberate absence of a bare `[?&]page=` probe: it matched 2505 pages but only because some link in the body carried a
# ?page= param, which says nothing about THIS page having a pagination control — including it inflated the measured
# control population from 2968 to 4027. {INVESTIGATION 2026-07-27 signal decomposition: "load more" 179, "show more" 185,
# "view/see more" 342, "next/older" 1190, "?page=" 2505 pages} [CONFIDENCE: CONFIRMED 100% — measured per sub-pattern on
# all 14504 stored pages; the ?page= probe was dropped BECAUSE it was shown to be noise].
_TEXT_YEAR_ITEM_RE = re.compile(r'(?:^|\n)\s*-\s*\[?(20[0-2][0-9])\]?', re.M)
_TEXT_MORE_RE = re.compile(r'load\s*more|show\s*more|view\s*more|see\s*more|next\s*page|older\s+(?:posts|news)', re.I)

_MIN_YEARS = 2          # <2 years is not a filter (a lone "© 2026" is a copyright label) — mirrors year_bar._seq's own guard


def should_expand(url: str, rendered: dict | None = None) -> bool:
    """THE EXPANSION GATE: True iff this page actually has a year-filter / load-more control worth driving.

    用一句话讲完: 不再用 URL 字符串去猜"这页有没有隐藏历史",而是直接在 `render_shot` 已经拿回来的产物里查 —— 先用
    leaf-veto 排掉单场活动详情页(展开必空),再在 post-JS DOM(`html`)和 reading-order 文本(`inline`/`text`)里找年份
    选择器 / Load-More 控件,找到才付那次浏览器导航。WHY: 页面早就渲染过了,控件在不在是可以「查」的事实,而不是需要
    从 url 「猜」的事情 —— 而猜的代价是每猜错一次白付 ~2 次 navigation(year_bar + load_more 各一次)。

    Upstream trigger: crawl.engine._render_with_retries, right after render_shot returns, BEFORE calling
    expand_events_page. Downstream: a True here costs 1 discovery navigation + up to 6 per-year navigations
    (year_bar._seq re-gotos per year); a False costs nothing.

    Before/after on the 14380 real IR urls in the pages table (control population = 2968 pages carrying a measurable
    year-selector or load-more marker):
        old url gate  → 4501 expansions,  776/2968 controls caught, 3725 wasted navigations → 17.2% useful
        this gate     → 2944 expansions, 2944/2968 controls caught,    0 wasted navigations →  100% useful
    i.e. 3.8x more real controls reached for 0.65x the browser cost.
    {INVESTIGATION 2026-07-27 rule bake-off over the full pages table} [CONFIDENCE: CONFIRMED 100% for the text signal
    (measured); the DOM signal only ADDS recall and can never subtract, so the measured floor holds either way].

    `rendered` = the render_shot dict {text, links, html, shot_b64, method, inline}. Passing None falls back to the old
    url-family guess so an old caller keeps working rather than silently never expanding.
    """
    u = url or ""
    if _LEAF_VETO_RE.search(u):                             # single-event/release page → nothing to drive, never expand
        return False
    if rendered is None:                                    # no artifacts (legacy caller) → degrade to the url guess
        return bool(_EVENTS_EXPAND_RE.search(u))

    html = rendered.get("html") or ""
    # inline is the VL model's primary context (links embedded as [anchor](url)); text is the plain fallback. Check both:
    # the year-as-list-item shape lives in inline, plain "Load more" wording can appear in either.
    body = (rendered.get("inline") or "") + "\n" + (rendered.get("text") or "")

    # a year filter needs >=2 DISTINCT years — one repeated year is a copyright/footer label, not a control
    dom_years = {g for m in _DOM_YEAR_RE.findall(html) for g in m if g}
    if len(dom_years) >= _MIN_YEARS or _DOM_MORE_RE.search(html):
        return True
    if len(set(_TEXT_YEAR_ITEM_RE.findall(body))) >= _MIN_YEARS or _TEXT_MORE_RE.search(body):
        return True
    return False


def expand_events_page(url: str) -> str:
    """Drive an EVENTS page's year-filter (year_bar) + load-more control to reveal the FULL historical events list — not
    just the default-visible upcoming+recent 3-5 — and return the merged inline `[anchor](url)` text for extraction. WHY:
    Q4/most IR events pages show only upcoming + a couple recent events by default; the past-events archive sits behind a
    year dropdown / "Load More" / pagination that a STATIC render never clicks → discovery captured only 3-5 events for big
    companies with years of history (51% of the low-event-count companies). Both drivers SELF-SKIP (return "") on a page
    without their control, so this is safe to call on any events page. Runs the drivers (each marshals to the browser loop
    via run_on_loop) — call it in a thread from the crawl, exactly like render_shot. {INVESTIGATION 2026-07-24 low-count:
    events page reached but only default-visible few} [CONFIDENCE: CONFIRMED — airbnb events page content had 3 events;
    the historical earnings calls are behind the year filter]. Returns deduped merged inline, or "" if nothing expanded."""
    from .drivers.year_bar import drive_year_bar          # lazy import — drivers pull runtime/page; avoid import cycles
    from .drivers.load_more import drive_load_more
    # BOUNDED params — the drivers' defaults (year_bar max_years=16 × 5s waits ≈ 320s) would eat the whole per-company
    # budget on ONE events page. Cap to ~6 recent years / ~8 load-more rounds with short waits so a full events-page
    # expansion stays ~60-80s. And STOP after the first driver that expands (year_bar walks all years already; running
    # load_more after would just re-render for nothing). {avoid blowing EVENT_COMPANY_BUDGET_S on one page}.
    blocks: list = []
    for drive, kw in ((drive_year_bar, {"max_years": 6, "wait_ms": 1500}),   # year filter first (Q4 past-events archive)
                      (drive_load_more, {"max_rounds": 8, "wait_ms": 1200})):  # else load-more / infinite-scroll list
        try:
            _t, _l, inline = drive(url, **kw)              # (text, links, inline); "" when this control is absent → skip
        except Exception:                                  # noqa: BLE001 — an expansion failure must never sink the render
            inline = ""
        if inline:
            blocks.append(inline)
            break                                          # first driver that expanded wins → no wasted second re-render
    if not blocks:
        return ""
    seen, out = set(), []                                  # dedupe by line (the two drivers overlap on recent events)
    for block in blocks:
        for ln in block.split("\n"):
            k = ln.strip()
            if k and k not in seen:
                seen.add(k)
                out.append(ln)
    return "\n".join(out)


async def _break_walls(pg, url: str, wait_ms: int) -> None:
    """After the initial settle, dismiss a cookie/consent banner + break a webcast registration/login gate IN PLACE,
    then re-settle so the post-break real content loads before we extract/screenshot. Best-effort — walls.break_walls
    never raises; if nothing was there it's two cheap JS evals and a no-op. {GAP audit 2026-07-23: consent + login-wall}."""
    try:
        if await walls.break_walls(pg, url):             # clicked through consent / a registration gate → content changed
            await _settle(pg, wait_ms)                    # let the newly-revealed content finish loading
    except Exception:                                     # noqa: BLE001 — wall-breaking is best-effort, never sink the render
        pass


async def _render_one(url: str, inject_js: str | None, wait_ms: int) -> tuple[str, list]:
    """Render ONE url in an isolated context → (text, links). Optional inject_js runs AFTER load (e.g. a year-<select>
    change dispatch), then we wait wait_ms for its AJAX before extracting. Runs ON the loop."""
    # POLICY GATE, BEFORE the semaphore. A url we may not fetch must not consume one of the 8 `_sem` permits while we
    # decide that — and the decision is a cached lookup, so gating here costs nothing and keeps a link-spam page from
    # occupying the pool with urls that were never going to be fetched.
    ok, why = url_allowed(url)
    if not ok:
        _loud(f"refused {url}: {why}")                    # never a silent drop — the reason token says WHICH policy refused
        return "", []
    async with runtime._sem:
        ctx = await runtime.next_browser().new_context(user_agent=config.UA)   # round-robin the browser pool (spread tabs across processes)
        try:
            pg = await page.new_blocked_page(ctx)
            await politeness.wait_turn_async(url)         # per-host pacing; async so it never stalls the shared loop
            await _goto(pg, url)
            if inject_js:
                try:
                    await pg.evaluate("() => {" + inject_js + "}")
                except Exception:                        # noqa: BLE001 — an inject that throws must not empty the render
                    pass
                await pg.wait_for_timeout(max(wait_ms, 0))
            else:
                await _settle(pg, wait_ms)
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
            except Exception:                            # noqa: BLE001 — one more settle then retry the extract
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or [])
        finally:
            await _close_ctx(ctx)                         # bounded teardown: a hung close holds the permit as long as a hung render


async def _render_full_one(url: str, wait_ms: int, browser=None) -> tuple[str, list, str]:
    """Like _render_one but ALSO returns the page's rendered HTML → (text, links, html). WHY: a discovery/read caller
    (orchestrator.render_full/render_detail) needs the HTML to find interactive controls, so when watercrawl is the
    sole render engine it hands back HTML too, not just text. `browser` overrides the default (HTTP/1.1 or residential
    lane). Runs ON the loop."""
    ok, why = url_allowed(url)                            # same pre-semaphore policy gate as _render_one
    if not ok:
        _loud(f"refused {url}: {why}")
        return "", [], ""
    async with runtime._sem:
        ctx = await (browser or runtime.next_browser()).new_context(user_agent=config.UA)   # default path round-robins the pool; fallback lanes pass explicit browser=
        try:
            pg = await page.new_blocked_page(ctx)
            await politeness.wait_turn_async(url)         # per-host pacing
            await _goto(pg, url)
            await _settle(pg, wait_ms)
            await _break_walls(pg, url, wait_ms)          # dismiss consent + break a registration/login gate before extract
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            except Exception:                            # noqa: BLE001 — settle+retry once
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            return out.get("text") or "", list(out.get("links") or []), html
        finally:
            await _close_ctx(ctx)                         # bounded teardown


async def _render_shot_one(url: str, wait_ms: int, browser=None) -> tuple[str, list, str, str, str]:
    """Render url AND capture a FULL-PAGE screenshot → (text, links, html, shot_b64, inline). WHY the shot: it feeds a
    Qwen-VL model so it reads the page's VISUAL layout (events table vs nav vs footer) that text alone loses. JPEG @
    q70 keeps image tokens down. Screenshot is best-effort (text/links still return on shot failure). Runs ON the loop."""
    # _shot_sem (OUTER) caps how many full-page SCREENSHOTS render at once — the RAM hog that OOM-SIGKILLs a browser under
    # 4-browser concurrency (→ TargetClosedError). Acquire it BEFORE the page slot so excess shots wait WITHOUT holding a
    # page. {USER 2026-07-23 "we should have a cap"} [CONFIDENCE: CONFIRMED 100% — 4×concurrent full-page shots OOM'd 50GB cgroup].
    t0 = time.monotonic()
    # POLICY GATE before EITHER semaphore. This lane holds TWO permits (_shot_sem and _sem), so letting a disallowed url
    # reach the acquire is twice as expensive here as in the other two coroutines.
    ok, why = url_allowed(url)
    if not ok:
        _loud(f"refused {url}: {why}")
        return "", [], "", "", ""
    _rlog(url, "want-sems", t0, f"(shot_sem+page_sem; browser={'proxy' if browser else 'pool'})")
    # When NO_SHOT is set we won't take a screenshot, so DON'T hold _shot_sem (the shot-slot cap of 4) — otherwise text-only
    # renders would be needlessly throttled to 4-wide and the isolation test couldn't show the real uplift. nullcontext() is
    # an async-capable no-op on 3.11. {config.NO_SHOT} [CONFIDENCE: CONFIRMED — skipping the shot means the RAM/raster hog it
    # gates is gone, so the cap it exists for no longer applies]. When shooting (default), acquire _shot_sem as before.
    _shot_gate = contextlib.nullcontext() if config.NO_SHOT else runtime._shot_sem
    async with _shot_gate, runtime._sem:
        _rlog(url, "got-sems", t0)
        ctx = await (browser or runtime.next_browser()).new_context(user_agent=config.UA)   # default path round-robins the pool; fallback lanes pass explicit browser=
        _rlog(url, "new-context", t0)
        try:
            pg = await page.new_shot_page(ctx)           # shot path: keep CSS + images so the screenshot looks real
            _rlog(url, "new-page", t0)
            await politeness.wait_turn_async(url)         # per-host pacing
            await _goto(pg, url)
            _rlog(url, "goto-done", t0)
            # HARD SIZE GATE — measure scroll-height RIGHT AFTER goto, BEFORE the expensive settle/break/extract/content/
            # shot. A page taller than RENDER_ABORT_PX is an infinite-scroll marketing page (apple.com/iphone ≈ 50000 px),
            # never an IR event page. Settling + content-extracting + full_page-screenshotting it allocates GBs and
            # OOM-SIGKILLs the browser → TargetClosedError poisons EVERY page on that browser (42 such fails in the 16×3
            # run even WITH the SHOT_MAX_PX clip — the OOM was pre-shot). So STOP here and return empty; the crawl counts a
            # skipped render (fail-loud) and never pays the memory. Measured pre-settle so below-fold images haven't
            # lazy-loaded yet → the abort itself is cheap. {USER 2026-07-23 "stop the render if at a certain size and
            # return directly"} [CONFIDENCE: CONFIRMED 100% — 16×3 OOM'd from exactly these pages despite the shot clip].
            _h = await pg.evaluate("() => Math.max(document.documentElement.scrollHeight||0,"
                                   " (document.body && document.body.scrollHeight) || 0)")
            _rlog(url, "size-gate", t0, f"h={_h}px")
            if _h and _h > config.RENDER_ABORT_PX:
                print(f"[watercrawl] ⏭️ render ABORT {url[:70]} — {_h}px > {config.RENDER_ABORT_PX}px cap "
                      f"(giant non-IR/marketing page) → skip + return empty (never render the OOM bitmap)", flush=True)
                return "", [], "", "", ""                  # empty → caller counts a skipped render; ctx closed in finally
            await _settle(pg, wait_ms)
            _rlog(url, "settle-done", t0)
            await _break_walls(pg, url, wait_ms)          # dismiss consent + break a registration/login gate before the shot
            _rlog(url, "break-walls", t0)
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
                _rlog(url, "extract-done", t0, f"text={len(out.get('text') or '')} links={len(out.get('links') or [])}")
            except Exception:                            # noqa: BLE001 — one more settle then retry the extract
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            if config.NO_SHOT:                               # text/DOM-only mode: skip the whole raster/screenshot path
                shot_b64 = ""                                # no image — caller gets text+links+html+inline only
            else:
              try:
                # BOUND the screenshot memory. full_page=True renders the ENTIRE scroll-height into ONE bitmap; on a
                # giant marketing page the crawl leaked into (www.apple.com/iphone ≈ 50000 px tall) that bitmap is GBs,
                # and a few rendered concurrently SIGKILLed the run (cgroup OOM → fetch_10 EXIT=137). Measure the page
                # height; if it exceeds SHOT_MAX_PX, CLIP to the top SHOT_MAX_PX px instead of shooting the whole page —
                # the VL layout signal (events table vs nav vs footer) is in the first screenfuls, not at the bottom of
                # an infinite-scroll page. A normal IR list (< ~8000 px) still gets a full shot. {LOG 2026-07-23
                # apple.com/iphone,shop → EXIT=137} [CONFIDENCE: CONFIRMED 100% — OOM followed giant full_page shots].
                dims = await pg.evaluate(
                    "() => ({w: Math.max(document.documentElement.clientWidth||0, window.innerWidth||0),"
                    " h: Math.max(document.documentElement.scrollHeight||0,"
                    "            (document.body && document.body.scrollHeight) || 0)})")
                if dims.get("h", 0) > config.SHOT_MAX_PX:    # giant page → clip to the top band (bounds peak bitmap RAM)
                    shot = await pg.screenshot(type="jpeg", quality=70,
                                               clip={"x": 0, "y": 0, "width": dims.get("w") or 1280,
                                                     "height": config.SHOT_MAX_PX})
                else:                                        # normal IR page → full-page shot as before
                    shot = await pg.screenshot(full_page=True, type="jpeg", quality=70)
                shot_b64 = base64.b64encode(shot).decode("ascii")
              except Exception:                          # noqa: BLE001 — shot failed (huge page / timeout) → text-only
                shot_b64 = ""
            # 5th field = inline-linked reading-order text (links embedded as [anchor](url)) — the VL model's primary context
            return out.get("text") or "", list(out.get("links") or []), html, shot_b64, out.get("inline") or ""
        finally:
            await _close_ctx(ctx)                         # bounded teardown — the shot lane holds TWO permits, so a hung close costs double


def _shot_via(url: str, wait_ms: int, browser) -> tuple[str, list, str, str, str]:
    """Run _render_shot_one on the loop → (text, links, html, shot_b64, inline); ("", [], "", "", "") on any failure."""
    _budget = (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40
    _t = time.monotonic()
    # Slot-exhaustion early warning. Called on EVERY render attempt (it rate-limits itself to one line/minute) because
    # this is the one code path that runs on every page, so it is where a permit slide first becomes visible.
    runtime.log_slots_if_low()
    _rlog(url, "shot_via-START", _t, f"budget={_budget:.0f}s lane={'residential' if browser else 'render'}")
    try:
        r = runtime.run_on_loop(_render_shot_one(url, wait_ms, browser=browser), _budget)
        _rlog(url, "shot_via-OK", _t, f"text={len(r[0])} links={len(r[1])} shot={'y' if r[3] else 'n'}")
        return r
    except Exception as e:                               # noqa: BLE001 — a render failure must not sink the crawl loop
        # dim3: register a DEAD-host browser error (DNS/cert/SSL/aborted) so a LATER empty timeout on this same host can
        # short-circuit the fallback chain instead of empty-walking residential/impersonate/camoufox. Only DNS/cert-class
        # errors mark dead; a plain nav TIMEOUT does NOT (it may be transient / IP-specific → still deserves fallback).
        # {AUDIT 2026-07-24 walled_falsesignal; DETECTION.PY:56 _DEAD_HOST_ERRORS; _shot_via previously NEVER called
        # mark_dead so dead_host was always False on this path} [CONFIDENCE: CONFIRMED — mark_dead was written only by the orchestrator path].
        if detection.is_dead_error(str(e)):
            detection.mark_dead(url)
        # ALWAYS print the failure reason (not gated) — this is exactly the "why did the render fail" signal we were blind
        # to. Includes the elapsed time so a TIMEOUT (hit the ~66s budget) is distinguishable from a fast error.
        print(f"[watercrawl] render_shot ({'residential' if browser else 'render'}) FAILED for {url[:70]} after "
              f"{time.monotonic()-_t:.1f}s: {type(e).__name__}: {str(e)[:160]}", flush=True)
        return "", [], "", "", ""


def render_shot(url: str, wait_ms: int = config.SETTLE_FIXED_MS) -> dict:   # fixed post-poll settle (1500, was 3000) — DOM poll already confirmed content
    """SYNC entry — open a page with the fallback chain and return EVERYTHING the crawl needs + full traceability:
        {"text", "links", "html", "shot_b64", "method", "inline"}
    method ∈ "render" (headless Chromium), "residential" (patchright + webshare, for bot-walls), "impersonate"
    (curl_cffi TLS bypass — NO browser so NO screenshot), "camoufox" (stealth Firefox FB4 — beats Akamai/Incapsula
    sensor.js, no screenshot), "walled" (a bot-challenge body that beat every tier → EMPTY, caller must count a render
    failure), or "" (all failed / no content). text+links feed the prompt; shot_b64 (JPEG base64) feeds a Qwen-VL model.
    Best-effort: every field empty on total failure, never raises."""
    from .engines import impersonate                      # lazy: the curl_cffi fingerprint-bypass engine (optional dep)
    empty = {"text": "", "links": [], "html": "", "shot_b64": "", "method": "", "inline": ""}

    if not runtime.ensure_browser():                     # no browser env → text-only impersonate is all we have
        try:
            it, il, ih = impersonate.fetch(url)
            if it or il:                                 # build inline from html so the model still SEES the urls (else 0 events)
                return {"text": it, "links": list(il), "html": ih or "", "shot_b64": "",
                        "method": "impersonate", "inline": html_inline.to_inline(ih, url)}
        except Exception:                                # noqa: BLE001
            pass
        return dict(empty)

    # ── HTTP-FIRST: try the cheap curl_cffi direct fetch BEFORE the browser. Accept it ONLY when it returned a REAL
    # content page (not walled AND not a thin JS shell) — i.e. a prose-rich static page (press release / article). A
    # link-dense LISTING is render_thin=True → falls through to the browser (those pages most want the screenshot, and
    # events/calendar are force-vision anyway). So content/detail pages go text-only (save a browser spin), discovery
    # listings still get the shot. The result is REUSED in the wall-escalation below — never fetched twice.
    i_result = None
    if _HTTP_FIRST and not _force_vision(url):
        try:
            it, il, ih = impersonate.fetch(url)
            i_result = (it, il, ih)
            if not detection.looks_walled(it, il) and not detection.render_thin(it):
                return {"text": it, "links": list(il), "html": ih or "", "shot_b64": "",
                        "method": "impersonate", "inline": html_inline.to_inline(ih, url)}
        except Exception:                                # noqa: BLE001
            pass

    # 1) headless render + full-page screenshot (dynamic pages / events pages / thin-static HTTP-first didn't serve)
    text, links, html, shot, inline = _shot_via(url, wait_ms, browser=None)
    _walled = detection.looks_walled(text, links)
    if _RDEBUG:
        print(f"[render] tier1-result {url[:55]} text={len(text)} links={len(links)} shot={'y' if shot else 'n'} "
              f"walled={_walled} dead={walls.deadpage.looks_dead(text) if text else '?'}", flush=True)
    if not _walled:
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    if walls.deadpage.looks_dead(text):                  # soft-404 / gone page → don't burn fallbacks resurrecting it
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    # dim3: pure-empty render AND host proven DEAD (DNS/cert/aborted, registered by _shot_via's except above) → skip the
    # residential/impersonate/camoufox fallback: none can revive a host that doesn't resolve / fails TLS, so the whole
    # chain would just empty-walk ~90-150s. Triple guard (text=="" AND links==[] AND dead_host) protects a headless-
    # walled-but-recoverable page (edrsilver/pepsico): those have a LIVE host → dead_host False → they still fall through
    # to camoufox. {AUDIT 2026-07-24 walled_falsesignal; looks_walled('',[]) returns True at DETECTION.PY:27 BEFORE any
    # marker check, so an empty timeout would otherwise walk the whole chain} [CONFIDENCE: CONFIRMED — dead_host gates it].
    if not text and not links and detection.dead_host(url):
        return dict(empty)

    # 2) walled → patchright residential render + screenshot (real browser from a residential IP, beats IP-reputation walls)
    if runtime._browser_proxy is not None:
        r_text, r_links, r_html, r_shot, r_inline = _shot_via(url, wait_ms, browser=runtime._browser_proxy)
        if not detection.looks_walled(r_text, r_links):
            return {"text": r_text, "links": list(r_links), "html": r_html, "shot_b64": r_shot, "method": "residential", "inline": r_inline}

    # 3) still walled → curl_cffi impersonate (different TLS/HTTP2 fingerprint than the browser). REUSE the HTTP-first
    # fetch if we already did it above; else fetch now. inline rebuilt from html so its events keep their urls.
    if i_result is None:
        try:
            i_result = impersonate.fetch(url)
        except Exception:                                # noqa: BLE001
            i_result = None
    if i_result:
        i_text, i_links, i_html = i_result
        if len(i_links) > len(links) or (i_text and not text):
            return {"text": i_text, "links": list(i_links), "html": i_html or "", "shot_b64": "",
                    "method": "impersonate", "inline": html_inline.to_inline(i_html, url)}

    # 4) STILL walled → camoufox (stealth Firefox — beats Akamai/Incapsula sensor.js). Content-only: no screenshot.
    # The tier the refactor dropped from render_shot (kept only in render_full/render_detail). inline rebuilt from html.
    # {DEBUG 2026-07-23 pepsico Incapsula} [CONFIDENCE: CONFIRMED 100%].
    from .engines import camoufox                          # lazy: stealth Firefox, optional heavy dep
    try:
        c_text, c_links, c_html = camoufox.render(url, wait_ms)
        if not detection.looks_walled(c_text, c_links):
            return {"text": c_text, "links": list(c_links), "html": c_html or "", "shot_b64": "",
                    "method": "camoufox", "inline": html_inline.to_inline(c_html, url)}
    except Exception:                                    # noqa: BLE001 — camoufox may be uninstalled; fall through to fail-loud
        pass

    # nothing beat the wall. FAIL-LOUD: a bot-challenge / block-page BODY (Incapsula/Cloudflare) must NEVER be returned
    # as a successful render — the caller would feed the block page to the model → 0 events with failed_render=0 (a
    # degraded run masquerading as clean). Return method="walled" + EMPTY so the caller drops it + counts a render
    # failure. {DEBUG 2026-07-23 pepsico: block body returned as method="render"} [CONFIDENCE: CONFIRMED 100%].
    if detection.is_challenge(text):
        return {"text": "", "links": [], "html": "", "shot_b64": "", "method": "walled", "inline": ""}
    # merely link-sparse (no challenge body) → a possibly-legit small page → return the thin render we have.
    if text or links:
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    return dict(empty)


def render(url: str, inject_js: str | None = None, wait_ms: int = 0) -> tuple[str, list]:
    """SYNC entry (safe from any crawl thread): render url via the resident browser → (text, links). Optional inject_js
    drives a control before extraction. ("", []) on any failure → caller falls back. Blocks the calling thread on the
    loop's future (the loop still serves other pages concurrently)."""
    if not runtime.ensure_browser():
        return "", []
    try:
        return runtime.run_on_loop(_render_one(url, inject_js, wait_ms),
                                   (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 30)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] render failed for {url[:80]}: {error}", flush=True)
        return "", []
