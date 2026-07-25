"""thekillerdeal — END-TO-END pipeline test on 100 companies. Each company: render seed → extract(events + routes) →
follow routes → render → extract → … (BFS close-loop, REAL Qwen-VL persona prompts, text-only under NO_SHOT) → collect
ALL events across ALL its pages. Every company's events land in ONE output folder: tests/thekillerdeal/.

用一句话讲完: 从 sample_100 的 100 个 clean ir_url 各跑一次完整 crawl_company(真 pipeline:render→VLM 抽 events+routes→
跟 route 往深爬→再 render→…,persona prompt)→ 每家把所有页的 events 汇总 → 写 companies/<slug>.json + 顶层
all_events.jsonl + summary.txt,全部页 trace 落 trace/<slug>/。company-level semaphore 控并发(4×batch5≈20 in-flight ≈
VLM 满载但不过饱和,保 routing 质量)。{USER 2026-07-24 "pick 100 companies, render then route then render, return final
results, all pages events in one folder, call it thekillerdeal"} [CONFIDENCE: CONFIRMED — 直接指令].

Run ON THE POD (vLLM up):
  KD_CONC=4 KD_N=100 QWEN_API_KEY=<key> PYTHONPATH=/workspace/WaterEvents /root/venv/bin/python tests/thekillerdeal.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import urlsplit

import asyncpg                                                # read the FULL company universe from public.companies (KD_ALL)

from agent.event_agent.crawl import crawl_company          # THE full pipeline: BFS render→extract→route→render
from agent.event_agent import db                           # Postgres layer → events land in waterevents.events (frontend reads it)
from providers.qwen_llm import QwenClient                  # shared VLM client (one, so continuous batching pools all calls)

OUT = os.environ.get("KD_OUT", "/workspace/WaterEvents/tests/thekillerdeal")   # output folder (env-configurable → works on GCP VM too)
MANIFEST = os.environ.get("KD_MANIFEST", "/workspace/WaterEvents/tests/ir_official_variants/sample_100/manifest.json")   # clean seeds
CONC = int(os.environ.get("KD_CONC", "4"))                 # companies in flight PER PROCESS (× batch 5 VLM in-flight)
N = int(os.environ.get("KD_N", "100"))                     # how many companies (before slicing); 0 = ALL
RUN_ID = os.environ.get("KD_RUN_ID", "killerdeal")          # tag every event row so this run is filterable in the DB
KD_ALL = os.environ.get("KD_ALL", "") in ("1", "true", "yes")   # read the FULL 2000+ universe from public.companies, not sample_100
# MULTI-PROCESS slicing: the single-process asyncio loop is CPU-bound on one core (all render funnels through one event
# loop thread), so raising CONC in one process hits a ceiling. The launcher spawns KD_NSLICES processes, each pinned to
# its own cores (taskset) → true N-core parallelism. This process takes urls[SLICE_IDX::NSLICES] (round-robin balances
# slow companies across processes). Each writes its OWN all_events/summary file (no shared-file write race); companies/
# <slug>.json is already disjoint. {USER 2026-07-24 "manifest-slice multi-process"} [CONFIDENCE: CONFIRMED — single-loop ceiling].
NSLICES = int(os.environ.get("KD_NSLICES", "1"))           # total slice processes the launcher started
SLICE_IDX = int(os.environ.get("KD_SLICE_IDX", "0"))       # 0-based index of THIS process


async def _company_id(pool, url: str) -> str:
    """Resolve (or create) the waterevents.companies row for this seed url → its uuid id. WHY: the frontend's /api/events
    joins events.company_id → companies.ir_url to show the company host, so every event we flush needs a real company row.
    The sample_100 seeds are NOT in the queue (they came from ir_official_variants), so SELECT-then-INSERT (ir_url has no
    unique constraint → can't ON CONFLICT). {frontend/api/events.js joins company_id→ir_url}."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM companies WHERE ir_url = $1 LIMIT 1", url)   # already queued? reuse its id
        if row:
            return row["id"]
        row = await conn.fetchrow(                                                             # else create it (id defaults gen_random_uuid)
            "INSERT INTO companies (ir_url, status, run_id) VALUES ($1, 'discovering', $2) RETURNING id", url, RUN_ID)
        return row["id"]


def _slug(url: str) -> str:                                # stable per-company folder name (host)
    host = urlsplit(url if url.startswith("http") else "https://" + url).netloc or "x"
    return re.sub(r"[^a-z0-9.]+", "-", host.lower()).strip("-")[:60] or "x"


async def _one(url: str, client: QwenClient, pool, sem: asyncio.Semaphore) -> dict:
    """Run the FULL crawl_company pipeline on one company → its events, written to companies/<slug>.json AND flushed live
    to waterevents.events (per-page, via on_events) so the Vercel frontend shows them as they're discovered."""
    async with sem:                                        # bound company concurrency so total VLM in-flight ≈ ceiling
        slug = _slug(url)
        t0 = time.time()
        try:
            cid = await _company_id(pool, url)             # this company's waterevents.companies uuid (create if new)
            # on_events: crawl calls this with the NEW events from EACH page → flush them to the DB immediately (live view)
            async def _flush(evs, page=None):               # noqa: ANN001 — evs: list[event dict], page: {url,content}
                await db.flush_events(pool, cid, RUN_ID, evs)
                if page:                                    # persist source page content → pages table (frontend "Source page")
                    await db.save_pages(pool, cid, RUN_ID, [page])
            res = await crawl_company(url, trace_dir=os.path.join(OUT, "trace", slug), client=client, on_events=_flush)
        except Exception as e:                             # noqa: BLE001 — one company must not sink the whole run
            return {"url": url, "slug": slug, "error": f"{type(e).__name__}: {str(e)[:160]}", "sec": round(time.time() - t0, 1)}
        evs = res.get("events") or []
        rec = {"url": url, "slug": slug, "pages": res.get("pages"), "n_events": len(evs),
               "sec": round(time.time() - t0, 1), "events": evs}
        os.makedirs(os.path.join(OUT, "companies"), exist_ok=True)     # per-company events file (written as each finishes)
        with open(os.path.join(OUT, "companies", slug + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        return rec


async def main() -> None:
    os.makedirs(os.path.join(OUT, "companies"), exist_ok=True)
    if KD_ALL:                                             # FULL universe: every distinct ir_url in public.companies (~2730)
        conn = await asyncpg.connect(os.environ["WATEREVENTS_DB_DSN"], statement_cache_size=0)   # default schema=public
        rows = await conn.fetch("SELECT DISTINCT ir_url FROM public.companies "
                                "WHERE ir_url IS NOT NULL AND ir_url <> '' ORDER BY ir_url")
        await conn.close()
        urls = [r["ir_url"] for r in rows]
    else:                                                 # scoped: the clean sample_100 seeds
        urls = [p["url"] for p in (json.load(open(MANIFEST, encoding="utf-8")).get("pages") or [])]
    urls = urls[:N] if N else urls                        # N=0 → all
    urls = urls[SLICE_IDX::NSLICES]                        # THIS process's disjoint round-robin slice
    _sfx = f"_s{SLICE_IDX}" if NSLICES > 1 else ""          # per-process output suffix → no shared-file write race
    print(f"[kd] slice {SLICE_IDX+1}/{NSLICES}: {len(urls)} companies | conc={CONC} | NO_SHOT text-only | run_id={RUN_ID}", flush=True)
    client = QwenClient()                                  # ONE shared client → vLLM continuous batching pools everything
    pool = await db.connect_pool(min_size=2, max_size=8)   # events flush live to waterevents.events (frontend reads it)
    sem = asyncio.Semaphore(CONC)
    t0 = time.time()
    done = [0]
    all_f = open(os.path.join(OUT, f"all_events{_sfx}.jsonl"), "w", encoding="utf-8")   # per-process flat corpus (no race)

    async def _wrap(u: str) -> dict:
        r = await _one(u, client, pool, sem)
        done[0] += 1
        for e in r.get("events") or []:                    # append each company's events to the flat corpus
            all_f.write(json.dumps({"company": r["slug"], **e}, ensure_ascii=False) + "\n")
        all_f.flush()                                      # flush so a mid-run kill keeps what finished
        print(f"[kd] {done[0]}/{len(urls)}  {r['slug']:32}  events={r.get('n_events','ERR')}  "
              f"pages={r.get('pages','-')}  {r.get('sec')}s  {r.get('error','')}", flush=True)
        return r

    results = await asyncio.gather(*(_wrap(u) for u in urls))
    all_f.close()
    dt = time.time() - t0

    # aggregate — the end-to-end pipeline scorecard
    ok = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]
    with_ev = [r for r in ok if (r.get("n_events") or 0) > 0]
    total_ev = sum(r.get("n_events") or 0 for r in ok)
    total_pages = sum(r.get("pages") or 0 for r in ok)
    top = sorted(ok, key=lambda r: -(r.get("n_events") or 0))[:15]
    summary = (
        "=== THE KILLER DEAL — 100-company end-to-end pipeline ===\n"
        f"companies            : {len(results)}\n"
        f"wall time            : {dt:.0f}s  ({len(results)/dt*60:.1f} companies/min)\n"
        f"crawled OK           : {len(ok)}   (hard errors: {len(err)})\n"
        f"companies WITH events: {len(with_ev)}  ({100*len(with_ev)/max(len(ok),1):.0f}% of OK)\n"
        f"TOTAL events         : {total_ev}\n"
        f"TOTAL pages rendered : {total_pages}\n"
        f"avg events/company   : {total_ev/max(len(ok),1):.1f}\n"
        "top 15 by events     :\n" + "".join(f"    {r['n_events']:4}ev {r['pages']:3}pg  {r['slug']}\n" for r in top)
    )
    print("\n" + summary, flush=True)
    with open(os.path.join(OUT, f"summary{_sfx}.txt"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
        for r in results:
            f.write(json.dumps({k: v for k, v in r.items() if k != "events"}, ensure_ascii=False) + "\n")
    await pool.close()                                     # release the asyncpg pool cleanly
    print(f"[kd] done → {OUT}/  (companies/<slug>.json · all_events.jsonl · summary.txt · trace/<slug>/) + waterevents.events", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
