"""bench_parallel — measure company-level CONCURRENT vs SEQUENTIAL crawl to see if parallelising companies actually
cuts total wall-clock (or if the single-A5000 server decode ceiling caps it).

用一句话讲完: 取 N 家公司,先 CONCURRENT(asyncio.gather 全家同时爬,共享一个 QwenClient → server continuous batch)
测总时间,再 SEQUENTIAL(逐家)测总时间 → 对比。假设:sequential 在每家 render 批次时 server 空转;concurrent 用
别家的 VLM 请求填满那些空隙 → 更快,但被 server 的 decode 吞吐上限卡住(不会线性降)。跑 concurrent 在前(冷缓存)、
sequential 在后(热缓存)—— 若 concurrent 冷跑还更快,并发收益是真的(保守测法)。

NO debug dump (qcfg.DEBUG_DIR raced under concurrency); this measures TIMING only. Run ON RUNPOD (AWQ server).
  BENCH_N=3 EVENT_MAX_PAGES=15 QWEN_...=... /root/venv/bin/python bench_parallel.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from agent.event_agent import crawl_company
from providers.qwen_llm import QwenClient
from providers.qwen_llm import config as qcfg

qcfg.DEBUG_DIR = ""                                           # OFF — a global dir would race across concurrent companies

_COMPANIES = os.environ.get("FETCH_COMPANIES",
                            os.path.join(os.path.dirname(__file__), "tests", "ten_companies.json"))
_N = int(os.environ.get("BENCH_N", "3"))                     # how many companies in the test (keep small — 2 full runs)
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "15"))
_BENCH_ROOT = "/tmp/bench10"


async def _one(company: dict, client: QwenClient, mode: str) -> dict:
    """Crawl ONE company (trace to a per-mode/per-ticker tmp dir so concurrent runs don't collide)."""
    ticker = company["ticker"].replace("/", "_").replace(":", "_")
    t0 = time.time()
    out = await crawl_company(company["ir_url"], max_pages=_MAX_PAGES, client=client,
                              trace_dir=os.path.join(_BENCH_ROOT, mode, ticker))
    return {"ticker": ticker, "events": len(out["events"]), "pages": out["pages"], "seconds": round(time.time() - t0, 1)}


async def run_concurrent(companies: list, client: QwenClient) -> tuple[float, list]:
    """All N companies crawl AT ONCE — they share the client's semaphore + the server's continuous batch."""
    t = time.time()
    res = await asyncio.gather(*(_one(c, client, "conc") for c in companies))
    return round(time.time() - t, 1), res


async def run_sequential(companies: list, client: QwenClient) -> tuple[float, list]:
    """Companies crawl ONE AFTER ANOTHER — the current fetch_10 behaviour."""
    t = time.time()
    res = [await _one(c, client, "seq") for c in companies]
    return round(time.time() - t, 1), res


async def main() -> None:
    companies = json.load(open(_COMPANIES, encoding="utf-8"))[:_N]
    client = QwenClient()
    tks = [c["ticker"] for c in companies]
    print(f"[bench] {len(companies)} companies {tks} | max_pages={_MAX_PAGES}", flush=True)

    # CONCURRENT first (COLD cache) — the harder case; if it still wins, the parallelism gain is real.
    print("\n[bench] === CONCURRENT (all at once, cold cache) ===", flush=True)
    tc, rc = await run_concurrent(companies, client)
    for r in rc:
        print(f"  {r['ticker']:10} {r['events']:3} events / {r['pages']:2} pages / {r['seconds']}s", flush=True)
    print(f"[bench] CONCURRENT total = {tc}s", flush=True)

    # SEQUENTIAL second (WARM cache) — biased FASTER; so a concurrent win here is conservative.
    print("\n[bench] === SEQUENTIAL (one by one, warm cache) ===", flush=True)
    ts, rs = await run_sequential(companies, client)
    for r in rs:
        print(f"  {r['ticker']:10} {r['events']:3} events / {r['pages']:2} pages / {r['seconds']}s", flush=True)
    print(f"[bench] SEQUENTIAL total = {ts}s", flush=True)

    print(f"\n[bench] ===== RESULT: concurrent {tc}s vs sequential {ts}s → "
          f"{ts / tc:.2f}× {'FASTER concurrent' if tc < ts else 'no gain / slower concurrent'} "
          f"(concurrent was COLD, sequential WARM → gain is a LOWER bound) =====", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
