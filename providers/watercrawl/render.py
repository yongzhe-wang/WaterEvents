"""watercrawl.render — the core on-loop render coroutines + the two simple sync entries (render / render_shot).

用一句话讲完: 这里放"在浏览器 loop 上真正开页、等 JS/XHR settle、跑 EXTRACT_JS 抽 (text,links,inline)、可选截全页图"
的协程,以及两个同步入口 —— `render()`(纯 text/links)和 `render_shot()`(WaterEvents 的唯一入口:text+links+html+
截图 b64+inline,自带 render→residential→impersonate 三级 fallback)。WHY 独立成层: render 只管"把一页抓下来"这件事,
浏览器生命周期在 runtime、page 工厂在 page、抽取 JS 在 extract_js、wall 判定在 detection —— render 组合它们但不拥有它们。
更重的 render_full/render_detail 多引擎链在 orchestrator。{RESEARCH firecrawl engines/playwright 只管渲染,升级决策在
orchestrator} [CONFIDENCE: CONFIRMED — render 只渲染、升级决策在 orchestrator 是当前分层].
"""
from __future__ import annotations

import base64
import contextlib
import os
import re
import time

from . import config, detection, extract_js, html_inline, page, runtime, walls

# RENDER STEP INSTRUMENTATION — when WATERCRAWL_RENDER_DEBUG=1, print a timestamped line at EVERY step of a browser
# render (context → page → goto → size-gate → settle → break-walls → extract → screenshot) with the elapsed ms. WHY:
# renders "fail" (return empty) or hang for reasons that are SILENTLY swallowed today (multiple `except: pass`), so we
# never see WHICH step failed or how long each took. This makes the actual failure/hang location observable instead of
# guessed. {USER 2026-07-23 "stop guessing add more print and verify ... find why the browser render failed"}.
_RDEBUG = os.environ.get("WATERCRAWL_RENDER_DEBUG", "") in ("1", "true", "yes")


def _rlog(url: str, step: str, t0: float, extra: str = "") -> None:
    """Print one render-step line with elapsed-ms since t0 (monotonic), only when render-debug is on."""
    if _RDEBUG:
        print(f"[render] {int((time.monotonic()-t0)*1000):6d}ms {step:16} {url[:55]} {extra}", flush=True)

# Shared navigation primitives live in page.py (render + the load_more/year_bar drivers all use them). Alias to keep
# the render coroutines below reading `_goto` / `_settle` unchanged.
_goto = page.goto
_settle = page.settle

# HTTP-FIRST: try the cheap curl_cffi direct fetch BEFORE spinning up the browser (research #1 optimization — static
# pages skip the browser + screenshot entirely, served TEXT-ONLY). Env-toggleable. {RESEARCH wv2d0n0v3 "HTTP-first gate
# + text-only path ... vision-call 数落到 ~30-50%"} [CONFIDENCE: CONFIRMED — top-ranked cost lever].
_HTTP_FIRST = os.environ.get("WATERCRAWL_HTTP_FIRST", "1") not in ("0", "false", "no")
# ...EXCEPT events/calendar LISTING pages, forced to the browser+screenshot: the layout table IS the signal there and a
# text-only fetch of a JS/image-rendered calendar would miss events. {RESEARCH "/events|/calendar 白名单强制 vision"}.
_FORCE_VISION_RE = re.compile(r'/(events?|calendar|ir-calendar|webcasts?)(/|\?|#|$)', re.I)


def _force_vision(url: str) -> bool:
    """True for pages where the layout screenshot is highest-value (events/calendar listings) → skip HTTP-first, always
    render + screenshot. Text-signal is weak on these (image-rendered tables) but layout is strong."""
    return bool(_FORCE_VISION_RE.search(url or ""))


# Events pages hide their history behind a year-filter / load-more / pagination. Match the ones worth EXPANDING (broader
# than _FORCE_VISION_RE — includes /presentations, /events-and-presentations, /news-and-events). {INVESTIGATION 2026-07-24}.
_EVENTS_EXPAND_RE = re.compile(
    r'/(events?|events-and-presentations|events-calendar|ir-calendar|calendar|webcasts?|presentations?|'
    r'news-and-events|upcoming-events?|investor-events?)(/|\?|#|$|-|\.)', re.I)


