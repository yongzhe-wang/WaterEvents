"""watercrawl.runtime — the ONE owner of shared browser state + the dedicated asyncio loop thread.

用一句话讲完: 整个 watercrawl 只有一个常驻浏览器进程 + 一条专属 asyncio loop 线程 + 一个限并发 semaphore,这些
可变全局状态**只此一份、只住在这个模块里** → render/drivers/orchestrator 全部 `from . import runtime` 读 `runtime._browser`
/`runtime._loop`,谁都不再各持一份 → 拆成多文件也不会状态分叉。任何跨线程调用协程都走 `runtime.run_on_loop(coro, timeout)`。
WHY 一个模块独占状态: async_playwright 对象绑定创建它的 loop,crawl 的 ThreadPoolExecutor worker 线程没有 loop,所以
我们自己拥有一条 loop 线程、用 run_coroutine_threadsafe marshal 过去。把这套 lifecycle + 状态收进 runtime.py 是拆分
god-module 的地基 —— 只有"状态单一持有者"才能让 render.py / drivers/*.py 安全地引用同一个 browser。
{RESEARCH crawl4ai `browser_manager.py` 把浏览器生命周期独立成模块} [CONFIDENCE: CONFIRMED — Playwright loop-affinity 约束].

Design invariants:
  - ONE browser per worker process, launched lazily on first use, kept warm for the process lifetime.
  - ALL Playwright calls run on ONE dedicated event-loop thread (loop-affinity).
  - A Semaphore bounds CONCURRENT pages so peak memory stays inside the worker's limit.
  - Best-effort: launch failure flips _dead so callers fall back to impersonate/jina — render is NEVER a hard dep.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time

from . import config

# ── shared mutable state (THE single copy; other modules read these via `runtime.<name>`) ──────────────────────
_lock = threading.Lock()                                  # guards the lazy launch (one launch across N caller threads)
_loop: asyncio.AbstractEventLoop | None = None            # the dedicated Playwright loop
_loop_thread: threading.Thread | None = None
_browser = None                                           # default headless Chromium (_browsers[0]; fallback code reads this)
_browsers: list = []                                      # POOL of default Chromiums (config.RENDER_BROWSERS of them) — render pages round-robin over these separate PROCESSES so no ONE browser starves at high tab count
_rr_browser = None                                        # itertools.cycle over _browsers (created on the loop in _launch)
_browser_h1 = None                                        # HTTP/1.1-forced Chromium (ERR_HTTP2 retry lane)
_browser_proxy = None                                     # patchright + webshare residential STEALTH browser (walls)
_playwright = None                                        # the async_playwright driver for the two Chromiums
_playwright_stealth = None                                # the patchright driver for the residential browser
_sem: asyncio.Semaphore | None = None                     # bounds concurrent pages (created on the loop in _launch)
_shot_sem: asyncio.Semaphore | None = None                # bounds concurrent FULL-PAGE SCREENSHOTS (the RAM hog) — created in _launch

# ── launch-failure COOLDOWN (replaces the old one-way `_dead = True` latch) ────────────────────────────────────────
# WHY this is no longer a latch: `_dead` used to be set once and never cleared, so a SINGLE transient launch failure
# (a momentary fork/ENOMEM while another worker was mid-render, a slow /dev/shm, a Chromium binary page-fault storm)
# disabled the render lane for the ENTIRE process lifetime. The worker then stayed alive, kept claiming work, and
# silently served every url from the impersonate lane only — no browser, no screenshot, no wall-breaking, and no error
# anyone could see. That is the same "process alive, producing nothing" shape the wedge post-mortem describes.
# {5B91B6A "THE HOST NEVER WENT DOWN: GCE SHOWED RUNNING, CPU SAT FLAT AT ~17%, AND ALL SIX WORKER PROCESSES WERE STILL
#  PRESENT IN PS"} [CONFIDENCE: INFERRED 70% — the wedge's own root cause was attributed to disk/page-fault thrash, NOT
#  to this latch; what is CONFIRMED is that the latch makes a transient failure permanent, which is a real defect on
#  its own. Do not read this as "the latch caused the wedge".]
# The replacement is a cooldown with linear backoff + a crash counter: retry the launch after a growing wait, and give
# up permanently only after _LAUNCH_MAX_FAILS consecutive failures (a genuinely broken environment — no Chromium
# installed, no shared memory — where retrying forever would only burn 90s per call).
_LAUNCH_MAX_FAILS = 5                                     # consecutive launch failures before the lane is declared permanently dead
_LAUNCH_COOLDOWN_S = 60.0                                 # base cooldown; multiplied by the failure count (60,120,180,240s)
_launch_fails = 0                                         # CONSECUTIVE launch failures; reset to 0 by a success
_launch_next_try = 0.0                                    # monotonic deadline before which ensure_browser refuses to retry
_dead = False                                             # True only after _LAUNCH_MAX_FAILS consecutive failures (terminal)

# ── SLOT OBSERVABILITY (fix for "exhaustion is invisible until it reaches zero") ───────────────────────────────────
# WHY: `_sem` is the render lane's hard concurrency limit, and a leaked permit is never returned. Before this, the ONLY
# externally visible symptom of permit exhaustion was total silence — every render blocking forever on a semaphore that
# will never be released, with the process healthy in `ps` and near-zero CPU. There was no counter, no log line, and no
# way to answer "how many render slots does this worker still have?" without attaching a debugger. These accessors make
# the free-slot count readable from the worker so the DEGRADATION is observable while slots still remain, instead of
# only the final zero being observable. {5B91B6A "WATCHDOG.SH CHECKS LIVENESS BY ASKING THE DATABASE WHEN SCAN_LOG LAST
#  GREW, NOT BY CHECKING WHETHER PROCESSES EXIST. DURING THE INCIDENT EVERY PROCESS EXISTED; ONLY THE OUTPUT HAD STOPPED"}
# [CONFIDENCE: CONFIRMED 100% — the commit's own watchdog rationale is that process-existence is not a liveness signal;
#  a free-slot count is the render-lane analogue of that same argument.]
_slot_warn_at = 0.0                                       # monotonic rate-limit stamp so the low-slot warning can't spam the log
_SLOT_WARN_EVERY_S = 60.0                                 # at most one low-slot warning per minute per worker


def _ensure_loop() -> None:
    """Start the dedicated event-loop thread ONCE (idempotent). WHY a dedicated thread: async_playwright must live
    on a single loop; the crawl's worker threads have no loop, so we own one here and marshal to it."""
    global _loop, _loop_thread
    if _loop is not None:
        return
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="browser-pool-loop", daemon=True)
    t.start()
    _loop, _loop_thread = loop, t


