"""Watercrawl ENGINE — resident headless-Chromium pool on a dedicated asyncio loop thread (WHY in __init__.py).

Design invariants:
  - ONE browser per worker process, launched lazily on first use, kept warm for the process lifetime.
  - ALL Playwright calls run on ONE dedicated event-loop thread — async_playwright objects are bound to the
    loop that created them, so crawl's ThreadPoolExecutor threads must NEVER touch them directly; they submit
    coroutines via run_coroutine_threadsafe and block on the returned future.
  - A Semaphore bounds CONCURRENT pages so peak memory stays inside the worker's limit (6×~60MB + ~400MB browser).
  - Best-effort: any launch/render failure returns empty ("", []) so the caller falls back to jina/firecrawl —
    self-hosted render must NEVER be a hard dependency that can sink a crawl.
"""
from __future__ import annotations

import asyncio
import os
import threading

_MAX_PAGES = int(os.environ.get("IR_WATERCRAWL_MAX_PAGES", "6"))
_NAV_TIMEOUT_MS = int(os.environ.get("IR_WATERCRAWL_NAV_TIMEOUT_MS", "22000"))
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_browser = None
_browser_h1 = None
_browser_proxy = None
_playwright = None
_playwright_stealth = None
_sem: asyncio.Semaphore | None = None
_dead = False

_EXTRACT_JS = """() => {
  const body = document.body ? document.body.innerText : '';
  const seen = new Set();
  const links = [];
  const annotated = [];
  // "signal" = the block text is substantial enough to be an event cluster: has a DATE or ≥40 chars. A bare
  // icon / "Read more" link whose immediate container is just the anchor fails this → we WALK UP.
  const DATE_RE = /\\b(20[12]\\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b/i;
  // M11 — CJK-AWARE SIGNAL: the ≥40-char + western-month DATE_RE test is English-biased. A JP/KR/CN event row
  // ("2026年2月4日 第3四半期決算説明会") is <40 chars AND carries no western month, so hasSignal wrongly failed and
  // the walk-up over-expanded / the row was judged thin — CJK pages under-extracted. Add (a) a CJK date pattern
  // (年月日 / 년월일 / 年 月) and (b) a lower length floor when the text CONTAINS CJK (each han/kana/hangul char is
  // ~1 word of info, so 12 CJK chars ≈ 40 latin). {AUDIT wf_7e9d7ce1 M11 "CJK render 信号漏判"} [CONFIDENCE: CONFIRMED].
  const CJK_RE = /[\\u3040-\\u30ff\\u3400-\\u9fff\\uac00-\\ud7af]/;               // kana + CJK ideographs + hangul
  const CJK_DATE_RE = /(19|20)\\d\\d\\s*[年년]|\\d{1,2}\\s*[月월]\\s*\\d{1,2}\\s*[日일]/;  // 2026年 / 2月4日 / 2월4일
  const hasSignal = (t) => DATE_RE.test(t) || CJK_DATE_RE.test(t) || (CJK_RE.test(t) ? t.length >= 12 : t.length >= 40);
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.href;                                  // .href is already absolute-resolved by the browser
    if (!(h.startsWith('http://') || h.startsWith('https://')) || seen.has(h)) continue;
    seen.add(h);
    links.push(h);
    // Start at the nearest ROW/CARD/list-item ancestor = the date+title+description cluster around this link.
    let node = a.closest('li, tr, article, .card, [class*=item], [class*=row], [class*=teaser], [class*=result], [class*=news], [class*=event]') || a.parentElement || a;
    let ctx = (node.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    // HARDENING: icon / bare "Read more" links put the date+title in a SIBLING block, so the immediate container
    // is thin. Walk UP to progressively larger ancestors until the text carries a real signal (a date or enough
    // words) — capped at 4 hops + 280 chars so a huge <section> can't dump the whole page into one link's context.
    // {USER 2026-07-05 "there might be cases where the title is far away" -> expand until the cluster is found]
    // [CONFIDENCE: CONFIRMED via 10-company probe — real events carry a title anchor; the walk-up covers the
    // icon/read-more edge case (MCO empty-anchor) where the title sits in a neighbouring node].
    let hops = 0;
    while (!hasSignal(ctx) && node.parentElement && hops < 4) {
      node = node.parentElement;
      ctx = (node.innerText || '').replace(/\\s+/g, ' ').trim();
      hops++;
    }
    ctx = ctx.slice(0, 280);
    annotated.push(ctx + ' ' + h);                     // cluster THEN url → link_contexts window captures the cluster
  }
  const text = body + '\\n' + annotated.join('\\n');   // innerText first (wall detection) + the link-annotated block
  return {text, links};
}"""


def _ensure_loop() -> None:
    """Start the dedicated event-loop thread ONCE (idempotent). WHY a dedicated thread: async_playwright must
    live on a single loop; the crawl's worker threads have no loop, so we own one here and marshal to it."""
    global _loop, _loop_thread
    if _loop is not None:
        return
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="browser-pool-loop", daemon=True)
    t.start()
    _loop, _loop_thread = loop, t


async def _launch() -> None:
    """Launch the resident browser + create the on-loop Semaphore. Runs ON the loop thread. Raises on failure
    (caught by the caller, which flips _dead so we never retry a broken environment every call)."""
    global _browser, _playwright, _sem
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    _shared_args = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer",
                    "--disable-extensions", "--disk-cache-size=1", "--media-cache-size=1"]
    _browser = await _playwright.chromium.launch(headless=True, args=_shared_args)
    global _browser_h1, _browser_proxy, _playwright_stealth
    _browser_h1 = await _playwright.chromium.launch(headless=True, args=_shared_args + ["--disable-http2"])
    from .. import webshare
    _px = webshare.playwright_proxy()
    if _px:
        try:
            from patchright.async_api import async_playwright as _async_patchright
            if _playwright_stealth is None:
                _playwright_stealth = await _async_patchright().start()
            _browser_proxy = await _playwright_stealth.chromium.launch(headless=True, args=_shared_args, proxy=_px)
            print(f"[watercrawl] webshare residential STEALTH browser UP (patchright, proxy {_px['server']})", flush=True)
        except Exception as _pxerr:
            print(f"[watercrawl] webshare stealth browser launch failed ({_pxerr}) — FALLBACK 3 dormant", flush=True)
            _browser_proxy = None
    _sem = asyncio.Semaphore(_MAX_PAGES)


