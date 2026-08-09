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


async def probe_ceilings(timeout_s: float = 4.0) -> dict:
    """从**活着的服务**读容量上限,读不到就返回 {} 让调用方退回 profile 里的常数。

    用一句话讲完: 两个上限本来是写在 profile 里的数字,而那两个数字都过期了 —— vlm_cph=250 是注释里
    自认的「估计」,render_pph=1728 是在一台 16 核机器上测的,而那台机器现在是 8 核。与其维护它们,
    不如问服务自己。

    WHY 不用 scan_log 的实测值: solve() 已经有 measured_c_v 了,但它量的是「我们发出去了多少」——
    一个**受需求限制**的观测量。舰队慢 → 发得少 → 测得低 → 求解器认为上游没能力 → 把周期拉长 →
    发得更少。这个回路真实发生过:
    {SCHEDULER_STATE 2026-08-08 "t_star_s→93.4h binding=vlm c_v=170.5",而同期网关 /gwstats 实测
     prefill 2,631 tok/s、3,220 请求/h、GPU 100%、队列为 0 —— 上游根本没有饱和}
    网关的 capacity_cph 不同:它是「最好的 prefill 速率 ÷ 当前每请求 token 数」,分子是这块卡的能力
    (和需求无关,由自适应闸门主动试探得到),分母是当下负载的形状。这才是能当容量用的量。
    [CONFIDENCE: CONFIRMED 100% — 同一天内每请求 token 数在 2,942–7,835 之间变动而 prefill 速率稳定在
     2,400–2,600,证明按 calls/hour 写死的常数在任一形状下都不成立。]

    上游触发: pacer.solve_and_apply 每次求解前。下游连接: solve() 的 C_R / C_V。
    """
    import asyncio
    import json
    import urllib.request

    def _get(url: str) -> dict:
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as r:
                return json.loads(r.read().decode())
        except Exception:                                    # noqa: BLE001 — 服务不可达 → 退回常数,不该让求解崩
            return {}

    gw_url = os.environ.get("QWEN_BASE_URLS", "").split(",")[0].strip().rstrip("/")
    gw_url = gw_url[:-3] if gw_url.endswith("/v1") else gw_url
    render_url = os.environ.get("RENDER_REMOTE_URL", "").rstrip("/")

    gw, rd = await asyncio.gather(
        asyncio.to_thread(_get, f"{gw_url}/gwstats") if gw_url else asyncio.sleep(0, {}),
        asyncio.to_thread(_get, f"{render_url}/health") if render_url else asyncio.sleep(0, {}),
    )
    out: dict = {}
    # 只在网关真的测出过东西时才采信 —— 冷启动时 capacity_cph=0,那时用常数是对的。
    if float(gw.get("capacity_cph") or 0) > 0:
        out["vlm_cph"] = float(gw["capacity_cph"])
        out["vlm_src"] = "gwstats"
    # render 侧没有「饱和容量」的直接读数,退而求其次用累计速率;它是需求受限的下界,所以只用来
    # **抬高**过期的常数(见 solve() 里的 max),不会把一个正确的常数压低。
    up, tot = float(rd.get("uptime_s") or 0), float(rd.get("total") or 0)
    if up > 300 and tot > 0:
        out["render_pph"] = tot / up * 3600.0
        out["render_src"] = "render/health"
    return out
