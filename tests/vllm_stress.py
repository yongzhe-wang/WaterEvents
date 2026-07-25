"""vllm_stress — ISOLATED stress test of the Qwen-VL server, DECOUPLED from render/crawl/worker.

用一句话讲完: 从已有的 trace 页(content.txt + screenshot.jpg)重建 ~200 个 PRODUCTION-identical VLM 请求(同 SYSTEM +
build_user(tag_links(text)) + 同图 + 同 SCHEMA + 同 12000 max_tokens),按给定并发 QWEN_CONCURRENCY 全部打给服务器,逐个
计时 → 报吞吐(req/s)、延迟分布(p50/p95/max)、空返回率、错误率、finish=length 截断率、平均输出 token。回答一个问题:
"这块 GPU 在全质量下,真实页面能以多快、多正确地处理?" —— 把 render 的噪音全隔离掉,只压 server。

Run ON the pod (GPU there):
  cd /workspace/WaterEvents && QWEN_BASE_URLS=http://127.0.0.1:8000/v1 QWEN_SERVED_NAME=qwen-vl QWEN_API_KEY=$KEY \
    QWEN_MAX_TOKENS=12000 QWEN_CONCURRENCY=16 STRESS_N=200 /root/venv/bin/python tests/vllm_stress.py
"""
from __future__ import annotations

import asyncio
import base64
import glob
import os
import statistics
import time

from agent.event_agent import prompts
from providers.qwen_llm import QwenClient

_N = int(os.environ.get("STRESS_N", "200"))                         # how many pages to fire
_CONC = int(os.environ.get("QWEN_CONCURRENCY", "16"))              # in-flight cap (the throughput dial)
_TRACES = os.environ.get("STRESS_TRACES", "/workspace/WaterEvents/tests/stress1000/traces")
_TEXTCAP = int(os.environ.get("EVENT_VISION_TEXT_CHARS", "24000"))  # same text cap as the crawl


def _gather_jobs(n: int) -> list[dict]:
    """Build n PRODUCTION-identical jobs from STAGED page dirs (each holds content.txt + screenshot.jpg). Reads from a
    FLAT local-disk dir (STRESS_TRACES/*/): the trace tree lives on a slow network FS, so a pre-stage step copies ~n
    pages to local /tmp first and points STRESS_TRACES here — reading them is then instant. Each job is the exact
    {system, user, image_b64, guided_json} the crawl's _job() sends — the server sees real prompts at real size."""
    jobs = []
    dirs = sorted(glob.glob(os.path.join(_TRACES, "*")))           # flat: STRESS_TRACES/<id>/{content.txt,screenshot.jpg}
    print(f"[stress] scanning {len(dirs)} staged page dirs under {_TRACES}", flush=True)
    for d in dirs:
        ct, sc = os.path.join(d, "content.txt"), os.path.join(d, "screenshot.jpg")
        try:
            text = open(ct, encoding="utf-8").read()
            img_b64 = base64.b64encode(open(sc, "rb").read()).decode("ascii")
        except Exception:                                          # noqa: BLE001 — skip an unreadable/incomplete staged page
            continue
        if len(text) < 500:                                        # skip near-empty pages (not representative load)
            continue
        tagged, _tm = prompts.tag_links(text)                      # same link-tagging the crawl applies before the model sees it
        user = prompts.build_user(tagged[:_TEXTCAP], "http://stress.local/" + os.path.basename(d))
        jobs.append({"system": prompts.SYSTEM, "user": user, "image_b64": img_b64, "guided_json": prompts.SCHEMA})
        if len(jobs) >= n:
            break
    return jobs


async def main() -> None:
    jobs = _gather_jobs(_N)
    print(f"[stress] built {len(jobs)} jobs | concurrency={_CONC} | max_tokens={os.environ.get('QWEN_MAX_TOKENS','?')} "
          f"| textcap={_TEXTCAP}", flush=True)
    if not jobs:
        print("[stress] NO jobs — no trace pages with content.txt+screenshot.jpg found under", _TRACES, flush=True)
        return
    client = QwenClient(concurrency=_CONC)                          # the semaphore keeps exactly _CONC in flight

    lat: list[float] = []                                           # per-request wall time
    rows: list[dict] = []

    async def _timed(job: dict) -> None:
        t0 = time.monotonic()
        res = await client.send_one(**job)                         # bounded by client._sem (concurrency)
        dt = time.monotonic() - t0
        lat.append(dt)
        rows.append(res if isinstance(res, dict) else {})

    t_all = time.monotonic()
    await asyncio.gather(*(_timed(j) for j in jobs))               # fire all; _CONC in flight at once
    total = time.monotonic() - t_all

    # ── analyze ──
    errs = [r for r in rows if r.get("_error")]
    ok = [r for r in rows if not r.get("_error")]
    empty = [r for r in ok if not (r.get("events") or []) and not (r.get("routes") or [])]
    trunc = [r for r in rows if r.get("__finish__") == "length"]
    ev_total = sum(len(r.get("events") or []) for r in ok)
    rt_total = sum(len(r.get("routes") or []) for r in ok)
    lat.sort()

    def _pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0.0

    print("\n==================== VLM SERVER STRESS RESULT ====================", flush=True)
    print(f"  requests      : {len(jobs)}   concurrency: {_CONC}", flush=True)
    print(f"  wall-clock    : {total:.1f}s", flush=True)
    print(f"  THROUGHPUT    : {len(jobs)/total:.2f} req/s   ({len(jobs)/total*60:.0f} req/min → "
          f"{len(jobs)/total*3600:.0f}/hr)", flush=True)
    print(f"  latency       : p50={_pct(0.5):.1f}s  p95={_pct(0.95):.1f}s  max={max(lat) if lat else 0:.1f}s  "
          f"min={min(lat) if lat else 0:.1f}s", flush=True)
    print(f"  correctness   : ok={len(ok)}  errors={len(errs)}  EMPTY(0ev+0rt)={len(empty)}  truncated(length)={len(trunc)}", flush=True)
    print(f"  yield         : {ev_total} events + {rt_total} routes over {len(ok)} ok pages "
          f"(avg {ev_total/max(1,len(ok)):.1f} ev/page)", flush=True)
    if errs:
        from collections import Counter
        ec = Counter((r.get("_error") or "")[:60] for r in errs)
        print(f"  error types   : {dict(ec)}", flush=True)
    print("==================================================================", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
