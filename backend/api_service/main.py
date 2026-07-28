"""api_service — the PUBLIC read-only HTTP surface for WaterEvents. Today's pulse counter is its first endpoint.

用一句话讲完: 一个独立的 aiohttp 进程(:8090)对外开放 GET /today/pulse → 它不连 Postgres,而是转调 PostgREST 的
`rpc/today_pulse` 函数,加上 10 秒进程内缓存和 per-IP 限流,把结果原样返回 → 所以无论这个公开端点被打多狠,爬虫
fleet 的 Supavisor 连接一条都不会被占用。起停这个进程完全不影响 fleet 和 webapp。

WHY it talks to PostgREST instead of opening its own Postgres pool: the fleet reaches Postgres through Supavisor
(transaction mode :6543); a public endpoint sharing that pool can starve the crawler just by being hammered. PostgREST
runs a SEPARATE pool under the `authenticator` role, so this service consumes zero Supavisor capacity — and needs no
database password, which matters in a repo that still has one hardcoded in eight files.
{MEASURED 2026-07-28 pg_stat_activity "postgres/Supavisor 7 idle + 1 active (fleet) vs authenticator/PostgREST 5 idle"}
{USER 2026-07-28 "we need to separate the supavisor"}
[CONFIDENCE: CONFIRMED 100% — the two pools are distinct usename/application_name sets in pg_stat_activity].

WHY aiohttp and not FastAPI: the VM's venv already ships aiohttp/httpx/asyncpg but NOT fastapi/uvicorn, and this box is
currently running the live crawl fleet. Building on what is installed avoids a pip install on a production host for a
service this small. {MEASURED 2026-07-28 ir-media-8 "fastapi MISSING | uvicorn MISSING | aiohttp OK | httpx OK"}
[CONFIDENCE: CONFIRMED 100% — import-probed on the VM's own interpreter].

Run:  PORT=8090 python -m api_service.main      (PYTHONPATH must point at backend/, as it does for the fleet)
"""
from __future__ import annotations

import json
import os
import time
from collections import deque

from aiohttp import ClientSession, ClientTimeout, web

# PostgREST endpoint + the PUBLISHABLE (anon) key. Public by design — it is the same key the browser already ships in
# the SPA bundle, and anon holds SELECT-only rights (a PATCH on work_queue returns 42501 "permission denied"), so
# exposing it here grants nothing new. Env-overridable so a project/key rotation is a config change, not a code edit.
# {frontend/lib/_db.js "anon key is public-by-design"} {MEASURED 2026-07-28 "PATCH work_queue as anon → 401 / 42501"}
# [CONFIDENCE: CONFIRMED 100% — write-denial probed directly against the live REST API].
_REST_URL = os.environ.get("SUPABASE_REST_URL", "https://ezuvmolyfgsadkehjnef.supabase.co/rest/v1")
_REST_KEY = os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImV6dXZtb2x5ZmdzYWRrZWhqbmVmIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODIyMjI1NDEsImV4cCI6MjA5Nzc5ODU0MX0.o06CM1IiezCNV_hHq5n9oY6JA9oImM4nz4Rey5qhtJE",
)

_PORT = int(os.environ.get("PORT", "8090"))
# 10s TTL: the fleet writes events continuously, so a caller polling once a second would otherwise issue 60 identical
# upstream queries a minute. One upstream call per 10s bounds our load on the SHARED PostgREST pool (the webapp uses it
# too) while keeping the number fresher than a human reads it. {plan §caching}.
_CACHE_TTL_S = float(os.environ.get("API_CACHE_TTL_S", "10"))
_RATE_MAX = int(os.environ.get("API_RATE_MAX", "60"))       # requests per IP per window
_RATE_WINDOW_S = float(os.environ.get("API_RATE_WINDOW_S", "60"))
_UPSTREAM_TIMEOUT_S = float(os.environ.get("API_UPSTREAM_TIMEOUT_S", "20"))

# tz allow-list. An arbitrary caller-supplied string reaches `now() AT TIME ZONE $1`; an unknown zone makes Postgres
# raise, which on a public endpoint is a 500 plus a database error in the response. Validating here turns that into a
# 400 with a clear message and keeps upstream error text off the wire.
_ALLOWED_TZ = {"UTC", "America/New_York", "America/Los_Angeles", "America/Chicago",
               "Europe/London", "Europe/Paris", "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong"}

