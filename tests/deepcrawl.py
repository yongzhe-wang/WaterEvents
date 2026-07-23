"""tests/deepcrawl.py — DEEP discovery crawl over a few diverse-platform companies → per-company summary.json + traces,
so we can hand-pick high-quality representative events for the media-enrichment dataset (NO ground-truth oracle; we read
the real crawl output and judge quality ourselves).

ALSO an A/B harness for the "companies-concurrent vs sequential" question: CONCURRENT=1 fires ALL companies at once
(their BFS batches interleave on the server's continuous batch); default runs them one at a time. Both modes print
per-company + total wall-clock so we can measure the speed-up AND confirm the concurrent run's traces/events stay
complete. {USER 2026-07-23 "若改成公司间也并发 ... 10 家能压到 ~5-10min ... 当前串行更稳 ... we want to test this"}.

Run ON RUNPOD with the browser+VLM venv:
  # sequential baseline
  cd /workspace/WaterEvents && QWEN_BASE_URLS=http://127.0.0.1:8000/v1 QWEN_API_KEY=<key> QWEN_MAX_TOKENS=12000 \
  EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=15 QWEN_RETRIES=3 /root/venv/bin/python tests/deepcrawl.py
  # companies-concurrent
  ... CONCURRENT=1 EVENT_MAX_PAGES=15 /root/venv/bin/python tests/deepcrawl.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

# Self-contained path bootstrap — put the repo root (parent of tests/) on sys.path so `providers`/`agent` import no
# matter the cwd / PYTHONPATH / shell. A `nohup bash -c` non-login shell drops the profile's PYTHONPATH, so relying on
# it fails (ModuleNotFoundError: No module named 'providers'); inserting the root here is bulletproof. {DEBUG 2026-07-23}.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.qwen_llm import QwenClient
from agent.event_agent.crawl import crawl_company

# 4 companies chosen for PLATFORM diversity — NVDA/JNJ ride Q4 (q4cdn/q4inc), Apple + Microsoft run custom IR stacks. A
# cross-platform set is what exposes the non-Q4 under-extraction the Block/Sanity investor-day page showed. {USER "跨平台"}.
COMPANIES = {
    "NVDA": "https://investor.nvidia.com",
    "AAPL": "https://investor.apple.com",
    "MSFT": "https://www.microsoft.com/en-us/investor",
    "JNJ":  "https://www.investor.jnj.com",
}

_CONCURRENT = os.environ.get("CONCURRENT", "0") not in ("0", "false", "no")
_MODE = "concurrent" if _CONCURRENT else "sequential"
_OUT = os.path.join(os.path.dirname(__file__), f"deepcrawl_{_MODE}")   # mode-suffixed so seq/conc don't clobber each other


async def _one(name: str, url: str, client: QwenClient) -> tuple:
    """Crawl ONE company, timed. Shares a SINGLE QwenClient across all companies so its global concurrency semaphore is
    the real throttle — in concurrent mode that's what lets N companies' pages coexist on one bounded server queue."""
    t0 = time.time()                                          # per-company wall-clock start
    try:
        r = await crawl_company(url, client=client, trace_dir=os.path.join(_OUT, name))   # summary.json + traces here
        dt = time.time() - t0
        print(f"[deepcrawl:{_MODE}] {name} DONE in {dt:5.1f}s — {len(r['events'])} events / {r['pages']} pages / "
              f"status={r['status']}", flush=True)
        return (name, len(r["events"]), r["pages"], r["status"], round(dt, 1))
    except Exception as e:                                    # one company's crash must not sink the others
        dt = time.time() - t0
        print(f"[deepcrawl:{_MODE}] {name} CRASHED in {dt:5.1f}s: {type(e).__name__}: {e}", flush=True)
        return (name, "CRASH", 0, str(e)[:60], round(dt, 1))


async def main() -> None:
    os.makedirs(_OUT, exist_ok=True)
    client = QwenClient()                                     # ONE shared client → one shared in-flight semaphore for both modes
    print(f"\n{'='*80}\n===== DEEPCRAWL mode={_MODE} | {len(COMPANIES)} companies | max_pages={os.environ.get('EVENT_MAX_PAGES','?')}\n{'='*80}", flush=True)

    t_start = time.time()                                     # total wall-clock start
    if _CONCURRENT:
        # ALL companies at once — each crawl_company runs its own BFS, but every page's VLM call lands on the SAME server
        # continuous batch, so 4 companies' pages decode together. gather => wall-clock ≈ the slowest company, NOT the sum.
        results = await asyncio.gather(*(_one(n, u, client) for n, u in COMPANIES.items()))
    else:
        # one company fully finishes before the next starts — total ≈ SUM of per-company times. The stable/clear baseline.
        results = []
        for n, u in COMPANIES.items():
            results.append(await _one(n, u, client))
    total = time.time() - t_start

    per_sum = sum(r[4] for r in results)                      # sum of per-company durations (what pure-sequential would cost)
    print(f"\n{'='*80}\n===== DEEPCRAWL SUMMARY  mode={_MODE} =====", flush=True)
    for name, nev, npg, st, dt in results:
        print(f"  {name:6} {str(nev):>5} events | {npg:>3} pages | {dt:5.1f}s | {st}", flush=True)
    print(f"\n  TOTAL wall-clock: {total:5.1f}s   |   sum-of-per-company: {per_sum:5.1f}s", flush=True)
    if _CONCURRENT and per_sum > 0:                           # overlap factor: how much the shared batch actually saved
        print(f"  overlap speed-up vs sum: {per_sum/total:4.2f}x  (1.0 = no overlap; higher = concurrency helped)", flush=True)
    print(f"  每家 events → {_OUT}/<NAME>/summary.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
