"""queue_boost — reorder the crawl queue by hand, safely, while the fleet keeps running.

用一句话讲完: claim_work 是 `ORDER BY priority ASC, due_at ASC` 领活的,所以「插队」= 改 priority + 把 due_at 拉到
现在;本脚本只碰 `status='queued'` 的行,正在跑的 running 行一个都不动,因此 fleet 完全无感。改之前先把旧值写成
TSV 快照,--undo 可以整批还原。

WHY this exists: we know which companies are under-fetched (the 2026-07-27 investigation found 409 companies with
<10 events, 122 of them with zero), but full units are all priority 100 and are claimed in essentially arbitrary
due_at order, so a known-broken company waits behind ~2,400 healthy ones. This makes "scan these next" a one-liner.

PRIORITY BANDS (no schema change — the band IS the marker):
    0    jump everything, including incremental — use for one or two companies you want answered NOW
    10   incremental (untouched)
    50   default boost: first among full, but still yields to incremental so the rotation never stalls
    100  full default (untouched)
"boosted" == `type='full' AND priority < 100`; undo == set back to 100.

    python -m scripts.ops.queue_boost --max-events 10 --never-full            # preview (dry-run is the default)
    python -m scripts.ops.queue_boost --max-events 10 --never-full --apply
    python -m scripts.ops.queue_boost --tickers WMT,UNH,RELX --priority 0 --apply
    python -m scripts.ops.queue_boost --status
    python -m scripts.ops.queue_boost --undo --apply
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone

import asyncpg

_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")
_SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")
_SNAP_DIR = os.environ.get("QUEUE_BOOST_SNAPSHOT_DIR", "/tmp")
_MAX_LIMIT = 2000                                   # hard ceiling: a typo must not re-prioritise the entire queue
# MUST track queue._FULL_INTERVAL_S — it defines the weekly window a boost is forbidden to reopen (see cmd_boost).
_FULL_INTERVAL_S = int(os.environ.get("EVENTINC_FULL_INTERVAL_S", str(7 * 24 * 3600)))


async def _pool() -> asyncpg.Pool:
    # statement_cache_size=0 is REQUIRED on the Supavisor transaction pooler (it rotates backends per transaction, so
    # server-side prepared statements never survive). {EVENTS.PY module docstring} [CONFIDENCE: CONFIRMED 100%].
    if not _DSN:
        sys.exit("WATEREVENTS_DB_DSN is unset — source /etc/waterevents.env or backend/deploy/launch_fleet.sh")
    return await asyncpg.create_pool(_DSN, min_size=1, max_size=3, statement_cache_size=0,
                                     server_settings={"search_path": _SCHEMA})


def _where(a) -> tuple[str, list]:
    """Build the row filter shared by preview and apply, so the two can NEVER diverge (a preview that selects a
    different set than the apply is worse than no preview at all)."""
    # status='queued' is the safety property, not an optimisation: a 'running' row is mid-scan and owned by a worker
    # whose lease we must not disturb. Restricting here is what lets this run against a live fleet.
    sql = ["w.type = 'full'", "w.status = 'queued'"]
    args: list = []
    if a.never_full:
        sql.append("w.last_scanned_at IS NULL")
    if a.tickers:
        args.append([t.strip().upper() for t in a.tickers.split(",") if t.strip()])
        sql.append(f"c.ticker = ANY(${len(args)}::text[])")
    if a.max_events is not None:
        args.append(a.max_events)
        sql.append(f"COALESCE(ev.n, 0) < ${len(args)}")
    return " AND ".join(sql), args


_BASE = """
FROM work_queue w
JOIN companies c ON c.id = w.company_id
LEFT JOIN (SELECT company_id, count(*) n FROM events GROUP BY 1) ev ON ev.company_id = w.company_id
WHERE {where}
"""


async def cmd_boost(pool, a) -> None:
    where, args = _where(a)
    limit = min(a.limit, _MAX_LIMIT)
    q = f"""SELECT w.id, c.ticker, w.priority AS old_priority, COALESCE(ev.n,0) AS events, w.url
            {_BASE.format(where=where)}
            ORDER BY COALESCE(ev.n,0) ASC, c.ticker ASC LIMIT {limit}"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(q, *args)
    if not rows:
        print("no matching queued full units — nothing to do")
        return
    zero = sum(1 for r in rows if r["events"] == 0)
    print(f"matched {len(rows)} queued full unit(s) — {zero} with ZERO events, "
          f"avg {sum(r['events'] for r in rows)/len(rows):.1f} events")
    for r in rows[:12]:
        print(f"   {r['ticker'] or '—':<10} events={r['events']:<4} pri {r['old_priority']} → {a.priority}  {r['url'][:56]}")
    if len(rows) > 12:
        print(f"   … and {len(rows)-12} more")
    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return
    # snapshot BEFORE mutating: id + the exact prior values, so --undo restores rather than guesses
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(_SNAP_DIR, f"queue_boost_{stamp}.tsv")
    with open(snap, "w", newline="") as fh:
        wtr = csv.writer(fh, delimiter="\t")
        wtr.writerow(["id", "ticker", "old_priority", "events_before"])
        for r in rows:
            wtr.writerow([r["id"], r["ticker"] or "", r["old_priority"], r["events"]])
    ids = [r["id"] for r in rows]
    async with pool.acquire() as conn:
        # A boost changes the ORDER a unit is claimed in — it must NEVER make a unit eligible again inside its own
        # week. complete_work re-arms full to last_scan + _FULL_INTERVAL_S (7d), so pulling due_at to now() on a unit
        # already crawled this week forces a duplicate full BFS: ~400s of render plus a fresh VLM pass for data we
        # already hold. An unconditional due_at=now() did exactly that on 2026-07-28 — 21 units were made due again
        # and 7 were re-crawled before it was caught. So advance due_at ONLY when the weekly window has elapsed;
        # otherwise leave the slot alone and let the higher priority put the unit at the FRONT of next week's sweep.
        # {QUEUE.PY:19 "_FULL_INTERVAL_S = INT(OS.ENVIRON.GET("EVENTINC_FULL_INTERVAL_S", STR(7 * 24 * 3600)))"}
        # {USER 2026-07-28 "EVENTS IF FETCHED THIS WEEK WILL NEVER REFETCH THIS WEEK, EVEN IF PRIORITY IS HIGH BUT
        #  LEAVE TO FIRST AS NEXT WEEK"} [CONFIDENCE: CONFIRMED 100% — direct instruction + 7 observed duplicates].
        n = await conn.execute(
            "UPDATE work_queue SET priority=$1, updated_at=now(), "
            "       due_at = CASE WHEN last_scanned_at IS NULL "
            "                       OR last_scanned_at <= now() - make_interval(secs => $3) "
            "                     THEN now() ELSE due_at END "
            "WHERE id = ANY($2::uuid[]) AND status='queued'",
            a.priority, ids, float(_FULL_INTERVAL_S))
    print(f"\n{n} — snapshot {snap}")
    print("note: units already crawled inside the current weekly window keep their slot — the boost puts them at "
          "the front of NEXT week's sweep instead of re-crawling them now.")


