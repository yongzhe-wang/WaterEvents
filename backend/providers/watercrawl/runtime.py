"""watercrawl.runtime — the ONE owner of shared browser state + the dedicated asyncio loop thread.

用一句话讲完: 整个 watercrawl 只有一个常驻浏览器进程 + 一条专属 asyncio loop 线程 + 一个限并发 semaphore,这些
可变全局状态**只此一份、只住在这个模块里** → render/drivers/orchestrator 全部 `from . import runtime` 读 `runtime._browser`
/`runtime._loop`,谁都不再各持一份 → 拆成多文件也不会状态分叉。任何跨线程调用协程都走 `runtime.run_on_loop(coro, timeout)`。
WHY 一个模块独占状态: async_playwright 对象绑定创建它的 loop,crawl 的 ThreadPoolExecutor worker 线程没有 loop,所以
我们自己拥有一条 loop 线程、用 run_coroutine_threadsafe marshal 过去。把这套 lifecycle + 状态收进 runtime.py 是拆分
god-module 的地基 —— 只有"状态单一持有者"才能让 render.py / drivers/*.py 安全地引用同一个 browser。
{RESEARCH crawl4ai `browser_manager.py` 把浏览器生命周期独立成模块} [CONFIDENCE: CONFIRMED — Playwright loop-affinity 约束].

Design invariants:
  - ONE browser per worker process, launched lazily on first use, kept warm for the process lifetime.
  - ALL Playwright calls run on ONE dedicated event-loop thread (loop-affinity).
  - A Semaphore bounds CONCURRENT pages so peak memory stays inside the worker's limit.
  - Best-effort: launch failure flips _dead so callers fall back to impersonate/jina — render is NEVER a hard dep.
"""
from __future__ import annotations

import asyncio
import itertools
import threading

from . import config

# ── shared mutable state (THE single copy; other modules read these via `runtime.<name>`) ──────────────────────
_lock = threading.Lock()                                  # guards the lazy launch (one launch across N caller threads)
_loop: asyncio.AbstractEventLoop | None = None            # the dedicated Playwright loop
_loop_thread: threading.Thread | None = None
_browser = None                                           # default headless Chromium (_browsers[0]; fallback code reads this)
_browsers: list = []                                      # POOL of default Chromiums (config.RENDER_BROWSERS of them) — render pages round-robin over these separate PROCESSES so no ONE browser starves at high tab count
_rr_browser = None                                        # itertools.cycle over _browsers (created on the loop in _launch)
_browser_h1 = None                                        # HTTP/1.1-forced Chromium (ERR_HTTP2 retry lane)
_browser_proxy = None                                     # patchright + webshare residential STEALTH browser (walls)
_playwright = None                                        # the async_playwright driver for the two Chromiums
_playwright_stealth = None                                # the patchright driver for the residential browser
_sem: asyncio.Semaphore | None = None                     # bounds concurrent pages (created on the loop in _launch)
_shot_sem: asyncio.Semaphore | None = None                # bounds concurrent FULL-PAGE SCREENSHOTS (the RAM hog) — created in _launch
_dead = False                                             # True once a launch failed → never retry a broken env


def _ensure_loop() -> None:
    """Start the dedicated event-loop thread ONCE (idempotent). WHY a dedicated thread: async_playwright must live
    on a single loop; the crawl's worker threads have no loop, so we own one here and marshal to it."""
    global _loop, _loop_thread
    if _loop is not None:
        return
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="browser-pool-loop", daemon=True)
    t.start()
    _loop, _loop_thread = loop, t


