"""watercrawl.politeness — robots.txt compliance + per-host pacing for every outbound fetch.

用一句话讲完: 每次要抓一个 url 之前, 先用 host 查一份带 TTL 缓存的 robots.txt 判断允不允许, 再向该 host 的
token bucket 领一个时间片(默认 2 req/s, 被 robots 的 Crawl-delay 覆盖), 然后才让 render / impersonate 真正发请求 ——
所以"抓不抓"和"多快抓"这两个决定都收敛在这一个模块里, 而不是散落在四个 fallback tier 各自的代码里.

WHY this module exists at all: an audit of 2026-07-28 found the crawler had NO robots.txt handling and NO per-host
rate limit anywhere in the codebase, while simultaneously spoofing a real Chrome TLS fingerprint. The only concurrency
bounds were process-global semaphores, so all of them could land on a single host at once — a behaviour this repo has
already MEASURED on itself.
{AUDIT 2026-07-28 "GREP -RNIE 'ROBOTS\\.TXT|ROBOTPARSER|ROBOTFILEPARSER|CRAWL[-_]?DELAY' BACKEND/ → ZERO MATCHES"}
{HOST_HEALTH.PY:8-10 "ALL 20 SLOTS FREEZE ON THE SAME BAD HOST → 0 EV/S (MEASURED: \"INFLIGHT=20 | WROTE 0 IN 31S\")"}
[CONFIDENCE: CONFIRMED 100% — both the absence and the single-host pile-up are quoted from the tree itself.]

WHY it matters operationally, not just legally: this fleet re-scans ~2,900 companies on a repeating schedule and one
company's IR site can carry 70+ event urls. Hammering a host until the source IP is banned is an UNRECOVERABLE outage —
the only remedy left would be routing through the residential proxy, which escalates the problem instead of fixing it.

Upstream trigger: `render.url_allowed()` calls `allowed()` before every navigation; callers that are about to issue a
request call `next_delay()`/`wait_turn*()` to pace themselves.
Downstream: a refusal returns an empty render with a specific reason token, exactly like any other render failure, so a
robots denial is observable in the logs rather than looking like a timeout.
"""

import asyncio
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from . import config

# ── ESCAPE HATCH ───────────────────────────────────────────────────────────────────────────────────────────────────
# Deliberately DEFAULT-OFF and deliberately a separate flag from everything else: compliance must be switched off by an
# explicit human decision that leaves a trace in the unit file, never as a side effect of some other tuning knob.
# [CONFIDENCE: CONFIRMED 100% — a default-on bypass would make the rest of this module decorative.]
IGNORE_ROBOTS = os.environ.get("WATERCRAWL_IGNORE_ROBOTS", "") in ("1", "true", "yes")

# How long a parsed robots.txt stays trusted. An hour keeps us honest without re-fetching robots.txt once per page:
# a full BFS of one company is dozens of pages on ONE host, so the cache turns N robots fetches into 1.
_ROBOTS_TTL_S = float(os.environ.get("WATERCRAWL_ROBOTS_TTL_S", "3600"))
# Hard bound on the robots fetch. `allowed()` is SYNCHRONOUS and is called from render coroutines running on the single
# Playwright loop thread, so a slow robots fetch blocks that whole loop. 3s + aggressive caching keeps the worst case at
# one 3s stall per host per hour, which is cheaper than the machinery an async-only path would need.
# [CONFIDENCE: INFERRED 85% — the loop-blocking risk is structural (runtime.py marshals every render onto one loop);
#  3s is chosen as "shorter than NAV_TIMEOUT_MS/7" rather than measured against real robots.txt latency.]
_ROBOTS_TIMEOUT_S = float(os.environ.get("WATERCRAWL_ROBOTS_TIMEOUT_S", "3"))

# Default spacing between two requests to the SAME host, in seconds. 0.5s ≈ 2 req/s: slow enough that no single host
# sees this fleet as an attack, fast enough that a 60-page BFS still finishes inside EVENT_COMPANY_BUDGET_S=600.
# {LAUNCH_FLEET.SH "EVENT_COMPANY_BUDGET_S=\"600\""} [CONFIDENCE: INFERRED 80% — 2 req/s is the conventional default
#  for a polite crawler; the budget arithmetic (60 pages × 0.5s = 30s of pacing) is what makes it affordable here.]
_HOST_MIN_INTERVAL_S = float(os.environ.get("WATERCRAWL_HOST_MIN_INTERVAL_S", "0.5"))

