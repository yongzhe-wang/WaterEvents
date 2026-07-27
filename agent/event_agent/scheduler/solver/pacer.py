"""event_agent.pacer — THE packing solver = the "dynamic scheduling algorithm". Every cycle it reads the live
capacity profile + measured throughput/cost, solves the incremental period T* that packs incremental + full into the
week so BOTH render and VLM stay busy but neither overflows, then re-spaces the incremental due_at over T* and publishes
the state to scheduler_state (which complete_work reads for re-arm and the Today page reads for the dashboard).

用一句话讲完: 一周 168h,full 每周固定扫一遍(demand 固定)、incremental 每轮扫 N_hub 个 hub(轮数=168/T);对 render 和
VLM 各写「full demand + (168/T)·每轮 incremental demand = 容量·168」解出 T,取较大的(binding 资源)= T*;然后把所有 queued
incremental 的 due_at 均匀摊到 [now, now+T*] → full 用剩下的容量当 backfill 自动填 → 两个资源同时跑满。换 profile(RunPod→
GCP)→ 容量变 → 下一轮 T* 自动重解。{USER 2026-07-26 "best rotation T* so full fits perfectly; adjust dynamically;
adjustable for GCP; estimation of finish time"} [CONFIDENCE: CONFIRMED — 双资源 packing 方程本 session 推导 + 用户批准].

Run once (solve + print + apply):   PYTHONPATH=/workspace/WaterEvents python -m agent.event_agent.pacer
Run as the resident controller:      PYTHONPATH=/workspace/WaterEvents python -m agent.event_agent.pacer --loop
"""
from __future__ import annotations

import asyncio
import os
import sys

from ...storage import queue as q
from . import metrics
from . import profile as prof

_TICK_S = int(os.environ.get("EVENTINC_PACER_TICK_S", str(3600)))   # re-solve every hour (capacity/hit_rate drift slowly)
_METRIC_WINDOW_H = float(os.environ.get("EVENTINC_METRIC_WINDOW_H", "6"))   # throughput/hit_rate smoothing window
_EPS = 1e-6

# Cold-start per-unit costs when scan_log has no full rows yet (so the FIRST solve isn't a div-by-zero). full ≈ a BFS of
# ~6 pages each extracted — the MEASURED reality (killerdeal pages/company: mean 5.2, median 5, p90 10, max 26, ZERO
# companies hit 40). The old default 40 was ~8× too high → it made the packing solver falsely declare full INFEASIBLE.
# incremental ≈ 1 page, 1 extract. Overridden the moment real full scans land (unit_cost from work_queue.last_render_pages).
# {USER 2026-07-27 "90% of the companies don't even have 40 pages" — measured: 0% reach 40} [CONFIDENCE: CONFIRMED — DB measured].
_DEFAULT_FULL_PAGES = float(os.environ.get("EVENTINC_DEFAULT_FULL_PAGES", "6"))
_DEFAULT_FULL_CALLS = float(os.environ.get("EVENTINC_DEFAULT_FULL_CALLS", "6"))