def is_events_page(url: str) -> bool:
    """True when this url is an events/presentations hub — the pages whose FULL history sits behind an interactive control."""
    return bool(_EVENTS_EXPAND_RE.search(url or ""))


def expand_events_page(url: str) -> str:
    """Drive an EVENTS page's year-filter (year_bar) + load-more control to reveal the FULL historical events list — not
    just the default-visible upcoming+recent 3-5 — and return the merged inline `[anchor](url)` text for extraction. WHY:
    Q4/most IR events pages show only upcoming + a couple recent events by default; the past-events archive sits behind a
    year dropdown / "Load More" / pagination that a STATIC render never clicks → discovery captured only 3-5 events for big
    companies with years of history (51% of the low-event-count companies). Both drivers SELF-SKIP (return "") on a page
    without their control, so this is safe to call on any events page. Runs the drivers (each marshals to the browser loop
    via run_on_loop) — call it in a thread from the crawl, exactly like render_shot. {INVESTIGATION 2026-07-24 low-count:
    events page reached but only default-visible few} [CONFIDENCE: CONFIRMED — airbnb events page content had 3 events;
    the historical earnings calls are behind the year filter]. Returns deduped merged inline, or "" if nothing expanded."""
    from .drivers.year_bar import drive_year_bar          # lazy import — drivers pull runtime/page; avoid import cycles
    from .drivers.load_more import drive_load_more
    # BOUNDED params — the drivers' defaults (year_bar max_years=16 × 5s waits ≈ 320s) would eat the whole per-company
    # budget on ONE events page. Cap to ~6 recent years / ~8 load-more rounds with short waits so a full events-page
    # expansion stays ~60-80s. And STOP after the first driver that expands (year_bar walks all years already; running
    # load_more after would just re-render for nothing). {avoid blowing EVENT_COMPANY_BUDGET_S on one page}.
    blocks: list = []
    for drive, kw in ((drive_year_bar, {"max_years": 6, "wait_ms": 1500}),   # year filter first (Q4 past-events archive)
                      (drive_load_more, {"max_rounds": 8, "wait_ms": 1200})):  # else load-more / infinite-scroll list
        try:
            _t, _l, inline = drive(url, **kw)              # (text, links, inline); "" when this control is absent → skip
        except Exception:                                  # noqa: BLE001 — an expansion failure must never sink the render
            inline = ""
        if inline:
            blocks.append(inline)
            break                                          # first driver that expanded wins → no wasted second re-render
    if not blocks:
        return ""
    seen, out = set(), []                                  # dedupe by line (the two drivers overlap on recent events)
    for block in blocks:
        for ln in block.split("\n"):
            k = ln.strip()
            if k and k not in seen:
                seen.add(k)
                out.append(ln)
    return "\n".join(out)


async def _break_walls(pg, url: str, wait_ms: int) -> None:
    """After the initial settle, dismiss a cookie/consent banner + break a webcast registration/login gate IN PLACE,
    then re-settle so the post-break real content loads before we extract/screenshot. Best-effort — walls.break_walls
    never raises; if nothing was there it's two cheap JS evals and a no-op. {GAP audit 2026-07-23: consent + login-wall}."""
    try:
        if await walls.break_walls(pg, url):             # clicked through consent / a registration gate → content changed
            await _settle(pg, wait_ms)                    # let the newly-revealed content finish loading
    except Exception:                                     # noqa: BLE001 — wall-breaking is best-effort, never sink the render
        pass


async def _render_one(url: str, inject_js: str | None, wait_ms: int) -> tuple[str, list]:
    """Render ONE url in an isolated context → (text, links). Optional inject_js runs AFTER load (e.g. a year-<select>
    change dispatch), then we wait wait_ms for its AJAX before extracting. Runs ON the loop."""
    async with runtime._sem:
        ctx = await runtime.next_browser().new_context(user_agent=config.UA)   # round-robin the browser pool (spread tabs across processes)
        try:
            pg = await page.new_blocked_page(ctx)
            await _goto(pg, url)
            if inject_js:
                try:
                    await pg.evaluate("() => {" + inject_js + "}")
                except Exception:                        # noqa: BLE001 — an inject that throws must not empty the render
                    pass
                await pg.wait_for_timeout(max(wait_ms, 0))
            else:
                await _settle(pg, wait_ms)
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
            except Exception:                            # noqa: BLE001 — one more settle then retry the extract
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
            return out.get("text") or "", list(out.get("links") or [])
        finally:
            await ctx.close()


