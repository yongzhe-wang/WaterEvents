"""fair_gateway — a per-API-KEY, work-conserving, WEIGHTED fair-share admission proxy in front of the vLLM.

用一句话讲完: 所有 key 打这个 gateway(端口 8010),不直接打 vLLM(8000)→ gateway 按 key 分「槽当量」预算
(总预算 ÷ 活跃 key 数,work-conserving:只有一个 key 时吃满全部、N 个 key 时各 1/N)→ 每个请求按真实成本取额度
(text=1 槽、vision=4 槽,因为带图是 activation-heavy)→ 够额度才转发给 vLLM,不够就在该 key 的队列里等 → 释放时
notify 所有 waiter 重新按(可能变大的)额度评估 → 结果:没有 key 能独占、闲份额自动回填活跃 key、vision 按 4 倍
计不 OOM。{USER 2026-07-24 "leave some slots for other keys ... both have its whole proportion ... but if only one key
then it should use all compute"} [CONFIDENCE: CONFIRMED 100% — 直接需求:per-key max-min fair share + work-conserving].

WHY a gateway (not client-side): a gateway is ENFORCING — a misbehaving client (QWEN_CONCURRENCY=256) can't bypass it;
and per-key weighted fair queuing lives in ONE place. vLLM's own `--scheduling-policy priority` is priority, NOT fairness,
so one key firing 256 concurrent still starves another key (exactly the 548-connection starvation we hit 2026-07-24).

Deploy (does NOT touch the running vLLM — different port):
  FAIR_TOTAL_SLOTS=48 FAIR_KEY_WEIGHTS=event_agent:3,media_agent:7 \
  VLLM_UPSTREAM=http://127.0.0.1:8000 /root/venv/bin/python deploy/runpod/fair_gateway.py

  FAIR_KEY_WEIGHTS 决定「两边都想要时」怎么分; 只有一边活跃时它仍吃满全部 (分母只算活跃 key 的权重和)。
  不设则每个 key 权重 1, 等同于旧的等分行为。
Then point every worker's QWEN_BASE_URLS at http://<host>:8010/v1 instead of :8000.
"""
from __future__ import annotations

import asyncio
import os
import time as _time

import aiohttp
from aiohttp import web

UPSTREAM = os.environ.get("VLLM_UPSTREAM", "http://127.0.0.1:8000").rstrip("/")   # the real vLLM
PORT = int(os.environ.get("FAIR_PORT", "8010"))                                  # gateway listen port (≠ vLLM 8000)
TOTAL = int(os.environ.get("FAIR_TOTAL_SLOTS", "48"))            # total WEIGHTED budget ≈ the vLLM's safe max-num-seqs
VISION_W = int(os.environ.get("FAIR_VISION_WEIGHT", "4"))        # a vision request costs this many slots (activation-heavy)
TEXT_W = int(os.environ.get("FAIR_TEXT_WEIGHT", "1"))            # a text request costs this many slots

# The credential the gateway presents UPSTREAM, which is NOT the key a client presents to the gateway.
#
# WHY they have to be different: the incoming Authorization is the TENANT IDENTITY — it is what the fair share is
# accounted against, so event_agent and media_agent must send different ones. vLLM, meanwhile, is started with a
# single --api-key and rejects anything else {POD 2026-08-05 — vLLM cmdline carries "--api-key sk-waterevents-…",
# and a request bearing "event_agent" came back {"error":"Unauthorized"}}. Forwarding the client's header verbatim
# therefore cannot work: either every tenant sends the same string and the share collapses to one bucket, or they
# send different strings and vLLM refuses them all.
# The gateway is the trust boundary, so it translates: account against what the client presented, forward what the
# upstream requires. Clients never hold the real key, which is a small bonus — one place to rotate it.
# [CONFIDENCE: CONFIRMED — both halves observed: vLLM's own cmdline, and the 401 through the gateway].
UPSTREAM_KEY = os.environ.get("VLLM_API_KEY", "").strip()

