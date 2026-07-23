"""Watercrawl — a self-hosted, zero-cost "free firecrawl": a resident headless-Chromium crawler that renders
JS pages, clicks year dropdowns / tabs / load-more, and reads AJAX-loaded archives — everything firecrawl's
browser does, minus the paid residential-proxy stealth layer, for $0 and no API key.

用一句话讲完: 一个进程内常驻 Playwright Chromium(懒启动)+ 专属 asyncio loop 线程 + semaphore 限并发 →
crawl 的 ThreadPoolExecutor 线程提交一个 URL(可带注入 JS / 年份下拉驱动)→ 在 loop 上开隔离 context 渲染/点击/
抽 (text, links) → 同步返回。WHY 存在: firecrawl 卖的不是浏览器(Playwright 免费且镜像已装 chromium)而是住宅
代理 stealth 层; 渲染 + 驱动年份下拉这两件 firecrawl 干的活自托管免费就能做。{USER 2026-07-04 "why couldnt we build
a free firecrawl ourselves"; "build the new tool under providers called watercrawl ... put this entire system into
that as a good tool"} [CONFIDENCE: CONFIRMED].

内存: 1 browser(~400MB)+ 每 page ~60MB → 6 并发 ≈ 760MB。{USER 2026-07-04 "正解是一个常驻浏览器 + 多个 page/context"}.

PACKAGE LAYOUT (refactored 2026-07-23 from the 970-line god-module pool.py — WHY: pool.py mixed 6 responsibilities
into one file; split by responsibility so each layer is single-purpose and well under the 1500-line cap, and the
PDF path was de-hardcoded off the old `src.agents.company_agent` coupling into a self-contained engine):
  - config.py      env knobs        - runtime.py   loop thread + browser lifecycle + shared state
  - extract_js.py  DOM→(text,links,inline) serializer     - page.py   page factory + resource blocking + goto/settle
  - render.py      core render coroutines + render()/render_shot()    - detection.py  wall/thin/dead predicates
  - orchestrator.py  render_full/render_detail multi-engine fallback chains
  - engines/       impersonate (curl_cffi) · camoufox (stealth FB4) · pdf (self-contained, de-hardcoded)
  - drivers/       years · clicks · load_more · year_select · year_bar   (pagination/expansion)
  - client.py      firecrawl-shaped WaterDoc facade        - host_health.py  per-host health/backoff

TWO APIs, same engine:
  - LOW-LEVEL (functions): render / render_full / render_detail / render_shot / drive_* — each returns (text, links)
    (or a shot dict) and self-skips ("" / []) when the browser is unavailable so callers fall back.
  - FIRECRAWL-SHAPED (watercrawl_client): `.scrape(url, actions=...) -> doc.markdown/.links/.html` — a drop-in for
    `firecrawl_client()` so a render can move off paid firecrawl with a one-line import swap.

Chromium ships in the image {DOCKERFILE:34 "PYTHON -M PLAYWRIGHT INSTALL --WITH-DEPS CHROMIUM"}; the launch is
container-safe (--disable-dev-shm-usage).
"""
# render lane — render()/render_shot() live in render.py; the heavier render_full/render_detail fallback chains in
# orchestrator.py. render_shot = open page + full-page screenshot (the VL project's sole entry).
from .render import render, render_shot
from .orchestrator import render_full, render_detail

# interaction drivers — each self-skips when its control is absent, so callers invoke them unconditionally on any hub.
from .drivers.years import drive_years
from .drivers.clicks import drive_clicks
from .drivers.load_more import drive_load_more
from .drivers.year_select import drive_year_select
from .drivers.year_bar import drive_year_bar

# lifecycle + wall predicates exposed to callers that route BEFORE building a job / classify a dead host.
from .runtime import browser_available
from .detection import dead_host

# firecrawl-shaped facade
from .client import WaterDoc, WatercrawlClient, watercrawl_client

# `available` is the friendlier public name; `browser_available` stays as the internal alias the runtime exposes.
available = browser_available

__all__ = [
    "render", "render_full", "render_detail", "render_shot",
    "drive_years", "drive_clicks", "drive_year_select", "drive_year_bar", "drive_load_more",
    "available", "browser_available", "dead_host",
    "WaterDoc", "WatercrawlClient", "watercrawl_client",
]
