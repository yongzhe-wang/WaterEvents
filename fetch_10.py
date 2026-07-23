"""fetch_10 — run the event crawl over 10 real company IR pages → a READABLE per-company output tree.

用一句话讲完: 读 tests/ten_companies.json(10 家 IR 落地页)→ 每家跑 event_agent.crawl_company → 为每家产出一个
清晰目录:events.txt(最终事件列表)+ pages/NNNN_<url>.txt(每页一份:用的完整 prompt〔含页面正文〕+ 模型 RAW
输出 + 解析出的 events/routes)+ trace/(截图 + 原始 artifact)。QWEN_DEBUG_DIR 让 client 把每次 VLM 调用的
prompt+页面内容+输出落成 req_*.txt,本 runner 再按页面 url 重命名 + 写 events.txt 汇总。

Structure per company (tests/10event/<TICKER>/):
  events.txt                     — final deduped event list: date | type | title + urls
  pages/0001_<url-slug>.txt      — one VLM call = one page: SYSTEM+USER prompt (page content INSIDE) + RAW OUTPUT + PARSED
  trace/pages/NNNN_slug/...      — Tracer: content.txt / screenshot.jpg / result.json / meta.json (deep debug)

Run ON RUNPOD (AWQ server, --max-model-len 32768):
  cd /workspace/WaterEvents && QWEN_BASE_URLS=http://127.0.0.1:8000/v1 QWEN_SERVED_NAME=qwen-vl QWEN_API_KEY=$KEY \
    QWEN_MAX_TOKENS=12000 EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=40 EVENT_USE_IMAGE=1 \
    /root/venv/bin/python fetch_10.py
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time

from agent.event_agent import crawl_company
from providers.qwen_llm import QwenClient
from providers.qwen_llm import config as qcfg               # mutate DEBUG_DIR per-company so its req_*.txt land in its dir

_COMPANIES = os.environ.get("FETCH_COMPANIES",
                            os.path.join(os.path.dirname(__file__), "tests", "ten_companies.json"))
_OUTROOT = os.environ.get("FETCH_OUT", os.path.join(os.path.dirname(__file__), "tests", "10event"))
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "40"))

_PAGE_URL_RE = re.compile(r'PAGE URL:\s*(\S+)')               # every dumped USER prompt starts with "PAGE URL: <url>"


def _slug(url: str) -> str:
    """Filesystem-safe short slug from a url — for a human-readable per-page filename."""
    return re.sub(r"[^a-z0-9]+", "-", (url or "").lower()).strip("-")[:70] or "page"


def _write_events_txt(path: str, ticker: str, ir_url: str, events: list, pages: int) -> None:
    """The company's FINAL event list — one readable block: date | type | title, then its urls indented under it."""
    L = [f"===== {ticker} — {len(events)} events over {pages} pages =====", f"IR: {ir_url}", ""]
    for e in sorted(events, key=lambda x: x.get("date") or "", reverse=True):
        L.append(f"{(e.get('date') or '—'):12} | {(e.get('type') or '—'):18} | {(e.get('title') or '')[:80]}")
        for u in e.get("urls", []):
            L.append(f"       {u}")
        L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def _organize_pages(pages_dir: str) -> int:
    """Rename each QWEN_DEBUG_DIR req_*.txt → NNNN_<page-url-slug>.txt. Each file ALREADY holds the full SYSTEM+USER
    prompt (the page content is inside the USER prompt) + the RAW MODEL OUTPUT + the PARSED events/routes — so one file
    per page IS 'the page + the prompt + its output'. We just give it a page-recognisable name. Returns count."""
    n = 0
    for f in sorted(glob.glob(os.path.join(pages_dir, "req_*.txt"))):
        try:
            head = open(f, encoding="utf-8").read(8000)       # url is near the top (in the USER prompt)
        except Exception:                                     # noqa: BLE001 — unreadable dump → keep original name
            continue
        m = _PAGE_URL_RE.search(head)
        idx = os.path.basename(f).replace("req_", "").replace(".txt", "")
        newname = f"{idx}_{_slug(m.group(1)) if m else 'page'}.txt"
        os.rename(f, os.path.join(pages_dir, newname))
        n += 1
    return n


