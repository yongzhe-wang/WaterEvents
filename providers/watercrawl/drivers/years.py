"""watercrawl.drivers.years — walk N year-views in ONE page session via caller-supplied inject-JS (the low-level driver).

用一句话讲完: caller 传一串 select-year 的 JS(每个切一个年份),我们 goto 一次、然后逐条 evaluate(切年)+wait+抽取 →
返回每年一个 (text, links)。WHY 一个 session 复用页面(vs firecrawl 每年一次 scrape): 每次 year-select CHANGE 在同一个
DOM 里替换内容,复用 page 既正确又比开 16 个浏览器 session 便宜得多。这是 year_select(自动发现年份)的低层版:年份 JS
由 caller 给。{POOL.PY:426-447, 959-970} [CONFIDENCE: CONFIRMED — self-hosted 替代 drive_archive 的 firecrawl per-year loop].
"""
from __future__ import annotations

from .. import config, extract_js, page, runtime


async def _seq(url: str, js_list: list, wait_ms: int) -> list:
    """goto once, then per year evaluate(select-year JS)+wait+extract → list of (text, links), one per js. Runs ON
    the loop. {POOL.PY:426-447}."""
    async with runtime._sem:
        ctx = await runtime._browser.new_context(user_agent=config.UA)
        results: list = []
        try:
            pg = await page.new_blocked_page(ctx)
            await pg.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            for js in js_list:
                try:
                    await pg.evaluate("() => {" + js + "}")
                except Exception:                        # noqa: BLE001 — a bad year-JS must not sink the whole walk
                    pass
                await pg.wait_for_timeout(max(wait_ms, 0))
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                results.append((out.get("text") or "", list(out.get("links") or [])))
            return results
        finally:
            await ctx.close()


def drive_years(url: str, js_list: list, wait_ms: int = 4000) -> list:
    """SYNC entry: walk the year-<select> views for url in ONE session → list of (text, links). Empty list on failure /
    no browser / no js_list. Self-hosted replacement for drive_archive's firecrawl one-scrape-per-year loop. {POOL.PY:959-970}."""
    if not js_list or not runtime.ensure_browser():
        return []
    try:
        budget_s = (config.NAV_TIMEOUT_MS / 1000) + len(js_list) * (max(wait_ms, 0) / 1000 + 2) + 30
        return runtime.run_on_loop(_seq(url, js_list, wait_ms), budget_s)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] drive_years failed for {url[:80]}: {error}", flush=True)
        return []
