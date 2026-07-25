"""Enqueue the sample_100 companies into waterevents.companies for the multi-process worker fleet (run_id=killerdeal).

用一句话讲完: 读 sample_100 manifest 的 100 个 clean ir_url → 清掉 run_id=killerdeal 的旧 events(混合旧代码产出,按
「archive & restart on code change」硬规矩重来)→ 把 100 家 upsert 成 status='queued'(worker 才认领)→ 打印 queue 计数。
这样 deploy/supervise_workers.sh 起的 N 个绑核 worker 进程能从队列 SKIP-LOCKED 各抢一片,真·多进程并行。
{USER 2026-07-24 "yes" — 授权多进程重跑 + 清旧} [CONFIDENCE: CONFIRMED — 直接指令 + memory「archive & restart on code change」].

Run ON THE POD:  PYTHONPATH=/workspace/WaterEvents WATEREVENTS_DB_DSN=<dsn> /root/venv/bin/python tests/enqueue_killerdeal.py
"""
from __future__ import annotations

import asyncio
import json
import os

import asyncpg

DSN = os.environ["WATEREVENTS_DB_DSN"]                       # transaction pooler DSN (statement_cache_size=0)
SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")
RUN_ID = os.environ.get("KD_RUN_ID", "killerdeal")
MANIFEST = "/workspace/WaterEvents/tests/ir_official_variants/sample_100/manifest.json"


async def main() -> None:
    urls = [p["url"] for p in (json.load(open(MANIFEST, encoding="utf-8")).get("pages") or [])]
    c = await asyncpg.connect(DSN, statement_cache_size=0, server_settings={"search_path": SCHEMA})

    # 1) CLEAR the old killerdeal events — they were produced by MIXED buggy code (pre-browser-fix curl shells,
    #    pre-grounding hallucinations). Per the "archive & restart on code change" rule, a code change → results from
    #    scratch. run_id-scoped so ONLY this test run's rows are touched. {memory archive-and-restart-on-code-change}.
    ev_before = await c.fetchval("SELECT count(*) FROM events WHERE run_id=$1", RUN_ID)
    await c.execute("DELETE FROM events WHERE run_id=$1", RUN_ID)

    # 2) UPSERT the 100 seeds to status='queued'. ir_url has no unique constraint (per the schema) → UPDATE-then-INSERT
    #    per url: reset any existing killerdeal row (the 8 stuck 'discovering') back to 'queued', insert the rest fresh.
    reset, inserted = 0, 0
    for u in urls:
        r = await c.execute("UPDATE companies SET status='queued', lease_until=NULL, lease_owner=NULL "
                            "WHERE ir_url=$1 AND run_id=$2", u, RUN_ID)
        if r.endswith(" 0"):                                 # no existing killerdeal row for this url → insert one
            await c.execute("INSERT INTO companies (ir_url, status, run_id) VALUES ($1, 'queued', $2)", u, RUN_ID)
            inserted += 1
        else:
            reset += 1

    queued = await c.fetchval("SELECT count(*) FROM companies WHERE run_id=$1 AND status='queued'", RUN_ID)
    print(f"[enqueue] cleared {ev_before} old events | reset {reset} + inserted {inserted} = {queued} companies QUEUED (run_id={RUN_ID})", flush=True)
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