async def _render_full_one(url: str, wait_ms: int, browser=None) -> tuple[str, list, str]:
    """Like _render_one but ALSO returns the page's rendered HTML → (text, links, html). WHY: a discovery/read caller
    (orchestrator.render_full/render_detail) needs the HTML to find interactive controls, so when watercrawl is the
    sole render engine it hands back HTML too, not just text. `browser` overrides the default (HTTP/1.1 or residential
    lane). Runs ON the loop."""
    async with runtime._sem:
        ctx = await (browser or runtime.next_browser()).new_context(user_agent=config.UA)   # default path round-robins the pool; fallback lanes pass explicit browser=
        try:
            pg = await page.new_blocked_page(ctx)
            await _goto(pg, url)
            await _settle(pg, wait_ms)
            await _break_walls(pg, url, wait_ms)          # dismiss consent + break a registration/login gate before extract
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            except Exception:                            # noqa: BLE001 — settle+retry once
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            return out.get("text") or "", list(out.get("links") or []), html
        finally:
            await ctx.close()


async def _render_shot_one(url: str, wait_ms: int, browser=None) -> tuple[str, list, str, str, str]:
    """Render url AND capture a FULL-PAGE screenshot → (text, links, html, shot_b64, inline). WHY the shot: it feeds a
    Qwen-VL model so it reads the page's VISUAL layout (events table vs nav vs footer) that text alone loses. JPEG @
    q70 keeps image tokens down. Screenshot is best-effort (text/links still return on shot failure). Runs ON the loop."""
    # _shot_sem (OUTER) caps how many full-page SCREENSHOTS render at once — the RAM hog that OOM-SIGKILLs a browser under
    # 4-browser concurrency (→ TargetClosedError). Acquire it BEFORE the page slot so excess shots wait WITHOUT holding a
    # page. {USER 2026-07-23 "we should have a cap"} [CONFIDENCE: CONFIRMED 100% — 4×concurrent full-page shots OOM'd 50GB cgroup].
    t0 = time.monotonic()
    _rlog(url, "want-sems", t0, f"(shot_sem+page_sem; browser={'proxy' if browser else 'pool'})")
    # When NO_SHOT is set we won't take a screenshot, so DON'T hold _shot_sem (the shot-slot cap of 4) — otherwise text-only
    # renders would be needlessly throttled to 4-wide and the isolation test couldn't show the real uplift. nullcontext() is
    # an async-capable no-op on 3.11. {config.NO_SHOT} [CONFIDENCE: CONFIRMED — skipping the shot means the RAM/raster hog it
    # gates is gone, so the cap it exists for no longer applies]. When shooting (default), acquire _shot_sem as before.
    _shot_gate = contextlib.nullcontext() if config.NO_SHOT else runtime._shot_sem
    async with _shot_gate, runtime._sem:
        _rlog(url, "got-sems", t0)
        ctx = await (browser or runtime.next_browser()).new_context(user_agent=config.UA)   # default path round-robins the pool; fallback lanes pass explicit browser=
        _rlog(url, "new-context", t0)
        try:
            pg = await page.new_shot_page(ctx)           # shot path: keep CSS + images so the screenshot looks real
            _rlog(url, "new-page", t0)
            await _goto(pg, url)
            _rlog(url, "goto-done", t0)
            # HARD SIZE GATE — measure scroll-height RIGHT AFTER goto, BEFORE the expensive settle/break/extract/content/
            # shot. A page taller than RENDER_ABORT_PX is an infinite-scroll marketing page (apple.com/iphone ≈ 50000 px),
            # never an IR event page. Settling + content-extracting + full_page-screenshotting it allocates GBs and
            # OOM-SIGKILLs the browser → TargetClosedError poisons EVERY page on that browser (42 such fails in the 16×3
            # run even WITH the SHOT_MAX_PX clip — the OOM was pre-shot). So STOP here and return empty; the crawl counts a
            # skipped render (fail-loud) and never pays the memory. Measured pre-settle so below-fold images haven't
            # lazy-loaded yet → the abort itself is cheap. {USER 2026-07-23 "stop the render if at a certain size and
            # return directly"} [CONFIDENCE: CONFIRMED 100% — 16×3 OOM'd from exactly these pages despite the shot clip].
            _h = await pg.evaluate("() => Math.max(document.documentElement.scrollHeight||0,"
                                   " (document.body && document.body.scrollHeight) || 0)")
            _rlog(url, "size-gate", t0, f"h={_h}px")
            if _h and _h > config.RENDER_ABORT_PX:
                print(f"[watercrawl] ⏭️ render ABORT {url[:70]} — {_h}px > {config.RENDER_ABORT_PX}px cap "
                      f"(giant non-IR/marketing page) → skip + return empty (never render the OOM bitmap)", flush=True)
                return "", [], "", "", ""                  # empty → caller counts a skipped render; ctx closed in finally
            await _settle(pg, wait_ms)
            _rlog(url, "settle-done", t0)
            await _break_walls(pg, url, wait_ms)          # dismiss consent + break a registration/login gate before the shot
            _rlog(url, "break-walls", t0)
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
                _rlog(url, "extract-done", t0, f"text={len(out.get('text') or '')} links={len(out.get('links') or [])}")
            except Exception:                            # noqa: BLE001 — one more settle then retry the extract
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            if config.NO_SHOT:                               # text/DOM-only mode: skip the whole raster/screenshot path
                shot_b64 = ""                                # no image — caller gets text+links+html+inline only
            else:
              try:
                # BOUND the screenshot memory. full_page=True renders the ENTIRE scroll-height into ONE bitmap; on a
                # giant marketing page the crawl leaked into (www.apple.com/iphone ≈ 50000 px tall) that bitmap is GBs,
                # and a few rendered concurrently SIGKILLed the run (cgroup OOM → fetch_10 EXIT=137). Measure the page
                # height; if it exceeds SHOT_MAX_PX, CLIP to the top SHOT_MAX_PX px instead of shooting the whole page —
                # the VL layout signal (events table vs nav vs footer) is in the first screenfuls, not at the bottom of
                # an infinite-scroll page. A normal IR list (< ~8000 px) still gets a full shot. {LOG 2026-07-23
                # apple.com/iphone,shop → EXIT=137} [CONFIDENCE: CONFIRMED 100% — OOM followed giant full_page shots].
                dims = await pg.evaluate(
                    "() => ({w: Math.max(document.documentElement.clientWidth||0, window.innerWidth||0),"
                    " h: Math.max(document.documentElement.scrollHeight||0,"
                    "            (document.body && document.body.scrollHeight) || 0)})")
                if dims.get("h", 0) > config.SHOT_MAX_PX:    # giant page → clip to the top band (bounds peak bitmap RAM)
                    shot = await pg.screenshot(type="jpeg", quality=70,
                                               clip={"x": 0, "y": 0, "width": dims.get("w") or 1280,
                                                     "height": config.SHOT_MAX_PX})
                else:                                        # normal IR page → full-page shot as before
                    shot = await pg.screenshot(full_page=True, type="jpeg", quality=70)
                shot_b64 = base64.b64encode(shot).decode("ascii")
              except Exception:                          # noqa: BLE001 — shot failed (huge page / timeout) → text-only
                shot_b64 = ""
            # 5th field = inline-linked reading-order text (links embedded as [anchor](url)) — the VL model's primary context
            return out.get("text") or "", list(out.get("links") or []), html, shot_b64, out.get("inline") or ""
        finally:
            await ctx.close()


