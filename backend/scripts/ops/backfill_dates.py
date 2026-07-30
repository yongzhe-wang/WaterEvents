"""backfill_dates — bring the already-stored non-ISO event dates onto the same normalisation the extractor now applies.

用一句话讲完: 库里有 3,090 行的 event_date 是非 ISO 形状(`2011-08-17T13:00:00` / `01/04/2019` / `2003-H1` …), 归一化
上线后同一个事件会算出不同的 dedup_key, 于是下次扫到同一家公司时它会被 INSERT 成新行而不是 UPDATE 老行 —— 这个脚本
用**和抽取路径完全同一个函数**把老行也归一化, 顺便把归一化后撞到一起的重复行合并掉。

WHY it imports _norm_date_shape instead of reimplementing the rules in SQL: two implementations of the same policy
drift, and the drift is silent — the code would normalise one way and the backfill another, leaving rows that look
migrated and still miss on ON CONFLICT. There is exactly one normaliser and both callers use it.
{DB 2026-07-30 "3,090 rows hold a shape outside the four permitted ones; same histogram as the 853 dropped events"}
[CONFIDENCE: CONFIRMED 100% — the shape counts and the collision behaviour below were measured against the live table.]

A MERGE, not just an UPDATE. Normalising `2011-08-17T13:00:00` to `2011-08-17` can collide with a row that already
holds `2011-08-17` for the same company — and those two rows ARE the same event, recorded twice because the format
differed. So a collision is not an error to route around; it is the duplicate this change exists to collapse. The
surviving row keeps the union of both media_urls and the first non-empty source_url, matching what flush_events'
ON CONFLICT already does, so a merged row is indistinguishable from one the crawler wrote itself.

DRY RUN BY DEFAULT. Prints the plan and changes nothing without --apply, same convention as queue_boost.py.

Usage:
  python backend/scripts/ops/backfill_dates.py            # report only
  python backend/scripts/ops/backfill_dates.py --apply    # write
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import asyncpg  # noqa: E402

from agent.event_agent.crawl.extract import _norm_date_shape  # noqa: E402 — THE one normaliser; never a second copy
from agent.event_agent.storage.urls import _event_key         # noqa: E402 — THE one key builder, same reason

_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")

# The four permitted shapes, as a SQL predicate. Rows already matching are untouched, so a re-run is a no-op and the
# script is safe to leave in a cron if that is ever wanted.
_ISO_OK = (
    "(event_date IS NULL OR event_date = '' "
    " OR event_date ~ '^\\d{4}$' OR event_date ~ '^\\d{4}-\\d{2}$' "
    " OR event_date ~ '^\\d{4}-[Qq][1-4]$' OR event_date ~ '^\\d{4}-\\d{2}-\\d{2}$')"
)


async def main() -> int:
    apply = "--apply" in sys.argv
    if not _DSN:
        print("WATEREVENTS_DB_DSN not set — point it at the Supabase Supavisor pooler (port 6543).", file=sys.stderr)
        return 78
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2, statement_cache_size=0,
                                     server_settings={"search_path": "waterevents"})
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id, company_id, title, event_date, dedup_key, media_urls, source_url "
                f"FROM events WHERE NOT {_ISO_OK} ORDER BY company_id, id")
            print(f"[backfill] {len(rows)} row(s) hold a non-permitted date shape")

            plan, unreadable = [], []
            for r in rows:
                new_date = _norm_date_shape(r["event_date"])
                if not new_date:
                    unreadable.append(r)                   # keep it: dropping stored data is not this script's job
                    continue
                new_key = _event_key(r["title"], new_date, [])
                if new_date == r["event_date"] and new_key == r["dedup_key"]:
                    continue
                plan.append((r, new_date, new_key))

            # Which of the planned keys already exist for that company → those are merges, not updates.
            merges = 0
            for r, new_date, new_key in plan:
                hit = await conn.fetchval(
                    "SELECT id FROM events WHERE company_id=$1 AND dedup_key=$2 AND id<>$3",
                    r["company_id"], new_key, r["id"])
                if hit:
                    merges += 1

            print(f"[backfill] {len(plan)} to rewrite · {merges} of those collide with an existing row (a MERGE) · "
                  f"{len(unreadable)} unreadable, left as-is")
            for r, nd, nk in plan[:10]:
                print(f"    {r['event_date']!r:<30} -> {nd!r:<12} {str(r['title'])[:40]!r}")
            if not apply:
                print("[backfill] DRY RUN — nothing written. Re-run with --apply.")
                return 0

            done = merged = 0
            for r, new_date, new_key in plan:
                async with conn.transaction():
                    keep = await conn.fetchrow(
                        "SELECT id, media_urls, source_url FROM events "
                        "WHERE company_id=$1 AND dedup_key=$2 AND id<>$3 FOR UPDATE",
                        r["company_id"], new_key, r["id"])
                    if keep:
                        # Same event, two rows. Fold this row's media into the survivor, then delete this one — exactly
                        # the union flush_events' ON CONFLICT performs, so the result matches a crawler-written row.
                        await conn.execute(
                            "UPDATE events SET media_urls = ("
                            "  SELECT coalesce(jsonb_agg(DISTINCT u), '[]'::jsonb)"
                            "  FROM jsonb_array_elements(media_urls || $2::jsonb) AS u), "
                            "  source_url = COALESCE(NULLIF(source_url,''), NULLIF($3,'')) "
                            "WHERE id = $1", keep["id"], r["media_urls"], r["source_url"])
                        await conn.execute("DELETE FROM events WHERE id = $1", r["id"])
                        merged += 1
                    else:
                        await conn.execute("UPDATE events SET event_date=$2, dedup_key=$3 WHERE id=$1",
                                           r["id"], new_date, new_key)
                        done += 1
            print(f"[backfill] APPLIED — {done} rewritten, {merged} merged into an existing row")
            return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
