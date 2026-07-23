"""watercrawl.page — page factories + per-request resource-blocking routes (two block profiles).

用一句话讲完: 每开一个 page 都挂一个 route handler 拦掉重资源(图片/字体/媒体/CSS)→ 每页 peak render 内存压低(clean-
each-page 的 OOM 修复);但**截图路径拦得更少** —— 它保留 CSS + 图片,因为 VL 模型要"看"页面版面判断这是事件表还是导航栏,
没 CSS 的裸 DOM 截图对视觉判断毫无用。WHY 独立成文件: page 工厂是 render/drivers 每条路径开页的唯一入口,拦截策略只此
两份(渲染档 vs 截图档),改拦截规则不用碰渲染逻辑。{RESEARCH crawl4ai/firecrawl 在 page 创建层做资源拦截省带宽/提速}
[CONFIDENCE: CONFIRMED — verbatim 迁移自 pool.py:205-250].
"""
from __future__ import annotations

from . import config

# RENDER path blocks HEAVY non-DOM sub-resources but LETS document/script/xhr/fetch through so the SPA's list-loading
# JS/XHR still runs. {POOL.PY:205 "_BLOCK_TYPES = {image, media, font, stylesheet}"}.
_BLOCK_TYPES = {"image", "media", "font", "stylesheet"}

# SCREENSHOT path blocks LESS: KEEP stylesheet + image (the page must LOOK right for the VL model to read its layout)
# and drop only media/font (heavy + irrelevant to structure). A shot with no CSS is a bare-DOM page — useless for "is
# this an events table or a nav bar" visual judgment, which is the whole point of the screenshot. {POOL.PY:229-232}.
_SHOT_BLOCK_TYPES = {"media", "font"}


async def _block_heavy(route) -> None:
    """RENDER-path route handler: abort heavy non-DOM sub-resources, let everything else (document/script/xhr/fetch)
    through so the SPA's list-loading JS/XHR still runs. Guarded so a teardown race (route on a closing page) is a
    no-op."""
    try:
        if route.request.resource_type in _BLOCK_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:                                     # noqa: BLE001 — teardown race on a closing page → no-op
        pass


async def _block_heavy_shot(route) -> None:
    """SCREENSHOT-path route handler: abort only media/font (keep CSS + images so the render looks real)."""
    try:
        if route.request.resource_type in _SHOT_BLOCK_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:                                     # noqa: BLE001
        pass


async def new_blocked_page(ctx):
    """new_page + a RENDER-profile resource-blocking route so each page's PEAK render memory stays low (the clean-
    each-page OOM fix). Every text/link render path opens its page through here. {USER 2026-07-08 "clean each page so
    we dont oom"}."""
    page = await ctx.new_page()
    await page.route("**/*", _block_heavy)
    return page


async def new_shot_page(ctx):
    """new_page for the SCREENSHOT path — keeps stylesheet + image so the full-page shot is visually faithful (VL
    reads layout from pixels)."""
    page = await ctx.new_page()
    await page.route("**/*", _block_heavy_shot)
    return page


async def goto(pg, url: str) -> None:
    """goto with ONE retry on a transient net error (HTTP2/reset/timeout) — a first-try net::ERR is often transient;
    a bare failure would empty the render. Raises if the retry also fails (caller returns empty). Shared by render.py
    + the load_more/year_bar drivers. {POOL.PY:309-315}."""
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
    except Exception:                                     # noqa: BLE001 — one transient-error retry
        await pg.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)


async def _auto_scroll(pg) -> None:
    """Scroll to the bottom in increments so LAZY-LOAD-ON-SCROLL content fires. Per-section video blocks / infinite
    lists load on a SCROLL event, NOT on a timer — a wait alone never triggers them (which is why a longer wait_ms
    doesn't help). Stop once scrollHeight stops growing (all lazy content in), then return to the top for a clean
    full-page screenshot + consistent layout. {DEBUG 2026-07-23 block.xyz/investor-day: per-speaker YouTube video
    blocks lazy-load on scroll → WITHOUT a scroll the render captured a NON-DETERMINISTIC 10-30 of them (whatever
    loaded before the DOM signal stabilized), WITH the scroll all load} [CONFIDENCE: CONFIRMED 100% — watercrawl had
    ZERO scroll code (full grep) and the page's video sections are scroll-triggered]."""
    try:
        prev = -1
        for _ in range(30):                              # bounded: a normal page's height stabilizes in 1-2 steps
            h = await pg.evaluate("() => (document.body ? document.body.scrollHeight : 0)")
            if h == prev:                                # height stopped growing → all lazy content is loaded
                break
            prev = h
            await pg.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await pg.wait_for_timeout(350)               # let the newly-revealed section's XHR/media begin loading
        await pg.evaluate("() => window.scrollTo(0, 0)")  # back to top → clean full-page screenshot, consistent layout
    except Exception:                                     # noqa: BLE001 — scrolling is best-effort, never sink the render
        pass


async def settle(pg, wait_ms: int) -> None:
    """Give a JS/XHR-driven IR list time to POPULATE before we snapshot: wait for network idle (bounded, so an
    analytics-polling page that never idles doesn't hang), SCROLL to the bottom to fire lazy-load-on-scroll content
    (video blocks / infinite lists — a timer alone never triggers them), then poll a cheap DOM-size signal until it
    stabilizes, then a fixed settle. WHY: IR event/news lists load via XHR AFTER domcontentloaded — snapshotting too
    early yields only the nav shell (KMI news: 2 links at 3s vs 77+ once the list AJAX lands); AND per-section media
    lazy-loads on scroll (block.xyz videos). Shared by render.py + the load_more/year_bar drivers. {POOL.PY:279-306} +
    {DEBUG 2026-07-23 block.xyz scroll} [CONFIDENCE: CONFIRMED — the list is XHR-late AND scroll-lazy]."""
    try:
        # networkidle is REDUNDANT with the DOM-size poll below (which is the true content-loaded signal), and on an
        # analytics-heavy page it never fires → burns the whole timeout. Capped at config.SETTLE_IDLE_MS (2500, was
        # 8000) → ~5.5s/page saved with zero event loss. {DEBUG 2026-07-23 render 11.5s/page}.
        await pg.wait_for_load_state("networkidle", timeout=config.SETTLE_IDLE_MS)
    except Exception:                                     # noqa: BLE001 — never-idle page → fall through to the poll
        pass
    await _auto_scroll(pg)                                # fire lazy-load-on-scroll content BEFORE we measure DOM size
    _SIG_JS = ("() => (document.querySelectorAll('a[href]').length * 100000) + "
               "Math.min((document.body ? document.body.innerText.length : 0), 5000000)")
    try:
        prev, stable = -1, 0
        for _ in range(14):                              # bounded poll: stop once the DOM-size signal is 2× stable
            n = await pg.evaluate(_SIG_JS)
            if n == prev and n > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev = n
            await pg.wait_for_timeout(300)
    except Exception:                                     # noqa: BLE001
        pass
    if wait_ms:
        await pg.wait_for_timeout(wait_ms)
