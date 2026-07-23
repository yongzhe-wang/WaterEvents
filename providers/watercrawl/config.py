"""watercrawl.config — every env-overridable knob in ONE place (pure constants, no imports of sibling modules).

用一句话讲完: 把原来散在 pool.py 顶部和函数体里的 magic number(并发页数 / nav 超时 / UA / wait-retry 阶梯 /
camoufox 并发帽)全部收到这一个无依赖模块 → 任何层 `from . import config` 读同一份配置,改一个默认值不用翻 5 个文件。
WHY 独立成文件: config 是整个 watercrawl 的叶子依赖(runtime/render/drivers/engines 都 import 它),它自己不 import
任何 sibling → 永远不会参与 circular import。{RESEARCH scrapy `settings/` 独立目录} [CONFIDENCE: CONFIRMED].
"""
from __future__ import annotations

import os

# Resident-browser pool size: how many pages render CONCURRENTLY on the one browser. Peak mem ≈ MAX_PAGES×~60MB +
# ~400MB browser, so 6 sits comfortably in a 4Gi worker. {POOL.PY:13 "6 并发 ≈ 760MB 舒服进 crawl worker 的 4Gi"}
# [CONFIDENCE: CONFIRMED — user's memory-budget note].
MAX_PAGES = int(os.environ.get("IR_WATERCRAWL_MAX_PAGES", "6"))

# Per-navigation goto timeout (ms). 22s covers a slow SSR + first-paint; the render coroutines add their own settle
# on top. {POOL.PY:19 "_NAV_TIMEOUT_MS ... '22000'"} [CONFIDENCE: CONFIRMED].
NAV_TIMEOUT_MS = int(os.environ.get("IR_WATERCRAWL_NAV_TIMEOUT_MS", "22000"))

# The UA every context sends — a real desktop Chrome string so a plain UA-sniff wall (the cheapest kind) passes.
# {POOL.PY:20 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ... Chrome/120.0 Safari/537.36"} [CONFIDENCE: CONFIRMED].
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# wait-retry ladder for JS/AJAX-late pages (detection._render_with_wait_retries): try WAIT_RETRIES times, each waiting
# base_wait + attempt*WAIT_STEP_MS, stopping as soon as the page is no longer a thin nav shell. An SSR page passes
# attempt 0 and never pays the extra waits. {POOL.PY:759-760, USER 2026-07-22 "retry ... each time the wait ms is
# longer"} [CONFIDENCE: CONFIRMED — Q4/Sitecore .aspx detail content is JS-loaded after the load event].
WAIT_RETRIES = int(os.environ.get("WATERCRAWL_WAIT_RETRIES", "1"))
WAIT_STEP_MS = int(os.environ.get("WATERCRAWL_WAIT_STEP_MS", "3000"))

# Camoufox (FB4) launches a FULL Firefox per call; a burst of the hardest walled pages would OOM on N concurrent
# Firefoxes. Bound concurrent launches. {POOL.PY:808-810 "camoufox launches a FULL Firefox per call ... Bound it"}
# [CONFIDENCE: CONFIRMED — OOM guard].
CAMOUFOX_CAP = int(os.environ.get("CAMOUFOX_CAP", "2"))