async def _launch() -> None:
    """Launch the resident browsers + create the on-loop Semaphore. Runs ON the loop thread. Raises on failure
    (caught by ensure_browser, which flips _dead so we never retry a broken environment every call).

    THREE browsers, layered by cost: (1) default headless Chromium — the fast path; (2) an HTTP/1.1-forced Chromium
    for the ERR_HTTP2 retry lane (Akamai deliberately breaks headless HTTP/2); (3) a patchright + webshare residential
    STEALTH browser for bot-walls (only if a webshare proxy is configured)."""
    global _browser, _browsers, _rr_browser, _playwright, _sem, _shot_sem, _browser_h1, _browser_proxy, _playwright_stealth
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    # Container-safe flags (--disable-dev-shm-usage) + cache/GPU trims so peak render memory stays low.
    _shared_args = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer",
                    "--disable-extensions", "--disk-cache-size=1", "--media-cache-size=1"]
    # POOL of default Chromiums — config.RENDER_BROWSERS SEPARATE browser processes (min 1). render pages round-robin
    # across them (next_browser) so N total tabs split into N/K tabs per browser → no single browser's main-thread/IPC
    # starves. _browser stays = _browsers[0] so the fallback lanes (h1/residential) that read runtime._browser still work.
    # {USER 2026-07-23 "you have multiple cpu right"} [CONFIDENCE: CONFIRMED — 1-browser@48 starved; K procs spread the load].
    _browsers = [await _playwright.chromium.launch(headless=True, args=_shared_args)
                 for _ in range(max(1, config.RENDER_BROWSERS))]
    _rr_browser = itertools.cycle(_browsers)              # round-robin picker (consumed on the single loop thread → no lock needed)
    _browser = _browsers[0]                               # fallback-lane compatibility: h1/residential code references _browser
    # HTTP/1.1 lane — a second Chromium with HTTP/2 disabled, used only when the default browser ERR_HTTP2s.
    _browser_h1 = await _playwright.chromium.launch(headless=True, args=_shared_args + ["--disable-http2"])
    # Residential STEALTH lane — patchright (source-patched Playwright) through the webshare rotating proxy. Only
    # armed when a proxy is configured; a launch failure leaves it dormant (FALLBACK 3 skipped, never fatal).
    from .. import webshare                                # providers/webshare — the residential rotating gateway
    _px = webshare.playwright_proxy()
    if _px:
        try:
            from patchright.async_api import async_playwright as _async_patchright
            if _playwright_stealth is None:
                _playwright_stealth = await _async_patchright().start()
            _browser_proxy = await _playwright_stealth.chromium.launch(headless=True, args=_shared_args, proxy=_px)
            print(f"[watercrawl] webshare residential STEALTH browser UP (patchright, proxy {_px['server']})", flush=True)
        except Exception as _pxerr:                       # noqa: BLE001 — residential lane is best-effort, never fatal
            print(f"[watercrawl] webshare stealth browser launch failed ({_pxerr}) — FALLBACK 3 dormant", flush=True)
            _browser_proxy = None
    else:
        # FAIL-LOUD at launch (not per-page) when NO residential proxy is configured → BOTH tier2 (residential) AND tier4
        # (camoufox, which requires the proxy) are DORMANT. This is the silent gap that let the whole 2668-run's 43 bot-
        # walled big-caps (tesla/homedepot/nestle Akamai) fail unrecovered without a trace. Surface it once, at browser
        # launch, so "walled site 0-events" is traceable to a missing WEBSHARE_PROXY, not a mystery. {AUDIT 2026-07-24
        # residential_dormant; rewall recovered 39/45 once WEBSHARE_PROXY set + libgtk installed} [CONFIDENCE: CONFIRMED].
        print("[watercrawl] ⚠ NO webshare proxy configured — tier2 residential + tier4 camoufox DORMANT; "
              "Akamai/Incapsula-walled hosts will NOT be recovered (set WEBSHARE_PROXY or WEBSHARE_USERNAME/PASSWORD/PROXIES)", flush=True)
    _sem = asyncio.Semaphore(config.MAX_PAGES)            # bound concurrent pages (created on THIS loop)
    _shot_sem = asyncio.Semaphore(config.SHOT_CONCURRENCY)   # bound concurrent FULL-PAGE SHOTS (RAM hog) so 4 browsers don't OOM the cgroup


def ensure_browser() -> bool:
    """Lazily start loop + launch browsers, blocking the CALLER until ready. Returns True if the browser is usable,
    False if launch failed (→ caller falls back to impersonate/jina). Thread-safe via _lock. The _dead-latch
    means a broken env is never retried per call."""
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
            print(f"[watercrawl] resident Chromium launched ({len(_browsers)} browser(s) × max_pages={config.MAX_PAGES} "
                  f"total) — self-hosted render lane UP", flush=True)
            return True
        except Exception as error:                        # noqa: BLE001 — a broken env must disable render, not crash
            print(f"[watercrawl] launch failed, self-hosted render disabled this process: {error}", flush=True)
            _dead = True
            return False


