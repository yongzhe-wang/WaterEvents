"""watercrawl.runtime — the ONE owner of shared browser state + the dedicated asyncio loop thread.

用一句话讲完: 整个 watercrawl 只有一个常驻浏览器进程 + 一条专属 asyncio loop 线程 + 一个限并发 semaphore,这些
可变全局状态**只此一份、只住在这个模块里** → render/drivers/orchestrator 全部 `from . import runtime` 读 `runtime._browser`
/`runtime._loop`,谁都不再各持一份 → 拆成多文件也不会状态分叉。任何跨线程调用协程都走 `runtime.run_on_loop(coro, timeout)`。
WHY 一个模块独占状态: async_playwright 对象绑定创建它的 loop,crawl 的 ThreadPoolExecutor worker 线程没有 loop,所以
我们自己拥有一条 loop 线程、用 run_coroutine_threadsafe marshal 过去。把这套 lifecycle + 状态收进 runtime.py 是拆分
god-module 的地基 —— 只有"状态单一持有者"才能让 render.py / drivers/*.py 安全地引用同一个 browser。
{RESEARCH crawl4ai `browser_manager.py` 把浏览器生命周期独立成模块} [CONFIDENCE: CONFIRMED — Playwright loop-affinity 约束].

Design invariants (迁移自原 pool.py 文件头,逐条保留):
  - ONE browser per worker process, launched lazily on first use, kept warm for the process lifetime.
  - ALL Playwright calls run on ONE dedicated event-loop thread (loop-affinity).
  - A Semaphore bounds CONCURRENT pages so peak memory stays inside the worker's limit.
  - Best-effort: launch failure flips _dead so callers fall back to impersonate/jina — render is NEVER a hard dep.
"""
from __future__ import annotations

import asyncio
import threading

from . import config

# ── shared mutable state (THE single copy; other modules read these via `runtime.<name>`) ──────────────────────
_lock = threading.Lock()                                  # guards the lazy launch (one launch across N caller threads)
_loop: asyncio.AbstractEventLoop | None = None            # the dedicated Playwright loop
_loop_thread: threading.Thread | None = None
_browser = None                                           # default headless Chromium (the fast path)
_browser_h1 = None                                        # HTTP/1.1-forced Chromium (ERR_HTTP2 retry lane)
_browser_proxy = None                                     # patchright + webshare residential STEALTH browser (walls)
_playwright = None                                        # the async_playwright driver for the two Chromiums
_playwright_stealth = None                                # the patchright driver for the residential browser
_sem: asyncio.Semaphore | None = None                     # bounds concurrent pages (created on the loop in _launch)
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
    global _browser, _playwright, _sem, _browser_h1, _browser_proxy, _playwright_stealth
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    # Container-safe flags (--disable-dev-shm-usage) + cache/GPU trims so peak render memory stays low.
    _shared_args = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer",
                    "--disable-extensions", "--disk-cache-size=1", "--media-cache-size=1"]
    _browser = await _playwright.chromium.launch(headless=True, args=_shared_args)
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
    _sem = asyncio.Semaphore(config.MAX_PAGES)            # bound concurrent pages (created on THIS loop)


def ensure_browser() -> bool:
    """Lazily start loop + launch browsers, blocking the CALLER until ready. Returns True if the browser is usable,
    False if launch failed (→ caller falls back to impersonate/jina). Thread-safe via _lock. (Was
    pool._ensure_browser_blocking — same behavior, same _dead-latch so a broken env is never retried per call.)"""
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
            print(f"[watercrawl] resident Chromium launched (max_pages={config.MAX_PAGES}) — self-hosted render lane UP", flush=True)
            return True
        except Exception as error:                        # noqa: BLE001 — a broken env must disable render, not crash
            print(f"[watercrawl] launch failed, self-hosted render disabled this process: {error}", flush=True)
            _dead = True
            return False


def browser_available() -> bool:
    """Cheap check for callers that want to decide routing BEFORE building a job. Triggers the lazy launch."""
    return ensure_browser()


def run_on_loop(coro, timeout: float):
    """Marshal an already-created coroutine onto the dedicated Playwright loop and BLOCK the calling thread on its
    result. THE single crossing point from a crawl worker thread into the browser loop — every sync entry (render/
    render_shot/drive_*) funnels its on-loop coroutine through here. Raises whatever the coroutine raised (callers
    wrap in try/except → empty result on failure)."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)
