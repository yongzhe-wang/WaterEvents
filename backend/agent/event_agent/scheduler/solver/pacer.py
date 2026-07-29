"""event_agent.pacer — THE packing solver = the "dynamic scheduling algorithm". Every cycle it reads the live
capacity profile + measured throughput/cost, solves the incremental period T* that packs incremental + full into the
week so BOTH render and VLM stay busy but neither overflows, then re-spaces the incremental due_at over T* and publishes
the state to scheduler_state (which complete_work reads for re-arm and the Today page reads for the dashboard).

用一句话讲完: 一周 168h,full 每周固定扫一遍(demand 固定)、incremental 每轮扫 N_hub 个 hub(轮数=168/T);对 render 和
VLM 各写「full demand + (168/T)·每轮 incremental demand = 容量·168」解出 T,取较大的(binding 资源)= T*;然后把所有 queued
incremental 的 due_at 均匀摊到 [now, now+T*] → full 用剩下的容量当 backfill 自动填 → 两个资源同时跑满。换 profile(RunPod→
GCP)→ 容量变 → 下一轮 T* 自动重解。{USER 2026-07-26 "best rotation T* so full fits perfectly; adjust dynamically;
adjustable for GCP; estimation of finish time"} [CONFIDENCE: CONFIRMED — 双资源 packing 方程本 session 推导 + 用户批准].

Run once (solve + print + apply):   PYTHONPATH=/workspace/WaterEvents/backend python -m agent.event_agent.pacer
Run as the resident controller:      PYTHONPATH=/workspace/WaterEvents/backend python -m agent.event_agent.pacer --loop
"""
from __future__ import annotations

import asyncio
import math
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

# Fraction of EVERY lane reserved for full when the weekly full sweep can't be packed (DEGRADED mode in solve()). full
# then finishes in ~1/_FULL_SHARE weeks instead of never, and incremental stretches to fit the remainder. WHY a reserved
# share rather than "run full as opportunistic backlog": the priority ladder gives incremental strict precedence, so
# "opportunistic" resolves to "never" whenever the incremental pool is non-empty — which the floor cadence guaranteed.
# 0.5 = an even split (full completes in ~2 weeks at the measured cost). {MEASURED 2026-07-27 "FULL SCANS YIELD 62.08
# EV/SCAN VS INCREMENTAL 7.61; COMPANIES WITH A FULL SCAN AVG 91 EV VS 47 WITHOUT"}
# [CONFIDENCE: CONFIRMED 95% — the split ratio is a policy choice; the need for a guaranteed (not opportunistic) share
#  is proven by the 96.7% never-scanned figure. Tune with EVENTINC_FULL_SHARE without a code edit.]
_FULL_SHARE = float(os.environ.get("EVENTINC_FULL_SHARE", "0.5"))

# Usable fraction of nominal slot-seconds. `slots x 3600` assumes PERFECT packing — no claim/poll gap, no backoff, no
# tail effect from a 367 s full unit finishing alone. Two independent reasons to discount it, and they agree:
#   (a) MEASURED: at 18 slots the fleet sustained 50,934 worker-s/h against 64,800 nominal = 78.6%.
#   (b) THEORY: for n periodic task classes, rate-monotonic scheduling only guarantees deadlines below
#       U <= n(2^(1/n) - 1); for the two classes here that bound is 2(sqrt(2) - 1) = 82.8%. Past it, non-harmonic
#       periods start missing deadlines — which is exactly the climbing `due_now` observed at 18 slots.
# Taking the theoretical bound rather than the measured 78.6% because the measurement was itself taken while OVER the
# bound (i.e. already degraded), so it under-reports what a correctly-paced fleet sustains.
# {SHA ET AL. "Generalized Rate-Monotonic Scheduling Theory", utilization bound n(2^(1/n)-1) -> ln 2 as n grows}
# {MEASURED 2026-07-29 "slots=18 worker_s_per_h=50934 util=78%" with due_now climbing 6->13->21}
# [CONFIDENCE: CONFIRMED 90% — the bound is textbook and the measurement brackets it; the 4-point gap between 78.6%
#  and 82.8% is not separately verified, which is why this is env-tunable rather than hard-coded.]
# NOTE: harmonic periods lift this bound to 1.0 — see the plan's option C. Worth revisiting if T* is ever quantised
# to an integer divisor of the full week.
_SLOT_UTIL = float(os.environ.get("EVENTINC_SLOT_UTIL", "0.828"))