async def _launch() -> None:
    """Launch the resident browsers + create the on-loop Semaphore. Runs ON the loop thread. Raises on failure
    (caught by ensure_browser, which flips _dead so we never retry a broken environment every call).

    THREE browsers, layered by cost: (1) default headless Chromium — the fast path; (2) an HTTP/1.1-forced Chromium
    for the ERR_HTTP2 retry lane (Akamai deliberately breaks headless HTTP/2); (3) a patchright + webshare residential
    STEALTH browser for bot-walls (only if a webshare proxy is configured)."""
    global _browser, _browsers, _rr_browser, _playwright, _sem, _shot_sem, _browser_h1, _browser_proxy, _playwright_stealth
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    # Container-safe flags (--disable-dev-shm-usage) + cache/GPU trims so peak render memory stays low.
    _shared_args = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer",
                    "--disable-extensions", "--disk-cache-size=1", "--media-cache-size=1"]
    # POOL of default Chromiums — config.RENDER_BROWSERS SEPARATE browser processes (min 1). render pages round-robin
    # across them (next_browser) so N total tabs split into N/K tabs per browser → no single browser's main-thread/IPC
    # starves. _browser stays = _browsers[0] so the fallback lanes (h1/residential) that read runtime._browser still work.
    # {USER 2026-07-23 "you have multiple cpu right"} [CONFIDENCE: CONFIRMED — 1-browser@48 starved; K procs spread the load].
    _browsers = [await _playwright.chromium.launch(headless=True, args=_shared_args)
                 for _ in range(max(1, config.RENDER_BROWSERS))]
    _rr_browser = itertools.cycle(_browsers)              # round-robin picker (consumed on the single loop thread → no lock needed)
    _browser = _browsers[0]                               # fallback-lane compatibility: h1/residential code references _browser
    # HTTP/1.1 lane — a second Chromium with HTTP/2 disabled, used only when the default browser ERR_HTTP2s.
    _browser_h1 = await _playwright.chromium.launch(headless=True, args=_shared_args + ["--disable-http2"])
    # Residential STEALTH lane — patchright (source-patched Playwright) through the webshare rotating proxy. Only
    # armed when a proxy is configured; a launch failure leaves it dormant (FALLBACK 3 skipped, never fatal).
    from .. import webshare                                # providers/webshare — the residential rotating gateway
    _px = webshare.playwright_proxy()
    if _px:
        try:
            from patchright.async_api import async_playwright as _async_patchright
            if _playwright_stealth is None:
                _playwright_stealth = await _async_patchright().start()
            _browser_proxy = await _playwright_stealth.chromium.launch(headless=True, args=_shared_args, proxy=_px)
            print(f"[watercrawl] webshare residential STEALTH browser UP (patchright, proxy {_px['server']})", flush=True)
        except Exception as _pxerr:                       # noqa: BLE001 — residential lane is best-effort, never fatal
            print(f"[watercrawl] webshare stealth browser launch failed ({_pxerr}) — FALLBACK 3 dormant", flush=True)
            _browser_proxy = None
    else:
        # FAIL-LOUD at launch (not per-page) when NO residential proxy is configured → BOTH tier2 (residential) AND tier4
        # (camoufox, which requires the proxy) are DORMANT. This is the silent gap that let the whole 2668-run's 43 bot-
        # walled big-caps (tesla/homedepot/nestle Akamai) fail unrecovered without a trace. Surface it once, at browser
        # launch, so "walled site 0-events" is traceable to a missing WEBSHARE_PROXY, not a mystery. {AUDIT 2026-07-24
        # residential_dormant; rewall recovered 39/45 once WEBSHARE_PROXY set + libgtk installed} [CONFIDENCE: CONFIRMED].
        print("[watercrawl] ⚠ NO webshare proxy configured — tier2 residential + tier4 camoufox DORMANT; "
              "Akamai/Incapsula-walled hosts will NOT be recovered (set WEBSHARE_PROXY or WEBSHARE_USERNAME/PASSWORD/PROXIES)", flush=True)
    _sem = asyncio.Semaphore(config.MAX_PAGES)            # bound concurrent pages (created on THIS loop)
    _shot_sem = asyncio.Semaphore(config.SHOT_CONCURRENCY)   # bound concurrent FULL-PAGE SHOTS (RAM hog) so 4 browsers don't OOM the cgroup