def solve(profile: dict, n_hub: int, inc_pages: float, inc_calls: float, hit_rate: float,
          n_full: int, full_pages: float, full_calls: float, full_gated: bool = False) -> dict:
    """PURE packing solve → {t_star_s, binding, infeasible, ...}. Given the week budget and the two per-unit demands,
    solve T for each resource (T = week·inc_per_cycle / residual-after-full) and take the max (the binding resource sets
    the achievable period). residual ≤ 0 → that resource can't even fit the weekly full sweep → infeasible flag (the
    'full weekly = 446 VLM-hr' case until full-hash-gate lands). full_gated=False → full re-extracts every page (no
    hash-gate yet) → full_vlm = n_full·full_calls; True → only the changed fraction (~hit_rate) hits the VLM.
    {PLAN §packing solver; USER 2026-07-26} [CONFIDENCE: CONFIRMED — the two-equation max is the derived T*]."""
    week = profile["week_hours"]
    C_R = profile["render_pph"]
    C_V = profile["vlm_cph"]
    # per-CYCLE incremental demand: ALL hubs are rendered (to compute the hash), only the CHANGED fraction hits the VLM.
    inc_render_cycle = n_hub * inc_pages
    inc_vlm_cycle = n_hub * inc_calls * hit_rate
    # WEEKLY full demand (fixed — every company once/week). Render is always paid (must render to see routes); VLM is paid
    # per page unless full-hash-gate skips unchanged pages (full_gated).
    full_render_wk = n_full * full_pages
    full_vlm_wk = n_full * full_calls * (hit_rate if full_gated else 1.0)
    # residual capacity left for incremental after full's weekly slice
    render_resid = C_R * week - full_render_wk
    vlm_resid = C_V * week - full_vlm_wk
    inf = float("inf")
    # T (hours) = week · (incremental per cycle) / (residual capacity). Bigger demand or smaller residual → longer period.
    t_render_h = (week * inc_render_cycle / render_resid) if render_resid > _EPS else inf
    t_vlm_h = (week * inc_vlm_cycle / vlm_resid) if vlm_resid > _EPS else inf
    infeasible = (t_render_h == inf) or (t_vlm_h == inf)          # a resource can't fit full weekly at all
    binding = "render" if t_render_h >= t_vlm_h else "vlm"        # the resource that sets the (larger) period
    floor_h = profile["inc_floor_s"] / 3600.0                     # politeness: never cycle faster than this
    if infeasible:
        t_star_h = inf
    else:
        t_star_h = max(t_render_h, t_vlm_h, floor_h)             # binding resource, but not below the floor
    return {
        "t_star_s": None if infeasible else t_star_h * 3600.0,
        "t_render_h": t_render_h, "t_vlm_h": t_vlm_h, "binding": binding, "infeasible": infeasible,
        "inc_render_cycle": inc_render_cycle, "inc_vlm_cycle": inc_vlm_cycle,
        "full_render_wk": full_render_wk, "full_vlm_wk": full_vlm_wk,
        "render_resid": render_resid, "vlm_resid": vlm_resid,
    }