# Harmonic-period quantisation, DEFAULT OFF — see the block in solve() for why it is not enabled. Pair with
# EVENTINC_SLOT_UTIL=1.0 when turning it on; enabling one without the other gets the constraint and not the benefit.
_HARMONIC = os.environ.get("EVENTINC_HARMONIC", "") in ("1", "true", "yes")


def solve(profile: dict, n_hub: int, inc_pages: float, inc_calls: float, hit_rate: float,
          n_full: int, full_pages: float, full_calls: float, full_gated: bool = False,
          measured_c_r: float = 0.0, measured_c_v: float = 0.0,
          inc_secs: float = 0.0, full_secs: float = 0.0) -> dict:
    """PURE packing solve → {t_star_s, binding, infeasible, ...}. Given the week budget and the two per-unit demands,
    solve T for each resource (T = week·inc_per_cycle / residual-after-full) and take the max (the binding resource sets
    the achievable period). residual ≤ 0 → that resource can't fit the weekly full sweep → DEGRADED mode (see below).
    full_gated=False → full re-extracts every page (no hash-gate yet) → full_vlm = n_full·full_calls; True → only the
    changed fraction (~hit_rate) hits the VLM.
    {PLAN §packing solver; USER 2026-07-26} [CONFIDENCE: CONFIRMED — the two-equation max is the derived T*]."""
    week = profile["week_hours"]
    # CAPACITY = max(static profile ceiling, SUSTAINED MEASURED rate). WHY the max and not the ceiling alone: a rate the
    # fleet actually sustained across the whole metric window is PROOF the lane carries at least that much, whereas the
    # profile number is a hand-estimate that can be too LOW — and a too-low ceiling makes the packing solve falsely
    # declare INFEASIBLE. That is exactly what happened: vlm_cph=250 (an estimate) vs 338.33 measured → vlm_resid =
    # 250*168 - 42074 = -74 calls (0.18% short) → infeasible → T* slammed to the 1800s floor → incremental flooded the
    # queue and full starved at 96.7% never-scanned. When measured < ceiling the fleet is merely demand-limited (not at
    # capacity), so the ceiling correctly wins and max() is a no-op. This implements the behaviour profile.py already
    # DOCUMENTED but which was never wired up — solve() read profile["vlm_cph"] unconditionally.
    # {PROFILE.PY:19 "THE SOLVER PREFERS THE MEASURED C_V WHEN SCAN_LOG HAS ENOUGH DATA; THIS IS THE COLD-START FALLBACK"}
    # {MEASURED 2026-07-27 6H WINDOW "C_V_MEASURED 338.33 | VLM_RESID -74 | T_STAR PUBLISHED 1800S = INC_FLOOR_S"}
    # [CONFIDENCE: CONFIRMED 100% — the -74 shortfall was reproduced from live DB inputs and matches the published
    #  scheduler_state.note verbatim; same failure class as the _DEFAULT_FULL_PAGES=40 bug fixed above].
    C_R = max(profile["render_pph"], measured_c_r or 0.0)
    C_V = max(profile["vlm_cph"], measured_c_v or 0.0)
    # Slots is a HARD physical count, not an estimate, so it takes no max() with a measured rate — you cannot run more
    # units at once than there are slots. A missing/zero cost measurement disables the slot equation rather than
    # producing a divide-by-a-guess: cold start should fall back to the two lanes that already worked, not invent one.
    slots = float(profile.get("slots") or 0.0)
    slots_known = slots > 0 and inc_secs > 0 and full_secs > 0
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
    # ── THIRD RESOURCE: WORKER SLOTS, in unit-seconds ────────────────────────────────────────────────────────────────
    # A unit holds its slot for its whole wall-clock duration no matter which lane it is blocked on, so concurrency —
    # not pages and not calls — is what actually bounds the fleet. Modelling only render and VLM let this solver publish
    # a T* the fleet could not physically sustain, and the shortfall was invisible in every number it printed: the queue
    # simply fell behind. Same shape as the two equations above so the three compose as a plain max().
    # {MEASURED 2026-07-29 duration_s over 7,076 rows — full 367.1 s/unit, incremental 31.3 s/unit (11.7x apart)}
    # {MEASURED 2026-07-29 "slots=18 running=18 due_now 6→13→21" then, after slots=24, "due_now 6→5→3→0→0→0"}
    # [CONFIDENCE: CONFIRMED 100% — the pinned `running` with a monotonically climbing `due_now` is the signature of a
    #  concurrency bound, and raising ONLY the slot count drained the backlog within three minutes.]
    # NOT-MEASURED must mean NON-BINDING (0.0), never inf. An unknown cost is not evidence of infeasibility, and an inf
    # here would propagate straight into `infeasible` and slam T* to the politeness floor — the exact 2026-07-27
    # failure this file already documents, re-created from a cold start with no duration data.
    slot_seconds_wk = slots * 3600.0 * week * _SLOT_UTIL if slots_known else 0.0
    full_secs_wk = n_full * full_secs if slots_known else 0.0
    inc_secs_cycle = n_hub * inc_secs if slots_known else 0.0
    slot_resid = slot_seconds_wk - full_secs_wk                   # what is left for incremental after full's weekly slice
    if not slots_known:
        t_slots_h = 0.0                                           # unmeasured → contributes nothing to the max()
    else:
        t_slots_h = (week * inc_secs_cycle / slot_resid) if slot_resid > _EPS else inf
    infeasible = (t_render_h == inf) or (t_vlm_h == inf) or (t_slots_h == inf)   # a resource can't fit full weekly at all
    floor_h = profile["inc_floor_s"] / 3600.0                     # politeness: never cycle faster than this
    if infeasible:
        # DEGRADED MODE — full's weekly sweep alone over-subscribes a lane, so a WEEKLY full cadence is off the table.
        # The correct response is to slow full down while still GUARANTEEING it a fixed slice of every lane, and to
        # stretch incremental over whatever is left. The previous behaviour did the exact opposite: it published
        # t_star = inc_floor_s (the FASTEST cadence the politeness cap allows), which maximised incremental pressure
        # precisely when there was no spare capacity. Because claim_work orders by `priority ASC` and incremental is
        # priority 10 vs full 100, a permanently-due incremental pool means a worker can essentially never reach a full
        # row → full starved at 2595/2683 (96.7%) never scanned, and companies never got their deep BFS.
        # {QUEUE.PY claim_work "ORDER BY PRIORITY ASC, DUE_AT ASC"; DB 2026-07-27 "INCREMENTAL PRIORITY 10 / FULL 100"}
        # {MEASURED 2026-07-27 "18/18 RUNNING WORKERS ON INCREMENTAL, 0 ON FULL; FULL NEVER_SCANNED 2595/2683 = 96.7%"}
        # [CONFIDENCE: CONFIRMED 100% — starvation observed live; the floor fallback is the mechanism].
        r_resid = C_R * week * (1.0 - _FULL_SHARE)                # incremental may only spend the non-reserved slice
        v_resid = C_V * week * (1.0 - _FULL_SHARE)
        s_resid = slot_seconds_wk * (1.0 - _FULL_SHARE)
        t_render_h = (week * inc_render_cycle / r_resid) if r_resid > _EPS else inf
        t_vlm_h = (week * inc_vlm_cycle / v_resid) if v_resid > _EPS else inf
        t_slots_h = (week * inc_secs_cycle / s_resid) if s_resid > _EPS else inf
    # The binding resource is whichever demands the LONGEST period; T* must satisfy all three simultaneously.
    _cands = {"render": t_render_h, "vlm": t_vlm_h, "slots": t_slots_h}
    binding = max(_cands, key=lambda k: _cands[k])
    t_star_h = max(t_render_h, t_vlm_h, t_slots_h, floor_h)       # binding resource, but not below the politeness floor
    # OPTIONAL: snap T* DOWN to an integer divisor of the full-sweep week, making the two periods harmonic.
    # WHY it would help: the _SLOT_UTIL discount above exists because rate-monotonic scheduling only guarantees
    # deadlines below n(2^(1/n)-1) = 82.8% for two NON-harmonic classes. For a harmonic task set that bound rises to
    # 1.0, so harmonising would recover ~17% of usable capacity for free.
    # WHY IT IS OFF BY DEFAULT: it is an optimisation with no demonstrated need. After raising the slot count the fleet
    # sits at ~58% utilisation with the backlog drained, so there is nothing for the extra headroom to buy, and turning
    # it on quantises T* — a real constraint that would have to be reasoned about every time the solve moves. Enabling
    # it without evidence would be optimising the part of the system that is currently not the problem.
    # Snapping DOWN (to the next-shorter harmonic period) keeps the result feasible: a shorter period costs MORE
    # capacity, so it must only be taken when the harmonic bound actually licenses it — hence the pairing with
    # EVENTINC_SLOT_UTIL=1.0, which the operator sets together with this flag.
    # {SHA ET AL. — for harmonic periods RMS achieves 100% utilisation vs 82.8% for two arbitrary periods}
    # [CONFIDENCE: CONFIRMED 90% for the theory; UNVERIFIED on this fleet — deliberately not enabled, so it has never
    #  run in production. Do not turn it on without re-measuring due_now slope.]
    if _HARMONIC and t_star_h > _EPS:
        k = math.floor(week / t_star_h)                           # largest integer number of cycles that fits the week
        if k >= 1:
            t_star_h = week / k                                   # exact divisor → the two periods are harmonic
    return {
        "t_star_s": t_star_h * 3600.0,
        "t_render_h": t_render_h, "t_vlm_h": t_vlm_h, "t_slots_h": t_slots_h,
        "binding": binding, "infeasible": infeasible,
        "inc_render_cycle": inc_render_cycle, "inc_vlm_cycle": inc_vlm_cycle,
        "full_render_wk": full_render_wk, "full_vlm_wk": full_vlm_wk,
        "render_resid": render_resid, "vlm_resid": vlm_resid,
        # Slot terms exposed so `binding=slots` can be explained without re-deriving it from the profile.
        "slots": slots, "slot_seconds_wk": slot_seconds_wk, "full_secs_wk": full_secs_wk, "slot_resid": slot_resid,
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


async def _respace_full(pool, week_hours: float) -> int:
    """Spread all QUEUED full rows evenly across [now, now+week], STALEST FIRST → the deep sweep completes in exactly
    one week by construction instead of by hope.

    WHY this had to exist. complete_work sets a full unit's next due_at to `now + 7 days`, so the rate at which full
    units become DUE is simply the rate at which they were scanned a week ago. The lane therefore replays its own
    history forever and has no way to catch up from any deficit: measured 2026-07-29, full needed 16.0 units/h to meet
    the weekly contract and was getting 11.2 — 70% of the required rate, a 10.0-day sweep — while the fleet sat at only
    61% utilisation. There was spare capacity the whole time; nothing was asking full to use it. Incremental has been
    re-spaced every tick since day one (see _respace_incremental); full never was, and that asymmetry IS the missed
    deadline.

    Re-spacing by rank forces exactly n/week units to come due per hour, which is the definition of the constraint.
    Ordering by last_scanned_at NULLS FIRST puts never-scanned and longest-neglected companies at the front of the
    week, so a deficit drains oldest-first rather than at random.

    This pairs with the lateness-ratio ordering in claim_work: re-spacing decides WHEN a unit becomes due, that
    ordering guarantees a due unit is actually reached. Either alone is insufficient — spacing without the ordering
    starves against strict priority, ordering without spacing never generates the demand in the first place.

    `running` rows are left alone; they re-arm through complete_work. Returns rows re-spaced.
    {MEASURED 2026-07-29 "full_needed_per_h 16.0 | full_actual_per_h 11.2 | days_for_a_full_sweep 10.0 | covered_7d
     1118/2683" against a fleet at 61% utilisation}
    [CONFIDENCE: CONFIRMED 100% — the rate deficit and the spare capacity were measured in the same query window.]"""
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (ORDER BY last_scanned_at ASC NULLS FIRST, due_at ASC) - 1 AS rk,
                       count(*) OVER () AS n
                FROM work_queue WHERE type='full' AND status='queued'
            )
            UPDATE work_queue w
            SET due_at = now() + ((r.rk::float / greatest(r.n,1)) * $1 || ' seconds')::interval, updated_at = now()
            FROM ranked r WHERE w.id = r.id;
            """,
            week_hours * 3600.0,
        )
    try:
        return int(tag.split()[-1]) if tag and tag.startswith("UPDATE") else 0
    except (ValueError, IndexError):
        return 0


async def _reap_failed(pool, cooloff_h: float = 6.0) -> int:
    """Return 'failed' units to the queue once they have cooled off → the queue stops leaking rows permanently.

    用一句话讲完: fail_work 在连续失败到达上限时把行标成 'failed',但 claim_work 只认领 'queued' 和 lease 过期的
    'running' —— 'failed' 不在里面,而全仓库**没有任何代码**会把它改回去。所以那是个绝对终态:一个 url 因为一次运维重启
    或一段网络抖动被判死,就永远退出队列,而 fail_work 的 docstring 却承诺 "a reconcile/monitor surfaces it"。这个
    reconcile 从来不存在(events.py 的 reconcile_events 只管 events 表的 enrichment,不碰 work_queue)。
    这里补上它:冷却 cooloff_h 之后把行放回 queued 并清零 attempt,让它有机会重新证明自己;真正永久坏掉的 url 会再次
    失败并再次退出,所以这不会变成无限重试 —— 只是把「永久死刑」降级成「带冷却的重试」。

    Upstream trigger: the pacer's hourly tick (it is the only always-on singleton, so no new process is needed).
    Downstream: rows become claimable again on the next claim_work.
    {QUEUE.PY fail_work "MARK 'FAILED' (FAIL-LOUD, A RECONCILE/MONITOR SURFACES IT) SO A PERMANENTLY-BROKEN URL DOESN'T
     SPIN FOREVER" — the promised reconcile did not exist}
    {QUEUE.PY claim_work "WHERE (STATUS = 'QUEUED' OR (STATUS = 'RUNNING' AND LEASE_UNTIL < NOW()))" — 'failed' excluded}
    [CONFIDENCE: CONFIRMED 100% — grep over backend/ found exactly one writer of status='failed' and zero readers that
     restore it; live work_queue had 0 failed rows at the time of writing, so this lands before any damage, not after].
    """
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """
            UPDATE work_queue SET status='queued', attempt=0, lease_owner=NULL, lease_until=NULL,
                   due_at=now(), updated_at=now()
            WHERE status='failed' AND updated_at < now() - ($1 || ' hours')::interval;
            """,
            str(cooloff_h),
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
    # measured_c_r/measured_c_v let solve() raise a too-low profile ceiling to the rate the fleet demonstrably sustains
    # (see the capacity comment in solve()) — the "solver prefers the MEASURED C_V" behaviour profile.py documents.
    # Per-unit WALL-CLOCK seconds — the slot equation's demand term. metrics.unit_cost has always returned these; the
    # solver simply never consumed them. 0.0 when a type has never completed a scan, which switches the slot equation
    # off rather than guessing (see `slots_known` in solve()).
    inc_secs = inc.get("sec_per_unit") or 0.0
    full_secs = full.get("sec_per_unit") or 0.0
    sol = solve(profile, n_hub, inc_pages, inc_calls, tp["hit_rate"], n_full, full_pages, full_calls, full_gated=False,
                measured_c_r=tp["C_R"], measured_c_v=tp["C_V"], inc_secs=inc_secs, full_secs=full_secs)

    # full ETA (Little's Law, serial-VLM upper bound): remaining full VLM demand ÷ measured C_V. None when no full/no rate.
    eta_full_h = None
    if n_full > 0 and tp["C_V"] > _EPS:
        # In DEGRADED mode full only owns _FULL_SHARE of the lane, so dividing by the WHOLE C_V would under-report the
        # sweep time (it read 136.7h while full was in fact making no progress at all). Scale by the share it actually
        # holds. {MEASURED 2026-07-27 "ETA_FULL_H 136.68 PUBLISHED WHILE FULL NEVER_SCANNED = 96.7%"}
        # [CONFIDENCE: CONFIRMED 100% — published ETA contradicted the observed zero progress].
        share = _FULL_SHARE if sol["infeasible"] else 1.0
        eta_full_h = sol["full_vlm_wk"] / (tp["C_V"] * share)

    # solve() now ALWAYS returns a finite T* — feasible → packed against the residual after full's weekly slice;
    # infeasible → DEGRADED, packed against the (1-_FULL_SHARE) slice with full holding a guaranteed reservation.
    # Either way there is exactly one re-space call and one published cadence, so complete_work's re-arm and the Today
    # page's "refresh cycle" always agree. {see solve() DEGRADED MODE comment for why the old floor fallback starved full}.
    if sol["infeasible"]:
        note = (f"DEGRADED: weekly full sweep of {n_full} won't fit a lane → full reserved {_FULL_SHARE*100:.0f}% of "
                f"capacity (≈{1.0/max(_FULL_SHARE,_EPS):.1f} weeks/sweep), incremental stretched to "
                f"T*={sol['t_star_s']/3600:.2f}h ({sol['binding']}-bound).")
    else:
        note = f"T*={sol['t_star_s']/3600:.2f}h ({sol['binding']}-bound); full fills residual."
    # Reap before re-spacing: a revived row goes back to 'queued' with due_at=now(), so letting the respace pass see it
    # puts it in the rank order with everything else instead of leaving it bunched at now().
    reaped = await _reap_failed(pool)
    respaced = await _respace_incremental(pool, sol["t_star_s"])
    # Same treatment for full, over the week rather than over T*. Incremental has been re-spaced every tick since this
    # controller was written and full never was; that asymmetry is why the weekly deadline was being missed at 70% of
    # the required rate while capacity sat idle. See _respace_full for the measurement.
    respaced_full = await _respace_full(pool, profile["week_hours"])
    await _publish(pool, profile, sol, tp, n_hub, eta_full_h, note)
    sol["_note"], sol["_tp"], sol["_n_hub"], sol["_n_full"], sol["_eta_full_h"] = note, tp, n_hub, n_full, eta_full_h
    sol["_respaced_full"] = respaced_full
    # Surface both counts — "re-spaced 5000 rows" and "re-spaced 0 because everything is stuck in running" printed
    # identically before, which is the silently-does-nothing shape this audit was looking for.
    sol["_reaped"], sol["_respaced"] = reaped, respaced
    if reaped:
        print(f"[pacer] revived {reaped} failed unit(s) after cooloff", flush=True)
    return sol


def _fmt(sol: dict) -> str:
    tp = sol["_tp"]
    t = f"{sol['t_star_s']/3600:.2f}h" + (" DEGRADED" if sol["infeasible"] else "")   # always a real cadence now
    return (f"[pacer] T*={t} binding={sol['binding']} | N_hub={sol['_n_hub']} N_full={sol['_n_full']} | "
            f"C_R={tp['C_R']:.0f}p/h C_V={tp['C_V']:.0f}c/h hit={tp['hit_rate']*100:.0f}% | "
            f"T_render={sol['t_render_h']:.2f}h T_vlm={sol['t_vlm_h']:.2f}h | "
            f"respaced={sol.get('_respaced', 0)} reaped={sol.get('_reaped', 0)} | {sol['_note']}")


async def run() -> None:
    pool = await q.connect_pool()
    loop = "--loop" in sys.argv
    print(f"[pacer] profile={prof.active()['name']} tick={_TICK_S}s window={_METRIC_WINDOW_H}h loop={loop}", flush=True)
    while True:
        # LEASE SWEEP — reclaim work_queue rows whose lease lapsed (a worker died / was restarted mid-unit). Runs HERE,
        # in the pacer, because the pacer is the one SINGLE-INSTANCE service in the fleet (waterevents-pacer.service is a
        # plain unit; the workers are a templated @-unit run N-up), so the sweep happens exactly once per tick instead of
        # six times. Deliberately BEFORE solve_and_apply: reclaimed rows become 'queued' again and so are counted by the
        # very next solve, rather than being invisible to it for another whole tick.
        # WHY it is needed at all: complete_work pushes a full unit's due_at a week out, and claim_work's opportunistic
        # reclaim arm is conjoined with `due_at <= now()` — so a full unit that dies mid-scan is invisible work for up to
        # 7 days. An audit found 28 such rows already stuck in production, all owned by the host that wedged.
        # {AUDIT 2026-07-28 "WORK_QUEUE STATUS=RUNNING -> 91, OF WHICH RUNNING ROWS WHOSE LEASE ALREADY LAPSED: 28"}
        # {WORK_QUEUE.SQL:35 "A RECONCILE CRON FLIPS THEM BACK TO 'QUEUED'" — the migration promised this cron; it was
        #  never written, and reconcile_work had zero callers until this line}
        # [CONFIDENCE: CONFIRMED 100% — the stuck-row count is a live measurement and the missing caller was verified by
        #  `git grep reconcile_work` returning only the definition.]
        try:
            reclaimed = await q.reconcile_work(pool)
            if reclaimed:                                        # silent when there is nothing to reclaim (the normal case)
                print(f"[pacer] reclaimed {reclaimed} lapsed lease(s) → queued", flush=True)
        except Exception as e:                                   # noqa: BLE001 — a sweep error must not stop the solve
            print(f"[pacer] ✗ reconcile error: {type(e).__name__}: {e}", flush=True)
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
