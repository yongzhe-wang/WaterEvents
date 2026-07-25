"""tests/onecompany — RENDER/CRAWL baseline over N companies using the REAL crawl loop with the VLM replaced by a
random link selector (EVENT_FAKE_EXTRACT=1). Each company is crawled with its OWN kept trace dir; a results.txt
summarises every company. 1 browser, batch pages at a time (default 5), max_pages each (default 30), NO VLM.

Keeps ALL traces — every company gets tests/onecompany_out/<slug>/trace/pages/NNNN_<url>/{content,screenshot,html,
links,result,meta}. Nothing is deleted between companies. {USER 2026-07-24 "test three companies ... 30 page each ...
write to results txt ... keep all the trace"}.

Run ON THE POD:
  EVENT_FAKE_EXTRACT=1 IR_WATERCRAWL_BROWSERS=1 EVENT_BATCH=5 EVENT_MAX_PAGES=30 EVENT_USE_IMAGE=1 \
    WATERCRAWL_SHOT_CONCURRENCY=4 PYTHONPATH=/workspace/WaterEvents \
    /root/venv/bin/python /workspace/WaterEvents/tests/onecompany.py <url1> <url2> <url3>
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from urllib.parse import urlsplit

from agent.event_agent.crawl import crawl_company

OUT = os.environ.get("ONE_OUT") or os.path.join(os.path.dirname(__file__), "onecompany_out")
DEFAULT_SEEDS = [
    "https://investor.apple.com/investor-relations/default.aspx",
    "https://abc.xyz/investor/",
    "https://www.microsoft.com/en-us/investor/default",
]


def _slug(url: str) -> str:
    host = urlsplit(url if url.startswith("http") else "https://" + url).netloc or "company"
    return re.sub(r"[^a-z0-9.]+", "-", host.lower()).strip("-")


async def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    seeds = sys.argv[1:] or DEFAULT_SEEDS
    max_pages = int(os.environ.get("EVENT_MAX_PAGES", "30"))
    batch = int(os.environ.get("EVENT_BATCH", "5"))
    shot = os.environ.get("WATERCRAWL_SHOT_CONCURRENCY", "4")
    browsers = os.environ.get("IR_WATERCRAWL_BROWSERS", "1")

    header = (f"=== RENDER BASELINE (real crawl loop, VLM→random selector, NO VLM) ===\n"
              f"    companies={len(seeds)} | max_pages={max_pages} | batch={batch} | browsers={browsers} | "
              f"shot_concurrency={shot}\n")
    print(header, flush=True)
    lines = [header]
    total_pages = total_fail = 0
    t_all = time.time()

    for i, seed in enumerate(seeds, 1):
        slug = _slug(seed)
        trace = os.path.join(OUT, slug, "trace")                    # OWN kept trace dir per company
        print(f"\n### [{i}/{len(seeds)}] {seed}  → {trace}", flush=True)
        t0 = time.time()
        out = await crawl_company(seed, max_pages=max_pages, batch=batch, trace_dir=trace)
        dt = time.time() - t0
        # successful renders = pages the Tracer actually saved (a failed/empty render is not saved)
        pages_dir = os.path.join(trace, "pages")
        ok_renders = len([d for d in os.listdir(pages_dir)]) if os.path.isdir(pages_dir) else 0
        pages = out.get("pages") or 0
        fr = out.get("failed_render") or 0
        thru = round(pages / dt, 2) if dt else 0
        total_pages += pages
        total_fail += fr
        blk = (f"[{i}] {seed}\n"
               f"     slug          : {slug}\n"
               f"     pages visited : {pages}\n"
               f"     renders saved : {ok_renders}   (trace page folders)\n"
               f"     failed_render : {fr}   (empty/walled/giant-page-abort — no real render should be here)\n"
               f"     status        : {out.get('status')}\n"
               f"     wall time     : {dt:.0f}s   ({thru} pages/s)\n"
               f"     trace_dir     : {trace}\n")
        print(blk, flush=True)
        lines.append(blk)

    dt_all = time.time() - t_all
    footer = (f"\n=== OVERALL: {len(seeds)} companies | {total_pages} pages | {dt_all:.0f}s | "
              f"{round(total_pages/dt_all,2) if dt_all else 0} pages/s | total failed_render={total_fail} ===\n")
    print(footer, flush=True)
    lines.append(footer)

    with open(os.path.join(OUT, "results.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[onecompany] wrote {OUT}/results.txt  (all traces kept under {OUT}/<slug>/trace/)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