def _live_browsers() -> list:
    """The subset of `_browsers` whose Chromium PROCESS is still alive, per Playwright's `browser.is_connected()`.

    WHY this exists: every liveness check in this module used to be `_browser is not None`, which tests a PYTHON OBJECT
    REFERENCE, not the process behind it. When Chromium is OOM-SIGKILLed the Python `Browser` object stays perfectly
    non-None — nothing clears it — so `ensure_browser()` kept returning True and every subsequent `new_context()` raised
    `TargetClosedError`. The blast radius is recorded in this repo's own code: an OOM-killed browser poisons EVERY page
    on that browser. {RENDER.PY:279-281 "OOM-SIGKILLS THE BROWSER → TARGETCLOSEDERROR POISONS EVERY PAGE ON THAT BROWSER
    (42 SUCH FAILS IN THE 16×3 RUN EVEN WITH THE SHOT_MAX_PX CLIP — THE OOM WAS PRE-SHOT)"}
    [CONFIDENCE: CONFIRMED 100% — the 42-failure count is a measured number already written into render.py's size-gate
     comment; `is_connected()` is Playwright's documented process-liveness predicate, distinct from object identity.]

    Upstream trigger: `next_browser()` on every render/driver context open, and `ensure_browser()`'s fast path.
    Downstream: a dead browser is excluded from the rotation, so renders route only to live processes instead of
    round-robining 50% of traffic into a corpse.
    """
    live = []
    for b in _browsers:
        try:
            if b.is_connected():                          # Playwright process-liveness (NOT `b is not None`)
                live.append(b)
        except Exception:                                 # noqa: BLE001 — a handle whose transport is gone counts as dead
            pass
    return live