def _ensure_browser_blocking() -> bool:
    """Lazily start loop + launch browser, blocking the CALLER until ready. Returns True if the browser is
    usable, False if launch failed (→ caller falls back to jina/firecrawl). Thread-safe via _lock."""
    global _dead
    if _dead:
        return False
    if _browser is not None:
        return True
    with _lock:
        if _dead:
            return False
        if _browser is not None:
            return True
        try:
            _ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(_launch(), _loop)
            fut.result(timeout=90)
            print(f"[watercrawl] resident Chromium launched (max_pages={_MAX_PAGES}) — self-hosted render lane UP", flush=True)
            return True
        except Exception as error:
            print(f"[watercrawl] launch failed, self-hosted render disabled this process: {error}", flush=True)
            _dead = True
            return False


def browser_available() -> bool:
    """Cheap check for callers that want to decide routing BEFORE building a job. Triggers the lazy launch."""
    return _ensure_browser_blocking()


_BLOCK_TYPES = {"image", "media", "font", "stylesheet"}


async def _block_heavy(route) -> None:
    """Route handler: abort heavy non-DOM sub-resources, let everything else (document/script/xhr/fetch) through
    so the SPA's list-loading JS/XHR still runs. Guarded so a teardown race (route on a closing page) is a no-op."""
    try:
        if route.request.resource_type in _BLOCK_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


async def _new_blocked_page(ctx):
    """new_page + a resource-blocking route so each page's PEAK render memory stays low (the clean-each-page OOM
    fix). Every render path in this module opens its page through here. {USER 2026-07-08 "clean each page so we
    dont oom"}."""
    page = await ctx.new_page()
    await page.route("**/*", _block_heavy)
    return page


# SCREENSHOT path blocks LESS: it KEEPS stylesheet + image (the page must LOOK right for the VL model to read its
# layout) and drops only media/font (heavy + irrelevant to structure). A shot with no CSS is a bare-DOM page —
# useless for "is this an events table or a nav bar" visual judgment, which is the whole point of the screenshot.
_SHOT_BLOCK_TYPES = {"media", "font"}


async def _block_heavy_shot(route) -> None:
    """Route handler for the screenshot path: abort only media/font (keep CSS + images so the render looks real)."""
    try:
        if route.request.resource_type in _SHOT_BLOCK_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


async def _new_shot_page(ctx):
    """new_page for the SCREENSHOT path — keeps stylesheet + image so the full-page shot is visually faithful."""
    page = await ctx.new_page()
    await page.route("**/*", _block_heavy_shot)
    return page


async def _render_one(url: str, inject_js: str | None, wait_ms: int) -> tuple[str, list]:
    """Render ONE url in an isolated context → (text, links). Optional inject_js runs AFTER load (e.g. a
    year-<select> change dispatch), then we wait wait_ms for its AJAX before extracting. Runs ON the loop."""
    async with _sem:
        ctx = await _browser.new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await _goto(page, url)
            if inject_js:
                try:
                    await page.evaluate("() => {" + inject_js + "}")
                except Exception:
                    pass
                await page.wait_for_timeout(max(wait_ms, 0))
            else:
                await _settle(page, wait_ms)
            try:
                out = await page.evaluate(_EXTRACT_JS)
            except Exception:
                await _settle(page, wait_ms)
                out = await page.evaluate(_EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or [])
        finally:
            await ctx.close()


async def _settle(page, wait_ms: int) -> None:
    """Give a JS/XHR-driven IR list time to POPULATE before we snapshot: wait for network to go idle (bounded,
    so an analytics-polling page that never idles doesn't hang), THEN a fixed settle. WHY: IR event/news lists
    load via XHR that fires AFTER domcontentloaded — snapshotting too early yields only the nav shell (KMI news:
    2 links at 3s vs 77+ once the list AJAX lands). {LOCAL 2026-07-04 "KMI /news 2 links / 0 controls at
    domcontentloaded+3s"} [CONFIDENCE: CONFIRMED — the list is XHR-late]."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    _SIG_JS = ("() => (document.querySelectorAll('a[href]').length * 100000) + "
               "Math.min((document.body ? document.body.innerText.length : 0), 5000000)")
    try:
        prev, stable = -1, 0
        for _ in range(14):
            n = await page.evaluate(_SIG_JS)
            if n == prev and n > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev = n
            await page.wait_for_timeout(300)
    except Exception:
        pass
    if wait_ms:
        await page.wait_for_timeout(wait_ms)


async def _goto(page, url: str) -> None:
    """goto with ONE retry on a transient net error (HTTP2/reset/timeout) — a first-try net::ERR is often
    transient; a bare failure would empty the render. Raises if the retry also fails (caller returns empty)."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)


async def _render_full_one(url: str, wait_ms: int, browser=None) -> tuple[str, list, str]:
    """Like _render_one but ALSO returns the page's rendered HTML → (text, links, html). WHY: fetch_page's
    control-gathering (gather_controls) needs the HTML to find the interactive <select>/<button>/tab elements
    DeepSeek judges — so when watercrawl is the SOLE render engine, it must hand back HTML too, not just text.
    Runs ON the loop."""
    async with _sem:
        ctx = await (browser or _browser).new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await _goto(page, url)
            await _settle(page, wait_ms)
            try:
                out = await page.evaluate(_EXTRACT_JS)
                html = await page.content()
            except Exception:
                await _settle(page, wait_ms)
                out = await page.evaluate(_EXTRACT_JS)
                html = await page.content()
            return out.get("text") or "", list(out.get("links") or []), html
        finally:
            await ctx.close()


