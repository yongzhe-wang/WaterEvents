"""event_agent.profile — the ADJUSTABLE capacity profile the packing solver reads as the resource ceilings. Switching
machines (RunPod → GCP) = changing EVENTINC_PROFILE (or one dict entry); the solver's math is unchanged.

用一句话讲完: 每个 profile 一行 = {render_pph(该机器 render 天花板 pages/hr), vlm_cph(VLM 天花板 page-extractions/hr,
两台都是同一块 A5000 → 不变), week_hours, inc_floor_s(再快也不低于这个周期,别把网站爬崩)};solver 用 active profile 的
render_pph/vlm_cph 当 CAPACITY,用实测 hit_rate + unit_cost 当 DEMAND,解出 T*。RunPod 7.65 核 render-bound,GCP 加核后
VLM-bound —— 换 profile 就换 T*。{USER 2026-07-26 "adjustable when we move to GCP; two bottleneck render and vlm"}
[CONFIDENCE: CONFIRMED — render_pph 本 session 实测(RunPod 1728);gcp 值按核数线性外推, vlm_cph=250 是 14B AWQ 估计].
"""
from __future__ import annotations

import os

# render_pph = pages/hr the RENDER lane can sustain at saturation (CPU-bound; scales ~linearly with cores).
#   RunPod: 7.65-core cgroup ceiling ~0.48 pages/s × 3600 = 1728 {MEASURED PRIOR-SESSION "40-way==8-way ~0.48 pages/s"}.
#   gcp32/gcp64: c2d-standard-{32,64} → ~4×/~8× the cores → linear extrapolation (refine with a real measurement on GCP).
# vlm_cph = page-extractions/hr the VLM can sustain at saturation. SAME A5000 on both RunPod and GCP (VLM stays on the pod
#   via the SSH tunnel), so it does NOT change with the render machine. 250 is the 14B-AWQ estimate (~40s/extract, continuous
#   batching midpoint) — the solver prefers the MEASURED C_V when scan_log has enough data; this is the cold-start fallback.
# inc_floor_s = never cycle incremental faster than this even if resources allow — politeness cap so a fast fleet doesn't
#   hammer a hub every few minutes. 1800s = 30 min (the original target cadence floor).
# slots = CONCURRENT UNITS the fleet can hold at once = (worker processes) x EVENTINC_WORKERS. This is the third and,
# on this host, the FIRST-BINDING resource, and the solver had no concept of it: it modelled render pages/hr and VLM
# calls/hr only. A unit occupies its slot for its whole wall-clock duration regardless of which lane it is waiting on,
# so slots — not pages and not calls — are what actually caps concurrency. Modelling only the other two lets the solver
# publish a T* that needs more concurrent unit-seconds than exist, and nothing notices: the symptom is `running` pinned
# at the cap while `due_now` climbs monotonically, i.e. the fleet silently falls behind its own published schedule.
# {MEASURED 2026-07-29 03:27 at EVENTINC_WORKERS=3 "slots=18 running=18 due_now=6→13→21 util=78%"}
# {MEASURED 2026-07-29 03:30 at EVENTINC_WORKERS=4 "slots=24 due_now=6→5→3→0→0→0 util=58%" — the backlog drained}
# [CONFIDENCE: CONFIRMED 100% — the before/after was a single-variable change measured on the live fleet minutes apart.]
# Default 24 = the 6 systemd worker units x EVENTINC_WORKERS=4 now deployed. Override per host without a code edit.
_PROFILES = {
    "runpod": {"render_pph": 1728.0, "vlm_cph": 250.0, "week_hours": 168.0, "inc_floor_s": 1800.0, "slots": 24},
    "gcp32":  {"render_pph": 7000.0, "vlm_cph": 250.0, "week_hours": 168.0, "inc_floor_s": 1800.0, "slots": 24},
    "gcp64":  {"render_pph": 14000.0, "vlm_cph": 250.0, "week_hours": 168.0, "inc_floor_s": 1800.0, "slots": 48},
}


def active() -> dict:
    """The active capacity profile (EVENTINC_PROFILE, default 'runpod'), with per-field env overrides so a single knob can
    be tuned without a code edit (e.g. EVENTINC_VLM_CPH=320 after a real saturation measurement). Returns a dict with
    name + the four ceilings. {USER 2026-07-26 "adjustable"} [CONFIDENCE: CONFIRMED — env override = the adjust mechanism]."""
    name = os.environ.get("EVENTINC_PROFILE", "runpod")
    p = dict(_PROFILES.get(name, _PROFILES["runpod"]))       # copy so env overrides don't mutate the module table
    p["name"] = name
    p["render_pph"] = float(os.environ.get("EVENTINC_RENDER_PPH", p["render_pph"]))   # per-field override → tune live
    p["vlm_cph"] = float(os.environ.get("EVENTINC_VLM_CPH", p["vlm_cph"]))
    p["week_hours"] = float(os.environ.get("EVENTINC_WEEK_HOURS", p["week_hours"]))
    p["inc_floor_s"] = float(os.environ.get("EVENTINC_INC_FLOOR_S", p["inc_floor_s"]))
    # Prefer deriving slots from the SAME env the workers read, so the solver's model and the fleet's actual concurrency
    # cannot drift: EVENTINC_WORKERS is per-process, EVENTINC_PROCS is how many worker units run. A hand-set
    # EVENTINC_SLOTS still wins for the case where the two are not on the same host.
    procs = float(os.environ.get("EVENTINC_PROCS", 6))
    per_proc = float(os.environ.get("EVENTINC_WORKERS", 0) or 0)
    derived = procs * per_proc if per_proc else 0.0
    p["slots"] = float(os.environ.get("EVENTINC_SLOTS", derived or p["slots"]))
    return p