async def _relaunch_dead(dead_count: int) -> None:
    """Replace `dead_count` crashed Chromiums in `_browsers` and rebuild the round-robin cycle. Runs ON the loop.

    WHY relaunch instead of just dropping: `_browsers` is the render lane's whole capacity. Dropping a dead browser
    without replacing it permanently shrinks the pool — after K crashes the pool is empty and the lane is silently gone,
    which is the same invisible-degradation failure this whole change set is about. Relaunching restores capacity so a
    single OOM costs one browser and a few seconds, not the worker's render ability.

    Upstream trigger: `next_browser()` when `_live_browsers()` comes back short. Downstream: `_rr_browser` is rebuilt
    over the LIVE pool, so the very next `next()` hands out a working browser.
    """
    global _browsers, _rr_browser, _browser
    _shared_args = ["--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer",
                    "--disable-extensions", "--disk-cache-size=1", "--media-cache-size=1"]
    live = _live_browsers()                               # keep the survivors; only the corpses are replaced
    for _ in range(max(0, dead_count)):
        try:
            live.append(await _playwright.chromium.launch(headless=True, args=_shared_args))
        except Exception as err:                          # noqa: BLE001 — a failed replacement must not kill the survivors
            print(f"[watercrawl] browser RELAUNCH failed ({type(err).__name__}: {str(err)[:120]}) — "
                  f"pool now {len(live)} live", flush=True)
            break
    if live:                                              # only publish a non-empty pool; an empty one would break next()
        _browsers = live
        _rr_browser = itertools.cycle(_browsers)          # rebuild the cycle — the old one still yields dead handles
        _browser = _browsers[0]                           # keep the fallback-lane alias pointing at a LIVE browser