def _kill_stale_fetch10() -> None:
    """Auto-kill any OTHER running fetch_10 process (a prior overlapping run) so this run starts clean. Excludes our
    OWN pid — that's exactly why `pkill -f fetch_10.py` can't be used (its command line self-matches "fetch_10.py" and
    kills the killer). {USER 2026-07-23 "we need auto kill, fix the code" — the pkill self-match left 2 overlapping
    runs polluting tests/10event}. [CONFIDENCE: CONFIRMED 100% — the self-match left proc=2 after every pkill]."""
    import subprocess
    my = os.getpid()
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10).stdout
    except Exception:                                         # noqa: BLE001 — ps unavailable → best-effort skip
        return
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid, args = int(parts[0]), parts[1]
        if pid != my and "fetch_10.py" in args:               # another fetch_10 (not us) → kill it
            try:
                os.kill(pid, 9)
                print(f"[fetch_10] 🔪 killed stale run pid={pid}", flush=True)
            except Exception:                                 # noqa: BLE001 — already gone / no perm → ignore
                pass


# Company-level concurrency cap. WHY concurrent (not the old sequential `for c in companies`): a SINGLE company's BFS is
# render-bound — between VLM rounds the GPU idles while the browser renders the next batch (NVDA: 40 pages took ~48min,
# GPU 100% only in bursts). Crawling companies CONCURRENTLY overlaps company A's render gaps with company B's VLM decode
# on the server's continuous batch → fills the GPU → ~2.8× faster wall-clock (measured by bench_parallel.py: 44s vs
# 123.7s for 3 companies). The cap bounds how many companies render at once (each holds up to EVENT_BATCH browser pages);
# the server's --max-num-seqs + the client semaphore bound the VLM side. {USER 2026-07-23 "the gpu can handle more";
# bench 2.81× concurrent} [CONFIDENCE: CONFIRMED — bench measured the 2.8× (once run with a VALID api key; the earlier
# 0-event bench was a 401 auth failure, not a throughput ceiling)].
# Default 8: the server pool is 48 seqs (274,640-token KV, AWQ 2.6×) with PRIORITY scheduling — bulk crawl runs at the
# default priority=0 and real-time incremental monitors preempt via priority=-10, so a bulk batch can safely fill most of
# the pool without starving latency-sensitive traffic. Each company keeps ~3-4 VLM requests in flight (render-bound), so
# 8 companies ≈ 28-32 seqs — fills the pool with headroom. Measured: concurrency=4 held Running at 12-14/48 (pool
# under-fed); raise to fill it. For a full 10-company batch set FETCH_COMPANY_CONCURRENCY=10 (all at once). {USER
# 2026-07-23 "max_num_seqs 48 ... priority 调度 ... 多个服务安全共享"} [CONFIDENCE: CONFIRMED — live server showed
# Running 12-14/48 Waiting 0 at concurrency=4, i.e. the feeder, not the server, was the cap].
_COMPANY_CONCURRENCY = int(os.environ.get("FETCH_COMPANY_CONCURRENCY", "8"))