async def cmd_undo(pool, a) -> None:
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM work_queue WHERE type='full' AND priority < 100")
        print(f"{n} boosted full unit(s) currently at priority < 100")
        if not n or not a.apply:
            print("DRY RUN — nothing written. Re-run with --apply." if n else "")
            return
        res = await conn.execute("UPDATE work_queue SET priority=100, updated_at=now() "
                                 "WHERE type='full' AND priority < 100")
    print(f"{res} — all boosted units returned to the full default (100)")


async def cmd_status(pool, _a) -> None:
    """The debugging loop: what did boosting actually buy us? events NOW vs at boost time, per boosted company."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.ticker, w.priority, w.status, w.last_scanned_at, w.last_event_count,
                   (SELECT count(*) FROM events e WHERE e.company_id = w.company_id) AS events_now
            FROM work_queue w JOIN companies c ON c.id = w.company_id
            WHERE w.type='full' AND w.priority < 100
            ORDER BY (w.last_scanned_at IS NULL), w.last_scanned_at DESC NULLS LAST LIMIT 40""")
        agg = await conn.fetchrow("""
            SELECT count(*) total, count(*) FILTER (WHERE last_scanned_at IS NOT NULL) scanned
            FROM work_queue WHERE type='full' AND priority < 100""")
    if not agg or not agg["total"]:
        print("nothing is boosted right now")
        return
    print(f"boosted: {agg['total']}   already re-scanned: {agg['scanned']}   pending: {agg['total']-agg['scanned']}\n")
    print(f"{'ticker':<10}{'pri':>5}{'status':>10}{'events_now':>12}{'last_scan_ev':>14}  last_scanned_at")
    for r in rows:
        print(f"{r['ticker'] or '—':<10}{r['priority']:>5}{r['status']:>10}{r['events_now']:>12}"
              f"{(r['last_event_count'] if r['last_event_count'] is not None else '—'):>14}  "
              f"{r['last_scanned_at'] or 'not yet'}")


def main() -> None:
    p = argparse.ArgumentParser(description="reorder the crawl queue by hand (safe against a live fleet)")
    p.add_argument("--tickers", help="comma-separated tickers to boost")
    p.add_argument("--max-events", type=int, help="only companies with fewer than N events")
    p.add_argument("--never-full", action="store_true", help="only units whose full BFS has never run")
    p.add_argument("--priority", type=int, default=50, help="0 jumps incremental too; 50 (default) does not")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    p.add_argument("--undo", action="store_true", help="reset every boosted full unit back to priority 100")
    p.add_argument("--status", action="store_true", help="show boosted units and what they have scanned so far")
    a = p.parse_args()
    if not (a.undo or a.status) and not (a.tickers or a.max_events is not None or a.never_full):
        p.error("give at least one selector (--tickers / --max-events / --never-full), or use --status / --undo")

    async def run():
        pool = await _pool()
        try:
            await (cmd_status(pool, a) if a.status else cmd_undo(pool, a) if a.undo else cmd_boost(pool, a))
        finally:
            await pool.close()
    asyncio.run(run())


if __name__ == "__main__":
    main()