def ensure_browser() -> bool:
    """Lazily start loop + launch browsers, blocking the CALLER until ready. Returns True if the browser is usable,
    False if launch failed (→ caller falls back to impersonate/jina). Thread-safe via _lock.

    COOLDOWN, NOT A LATCH (changed): the fast path now asks whether a browser PROCESS is alive (`_live_browsers()`),
    not whether a Python reference is non-None, and a launch failure schedules a retry instead of disabling the lane
    forever. `_dead` is only set after `_LAUNCH_MAX_FAILS` consecutive failures, which is the "no Chromium installed"
    case where retrying is pure waste. Between failures the lane refuses fast (no 90s wait per call) until the backoff
    deadline passes. See the `_LAUNCH_MAX_FAILS` block above for the WHY and its confidence note.

    Upstream: every sync entry (render / render_shot / drive_*) calls this before marshalling work onto the loop.
    Downstream: True → the caller opens a context; False → the caller falls back to impersonate/camoufox.
    """
    global _dead, _launch_fails, _launch_next_try
    if _dead:                                             # terminal only after repeated failures — a genuinely broken env
        return False
    if _browsers and _live_browsers():                    # fast path: at least one LIVE process (not merely a non-None ref)
        return True
    with _lock:
        if _dead:
            return False
        if _browsers and _live_browsers():                # another thread launched while we waited on the lock
            return True
        if time.monotonic() < _launch_next_try:           # still inside the backoff window → refuse fast, don't burn 90s
            return False
        try:
            _ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(_launch(), _loop)
            fut.result(timeout=90)
            _launch_fails = 0                             # success clears the consecutive-failure run
            print(f"[watercrawl] resident Chromium launched ({len(_browsers)} browser(s) × max_pages={config.MAX_PAGES} "
                  f"total) — self-hosted render lane UP", flush=True)
            return True
        except Exception as error:                        # noqa: BLE001 — a broken env must disable render, not crash
            _launch_fails += 1
            # Linear backoff so a transient failure retries soon and a persistent one backs off: 60,120,180,240s.
            _launch_next_try = time.monotonic() + _LAUNCH_COOLDOWN_S * _launch_fails
            if _launch_fails >= _LAUNCH_MAX_FAILS:        # repeatedly broken → stop paying the launch cost entirely
                _dead = True
                print(f"[watercrawl] launch failed {_launch_fails}× consecutively — self-hosted render PERMANENTLY "
                      f"disabled this process: {error}", flush=True)
            else:
                print(f"[watercrawl] launch failed ({_launch_fails}/{_LAUNCH_MAX_FAILS}), retrying in "
                      f"{_LAUNCH_COOLDOWN_S * _launch_fails:.0f}s: {error}", flush=True)
            return False


def next_browser():
    """Round-robin the NEXT default browser from the pool (render.py's default render path calls this instead of reading
    the single runtime._browser). Spreads consecutive render pages across the K separate browser processes so no one
    browser accumulates all the tabs. Called ONLY from render coroutines on the single loop thread, so the itertools
    cycle needs no lock. Falls back to _browser if the pool isn't built yet (defensive; ensure_browser runs first).

    EVERY on-loop context opener must come through here — the five interaction drivers (year_bar / year_select / years /
    load_more / clicks) used to read `runtime._browser` directly, which is hard-wired to `_browsers[0]`. That put ALL
    expansion work on a single browser process while only the render path round-robined, and expansion is the heaviest
    session there is (year_bar re-navigates once per year, up to 7 navigations for one page). Piling those on one browser
    is the exact configuration config.py already records as collapsing — and what collapses first is the events pages,
    i.e. precisely the pages with the most events to win.
    {RUNTIME.PY:73 "_BROWSER = _BROWSERS[0]"} {CONFIG.PY:22 "PILING ALL TABS ON ONE BROWSER STARVES AT HIGH N ... 48 TABS
     ON 1 BROWSER TIMED OUT THE EVENTS PAGES THEMSELVES (RENDER_SHOT 22S NAV TIMEOUT) AND EVENT YIELD COLLAPSED (NVDA 94→8)"}
    [CONFIDENCE: CONFIRMED 100% — all five drivers verified on the same single-browser handle before this change; the
     collapse mode is documented from a measured NVDA regression, not predicted].

    LIVENESS FILTER (added): the rotation used to be an UNCONDITIONAL `next(_rr_browser)` over `_browsers`, with no
    check that the browser it handed back still had a process behind it. With the default pool of 3, one OOM-killed
    Chromium meant a THIRD of all renders were routed into a dead handle and raised `TargetClosedError` — permanently,
    because nothing ever removed it from the cycle and nothing ever relaunched it. The worker looks healthy the whole
    time; only the event yield drops. {CONFIG.PY:28 "RENDER_BROWSERS = INT(OS.ENVIRON.GET(\"IR_WATERCRAWL_BROWSERS\",
    \"3\"))"} {RENDER.PY:279-281 "OOM-SIGKILLS THE BROWSER → TARGETCLOSEDERROR POISONS EVERY PAGE ON THAT BROWSER (42
    SUCH FAILS IN THE 16×3 RUN ...)"} [CONFIDENCE: CONFIRMED 100% — the round-robin had no liveness predicate of any
    kind (the whole previous body was the single `next(...)` line), and TargetClosedError-on-dead-browser is measured
    in this repo's own comments].
    """
    if _rr_browser is None:                               # pool not built yet (ensure_browser runs first) — defensive
        return _browser
    # Fast path: try up to len(_browsers) rotations for a browser that is actually connected. This keeps the common
    # all-alive case at ONE `is_connected()` call and preserves the round-robin spread.
    for _ in range(max(1, len(_browsers))):
        b = next(_rr_browser)
        try:
            if b.is_connected():                          # a LIVE process → hand it out, rotation unchanged
                return b
        except Exception:                                 # noqa: BLE001 — broken transport counts as dead; keep rotating
            pass
    # Every browser in the cycle is dead → replace the corpses IN PLACE and hand back a fresh one. Scheduled on this
    # same loop thread via a nested coroutine is not possible from a sync function, so callers reach this only from ON
    # the loop; `_relaunch_dead` is awaited by `ensure_live_browsers()` instead. Here we degrade to `_browser` so the
    # caller gets a definite TargetClosedError it can attribute, rather than a silent hang.
    return _browser


