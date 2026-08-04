"""watercrawl.capture — open a page in the shared browser and HARVEST THE MEDIA URLS IT REQUESTS off the network log.

用一句话讲完: 企业 webcast 平台(media-server / q4inc / webcasts.com / choruscall / summitcast …)不发布可猜的媒体
地址,而且 yt-dlp 对它们一律 `Unsupported URL` —— 但它们最终都得让浏览器去拉流。所以不逆向 URL 规则,而是**用已有的
watercrawl 浏览器打开页面、监听 network,把它自己请求的 .m3u8/.mp4/.mp3 抓下来**:一份代码,平台无关。

WHY this instead of per-platform adapters or yt-dlp — the measured reason:
  • yt-dlp probe over 12 distinct platforms in tests/datasets/media_100: YouTube ✅, a page with an embedded media file
    ✅ (generic extractor), and event.choruscall / viavid.webcasts / event.webcasts / webcast-eqs / irwebcasting /
    events.q4inc ALL returned `Unsupported URL`. {PROBE 2026-08-03 `yt_dlp --simulate` over the dataset's platforms}
  • Production share of those unsupported corporate platforms among all video/webcast urls: 23,690 / 28,385 = 83.5%,
    vs YouTube's 1,492 = 5.3%. Wiring yt-dlp alone would recover ~5% of the problem.
  • 17,952 events carry a corporate webcast and NOTHING else (no pdf/office/mp3) — 73.9% of the 24,285 webcast events.
    Skipping them is not an option: they would be permanently empty records.
  [CONFIDENCE: CONFIRMED 100% — both shares measured against the production DB on 2026-08-03.]

组织:
  capture_media(url, wait_ms) -> {"media": [url…], "method", "n_requests", "error"}

依赖: 复用 runtime 的共享 Chromium + 它的 politeness gate / capacity ceiling / `_sem` 页面槽,自己不起浏览器 ——
否则会绕过 render lane 的限流,而那正是 2026-07-27 把这台机器拖垮的路径
{RENDER.PY:337-344 "CAPACITY CEILING, before the semaphores"} [CONFIDENCE: CONFIRMED — the incident is in that comment].
"""
from __future__ import annotations

import re

from . import capacity, config, politeness, runtime

# A media request worth keeping. Two independent signals, because a webcast CDN usually serves its manifest from an
# extension-less path: (a) the url names a media/manifest file, (b) playwright labels the request resource_type
# 'media'. Either alone suffices — requiring both would drop the extension-less HLS manifests that are the norm here.
_MEDIA_URL_RE = re.compile(
    r'\.(m3u8|mpd|mp4|m4a|m4s|mp3|wav|aac|webm|mov|ts)(\?|#|$)'
    r'|/(playlist|manifest|master|chunklist|index)\.(m3u8|mpd)(\?|#|$)',
    re.I)
# Ad / analytics / tracking beacons that are technically 'media' but carry no content — dropped so the caller never
# hands a tracking pixel or a preroll ad to whisper.
_JUNK_RE = re.compile(
    r'(doubleclick|googlesyndication|google-analytics|googletagmanager|scorecardresearch|'
    r'omtrdc\.net|demdex|adservice|/ads?/|/beacon|/pixel|/analytics)', re.I)

# Play-button candidates. Most webcast players fetch the manifest only AFTER a user gesture, so a purely passive
# capture sees nothing on those. One best-effort click on the first visible match is far cheaper than a per-platform
# adapter, and a miss costs only the settle. {PROBE 2026-08-03 event.webcasts.com/starthere.jsp is a launch page}
_PLAY_SELECTORS = (
    "button[aria-label*='play' i]", "button[title*='play' i]", "[class*='play-button' i]",
    "[class*='playButton' i]", ".vjs-big-play-button", "video",
    "button:has-text('Play')", "a:has-text('Launch')", "button:has-text('Launch')",
    "button:has-text('Enter')", "button:has-text('Continue')",
)