async def _render_shot_one(url: str, wait_ms: int, browser=None) -> tuple[str, list, str, str]:
    """Render url AND capture a FULL-PAGE screenshot → (text, links, html, screenshot_b64). WHY the shot: it feeds a
    Qwen-VL model so it reads the page's VISUAL layout (an events table vs a nav menu vs a footer) that text alone
    loses — the visual signal is exactly what tells a real event listing from chrome. JPEG @ q70 keeps the image
    small (VL image tokens scale with pixels). Runs ON the loop; screenshot is best-effort (text/links still return)."""
    import base64
    async with _sem:
        ctx = await (browser or _browser).new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await _goto(page, url)
            await _settle(page, wait_ms)
            try:
                out = await page.evaluate(_EXTRACT_JS)
                html = await page.content()
            except Exception:                                # one more settle then retry the extract
                await _settle(page, wait_ms)
                out = await page.evaluate(_EXTRACT_JS)
                html = await page.content()
            try:
                shot = await page.screenshot(full_page=True, type="jpeg", quality=70)
                shot_b64 = base64.b64encode(shot).decode("ascii")
            except Exception:                                # noqa: BLE001 — shot failed (huge page / timeout) → text-only
                shot_b64 = ""
            return out.get("text") or "", list(out.get("links") or []), html, shot_b64
        finally:
            await ctx.close()


def render_shot(url: str, wait_ms: int = 3000) -> tuple[str, list, str, str]:
    """SYNC entry — open a page and return (text, links, html, screenshot_b64) for the LLM event extractor. text +
    links feed the prompt; screenshot_b64 (JPEG, base64) feeds a Qwen-VL model. Best-effort: ("", [], "", "") when
    the browser is unavailable or the render fails, so one bad page never raises into the crawl loop."""
    if not _ensure_browser_blocking():
        return "", [], "", ""
    try:
        fut = asyncio.run_coroutine_threadsafe(_render_shot_one(url, wait_ms), _loop)
        return fut.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40)
    except Exception as e:                                    # noqa: BLE001 — render failure must not sink the crawl loop
        print(f"[watercrawl] render_shot failed for {url[:70]}: {type(e).__name__}: {e}", flush=True)
        return "", [], "", ""


async def _drive_years_seq(url: str, js_list: list, wait_ms: int) -> list:
    """Walk N year-views in ONE page session: goto once, then per year evaluate(select-year JS)+wait+extract.
    WHY one session (vs firecrawl's one-scrape-per-year): each year-select CHANGE replaces the content in the
    SAME DOM, so reusing the page is both correct AND far cheaper than 16 separate browser sessions. Returns
    a list of (text, links), one per js in js_list. Runs ON the loop."""
    async with _sem:
        ctx = await _browser.new_context(user_agent=_UA)
        results: list = []
        try:
            page = await _new_blocked_page(ctx)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            for js in js_list:
                try:
                    await page.evaluate("() => {" + js + "}")
                except Exception:
                    pass
                await page.wait_for_timeout(max(wait_ms, 0))
                out = await page.evaluate(_EXTRACT_JS)
                results.append((out.get("text") or "", list(out.get("links") or [])))
            return results
        finally:
            await ctx.close()


async def _drive_clicks_seq(url: str, click_js: str, times: int, wait_ms: int) -> tuple[str, list]:
    """'Load more' style: click the SAME control `times` times in ONE page session so the list ACCUMULATES,
    then extract once. WHY one session + repeated click (vs years' replace): load-more APPENDS rows, so we
    must keep the same DOM and re-click, not reload. Runs ON the loop."""
    async with _sem:
        ctx = await _browser.new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            for _ in range(max(times, 1)):
                try:
                    await page.evaluate("() => {" + click_js + "}")
                except Exception:
                    pass
                await page.wait_for_timeout(max(wait_ms, 0))
            out = await page.evaluate(_EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or [])
        finally:
            await ctx.close()


def drive_clicks(url: str, click_js: str, times: int, wait_ms: int = 2500) -> tuple[str, list]:
    """SYNC entry: 'load more' walk — click `click_js` `times` times in one session, accumulate, extract →
    (text, links). ("", []) on failure → caller falls back. Self-hosted replacement for the firecrawl
    click_more action chain."""
    if not click_js or not _ensure_browser_blocking():
        return "", []
    try:
        budget_s = (_NAV_TIMEOUT_MS / 1000) + max(times, 1) * (max(wait_ms, 0) / 1000 + 2) + 30
        fut = asyncio.run_coroutine_threadsafe(_drive_clicks_seq(url, click_js, times, wait_ms), _loop)
        return fut.result(timeout=budget_s)
    except Exception as error:
        print(f"[watercrawl] drive_clicks failed for {url[:80]}: {error}", flush=True)
        return "", []


_LOADMORE_JS = """() => {
  const RX = /(load|show|view|see)\\s*(more|older)|older\\s+(news|posts|results|releases|events|articles)|more\\s+(news|results|releases|events|articles)/i;
  const cands = document.querySelectorAll('button, a, [role="button"], [class*="more"], [class*="load"], [class*="pager"]');
  for (const el of cands) {
    const t = ((el.innerText||el.textContent||'') + ' ' + (el.getAttribute('aria-label')||'') + ' ' + (el.className||'')).slice(0,200);
    if (RX.test(t) && el.offsetParent !== null) { el.scrollIntoView({block:'center'}); el.click(); return 'click'; }
  }
  window.scrollTo(0, document.body.scrollHeight);   // no load-more button → infinite-scroll fallback
  return 'scroll';
}"""


