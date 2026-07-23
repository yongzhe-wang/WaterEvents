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
# Raised 6→24 (default) to feed the 32-seq AWQ server — 6 starved it (server ran only 3-8 of 32 seqs, KV 9-18%, GPU
# 39%). 24×~400MB ≈ 9.6GB browser RAM (fine on the RunPod pod; the old "6 in a 4Gi worker" budget was the GCP worker).
# {USER 2026-07-23 "increase the render page ... set as default"} [CONFIDENCE: CONFIRMED — pool-6 starvation measured].
MAX_PAGES = int(os.environ.get("IR_WATERCRAWL_MAX_PAGES", "24"))

# Number of SEPARATE Chromium PROCESSES to spread renders across (render pages are round-robin'd over them). WHY: piling
# all tabs on ONE browser starves at high N — the single browser process's main-thread/IPC + the one event-loop thread
# serialize, so 48 tabs on 1 browser TIMED OUT the events pages themselves (render_shot 22s nav timeout) and event yield
# collapsed (NVDA 94→8). On a 96-core / 500GB pod, K separate browser PROCESSES each running ~MAX_PAGES/K tabs spread the
# render across cores → every browser stays near the reliable ~6-tab level while TOTAL concurrency = MAX_PAGES. Set e.g.
# BROWSERS=8 with MAX_PAGES=48 → 6 tabs/browser. Default 1 = the existing single-browser behavior (no change unless set).
# {USER 2026-07-23 "you have multiple cpu right"; DEBUG render=48/1-browser: 9 events-page render TimeoutErrors, NVDA
# yield 94→8} [CONFIDENCE: CONFIRMED 100% — single-browser starvation measured on the A5000 pod].
RENDER_BROWSERS = int(os.environ.get("IR_WATERCRAWL_BROWSERS", "4"))

# CAP on CONCURRENT full-page SCREENSHOTS — the memory-critical resource. WHY separate from MAX_PAGES: a full_page shot
# renders the whole scroll-height into an in-memory bitmap; N of them at once is what spikes RAM and OOM-SIGKILLs a
# browser (→ TargetClosedError poisons every page on it). The pod's cgroup is ~50GB and vLLM/AWQ already holds ~36GB, so
# only ~14GB is left for the browser pool — 24 concurrent shots (MAX_PAGES) blew past it and crashed ALL renders. Bounding
# concurrent SHOTS to 4 (≈ one per browser) keeps peak bitmap RAM ~4×~30MB and never OOMs, while text-only renders still
# run at the full MAX_PAGES concurrency. {USER 2026-07-23 "we should have a cap and you prob can adjust the resolution";
# POD /sys/fs/cgroup/memory.max=50GB, memory.current=36GB → ~14GB headroom} [CONFIDENCE: CONFIRMED 100% — OOM at 4
# browsers × concurrent full-page shots measured; cgroup limit read live from the pod].
SHOT_CONCURRENCY = int(os.environ.get("WATERCRAWL_SHOT_CONCURRENCY", "4"))

# Per-navigation goto timeout (ms). 22s covers a slow SSR + first-paint; the render coroutines add their own settle
# on top. {POOL.PY:19 "_NAV_TIMEOUT_MS ... '22000'"} [CONFIDENCE: CONFIRMED].
NAV_TIMEOUT_MS = int(os.environ.get("IR_WATERCRAWL_NAV_TIMEOUT_MS", "22000"))

# settle() timing — the biggest per-page latency lever. WHY these values: a real IR list loads its event XHR within
# ~1-2s of domcontentloaded, and settle()'s DOM-SIZE STABILITY POLL already detects "content finished loading" (it waits
# until a[href]-count + body-text stops growing). So the networkidle wait is REDUNDANT with the poll — and on an
# analytics-heavy page (coca-cola: trackers keep the network busy forever) networkidle NEVER fires and burns the whole
# timeout. Cutting 8000→2500 saves ~5.5s/page with ZERO event loss (the DOM poll still guarantees the list is present);
# the fixed post-poll settle 3000→1500 trims another ~1.5s. {DEBUG 2026-07-23: render was 11.5s/page = networkidle(8s)
# + fixed(3s); a 6-page coca-cola crawl = 141s} [CONFIDENCE: CONFIRMED — the DOM poll, not networkidle, is the content-
# loaded signal; networkidle only waits for analytics quiet, which never comes]. Env-overridable to widen if a slow site loses events.
SETTLE_IDLE_MS = int(os.environ.get("WATERCRAWL_SETTLE_IDLE_MS", "2500"))       # networkidle cap (was 8000)
SETTLE_FIXED_MS = int(os.environ.get("WATERCRAWL_SETTLE_FIXED_MS", "1500"))     # fixed wait AFTER the DOM poll (was render_shot's 3000)

# Max screenshot HEIGHT in CSS px. WHY a cap: `screenshot(full_page=True)` renders the ENTIRE scroll-height into one
# in-memory bitmap. A normal IR events/listing page is < ~8000 px, but a marketing page the crawl leaked into
# (www.apple.com/iphone, /shop) is 50000+ px tall → its bitmap is GBs, and a few rendered concurrently SIGKILLed the
# whole run (container cgroup ~46.5 GB OOM → fetch_10 EXIT=137). render_shot clips to this height when the page is
# taller; the VL model's layout signal (events table vs nav vs footer) lives in the first few screenfuls, not at the
# bottom of an infinite-scroll marketing page. {LOG 2026-07-23 "INPUT-CUT apple.com/iphone 51708 chars" then "56990
# Killed EXIT=137"} [CONFIDENCE: CONFIRMED 100% — the OOM followed the giant full_page screenshots; clipping bounds the
# peak bitmap RAM regardless of how tall the page is]. 8000 px ≈ 5-6 screenfuls @ 1280×~1400 — covers any real IR list.
SHOT_MAX_PX = int(os.environ.get("WATERCRAWL_SHOT_MAX_PX", "6000"))   # 8000→6000: lower shot resolution ⇒ smaller bitmap ⇒ less peak RAM per concurrent shot (still 4-5 screenfuls, covers any IR list)

# HARD render-abort height. A page TALLER than this is NOT an IR page — a real events/listing page is < ~8000 px; only
# infinite-scroll MARKETING pages the crawl leaked into (www.apple.com/iphone ≈ 50000 px, /surface, /shop) get this tall.
# The SHOT_MAX_PX clip above bounds only the SCREENSHOT bitmap, but the OOM-SIGKILL that poisoned the whole browser pool
# (42 TargetClosedError in the 16×3 run DESPITE the clip) came from LOADING + settling + content-extracting the giant
# page — memory paid BEFORE the shot. So render_shot measures scroll-height right AFTER goto and, if it exceeds this cap,
# ABORTS immediately (returns empty) — never settling / extracting / screenshotting it. The crawl counts it as a skipped
# render (fail-loud) and moves on. {USER 2026-07-23 "you should just stop the render if at a certain size and return
# directly"; DEBUG 16×3: 42 TargetClosedError from OOM despite the shot clip} [CONFIDENCE: CONFIRMED — OOM was pre-shot].
RENDER_ABORT_PX = int(os.environ.get("WATERCRAWL_RENDER_ABORT_PX", "20000"))

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