_cache: dict[str, tuple[float, dict]] = {}      # tz -> (fetched_at_monotonic, payload)
_hits: dict[str, deque] = {}                    # client ip -> timestamps inside the current window


def _rate_ok(ip: str) -> bool:
    """Sliding-window per-IP limiter. WHY this exists on a read-only endpoint: the PostgREST pool is SHARED with the
    webapp, so an unthrottled scraper here degrades the dashboard for everyone even though it can never touch the
    fleet. Cheap to run and bounded in memory by the window."""
    now = time.monotonic()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > _RATE_WINDOW_S:     # drop stamps that fell out of the window
        q.popleft()
    if len(q) >= _RATE_MAX:
        return False
    q.append(now)
    return True


async def _fetch_pulse(tz: str) -> dict:
    """Call the today_pulse RPC through PostgREST and return its jsonb payload, serving a cached copy inside the TTL.

    The function itself does the counting (one scan, three buckets) — see
    supabase/migrations/20260728031746_waterevents_today_pulse_rpc.sql. Keeping the SQL there rather than here means
    the semantics are versioned with the schema and reviewable as a migration."""
    hit = _cache.get(tz)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_S:
        return hit[1]
    headers = {"apikey": _REST_KEY, "Authorization": f"Bearer {_REST_KEY}",
               "Content-Profile": "waterevents", "Content-Type": "application/json"}
    async with ClientSession(timeout=ClientTimeout(total=_UPSTREAM_TIMEOUT_S)) as s:
        async with s.post(f"{_REST_URL}/rpc/today_pulse", headers=headers,
                          data=json.dumps({"tz": tz})) as r:
            r.raise_for_status()
            payload = await r.json()
    _cache[tz] = (time.monotonic(), payload)
    return payload


async def today_pulse(request: web.Request) -> web.Response:
    """GET /today/pulse?tz=UTC → the Today page's live counter.

    Counts events where the event's DATE PERIOD is current AND we discovered it inside the window (intersection, per
    {USER 2026-07-28 "No not union but interaction"}). A coarse event_date counts when the period it names contains
    today — 2026-Q2 is current in July because that is the quarter being reported, 2026 is current all year.

    EXPECT the minute bucket to read 0 most of the time: over a sampled 24h only 132 of 1440 minutes (9.2%) had a
    qualifying row, against 21 of 24 hours. That is data sparsity, not a fault.
    {MEASURED 2026-07-28 "209 hits/24h across 132 distinct minutes"} [CONFIDENCE: CONFIRMED 100% — counted directly]."""
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (
        request.remote or "?")
    if not _rate_ok(ip):
        return web.json_response({"error": "rate limited", "limit_per_window": _RATE_MAX,
                                  "window_s": _RATE_WINDOW_S}, status=429)
    tz = request.query.get("tz", "UTC")
    if tz not in _ALLOWED_TZ:
        return web.json_response({"error": "unsupported tz", "allowed": sorted(_ALLOWED_TZ)}, status=400)
    try:
        payload = await _fetch_pulse(tz)
    except Exception as e:                       # noqa: BLE001 — never leak upstream/db text to a public caller
        return web.json_response({"error": "upstream unavailable", "kind": type(e).__name__}, status=503)
    return web.json_response(payload, headers={"Cache-Control": f"public, max-age={int(_CACHE_TTL_S)}",
                                               "Access-Control-Allow-Origin": "*"})


async def health(_request: web.Request) -> web.Response:
    """Liveness only — deliberately does NOT touch the database, so a probe loop cannot add upstream load."""
    return web.json_response({"ok": True, "service": "api_service", "cache_ttl_s": _CACHE_TTL_S})


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/health", health), web.get("/today/pulse", today_pulse)])
    return app


if __name__ == "__main__":
    print(f"[api_service] listening on :{_PORT} → {_REST_URL}/rpc/today_pulse "
          f"(cache {_CACHE_TTL_S}s, rate {_RATE_MAX}/{_RATE_WINDOW_S}s)", flush=True)
    web.run_app(build_app(), port=_PORT, access_log=None)
