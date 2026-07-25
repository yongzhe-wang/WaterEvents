"""watercrawl.drivers.year_bar — walk a clickable YEAR-BAR (year tabs/buttons/links, NOT a <select>) newest→oldest.

用一句话讲完: 有些年份过滤不是 <select> 而是一排可点的年份 tab/button/chip(或藏在一个下拉里)—— 先尝试打开可能的
下拉,发现可见的年份 chip(Gregorian + 日本/ROC 纪年,>8 字的当事件标题排除),然后逐年点它抓列表;如果点完 chip 没了
(AJAX re-render 掉了 or 上次点击导航走了),就重新 goto hub 再试。WHY 再-goto 兜底(vs <select> 的纯单 session): 年份
chip 可能"就地替换列表"也可能"导航到 per-year URL"——re-goto 让两种都成立,同时对常见的"替换"情形仍单 session 快。<2 个
年份 chip 就自跳过(孤零零一个 '2026' 版权号不是过滤器)。{USER 2026-07-12 "year filter ... dropdown bar"} [CONFIDENCE:
INFERRED 80% — 镜像已验证的 <select>/load-more 走法;exact-text chip 匹配仍需 live-site 复核].
"""
from __future__ import annotations

from .. import config, extract_js, page, runtime

# Open any collapsed dropdown/combobox that might HOLD the year chips (aria-haspopup, dropdown-toggle, year-select
# class…) so the chips become visible + clickable before discovery.
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

# Discover VISIBLE year chips (≤8 chars so an event title isn't mistaken for a year): Gregorian dedup by 4-digit,
# era-year dedup by raw text; sort newest-first (era chips yr=0 trail). Returns RAW normalized chip text so the click
# below matches it exactly.
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

# Click the chip whose normalized text == the given year label.
_YEARBAR_CLICK_JS = """(chip) => {
  const nm = s => (s||'').replace(/[\\uFF10-\\uFF19]/g, c => String.fromCharCode(c.charCodeAt(0)-0xFEE0)).trim();
  for (const el of document.querySelectorAll('a,button,li,span,div,[role="tab"],[role="option"],[role="menuitem"],[role="button"]')) {
    if (el.offsetParent === null) continue;
    if (nm(el.innerText||el.textContent) === chip) { el.scrollIntoView({block:'center'}); el.click(); return true; }
  }
  return false;
}"""


async def _seq(url: str, max_years: int, wait_ms: int) -> tuple[str, list]:
    """Discover year chips, then per year re-goto + open-dropdown + click chip + capture → merged (text, deduped links).
    Self-skips ("", []) when <2 year chips exist (a lone '2026' copyright label is not a filter). Runs ON the loop."""
    async with runtime._sem:
        ctx = await runtime._browser.new_context(user_agent=config.UA)
        try:
            pg = await page.new_blocked_page(ctx)
            await page.goto(pg, url)
            await page.settle(pg, wait_ms)
            try:
                await pg.evaluate(_DROPDOWN_OPEN_JS)     # reveal chips hidden inside a collapsed dropdown
            except Exception:                            # noqa: BLE001
                pass
            await pg.wait_for_timeout(min(max(wait_ms, 0), 2500))
            years = list(await pg.evaluate(_YEARBAR_DISCOVER_JS))[:max_years]
            if len(years) < 2:                           # <2 chips ⇒ not a year filter → self-skip
                return "", []
            merged_text: list = []
            merged_links: list = []
            for y in years:
                try:
                    # re-goto each year: a chip may REPLACE the list in place OR navigate to a per-year URL — re-goto
                    # makes BOTH work while staying single-session-fast for the common REPLACE case.
                    await page.goto(pg, url)
                    await page.settle(pg, wait_ms)
                    try:
                        await pg.evaluate(_DROPDOWN_OPEN_JS)
                    except Exception:                    # noqa: BLE001
                        pass
                    await pg.wait_for_timeout(min(max(wait_ms, 0), 2000))
                    if not await pg.evaluate(_YEARBAR_CLICK_JS, y):   # chip gone this round → skip it
                        continue
                    await pg.wait_for_timeout(max(wait_ms, 0))
                    out = await pg.evaluate(extract_js.EXTRACT_JS)
                    merged_text.append(out.get("text") or "")
                    merged_links += list(out.get("links") or [])
                except Exception:                        # noqa: BLE001 — one year failing must not sink the rest
                    pass
            return "\n".join(merged_text), list(dict.fromkeys(merged_links))
        finally:
            await ctx.close()


def drive_year_bar(url: str, max_years: int = 16, wait_ms: int = 5000) -> tuple[str, list]:
    """SYNC entry: drive a YEAR-BAR (clickable year tabs/buttons, not a <select>) → (merged_text, deduped_links).
    ('', []) when there is no year bar or the browser is unavailable — so callers invoke it UNCONDITIONALLY right
    after drive_year_select and it self-skips pages whose year filter is a <select> (already driven) or absent."""
    if not runtime.ensure_browser():
        return "", []
    try:
        budget_s = (config.NAV_TIMEOUT_MS / 1000) + (max_years + 1) * (config.NAV_TIMEOUT_MS / 1000 + max(wait_ms, 0) / 1000 + 5) + 30
        return runtime.run_on_loop(_seq(url, max_years, wait_ms), budget_s)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] drive_year_bar failed for {url[:80]}: {error}", flush=True)
        return "", []
