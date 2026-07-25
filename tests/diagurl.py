"""tests/diagurl — per-URL failure diagnosis. For each url (argv or stdin), run EVERY watercrawl fetch method + a raw
HTTP probe and emit ONE JSON line with each method's outcome, so we can pinpoint WHY render_shot failed (dead DNS /
connection refused / HTTP 4xx-5xx / load timeout / bot-wall vendor / thin JS-shell / transient). NO crawl loop, one url
at a time. {USER 2026-07-24 "test all the methods from watercrawl on these url and find the reason for the failure"}.

Run ON THE POD (needs playwright + curl_cffi):
  IR_WATERCRAWL_BROWSERS=1 PYTHONPATH=/workspace/WaterEvents /root/venv/bin/python tests/diagurl.py <url1> <url2> ...
"""
from __future__ import annotations

import json
import sys
import time


_MARKERS = ("cloudflare", "incapsula", "imperva", "akamai", "datadome", "perimeterx",
            "captcha", "are you a robot", "attention required", "access denied", "request unsuccessful")


def _probe_http(url: str) -> dict:
    """Raw stdlib GET — the cheapest signal: DNS resolves? connection? HTTP status? body size? bot-challenge markers?
    Uses urllib (no curl_cffi dep) with a desktop-Chrome UA so a plain UA sniff passes; reports the exact failure mode."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(60000).decode("utf-8", "ignore")
            low = body.lower()
            vendor = next((v for v in _MARKERS if v in low), "")
            return {"status": r.status, "bytes": len(body), "sec": round(time.time() - t, 1),
                    "final_url": r.geturl()[:120], "challenge_marker": vendor}
    except urllib.error.HTTPError as e:                       # got an HTTP status (403/404/5xx = a real server response)
        body = ""
        try:
            body = e.read(60000).decode("utf-8", "ignore")
        except Exception:                                    # noqa: BLE001
            pass
        vendor = next((v for v in _MARKERS if v in body.lower()), "")
        return {"status": e.code, "bytes": len(body), "sec": round(time.time() - t, 1), "challenge_marker": vendor}
    except Exception as e:                                    # noqa: BLE001 — DNS fail / conn refused / timeout / TLS
        return {"error": f"{type(e).__name__}: {str(e)[:140]}", "sec": round(time.time() - t, 1)}


def _try(label: str, fn) -> dict:
    t = time.time()
    try:
        return {"ok": True, **fn(), "sec": round(time.time() - t, 1)}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}", "sec": round(time.time() - t, 1)}


def diag(url: str) -> dict:
    # importlib.import_module returns the actual MODULE from sys.modules. WHY not `import providers.watercrawl.render`:
    # __init__.py does `from .render import render`, shadowing the `render` package-attribute with the render() FUNCTION,
    # so BOTH `from ... import render` AND `import ....render as rmod` bind the function, not the module. import_module
    # bypasses the shadow. {DEBUG 2026-07-24 diagurl: rmod._shot_via → AttributeError 'function' object}.
    import importlib
    rmod = importlib.import_module("providers.watercrawl.render")
    det = importlib.import_module("providers.watercrawl.detection")
    from providers.watercrawl.engines import impersonate

    out: dict = {"url": url}
    out["http_probe"] = _probe_http(url)

    # 1) impersonate (watercrawl's curl_cffi/urllib engine — the no-browser HTTP lane)
    def _imp():
        it, il, ih = impersonate.fetch(url)
        return {"text": len(it or ""), "links": len(il or []), "walled": det.looks_walled(it, il)}
    out["impersonate"] = _try("impersonate", _imp)

    # 2) headless-render tier (the primary browser lane) — rmod._shot_via with the default pool browser
    def _render():
        t, l, h, shot, inline = rmod._shot_via(url, rmod.config.SETTLE_FIXED_MS, browser=None)
        return {"text": len(t), "links": len(l), "shot": bool(shot), "walled": det.looks_walled(t, l)}
    out["headless_render"] = _try("headless_render", _render)

    # 3) camoufox stealth tier (beats Akamai/Incapsula sensor.js) — optional heavy dep
    def _camo():
        from providers.watercrawl.engines import camoufox
        t, l, h = camoufox.render(url, rmod.config.SETTLE_FIXED_MS)
        return {"text": len(t or ""), "links": len(l or []), "walled": det.looks_walled(t, l)}
    out["camoufox"] = _try("camoufox", _camo)

    # 4) what render_shot (the full fallback chain) ultimately returns
    def _full():
        r = rmod.render_shot(url)
        return {"method": r.get("method"), "text": len(r.get("text") or ""), "links": len(r.get("links") or [])}
    out["render_shot_full"] = _try("render_shot_full", _full)
    return out


def main() -> None:
    urls = sys.argv[1:] or [ln.strip() for ln in sys.stdin if ln.strip()]
    for u in urls:
        print(json.dumps(diag(u)), flush=True)


if __name__ == "__main__":
    main()
