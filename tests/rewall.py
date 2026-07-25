"""tests/rewall — re-run ONLY the sites that failed_render in the full 2668 run, now WITH the residential proxy armed
(WEBSHARE_PROXY) + camoufox alive (libgtk-3 installed) → measure how many of the bot-walled sites the tier2/tier4
fallbacks now recover. FAKE_EXTRACT (no VLM). {USER 2026-07-24 "run the 45 failed with proxy+camoufox, measure recovery"}.

Run ON THE POD (WEBSHARE_PROXY must be set so runtime arms the residential + camoufox lanes):
  WEBSHARE_PROXY=... EVENT_FAKE_EXTRACT=1 IR_WATERCRAWL_BROWSERS=3 PYTHONPATH=/workspace/WaterEvents \
    /root/venv/bin/python tests/rewall.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from agent.event_agent.crawl import crawl_company

RESULTS = os.path.join(os.path.dirname(__file__), "hstress_out", "results.txt")   # source: the full-run per-company json lines
CONC = int(os.environ.get("REWALL_CONC", "6"))                                     # modest — camoufox serializes at CAMOUFOX_CAP anyway


def _failed_urls() -> list[str]:
    """Pull every url that FAILED render in the full run (failed_render>0 OR pages falsy OR hard error)."""
    urls = []
    for ln in open(RESULTS, encoding="utf-8"):
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            r = json.loads(ln)
        except Exception:                                   # noqa: BLE001
            continue
        if (r.get("failed_render") or 0) or (not r.get("pages")) or r.get("error"):
            urls.append(r["url"])
    return urls


async def _one(url: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        t0 = time.time()
        try:
            out = await crawl_company(url, max_pages=1, batch=1)
            fr = out.get("failed_render") or 0
            return {"url": url, "sec": round(time.time() - t0, 1), "pages": out.get("pages"),
                    "failed_render": fr, "recovered": (not fr) and bool(out.get("pages"))}
        except Exception as e:                               # noqa: BLE001
            return {"url": url, "sec": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {str(e)[:120]}", "recovered": False}


async def main() -> None:
    urls = _failed_urls()
    print(f"[rewall] {len(urls)} previously-failed sites | conc={CONC} | proxy={'ON' if os.environ.get('WEBSHARE_PROXY') else 'OFF'}", flush=True)
    sem = asyncio.Semaphore(CONC)
    t0 = time.time()
    results = await asyncio.gather(*(_one(u, sem) for u in urls))
    dt = time.time() - t0
    rec = [r for r in results if r.get("recovered")]
    still = [r for r in results if not r.get("recovered")]
    print(f"\n=== REWALL RESULT ===")
    print(f"previously failed : {len(urls)}")
    print(f"RECOVERED now     : {len(rec)}  ({100*len(rec)/max(1,len(urls)):.0f}%)")
    print(f"still failed      : {len(still)}")
    print(f"wall time         : {dt:.0f}s")
    print(f"\n--- RECOVERED ---")
    for r in sorted(rec, key=lambda r: -r["sec"]):
        print(f"  ✓ {r['sec']:>6}s  {r['url'][:70]}")
    print(f"\n--- STILL FAILED ---")
    for r in sorted(still, key=lambda r: -(r.get('sec') or 0)):
        print(f"  ✗ {r.get('sec'):>6}s  {r['url'][:70]}  {r.get('error','')}")
    with open(os.path.join(os.path.dirname(__file__), "rewall_out.txt"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
