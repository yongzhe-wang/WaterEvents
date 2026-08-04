"""tests/media/media_dispatch_run.py — run the REAL stage-2 path over a dataset and measure it per url kind.

用一句话讲完: 读 dataset 里每个 event → 建一个 Chart → 把它 `media_urls` 里的**每一条** url 交给
`handlers.dispatch`(html→渲染+VLM、pdf/pptx/docx/xlsx→Docling、audio→whisper、video/webcast→yt-dlp 或
watercrawl.capture 抓流)→ 逐条计时、记录状态和产出 → 每个 event 落一份 trace.txt,再汇总成按 stratum 分组的
延迟表。**这是决定"要不要买 GPU"的那把尺子** —— Docling 和 whisper 在纯 CPU 上到底多慢,只有真跑一遍才知道。

WHY a second runner instead of extending media_run.py: media_run.py renders ONE url per event (its `event_url`) and
calls enrich_page directly — 0 references to `dispatch`. It therefore never reaches handle_office / handle_audio /
handle_video, which is exactly the 34% of urls (Docling) and 4.26% (video/webcast) this dataset was built to exercise.
{GREP 2026-08-03 "0 处 dispatch, 5 处 enrich_page"} [CONFIDENCE: CONFIRMED — counted in the file].

It writes NOTHING to the database. Chart is filled in memory and dumped to disk; the DB write path
(db_media.mark_enriched_media) is exercised separately by the worker, not here.

Run ON the VM (browser + tunnel to the VLM are both local there):
  cd /home/thebigsun/WaterEvents/backend
  set -a; sudo cat /etc/waterevents/fleet.env > /tmp/.f; . /tmp/.f; set +a
  PYTHONPATH=/home/thebigsun/WaterEvents/backend \
  MEDIA_DS=/home/thebigsun/WaterEvents/tests/datasets/media_100 \
  MEDIA_OUT=/home/thebigsun/WaterEvents/tests/media_dispatch_out \
  MEDIA_CONC=4 python3 ../tests/media/media_dispatch_run.py

Env:
  MEDIA_DS      dataset dir (a dir of ev*.json)
  MEDIA_OUT     output dir (one folder per event + results.jsonl + summary.txt)
  MEDIA_CONC    how many EVENTS in flight (default 4 — measured safe alongside the live fleet: the VLM peaks at
                ~2,059 req/h at concurrency 8 and production already uses ~1, so 4 leaves headroom)
  MEDIA_ONLY    comma-separated stratum filter, e.g. "pdf,xlsx" — run just those
  MEDIA_LIMIT   stop after N events (0 = all)
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import statistics
import sys
import time
import traceback

sys.path.insert(0, os.environ.get("PYTHONPATH", "").split(":")[0] or ".")

from providers.qwen_llm import QwenClient

from agent.media_agent.extract import router
from agent.media_agent.extract.chart import Chart
from agent.media_agent.pipeline.handlers import dispatch

_DS = os.environ.get("MEDIA_DS", "tests/datasets/media_100")
_OUT = os.environ.get("MEDIA_OUT", "tests/media_dispatch_out")
_CONC = int(os.environ.get("MEDIA_CONC", "4"))
_ONLY = [s.strip() for s in os.environ.get("MEDIA_ONLY", "").split(",") if s.strip()]
_LIMIT = int(os.environ.get("MEDIA_LIMIT", "0"))
_NO_SHOT = os.environ.get("WATERCRAWL_NO_SHOT", "1") in ("1", "true", "yes")
_USE_IMAGE = not _NO_SHOT
_PROXY = os.environ.get("WEBSHARE_PROXY") or None


def _trace(entry: dict, chart: Chart, per_url: list[dict], wall: float) -> str:
    """One event's full trace — what went in, what each url cost, what came out. Written per event so a bad result is
    debuggable without re-running (the whole point of the harness)."""
    m = entry.get("meta", {})
    L = [f"===== {entry['id']} =====",
         f"ticker={m.get('ticker')}  market={m.get('market')}  stratum={m.get('stratum')}  "
         f"type={entry['known_event'].get('type')}",
         f"wall={wall:.1f}s   urls={len(per_url)}",
         "", "--- KNOWN EVENT ---", json.dumps(entry["known_event"], ensure_ascii=False, indent=2),
         "", "--- PER URL ---"]
    for r in per_url:
        L.append(f"  [{r['kind']:6}] {r['secs']:7.1f}s  {r['status']:38}  {r['url'][:90]}")
    L += ["", "--- PRODUCED ---",
          f"  basic_info blocks : {len(chart.basic_info)}  "
          f"({', '.join(b.get('type', '?') for b in chart.basic_info[:12])})",
          f"  transcript segs   : {len(chart.transcript_segments)}",
          f"  files             : { {k: len(v) for k, v in chart.files.items() if v} }",
          f"  audio             : {len(chart.audio)}",
          "", "--- BASIC_INFO (first 3 blocks) ---"]
    for b in chart.basic_info[:3]:
        if b.get("type") == "table":
            L.append(f"  [TABLE] {b.get('caption', '')} headers={b.get('headers')} rows={len(b.get('rows') or [])}")
        else:
            L.append(f"  [{b.get('type')}] {(b.get('md') or '')[:400]}")
    if chart.transcript_segments:
        L += ["", "--- TRANSCRIPT (first 3 segments) ---"]
        for s in chart.transcript_segments[:3]:
            L.append(f"  {s.get('speaker')}: {(s.get('text') or '')[:220]}")
    for kind, items in chart.files.items():
        for f in items:
            L += ["", f"--- FILE {kind} {f['url'][:80]} ---",
                  f"  n_pages={f.get('n_pages')} tables={len(f.get('tables') or [])}",
                  f"  markdown[:600]: {(f.get('markdown') or '')[:600]}"]
    return "\n".join(L) + "\n"


async def run_event(entry: dict, client: QwenClient, sem: asyncio.Semaphore) -> dict:
    """One dataset event through the production path. Never raises — a crash becomes a recorded failure so one bad
    event cannot end the run (the harness must always produce a full table)."""
    async with sem:
        m = entry.get("meta", {})
        known = entry["known_event"]
        urls = list(known.get("media_urls") or [])
        chart = Chart({"title": known.get("title"), "date": known.get("date"),
                       "type": known.get("type"), "urls": urls})
        per_url: list[dict] = []
        t0 = time.monotonic()
        for u in urls:
            kind = router.classify(u)
            tu = time.monotonic()
            try:
                await dispatch(u, kind, chart, client=client, use_image=_USE_IMAGE, proxy=_PROXY)
                status = next((s.get("status", "?") for s in chart.urls.values() if s.get("url") == u), "?")
            except Exception as e:                       # noqa: BLE001 — record and continue; the table must complete
                status = f"EXC:{type(e).__name__}"
                print(f"[run] ⚠️ {entry['id']} {u[:60]} raised {type(e).__name__}: {str(e)[:90]}", flush=True)
            per_url.append({"url": u, "kind": kind, "secs": time.monotonic() - tu, "status": status})
        wall = time.monotonic() - t0

        d = os.path.join(_OUT, entry["id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "trace.txt"), "w", encoding="utf-8") as fh:
            fh.write(_trace(entry, chart, per_url, wall))
        with open(os.path.join(d, "chart.json"), "w", encoding="utf-8") as fh:
            json.dump(chart.to_dict(), fh, ensure_ascii=False, indent=2)

        rec = {"id": entry["id"], "stratum": m.get("stratum"), "ticker": m.get("ticker"),
               "market": m.get("market"), "type": known.get("type"), "wall": wall,
               "n_urls": len(urls), "per_url": per_url,
               "n_blocks": len(chart.basic_info), "n_segs": len(chart.transcript_segments),
               "n_files": sum(len(v) for v in chart.files.values()), "n_audio": len(chart.audio)}
        ok = bool(rec["n_blocks"] or rec["n_segs"] or rec["n_files"] or rec["n_audio"])
        rec["usable"] = ok
        print(f"[run] {'✅' if ok else '⛔'} {entry['id']:22} {wall:7.1f}s  {len(urls)} urls → "
              f"{rec['n_blocks']}b {rec['n_segs']}s {rec['n_files']}f {rec['n_audio']}a", flush=True)
        return rec


def summarize(recs: list[dict]) -> str:
    """The table this whole harness exists to produce: per-stratum latency + usable rate, and per-KIND latency —
    the kind rows are what decide whether Docling and whisper need a GPU."""
    L = ["", "=" * 100, f"SUMMARY  n={len(recs)}  concurrency={_CONC}", "=" * 100, "",
         "--- BY STRATUM (event wall-clock) ---",
         f"  {'stratum':12} {'n':>3} {'usable':>7} {'p50':>8} {'p95':>8} {'max':>8}  {'blocks':>7} {'segs':>6} {'files':>6} {'audio':>6}"]
    by_s: dict[str, list] = {}
    for r in recs:
        by_s.setdefault(r["stratum"] or "?", []).append(r)
    for s, rs in sorted(by_s.items(), key=lambda kv: -len(kv[1])):
        w = sorted(r["wall"] for r in rs)
        L.append(f"  {s:12} {len(rs):>3} {sum(1 for r in rs if r['usable']):>4}/{len(rs):<2} "
                 f"{statistics.median(w):>8.1f} {w[max(0, int(len(w)*0.95)-1)]:>8.1f} {w[-1]:>8.1f}  "
                 f"{sum(r['n_blocks'] for r in rs):>7} {sum(r['n_segs'] for r in rs):>6} "
                 f"{sum(r['n_files'] for r in rs):>6} {sum(r['n_audio'] for r in rs):>6}")

    L += ["", "--- BY URL KIND (per-url latency — THE gpu-sizing numbers) ---",
          f"  {'kind':8} {'n':>4} {'p50':>8} {'p95':>8} {'max':>8}   top statuses"]
    by_k: dict[str, list] = {}
    for r in recs:
        for u in r["per_url"]:
            by_k.setdefault(u["kind"], []).append(u)
    for k, us in sorted(by_k.items(), key=lambda kv: -len(kv[1])):
        sec = sorted(u["secs"] for u in us)
        st: dict[str, int] = {}
        for u in us:
            key = (u["status"] or "?").split(":")[0] + (":" + (u["status"] or "").split(":")[1] if ":" in (u["status"] or "") else "")
            st[key] = st.get(key, 0) + 1
        top = ", ".join(f"{a}×{b}" for a, b in sorted(st.items(), key=lambda kv: -kv[1])[:3])
        L.append(f"  {k:8} {len(us):>4} {statistics.median(sec):>8.1f} "
                 f"{sec[max(0, int(len(sec)*0.95)-1)]:>8.1f} {sec[-1]:>8.1f}   {top}")

    bad = [r for r in recs if not r["usable"]]
    L += ["", f"--- NOTHING-USABLE: {len(bad)}/{len(recs)} ---"]
    for r in bad[:20]:
        L.append(f"  {r['id']:22} {r['stratum']:10} " +
                 "; ".join(f"{u['kind']}={u['status'][:40]}" for u in r["per_url"][:3]))
    return "\n".join(L)


async def main() -> None:
    files = sorted(glob.glob(os.path.join(_DS, "ev*.json")))
    entries = [json.load(open(f, encoding="utf-8")) for f in files]
    if _ONLY:
        entries = [e for e in entries if (e.get("meta", {}).get("stratum") in _ONLY)]
    if _LIMIT:
        entries = entries[:_LIMIT]
    os.makedirs(_OUT, exist_ok=True)
    print(f"dataset={_DS}  events={len(entries)}  conc={_CONC}  use_image={_USE_IMAGE}  proxy={'yes' if _PROXY else 'no'}",
          flush=True)

    client = QwenClient()
    sem = asyncio.Semaphore(_CONC)
    t0 = time.monotonic()
    recs = await asyncio.gather(*(run_event(e, client, sem) for e in entries), return_exceptions=True)
    good = [r for r in recs if isinstance(r, dict)]
    for r in recs:
        if not isinstance(r, dict):
            print(f"[run] ⛔ ESCAPED: {r!r}", flush=True)
            traceback.print_exception(type(r), r, r.__traceback__)

    with open(os.path.join(_OUT, "results.jsonl"), "w", encoding="utf-8") as fh:
        for r in good:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    s = summarize(good) + f"\n\nTOTAL WALL: {time.monotonic()-t0:.0f}s for {len(good)} events at conc={_CONC}\n"
    print(s, flush=True)
    with open(os.path.join(_OUT, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(s)


if __name__ == "__main__":
    asyncio.run(main())