async def _crawl_one(c: dict, client: "QwenClient", sem: asyncio.Semaphore) -> dict:
    """Crawl ONE company end-to-end (render→VLM→events.txt→organize pages), bounded by the company-level semaphore. Sets
    a TASK-LOCAL debug dir (qcfg.set_debug_dir) so this company's req_*.txt dumps land in ITS pages/ dir even while other
    companies crawl concurrently — the old module-global qcfg.DEBUG_DIR would have raced across the concurrent tasks."""
    ticker = c["ticker"].replace("/", "_").replace(":", "_")
    url = c["ir_url"]
    cdir = os.path.join(_OUTROOT, ticker)
    pages_dir = os.path.join(cdir, "pages")
    # RESUME guard — if THIS company already produced a non-empty events.txt in a PRIOR supervisor attempt, skip re-crawling
    # it. WHY: the auto-restart supervisor (supervise_fetch.sh) relaunches fetch_10 after an OOM/crash; without this, each
    # restart would `rm`-and-redo all 10 companies, so a run that OOMs on company #7 could never finish. Skipping the
    # already-done companies makes every restart move FORWARD (only the mid-crawl company that died gets redone — its
    # events.txt was never written). {USER 2026-07-23 "we need a way to auto restart or clean"} [CONFIDENCE: CONFIRMED 100%
    # — direct user request; events.txt is written only on full-company completion, so its presence == that company is done].
    events_txt = os.path.join(cdir, "events.txt")
    if os.path.exists(events_txt) and os.path.getsize(events_txt) > 0:
        head = open(events_txt, encoding="utf-8").readline()          # "===== TICKER — N events over M pages ====="
        m = re.search(r"—\s*(\d+)\s*events over\s*(\d+)\s*pages", head)
        ev, pg = (int(m.group(1)), int(m.group(2))) if m else (0, 0)  # parse the header counts for _SUMMARY
        print(f"[fetch_10] ⏭ {ticker:10} RESUME-skip (already done: {ev} events / {pg} pages)", flush=True)
        return {"ticker": ticker, "url": url, "events": ev, "pages": pg, "resumed": True}
    os.makedirs(pages_dir, exist_ok=True)
    async with sem:                                          # bound concurrent companies (each renders up to EVENT_BATCH pages)
        qcfg.set_debug_dir(pages_dir)                        # TASK-LOCAL (contextvar) — no race with sibling companies
        t0 = time.time()
        try:
            out = await crawl_company(url, max_pages=_MAX_PAGES, client=client,
                                      trace_dir=os.path.join(cdir, "trace"))
            _write_events_txt(os.path.join(cdir, "events.txt"), ticker, url, out["events"], out["pages"])
            npages = _organize_pages(pages_dir)
            dt = time.time() - t0
            print(f"[fetch_10] ✓ {ticker:10} {len(out['events']):4} events / {out['pages']:3} pages "
                  f"({npages} page-txts) in {dt:.0f}s", flush=True)
            return {"ticker": ticker, "url": url, "events": len(out["events"]), "pages": out["pages"], "seconds": round(dt)}
        except Exception as e:                              # noqa: BLE001 — one company failing must not sink the batch
            print(f"[fetch_10] ⛔ {ticker:10} FAILED — {type(e).__name__}: {e}", flush=True)
            return {"ticker": ticker, "url": url, "events": 0, "pages": 0, "error": str(e)[:200]}


async def main() -> None:
    _kill_stale_fetch10()                                     # AUTO-KILL any overlapping prior run before we start
    companies = json.load(open(_COMPANIES, encoding="utf-8"))
    os.makedirs(_OUTROOT, exist_ok=True)
    client = QwenClient()                                     # ONE client reused across all companies (shared semaphore)
    sem = asyncio.Semaphore(_COMPANY_CONCURRENCY)             # how many companies crawl AT ONCE (fills GPU render gaps)
    print(f"[fetch_10] {len(companies)} companies | max_pages={_MAX_PAGES} | concurrency={_COMPANY_CONCURRENCY} "
          f"| out={_OUTROOT}", flush=True)

    # All companies crawl CONCURRENTLY (bounded by sem) — company A's render idle time is filled by company B's VLM decode
    # on the server's continuous batch. gather preserves order so _SUMMARY matches ten_companies.json.
    summary = await asyncio.gather(*(_crawl_one(c, client, sem) for c in companies))
    finally_dir = os.path.join(_OUTROOT, "_SUMMARY.txt")
    with open(finally_dir, "w", encoding="utf-8") as f:
        f.write(f"10event run — {sum(s['events'] for s in summary)} events over {sum(s['pages'] for s in summary)} pages\n\n")
        for s in summary:
            f.write(f"  {s['ticker']:10} {s['events']:4} events / {s['pages']:3} pages"
                    f"{'  ⛔ ' + s['error'][:50] if s.get('error') else ''}\n")
    print(f"\n===== 10event DONE: {sum(s['events'] for s in summary)} events / "
          f"{sum(s['pages'] for s in summary)} pages =====", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
