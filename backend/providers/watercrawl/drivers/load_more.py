"""watercrawl.drivers.load_more — AUTO-walk a 'load more' / infinite-scroll archive to EXHAUSTION in ONE session.

用一句话讲完: 自动找页面上的"加载更多"按钮(文案含 load/show/view more/older…)点它,没按钮就滚到底(无限滚动兜底),
每轮重新数链接数,连续 2 轮不增长就判定走完(no-growth convergence = 完整性信号),或到 max_rounds。WHY 一个 session +
重新计数: load-more/滚动是往同一个 DOM 追加行,所以保持页面重新测量 —— "不再增长"就是"抓全了",替代了脆弱的"<30 链接
就算被墙"的猜测。对任意 hub 无脑调用安全:没 load-more 又不增长的页 ~2 轮收敛返回其渲染列表。{USER
2026-07-10 "also fix completeness"} [CONFIDENCE: INFERRED — 镜像 year_select 的 session 模式;收敛停止把不增长的页限在 ~2 轮].
"""
from __future__ import annotations

from .. import config, extract_js, page, runtime

# Find a 'load more'/'older' control (button/link/role=button/pager class); click it. No control → scroll to bottom
# (infinite-scroll fallback).
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


async def _seq(url: str, max_rounds: int, wait_ms: int) -> tuple[str, list, str]:
    """Click/scroll, wait, re-count links; stop when the count stops growing (2 stable rounds = complete) or max_rounds.
    Returns ("", [], "") if the list never grew past ~5% of its initial size (no real load-more here). The 3rd element is
    the INLINE `[anchor](url)` reading-order text — the discovery event-extractor's primary context. Runs ON the loop."""
    async with runtime._sem:
        ctx = await runtime.next_browser().new_context(user_agent=config.UA)   # POOL, not browsers[0] — see runtime.next_browser
        try:
            pg = await page.new_blocked_page(ctx)
            await page.goto(pg, url)                      # shared goto (1 transient retry)
            await page.settle(pg, 0)                      # networkidle + DOM-size stabilization so `initial` is the real count
            initial = await pg.evaluate("() => document.querySelectorAll('a[href]').length")
            prev, stable = initial, 0
            for _ in range(max(max_rounds, 1)):
                try:
                    await pg.evaluate(_LOADMORE_JS)
                except Exception:                        # noqa: BLE001
                    pass
                await pg.wait_for_timeout(max(wait_ms, 0))
                n = await pg.evaluate("() => document.querySelectorAll('a[href]').length")
                if n == prev:
                    stable += 1
                    if stable >= 2:                      # 2 stable rounds → the list is exhausted
                        break
                else:
                    stable = 0
                prev = n
            if prev <= initial * 1.05:                   # never grew >5% → no real load-more; let caller use plain render
                return "", [], ""
            out = await pg.evaluate(extract_js.EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or []), out.get("inline") or ""
        finally:
            await ctx.close()


def drive_load_more(url: str, max_rounds: int = 40, wait_ms: int = 1500) -> tuple[str, list, str]:
    """SYNC entry: AUTO-walk a load-more / infinite-scroll list to exhaustion → (accumulated_text, deduped_links, inline).
    ("", [], "") on failure / no browser / no growth. Safe to call UNCONDITIONALLY on any hub — a page with no load-more
    and no scroll-growth simply converges in ~2 rounds and self-skips. The 3rd element (inline `[anchor](url)`) is what the
    discovery event-extractor consumes."""
    if not runtime.ensure_browser():
        return "", [], ""
    try:
        budget_s = (config.NAV_TIMEOUT_MS / 1000) + max(max_rounds, 1) * (max(wait_ms, 0) / 1000 + 1) + 30
        return runtime.run_on_loop(_seq(url, max_rounds, wait_ms), budget_s)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] drive_load_more failed for {url[:80]}: {error}", flush=True)
        return "", [], ""
