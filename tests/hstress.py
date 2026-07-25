"""tests/hstress — HORIZONTAL render stress test. Render EVERY company's IR seed page ONCE (max_pages=1 → NO BFS, no
going deeper), across all companies, concurrently. VLM is replaced by the random selector (EVENT_FAKE_EXTRACT=1) so this
purely stresses the RENDER fleet at scale — no VLM load. Each page keeps its full trace (content/screenshot/html/links).

用一句话讲完: 从 public.companies 拿全部 distinct ir_url → 每个 url 跑 crawl_company(max_pages=1) 只渲染那一页(不进
frontier)→ 用 semaphore 控住并发 → 汇总 render 成功/失败/耗时,算大规模吞吐(pages/s)。{USER 2026-07-24 "stress test
horizontally, use all existing ir page link from all companies, each company one, no bfs no deeper, just run on all pages"}.

Run ON THE POD:
  EVENT_FAKE_EXTRACT=1 IR_WATERCRAWL_BROWSERS=3 WATERCRAWL_SHOT_CONCURRENCY=4 HSTRESS_CONCURRENCY=12 \
    WATEREVENTS_DB_DSN=<pooler dsn> PYTHONPATH=/workspace/WaterEvents /root/venv/bin/python /workspace/WaterEvents/tests/hstress.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import urlsplit

import asyncpg

from agent.event_agent.crawl import crawl_company

DSN = os.environ.get("WATEREVENTS_DB_DSN", "")              # source: public.companies; only required in DB mode (not file mode)
CONCURRENCY = int(os.environ.get("HSTRESS_CONCURRENCY", "12"))   # how many companies render at once
LIMIT = int(os.environ.get("HSTRESS_LIMIT", "0"))               # 0 = all; else cap the list (for a quick trial)
OUT = os.path.join(os.path.dirname(__file__), "hstress_out")


def _slug(url: str) -> str:
    host = urlsplit(url if url.startswith("http") else "https://" + url).netloc or "x"
    return re.sub(r"[^a-z0-9.]+", "-", host.lower()).strip("-")[:60] or "x"


async def _get_seeds() -> list[str]:
    # HSTRESS_URLS_FILE overrides the DB source: read a fixed url-per-line list instead of all companies. WHY: to re-test
    # a KNOWN failing subset (e.g. the 28 render-failure urls the diagnosis workflow surfaced) under the REAL controlled
    # concurrency (3 browsers × shot 4) — the low-concurrency diagurl proved 12 of them CAN render, but never measured the
    # true failure rate at the optimal single-GPU config. {USER 2026-07-24 "test all the methods ... find the reason for
    # the failure"} [CONFIDENCE: CONFIRMED — file path is opt-in; empty/unset falls back to the full-company DB query].
    urls_file = os.environ.get("HSTRESS_URLS_FILE", "")
    if urls_file:                                            # fixed-list mode: one url per line, blanks/`#` comments skipped
        with open(urls_file, encoding="utf-8") as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        return urls[:LIMIT] if LIMIT else urls
    c = await asyncpg.connect(DSN, statement_cache_size=0)   # default schema = public (where companies live)
    rows = await c.fetch("select distinct ir_url from public.companies "
                         "where ir_url is not null and ir_url <> '' order by ir_url")
    await c.close()
    urls = [r["ir_url"] for r in rows]
    return urls[:LIMIT] if LIMIT else urls


async def _one(url: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        t0 = time.time()
        try:
            out = await crawl_company(url, max_pages=1, batch=1, trace_dir=os.path.join(OUT, "trace", _slug(url)))
            return {"url": url, "sec": round(time.time() - t0, 1), "pages": out.get("pages"),
                    "failed_render": out.get("failed_render"), "status": out.get("status")}
        except Exception as e:                               # noqa: BLE001 — one page must not sink the whole stress run
            return {"url": url, "sec": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    seeds = await _get_seeds()
    n = len(seeds)
    print(f"[hstress] {n} IR pages | concurrency={CONCURRENCY} | max_pages=1 (NO BFS) | "
          f"browsers={os.environ.get('IR_WATERCRAWL_BROWSERS','?')} shot={os.environ.get('WATERCRAWL_SHOT_CONCURRENCY','?')} "
          f"FAKE_EXTRACT={os.environ.get('EVENT_FAKE_EXTRACT')}", flush=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()
    done = 0

    async def _wrap(u: str) -> dict:
        nonlocal done
        r = await _one(u, sem)
        done += 1
        if done % 25 == 0:
            el = time.time() - t0
            print(f"[hstress] {done}/{n}  {el:.0f}s  {done/el:.2f} pages/s", flush=True)
        return r

    results = await asyncio.gather(*(_wrap(u) for u in seeds))
    dt = time.time() - t0
    ok = sum(1 for r in results if r.get("pages"))
    fr = sum(1 for r in results if (r.get("failed_render") or 0))
    err = sum(1 for r in results if r.get("error"))
    slow = sorted((r for r in results if r.get("sec")), key=lambda r: -r["sec"])[:10]

    summary = (f"=== HORIZONTAL RENDER STRESS ===\n"
               f"pages          : {n}\n"
               f"wall time      : {dt:.0f}s\n"
               f"throughput     : {n/dt:.2f} pages/s\n"
               f"rendered ≥1pg  : {ok}\n"
               f"had failed_render: {fr}\n"
               f"hard errors    : {err}\n"
               f"slowest 10     : " + ", ".join(f"{r['sec']}s {_slug(r['url'])}" for r in slow) + "\n")
    print("\n" + summary, flush=True)
    with open(os.path.join(OUT, "results.txt"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"[hstress] wrote {OUT}/results.txt (+ traces under {OUT}/trace/<slug>/)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
