"""watercrawl.drivers.clicks — 'load more' style: click the SAME control N times in ONE session so the list ACCUMULATES.

用一句话讲完: caller 给一段 click JS(点某个"加载更多"控件),我们在同一个 page session 里点 `times` 次,列表逐次
APPEND 累积,最后抽取一次 → (text, links)。WHY 一个 session + 重复点击(vs years 的 replace): load-more 是往同一个 DOM
追加行,所以必须保持同一页面反复点、不能 reload。这是 caller 已知 click JS 时的显式版;load_more 是自动发现按钮的版本。
[CONFIDENCE: CONFIRMED — self-hosted 替代 firecrawl click_more action 链].
"""
from __future__ import annotations

from .. import config, extract_js, page, runtime


async def _seq(url: str, click_js: str, times: int, wait_ms: int) -> tuple[str, list]:
    """Click click_js `times` times in ONE page session (list ACCUMULATES), then extract once → (text, links). Runs
    ON the loop."""
    async with runtime._sem:
        ctx = await runtime.next_browser().new_context(user_agent=config.UA)   # POOL, not browsers[0] — see runtime.next_browser
        try:
            pg = await page.new_blocked_page(ctx)
            await pg.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            for _ in range(max(times, 1)):
                try:
                    await pg.evaluate("() => {" + click_js + "}")
                except Exception:                        # noqa: BLE001 — a click that throws must not abort the walk
                    pass
                await pg.wait_for_timeout(max(wait_ms, 0))
            out = await pg.evaluate(extract_js.EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or [])
        finally:
            await ctx.close()


def drive_clicks(url: str, click_js: str, times: int, wait_ms: int = 2500) -> tuple[str, list]:
    """SYNC entry: 'load more' walk — click `click_js` `times` times in one session, accumulate, extract → (text, links).
    ("", []) on failure / no browser / no click_js → caller falls back."""
    if not click_js or not runtime.ensure_browser():
        return "", []
    try:
        budget_s = (config.NAV_TIMEOUT_MS / 1000) + max(times, 1) * (max(wait_ms, 0) / 1000 + 2) + 30
        return runtime.run_on_loop(_seq(url, click_js, times, wait_ms), budget_s)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] drive_clicks failed for {url[:80]}: {error}", flush=True)
        return "", []