async def _drive_load_more_seq(url: str, max_rounds: int, wait_ms: int) -> tuple[str, list]:
    """Walk a 'load more' / infinite-scroll archive to EXHAUSTION in ONE page session: click/scroll, wait, re-count
    links; stop when the count stops growing (2 stable rounds = complete) or max_rounds. WHY one session + re-count:
    load-more/scroll APPEND rows to the SAME DOM, so we keep the page and re-measure — the no-growth convergence IS
    the completeness signal (replaces the fragile '<30 links = walled' guess). Runs ON the loop."""
    async with _sem:
        ctx = await _browser.new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await _goto(page, url)
            await _settle(page, 0)
            initial = await page.evaluate("() => document.querySelectorAll('a[href]').length")
            prev, stable = initial, 0
            for _ in range(max(max_rounds, 1)):
                try:
                    await page.evaluate(_LOADMORE_JS)
                except Exception:
                    pass
                await page.wait_for_timeout(max(wait_ms, 0))
                n = await page.evaluate("() => document.querySelectorAll('a[href]').length")
                if n == prev:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                prev = n
            if prev <= initial * 1.05:
                return "", []
            out = await page.evaluate(_EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or [])
        finally:
            await ctx.close()


def drive_load_more(url: str, max_rounds: int = 40, wait_ms: int = 1500) -> tuple[str, list]:
    """SYNC entry: AUTO-walk a load-more / infinite-scroll list to exhaustion → (accumulated_text, deduped_links).
    ("", []) on failure / no browser. Safe to call UNCONDITIONALLY on any hub — a page with no load-more and no
    scroll-growth simply converges in ~2 rounds and returns its rendered list (the caller dedups). This is the
    completeness driver that pairs with drive_year_select (years) + paginate_hub (?page=) to cover the third
    archive-navigation mode. {USER 2026-07-10 "also fix completeness"} [CONFIDENCE: INFERRED — mirrors the proven
    drive_year_select session pattern; convergence-stop bounds a page that never grows to ~2 wasted rounds]."""
    if not _ensure_browser_blocking():
        return "", []
    try:
        budget_s = (_NAV_TIMEOUT_MS / 1000) + max(max_rounds, 1) * (max(wait_ms, 0) / 1000 + 1) + 30
        fut = asyncio.run_coroutine_threadsafe(_drive_load_more_seq(url, max_rounds, wait_ms), _loop)
        return fut.result(timeout=budget_s)
    except Exception as error:
        print(f"[watercrawl] drive_load_more failed for {url[:80]}: {error}", flush=True)
        return "", []


async def _drive_year_select_seq(url: str, max_years: int, wait_ms: int) -> tuple[str, list]:
    """AUTO-discover the year <select> on url FROM THE DOM (not the markdown), then walk newest→oldest,
    selecting each year + capturing its AJAX listing. WHY DOM-discovery: q4/Evergreen year filters load their
    <option>s via XHR ("Loading" then the years), so the years are NEVER in jina's markdown — q4_year_action
    (which reads years from the page TEXT) finds nothing and the archive is silently dropped. Playwright reads
    the LIVE select.options, so it finds the years the markdown can't. {PROBE 2026-07-04 KMI news select
    "_CTRL0_CTL64_SELECTEVERGREENNEWSYEAR:LOADING" — options AJAX-loaded, absent from markdown} [CONFIDENCE:
    CONFIRMED — driving the DOM-read years recovered KMI news 7→63 detail links in the Cloud Run probe]."""
    async with _sem:
        ctx = await _browser.new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            await page.wait_for_timeout(max(wait_ms, 0))
            years = await page.evaluate("""() => {
              const nm = s => (s||'').replace(/[\\uFF10-\\uFF19]/g, c => String.fromCharCode(c.charCodeAt(0)-0xFEE0)).trim();
              const G = /^(q[1-4][\\s'._-]*)?(fy[\\s'._-]?)?((19|20)\\d{2})([\\s'._-]*q[1-4])?\\s*(年|年度|년|년도)?$/i;
              const E = /^(令和|平成|昭和|大正|民國|民国)\\s*\\d{1,3}\\s*年(度)?$/;
              for (const s of document.querySelectorAll('select')) {
                const ys = [];
                for (const o of s.options) { const t = nm(o.textContent); if (t.length <= 12 && (G.test(t) || E.test(t))) ys.push(t); }
                if (ys.length >= 2) { ys.sort((a,b) => (b.match(/\\d{4}/)||['0'])[0] - (a.match(/\\d{4}/)||['0'])[0]); return ys; }
              }
              return [];
            }""")
            if not years:
                return "", []
            merged_links: list = []
            merged_text: list = []
            for y in years[:max_years]:
                js = ("var nm=function(s){return (s||'').replace(/[\\uFF10-\\uFF19]/g,function(c){"
                      "return String.fromCharCode(c.charCodeAt(0)-0xFEE0);}).trim();};"
                      "var ss=document.querySelectorAll('select');var s=null;"
                      "for(var k=0;k<ss.length;k++){for(var j=0;j<ss[k].options.length;j++){"
                      "if(nm(ss[k].options[j].text)==" + repr(y) + "){s=ss[k];s.selectedIndex=j;break;}}if(s)break;}"
                      "if(s){s.dispatchEvent(new Event('change',{bubbles:true}));}")
                try:
                    await page.evaluate("() => {" + js + "}")
                except Exception:
                    pass
                await page.wait_for_timeout(max(wait_ms, 0))
                out = await page.evaluate(_EXTRACT_JS)
                merged_text.append(out.get("text") or "")
                merged_links += list(out.get("links") or [])
            return "\n".join(merged_text), list(dict.fromkeys(merged_links))
        finally:
            await ctx.close()


