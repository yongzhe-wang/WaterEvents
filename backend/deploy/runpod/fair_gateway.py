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


async def _acquire(key: str, w: float) -> None:
    """Block until this key can admit a `w`-weight request WITHOUT (a) exceeding its fair budget TOTAL/active, NOR (b)
    overflowing the global TOTAL. Re-evaluated on every notify → a key expands to the whole server the moment it's alone."""
    async with _cond:
        while True:
            cap = TOTAL * _w_of(key) / _active_weight()        # this key's CURRENT fair budget (weighted, work-conserving)
            cur = _inflight.get(key, 0.0)                       # its weighted in-flight now
            gtot = sum(_inflight.values())                     # everyone's weighted in-flight (the hard KV/OOM bound)
            if cur + w <= cap and gtot + w <= TOTAL:            # within BOTH my fair share AND the global ceiling → admit
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
        await _acquire(key, w)                                 # fair-share admission (may block until a slot frees)
        try:
            return await _forward(req, body_bytes)
        finally:
            await _release(key, w)
    finally:
        _want[key] = _want.get(key, 1) - 1                     # no longer competing
        if _want[key] <= 0:
            _want.pop(key, None)


def main() -> None:
    app = web.Application(client_max_size=64 * 1024 * 1024)     # allow big multimodal bodies (base64 images)
    app.router.add_route("*", "/{tail:.*}", handle)            # proxy everything
    print(f"[fair_gateway] :{PORT} → {UPSTREAM} | TOTAL={TOTAL} weighted slots (text={TEXT_W}, vision={VISION_W}) | "
          f"per-key fair share = TOTAL/active_keys, work-conserving", flush=True)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
