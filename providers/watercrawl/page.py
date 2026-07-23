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


async def settle(pg, wait_ms: int) -> None:
    """Give a JS/XHR-driven IR list time to POPULATE before we snapshot: wait for network idle (bounded, so an
    analytics-polling page that never idles doesn't hang), then poll a cheap DOM-size signal until it stabilizes,
    then a fixed settle. WHY: IR event/news lists load via XHR AFTER domcontentloaded — snapshotting too early yields
    only the nav shell (KMI news: 2 links at 3s vs 77+ once the list AJAX lands). Shared by render.py + the
    load_more/year_bar drivers. {POOL.PY:279-306} [CONFIDENCE: CONFIRMED — the list is XHR-late]."""
    try:
        await pg.wait_for_load_state("networkidle", timeout=8000)
    except Exception:                                     # noqa: BLE001 — never-idle page → fall through to the poll
        pass
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