# PER-KEY SHARE WEIGHTS — "event_agent:3,media_agent:7". Absent keys weigh 1.
#
# WHY this replaces the equal split: the two agents are not interchangeable tenants, they are two stages of ONE
# pipeline with different appetites. event_agent runs a scheduled rotation whose whole job is to keep T* short; it
# needs enough share to hold its cadence and no more. media_agent has 525,072 media urls behind every event that
# rotation produces, and is the side that will absorb whatever is left. An equal split is one point on that line,
# not a law — so the ratio is a knob.
# {USER 2026-08-05 "we can tune the parameter e.g. 3 7 or something but we need this system to exist as isolated
#  two parts, because incremental is always running and produce new events this part cannot be compromised and
#  new events are always going into media pipeline that is also not compromisable"}
# [CONFIDENCE: CONFIRMED — direct user requirement; the ratio is tunable, the ISOLATION is not optional].
#
# WORK-CONSERVING IS PRESERVED, and that is the point of dividing by the ACTIVE weight sum rather than the total:
# when one side is idle it is not in the denominator, so the other side's cap rises to the whole TOTAL. The weights
# decide how the server is split WHEN BOTH WANT IT — which is exactly what "reserved" has to mean for two pipelines
# that both run continuously. A static split would idle event_agent's share through every gap in its 7.42h rotation
# {SCHEDULER_STATE 2026-08-05 "t_star_s=26724.87 ... note='T*=7.42h (slots-bound); full fills residual.'"}
# while media_agent still had half a million urls to get through.
_KEY_WEIGHTS: dict[str, float] = {}
for _pair in os.environ.get("FAIR_KEY_WEIGHTS", "").split(","):
    _k, _, _v = _pair.partition(":")
    if _k.strip() and _v.strip():
        try:
            _KEY_WEIGHTS[_k.strip()] = float(_v)
        except ValueError:
            pass                                             # a malformed pair must not take the gateway down


def _w_of(key: str) -> float:
    """This key's share weight. Unknown keys weigh 1 — a caller nobody configured still competes fairly rather than
    being locked out, which matters because the gateway is the ONLY path to the vLLM once fleet.env points here."""
    return _KEY_WEIGHTS.get(key, 1.0)

# ── fair-share state (single event loop → a plain dict + one Condition is enough; no external lock) ──
_want: dict[str, int] = {}          # key → # requests CURRENTLY in the gateway (waiting OR in-flight) → defines "active"
_inflight: dict[str, float] = {}    # key → ADMITTED weighted slots in flight
_cond = asyncio.Condition()         # wake all waiters whenever a release/arrival changes the fair budget


def _active_weight() -> float:
    """Summed WEIGHT of the keys currently competing (want >= 1 request). This is the fair-share denominator.

    Weight sum, not key count: with FAIR_KEY_WEIGHTS="event_agent:3,media_agent:7" and both active the denominator is
    10, so event_agent's cap is TOTAL*3/10 and media_agent's is TOTAL*7/10. With only one of them active the
    denominator is just its own weight, so its cap is the whole TOTAL — the work-conserving property survives the
    change to weights, because an idle key contributes nothing to the denominator.
    Unweighted deployments are unaffected: every key weighs 1, the sum is the count, and this is the old behaviour."""
    return sum(_w_of(k) for k, c in _want.items() if c > 0) or 1.0