def _shot_via(url: str, wait_ms: int, browser) -> tuple[str, list, str, str, str]:
    """Run _render_shot_one on the loop → (text, links, html, shot_b64, inline); ("", [], "", "", "") on any failure."""
    _budget = (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40
    _t = time.monotonic()
    _rlog(url, "shot_via-START", _t, f"budget={_budget:.0f}s lane={'residential' if browser else 'render'}")
    try:
        r = runtime.run_on_loop(_render_shot_one(url, wait_ms, browser=browser), _budget)
        _rlog(url, "shot_via-OK", _t, f"text={len(r[0])} links={len(r[1])} shot={'y' if r[3] else 'n'}")
        return r
    except Exception as e:                               # noqa: BLE001 — a render failure must not sink the crawl loop
        # dim3: register a DEAD-host browser error (DNS/cert/SSL/aborted) so a LATER empty timeout on this same host can
        # short-circuit the fallback chain instead of empty-walking residential/impersonate/camoufox. Only DNS/cert-class
        # errors mark dead; a plain nav TIMEOUT does NOT (it may be transient / IP-specific → still deserves fallback).
        # {AUDIT 2026-07-24 walled_falsesignal; DETECTION.PY:56 _DEAD_HOST_ERRORS; _shot_via previously NEVER called
        # mark_dead so dead_host was always False on this path} [CONFIDENCE: CONFIRMED — mark_dead was written only by the orchestrator path].
        if detection.is_dead_error(str(e)):
            detection.mark_dead(url)
        # ALWAYS print the failure reason (not gated) — this is exactly the "why did the render fail" signal we were blind
        # to. Includes the elapsed time so a TIMEOUT (hit the ~66s budget) is distinguishable from a fast error.
        print(f"[watercrawl] render_shot ({'residential' if browser else 'render'}) FAILED for {url[:70]} after "
              f"{time.monotonic()-_t:.1f}s: {type(e).__name__}: {str(e)[:160]}", flush=True)
        return "", [], "", "", ""


def render_shot(url: str, wait_ms: int = config.SETTLE_FIXED_MS) -> dict:   # fixed post-poll settle (1500, was 3000) — DOM poll already confirmed content
    """SYNC entry — open a page with the fallback chain and return EVERYTHING the crawl needs + full traceability:
        {"text", "links", "html", "shot_b64", "method", "inline"}
    method ∈ "render" (headless Chromium), "residential" (patchright + webshare, for bot-walls), "impersonate"
    (curl_cffi TLS bypass — NO browser so NO screenshot), "camoufox" (stealth Firefox FB4 — beats Akamai/Incapsula
    sensor.js, no screenshot), "walled" (a bot-challenge body that beat every tier → EMPTY, caller must count a render
    failure), or "" (all failed / no content). text+links feed the prompt; shot_b64 (JPEG base64) feeds a Qwen-VL model.
    Best-effort: every field empty on total failure, never raises."""
    from .engines import impersonate                      # lazy: the curl_cffi fingerprint-bypass engine (optional dep)
    empty = {"text": "", "links": [], "html": "", "shot_b64": "", "method": "", "inline": ""}

    if not runtime.ensure_browser():                     # no browser env → text-only impersonate is all we have
        try:
            it, il, ih = impersonate.fetch(url)
            if it or il:                                 # build inline from html so the model still SEES the urls (else 0 events)
                return {"text": it, "links": list(il), "html": ih or "", "shot_b64": "",
                        "method": "impersonate", "inline": html_inline.to_inline(ih, url)}
        except Exception:                                # noqa: BLE001
            pass
        return dict(empty)

    # ── HTTP-FIRST: try the cheap curl_cffi direct fetch BEFORE the browser. Accept it ONLY when it returned a REAL
    # content page (not walled AND not a thin JS shell) — i.e. a prose-rich static page (press release / article). A
    # link-dense LISTING is render_thin=True → falls through to the browser (those pages most want the screenshot, and
    # events/calendar are force-vision anyway). So content/detail pages go text-only (save a browser spin), discovery
    # listings still get the shot. The result is REUSED in the wall-escalation below — never fetched twice.
    i_result = None
    if _HTTP_FIRST and not _force_vision(url):
        try:
            it, il, ih = impersonate.fetch(url)
            i_result = (it, il, ih)
            if not detection.looks_walled(it, il) and not detection.render_thin(it):
                return {"text": it, "links": list(il), "html": ih or "", "shot_b64": "",
                        "method": "impersonate", "inline": html_inline.to_inline(ih, url)}
        except Exception:                                # noqa: BLE001
            pass

    # 1) headless render + full-page screenshot (dynamic pages / events pages / thin-static HTTP-first didn't serve)
    text, links, html, shot, inline = _shot_via(url, wait_ms, browser=None)
    _walled = detection.looks_walled(text, links)
    if _RDEBUG:
        print(f"[render] tier1-result {url[:55]} text={len(text)} links={len(links)} shot={'y' if shot else 'n'} "
              f"walled={_walled} dead={walls.deadpage.looks_dead(text) if text else '?'}", flush=True)
    if not _walled:
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    if walls.deadpage.looks_dead(text):                  # soft-404 / gone page → don't burn fallbacks resurrecting it
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    # dim3: pure-empty render AND host proven DEAD (DNS/cert/aborted, registered by _shot_via's except above) → skip the
    # residential/impersonate/camoufox fallback: none can revive a host that doesn't resolve / fails TLS, so the whole
    # chain would just empty-walk ~90-150s. Triple guard (text=="" AND links==[] AND dead_host) protects a headless-
    # walled-but-recoverable page (edrsilver/pepsico): those have a LIVE host → dead_host False → they still fall through
    # to camoufox. {AUDIT 2026-07-24 walled_falsesignal; looks_walled('',[]) returns True at DETECTION.PY:27 BEFORE any
    # marker check, so an empty timeout would otherwise walk the whole chain} [CONFIDENCE: CONFIRMED — dead_host gates it].
    if not text and not links and detection.dead_host(url):
        return dict(empty)

    # 2) walled → patchright residential render + screenshot (real browser from a residential IP, beats IP-reputation walls)
    if runtime._browser_proxy is not None:
        r_text, r_links, r_html, r_shot, r_inline = _shot_via(url, wait_ms, browser=runtime._browser_proxy)
        if not detection.looks_walled(r_text, r_links):
            return {"text": r_text, "links": list(r_links), "html": r_html, "shot_b64": r_shot, "method": "residential", "inline": r_inline}

    # 3) still walled → curl_cffi impersonate (different TLS/HTTP2 fingerprint than the browser). REUSE the HTTP-first
    # fetch if we already did it above; else fetch now. inline rebuilt from html so its events keep their urls.
    if i_result is None:
        try:
            i_result = impersonate.fetch(url)
        except Exception:                                # noqa: BLE001
            i_result = None
    if i_result:
        i_text, i_links, i_html = i_result
        if len(i_links) > len(links) or (i_text and not text):
            return {"text": i_text, "links": list(i_links), "html": i_html or "", "shot_b64": "",
                    "method": "impersonate", "inline": html_inline.to_inline(i_html, url)}

    # 4) STILL walled → camoufox (stealth Firefox — beats Akamai/Incapsula sensor.js). Content-only: no screenshot.
    # The tier the refactor dropped from render_shot (kept only in render_full/render_detail). inline rebuilt from html.
    # {DEBUG 2026-07-23 pepsico Incapsula} [CONFIDENCE: CONFIRMED 100%].
    from .engines import camoufox                          # lazy: stealth Firefox, optional heavy dep
    try:
        c_text, c_links, c_html = camoufox.render(url, wait_ms)
        if not detection.looks_walled(c_text, c_links):
            return {"text": c_text, "links": list(c_links), "html": c_html or "", "shot_b64": "",
                    "method": "camoufox", "inline": html_inline.to_inline(c_html, url)}
    except Exception:                                    # noqa: BLE001 — camoufox may be uninstalled; fall through to fail-loud
        pass

    # nothing beat the wall. FAIL-LOUD: a bot-challenge / block-page BODY (Incapsula/Cloudflare) must NEVER be returned
    # as a successful render — the caller would feed the block page to the model → 0 events with failed_render=0 (a
    # degraded run masquerading as clean). Return method="walled" + EMPTY so the caller drops it + counts a render
    # failure. {DEBUG 2026-07-23 pepsico: block body returned as method="render"} [CONFIDENCE: CONFIRMED 100%].
    if detection.is_challenge(text):
        return {"text": "", "links": [], "html": "", "shot_b64": "", "method": "walled", "inline": ""}
    # merely link-sparse (no challenge body) → a possibly-legit small page → return the thin render we have.
    if text or links:
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    return dict(empty)


def render(url: str, inject_js: str | None = None, wait_ms: int = 0) -> tuple[str, list]:
    """SYNC entry (safe from any crawl thread): render url via the resident browser → (text, links). Optional inject_js
    drives a control before extraction. ("", []) on any failure → caller falls back. Blocks the calling thread on the
    loop's future (the loop still serves other pages concurrently)."""
    if not runtime.ensure_browser():
        return "", []
    try:
        return runtime.run_on_loop(_render_one(url, inject_js, wait_ms),
                                   (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 30)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] render failed for {url[:80]}: {error}", flush=True)
        return "", []