# Identify ourselves. WHY a bot token at all: an operator who wants to rate-limit or contact us currently CANNOT — the
# UA is a verbatim Chrome string with no name and no contact. This token is what makes the traffic attributable.
# It is used for the robots.txt fetch and for the robots RULE LOOKUP, so a site can write a rule addressed to us.
# The escalated anti-bot tiers keep the spoofed UA — that is a deliberate, narrow exception, not the default.
# {CONFIG.PY UA "MOZILLA/5.0 (MACINTOSH; INTEL MAC OS X 10_15_7) ... CHROME/120.0 SAFARI/537.36" — no bot identity}
# [CONFIDENCE: CONFIRMED 100% — the UA string carries no identifying token; read directly from config.py.]
BOT_TOKEN = os.environ.get("WATERCRAWL_BOT_TOKEN", "WaterEventsBot")
BOT_CONTACT = os.environ.get("WATERCRAWL_BOT_CONTACT", "")
UA = f"{config.UA} {BOT_TOKEN}" + (f" (+{BOT_CONTACT})" if BOT_CONTACT else "")

# host -> (RobotFileParser|None, fetched_at). None means "fetched and there are no rules to apply" (404, or unreachable
# under the fail-open policy below) — cached exactly like a successful parse so we don't re-fetch a missing file per page.
_ROBOTS: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}
_ROBOTS_LOCK = threading.Lock()          # render coroutines and impersonate worker threads both reach this map

# host -> monotonic timestamp at which the NEXT request to that host may start. Reservation is done under a lock and the
# SLEEPING is done by the caller, so two callers can never be handed the same slot.
_NEXT_OK: dict[str, float] = {}
_PACE_LOCK = threading.Lock()

_CACHE_MAX = 4096                        # same bound as render._SSRF_CACHE: a link-spam page must not grow these maps


def _loud(msg: str) -> None:
    """Single place that decides where a politeness decision is announced. Everything here goes to stderr because a
    refusal must never look like a silent empty render — the whole point of this module is that the reason is legible.
    Mirrors the fail-loud style `backend/tools/**` already uses."""
    print(f"[politeness] {msg}", file=sys.stderr, flush=True)


def _host_key(url: str) -> str:
    """(scheme, host) collapsed to one cache key. Scheme is part of the key because robots.txt is fetched per-scheme and
    an http:// and an https:// origin are, strictly, different origins with possibly different rules."""
    p = urllib.parse.urlparse(url or "")
    return f"{p.scheme}://{(p.hostname or '').lower()}" + (f":{p.port}" if p.port else "")


def _fetch_robots(origin: str) -> urllib.robotparser.RobotFileParser | None:
    """Fetch and parse <origin>/robots.txt, or return None when there are no rules to apply.

    FAIL-OPEN, DELIBERATELY. RFC 9309 says a crawler SHOULD treat an unreachable robots.txt (5xx / network error) as a
    complete disallow. We do NOT do that by default, and the reason is specific to this system: a transient 500 on one
    IR host would silently take that company's coverage to zero, and "coverage silently went to zero" is the exact class
    of failure this audit exists to eliminate — it would be indistinguishable from the crawler simply not working. So an
    unreachable robots.txt allows, and says so LOUDLY. Set WATERCRAWL_ROBOTS_STRICT=1 to get the RFC behaviour instead.
    A 404 allows unconditionally under the RFC itself, so that case needs no exception.
    {RFC 9309 §2.3.1.4 "IF THE SERVER RESPONSE INDICATES A SERVER ERROR ... CRAWLERS SHOULD ASSUME COMPLETE DISALLOW"}
    [CONFIDENCE: CONFIRMED 90% — the RFC text is unambiguous; the DEVIATION is a judgement call, documented here so it
     is a decision on the record rather than an oversight.]"""
    url = f"{origin}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=_ROBOTS_TIMEOUT_S) as resp:   # noqa: S310 — scheme validated upstream
            body = resp.read(512_000).decode("utf-8", "replace")               # cap: a hostile robots.txt is still input
        rp.parse(body.splitlines())
        return rp
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:                           # 404/403 → "no rules exist" → allow all, per the RFC
            return None
        _loud(f"{origin}/robots.txt HTTP {e.code} — treating as no-rules (set WATERCRAWL_ROBOTS_STRICT=1 to disallow)")
        return _deny_all() if _STRICT else None
    except Exception as e:                                # noqa: BLE001 — DNS/TLS/timeout: unreachable, not forbidden
        _loud(f"{origin}/robots.txt unreachable ({type(e).__name__}) — treating as no-rules")
        return _deny_all() if _STRICT else None


