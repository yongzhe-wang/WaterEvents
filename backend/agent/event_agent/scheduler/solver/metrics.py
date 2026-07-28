"""event_agent.metrics — turn the scan_log + work_queue instrumentation into the FOUR live numbers the packing solver
needs: C_R (render pages/hr), C_V (VLM calls/hr), hit_rate (fraction of rendered pages that changed → actually hit the
VLM), and per-type unit cost (pages/unit, calls/unit, sec/unit).

用一句话讲完: 从 scan_log 取最近 window 小时的行 → Σrender_pages/window = C_R、Σvlm_calls/window = C_V、
vlm_calls/(vlm_calls+vlm_skipped) = hit_rate;从 work_queue 按 type 取 duration_s/last_render_pages/last_vlm_calls 的
均值 = 每个 full/incremental 单元的成本 → 这四组数喂给 packing solver 解出 incremental 最优周期 T*。这层只读、不改状态,
worker 跑着它随时能查。{USER 2026-07-26 "calculate render and vlm usage for parallel; best rotation so full fits perfectly;
estimation of finish time"} [CONFIDENCE: CONFIRMED — 双资源模型 + packing 方程的实测输入层].

Run:  PYTHONPATH=/workspace/WaterEvents/backend python -m agent.event_agent.metrics [window_hours]
"""
from __future__ import annotations

import asyncio
import os
import sys

from ...storage import queue as q


async def throughput(pool, window_h: float = 6.0) -> dict:
    """Fleet throughput over the last `window_h` hours from scan_log. C_R/C_V are Σ(resource)/window_h — an OBSERVED rate
    (bounded by demand when the fleet isn't saturated), which is exactly what the solver wants to detect "are we keeping
    the bottleneck busy". hit_rate = vlm_calls/(vlm_calls+vlm_skipped) = fraction of rendered pages that CHANGED → the knob
    that sets VLM demand. Empty window → zeros (solver treats as "no data, hold current T"). {PLAN §finish-time: rate =
    Σ over window ÷ window-hours} [CONFIDENCE: CONFIRMED — Little's Law throughput is count/time]."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT coalesce(sum(render_pages),0) AS rp,
                   coalesce(sum(vlm_calls),0)    AS vc,
                   coalesce(sum(vlm_skipped),0)  AS vs,
                   coalesce(sum(events),0)       AS ev,
                   count(*)                       AS n
            FROM scan_log WHERE ts > now() - ($1 || ' hours')::interval;
            """,
            str(window_h),
        )
    rp, vc, vs = int(row["rp"]), int(row["vc"]), int(row["vs"])
    denom = vc + vs                                          # rendered pages that reached the gate (call OR skip)
    return {
        "window_h": window_h,
        "scans": int(row["n"]),
        "C_R": rp / window_h if window_h else 0.0,          # pages/hr rendered (observed)
        "C_V": vc / window_h if window_h else 0.0,          # calls/hr sent to VLM (observed)
        "hit_rate": (vc / denom) if denom else 1.0,          # changed-fraction; no data → assume 1.0 (worst case = extract all)
        "render_pages": rp, "vlm_calls": vc, "vlm_skipped": vs, "events": int(row["ev"]),
    }


async def unit_cost(pool) -> dict:
    """Per-type unit cost from work_queue's last-scan columns (avg over rows that have completed at least once). full and
    incremental cost ~20× apart (BFS 40 pages vs 1), so the solver MUST size them separately. sec/unit → finish-time ETA;
    pages/unit + calls/unit → per-cycle render/VLM demand. {PLAN §finish-time EWMA per type}."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT type,
                   count(*)                       AS scanned,
                   avg(duration_s)                AS sec,
                   avg(last_render_pages)         AS pages,
                   avg(last_vlm_calls)            AS calls,
                   avg(last_event_count)          AS events
            FROM work_queue WHERE last_scanned_at IS NOT NULL
            GROUP BY type;
            """
        )
    out: dict = {}
    for r in rows:                                          # one entry per type present (full / incremental)
        out[r["type"]] = {
            "scanned": int(r["scanned"]),
            "sec_per_unit": float(r["sec"]) if r["sec"] is not None else None,
            "pages_per_unit": float(r["pages"]) if r["pages"] is not None else None,
            "calls_per_unit": float(r["calls"]) if r["calls"] is not None else None,
            "events_per_unit": float(r["events"]) if r["events"] is not None else None,
        }
    return out


async def queue_depth(pool) -> dict:
    """How many units of each type are pending (the solver's backlog = the numerator in ETA = backlog / throughput)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT type, status, count(*) n FROM work_queue GROUP BY 1,2")
    out: dict = {}
    for r in rows:                                          # nest {type: {status: n}} for the dashboard + solver
        out.setdefault(r["type"], {})[r["status"]] = int(r["n"])
    return out


async def main() -> None:
    window_h = float(sys.argv[1]) if len(sys.argv) > 1 else float(os.environ.get("EVENTINC_METRIC_WINDOW_H", "6"))
    pool = await q.connect_pool()
    tp = await throughput(pool, window_h)
    uc = await unit_cost(pool)
    qd = await queue_depth(pool)
    print(f"=== eventinc metrics (last {window_h}h) ===")
    print(f"throughput : C_R={tp['C_R']:.1f} pages/hr · C_V={tp['C_V']:.1f} calls/hr · hit_rate={tp['hit_rate']*100:.0f}% "
          f"({tp['scans']} scans, {tp['vlm_calls']} calls / {tp['vlm_skipped']} skipped)")
    for t, c in uc.items():
        sec = f"{c['sec_per_unit']:.1f}s" if c["sec_per_unit"] is not None else "—"
        print(f"unit[{t:11}]: {c['scanned']} scanned · {sec}/unit · "
              f"{c['pages_per_unit'] or 0:.1f} pages · {c['calls_per_unit'] or 0:.1f} calls · {c['events_per_unit'] or 0:.1f} ev")
    print(f"queue      : {qd}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
