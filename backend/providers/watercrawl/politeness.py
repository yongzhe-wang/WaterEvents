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

Upstream trigger: `page.goto` (every browser navigation) and `impersonate.fetch` (the non-browser lane); callers about to issue a
request call `next_delay()`/`wait_turn*()` to pace themselves.
Downstream: a refusal returns an empty render with a specific reason token, exactly like any other render failure, so a
robots denial is observable in the logs rather than looking like a timeout.
"""

import asyncio
import ipaddress
import os
import socket
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
# Hard bound on the robots fetch. THE PREVIOUS REASONING HERE WAS WRONG AND IS WORTH KEEPING AS A CORRECTION: it argued
# that a synchronous fetch on the render loop cost "one 3s stall per host per hour, cheaper than the machinery an
# async-only path would need". Two measurements refuted both halves. The stall is not one render but the ENTIRE loop —
# heartbeat gap 51 ms idle versus 3042 ms while the fetch ran, so every concurrent render freezes with it. And the TTL
# cache does not cover the case that matters: 24 simultaneous first-touches of one origin produced 24 fetches, because
# nothing is cached until the first returns, and same-host clusters are the crawl's normal shape.
# The bound still exists as a backstop, but it is no longer what keeps the loop responsive — `url_allowed_async` doing
# the work off-thread with per-origin de-duplication is.
# {MEASURED 2026-07-29 "MAX HEARTBEAT GAP WHILE IT RAN: 3042MS" vs "BASELINE MAX HEARTBEAT GAP (IDLE LOOP): 51MS"}
# {MEASURED 2026-07-29 "24 CONCURRENT FIRST-TOUCHES OF ONE HOST -> 24 FETCHES (CACHE PREVENTS 0)"}
# [CONFIDENCE: CONFIRMED 100% — both figures from a repro running the real function on the real loop. The earlier
#  INFERRED 85% estimate was the wrong shape, not merely the wrong number.]
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


# ── SSRF GUARD ──────────────────────────────────────────────────────────────────────────────────────────────────────
# MOVED HERE FROM render.py 2026-07-29 so that page.py can enforce it. It has to live in the module with no
# first-party imports: page.py cannot import render.py (render imports page), so while the guard lived in render.py the
# only navigations it covered were render.py's own three coroutines. Five driver call sites — clicks, year_select,
# years, and load_more/year_bar via page.goto — went straight to the browser with no scheme check, no SSRF check and no
# pacing, and year_bar is the highest-goto-density path in the crawler at up to seven navigations for one page.
# {SHELL 2026-07-29 "git grep '\.goto(' -- backend/providers/watercrawl → 5 driver sites outside render.py"}
# [CONFIDENCE: CONFIRMED 100% — the bypass was found by listing every goto call site and checking which were preceded
#  by a gate; politeness.py imports only `config`, so hosting it here creates no cycle.]
_SSRF_CACHE: dict = {}                                    # host -> bool; DNS resolution is the expensive part
_SSRF_CACHE_MAX = 4096                                    # hard cap so a link-spam page cannot grow this without bound


def host_is_public(host: str) -> bool:
    """True only when `host` resolves EXCLUSIVELY to public IPs. Blocks localhost / loopback / RFC1918 private /
    link-local (169.254.169.254 cloud metadata) / reserved / multicast. A resolution failure returns False — fail
    CLOSED, because an unresolvable host is not worth navigating to anyway.

    BLOCKING: `socket.getaddrinfo` is a synchronous DNS call, so this must never be invoked directly from a coroutine
    on the shared Playwright loop. `url_allowed_async` is the entry point that offloads it.
    {OFFICEALL/FETCH.PY "TRUE ONLY WHEN `HOST` RESOLVES TO A PUBLIC IP (SSRF GUARD)"} — same policy, same stdlib
    predicates, deliberately duplicated so the two fetchers cannot diverge.
    [CONFIDENCE: CONFIRMED 100% — on GCE the metadata endpoint serves service-account tokens to any unauthenticated
     GET from the instance, and the crawl frontier's only address filter is `startswith("http")`.]"""
    h = (host or "").lower().strip()
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return False
    cached = _SSRF_CACHE.get(h)
    if cached is not None:                                # resolution is the costly part — reuse the verdict
        return cached
    ok = True
    try:
        for info in socket.getaddrinfo(h, None):           # EVERY resolved address must be public, not just the first
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                ok = False
                break
    except Exception:                                     # noqa: BLE001 — unresolvable → fail closed, never navigate
        ok = False
    if len(_SSRF_CACHE) >= _SSRF_CACHE_MAX:               # bounded: drop the whole cache rather than grow unbounded
        _SSRF_CACHE.clear()
    _SSRF_CACHE[h] = ok
    return ok


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

    Upstream: `url_allowed` / `url_allowed_async`, which combine this with the scheme check and the SSRF guard and turn a False
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


# ── THE ASYNC GATE ──────────────────────────────────────────────────────────────────────────────────────────────────
# `allowed()` above is SYNCHRONOUS and does network I/O (the robots fetch) plus, via the caller's SSRF check, a blocking
# `socket.getaddrinfo`. Both run on the single Playwright loop thread that every render shares. The comment on
# _ROBOTS_TIMEOUT_S reasoned about this as "one 3s stall per host per hour" and that reasoning was wrong in two ways,
# both measured:
#   • the stall is not one render, it is the WHOLE LOOP — a heartbeat probe on an idle loop showed a 51 ms maximum gap,
#     and 3042 ms while a sync robots fetch ran inside a coroutine. Every other in-flight render freezes with it.
#   • the TTL cache does not help the case that matters. 24 concurrent first-touches of one origin produced 24 fetches,
#     because nothing is stored until the first one returns. The crawl claims work in id order, so same-host clusters
#     are the normal shape, not the exception — worst case 24 x 3s of frozen loop.
# {MEASURED 2026-07-29 "BASELINE MAX HEARTBEAT GAP (IDLE LOOP): 51MS" / "MAX HEARTBEAT GAP WHILE IT RAN: 3042MS"}
# {MEASURED 2026-07-29 "24 CONCURRENT FIRST-TOUCHES OF ONE HOST -> 24 FETCHES (CACHE PREVENTS 0)"}
# [CONFIDENCE: CONFIRMED 100% — both numbers come from a repro that ran the real function on the real loop.]
#
# The fix is two things, and both are needed: move the blocking work off the loop with asyncio.to_thread, AND
# de-duplicate concurrent work per origin so a stampede collapses to one computation.
_INFLIGHT: dict[str, asyncio.Future] = {}


async def url_allowed_async(url: str) -> tuple[bool, str]:
    """(allowed, reason) for a url about to be navigated, WITHOUT blocking the shared loop.

    The scheme test is done inline because it is pure string work. Everything that can block is handed to a thread, and
    concurrent callers for the same origin await ONE shared future instead of each starting their own resolution and
    fetch — which is what turned a 3-second stall into a potential 72-second one.

    Upstream: `page.goto` (so every browser navigation is covered, including the drivers) and any other async
    callers. Downstream: a False means the navigation never happens and the caller returns an empty render with the
    specific reason token, so a refusal is legible in the logs rather than looking like a timeout."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):            # pure string work — no reason to leave the loop for it
        return False, "bad-scheme"
    if IGNORE_ROBOTS and _SSRF_CACHE.get((parsed.hostname or "").lower().strip()) is True:
        return True, ""                                   # fully warm and compliance disabled → nothing left to check
    origin = _host_key(url)
    fut = _INFLIGHT.get(origin)
    if fut is None:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _INFLIGHT[origin] = fut
        try:
            res = await asyncio.to_thread(url_allowed, url)
            if not fut.done():
                fut.set_result(res)
        except Exception as e:                            # noqa: BLE001 — a gate crash must not wedge every waiter
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            # Drop the slot BEFORE returning so the next first-touch after the TTL lapses starts a fresh computation.
            # Leaving it would pin one verdict forever and quietly defeat _ROBOTS_TTL_S.
            _INFLIGHT.pop(origin, None)
        return res
    return await asyncio.shield(fut)                      # a stampede member: share the leader's single result


def url_allowed(url: str) -> tuple[bool, str]:
    """(allowed, reason) — the SYNCHRONOUS gate, for callers that are already on a worker thread.

    Two entry points exist on purpose, and picking the wrong one is the bug this module was rewritten to fix:
      • `url_allowed_async` — for anything running on the shared Playwright loop. Offloads the blocking work.
      • `url_allowed` (this) — for thread contexts, e.g. the impersonate lane's curl_cffi GET, which is a plain
        synchronous function called off-loop. Using the async form there would need a loop it does not have; using this
        one ON the loop is what froze every in-flight render for 3 seconds.
    `url_allowed_async` calls this via asyncio.to_thread, so the policy itself has exactly one implementation.

    THE SCHEME CHECK IS HERE, not only in the async wrapper. It was briefly in the wrapper alone, and that made the two
    entry points disagree in the one direction that matters: `gopher://investors.amgen.com/x` and `ftp://...` returned
    (True, '') from this function, because a hostile scheme with a PUBLIC hostname passes the SSRF test cleanly. The
    async path happened to be safe only because it tested the scheme before delegating. `javascript:` and `file:` were
    caught either way, but by accident — they have no hostname, so resolution fails and the SSRF check fails closed.
    Relying on that accident is what hid the gap.
    {MEASURED 2026-07-29 "async=(False,'bad-scheme') sync=(True,'') gopher://investors.amgen.com/x"}
    [CONFIDENCE: CONFIRMED 100% — the divergence was produced by running both entry points over the same url list.]
    [CONFIDENCE: CONFIRMED 100% — the 3042 ms loop stall was measured with a heartbeat probe against an idle-loop
     baseline of 51 ms.]"""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):            # blocks file: / javascript: / data: / gopher: / ftp: …
        return False, "bad-scheme"
    if not host_is_public(parsed.hostname or ""):
        return False, "ssrf-blocked"
    if not allowed(url):
        return False, "robots-denied"
    return True, ""


async def wait_turn_async(url: str) -> None:
    """Async pacing, for anything running on the Playwright loop. MUST be used instead of `wait_turn` there: a
    `time.sleep` on that loop thread would stall every other in-flight render, not just this one."""
    d = next_delay(url)
    if d > 0:
        await asyncio.sleep(d)