async def ensure_live_browsers() -> bool:
    """ON-LOOP liveness repair: count the dead Chromiums in the pool, relaunch that many, return whether any live
    browser exists afterwards. Awaited from the render coroutines BEFORE they open a context.

    WHY it is a coroutine and `next_browser()` is not: relaunching requires `await playwright.chromium.launch(...)`,
    which can only happen on the loop thread. `next_browser()` is called from inside already-running coroutines, so the
    repair is hoisted to an explicit await at the top of the render path instead of being hidden in the picker.

    Upstream trigger: `render._render_shot_one` / `_render_one` / `_render_full_one`, right after acquiring the
    semaphore. Downstream: the round-robin cycle contains only live browsers, so `next_browser()` cannot hand out a
    corpse and the `TargetClosedError` storm cannot start.
    """
    live = _live_browsers()
    if len(live) == len(_browsers) and live:              # all present and connected → nothing to repair (common case)
        return True
    dead = len(_browsers) - len(live)                     # how many processes we lost since the last check
    if dead > 0:
        print(f"[watercrawl] ⚠ {dead}/{len(_browsers)} Chromium process(es) DEAD (is_connected=False) — relaunching; "
              f"a dead browser raises TargetClosedError on EVERY context opened on it", flush=True)
        await _relaunch_dead(dead)                        # replace the corpses + rebuild the round-robin cycle
    return bool(_live_browsers())


def free_slots() -> tuple[int, int]:
    """(free_render_slots, free_shot_slots) — the CURRENT unused capacity of `_sem` and `_shot_sem`.

    WHY this accessor exists: permit exhaustion had NO observable signal before it was total. A worker holding zero
    free render slots is indistinguishable, from the outside, from a worker that is merely busy — both show a live
    process at low CPU producing nothing. Exposing the count lets the caller log the DEGRADATION (slots trending down)
    rather than only the terminal state. See the `_slot_warn_at` block above for the full rationale + evidence.

    Upstream: the worker's periodic status log, or any caller that wants to record capacity alongside throughput.
    Downstream: none — this is a pure read of `asyncio.Semaphore._value` and mutates nothing.

    NOTE on `_value`: asyncio.Semaphore exposes no public "available permits" property, so this reads the private
    attribute defensively (returns -1 when unavailable) rather than maintaining a parallel counter that could itself
    drift out of sync with the real semaphore. [CONFIDENCE: CONFIRMED 100% — `_value` is the permit count CPython's
    asyncio.Semaphore decrements in acquire() and increments in release(); reading it is observation-only and cannot
    perturb the semaphore.]
    """
    def _v(sem) -> int:
        try:
            return int(getattr(sem, "_value"))            # remaining permits; -1 when the semaphore isn't built yet
        except Exception:                                 # noqa: BLE001 — observability must never raise into a caller
            return -1
    return _v(_sem), _v(_shot_sem)