async def _respace_incremental(pool, t_star_s: float) -> int:
    """Spread all QUEUED incremental rows evenly across [now, now+T*] by rank → the fleet touches the hubs at a steady
    drip over the whole period instead of a burst, and full backfills the gaps. running rows are left alone (they re-arm
    via complete_work at the current T*). Returns rows re-spaced. {PLAN §full distribution: due_at staggered by rank}."""
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (ORDER BY due_at) - 1 AS rk, count(*) OVER () AS n
                FROM work_queue WHERE type='incremental' AND status='queued'
            )
            UPDATE work_queue w
            SET due_at = now() + ((r.rk::float / greatest(r.n,1)) * $1 || ' seconds')::interval, updated_at = now()
            FROM ranked r WHERE w.id = r.id;
            """,
            t_star_s,
        )
    try:
        return int(tag.split()[-1]) if tag and tag.startswith("UPDATE") else 0
    except (ValueError, IndexError):
        return 0


async def _publish(pool, profile, sol, tp, n_hub, eta_full_h, note) -> None:
    """Write the solved state to scheduler_state (single row) → complete_work reads t_star_s for re-arm, Today page reads
    the rest for the dashboard. {scheduler_state migration}."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE scheduler_state SET
                profile=$1, t_star_s=$2, binding=$3, c_r=$4, c_v=$5, hit_rate=$6,
                eta_full_h=$7, inc_hubs=$8, note=$9, updated_at=now()
            WHERE id=1;
            """,
            profile["name"], sol["t_star_s"], sol["binding"], tp["C_R"], tp["C_V"], tp["hit_rate"],
            eta_full_h, n_hub, note,
        )


async def solve_and_apply(pool) -> dict:
    """ONE solve cycle: gather live inputs → solve() → re-space incremental due_at → publish scheduler_state. Returns the
    solution for logging. This is the whole controller body; run() just calls it on a timer."""
    profile = prof.active()
    tp = await metrics.throughput(pool, _METRIC_WINDOW_H)        # observed C_R/C_V + hit_rate
    uc = await metrics.unit_cost(pool)                            # per-type measured cost (pages/calls per unit)
    qd = await metrics.queue_depth(pool)                          # backlog per type/status

    inc = uc.get("incremental", {})
    inc_pages = inc.get("pages_per_unit") or 1.0                  # measured; cold-start 1 page / 1 call
    inc_calls = inc.get("calls_per_unit") or 1.0
    full = uc.get("full", {})
    full_pages = full.get("pages_per_unit") or _DEFAULT_FULL_PAGES
    full_calls = full.get("calls_per_unit") or _DEFAULT_FULL_CALLS

    # N_hub = incremental units in rotation; N_full = full companies due to be swept weekly
    inc_q = qd.get("incremental", {})
    n_hub = int(inc_q.get("queued", 0)) + int(inc_q.get("running", 0))
    full_q = qd.get("full", {})
    n_full = int(full_q.get("queued", 0)) + int(full_q.get("running", 0))

    # full_gated=False: full is deep-BFS DISCOVERY and always fully extracts (NOT hash-gated) — hash-gate is incremental-
    # only. So full's weekly VLM demand = all pages. {USER 2026-07-26 "seed is for full bfs; hub[gate] is for incremental"}.
    sol = solve(profile, n_hub, inc_pages, inc_calls, tp["hit_rate"], n_full, full_pages, full_calls, full_gated=False)

    # full ETA (Little's Law, serial-VLM upper bound): remaining full VLM demand ÷ measured C_V. None when no full/no rate.
    eta_full_h = None
    if n_full > 0 and tp["C_V"] > _EPS:
        eta_full_h = sol["full_vlm_wk"] / tp["C_V"]

    if sol["infeasible"]:
        note = (f"Full weekly sweep of {n_full} companies won't fit one GPU (needs full-hash-gate or more capacity); "
                f"running incremental on a {profile['inc_floor_s']/3600:.1f}h cycle, full as opportunistic backlog.")
        # can't pack a WEEKLY full sweep → keep incremental at the politeness floor so it still cycles at a known rate;
        # publish t_star_s = floor so the UI's "refresh cycle" + complete_work re-arm both use the real 30-min cadence
        # (not null → "—"). full still runs as backlog whenever a full row is due. {USER 2026-07-26 full 2683 un-gated}.
        sol["t_star_s"] = profile["inc_floor_s"]
        await _respace_incremental(pool, profile["inc_floor_s"])
    else:
        note = f"T*={sol['t_star_s']/3600:.2f}h ({sol['binding']}-bound); full fills residual."
        await _respace_incremental(pool, sol["t_star_s"])
    await _publish(pool, profile, sol, tp, n_hub, eta_full_h, note)
    sol["_note"], sol["_tp"], sol["_n_hub"], sol["_n_full"], sol["_eta_full_h"] = note, tp, n_hub, n_full, eta_full_h
    return sol


def _fmt(sol: dict) -> str:
    tp = sol["_tp"]
    t = "INFEASIBLE" if sol["infeasible"] else f"{sol['t_star_s']/3600:.2f}h"
    return (f"[pacer] T*={t} binding={sol['binding']} | N_hub={sol['_n_hub']} N_full={sol['_n_full']} | "
            f"C_R={tp['C_R']:.0f}p/h C_V={tp['C_V']:.0f}c/h hit={tp['hit_rate']*100:.0f}% | "
            f"T_render={sol['t_render_h']:.2f}h T_vlm={sol['t_vlm_h']:.2f}h | {sol['_note']}")


async def run() -> None:
    pool = await q.connect_pool()
    loop = "--loop" in sys.argv
    print(f"[pacer] profile={prof.active()['name']} tick={_TICK_S}s window={_METRIC_WINDOW_H}h loop={loop}", flush=True)
    while True:
        try:
            sol = await solve_and_apply(pool)
            print(_fmt(sol), flush=True)
        except Exception as e:                                   # noqa: BLE001 — a solve error must not kill the controller
            print(f"[pacer] ✗ solve error: {type(e).__name__}: {e}", flush=True)
        if not loop:
            break
        await asyncio.sleep(_TICK_S)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
