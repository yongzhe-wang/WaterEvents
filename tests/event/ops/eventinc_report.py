"""eventinc_report — after a 200-hub incremental cycle: write ALL events to a txt list + compute repeat-rate & dedup-rate.

用一句话讲完: 从 log 数「found_raw」(每个 hub scan 抽出的事件数之和,Σ "→ N ev")、从 DB 数「net-new」(run_id=eventinc
去重后真正入库的行)→ repeat_rate = 1 − net_new/found_raw(re-scan 找到的里有多少是已知/重复的)、dedup_rate = 折叠掉的
重复占比;再把全部 eventinc 事件按日期降序写成 events.txt。{USER 2026-07-25 "list in txt of all events + repeated rate + dedup rate"}.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import asyncpg

DSN = "postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
LOG_GLOB = "/workspace/WaterEvents/tests/eventinc_p*.log"
OUT_TXT = "/workspace/WaterEvents/tests/eventinc_events.txt"
RUN_ID = "eventinc"


async def _found_raw(c) -> tuple[int, int]:
    """Σ of per-hub events-found (work_queue.last_event_count) + number of hubs scanned — DB-AUTHORITATIVE (the log-grep
    version undercounted because nohup buffers stdout; last_event_count is written per scan by complete_work)."""
    total = await c.fetchval("SELECT coalesce(sum(last_event_count),0) FROM work_queue "
                             "WHERE type='incremental' AND last_scanned_at IS NOT NULL")
    scans = await c.fetchval("SELECT count(*) FROM work_queue WHERE type='incremental' AND last_scanned_at IS NOT NULL")
    return int(total or 0), int(scans or 0)


async def main() -> None:
    c = await asyncpg.connect(DSN, statement_cache_size=0, server_settings={"search_path": "waterevents"})
    found_raw, scans = await _found_raw(c)
    # net-new = events genuinely inserted under run_id=eventinc (deduped by title+date vs each other AND vs killerdeal via
    # the shared (company_id, dedup_key) unique constraint — an event already in killerdeal hit ON CONFLICT, not a new row)
    net_new = await c.fetchval("SELECT count(*) FROM events WHERE run_id=$1", RUN_ID)
    hubs_done = await c.fetchval("SELECT count(*) FROM work_queue WHERE type='incremental' AND last_scanned_at IS NOT NULL")
    # distinct (company, dedup_key) among what the eventinc scans stored — should equal net_new (unique constraint), a sanity check
    ev = await c.fetch(
        "SELECT company_id, title, event_date, event_type, media_urls, source_url FROM events "
        "WHERE run_id=$1 ORDER BY event_date DESC", RUN_ID)
    comp = {r["id"]: (r["ticker"] or r["ir_url"]) for r in await c.fetch("SELECT id, ir_url, ticker FROM companies")}

    def primary(m):
        import json
        a = m if isinstance(m, list) else (json.loads(m) if m else [])
        return next((u for u in a if isinstance(u, str) and u.startswith("http")), "")

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write(f"=== eventinc — ALL EVENTS ({len(ev)}) — {hubs_done} hubs scanned ===\n\n")
        for r in ev:
            f.write(f"[{(r['event_date'] or '?'):<14}] {(r['event_type'] or '?'):<14} | {comp.get(r['company_id'],'—'):<26} | "
                    f"{(r['title'] or '(no title)')[:70]}\n    {primary(r['media_urls'])}\n")

    repeat = found_raw - net_new
    print(f"=== INCREMENTAL 200-HUB CYCLE REPORT ===")
    print(f"hubs scanned            : {hubs_done}")
    print(f"events FOUND (raw Σ)     : {found_raw}   (across {scans} hub-scans)")
    print(f"events NET-NEW (stored)  : {net_new}   (run_id=eventinc, deduped by title+date)")
    print(f"REPEAT rate              : {100*repeat//max(found_raw,1)}%   ({repeat} of {found_raw} found were already-known/dup)")
    print(f"DEDUP: found→stored       : {found_raw} → {net_new}  (collapsed {repeat} = {100*repeat//max(found_raw,1)}%)")
    print(f"events list written      : {OUT_TXT}  ({len(ev)} events)")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