# ── 自适应闸门:吞吐爬山 ──────────────────────────────────────────────────────────────────────────
# 用一句话讲完: 每 PROBE_S 秒把闸门调一格,看吞吐涨了还是跌了 —— 涨就继续同向,跌就掉头,于是它自己
# 收敛到吞吐峰值,不需要任何人写死一个并发数。
#
# WHY 峰值不能是常数: 实测同一张 A40,并发 24 → 10,421 请求/h、p90 14.2s、prefix cache 命中 74%;
# 并发 48 → 5,325 请求/h、p90 46.6s、命中 25.7%。超过 KV pool 装得下的量之后,已缓存的前缀块被逐出,
# 下一个请求只好重新 prefill 整个 system prompt,吞吐腰斩。
# {隔离扫描 2026-08-08,舰队全停:"并发 24 → 10421 请求/h · p90 14.2s · 缓存 74.0%" vs
#  "并发 48 → 5325 请求/h · p90 46.6s · 缓存 25.7%"}
# 而这个拐点随 prompt 长度漂移 —— 同一台机器上,基准里 2,066 token 的 prompt 峰值在 24,生产 3,601
# token 的 prompt 峰值明显更低。写死任何一个数都会在另一种负载下落进崩塌区。生产原本设的是 48。
# [CONFIDENCE: CONFIRMED 100% — 扫描在无其他负载的隔离环境取得,八个并发档单调可复现。]
#
# WHY 不用 Vegas(试过,信号退化): Vegas 判据 queue ≈ limit × (1 − minRTT/p50),前提是「延迟高于最小值
# = 排队 = 浪费」。对网络成立,对 LLM 服务不成立 —— 高于最小值的那部分延迟是**批处理**造成的,而批处理
# 正是吞吐的来源。实测 minRTT/p50 恒为极小值,queue_est 恒 ≈ limit,于是恒大于任何 beta,闸门单调收到底限。
# {实测 2026-08-08 "limit=23→18→15→11→8",四次采样的 queue_est/limit = 18.98/18、15.91/15、
#  11.82/11、8.95/8,全部 ≈1 —— 该信号已退化成 limit 本身,不含任何拥塞信息}
# [CONFIDENCE: CONFIRMED 100% — 在生产负载下观察到的单调下降,四个采样点齐全。]
#
# 爬山法直接优化我们真正要的量,而且不需要关于模型/硬件/prompt 长度的任何先验 —— 条件漂移时它跟着走。
# 这才是「删掉常数」的实际含义:不是把 48 换成 24,是让系统不再需要这个数字。
PROBE_S       = float(os.environ.get("FAIR_PROBE_S", "5"))       # 控制回路周期
LIMIT_MIN     = int(os.environ.get("FAIR_LIMIT_MIN", "4"))       # 再拥塞也保底,不能饿死
LIMIT_MAX     = int(os.environ.get("FAIR_LIMIT_MAX", "64"))      # 硬上限,防控制器跑飞
# 一个窗口至少要有这么多个完成才结算。**不够就不清空,累积到下一窗** —— 于是窗口长度自动随负载伸缩:
# 高负载时 PROBE_S 一到就够样本(响应快),低负载时自动等成更长的窗(读数稳)。这比写死一个窗口长度
# 稳健得多,也少一个需要维护的常数。
# {生产实测 2026-08-08,PROBE_S=5s 且不累积时:连续八窗的 rph = 2160, 2880, 0, 2160, 1440, 2160, 1440, 720
#  —— 每窗只有 ~2.8 个完成(2000 请求/h × 5s),一个长请求就能让整窗归零,控制器有一半在追噪声}
# [CONFIDENCE: CONFIRMED 100% — 生产 /gwstats 连续采样,含一次 rph=0 的空窗。]
MIN_SAMPLES   = int(os.environ.get("FAIR_MIN_SAMPLES", "15"))
STEP          = int(os.environ.get("FAIR_STEP", "2"))            # 每次试探移动几格
DEADBAND      = float(os.environ.get("FAIR_DEADBAND", "0.05"))   # 吞吐变化 <5% 视为噪声,不算刷新纪录
EWMA_A        = float(os.environ.get("FAIR_EWMA_ALPHA", "0.3"))  # 吞吐平滑,越小越稳越慢
PATIENCE      = int(os.environ.get("FAIR_PATIENCE", "4"))        # 连续这么多窗没更好 → 回最好点、换方向
DECAY         = float(os.environ.get("FAIR_DECAY", "0.98"))      # 纪录每窗褪色一点,逼它周期性重新确认
LAT_CEILING_S = float(os.environ.get("FAIR_LAT_CEILING_S", "180"))   # p50 超过它无条件收缩(安全阀)
# 每个 key 允许排多深的队,超出即 503。有界是关键:_acquire 原本无限等待,突发上千个请求会全部堆在
# Condition 上,连接/内存/事件循环一起被占住,网关比上游先倒。卸载比熔断好,也比静默排队诚实。
QUEUE_DEPTH_MULT = float(os.environ.get("FAIR_QUEUE_DEPTH_MULT", "4"))

