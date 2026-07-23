"""tests/pickpool_crawl.py — MY decoupled deep crawl to build the media-enrichment cherry-pick pool.

Kept SEPARATE from deepcrawl.py (which a parallel session is actively editing into an A/B harness) so the two never
collide: unique script name, unique output dir tests/pickpool/<NAME>/. Sequential, deep (max_pages=20) so Apple/MSFT/JNJ
— which bury their events 1-2 BFS hops down — are actually reached, not just NVDA's event-dense home. NO ground-truth
oracle: we read the real crawl output and judge event quality ourselves. {USER 2026-07-23 "先深爬 3-4 家再混选 ... dont
rely on ground truth read the output yourself"} [CONFIDENCE: CONFIRMED 100% — direct user directive].

Run ON RUNPOD with INLINE env (NOT `env $E` — that quoting ate QWEN_API_KEY → 401 on the parallel A/B run):
  cd /workspace/WaterEvents && PYTHONPATH=/workspace/WaterEvents QWEN_BASE_URLS=http://127.0.0.1:8000/v1 \
  QWEN_API_KEY=<key> QWEN_MAX_TOKENS=12000 EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=20 QWEN_RETRIES=3 \
  /root/venv/bin/python tests/pickpool_crawl.py
"""
from __future__ import annotations

import asyncio
import os
import time

from providers.qwen_llm import QwenClient
from agent.event_agent.crawl import crawl_company

# 4 diverse-platform companies — NVDA/JNJ on Q4 (q4cdn/q4inc), Apple + Microsoft on custom IR stacks. Cross-platform is
# what surfaces the non-Q4 under-extraction the Block/Sanity investor-day page showed. {USER "跨平台多样性"}.
COMPANIES = {
    "NVDA": "https://investor.nvidia.com",
    "AAPL": "https://investor.apple.com",
    "MSFT": "https://www.microsoft.com/en-us/investor",
    "JNJ":  "https://www.investor.jnj.com",
}

_OUT = os.path.join(os.path.dirname(__file__), "pickpool")   # tests/pickpool/<NAME>/ — mine only, never touched by deepcrawl.py


async def main() -> None:
    os.makedirs(_OUT, exist_ok=True)
    client = QwenClient()                                     # one shared client; its global semaphore bounds in-flight VLM calls
    results = []
    for name, url in COMPANIES.items():                      # sequential — one company's BFS batch at a time = sane server load
        print(f"\n{'#'*80}\n##### {name}  {url}\n{'#'*80}", flush=True)
        t0 = time.time()
        try:
            r = await crawl_company(url, client=client, trace_dir=os.path.join(_OUT, name))   # writes summary.json + traces
            results.append((name, len(r["events"]), r["pages"], r["status"], round(time.time() - t0, 1)))
            print(f"[pickpool] {name}: {len(r['events'])} events / {r['pages']} pages / status={r['status']} "
                  f"in {time.time()-t0:.0f}s", flush=True)
        except Exception as e:                                # a company crash must not sink the rest
            results.append((name, "CRASH", 0, str(e)[:60], round(time.time() - t0, 1)))
            print(f"[pickpool] {name} CRASHED: {type(e).__name__}: {e}", flush=True)

    print(f"\n{'='*80}\n===== PICKPOOL SUMMARY =====", flush=True)
    for name, nev, npg, st, dt in results:
        print(f"  {name:6} {str(nev):>5} events | {npg:>3} pages | {dt:6.1f}s | {st}", flush=True)
    print(f"\n每家 events → tests/pickpool/<NAME>/summary.json (供 cherry-pick)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
