"""watercrawl.engines.camoufox — FALLBACK 4: the ONLY $0 method that beats FULL Akamai/Incapsula sensor.js.

用一句话讲完: 用 Camoufox(一个把 anti-detect stealth 打在 C++ 源码层、不是 JS 注入 → 0% headless 检测的 Firefox fork)
经住宅轮换代理开页,让 Akamai/Incapsula 的 sensor.js 完整跑完 → `_abck` cookie 落地 → 墙打开。WHY 只有它能过 sensor 层:
curl_cffi(FB1)拿不到 _abck cookie 会 403,patchright(FB3)的 headless HTTP/2 流被 Akamai 故意打断 ERR_HTTP2 —— 只有
Camoufox 的不可检测 headless 能让 sensor.js 跑到底。每调用起一个完整 Firefox(慢 + 重),所以并发被 config.CAMOUFOX_CAP
限死,只有最硬的墙才会走到这里。{TESTED 2026-07-22 Cloud Run: RJF/raymondjames Akamai 200 (577KB), wanhai Incapsula 200
(53KB), secom 200 — 全是 FB1+FB3 拿不到的页} [CONFIDENCE: CONFIRMED — GCP-proven on the three hardest walls].
{RESEARCH camoufox = Playwright Firefox launcher wrapper,stealth 隔离在 wrapper,业务代码照常用 Playwright API}.
"""
from __future__ import annotations

import threading

from .. import config
from .. import politeness, runtime

# Camoufox launches a FULL Firefox per call; a burst of the hardest walled pages would OOM on N concurrent Firefoxes.
# Bound concurrent launches. [CONFIDENCE: CONFIRMED — OOM guard].
_CAMOUFOX_SEM = threading.Semaphore(config.CAMOUFOX_CAP)


async def _render_one(url: str, wait_ms: int) -> tuple[str, list, str]:
    """Render url with Camoufox through the residential ROTATING proxy → (text, links, html). Lazy camoufox import:
    absent camoufox → ImportError → the sync wrapper's except returns empty so the caller keeps its prior result.
    Runs ON the shared Playwright loop."""
    from camoufox.async_api import AsyncCamoufox           # lazy: absent camoufox → import fails → caller skips FB4
    from ... import webshare                               # providers/webshare — the residential rotating gateway
    pxd = webshare.playwright_proxy()
    if not pxd:                                            # no residential proxy configured → FB4 dormant
        # FAIL-LOUD (one line): a dormant FB4 is exactly what hid the dead camoufox for a whole 2668-run — surface it so a
        # walled site's 0-events is traceable to "no proxy" not a mystery. {AUDIT 2026-07-24 tier4 silently dormant + Firefox
        # launch-dead (libgtk-3 missing)} [CONFIDENCE: CONFIRMED — camoufox recovered 39/45 walls once proxy+libgtk fixed].
        print(f"[watercrawl] camoufox FB4 DORMANT for {url[:60]} — no WEBSHARE proxy configured (tier4 anti-Akamai off)", flush=True)
        return "", [], ""
    # geoip=True: match the Firefox timezone/locale/WebGL to the residential proxy's IP geo → consistent fingerprint (a
    # US-IP browser advertising a non-US locale is a bot tell Akamai flags). camoufox heavily recommends it with a proxy
    # (LeakWarning otherwise). Verified it launches on the pod (geoip db present). {AUDIT 2026-07-24 geoip warning; probe
    # "geoip=True OK content=559"} [CONFIDENCE: CONFIRMED — tested on the pod, tesla recovered with camoufox].
    async with AsyncCamoufox(headless=True, proxy=pxd, geoip=True) as browser:   # C++-stealth Firefox on the residential IP
        page = await browser.new_page()
        # This is camoufox's OWN Playwright page, not watercrawl.page, so the gate that now lives in page.goto does
        # not reach it. Async form because this coroutine runs on a loop. Raising is the established contract for a
        # refused navigation — the caller already treats a goto failure as "no content".
        # [CONFIDENCE: CONFIRMED 100% — `page` here is bound from `browser.new_page()` a few lines above, so the name
        #  collision with the watercrawl.page module is exactly why this site was missed.]
        _ok, _why = await politeness.url_allowed_async(url)
        if not _ok:
            raise PermissionError(f"navigation refused ({_why}): {url[:120]}")
        await politeness.wait_turn_async(url)
        await page.goto(url, timeout=config.NAV_TIMEOUT_MS + 20000, wait_until="domcontentloaded")
        await page.wait_for_timeout(max(wait_ms, 6000))   # let Akamai/Incapsula sensor.js run → _abck cookie lands
        html = await page.content()                        # the post-challenge REAL page
        try:
            links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")   # browser resolves → absolute
        except Exception:                                  # noqa: BLE001 — link eval hiccup → html/text still usable
            links = []
        try:
            text = await page.inner_text("body")           # clean visible text (feeds detection.looks_walled/render_thin)
        except Exception:                                  # noqa: BLE001
            text = ""
        return text, links, html


def render(url: str, wait_ms: int) -> tuple[str, list, str]:
    """SYNC wrapper for FB4 — run _render_one on the shared loop, bounded by _CAMOUFOX_SEM. ('', [], '') on ANY
    failure (camoufox absent / launch error / wall unbeaten) so a caller keeps its prior result. WHY sync: the
    orchestrator's render_full/render_detail are sync entry points into the one Playwright loop thread."""
    with _CAMOUFOX_SEM:                                    # cap concurrent Firefox launches (OOM guard)
        try:
            # Firefox launch is slow → generous timeout on top of nav + wait.
            return runtime.run_on_loop(_render_one(url, wait_ms),
                                       (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 70)
        except Exception as _ce:                           # noqa: BLE001 — FB4 is a best-effort last resort
            print(f"[watercrawl] camoufox FB4 failed for {url[:70]}: {_ce}", flush=True)
            return "", [], ""