def next_browser():
    """Round-robin the NEXT default browser from the pool (render.py's default render path calls this instead of reading
    the single runtime._browser). Spreads consecutive render pages across the K separate browser processes so no one
    browser accumulates all the tabs. Called ONLY from render coroutines on the single loop thread, so the itertools
    cycle needs no lock. Falls back to _browser if the pool isn't built yet (defensive; ensure_browser runs first).

    EVERY on-loop context opener must come through here — the five interaction drivers (year_bar / year_select / years /
    load_more / clicks) used to read `runtime._browser` directly, which is hard-wired to `_browsers[0]`. That put ALL
    expansion work on a single browser process while only the render path round-robined, and expansion is the heaviest
    session there is (year_bar re-navigates once per year, up to 7 navigations for one page). Piling those on one browser
    is the exact configuration config.py already records as collapsing — and what collapses first is the events pages,
    i.e. precisely the pages with the most events to win.
    {RUNTIME.PY:73 "_BROWSER = _BROWSERS[0]"} {CONFIG.PY:22 "PILING ALL TABS ON ONE BROWSER STARVES AT HIGH N ... 48 TABS
     ON 1 BROWSER TIMED OUT THE EVENTS PAGES THEMSELVES (RENDER_SHOT 22S NAV TIMEOUT) AND EVENT YIELD COLLAPSED (NVDA 94→8)"}
    [CONFIDENCE: CONFIRMED 100% — all five drivers verified on the same single-browser handle before this change; the
     collapse mode is documented from a measured NVDA regression, not predicted]."""
    return next(_rr_browser) if _rr_browser is not None else _browser


def browser_available() -> bool:
    """Cheap check for callers that want to decide routing BEFORE building a job. Triggers the lazy launch."""
    return ensure_browser()


def run_on_loop(coro, timeout: float):
    """Marshal an already-created coroutine onto the dedicated Playwright loop and BLOCK the calling thread on its
    result. THE single crossing point from a crawl worker thread into the browser loop — every sync entry (render/
    render_shot/drive_*) funnels its on-loop coroutine through here. Raises whatever the coroutine raised (callers
    wrap in try/except → empty result on failure).

    CANCEL ON THE WAY OUT — this is the leak fix. `fut.result(timeout=)` only stops the CALLER waiting; the coroutine
    keeps running on the loop, still holding the browser context it opened and still occupying its `_sem` slot. Its
    `finally: await ctx.close()` cannot run until it finishes naturally — and a timeout is precisely the case where it
    is stuck in _goto/_settle and will not. Every timed-out render therefore leaked one context plus one semaphore
    permit, and engine.py retries the same url up to _RENDER_TRIES=3 times with backoff while the earlier attempts are
    still alive, so one slow host could hold three contexts at once. Leaked permits are never returned, so once enough
    accumulate the semaphore is exhausted and EVERY subsequent render blocks on it forever — the worker goes silent
    rather than erroring, which is the failure mode hardest to notice.
    Cancelling propagates CancelledError into the coroutine, which runs its `finally` immediately: context closed,
    permit returned. {INCIDENT 2026-07-27 "155 CHROMIUM PROCESSES / 9.49GB CHROME-HEADLESS RSS AFTER 12 MINUTES OF
    UPTIME" — steady-state concurrency cannot reach that in 12 minutes, a leak term is required to explain it}
    {ENGINE.PY "_RENDER_TRIES = INT(OS.ENVIRON.GET("EVENT_RENDER_TRIES", "3"))" — retries overlap the leaked attempts}
    [CONFIDENCE: CONFIRMED 100% — asyncio.Future.result(timeout) is documented to leave the coroutine running; the
     browser-pool cap shipped in 5b91b6a only lowers the BASELINE and, by shrinking the semaphore from 24 to 8 permits,
     actually makes exhaustion arrive with FEWER leaked slots. This is the fix that addresses the cause.]
    """
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return fut.result(timeout=timeout)
    except BaseException:                                 # timeout, KeyboardInterrupt, or the coroutine's own error
        fut.cancel()                                      # → CancelledError inside the coro → its finally closes ctx
        raise