def log_slots_if_low(threshold: int = 2) -> None:
    """Print a WARNING when free render slots fall to/below `threshold`, rate-limited to one line per minute.

    WHY a warning BEFORE zero: at zero, every render blocks forever on the semaphore and the worker goes silent — by
    then the log line is useless because nothing is running to emit it. Warning while slots remain is what makes the
    slide observable in time to act on. Rate-limited because the render path calls this on every page and an unbounded
    warning would itself become the noise that hides the signal.

    Upstream: called from `render._shot_via` on each render attempt. Downstream: stdout only — never raises, never
    blocks, never changes routing.
    """
    global _slot_warn_at
    free, shot_free = free_slots()
    if free < 0 or free > threshold:                      # unbuilt (-1) or healthy → nothing to say
        return
    now = time.monotonic()
    if now - _slot_warn_at < _SLOT_WARN_EVERY_S:          # rate-limit: one warning per minute per worker
        return
    _slot_warn_at = now
    print(f"[watercrawl] ⚠ RENDER SLOTS LOW: {free}/{config.MAX_PAGES} page permits free, {shot_free} shot permits "
          f"free — if this reaches 0 and stays there, renders will block forever (leaked permits never return)",
          flush=True)


def browser_available() -> bool:
    """Cheap check for callers that want to decide routing BEFORE building a job. Triggers the lazy launch."""
    return ensure_browser()


