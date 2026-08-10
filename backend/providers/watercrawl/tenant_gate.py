"""tenant_gate — weighted work-conserving admission control for the render VM, so event_agent and media_agent cannot
starve each other for browser slots or download bandwidth.

用一句话讲完: 每个请求带一个 X-WE-Tenant 头 → 进闸门先算「我这个租户当前的额度 = 总量 × 我的权重 ÷ 活跃租户的权重和」
→ 够额度才放行, 不够就在条件变量上等 → 释放时唤醒所有等待者重新按(可能变大的)额度评估。

WHY 这个存在: 两条流水线都是常态流, 谁都不能被饿死 —— incremental 一直在跑并持续产出新事件, 而每个新事件都要进
media 富集。render VM 上的并发闸门(runtime._sem / _shot_sem)是全局的, 完全不知道请求来自谁, 所以在 media 上线
之前这里没有任何东西阻止一边把另一边挤干。
{USER 2026-08-05 "we need this system to exist as isolated two parts, because incremental is always running and
 produce new events this part cannot be compromised and new events are always going into media pipeline that is also
 not compromisable"}
[CONFIDENCE: CONFIRMED — direct user requirement; the ratio is tunable, the isolation is not].

WHY 权重可调而不是写死对半: 一个 fleet 用多少不是固定的需求, 是调度的产物 —— stage-1 的 T* 是 slots-bound 的
{SCHEDULER_STATE 2026-08-05 "binding='slots'", "note='T*=7.42h (slots-bound); full fills residual.'"}, 给它更多槽位
它就用更多、T* 就下降。所以「几比几」是产品决定(数据要多新鲜), 不是可以测出来的容量, 因此必须是旋钮。

WHY work-conserving: 分母只算**活跃**租户的权重, 所以一边没有请求在飞时另一边的额度自动涨到总量。这不是为了应付
「某条流水线整段空闲」的场景(那不会发生), 而是为了填补毫秒级的请求间隙 —— 两边的瞬时用量都在波动, 每个波谷都是
对方可以用的容量。硬切会把这些间隙永久浪费掉。

WHY 两个独立预算而不是一个: 浏览器槽和下载带宽是同一台机器上的两种资源。一次 fetch_audio 可能占住几分钟(一个
choruscall 财报音频 91.7 MB {SERVER CONTENT-LENGTH 91,723,583}), 如果它和页面渲染共用一个预算, 一个大文件下载
会堵死页面渲染 —— 那是优先级反转, 不是公平。
[CONFIDENCE: CONFIRMED — the file size is the server's own header].

这份逻辑和 deploy/runpod/fair_gateway.py 是同一套(acquire/release + 活跃权重和), 刻意保持一致: 两层用同一个心智
模型, 才能在压力下推理出系统会怎么表现。
"""
from __future__ import annotations

import asyncio
import os
import sys

# Tenant comes from a header the client sets from its own WE_TENANT env, which is set per systemd unit — the same
# mechanism the vLLM gateway uses, for the same reason: it is the ONE thing that has to differ between two fleets that
# otherwise read identical config.
HEADER = "X-WE-Tenant"

# Unlabelled requests are NOT rejected — they are given a deliberately small slice. Rejecting would turn a forgotten
# header into an outage; letting them run unmetered would turn it into a silent bypass of the whole scheme. A small
# share makes the omission visible in the numbers (an 'anon' bucket that keeps filling) while nothing actually breaks.
ANON = "anon"
ANON_W = float(os.environ.get("RENDER_ANON_WEIGHT", "1"))


def _parse_weights(raw: str) -> dict[str, float]:
    """"event:5,media:5" → {"event": 5.0, "media": 5.0}. A malformed pair is skipped rather than raised: this runs at
    import time on the render VM, and a typo in an env var must not take the render service down for the whole fleet."""
    out: dict[str, float] = {}
    for pair in (raw or "").split(","):
        k, _, v = pair.partition(":")
        if k.strip() and v.strip():
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out


class Gate:
    """One weighted work-conserving budget. Two of these exist: one for browser work, one for downloads.

    Not a semaphore: a semaphore has one global count and no idea who is holding it. The whole point here is that the
    ceiling a caller sees depends on who ELSE is currently asking."""

    def __init__(self, name: str, total: float, weights: dict[str, float]):
        self.name = name
        self.total = total
        self.weights = weights
        self._want: dict[str, int] = {}          # tenant → requests in the gate (waiting OR admitted) = "active"
        self._inflight: dict[str, float] = {}    # tenant → admitted weight currently held
        self._cond = asyncio.Condition()

    def w(self, tenant: str) -> float:
        return self.weights.get(tenant, ANON_W)

    def _active_weight(self) -> float:
        """Summed weight of tenants currently asking. An idle tenant contributes nothing, which is exactly what makes
        the scheme work-conserving: with one side quiet the denominator is just the other side's weight, so its cap is
        the whole total."""
        return sum(self.w(t) for t, c in self._want.items() if c > 0) or 1.0

    async def acquire(self, tenant: str, cost: float = 1.0) -> None:
        """Block until `tenant` can hold `cost` more without exceeding EITHER its own share OR the global total.

        Both conditions are needed and they are different: the share stops one tenant starving the other, the global
        total stops the box being overloaded when only one tenant is present and its cap is therefore everything."""
        async with self._cond:
            self._want[tenant] = self._want.get(tenant, 0) + 1
            try:
                while True:
                    cap = self.total * self.w(tenant) / self._active_weight()
                    cur = self._inflight.get(tenant, 0.0)
                    gtot = sum(self._inflight.values())
                    if cur + cost <= cap and gtot + cost <= self.total:
                        self._inflight[tenant] = cur + cost
                        return
                    await self._cond.wait()
            finally:
                self._want[tenant] -= 1          # leaving the queue, admitted or not, stops counting as demand

    async def release(self, tenant: str, cost: float = 1.0) -> None:
        """Return the weight and wake EVERY waiter — not just one. A release can raise several tenants' caps at once
        (it may also have been the last request of a tenant, shrinking the denominator for everyone else), so who is
        now admissible cannot be decided without re-evaluating all of them."""
        async with self._cond:
            self._inflight[tenant] = max(0.0, self._inflight.get(tenant, 0.0) - cost)
            self._cond.notify_all()

    def snapshot(self) -> dict:
        """What /health reports. inflight per tenant plus the cap each currently sees — the cap is the interesting half,
        because it is what makes a queue legible: a tenant sitting at its cap is being shaped, one below it is not."""
        aw = self._active_weight()
        return {
            "total": self.total,
            "weights": dict(self.weights),
            "inflight": {t: round(v, 2) for t, v in self._inflight.items() if v > 0},
            "waiting": {t: c for t, c in self._want.items() if c > 0},
            # Clamped to total: with nobody asking, _active_weight() falls back to 1.0 and the raw formula yields a
            # cap LARGER than the budget it is a share of. Admission is unaffected (it also checks the global total),
            # but a dashboard showing "cap 120 of 24" is just wrong to a reader, and this number exists to be read.
            "cap_now": {t: round(min(self.total, self.total * self.w(t) / aw), 2)
                        for t in set(self.weights) | set(self._want)},
        }