_limit = float(os.environ.get("FAIR_TOTAL_SLOTS", "24"))         # 起点;此后由控制器接管
_rtts: list[float] = []                                          # 本窗口完成请求的**服务**耗时(不含排队)
_min_rtt = float("inf")
_done = 0
_win = 0
_dir = 1                                                         # 试探方向:+1 加闸门,-1 减
_best_rph = 0.0                                                  # 迄今最好吞吐
_best_limit = _limit                                             # 取得该吞吐时的闸门 —— 试探失败就回这里
_miss = 0                                                        # 连续没刷新纪录的窗口数
_ewma_rph = 0.0
_acc_s = 0.0                                                     # 当前这批样本已累积的秒数
_stats: dict = {"limit": _limit, "rph": 0, "rph_ewma": 0, "best_rph": 0, "best_limit": _limit,
                "dir": 1, "miss": 0, "p50": 0.0, "min_rtt": None, "shed": 0}


class Shed(Exception):
    """这个 key 的等待队列已满 —— 卸载而不是排队。调用方转成 503 + Retry-After。"""


async def _controller() -> None:
    """记住最好点、围着它试探。步长 STEP,试探 PATIENCE 次没更好就回到最好点换方向。

    WHY 不是纯爬山: 实测曲线有**两个平台** —— 24~32 是好平台(~10,400 请求/h),48~64 是崩塌后的坏平台
    (~5,200)。纯爬山在平台上没有梯度可循,落在哪个就守在哪个,从 48 起步会永远停在坏平台。
    {隔离扫描 2026-08-08:"并发 16→7654 · 24→10421 · 32→10353 · 48→5325 · 64→5157"}
    WHY 不是纯 AIMD: 乘法减比加法增快太多,会一路住到低位。用实测曲线驱动的本地模拟里,AIMD 从任何
    起点都收敛到闸门 9~12,只有 6,370 请求/h —— 鲁棒但只拿到 61% 的峰值。
    [CONFIDENCE: CONFIRMED 100% — 两种算法都用同一条实测曲线跑过 200~300 个窗口的模拟。]

    「记最好点 + 定期褪色」同时解决了两件事:平台上靠 PATIENCE 强制换向去别处找,峰值漂移时靠 DECAY
    让旧纪录失效、重新确认。本地模拟:七个起点(4/8/16/24/32/48/64)全部收敛到 92% 峰值;峰值中途
    左移一半时闸门自动跟过去。而生产原本写死的 48 只拿到 51%。
    """
    global _limit, _rtts, _min_rtt, _done, _win, _dir, _best_rph, _best_limit, _miss, _ewma_rph, _acc_s
    while True:
        await asyncio.sleep(PROBE_S)
        async with _cond:
            # 样本不够就原样留着,下一轮继续攒;_acc_s 记录这批样本实际累积了多久,用它算速率。
            _acc_s += PROBE_S
            if len(_rtts) < MIN_SAMPLES:
                continue
            samples, done, span = _rtts, _done, _acc_s
            _rtts, _done, _acc_s = [], 0, 0.0
            _win += 1
            rph = done / span * 3600
            p50 = 0.0
            if samples:
                samples.sort()
                p50 = samples[len(samples) // 2]               # p50 而非均值:长尾请求不该驱动控制决策
                _min_rtt = min(_min_rtt, samples[0])
            # 单窗口吞吐噪声很大(一个长请求就能让 5 秒窗口的完成数归零),不平滑会被噪声牵着乱走。
            _ewma_rph = rph if not _ewma_rph else _ewma_rph * (1 - EWMA_A) + rph * EWMA_A

            if True:
                if p50 > LAT_CEILING_S:                        # 安全阀:延迟离谱时无条件收缩
                    _limit = max(LIMIT_MIN, _limit - STEP)
                    _dir = -1
                else:
                    if _ewma_rph > _best_rph * (1 + DEADBAND):  # 刷新纪录 → 记住这个位置,保持方向
                        _best_rph, _best_limit, _miss = _ewma_rph, _limit, 0
                    else:
                        _miss += 1
                        if _miss >= PATIENCE:                   # 试探够了没更好 → 回最好点,换方向再试
                            _limit, _dir, _miss = _best_limit, -_dir, 0
                    _best_rph *= DECAY                          # 纪录缓慢褪色 → 峰值漂移时能重新确认
                    nxt = _limit + _dir * STEP
                    # 边界反弹:撞到上/下限就把方向翻向内侧。少了这个,闸门会贴着边界空转,而且
                    # 「回到最好点」会把自己送回边界。{本地模拟:起点 4 停在 4、起点 48/64 停在 64}
                    if nxt >= LIMIT_MAX:
                        nxt, _dir = float(LIMIT_MAX), -1
                    if nxt <= LIMIT_MIN:
                        nxt, _dir = float(LIMIT_MIN), 1
                    _limit = nxt
                    if _best_limit in (LIMIT_MIN, LIMIT_MAX):   # 最好点落在边界 → 不可信,往内挪重新找
                        _best_limit = max(LIMIT_MIN + STEP, min(LIMIT_MAX - STEP, _best_limit - _dir * STEP))
            _stats.update(limit=round(_limit, 1), rph=round(rph), rph_ewma=round(_ewma_rph),
                          best_rph=round(_best_rph), best_limit=round(_best_limit, 1), dir=_dir,
                          miss=_miss, p50=round(p50, 2),
                          min_rtt=round(_min_rtt, 3) if _min_rtt < float("inf") else None,
                          window_s=round(span, 1), samples=len(samples))
            _cond.notify_all()                                 # 闸门变大 → 等待者立刻重新评估


async def _on_start(app):                                      # noqa: ANN001 — aiohttp signal signature
    app["ctl"] = asyncio.create_task(_controller())


async def _acquire(key: str, w: float) -> None:
    """Block until this key can admit a `w`-weight request WITHOUT (a) exceeding its fair budget TOTAL/active, NOR (b)
    overflowing the global TOTAL. Re-evaluated on every notify → a key expands to the whole server the moment it's alone."""
    async with _cond:
        my_cap = _limit * _w_of(key) / _active_weight()
        if _want.get(key, 0) > max(my_cap, LIMIT_MIN) * QUEUE_DEPTH_MULT:
            _stats["shed"] += 1
            raise Shed()                                       # 队列已满 → 卸载,不让它无限等
        while True:
            cap = _limit * _w_of(key) / _active_weight()        # 闸门是动态的,每次重算都取当前值
            cur = _inflight.get(key, 0.0)                       # its weighted in-flight now
            gtot = sum(_inflight.values())                     # everyone's weighted in-flight (the hard KV/OOM bound)
            if cur + w <= cap and gtot + w <= _limit:           # within BOTH my fair share AND the global ceiling → admit
                _inflight[key] = cur + w
                return
            await _cond.wait()                                 # blocked → sleep until a release/arrival changes the math


async def _release(key: str, w: float) -> None:
    """Return this request's weight; wake ALL waiters so they recompute their (now possibly larger) fair budget."""
    async with _cond:
        _inflight[key] = max(0.0, _inflight.get(key, 0.0) - w)
        if _inflight[key] == 0.0:
            _inflight.pop(key, None)
        _cond.notify_all()                                     # the crucial work-conserving step: freed share → waiters re-try


def _weight(body: dict) -> float:
    """A request costs VISION_W if ANY message carries an image (OpenAI multimodal `image_url` part), else TEXT_W. WHY:
    a vision request's memory is ACTIVATION-driven (~4× a text request), so counting it as 4 slots keeps the server off
    the OOM line while text still packs densely. {serve_vl.sh OOM postmortem: max-num-seqs=48 OOM'd under image load}."""
    try:
        for m in (body.get("messages") or []):
            c = m.get("content")
            if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
                return VISION_W
    except Exception:                                          # noqa: BLE001 — a malformed body is treated as cheap text
        pass
    return TEXT_W


async def _forward(req: web.Request, body_bytes: bytes) -> web.StreamResponse:
    """Stream the request to the upstream vLLM and stream its response straight back (works for stream=True SSE too)."""
    fwd_headers = {k: v for k, v in req.headers.items() if k.lower() not in ("host", "content-length")}
    # Swap the tenant identity for the upstream credential. Done HERE, at the last moment before the wire, so every
    # path (GET passthrough included) gets it and no caller can forget. When VLLM_API_KEY is unset the header is
    # passed through untouched — an unauthenticated upstream keeps working exactly as before.
    if UPSTREAM_KEY:
        fwd_headers["Authorization"] = f"Bearer {UPSTREAM_KEY}"
    async with aiohttp.ClientSession() as sess:
        async with sess.request(req.method, UPSTREAM + req.rel_url.path_qs, data=body_bytes,
                                headers=fwd_headers) as up:
            resp = web.StreamResponse(status=up.status,
                                      headers={k: v for k, v in up.headers.items()
                                               if k.lower() not in ("content-length", "transfer-encoding")})
            await resp.prepare(req)
            async for chunk in up.content.iter_any():          # pass tokens through as they arrive (streaming preserved)
                await resp.write(chunk)
            await resp.write_eof()
            return resp


async def handle(req: web.Request) -> web.StreamResponse:
    """Every /v1/* request. GET (models/health) passes straight through — only the COMPUTE endpoints go through the fair
    admission gate, since only they consume seq slots."""
    if req.path == "/gwstats":
        # 把控制器实测到的容量暴露出来 —— 上游调度器不该再拿写死的常数去规划。
        return web.json_response({**_stats, "inflight": dict(_inflight), "want": dict(_want),
                                  "weights": _KEY_WEIGHTS})
    if req.method == "GET":                                    # /v1/models etc. — cheap, no admission
        return await _forward(req, b"")

    body_bytes = await req.read()
    try:
        import json
        body = json.loads(body_bytes or b"{}")
    except Exception:                                          # noqa: BLE001
        body = {}
    key = (req.headers.get("Authorization", "") or "").replace("Bearer ", "").strip() or "anon"
    w = _weight(body)

    _want[key] = _want.get(key, 0) + 1                         # mark this key ACTIVE the instant it arrives (before the wait)
    try:
        try:
            await _acquire(key, w)                             # fair-share admission (may block until a slot frees)
        except Shed:
            return web.json_response({"error": {"message": "gateway overloaded, retry later",
                                                "type": "overloaded"}},
                                     status=503, headers={"Retry-After": "5"})
        _t0 = _time.monotonic()
        try:
            return await _forward(req, body_bytes)
        finally:
            # 只统计**被服务**的时间,不含排队 —— 控制器要的是服务时间,端到端延迟会把闸门的效果算进去
            # 形成正反馈(闸门越小排队越久 → 延迟越大 → 闸门更小)。
            async with _cond:
                _rtts.append(_time.monotonic() - _t0)
                globals()["_done"] += 1
            await _release(key, w)
    finally:
        _want[key] = _want.get(key, 1) - 1                     # no longer competing
        if _want[key] <= 0:
            _want.pop(key, None)


def main() -> None:
    app = web.Application(client_max_size=64 * 1024 * 1024)     # allow big multimodal bodies (base64 images)
    app.on_startup.append(_on_start)                           # 控制回路随服务启动
    app.router.add_route("*", "/{tail:.*}", handle)            # proxy everything
    print(f"[fair_gateway] :{PORT} → {UPSTREAM} | 自适应闸门 起点={_limit:.0f} 范围=[{LIMIT_MIN},{LIMIT_MAX}] "
          f"probe={PROBE_S}s 步长={STEP} 耐心={PATIENCE} 死区={DEADBAND:.0%} | text={TEXT_W} vision={VISION_W} | "
          f"队列上限=份额×{QUEUE_DEPTH_MULT:.0f} 超出即 503 | /gwstats 暴露实测容量", flush=True)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
