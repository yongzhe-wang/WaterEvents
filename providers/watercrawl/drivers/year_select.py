"""watercrawl.drivers.year_select — AUTO-discover a year <select> FROM THE DOM and walk newest→oldest.

用一句话讲完: 直接读页面里 <select> 的 LIVE options 找出年份(Gregorian 2026/FY2026/2026年 + 日本/ROC 令和平成民國
纪年),然后逐年 selectedIndex+dispatch('change') 切换、抓每年的 AJAX 列表 → 合并 (text, deduped links)。WHY 读 DOM 而
非 markdown: q4/Evergreen 的年份 <option> 是 XHR 加载的("Loading"然后才出年份),所以年份从来不在 jina 的 markdown 里 —
从 TEXT 读年份的老办法找不到、整年归档被静默丢弃;Playwright 读 select.options 能拿到 markdown 拿不到的年份。{PROBE
2026-07-04 KMI news select "_CTRL0_CTL64_SELECTEVERGREENNEWSYEAR:LOADING" — options AJAX-loaded} [CONFIDENCE: CONFIRMED
— 驱动 DOM-read years 让 KMI news 7→63 detail links]. {POOL.PY:551-612}.
"""
from __future__ import annotations

from .. import config, extract_js, page, runtime

# Discover the year <select>: for each <select>, keep options whose normalized text matches a year pattern (Gregorian
# G with optional Q/FY/CJK-suffix, or era-year E); a select with ≥2 such options IS the year filter → return sorted
# newest-first. Fullwidth digits are normalized to ASCII. {POOL.PY:565-575}.
_DISCOVER_JS = """() => {
  const nm = s => (s||'').replace(/[\\uFF10-\\uFF19]/g, c => String.fromCharCode(c.charCodeAt(0)-0xFEE0)).trim();
  const G = /^(q[1-4][\\s'._-]*)?(fy[\\s'._-]?)?((19|20)\\d{2})([\\s'._-]*q[1-4])?\\s*(年|年度|년|년도)?$/i;
  const E = /^(令和|平成|昭和|大正|民國|民国)\\s*\\d{1,3}\\s*年(度)?$/;
  for (const s of document.querySelectorAll('select')) {
    const ys = [];
    for (const o of s.options) { const t = nm(o.textContent); if (t.length <= 12 && (G.test(t) || E.test(t))) ys.push(t); }
    if (ys.length >= 2) { ys.sort((a,b) => (b.match(/\\d{4}/)||['0'])[0] - (a.match(/\\d{4}/)||['0'])[0]); return ys; }
  }
  return [];
}"""


def _select_year_js(y: str) -> str:
    """Build the JS that finds the <select> holding option text == y, sets it, and dispatches a bubbling 'change'.
    y is embedded via repr() so any quotes/unicode in the year label are safely escaped. {POOL.PY:581-586}."""
    return ("var nm=function(s){return (s||'').replace(/[\\uFF10-\\uFF19]/g,function(c){"
            "return String.fromCharCode(c.charCodeAt(0)-0xFEE0);}).trim();};"
            "var ss=document.querySelectorAll('select');var s=null;"
            "for(var k=0;k<ss.length;k++){for(var j=0;j<ss[k].options.length;j++){"
            "if(nm(ss[k].options[j].text)==" + repr(y) + "){s=ss[k];s.selectedIndex=j;break;}}if(s)break;}"
            "if(s){s.dispatchEvent(new Event('change',{bubbles:true}));}")


async def _seq(url: str, max_years: int, wait_ms: int) -> tuple[str, list]:
    """Discover the year <select> from the DOM, then walk newest→oldest selecting each year + capturing its AJAX
    listing → merged (text, deduped links). ("", []) when there is no year <select>. Runs ON the loop. {POOL.PY:551-597}."""
    async with runtime._sem:
        ctx = await runtime._browser.new_context(user_agent=config.UA)
        try:
            pg = await page.new_blocked_page(ctx)
            await pg.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            await pg.wait_for_timeout(max(wait_ms, 0))   # let the <option>s AJAX-load before discovery
            years = await pg.evaluate(_DISCOVER_JS)
            if not years:
                return "", []
            merged_links: list = []
            merged_text: list = []
            for y in years[:max_years]:
                try:
                    await pg.evaluate("() => {" + _select_year_js(y) + "}")
                except Exception:                        # noqa: BLE001 — a year that won't select → skip, keep walking
                    pass
                await pg.wait_for_timeout(max(wait_ms, 0))
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                merged_text.append(out.get("text") or "")
                merged_links += list(out.get("links") or [])
            return "\n".join(merged_text), list(dict.fromkeys(merged_links))
        finally:
            await ctx.close()


def drive_year_select(url: str, max_years: int = 16, wait_ms: int = 5000) -> tuple[str, list]:
    """SYNC entry: auto-find the year <select> on url (reading the LIVE DOM, not the markdown) and walk every year →
    (merged_text, deduped_links). ("", []) when there is no year select or the browser is unavailable — so callers can
    invoke it UNCONDITIONALLY on any hub and it self-skips pages without a year filter. {POOL.PY:600-612}."""
    if not runtime.ensure_browser():
        return "", []
    try:
        budget_s = (config.NAV_TIMEOUT_MS / 1000) + (max_years + 1) * (max(wait_ms, 0) / 1000 + 2) + 30
        return runtime.run_on_loop(_seq(url, max_years, wait_ms), budget_s)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] drive_year_select failed for {url[:80]}: {error}", flush=True)
        return "", []