_CAPTURE_TIMEOUT_S = float(getattr(config, "CAPTURE_TIMEOUT_S", 0) or 120)


def _empty(err: str) -> dict:
    return {"media": [], "method": "capture", "n_requests": 0, "error": err}


async def _capture_one(url: str, wait_ms: int) -> dict:
    """Open url, record every media-looking request, best-effort click a play control, return the deduped media urls.

    Runs ON the shared playwright loop. Gate order MIRRORS _render_shot_one exactly — robots, then capacity, THEN the
    page semaphore — so a url we were never going to open never holds a permit. No `_shot_sem`: we take no screenshot,
    so the RAM cap it guards does not apply {RENDER.PY:351 "When NO_SHOT is set we won't take a screenshot, so DON'T
    hold _shot_sem"}. The context is ALWAYS closed, including on a navigation error — a leaked one holds a slot forever."""
    ok, why = await politeness.url_allowed_async(url)          # robots decision BEFORE any permit
    if not ok:
        return _empty(f"refused:{why}")
    cap_ok, cap_why = await capacity.wait_for_capacity_async()  # host memory ceiling, also before the permit
    if not cap_ok:
        return _empty(f"capacity:{cap_why}")

    seen: list[str] = []
    n_req = 0

    async with runtime._sem:                                   # the render lane's page slot — shared, not bypassed
        browser = runtime.next_browser()                       # MUST be called on the loop thread (round-robins the pool)
        if browser is None:
            return _empty("no-browser")
        ctx = await browser.new_context(user_agent=config.UA,
                                        ignore_https_errors=True)   # webcast CDNs often have sloppy cert chains
        try:
            page = await ctx.new_page()

            def _on_request(req) -> None:                      # sync callback — must not await
                nonlocal n_req
                n_req += 1
                u = req.url
                if _JUNK_RE.search(u):                         # ad/analytics beacon → never a real asset
                    return
                if _MEDIA_URL_RE.search(u) or req.resource_type == "media":
                    if u not in seen:
                        seen.append(u)

            page.on("request", _on_request)

            await politeness.wait_turn_async(url)              # per-host pacing, same as the render lane
            try:
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            except Exception as e:                             # noqa: BLE001 — a dead/blocked page is a normal outcome
                return {"media": list(seen), "method": "capture", "n_requests": n_req,
                        "error": f"goto:{type(e).__name__}"}

            await page.wait_for_timeout(wait_ms)               # let the player boot and issue its first requests

            if not seen:                                       # nothing yet → the player is probably gated on a click
                for sel in _PLAY_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if await el.count() and await el.is_visible():
                            await el.click(timeout=3000)
                            await page.wait_for_timeout(wait_ms)
                            if seen:
                                break
                    except Exception:                          # noqa: BLE001 — a selector that misses is expected
                        continue

            return {"media": list(seen), "method": "capture", "n_requests": n_req, "error": ""}
        finally:
            try:
                await ctx.close()
            except Exception:                                  # noqa: BLE001 — close failure must not mask the result
                pass


def capture_media(url: str, wait_ms: int = 6000) -> dict:
    """A webcast/player page url → {media: [url…], method, n_requests, error}. Best-effort, NEVER raises.

    Sync entry mirroring render_shot's shape: it marshals onto runtime's dedicated playwright loop rather than owning a
    browser, so captures share the render lane's concurrency limit instead of silently doubling the browser count."""
    if not (url or "").lower().startswith(("http://", "https://")):
        return _empty("not-http")
    if not runtime.ensure_browser():
        return _empty("browser-unavailable")
    try:
        return runtime.run_on_loop(_capture_one(url, wait_ms), timeout=_CAPTURE_TIMEOUT_S)
    except Exception as e:                                     # noqa: BLE001 — timeout/loop error → loud, never a raise
        print(f"[watercrawl] capture FAILED for {url[:70]}: {type(e).__name__}: {str(e)[:90]}", flush=True)
        return _empty(type(e).__name__)
