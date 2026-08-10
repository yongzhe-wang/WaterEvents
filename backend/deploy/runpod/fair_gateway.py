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


# ── 自适应闸门 ───────────────────────────────────────────────────────────────────────────────────
# 用一句话讲完: 每个窗口从 vLLM 的 /metrics 读一次累计 prompt token,算出 prefill tok/s —— 那是 GPU 实际
# 做功的直接度量 —— 然后围着「取得过最好 prefill 速率的那个闸门位置」小步试探,试探失败就退回去换方向。
#
# WHY 目标必须是 prefill tok/s 而不是 请求/小时: 请求数会因为**和闸门完全无关**的原因变化。生产实测,
# 同一天内爬虫走到一批更长的页面上,每请求 prompt 从 3,601 token 涨到 7,835 token,请求/小时随之从
# ~2,400 掉到 1,160 —— 而 prefill 速率纹丝不动(2,400 → 2,525 tok/s)。GPU 每秒做的功一模一样。
# {生产实测 2026-08-08 90 秒采样: "29 个请求 · prompt 227213 tok" → 每请求 7,835 tok · prefill 2,525 tok/s;
#  同日早些时候 "每请求 prompt 3,601 tok · prefill 2,400 tok/s"}
# 用请求数当目标的后果是实际发生过的事故:prompt 变长 → 请求数掉 → 控制器读成「闸门太大」→ 收缩 →
# 触发卸载 → 客户端重试 → 队列更长 → 更多卸载 → 吞吐真的掉下去 → 控制器继续收缩。自我强化的螺旋。
# {生产实测 2026-08-08 崩塌时 /gwstats: "limit=16 best_limit=24 dir=-1 shed=3007 min_rtt=0.012"}
# [CONFIDENCE: CONFIRMED 100% — 两组 token 计数都取自 vLLM 自己的 /metrics;螺旋的每一环都在 /gwstats 里。]
#
# WHY 峰值不能写死: 隔离扫描(舰队全停)在 2,066-token 的 prompt 上峰值在并发 24 → 6,445 tok/s,
# 并发 48 掉到 3,295;而生产 3,601-token 的 prompt 峰值明显更低。同一台机器,同一个模型,不同的 prompt
# 长度就是不同的峰。任何写下来的数字都会在另一种负载下落进崩塌区 —— 生产原本写的是 48。
# {隔离扫描 2026-08-08: 并发 1/4/8/16/24/32/48/64 → prefill 1037/4104/3578/4734/6445/6404/3295/3192 tok/s}
#
# 算法选型也是测出来的,不是挑出来的。用上面那条实测曲线跑 250 窗模拟:
#   Vegas(排队深度)  信号退化 —— minRTT/p50 恒极小,queue_est ≈ limit,闸门单调收到底限。
#                     生产实测证实:23→18→15→11→8。LLM 服务里高于最小延迟的部分是**批处理**,不是浪费。
#   纯爬山            曲线有两个平台(好的 24–32、崩塌后的 48–64),平台上没梯度,落哪守哪。
#   AIMD              从任何起点都收敛,但停在低位,只拿 61% 峰值。
#   记最好点 + 褪色    七个起点全部收敛到 92%;加 20% 噪声仍有 89–91%;负载整体变重时闸门不动。← 用这个
# [CONFIDENCE: CONFIRMED 100% — 四种算法都用同一条实测曲线模拟过,Vegas 那条另有生产实测佐证。]
PROBE_S       = float(os.environ.get("FAIR_PROBE_S", "10"))      # 控制回路周期
LIMIT_MIN     = int(os.environ.get("FAIR_LIMIT_MIN", "4"))       # 再拥塞也保底,不能饿死
LIMIT_MAX     = int(os.environ.get("FAIR_LIMIT_MAX", "64"))      # 硬上限,防控制器跑飞
STEP          = int(os.environ.get("FAIR_STEP", "2"))            # 每次试探移动几格
DEADBAND      = float(os.environ.get("FAIR_DEADBAND", "0.05"))   # 变化 <5% 不算刷新纪录
EWMA_A        = float(os.environ.get("FAIR_EWMA_ALPHA", "0.3"))  # 平滑,越小越稳越慢
PATIENCE      = int(os.environ.get("FAIR_PATIENCE", "4"))        # 连续这么多窗没更好 → 回最好点、换方向
DECAY         = float(os.environ.get("FAIR_DECAY", "0.98"))      # 纪录每窗褪色,峰值漂移时能重新确认
MIN_TOKENS    = int(os.environ.get("FAIR_MIN_TOKENS", "20000"))  # 一窗至少要有这么多 prompt token 才结算
# 卸载的判据是**估计等待时间**,不是队长。排队本身没有错 —— 错的是排一个客户端等不到的队:客户端
# 的 timeout 是 120s,如果队尾要等 150s,那让它排下去只是把「立刻知道」换成「120 秒后超时」,而且
# 那 120 秒里它还占着一条连接。诚实卸载让客户端能立刻退避重试。
# 等待时间 = 该 key 的队长 ÷ 该 key 的服务速率,两个量控制器都在测,所以这里不需要再拍一个数字。
#
# WHY 不是「队长 × 常数」: 之前的阈值是 max(份额,4)×4,它随闸门缩 —— 闸门降 → 份额降 → 阈值降 →
# 更多 503 → 客户端重试 → 队列更长 → 吞吐掉 → 闸门继续降。自我强化的螺旋。
# {生产实测 2026-08-08 螺旋跑满时 shed 累计 3007,闸门被压到 16,而它自己记录的最好点是 24;
#  同期正常运行时每 key 的 want 只有 18–48}
# [CONFIDENCE: CONFIRMED 100% — /gwstats 里 limit=16 best_limit=24 shed=3007 同时出现。]
SHED_WAIT_S   = float(os.environ.get("FAIR_SHED_WAIT_S", "120"))   # 与客户端 QWEN_TIMEOUT_S 对齐
# 内存兜底:即使速率还没测出来,也不能让队列无限长。一条连接加它的请求体大约几十 KB,一千条是几十 MB。
SHED_HARD_CAP = int(os.environ.get("FAIR_SHED_HARD_CAP", "1000"))