def drive_year_select(url: str, max_years: int = 16, wait_ms: int = 5000) -> tuple[str, list]:
    """SYNC entry: auto-find the year <select> on url (reading the LIVE DOM, not the markdown) and walk every
    year → (merged_text, deduped_links). ("", []) when there is no year select or the browser is unavailable —
    so callers can invoke it UNCONDITIONALLY on any hub and it self-skips pages without a year filter."""
    if not _ensure_browser_blocking():
        return "", []
    try:
        budget_s = (_NAV_TIMEOUT_MS / 1000) + (max_years + 1) * (max(wait_ms, 0) / 1000 + 2) + 30
        fut = asyncio.run_coroutine_threadsafe(_drive_year_select_seq(url, max_years, wait_ms), _loop)
        return fut.result(timeout=budget_s)
    except Exception as error:
        print(f"[watercrawl] drive_year_select failed for {url[:80]}: {error}", flush=True)
        return "", []


_DROPDOWN_OPEN_JS = """() => {
  let n = 0;
  const sel = '[aria-haspopup="listbox"],[aria-haspopup="menu"],[aria-haspopup="true"],[role="combobox"],'
            + 'button[aria-expanded="false"],[role="button"][aria-expanded="false"],'
            + '[class*="dropdown-toggle"],[class*="dropdown__toggle"],[class*="Dropdown"],[class*="select-trigger"],'
            + '[class*="select__control"],[class*="filter-toggle"],[class*="year-select"],[class*="yearSelect"],[class*="year-dropdown"]';
  for (const el of document.querySelectorAll(sel)) {
    if (el.offsetParent === null) continue;
    try { el.scrollIntoView({block:'center'}); el.click(); n++; } catch(e){}
  }
  return n;
}"""

_YEARBAR_DISCOVER_JS = """() => {
  const nm = s => (s||'').replace(/[\\uFF10-\\uFF19]/g, c => String.fromCharCode(c.charCodeAt(0)-0xFEE0)).trim();
  const G = /^(fy[\\s'._-]?)?((19|20)\\d{2})\\s*(年|年度|년|년도)?$/i;               // Gregorian, opt CJK year suffix
  const E = /^(令和|平成|昭和|大正|民國|民国)\\s*\\d{1,3}\\s*年(度)?$/;                 // JP/ROC era-year chip
  const seen = new Set(), out = [];
  for (const el of document.querySelectorAll('a,button,li,span,div,[role="tab"],[role="option"],[role="menuitem"],[role="button"]')) {
    if (el.offsetParent === null) continue;                      // visible only — a chip OR a now-open combobox item
    const t = nm(el.innerText||el.textContent);
    if (!t || t.length > 8) continue;                            // >8 chars ⇒ an event title, not a year chip
    let key = null, yr = 0;
    const g = t.match(G);
    if (g) { key = g[2]; yr = +g[2]; }                           // Gregorian → dedup by 4-digit ('FY2026'/'2026年' → one)
    else if (E.test(t)) { key = t; }                             // era-year → dedup by raw normalized text
    if (key && !seen.has(key)) { seen.add(key); out.push({t, yr}); }
  }
  out.sort((a,b) => b.yr - a.yr);                                // Gregorian newest-first; era chips (yr=0) trail
  return out.map(o => o.t);                                      // RAW normalized chip text → click matches it exactly
}"""

_YEARBAR_CLICK_JS = """(chip) => {
  const nm = s => (s||'').replace(/[\\uFF10-\\uFF19]/g, c => String.fromCharCode(c.charCodeAt(0)-0xFEE0)).trim();
  for (const el of document.querySelectorAll('a,button,li,span,div,[role="tab"],[role="option"],[role="menuitem"],[role="button"]')) {
    if (el.offsetParent === null) continue;
    if (nm(el.innerText||el.textContent) === chip) { el.scrollIntoView({block:'center'}); el.click(); return true; }
  }
  return false;
}"""


async def _drive_year_bar_seq(url: str, max_years: int, wait_ms: int) -> tuple[str, list]:
    """Walk a YEAR-BAR (clickable year tabs/buttons/links, NOT a <select>) newest→oldest → merged (text, links). For
    each year, click its chip + capture the listing; if the chip is GONE (an AJAX re-render dropped it, or the last
    click NAVIGATED away), re-goto the hub once and retry. WHY the re-goto fallback (vs the <select> pure one-session
    walk): a year chip may REPLACE the list in place OR navigate to a per-year URL — the re-goto makes BOTH work
    while staying single-session-fast for the common REPLACE case. Self-skips ('', []) when <2 year chips exist (a
    lone '2026' copyright label is not a filter). {USER 2026-07-12 "year filter ... dropdown bar"} [CONFIDENCE:
    INFERRED 80% — mirrors the proven <select>/load-more walks; exact-text chip match still needs a live-site check]."""
    async with _sem:
        ctx = await _browser.new_context(user_agent=_UA)
        try:
            page = await _new_blocked_page(ctx)
            await _goto(page, url)
            await _settle(page, wait_ms)
            try:
                await page.evaluate(_DROPDOWN_OPEN_JS)
            except Exception:
                pass
            await page.wait_for_timeout(min(max(wait_ms, 0), 2500))
            years = list(await page.evaluate(_YEARBAR_DISCOVER_JS))[:max_years]
            if len(years) < 2:
                return "", []
            merged_text: list = []
            merged_links: list = []
            for y in years:
                try:
                    await _goto(page, url)
                    await _settle(page, wait_ms)
                    try:
                        await page.evaluate(_DROPDOWN_OPEN_JS)
                    except Exception:
                        pass
                    await page.wait_for_timeout(min(max(wait_ms, 0), 2000))
                    if not await page.evaluate(_YEARBAR_CLICK_JS, y):
                        continue
                    await page.wait_for_timeout(max(wait_ms, 0))
                    out = await page.evaluate(_EXTRACT_JS)
                    merged_text.append(out.get("text") or "")
                    merged_links += list(out.get("links") or [])
                except Exception:
                    pass
            return "\n".join(merged_text), list(dict.fromkeys(merged_links))
        finally:
            await ctx.close()


