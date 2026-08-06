"""tests/media/enqueue_events.py — push a chosen set of events to the FRONT of the stage-2 queue.

用一句话讲完: 从 dataset 里读出每个事件的 `meta.db_event_id` → 把这些行的 `enrich_priority` 设高、状态清回
`discovered` → worker 下一轮就先领这批 → 在 Today · Media 页上马上能看见它们跑出来。**这是"用真 worker 跑指定 100 条"
的入口**,和 media_dispatch_run.py 的区别是:那个只在内存里跑、不写库、看不见;这个走完整生产路径,产出进数据库、上 UI。

WHY this exists at all: every `discovered` row ties for first place in the claim order, because the tiebreaker is
`next_retry_at` and they are all NULL —
  {EVENTS.PY CLAIM_EVENTS "ORDER BY ENRICH_PRIORITY DESC, NEXT_RETRY_AT NULLS FIRST"}
  {psql 2026-08-06 "DISCOVERED | 280774"}
— so without a priority column, "run these 100 next" is not expressible: the worker would claim 16 arbitrary rows out
of 280,774 and the chosen set might not surface for hours.
[CONFIDENCE: CONFIRMED 100% — the ORDER BY and the row count were read off the live database.]

Run (on the VM, where the DSN lives):
  set -a; . /etc/waterevents/fleet.env; set +a
  cd ~/WaterEvents && PYTHONPATH=$HOME/WaterEvents/backend \\
    ~/venv/bin/python tests/media/enqueue_events.py tests/datasets/media_100

  --clear   reset every other row's priority to 0 first (so an earlier batch does not stay ahead of this one)
  --prio N  priority to set (default 100)
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys

sys.path.insert(0, os.environ.get("PYTHONPATH", "").split(":")[0] or ".")

from agent.event_agent.storage import events as db     # noqa: E402 — path shim must run first


def dataset_ids(ds: str) -> list[str]:
    """Every `meta.db_event_id` in a dataset folder, in file order.

    Reads the id the dataset RECORDED when it was built, rather than re-deriving one from the url: the dataset's whole
    purpose is to point at specific production rows, and a re-derived key would silently drift if canonicalisation ever
    changed. A file without the field is skipped loudly — a silent skip would make the batch quietly smaller than asked.
    """
    ids: list[str] = []
    for f in sorted(glob.glob(os.path.join(ds, "ev*.json"))):
        meta = (json.load(open(f, encoding="utf-8")).get("meta") or {})
        eid = meta.get("db_event_id")
        if eid:
            ids.append(eid)
        else:
            print(f"  ⚠️ {os.path.basename(f)} has no meta.db_event_id — not enqueued", flush=True)
    return ids


async def main() -> None:
    ds = sys.argv[1] if len(sys.argv) > 1 else "tests/datasets/media_100"
    prio = int(os.environ.get("PRIO", "100"))
    clear = "--clear" in sys.argv

    ids = dataset_ids(ds)
    if not ids:
        print(f"⛔ no db_event_id found under {ds} — nothing to enqueue", flush=True)
        return

    pool = await db.connect_pool()
    try:
        async with pool.acquire() as conn:
            if clear:
                # Drop every other batch back to 0 so THIS set is unambiguously first. Without it, a previous run's
                # events would still outrank the fresh queue and the two batches would interleave.
                n = await conn.execute("UPDATE events SET enrich_priority = 0 WHERE enrich_priority <> 0;")
                print(f"  cleared previous priorities: {n}", flush=True)

            # Reset the stage-2 state alongside the priority. A row left at `enriched` would never be re-claimed, and a
            # row left at `failed` carries a backoff that would hold back the very batch being prioritised — so a
            # re-run of a chosen set starts from a clean slate by construction.
            res = await conn.execute(
                """
                UPDATE events
                   SET enrich_priority = $2, status = 'discovered', claim_token = NULL, lease_until = NULL,
                       fail_reason = NULL, fail_count = 0, next_retry_at = NULL, enriched_at = NULL
                 WHERE id = ANY($1::uuid[]);
                """,
                ids, prio)
            print(f"  enqueued {len(ids)} events at priority {prio}: {res}", flush=True)

            # Report what the worker will actually see next, so a mismatch (ids that are not in this database) is
            # visible here rather than as a mysteriously short run.
            rows = await conn.fetch(
                "SELECT status, count(*) AS n FROM events WHERE id = ANY($1::uuid[]) GROUP BY status;", ids)
            for r in rows:
                print(f"    {r['status']:12} {r['n']}", flush=True)
            missing = len(ids) - sum(r["n"] for r in rows)
            if missing:
                print(f"  ⚠️ {missing} of the dataset's ids are NOT in this database "
                      f"(dataset built against a different corpus?)", flush=True)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