_STRICT = os.environ.get("WATERCRAWL_ROBOTS_STRICT", "") in ("1", "true", "yes")


def _deny_all() -> urllib.robotparser.RobotFileParser:
    """A parser preloaded with a blanket Disallow, used only under WATERCRAWL_ROBOTS_STRICT to implement the RFC's
    disallow-on-server-error rule without special-casing it at every call site."""
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /"])
    return rp


def _robots_for(url: str) -> urllib.robotparser.RobotFileParser | None:
    """Cached robots.txt for the url's origin. Cache holds BOTH hits and misses so a site without robots.txt costs one
    fetch per TTL, not one per page. The whole map is dropped when it hits the cap rather than evicting entries one at a
    time — same bounded-not-clever policy as render._SSRF_CACHE."""
    origin = _host_key(url)
    now = time.time()
    with _ROBOTS_LOCK:
        hit = _ROBOTS.get(origin)
        if hit is not None and (now - hit[1]) < _ROBOTS_TTL_S:
            return hit[0]
    rp = _fetch_robots(origin)                            # network I/O OUTSIDE the lock: never serialise all hosts
    with _ROBOTS_LOCK:
        if len(_ROBOTS) >= _CACHE_MAX:
            _ROBOTS.clear()
        _ROBOTS[origin] = (rp, now)
    return rp


def allowed(url: str) -> bool:
    """MAY we fetch this url? True when robots.txt permits it (or there are no rules, or compliance is switched off).

    Upstream: `render.url_allowed()`, which combines this with the scheme check and the SSRF guard and turns a False
    into the "robots-denied" reason token. Downstream: a False means the render coroutine returns empty WITHOUT opening
    a browser context, so a disallowed url costs one cached lookup rather than a full page load."""
    if IGNORE_ROBOTS:                                     # explicit operator override; logged once per call site upstream
        return True
    rp = _robots_for(url)
    if rp is None:                                        # no rules published (or unreachable, fail-open) → allowed
        return True
    try:
        # Ask under our OWN token first: a site that wants to address us specifically must be able to. `can_fetch`
        # already falls back to the `User-agent: *` group when no group matches this token, so one call covers both.
        return rp.can_fetch(BOT_TOKEN, url)
    except Exception:                                     # noqa: BLE001 — a malformed robots.txt must not stop the crawl
        return True


def crawl_delay(url: str) -> float:
    """Crawl-delay the site asks for, in seconds, or 0 when it asks for none. Kept separate from `next_delay` so the
    pacing policy (max of our floor and the site's request) is visible at the one place that applies it."""
    if IGNORE_ROBOTS:
        return 0.0
    rp = _robots_for(url)
    if rp is None:
        return 0.0
    try:
        d = rp.crawl_delay(BOT_TOKEN)
        return float(d) if d else 0.0
    except Exception:                                     # noqa: BLE001 — same tolerance as `allowed`
        return 0.0


def next_delay(url: str) -> float:
    """RESERVE this host's next slot and return how long the caller must wait before using it.

    WHY reserve-then-sleep instead of a plain sleep: the reservation happens under a lock and moves the host's cursor
    forward immediately, so two concurrent callers targeting the same host get two DIFFERENT slots. A naive
    "sleep(interval)" would let N coroutines wake simultaneously and hit the host together — which is the pile-up this
    module exists to prevent, reproduced in a new place.
    Upstream: `wait_turn` / `wait_turn_async`. Downstream: the caller sleeps for the returned duration, then fetches."""
    interval = max(_HOST_MIN_INTERVAL_S, crawl_delay(url))
    if interval <= 0:
        return 0.0
    origin = _host_key(url)
    now = time.monotonic()
    with _PACE_LOCK:
        if len(_NEXT_OK) >= _CACHE_MAX:
            _NEXT_OK.clear()
        start = max(now, _NEXT_OK.get(origin, 0.0))       # our slot is whichever is later: now, or the queue's tail
        _NEXT_OK[origin] = start + interval               # advance the cursor so the NEXT caller queues behind us
    return start - now


def wait_turn(url: str) -> None:
    """Synchronous pacing, for the worker-thread lanes (impersonate / plain HTTP fetches)."""
    d = next_delay(url)
    if d > 0:
        time.sleep(d)


async def wait_turn_async(url: str) -> None:
    """Async pacing, for anything running on the Playwright loop. MUST be used instead of `wait_turn` there: a
    `time.sleep` on that loop thread would stall every other in-flight render, not just this one."""
    d = next_delay(url)
    if d > 0:
        await asyncio.sleep(d)