def drive_year_bar(url: str, max_years: int = 16, wait_ms: int = 5000) -> tuple[str, list]:
    """SYNC entry: drive a YEAR-BAR (clickable year tabs/buttons, not a <select>) → (merged_text, deduped_links).
    ('', []) when there is no year bar or the browser is unavailable — so callers invoke it UNCONDITIONALLY right
    after drive_year_select and it self-skips pages whose year filter is a <select> (already driven) or absent."""
    if not _ensure_browser_blocking():
        return "", []
    try:
        budget_s = (_NAV_TIMEOUT_MS / 1000) + (max_years + 1) * (_NAV_TIMEOUT_MS / 1000 + max(wait_ms, 0) / 1000 + 5) + 30
        fut = asyncio.run_coroutine_threadsafe(_drive_year_bar_seq(url, max_years, wait_ms), _loop)
        return fut.result(timeout=budget_s)
    except Exception as error:
        print(f"[watercrawl] drive_year_bar failed for {url[:80]}: {error}", flush=True)
        return "", []


def render(url: str, inject_js: str | None = None, wait_ms: int = 0) -> tuple[str, list]:
    """SYNC entry (safe from any crawl thread): render url via the resident browser → (text, links). Optional
    inject_js drives a control before extraction. ("", []) on any failure → caller falls back. Blocks the
    calling thread on the loop's future (the loop still serves other pages concurrently)."""
    if not _ensure_browser_blocking():
        return "", []
    try:
        fut = asyncio.run_coroutine_threadsafe(_render_one(url, inject_js, wait_ms), _loop)
        return fut.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 30)
    except Exception as error:
        print(f"[watercrawl] render failed for {url[:80]}: {error}", flush=True)
        return "", []


_WALL_MARKERS = ("just a quick security check", "verifying the security", "ray id", "request unsuccessful",
                 "access denied", "attention required", "checking your browser", "enable javascript and cookies",
                 "challenge-platform", "cf-chl", "__cf_chl", "cf-turnstile", "turnstile", "_incapsula_",
                 "確認しています", "しばらくお待ち", "セキュリティチェック", "アクセスが拒否",
                 "安全验证", "请稍候", "正在验证", "拒绝访问", "보안 확인", "잠시만 기다")


def _looks_walled(text: str, links: list) -> bool:
    """True when the browser render did NOT get the real page — either it came back near-empty (a bot-wall that
    ERR'd/timed out → we got nothing), or it's a challenge interstitial (wall-marker body). Both mean 'the
    headless-from-datacenter render was blocked → try the impersonate/webshare fallback'. GENERAL, no per-site logic."""
    if len(links) < 5:
        return True
    low = (text or "").lower()
    return any(m in low for m in _WALL_MARKERS)


_DEAD_HOST_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_CERT_", "ERR_SSL", "ERR_ABORTED")
_DEAD_URLS: set = set()


def dead_host(url: str) -> bool:
    """True if url's last render_full failed with a DEAD/BAD-host browser error (see _DEAD_HOST_ERRORS) — the
    caller uses this to NOT count the empty render as an infra failure. Recorded by render_full below."""
    return url in _DEAD_URLS


_WAIT_RETRIES = int(os.environ.get("WATERCRAWL_WAIT_RETRIES", "1"))
_WAIT_STEP_MS = int(os.environ.get("WATERCRAWL_WAIT_STEP_MS", "3000"))


def _render_thin(text: str) -> bool:
    """True when a render RESULT is still a NAV/JS-APP SHELL — the JS/AJAX content has not loaded yet, so a longer wait
    is worth trying. Signal: near-empty OR no sentence-terminating punctuation (a nav menu is Title-Case labels; real
    press-release/filing content is prose with sentences). {2026-07-22 PFS .aspx: a wait=0 capture = 2033 chars of pure
    menu, 0 sentence terminators; the filing content was JS-loaded} [CONFIDENCE: CONFIRMED]."""
    import re as _re2
    if not text or len(text.strip()) < 200:
        return True
    return len(_re2.findall(r"[.。][ \t\"'”)\]]|\.[A-Z]", text)) < 2


def _render_with_wait_retries(url: str, base_wait: int) -> tuple[str, list, str, bool]:
    """Render url with a GROWING wait (up to _WAIT_RETRIES tries) so a JS/AJAX-rendered page's real content has time to
    appear — STOP as soon as the page is no longer a thin nav shell. Also runs the ERR_HTTP2 → HTTP/1.1 retry and marks
    a DEAD host. Returns (text, links, html, dead). WHY the ladder: a Q4/Sitecore .aspx DETAIL page loads its filing/
    release content AFTER the load event, so a single wait=0 capture returns only the nav menu (measured: PFS 134
    filings all titled "Corporate Profile" because the 8-K body was JS-loaded and missing). Each retry waits base_wait +
    attempt*_WAIT_STEP_MS. An SSR page (content on first paint) passes attempt 0 and never pays the extra waits.
    {USER 2026-07-22 "retry 3 times, each time the wait ms is longer"} [CONFIDENCE: CONFIRMED — the content is JS-loaded]."""
    text, links, html, dead = "", [], "", False
    for attempt in range(max(_WAIT_RETRIES, 1)):
        w = base_wait + attempt * _WAIT_STEP_MS
        try:
            fut = asyncio.run_coroutine_threadsafe(_render_full_one(url, w), _loop)
            text, links, html = fut.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(w, 0) / 1000 + 30)
        except Exception as error:
            if "ERR_HTTP2" in str(error) and _browser_h1 is not None:
                try:
                    fut2 = asyncio.run_coroutine_threadsafe(_render_full_one(url, w, browser=_browser_h1), _loop)
                    text, links, html = fut2.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(w, 0) / 1000 + 30)
                    print(f"[watercrawl] {url[:70]} HTTP2 fail → HTTP1 retry got {len(links)} links", flush=True)
                except Exception as e2:
                    print(f"[watercrawl] render_full HTTP1 retry failed for {url[:70]}: {e2}", flush=True)
            else:
                print(f"[watercrawl] render_full failed for {url[:80]}: {error}", flush=True)
                if any(m in str(error) for m in _DEAD_HOST_ERRORS):
                    _DEAD_URLS.add(url); dead = True
            break
        if not _render_thin(text):
            if attempt:
                print(f"[watercrawl] {url[:70]} content arrived at wait={w}ms (attempt {attempt + 1}/{_WAIT_RETRIES})", flush=True)
            break
    return text, links, html, dead


