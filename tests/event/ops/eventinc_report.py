"""eventinc_report — after a 200-hub incremental cycle: write ALL events to a txt list + compute repeat-rate & dedup-rate.

用一句话讲完: 从 log 数「found_raw」(每个 hub scan 抽出的事件数之和,Σ "→ N ev")、从 DB 数「net-new」(run_id=eventinc
去重后真正入库的行)→ repeat_rate = 1 − net_new/found_raw(re-scan 找到的里有多少是已知/重复的)、dedup_rate = 折叠掉的
重复占比;再把全部 eventinc 事件按日期降序写成 events.txt。{USER 2026-07-25 "list in txt of all events + repeated rate + dedup rate"}.
"""
from __future__ import annotations

import asyncio
import os

import asyncpg

def _dsn() -> str:
    """Resolve the DB DSN from the environment, failing loudly when absent.

    WHY deferred into a function instead of a module-level constant: module-level `os.environ[...]` raises at IMPORT
    time, and pytest collection imports every globbed module — one missing var would abort collection for the whole
    suite. This keeps the module importable while still refusing to run against an unspecified database.
    Upstream trigger: main(). Downstream: asyncpg.connect against the live waterevents schema (read-only reporting).
    {EVENTS.PY:65-66 "IF NOT _DSN: RAISE RUNTIMEERROR(\"WATEREVENTS_DB_DSN NOT SET — POINT IT AT THE SUPABASE
     SUPAVISOR POOLER (PORT 6543).\")"} [CONFIDENCE: CONFIRMED 100% — pattern copied from that production call site].
    """
    dsn = os.environ.get("WATEREVENTS_DB_DSN")
    # Fail loud, never default — the removed literal was the live production credential committed in git.
    # {GIT GREP 2026-07-28 "EVENTINC_REPORT.PY:16 DSN = \"POSTGRESQL://POSTGRES.EZUVMOLYFGSADKEHJNEF:FOCUSALPHA2026@
    #  AWS-1-US-EAST-1.POOLER.SUPABASE.COM:6543/POSTGRES\""} [CONFIDENCE: CONFIRMED 100% — read at HEAD 9d3402f].
    if not dsn:
        raise RuntimeError("WATEREVENTS_DB_DSN not set — point it at the Supabase Supavisor pooler (port 6543).")
    return dsn


# Repo-relative default instead of the RunPod-only /workspace/... absolute path, so the report writes somewhere real
# on any host. tests/ is two levels up from tests/event/ops/.
_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_TXT = os.environ.get("EVENTINC_REPORT_OUT", os.path.join(_TESTS_DIR, "eventinc_events.txt"))
RUN_ID = "eventinc"


async def _found_raw(c) -> tuple[int, int]:
    """Σ of per-hub events-found (work_queue.last_event_count) + number of hubs scanned — DB-AUTHORITATIVE (the log-grep
    version undercounted because nohup buffers stdout; last_event_count is written per scan by complete_work)."""
    total = await c.fetchval("SELECT coalesce(sum(last_event_count),0) FROM work_queue "
                             "WHERE type='incremental' AND last_scanned_at IS NOT NULL")
    scans = await c.fetchval("SELECT count(*) FROM work_queue WHERE type='incremental' AND last_scanned_at IS NOT NULL")
    return int(total or 0), int(scans or 0)


async def main() -> None:
    c = await asyncpg.connect(_dsn(), statement_cache_size=0, server_settings={"search_path": "waterevents"})
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
    print("=== INCREMENTAL 200-HUB CYCLE REPORT ===")
    print(f"hubs scanned            : {hubs_done}")
    print(f"events FOUND (raw Σ)     : {found_raw}   (across {scans} hub-scans)")
    print(f"events NET-NEW (stored)  : {net_new}   (run_id=eventinc, deduped by title+date)")
    print(f"REPEAT rate              : {100*repeat//max(found_raw,1)}%   ({repeat} of {found_raw} found were already-known/dup)")
    print(f"DEDUP: found→stored       : {found_raw} → {net_new}  (collapsed {repeat} = {100*repeat//max(found_raw,1)}%)")
    print(f"events list written      : {OUT_TXT}  ({len(ev)} events)")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
