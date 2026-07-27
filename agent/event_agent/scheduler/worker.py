"""event_agent.queue_worker — the resident WORK-QUEUE WORKER POOL that drains work_queue: claim → scan → complete/fail,
forever. (Distinct from event_agent.worker, the older company-queue DISCOVERY worker — this one drains the unified
full+incremental work_queue; both live in event_agent after the 2026-07-26 folder merge.)

用一句话讲完: 每个 async worker 都是**全能的**(OMNIPOTENT)—— 循环 { claim_work(SKIP-LOCKED, priority 阶梯) →
scan_unit(统一引擎, incremental 浅/full 深) → complete_work(自 re-arm) },同一个共享池、无预留槽。哪种任务由 priority
阶梯决定:incremental(pri 10)到期就先抓、没有就啃 full(pri 100)backlog → VLM 永不空转。停进程=暂停(队列状态在 DB),
重启=续跑。{USER 2026-07-26 "same pool, omnipotent workers that take both tasks from the pool" — 去掉 reserved-slot}
[CONFIDENCE: CONFIRMED — 直接指令改为全能 worker;freshness 靠 priority 排序而非物理预留]。
权衡: 全能池最简单,但一个刚到期的 incremental 最坏要等某个在跑的 full BFS 释放(≤ EVENT_COMPANY_BUDGET_S)。若 30-min
新鲜度实测被 full 深爬拖累,再考虑恢复 reserved slot / 两个物理池。

WHY 多进程部署: 一个进程里所有 render 走 watercrawl 的单一 event-loop 线程(CPU-bound, 单核天花板 ~0.5 pages/s)。所以真
并行靠**多进程**(每个 `python -m agent.event_agent.queue_worker` 自己的 loop + taskset 绑核, 共享 work_queue 协调) —— 进程
内的 _WORKERS 个 async task 只是让多个 VLM 调用(I/O)重叠, render 仍串行。部署 = N 进程 × 少量 async task。
{USER 2026-07-25 "one queue, reserved slots, VLM never idle"} [CONFIDENCE: CONFIRMED — 单-loop 天花板本 session 实测].
"""
from __future__ import annotations

import asyncio
import os
import socket
import uuid

from providers.qwen_llm import QwenClient                    # ONE shared client → vLLM continuous batching pools all calls
from ..storage import queue as q
from . import seed                                            # rederive_hubs_for_company — the updatable-hubs hook
from .scan import scan_unit

_WORKERS = int(os.environ.get("EVENTINC_WORKERS", "4"))       # async tasks in THIS process (VLM-I/O overlap; render serializes)
_BACKOFF_S = int(os.environ.get("EVENTINC_BACKOFF_S", "5"))  # nothing due → sleep this long before re-polling
_MAX_ROUNDS = int(os.environ.get("EVENTINC_MAX_ROUNDS", "0"))   # 0 = run forever; >0 = exit after N empty polls (for tests)
_RUN_ID = os.environ.get("WATEREVENTS_RUN_ID", "eventinc")    # run tag the hub re-derivation reads events under (matches scan.py)
_TOP_K = int(os.environ.get("EVENTINC_TOP_K", "3"))          # hubs kept per company when a full run re-derives them


async def _worker(idx: int, pool, client) -> None:
    wid = f"{socket.gethostname()}:{os.getpid()}:{idx}:{uuid.uuid4().hex[:4]}"
    empties = 0
    while True:
        # OMNIPOTENT worker: type_filter=None → claim ANY due unit; the priority ladder (incremental pri 10 < full 100)
        # makes it grab a due incremental first, else the oldest-due full. No reserved slots. {USER 2026-07-26 "omnipotent"}.
        unit = await q.claim_work(pool, wid, type_filter=None)
        if unit is None:                                     # nothing due at all → back off (don't hot-spin the DB)
            empties += 1
            if _MAX_ROUNDS and empties >= _MAX_ROUNDS:
                return
            await asyncio.sleep(_BACKOFF_S)
            continue
        empties = 0
        try:
            s = await scan_unit(pool, client, unit["url"], unit["type"], unit["company_id"])   # stats dict (events + resource usage)
            await q.complete_work(pool, unit["id"], unit["type"], event_count=s["events"],     # self re-arm (full +7d / inc +T*)
                                  duration_s=s["duration_s"], render_pages=s["render_pages"], vlm_calls=s["vlm_calls"])
            # UPDATABLE HUBS — a full BFS may have surfaced new event-listing pages for this company → re-derive its hubs and
            # UPSERT them into the incremental queue so they start being monitored (idempotent; existing hubs no-op). Only
            # after full (incremental scans a single known hub, discovers no new ones). {USER 2026-07-26 "hubs is updatable"}.
            if unit["type"] == "full" and unit["company_id"]:
                added = await seed.rederive_hubs_for_company(pool, unit["company_id"], _RUN_ID, _TOP_K)
                if added:
                    print(f"[eventinc] {wid[-4:]} rederived {added} hub(s) from full {unit['url'][:40]}", flush=True)
            # log the resource split so a tail -f shows the hash-gate working (skips climb as hubs stabilize → VLM demand drops)
            print(f"[eventinc] {wid[-4:]} {unit['type']:11} {unit['url'][:48]} → {s['events']} ev "
                  f"({s['vlm_calls']} vlm, {s['vlm_skipped']} skip, {s['duration_s']}s)", flush=True)
        except Exception as e:                               # noqa: BLE001 — one unit must not kill the worker
            await q.fail_work(pool, unit["id"], unit["type"])   # backoff-retry, or 'failed' at the attempt cap (fail-loud)
            print(f"[eventinc] {wid[-4:]} ✗ FAIL {unit['url'][:52]}: {type(e).__name__}: {str(e)[:90]}", flush=True)


async def main() -> None:
    pool = await q.connect_pool(min_size=2, max_size=_WORKERS + 2)
    client = QwenClient()
    print(f"[eventinc] {_WORKERS} OMNIPOTENT workers (one shared pool, priority ladder) on {socket.gethostname()}:{os.getpid()}", flush=True)
    # every worker is identical + omnipotent: claims any due unit, priority → incremental first else full. No reserved slots.
    tasks = [asyncio.create_task(_worker(i, pool, client)) for i in range(_WORKERS)]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