# FB4 concurrency cap — Camoufox launches a FULL Firefox per call; only the hardest walled pages reach FB4, but a burst
# of them at once would OOM on N concurrent Firefoxes. Bound it. {2026-07-22 camoufox FB4}.
_CAMOUFOX_SEM = threading.Semaphore(int(os.environ.get("CAMOUFOX_CAP", "2")))


async def _camoufox_render_one(url: str, wait_ms: int) -> tuple[str, list, str]:
    """FALLBACK 4 — CAMOUFOX: render url with Camoufox (a Firefox fork whose anti-detect stealth is applied at the C++
    SOURCE level, not JS injection → 0% headless-detection) through the residential ROTATING proxy. The ONLY $0 method
    that beats FULL Akamai / Incapsula sensor.js: curl_cffi (FB1) 403s (no _abck cookie) and patchright (FB3) ERR_HTTP2s
    (Akamai deliberately breaks the headless HTTP/2 stream), but Camoufox's undetectable headless lets sensor.js run to
    completion → the _abck cookie lands → the wall opens. {TESTED 2026-07-22 Cloud Run: RJF/raymondjames Akamai 200
    (577KB), wanhai Incapsula 200 (53KB), secom 200 — all pages FB1+FB3 could NOT get} [CONFIDENCE: CONFIRMED — GCP-
    proven on the three hardest walls]. Per-call Firefox launch (only the hardest walls reach FB4)."""
    from camoufox.async_api import AsyncCamoufox                  # lazy: absent camoufox → import fails → caller skips FB4
    from .. import webshare                            # the residential rotating gateway (proxy dict)
    pxd = webshare.playwright_proxy()
    if not pxd:                                                   # no residential proxy configured → FB4 dormant
        return "", [], ""
    async with AsyncCamoufox(headless=True, proxy=pxd, geoip=False) as browser:   # C++-stealth Firefox on the rotating IP
        page = await browser.new_page()
        await page.goto(url, timeout=_NAV_TIMEOUT_MS + 20000, wait_until="domcontentloaded")
        await page.wait_for_timeout(max(wait_ms, 6000))          # let Akamai/Incapsula sensor.js run → _abck cookie lands
        html = await page.content()                              # the post-challenge REAL page
        try:
            links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")   # browser resolves → absolute
        except Exception:                                        # noqa: BLE001 — link eval hiccup → html/text still usable
            links = []
        try:
            text = await page.inner_text("body")                 # clean visible text (feeds _looks_walled / _render_thin)
        except Exception:                                        # noqa: BLE001
            text = ""
        return text, links, html


def _camoufox_render(url: str, wait_ms: int) -> tuple[str, list, str]:
    """SYNC wrapper for FB4 — run _camoufox_render_one on the shared loop, bounded by _CAMOUFOX_SEM. ('', [], '') on ANY
    failure (camoufox absent / launch error / wall unbeaten) so a caller keeps its prior result. WHY sync: render_full/
    render_detail are sync entry points into the one playwright loop thread."""
    with _CAMOUFOX_SEM:                                          # cap concurrent Firefox launches (OOM guard)
        try:
            fut = asyncio.run_coroutine_threadsafe(_camoufox_render_one(url, wait_ms), _loop)
            return fut.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 70)   # Firefox launch is slow
        except Exception as _ce:                                # noqa: BLE001 — FB4 is a best-effort last resort
            print(f"[watercrawl] camoufox FB4 failed for {url[:70]}: {_ce}", flush=True)
            return "", [], ""


def render_full(url: str, wait_ms: int = 0) -> tuple[str, list, str]:
    """SYNC entry: render url → (text, links, html) for the DISCOVERY fetch (event_fetch renders a LISTING page and
    wants its LINKS). INTERNAL general bot-wall fallback chain: headless render → curl_cffi impersonate (FALLBACK 1)
    → webshare residential render (FALLBACK 3). Each fallback's success test is LINK COUNT (more links = a better
    discovery result) — that is what distinguishes render_full from render_detail (the READ fetch, whose fallbacks
    test CONTENT PRESENCE instead). ("", [], "") only when the whole chain yields nothing.
    {USER 2026-07-04 "integrate into watercrawl and make all the fix general"} [CONFIDENCE: CONFIRMED].

    SITEMAP REMOVED (2026-07-22): the old FALLBACK 2 harvested the site's sitemap.xml when render+impersonate were
    both walled. It was DELETED — sitemap urls carry NO anchor/context, so every sitemap-harvested event landed with
    an empty anchor (65% of all events had empty anchors) and collapsed to a generic "Corporate Profile"-class title;
    it was also unreliable (per-host path guessing) and slow. A walled LISTING now simply yields fewer links rather
    than context-less junk. {USER 2026-07-22 "remove the sitemap one that is very unreliable and messy"; quantified:
    35370/54116 events empty-anchor} [CONFIDENCE: CONFIRMED — empty-anchor rate measured on the live events table]."""
    try:
        from src.agents.company_agent.tools.slides.base import is_pdf_url
        if is_pdf_url(url):
            from src.agents.company_agent.tools.slides import curl_cffi_pypdf
            pdf_text = curl_cffi_pypdf.fetch(url) or ""
            if pdf_text.strip():
                return pdf_text, [], ""
    except Exception as _pdf_err:
        print(f"[watercrawl] pdf extract path errored for {url[:70]}: {_pdf_err} — falling back to render", flush=True)
    text, links, html = "", [], ""
    dead = False
    if _ensure_browser_blocking():
        text, links, html, dead = _render_with_wait_retries(url, wait_ms)
    if _looks_walled(text, links) and not dead:
        from . import impersonate
        i_text, i_links, i_html = impersonate.fetch(url)
        if len(i_links) > len(links):
            print(f"[watercrawl] render blocked on {url[:70]} → curl_cffi impersonate got {len(i_links)} links (no proxy)", flush=True)
            text, links, html = (i_text or text), i_links, (i_html or html)
        else:
            print(f"[watercrawl] impersonate ALSO blocked/empty on {url[:70]} (got {len(i_links)} links)", flush=True)
    if _looks_walled(text, links) and not dead:
        if _browser_proxy is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(_render_full_one(url, wait_ms, browser=_browser_proxy), _loop)
                r_text, r_links, r_html = fut.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40)
                if len(r_links) > len(links):
                    print(f"[watercrawl] WALLED → webshare residential render got {len(r_links)} links for {url[:70]}", flush=True)
                    text, links, html = (r_text or text or "watercrawl-webshare"), r_links, (r_html or html)
                else:
                    print(f"[watercrawl] WALLED and webshare residential render got {len(r_links)} links for {url[:70]} too", flush=True)
            except Exception as _wserr:
                print(f"[watercrawl] webshare residential render failed for {url[:70]}: {_wserr}", flush=True)
        else:
            print(f"[watercrawl] WALLED and NO webshare proxy for {url[:70]} — set WEBSHARE_USERNAME/PASSWORD to recover", flush=True)
    if _looks_walled(text, links) and not dead:                # FALLBACK 4 — CAMOUFOX (beats Akamai/Incapsula sensor.js)
        c_text, c_links, c_html = _camoufox_render(url, wait_ms)
        if len(c_links) > len(links):                          # discovery success test = MORE links than the walled result
            print(f"[watercrawl] WALLED → camoufox FB4 got {len(c_links)} links for {url[:70]}", flush=True)
            text, links, html = (c_text or text or "watercrawl-camoufox"), c_links, (c_html or html)
    return text, links, html


