"""Undo the production-queue corruption my mis-scoped workers caused + clear the wrong events.

用一句话讲完: 我起的 killerdeal worker 用的是不 scoped 的生产 claim(WHERE status='queued',不认 run_id)→ 抢了生产队列里
run_id=NULL 的公司、re-tag 成 killerdeal。这脚本把「killerdeal 里但不在我 sample_100 名单」的公司(= 被误抢的生产公司)
run_id 恢复成 NULL、status 回 queued、清 lease → 生产队列复原;再删掉所有 killerdeal events(都是错公司的)。我的 100 家
killerdeal 行保留(manifest-slice 会直接用)。{USER 2026-07-24 认可清理 + manifest-slice} [CONFIDENCE: CONFIRMED — 逆转自造污染].

Run ON THE POD:  PYTHONPATH=/workspace/WaterEvents/backend WATEREVENTS_DB_DSN=<dsn> /root/venv/bin/python tests/cleanup_killerdeal.py
"""
from __future__ import annotations

import asyncio
import json
import os

import asyncpg

DSN = os.environ["WATEREVENTS_DB_DSN"]
SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")
RUN_ID = os.environ.get("KD_RUN_ID", "killerdeal")
MANIFEST = "/workspace/WaterEvents/tests/ir_official_variants/sample_100/manifest.json"


async def main() -> None:
    my_urls = [p["url"] for p in (json.load(open(MANIFEST, encoding="utf-8")).get("pages") or [])]
    c = await asyncpg.connect(DSN, statement_cache_size=0, server_settings={"search_path": SCHEMA})

    # 1) restore the production companies my workers wrongly re-tagged: killerdeal-tagged BUT not one of my 100 seeds →
    #    they were run_id=NULL production rows, claimed + re-tagged. Put run_id back to NULL, status back to queued.
    restored = await c.execute(
        "UPDATE companies SET run_id=NULL, status='queued', lease_until=NULL, lease_owner=NULL "
        "WHERE run_id=$1 AND ir_url != ALL($2::text[])", RUN_ID, my_urls)

    # 2) delete ALL killerdeal events — every one was flushed for a WRONG (production) company under the mis-scoped claim.
    ev = await c.fetchval("SELECT count(*) FROM events WHERE run_id=$1", RUN_ID)
    await c.execute("DELETE FROM events WHERE run_id=$1", RUN_ID)

    # 3) reset my 100 killerdeal companies to a clean 'queued' (manifest-slice reuses the row by ir_url; keeps them tidy).
    await c.execute("UPDATE companies SET status='queued', lease_until=NULL, lease_owner=NULL "
                    "WHERE run_id=$1 AND ir_url = ANY($2::text[])", RUN_ID, my_urls)

    kd = await c.fetchval("SELECT count(*) FROM companies WHERE run_id=$1", RUN_ID)
    none_q = await c.fetchval("SELECT count(*) FROM companies WHERE run_id IS NULL AND status='queued'")
    print(f"[cleanup] restored {restored} mis-tagged production rows → run_id=NULL/queued | "
          f"deleted {ev} wrong killerdeal events | killerdeal rows now: {kd} | None-queued now: {none_q}", flush=True)
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