def run_on_loop(coro, timeout: float):
    """Marshal an already-created coroutine onto the dedicated Playwright loop and BLOCK the calling thread on its
    result. THE single crossing point from a crawl worker thread into the browser loop — every sync entry (render/
    render_shot/drive_*) funnels its on-loop coroutine through here. Raises whatever the coroutine raised (callers
    wrap in try/except → empty result on failure).

    CANCEL ON THE WAY OUT — this is the leak fix. `fut.result(timeout=)` only stops the CALLER waiting; the coroutine
    keeps running on the loop, still holding the browser context it opened and still occupying its `_sem` slot. Its
    `finally: await ctx.close()` cannot run until it finishes naturally — and a timeout is precisely the case where it
    is stuck in _goto/_settle and will not. Every timed-out render therefore leaked one context plus one semaphore
    permit, and engine.py retries the same url up to _RENDER_TRIES=3 times with backoff while the earlier attempts are
    still alive, so one slow host could hold three contexts at once. Leaked permits are never returned, so once enough
    accumulate the semaphore is exhausted and EVERY subsequent render blocks on it forever — the worker goes silent
    rather than erroring, which is the failure mode hardest to notice.
    Cancelling propagates CancelledError into the coroutine, which runs its `finally` immediately: context closed,
    permit returned. {INCIDENT 2026-07-27 "155 CHROMIUM PROCESSES / 9.49GB CHROME-HEADLESS RSS AFTER 12 MINUTES OF
    UPTIME" — steady-state concurrency cannot reach that in 12 minutes, a leak term is required to explain it}
    {ENGINE.PY "_RENDER_TRIES = INT(OS.ENVIRON.GET("EVENT_RENDER_TRIES", "3"))" — retries overlap the leaked attempts}
    [CONFIDENCE: CONFIRMED 100% — asyncio.Future.result(timeout) is documented to leave the coroutine running; the
     browser-pool cap shipped in 5b91b6a only lowers the BASELINE and, by shrinking the semaphore from 24 to 8 permits,
     actually makes exhaustion arrive with FEWER leaked slots. This is the fix that addresses the cause.]

    WAKE THE LOOP AFTER CANCELLING (added): `fut.cancel()` alone is not sufficient. `concurrent.futures.Future.cancel()`
    on a future returned by `run_coroutine_threadsafe` schedules the underlying task's cancellation via the loop — but
    the cancellation is only DELIVERED when the loop next runs a cycle and the task next reaches an await point. This
    was verified experimentally: a repro printed `CALLER SAW TimeoutError at t=1s` and then, later, `COROUTINE RAN TO
    COMPLETION AFTER TIMEOUT -> NOT CANCELLED`. Posting an explicit no-op through `call_soon_threadsafe` forces the loop
    to wake and process the cancellation immediately instead of whenever it happens to next tick.
    {REPRO 2026-07-28 "CALLER SAW TimeoutError at t=1s" THEN "COROUTINE RAN TO COMPLETION AFTER TIMEOUT -> NOT CANCELLED"}
    [CONFIDENCE: CONFIRMED 100% — the repro output is the direct observation; `Future.result(timeout)` is documented to
     time out the WAIT, not the work.]

    BELT-AND-BRACES (`on_loop_bounded`): cancellation can only take effect at the coroutine's next await point, so a
    coroutine blocked inside a single long Playwright call still holds its permit until that call returns. The inner
    `asyncio.wait_for` wrapper (see `on_loop_bounded` below) gives the coroutine its OWN deadline, so it self-terminates
    on schedule even if the outer cancel is never delivered. Both layers are needed: the outer cancel handles the
    caller-side timeout, the inner wait_for handles the coroutine-side one.
    """
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return fut.result(timeout=timeout)
    except BaseException:                                 # timeout, KeyboardInterrupt, or the coroutine's own error
        fut.cancel()                                      # request cancellation → CancelledError at the coro's next await
        try:
            # WAKE the loop so the cancellation is delivered NOW. Without this the loop may sit idle in its selector
            # and the zombie keeps its _sem/_shot_sem permit and its open context until it finishes on its own.
            _loop.call_soon_threadsafe(lambda: None)
        except Exception:                                 # noqa: BLE001 — loop already closed → nothing left to wake
            pass
        raise


async def on_loop_bounded(coro, timeout: float):
    """Wrap an on-loop coroutine in its OWN `asyncio.wait_for` deadline — the inner half of the two-layer timeout.

    WHY both layers: `run_on_loop`'s `fut.cancel()` is a REQUEST that only lands at the coroutine's next await point.
    A coroutine parked inside one long Playwright call (a `goto` against a black-holing host, a `pg.evaluate` inheriting
    Playwright's 30s default) does not reach an await point until that call returns, so between the caller's timeout and
    the coroutine's eventual return it is a ZOMBIE holding a `_sem` permit. `wait_for` makes the coroutine responsible
    for its own deadline, so the permit comes back on schedule regardless of whether the outer cancel was delivered.
    {REPRO 2026-07-28 "COROUTINE RAN TO COMPLETION AFTER TIMEOUT -> NOT CANCELLED"}
    [CONFIDENCE: CONFIRMED 100% — the repro is exactly the "cancel not delivered" case this layer covers.]

    The inner budget is deliberately SHORTER than the caller's (`timeout` here is passed a fraction of the outer
    budget by the call sites) so the coroutine trips its own deadline FIRST and unwinds cleanly through its
    `finally: await ctx.close()`, rather than being cancelled mid-flight from outside.

    Upstream: the sync entries in render.py / the drivers wrap their coroutine in this before handing it to
    `run_on_loop`. Downstream: on timeout the coroutine raises `TimeoutError` inside itself → its `finally` closes the
    context and releases the permit → the caller sees a normal render failure instead of a silent leak.
    """
    return await asyncio.wait_for(coro, timeout=timeout)
