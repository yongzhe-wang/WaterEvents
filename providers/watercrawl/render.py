"""watercrawl.render — the core on-loop render coroutines + the two simple sync entries (render / render_shot).

用一句话讲完: 这里放"在浏览器 loop 上真正开页、等 JS/XHR settle、跑 EXTRACT_JS 抽 (text,links,inline)、可选截全页图"
的协程,以及两个同步入口 —— `render()`(纯 text/links)和 `render_shot()`(WaterEvents 的唯一入口:text+links+html+
截图 b64+inline,自带 render→residential→impersonate 三级 fallback)。WHY 独立成层: render 只管"把一页抓下来"这件事,
浏览器生命周期在 runtime、page 工厂在 page、抽取 JS 在 extract_js、wall 判定在 detection —— render 组合它们但不拥有它们。
更重的 render_full/render_detail 多引擎链在 orchestrator。{RESEARCH firecrawl engines/playwright 只管渲染,升级决策在
orchestrator} [CONFIDENCE: CONFIRMED — verbatim 迁移自 pool.py 渲染协程 + render_shot/render].
"""
from __future__ import annotations

import base64
import os
import re

from . import config, detection, extract_js, html_inline, page, runtime, walls

# Shared navigation primitives live in page.py (render + the load_more/year_bar drivers all use them). Alias to keep
# the render coroutines below reading `_goto` / `_settle` unchanged. {POOL.PY:279-315 moved to page.goto/page.settle}.
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
    change dispatch), then we wait wait_ms for its AJAX before extracting. Runs ON the loop. {POOL.PY:253-276}."""
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
    lane). Runs ON the loop. {POOL.PY:318-338}."""
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
    q70 keeps image tokens down. Screenshot is best-effort (text/links still return on shot failure). Runs ON the loop.
    {POOL.PY:341-368}."""
    async with runtime._sem:
        ctx = await (browser or runtime.next_browser()).new_context(user_agent=config.UA)   # default path round-robins the pool; fallback lanes pass explicit browser=
        try:
            pg = await page.new_shot_page(ctx)           # shot path: keep CSS + images so the screenshot looks real
            await _goto(pg, url)
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
            if _h and _h > config.RENDER_ABORT_PX:
                print(f"[watercrawl] ⏭️ render ABORT {url[:70]} — {_h}px > {config.RENDER_ABORT_PX}px cap "
                      f"(giant non-IR/marketing page) → skip + return empty (never render the OOM bitmap)", flush=True)
                return "", [], "", "", ""                  # empty → caller counts a skipped render; ctx closed in finally
            await _settle(pg, wait_ms)
            await _break_walls(pg, url, wait_ms)          # dismiss consent + break a registration/login gate before the shot
            try:
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
            except Exception:                            # noqa: BLE001 — one more settle then retry the extract
                await _settle(pg, wait_ms)
                out = await pg.evaluate(extract_js.EXTRACT_JS)
                html = await pg.content()
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
            except Exception:                            # noqa: BLE001 — shot failed (huge page / timeout) → text-only
                shot_b64 = ""
            # 5th field = inline-linked reading-order text (links embedded as [anchor](url)) — the VL model's primary context
            return out.get("text") or "", list(out.get("links") or []), html, shot_b64, out.get("inline") or ""
        finally:
            await ctx.close()


def _shot_via(url: str, wait_ms: int, browser) -> tuple[str, list, str, str, str]:
    """Run _render_shot_one on the loop → (text, links, html, shot_b64, inline); ("", [], "", "", "") on any failure."""
    try:
        return runtime.run_on_loop(_render_shot_one(url, wait_ms, browser=browser),
                                   (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40)
    except Exception as e:                               # noqa: BLE001 — a render failure must not sink the crawl loop
        print(f"[watercrawl] render_shot ({'residential' if browser else 'render'}) failed for {url[:70]}: "
              f"{type(e).__name__}: {e}", flush=True)
        return "", [], "", "", ""


def render_shot(url: str, wait_ms: int = config.SETTLE_FIXED_MS) -> dict:   # fixed post-poll settle (1500, was 3000) — DOM poll already confirmed content
    """SYNC entry — open a page with the fallback chain and return EVERYTHING the crawl needs + full traceability:
        {"text", "links", "html", "shot_b64", "method", "inline"}
    method ∈ "render" (headless Chromium), "residential" (patchright + webshare, for bot-walls), "impersonate"
    (curl_cffi TLS bypass — NO browser so NO screenshot), "camoufox" (stealth Firefox FB4 — beats Akamai/Incapsula
    sensor.js, no screenshot), "walled" (a bot-challenge body that beat every tier → EMPTY, caller must count a render
    failure), or "" (all failed / no content). text+links feed the prompt; shot_b64 (JPEG base64) feeds a Qwen-VL model.
    Best-effort: every field empty on total failure, never raises. {POOL.PY:382-423 + camoufox FB4 restored 2026-07-23}."""
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
    if not detection.looks_walled(text, links):
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}
    if walls.deadpage.looks_dead(text):                  # soft-404 / gone page → don't burn fallbacks resurrecting it
        return {"text": text, "links": list(links), "html": html, "shot_b64": shot, "method": "render", "inline": inline}

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
    # {DEBUG 2026-07-23 pepsico Incapsula; OLD pool.py:736 "FALLBACK 4 — CAMOUFOX"} [CONFIDENCE: CONFIRMED 100%].
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
    loop's future (the loop still serves other pages concurrently). {POOL.PY:718-729}."""
    if not runtime.ensure_browser():
        return "", []
    try:
        return runtime.run_on_loop(_render_one(url, inject_js, wait_ms),
                                   (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 30)
    except Exception as error:                           # noqa: BLE001
        print(f"[watercrawl] render failed for {url[:80]}: {error}", flush=True)
        return "", []
