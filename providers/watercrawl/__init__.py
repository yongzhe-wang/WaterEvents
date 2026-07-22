"""Watercrawl — a self-hosted, zero-cost "free firecrawl": a resident headless-Chromium crawler that renders
JS pages, clicks year dropdowns / tabs / load-more, and reads AJAX-loaded archives — everything firecrawl's
browser does, minus the paid residential-proxy stealth layer, for $0 and no API key.

用一句话讲完: 一个进程内常驻 Playwright Chromium(懒启动)+ 专属 asyncio loop 线程 + semaphore 限并发 →
crawl 的 ThreadPoolExecutor 线程提交一个 URL(可带注入 JS / 年份下拉驱动)→ 在 loop 上开隔离 context 渲染/点击/
抽 (text, links) → 同步返回。WHY 存在: firecrawl 卖的不是浏览器(Playwright 免费且镜像已装 chromium)而是住宅
代理 stealth 层; 渲染 + 驱动年份下拉这两件 firecrawl 干的活自托管免费就能做,把它们从 firecrawl.actions 搬到这里,
fc 用量塌到只剩真-bot-墙那 ~5%。{USER 2026-07-04 "why couldnt we build a free firecrawl ourselves"; "build the
new tool under providers called watercrawl ... put this entire system into that as a good tool"} [CONFIDENCE:
CONFIRMED — 直接指令; Cloud-Run probe 已验证: 容器内 Chromium launch + 驱动 KMI news 年份下拉 7→63 detail links].

内存: 1 browser(~400MB)+ 每 page ~60MB → 6 并发 ≈ 760MB, 舒服进 crawl worker 的 4Gi。{USER 2026-07-04
"正解是一个常驻浏览器 + 多个 page/context ... 6 并发 ≈ 760MB 舒服进 2Gi"}.

TWO APIs, same engine:
  - LOW-LEVEL (pool functions): render / drive_years / drive_clicks / drive_year_select — used by page.drive_archive
    and discover_company_events for the year-archive walks. Each returns (text, links) or a list thereof, and
    self-skips ("" / []) when the browser is unavailable so callers fall back to jina/firecrawl.
  - FIRECRAWL-SHAPED (watercrawl_client): `.scrape(url, actions=...) -> doc.markdown/.links/.html` — a drop-in
    for `firecrawl_client()` so a render can move off paid firecrawl with a one-line import swap.

Chromium ships in the image {DOCKERFILE:34 "PYTHON -M PLAYWRIGHT INSTALL --WITH-DEPS CHROMIUM"}; the launch is
container-safe (--disable-dev-shm-usage) — the same fix _capture.py uses for gen2 Cloud Run's tiny /dev/shm.
"""
from .pool import (render, render_full, render_shot, drive_years, drive_clicks, drive_year_select, drive_year_bar,
                   drive_load_more, browser_available, dead_host)   # render_shot = open page + full-page screenshot (VL)
# drive_year_bar (Pass-2c tab/button year-filter driver, pool.py:628) was added 2026-07-12 but NEVER re-exported
# here → `watercrawl.drive_year_bar(...)` raised AttributeError on EVERY hub with a year bar, and because that call
# runs in a crawl-pool worker thread its exception was swallowed by discover._process_done → the page's whole-year
# archive was SILENTLY dropped (0 events for tab-bar-archive IR sites). The teacher (teacher_collect.py:71) hit the
# same crash. Surfaced only after the 2026-07-12 traceback fix made the swallow LOUD. {AUDIT 2026-07-12 e2e demo:
# BAESY/267250.KS crawled to 0 events; log 'module watercrawl has no attribute drive_year_bar, did you mean drive_years'}.
from .client import WaterDoc, WatercrawlClient, watercrawl_client

# `available` is the friendlier public name; `browser_available` stays as the internal alias the pool exposes.
available = browser_available

__all__ = [
    "render", "render_full", "render_shot", "drive_years", "drive_clicks", "drive_year_select", "drive_year_bar", "drive_load_more",
    "available", "browser_available",
    "WaterDoc", "WatercrawlClient", "watercrawl_client",
]