_limit = float(os.environ.get("FAIR_TOTAL_SLOTS", "28"))         # 起点;此后由控制器接管
_done = 0
_win = 0
_dir = 1
_best_rate = 0.0                                                 # 迄今最好的 prefill tok/s
_best_limit = _limit                                             # 取得该速率时的闸门 —— 试探失败就回这里
_miss = 0
_ewma_rate = 0.0
_prev_tok = None                                                 # 上一窗的累计 prompt token
_acc_s = 0.0
_stats: dict = {"limit": _limit, "prefill_tok_s": 0, "ewma": 0, "best_rate": 0, "best_limit": _limit,
                "dir": 1, "miss": 0, "rph": 0, "shed": 0, "window_s": 0.0,
                "tok_per_call": 0, "capacity_cph": 0}


class Shed(Exception):
    """这个 key 排队太深 —— 卸载而不是无限等。调用方转成 503 + Retry-After。"""


async def _prompt_tokens() -> float | None:
    """从上游 vLLM 的 /metrics 读累计 prompt token。读不到返回 None(控制器该轮不动作)。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(UPSTREAM + "/metrics", timeout=aiohttp.ClientTimeout(total=5)) as r:
                for line in (await r.text()).splitlines():
                    if line.startswith("vllm:prompt_tokens_total"):
                        return float(line.split()[-1])
    except Exception:                                            # noqa: BLE001 — 上游抖动不该让控制器崩
        return None
    return None


async def _controller() -> None:
    """记最好点 + 围着它试探。步长 STEP,慢加慢减,避免和上游的批处理节奏共振。"""
    global _limit, _done, _win, _dir, _best_rate, _best_limit, _miss, _ewma_rate, _prev_tok, _acc_s
    while True:
        await asyncio.sleep(PROBE_S)
        tok = await _prompt_tokens()
        if tok is None:
            continue
        async with _cond:
            _acc_s += PROBE_S
            if _prev_tok is None:
                _prev_tok = tok
                continue
            d_tok = tok - _prev_tok
            # 样本不够就不结算,累积到下一窗 —— 窗口长度自动随负载伸缩:忙时短(响应快)、闲时长(读数稳)。
            if d_tok < MIN_TOKENS:
                continue
            span, done = _acc_s, _done
            _prev_tok, _acc_s, _done, _win = tok, 0.0, 0, _win + 1
            rate = d_tok / span                                  # prefill tok/s —— 这才是 GPU 做功的度量
            _ewma_rate = rate if not _ewma_rate else _ewma_rate * (1 - EWMA_A) + rate * EWMA_A

            if _ewma_rate > _best_rate * (1 + DEADBAND):         # 刷新纪录 → 记住这个位置,保持方向
                _best_rate, _best_limit, _miss = _ewma_rate, _limit, 0
            else:
                _miss += 1
                if _miss >= PATIENCE:                            # 试探够了没更好 → 回最好点,换方向再试
                    _limit, _dir, _miss = _best_limit, -_dir, 0
            _best_rate *= DECAY                                  # 纪录缓慢褪色 → 峰值漂移时重新确认
            nxt = _limit + _dir * STEP
            # 边界反弹:撞到上/下限就把方向翻向内侧。少了它,闸门会贴着边界空转,而且「回到最好点」
            # 会把自己送回边界。{本地模拟:起点 4 停在 4、起点 48/64 停在 64}
            if nxt >= LIMIT_MAX:
                nxt, _dir = float(LIMIT_MAX), -1
            if nxt <= LIMIT_MIN:
                nxt, _dir = float(LIMIT_MIN), 1
            _limit = nxt
            if _best_limit in (LIMIT_MIN, LIMIT_MAX):            # 最好点落在边界 → 不可信,往内挪重新找
                _best_limit = max(LIMIT_MIN + STEP, min(LIMIT_MAX - STEP, _best_limit - _dir * STEP))

            # capacity_cph —— 给上游调度器用的「每小时能跑多少次调用」。
            # 直接报 rph 是不对的:那是**观测到的**速率,受需求限制,闲的时候会很低,而调度器会把它当
            # 容量,于是从自己的低需求推断出「上游没能力」,然后进一步限制自己。这个反馈回路真实存在:
            # {SCHEDULER_STATE 2026-08-08 "t_star=93.4h binding=vlm c_v=170.5" —— 而同期网关实测
            #  prefill 2,631 tok/s、3,220 请求/h}
            # 正确的换算是拿**最好的 prefill 速率**除以当前每请求的 token 数:前者是这块卡的能力(和需求
            # 无关),后者是当下负载的形状。两者相除才是「这种 prompt 下每小时能跑多少次」。
            # [CONFIDENCE: CONFIRMED 100% — 每请求 token 数在同一天内从 3,601 变到 7,835 又回到 2,942,
            #  而 prefill 速率始终在 2,400–2,600;固定的 calls/hour 常数在任何一种形状下都是错的。]
            tok_per_call = rate / max(done / span, 1e-9)
            cap_cph = (_best_rate / tok_per_call * 3600) if tok_per_call > 0 else 0
            _stats.update(limit=round(_limit, 1), prefill_tok_s=round(rate), ewma=round(_ewma_rate),
                          best_rate=round(_best_rate), best_limit=round(_best_limit, 1), dir=_dir,
                          miss=_miss, rph=round(done / span * 3600), window_s=round(span, 1),
                          tok_per_call=round(tok_per_call), capacity_cph=round(cap_cph))
            _cond.notify_all()                                   # 闸门变大 → 等待者立刻重新评估


async def _on_start(app):                                        # noqa: ANN001 — aiohttp signal signature
    app["ctl"] = asyncio.create_task(_controller())


async def _acquire(key: str, w: float) -> None:
    """Block until this key can admit a `w`-weight request WITHOUT (a) exceeding its fair budget TOTAL/active, NOR (b)
    overflowing the global TOTAL. Re-evaluated on every notify → a key expands to the whole server the moment it's alone."""
    async with _cond:
        queued = _want.get(key, 0)
        if queued > SHED_HARD_CAP:                               # 内存兜底,与速率无关
            _stats["shed"] += 1
            raise Shed()
        # 该 key 的服务速率 = 全局完成率 × 它占的份额;据此估计队尾要等多久。
        # 速率还没测出来(启动初期)时 rate=0,est 为无穷 —— 那时**不卸载**,交给硬上限兜底,
        # 免得冷启动阶段把正常流量当成过载。
        svc = _stats.get("rph", 0) / 3600.0
        # ★ 服务器【完全空闲】时永不按速率卸载 —— 没有在飞请求就没有队列,估算等待毫无意义。
        #
        # 少了这一条会死锁,而且真实发生过、持续了两天:
        #   全部卸载 → 没有请求完成 → rph 塌到接近 0 → est_wait 爆表 → 继续全部卸载 → …
        # 2026-08-08 11:51 起 ir-media-8 的采集队列每小时约 800-1000 次调用全部 503,
        # 同一时刻 /gwstats 是 inflight={} want={} limit=64、后端 vLLM num_requests_running=0、
        # A40 利用率 0% —— 一个请求都没在跑,却已累计拒绝 497,601 次。
        #
        # 算式复现(与线上数值逐位吻合):
        #   rph=18 → svc=0.005 req/s;新到的请求在 handle() 里已把 _want[key] 顶到 1;
        #   est_wait = 1 / 0.005 = 200.0s > SHED_WAIT_S(120) → Shed。
        # 上面那条冷启动护栏守的是 `svc > 0`,但塌缩后的 rph 是**非零小值**,守卫放行,照拒不误。
        # {POD /gwstats 2026-08-10: "shed":497601, "rph":18, "last_est_wait":200.0, "inflight":{}}
        # [CONFIDENCE: CONFIRMED 100% — 手算 200.0 与线上 last_est_wait 完全一致;
        #  重启进程后 rph 归零、`svc > 0` 守卫生效,POST 立刻恢复 3/3 HTTP 200]
        if svc > 0 and queued > 0 and _inflight:
            share = _w_of(key) / _active_weight()
            est_wait = queued / max(svc * share, 1e-6)
            if est_wait > SHED_WAIT_S:
                _stats["shed"] += 1
                _stats["last_est_wait"] = round(est_wait, 1)
                raise Shed()
        while True:
            cap = _limit * _w_of(key) / _active_weight()        # this key's CURRENT fair budget (weighted, work-conserving)
            cur = _inflight.get(key, 0.0)                       # its weighted in-flight now
            gtot = sum(_inflight.values())                     # everyone's weighted in-flight (the hard KV/OOM bound)
            if cur + w <= cap and gtot + w <= _limit:            # within BOTH my fair share AND the global ceiling → admit
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
        return web.json_response({**_stats, "inflight": dict(_inflight), "want": dict(_want)})
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
        try:
            return await _forward(req, body_bytes)
        finally:
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
          f"目标=prefill tok/s probe={PROBE_S}s 步长={STEP} | text={TEXT_W} vision={VISION_W} | "
          f"卸载: 估计等待>{SHED_WAIT_S:.0f}s 或队长>{SHED_HARD_CAP} | /gwstats", flush=True)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
