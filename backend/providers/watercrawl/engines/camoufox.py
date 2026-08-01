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


# Set the first time camoufox fails to LAUNCH (missing system libs, no binary, OOM at start). Process-wide because
# the fault is a property of the HOST, not of a url — see the warning in render() for what it cost to not have this.
_LAUNCH_BROKEN = False


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
            # A LAUNCH failure is not a per-url outcome, it is a broken host, and it must say so ONCE and loudly.
            # This tier was dead on the production VM for an unknown length of time: camoufox's Firefox could not start
            # because libgtk-3.so.0 was not installed, so every attempt printed one line among thousands and returned
            # empty, and the render then reported method="walled" — indistinguishable from a wall that genuinely beat
            # all four tiers. Nothing counted it, nothing aggregated it, it never reached scan_log or fleet_health.
            # Meanwhile arganinc.com/category/news/ — 36 events already in the database — is a host ONLY camoufox can
            # read: from a working install it returns 6,303 chars with 62 date tokens, while tier 1 and the residential
            # tier both get 460 chars of block page.
            # The `⚠ NO webshare proxy` startup warning exists for exactly this failure mode one tier over. This is its
            # missing twin.
            # {MEASURED 2026-08-01 ir-media-8 "camoufox FB4 failed ... libgtk-3.so.0: cannot open shared object file:
            #  No such file or directory / Couldn't load XPCOM. / <process did exit: exitCode=255>"}
            # {MEASURED 2026-08-01 same url, pod vs prod: method=camoufox text=6303 links=292 dates=62 -> method=walled
            #  text=0 links=0 dates=0}
            # [CONFIDENCE: CONFIRMED 100% — the launch error was read from the browser log on the production host.]
            msg = str(_ce)
            if "Failed to launch" in msg or "cannot open shared object" in msg or "Couldn't load XPCOM" in msg:
                global _LAUNCH_BROKEN
                if not _LAUNCH_BROKEN:                     # once per process, not once per url
                    _LAUNCH_BROKEN = True
                    print("[watercrawl] ⚠ camoufox CANNOT LAUNCH on this host — tier 4 is DEAD, not merely unlucky. "
                          "Bot-walled hosts that only Firefox/FB4 can read will return 0 events forever and look "
                          f"'walled'. Fix the environment, then restart. First error: {msg[:300]}", flush=True)
            else:
                print(f"[watercrawl] camoufox FB4 failed for {url[:70]}: {_ce}", flush=True)
            return "", [], ""

# No launch_broken() accessor here on purpose. I wrote one, and check_unused.py failed the build for it: nothing
# called it. That check exists because of five fixes in one day that were believed shipped with zero call sites, and
# it was right — the one-time warning above IS the fix, and a getter waiting for a hypothetical future consumer is the
# exact shape the ratchet is built to reject. Add it back when something actually reads it.
# {CI 2026-08-01 "::error::defined but never used — camoufox.py:108 launch_broken"}
# [CONFIDENCE: CONFIRMED 100% — the build failed on it.]