_WEIGHTS = _parse_weights(os.environ.get("RENDER_TENANT_WEIGHTS", "event:5,media:5"))

# Browser budget. MAX_PAGES is the real page ceiling — NOT SHOT_CONCURRENCY, which only binds when screenshots are on,
# and they are off by default {CONFIG.PY "NO_SHOT ... DEFAULT ON (2026-07-24): NO_SHOT gave 0.47 vs 0.21 pages/s"};
# render.py then holds a nullcontext instead of _shot_sem, so the effective limit is MAX_PAGES.
# [CONFIDENCE: CONFIRMED — `_shot_gate = contextlib.nullcontext() if config.NO_SHOT else runtime._shot_sem`].
_BROWSER_TOTAL = float(os.environ.get("RENDER_GATE_BROWSER_SLOTS", os.environ.get("IR_WATERCRAWL_MAX_PAGES", "24")))

# Download budget. Separate on purpose (see module docstring). It was sized small on the reasoning that "these are
# 300 MB-capped transfers, so the binding resource is bandwidth and memory, not slots" — and that reasoning does not
# survive measurement. 300 MB is the AUDIO cap; documents are capped at 60 MB {OFFICEALL/FETCH.PY "_MAX_BYTES =
# INT(OS.ENVIRON.GET(\"OFFICE_FETCH_MAX_BYTES\", \"60000000\"))   # 60MB"} and in practice run 0.03–6 MB. With the lane
# pinned full, the two resources it names were sitting idle:
# {IR-RENDER-16 2026-08-10 — fetch inflight 12/12 with 17–18 waiting across repeated samples, while 下行 21 Mbps
#  against a c2d-standard-8 egress ceiling near 16,000 Mbps (0.13%), mem 5,529 MB of 32,093 (17%), CPU 38% idle}
# So the cap was not protecting bandwidth or memory; it was throttling stage-2, and that queue is most of why document
# throughput sat near half the fleet's recent peak {DB 2026-08-10 enriched 311/h vs 608/h on 08-07}.
# What DOES bind now is CPU — the box was resized 16→8 cores — so the ceiling is raised in a measured step rather than
# removed, and the browser lane is watched beside it because stage-1 shares those cores.
# [CONFIDENCE: CONFIRMED 100% — bandwidth, memory and the standing queue were sampled together on the running VM;
#  after the raise the fetch queue went to 0 with 6 of 18 slots held and CPU fell rather than rose.]
_FETCH_TOTAL = float(os.environ.get("RENDER_GATE_FETCH_SLOTS", "18"))

browser = Gate("browser", _BROWSER_TOTAL, _WEIGHTS)
fetch = Gate("fetch", _FETCH_TOTAL, _WEIGHTS)

ENABLED = os.environ.get("RENDER_TENANT_GATE", "1") in ("1", "true", "yes")

print(f"[tenant_gate] {'ON' if ENABLED else 'OFF'} — browser={_BROWSER_TOTAL} fetch={_FETCH_TOTAL} "
      f"weights={_WEIGHTS} anon_w={ANON_W}", file=sys.stderr, flush=True)


def tenant_of(request) -> str:
    """The tenant for one aiohttp request. Lowercased so 'Media' and 'media' are one bucket rather than two silently
    separate shares."""
    return (request.headers.get(HEADER) or "").strip().lower() or ANON


class hold:
    """`async with tenant_gate.hold(gate, tenant, cost):` — acquire on enter, release on exit even if the handler
    raises. A leaked permit here is permanent and invisible: the gate would keep shrinking until that tenant deadlocks,
    with the process healthy and no error anywhere."""

    def __init__(self, gate: Gate, tenant: str, cost: float = 1.0):
        self.gate, self.tenant, self.cost = gate, tenant, cost

    async def __aenter__(self):
        if ENABLED:
            await self.gate.acquire(self.tenant, self.cost)
        return self

    async def __aexit__(self, *exc):
        if ENABLED:
            await self.gate.release(self.tenant, self.cost)
        return False


def snapshot() -> dict:
    """Both gates, for /health."""
    return {"enabled": ENABLED, "browser": browser.snapshot(), "fetch": fetch.snapshot()}