def render_detail(url: str, wait_ms: int = 0) -> tuple[str, list, str]:
    """READ-type render for basic_info's DETAIL fetch — render ONE event page's CONTENT (JS) → (text, links, html).
    CODE-LEVEL ISOLATION from render_full (a separate function, not a bool flag): render_full is the DISCOVERY fetch
    and its fallbacks succeed on LINK COUNT (more links = better listing); render_detail is the READ fetch and its
    fallbacks succeed on CONTENT PRESENCE (`not _render_thin(...)` — real prose = better page). That success-criterion
    difference is why the two are distinct functions, not one parameterized entry. Fallback chain a detail page needs:
    render (JS wait-retries) → impersonate (TLS/HTTP2 fingerprint bypass) → webshare residential render (JS behind a
    residential-only wall). PDF → pypdf text. Neither function does sitemap-harvest anymore (removed 2026-07-22 as
    context-less junk). {USER 2026-07-22 "i want code level isolation not just a trigger"} [CONFIDENCE: CONFIRMED]."""
    try:
        from src.agents.company_agent.tools.slides.base import is_pdf_url
        if is_pdf_url(url):
            from src.agents.company_agent.tools.slides import curl_cffi_pypdf
            pdf_text = curl_cffi_pypdf.fetch(url) or ""
            if pdf_text.strip():
                return pdf_text, [], ""
    except Exception as _pe:
        print(f"[watercrawl] detail pdf path errored for {url[:70]}: {_pe} — falling back to render", flush=True)
    text, links, html, dead = "", [], "", False
    _imp = None
    if _ensure_browser_blocking():
        text, links, html, dead = _render_with_wait_retries(url, wait_ms)
    if _looks_walled(text, links) and not dead:
        from . import impersonate
        i_text, i_links, i_html = impersonate.fetch(url)
        if i_html or i_text:
            _imp = (i_text, i_links, i_html)
            if not _render_thin(i_text or ""):
                text, links, html = (i_text or text), (i_links or links), (i_html or html)
    if _looks_walled(text, links) and not dead and _browser_proxy is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(_render_full_one(url, wait_ms, browser=_browser_proxy), _loop)
            r_text, r_links, r_html = fut.result(timeout=(_NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40)
            if (r_html or r_text) and not _render_thin(r_text or ""):
                text, links, html = (r_text or text or "watercrawl-webshare"), (r_links or links), (r_html or html)
        except Exception as _we:
            print(f"[watercrawl] detail residential render failed for {url[:70]}: {_we}", flush=True)
    if _looks_walled(text, links) and not dead:                # FALLBACK 4 — CAMOUFOX (content-presence success test)
        c_text, c_links, c_html = _camoufox_render(url, wait_ms)
        if (c_html or c_text) and not _render_thin(c_text or ""):   # read success test = real prose, not a shell
            print(f"[watercrawl] WALLED → camoufox FB4 got detail content for {url[:70]}", flush=True)
            text, links, html = (c_text or text), (c_links or links), (c_html or html)
    if _looks_walled(text, links) and _imp is not None and (_imp[2] or _imp[0]):
        text, links, html = (_imp[0] or text), (_imp[1] or links), (_imp[2] or html)
    return text, links, html


def drive_years(url: str, js_list: list, wait_ms: int = 4000) -> list:
    """SYNC entry: walk the year-<select> views for url in ONE session → list of (text, links). Empty list on
    failure. This is the self-hosted replacement for drive_archive's firecrawl one-scrape-per-year loop."""
    if not js_list or not _ensure_browser_blocking():
        return []
    try:
        budget_s = (_NAV_TIMEOUT_MS / 1000) + len(js_list) * (max(wait_ms, 0) / 1000 + 2) + 30
        fut = asyncio.run_coroutine_threadsafe(_drive_years_seq(url, js_list, wait_ms), _loop)
        return fut.result(timeout=budget_s)
    except Exception as error:
        print(f"[watercrawl] drive_years failed for {url[:80]}: {error}", flush=True)
        return []
