"""In-memory per-host fetch health — a circuit-breaker so ONE datacenter-IP-blocking host can't freeze the fetch pool.

用一句话讲完: 每个 host 记一份健康档案(连续直连失败数 / 连续代理失败数 / 状态)。抓取前查档案 —— host 已 DEAD →
直接跳过不抓;host 的直连被证实封了机房 IP(DIRECT_BLOCKED)→ 跳过那 8 秒直连、直接走住宅代理。抓取后把每条路
(direct / proxy)的成功或失败回写档案。这样 liveramp 第一次踩坑就记下来,后面 73 条 liveramp 不再每条白等 8 秒。

WHY (the workflow bug this fixes): the fetch pool claims events in id-order, so a company's whole run (e.g. liveramp ×74)
lands in the 20 in-flight slots AT ONCE. liveramp blocks Cloud Run's datacenter IP → every direct GET waits the full 8s
ceiling before falling to the slow residential proxy → all 20 slots freeze on the SAME bad host → 0 ev/s (measured:
"inflight=20 | wrote 0 in 31s"). This module makes the SECOND+ event of a blocked host skip the wasted wait. Pairs with
the host-diverse claim (spreads hosts across slots) — this makes each blocked slot cheap, the claim keeps them few.
{DB 2026-07-21 id-range: liveramp ×74, htgc ×35 clustered; liveramp = 1s from a residential IP, 30s+ from prod}
[CONFIDENCE: CONFIRMED — the same host is fast off-datacenter, slow on it → datacenter-IP block].

STATE MACHINE (per host):
  UNKNOWN        never tried            → try direct (optimistic)
  HEALTHY        direct works           → keep using direct (fast path)
  DIRECT_BLOCKED direct failed K times  → SKIP direct, go straight to proxy; re-probe direct after a cooldown (recover)
  DEAD           direct AND proxy dead  → SKIP fetch entirely (anchor-only title, mark done). TERMINAL — never retried.
{USER 2026-07-21 "dead shouldnt retry"} [CONFIDENCE: CONFIRMED — a host where BOTH paths fail repeatedly is gone].
"""
from __future__ import annotations

import os
import threading
import time

# Consecutive-failure thresholds. WHY consecutive (not lifetime): a single failure is a transient blip, not a block —
# only a RUN of failures proves a real datacenter-IP wall. A success resets the counter. {plan edge-case: transient ≠ blocked}.
_DIRECT_K = 3                                                 # 3 consecutive direct failures → DIRECT_BLOCKED
_DEAD_K = 3                                                   # 3 consecutive proxy failures (while direct blocked) → DEAD
# DIRECT_BLOCKED re-probe: a datacenter block can lift (IP rotation / host un-bans) — after this cooldown allow ONE
# direct attempt to test recovery. DEAD gets NO such probe (terminal). {plan edge-case: hosts recover} [CONFIDENCE: INFERRED].
_PROBE_COOLDOWN_S = 1800                                      # 30 min

_lock = threading.Lock()                                     # 20 pool threads read/write the map — guard every access
_hosts: dict = {}                                            # host -> {"direct_fail","proxy_fail","state","blocked_at"}

# BACKEND SELECTOR: "memory" (default, ZERO behavior change — the original in-process dict) or "db" (shared table,
# lets M instances share ONE breaker view → horizontal scaling). WHY a flag not a hard switch: the memory backend
# must stay the unchanged default so nothing about the current single-instance run changes; db is opt-in per env.
# {TASK 2026-07-22 "flag 切换，默认走现有内存实现（零行为改变）"} [CONFIDENCE: CONFIRMED — default 'memory' is the
# pre-existing code path verbatim; db is reached only when HOST_HEALTH_BACKEND=db].
_BACKEND = os.environ.get("HOST_HEALTH_BACKEND", "memory").strip().lower()

# DB DSN — SAME construction as the basic_info worker (env secret first, then the Supabase pooler fallback) so the
# breaker table lives in the same Postgres as everything else and one env var configures both. {WORKER.PY:37-38
# "_DSN = os.environ.get('BCT_DB_DSN') or ('postgresql://postgres.ezuvmolyfgsadkehjnef...pooler.supabase.com:5432/postgres')"}
# [CONFIDENCE: CONFIRMED — copied verbatim from src/agents/event_agent/basic_info/worker.py so both point at one DB].
# NO LITERAL FALLBACK — see the note above. BCT_DB_DSN is this module's own name for the same database, so it
# falls back to the fleet-wide WATEREVENTS_DB_DSN and then to empty; psycopg2.connect raises on an empty DSN,
# which is the correct loud failure. {AUDIT 2026-07-28} [CONFIDENCE: CONFIRMED 100% — live credential].
_DSN = os.environ.get("BCT_DB_DSN") or os.environ.get("WATEREVENTS_DB_DSN", "")


_CONN_RETRIES = 4                                            # bounded retries when the pooler is momentarily saturated
_CONN_BACKOFF_S = 0.25                                       # base backoff; grows linearly per attempt (0.25, 0.5, 0.75)


def _db_conn():
    """Open a short-lived autocommit psycopg2 connection to the breaker DB, with bounded backoff on a saturated pooler.
    WHY a fresh conn per call (not a pooled module-global): host_health.record/is_dead/skip_direct are each ONE atomic
    statement, called from many pool threads — a psycopg2 connection is NOT thread-safe, so a shared handle would
    corrupt under the pool threads. A fresh conn per call is simplest-correct; the Supabase pooler (Supavisor) pools
    the TCP sockets underneath. WHY the retry loop: the Supavisor pooler runs SESSION mode with a small pool_size
    (measured 15), so a burst of concurrent breaker conns can transiently hit `EMAXCONNSESSION max clients reached`.
    That is a TRANSIENT saturation, not a real outage — a short linear backoff lets in-flight breaker conns drain and
    the next attempt succeed, instead of dropping the record() (which the caller swallows in a bare except → a LOST
    health update). {LOCAL TEST 2026-07-22 20-thread hammer → "FATAL: (EMAXCONNSESSION) max clients reached in session
    mode - max clients are limited to pool_size: 15"} [CONFIDENCE: CONFIRMED — reproduced the pooler cap under load;
    backoff-retry is the standard transient-saturation handling]. Only reached when _BACKEND=='db'."""
    import time as _t                                         # local: memory backend must not import anything db-related
    import psycopg2                                           # local import: memory backend must not need psycopg2 installed
    # FAIL LOUD on an unset DSN. After the hardcoded literal was removed _DSN defaults to "", and psycopg2.connect("")
    # does NOT error — libpq resolves an empty conninfo against its own defaults (local socket / $PGHOST / $USER), so
    # the breaker would quietly read and write host-health rows in whatever database happened to answer. Only reached
    # when HOST_HEALTH_BACKEND=db, which is exactly the configuration that expects a real shared table.
    # [CONFIDENCE: CONFIRMED 100% — empty-conninfo default resolution is libpq documented behaviour].
    if not _DSN:
        raise RuntimeError("HOST_HEALTH_BACKEND=db but neither BCT_DB_DSN nor WATEREVENTS_DB_DSN is set.")
    last_exc = None
    for attempt in range(_CONN_RETRIES):                     # bounded — never loop forever; give up after _CONN_RETRIES
        try:
            conn = psycopg2.connect(_DSN, connect_timeout=20)   # same connect_timeout as worker.py:347
            conn.autocommit = True                           # each breaker op is one self-contained statement — commit now
            return conn
        except psycopg2.OperationalError as exc:             # includes EMAXCONNSESSION (pooler full) + transient net errors
            last_exc = exc
            if attempt == _CONN_RETRIES - 1:                 # exhausted retries → re-raise to the caller (it decides)
                raise
            _t.sleep(_CONN_BACKOFF_S * (attempt + 1))        # linear backoff: 0.25, 0.5, 0.75s — let the pool drain
    raise last_exc                                           # unreachable (loop either returns or raises) — satisfies type


def is_dead(host: str) -> bool:
    """True if the host is DEAD → the caller skips fetching entirely (return empty → anchor-rescue title).
    Backend-dispatched: memory reads the in-process dict, db reads the shared table (same DEAD semantics)."""
    if _BACKEND == "db":                                      # shared-table read: one SELECT, no lock (DB is the arbiter)
        return _db_is_dead(host)
    with _lock:                                               # memory path UNCHANGED — original in-process dict read
        h = _hosts.get(host)
        return bool(h and h["state"] == "DEAD")


def skip_direct(host: str) -> bool:
    """True if the caller should SKIP the direct attempt (host's direct is blocked) and go straight to the proxy.
    Returns False once per cooldown so a recovered host is re-tested on direct (edge-case: hosts recover).
    Backend-dispatched: the memory path is unchanged; the db path does the read-then-conditional-write atomically."""
    if _BACKEND == "db":                                      # shared table: the cooldown-reset write must be atomic
        return _db_skip_direct(host)
    with _lock:                                               # memory path UNCHANGED — original in-process dict logic
        h = _hosts.get(host)
        if not h or h["state"] != "DIRECT_BLOCKED":
            return False
        if time.time() - h["blocked_at"] > _PROBE_COOLDOWN_S:  # cooldown elapsed → let THIS call probe direct again
            h["blocked_at"] = time.time()                      # reset the cooldown window regardless of the probe's result
            return False
        return True                                            # still cooling down → skip direct, use proxy


def record(host: str, path: str, ok: bool) -> None:
    """Update a host's health after a fetch attempt. path ∈ {'direct','proxy'}. State transitions per the module
    docstring. DEAD is terminal — once dead, we stop touching it (no retry). Called by impersonate.get after each try.
    Backend-dispatched: memory does read-modify-write under the lock; db does ONE atomic upsert (self-increment +
    CASE state transition in SQL) so M concurrent processes never lose an update — the horizontal-scale correctness key."""
    if _BACKEND == "db":                                     # shared table: atomic upsert, no lost updates across processes
        return _db_record(host, path, ok)
    with _lock:                                              # memory path UNCHANGED — original read-modify-write
        h = _hosts.setdefault(host, {"direct_fail": 0, "proxy_fail": 0, "state": "UNKNOWN", "blocked_at": 0.0})
        if h["state"] == "DEAD":                              # terminal — never revive (user: dead shouldnt retry)
            return
        if path == "direct":
            if ok:                                            # direct works → fully healthy, clear the block
                h["direct_fail"] = 0
                h["state"] = "HEALTHY"
            else:
                h["direct_fail"] += 1
                if h["direct_fail"] >= _DIRECT_K and h["state"] != "DIRECT_BLOCKED":
                    h["state"] = "DIRECT_BLOCKED"             # direct proven blocked → skip it next time
                    h["blocked_at"] = time.time()
        elif path == "proxy":
            if ok:
                h["proxy_fail"] = 0                           # proxy works → stay DIRECT_BLOCKED (not dead)
            else:
                h["proxy_fail"] += 1
                if h["proxy_fail"] >= _DEAD_K:               # BOTH paths dead → terminal skip, no retry
                    h["state"] = "DEAD"


def snapshot() -> dict:
    """A COPY of the current host→state map (for logging / a health probe). Never mutated by the caller.
    Backend-dispatched: memory copies the dict, db SELECTs the whole table into the same {host: {...}} shape."""
    if _BACKEND == "db":                                     # shared table: read every row into the dict shape memory returns
        return _db_snapshot()
    with _lock:                                              # memory path UNCHANGED
        return {k: dict(v) for k, v in _hosts.items()}


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# DB BACKEND (reached only when HOST_HEALTH_BACKEND=db) — the shared-table implementations of the 4 stateful funcs.
# Each is ONE atomic SQL statement so M concurrent processes stay correct without a cross-process lock: Postgres row
# locking (implicit in a single UPDATE / INSERT..ON CONFLICT) serializes concurrent writers to the SAME host row.
# WHY the state-machine logic lives in SQL (CASE), not read-into-Python-then-write-back: a read-modify-write from
# Python would race — two processes both read proxy_fail=2, both write 3, one increment lost, DEAD never triggers.
# Doing the increment + threshold check inside ONE statement makes the whole transition atomic per row.
# {TASK 2026-07-22 "record() 的读-改-写要用原子 upsert ... 保证 M 个进程并发 record 同一 host 不丢更新"}
# [CONFIDENCE: CONFIRMED — a single UPDATE holds a row lock for its duration; concurrent updates to one row serialize].
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _db_is_dead(host: str) -> bool:
    """DB read of DEAD state — one SELECT. Absent row → not dead (== dict .get() returning None)."""
    conn = _db_conn()                                       # fresh autocommit conn (thread-safe per-call)
    try:
        cur = conn.cursor()
        # Row-existence + state check in one query; no row → no result → not dead (mirrors memory's `h and ...`).
        cur.execute("SELECT state = 'DEAD' FROM host_health WHERE host = %s", (host,))
        row = cur.fetchone()
        return bool(row and row[0])                         # (True,) iff the row exists AND state is DEAD
    finally:
        conn.close()                                        # short-lived conn — always close (pooler reclaims the socket)


def _db_skip_direct(host: str) -> bool:
    """DB analog of skip_direct: True iff the host is DIRECT_BLOCKED and still inside the 30-min re-probe cooldown.
    The cooldown-expiry RESET (blocked_at = now) must be ATOMIC with the read — otherwise M processes each see the
    cooldown expired and each fire a direct probe. One UPDATE ... WHERE cooldown-expired RETURNING does it: exactly
    ONE process's UPDATE matches the stale-cooldown row (the row lock serializes them), gets a RETURNING row, and
    grants ITS call the probe (returns False = try direct); every other concurrent caller's UPDATE now sees the
    freshly-bumped blocked_at, matches 0 rows, and returns True (skip direct). {TASK 2026-07-22 atomic cooldown}
    [CONFIDENCE: CONFIRMED — UPDATE..RETURNING under a row lock is the standard atomic test-and-set]."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        # Step 1 — try to CLAIM the re-probe atomically: only a DIRECT_BLOCKED row whose cooldown has elapsed matches;
        # the matching process bumps blocked_at (resets the window) and RETURNs a row → it earns the probe (skip=False).
        cur.execute(
            """
            UPDATE host_health
               SET blocked_at = now()
             WHERE host = %s
               AND state = 'DIRECT_BLOCKED'
               AND blocked_at IS NOT NULL
               AND blocked_at < now() - make_interval(secs => %s)
            RETURNING host
            """,
            (host, _PROBE_COOLDOWN_S),
        )
        if cur.fetchone() is not None:                      # WE won the atomic re-probe claim → caller tries direct
            return False
        # Step 2 — no re-probe claimed. Either the host is not DIRECT_BLOCKED (→ don't skip) or it IS but still cooling
        # down (→ skip). One SELECT distinguishes: skip iff the current state is DIRECT_BLOCKED. (A row another process
        # just re-probed reads back as DIRECT_BLOCKED with a fresh blocked_at → still cooling → skip, exactly right.)
        cur.execute("SELECT state = 'DIRECT_BLOCKED' FROM host_health WHERE host = %s", (host,))
        row = cur.fetchone()
        return bool(row and row[0])                         # True → skip direct (blocked & cooling); False → not blocked
    finally:
        conn.close()


def _db_record(host: str, path: str, ok: bool) -> None:
    """DB analog of record(): ONE atomic INSERT..ON CONFLICT DO UPDATE that self-increments the fail counter and runs
    the state transition inside SQL CASE expressions, so concurrent record() calls to the SAME host never lose an
    update. The four (path, ok) combinations map to four upserts; each mirrors the memory branch's transition EXACTLY:
      • direct+ok   → direct_fail=0, state=HEALTHY (clear the block)
      • direct+fail → direct_fail+=1; state→DIRECT_BLOCKED once it hits _DIRECT_K (set blocked_at then)
      • proxy+ok    → proxy_fail=0 (stay whatever state; a working proxy is not 'healthy')
      • proxy+fail  → proxy_fail+=1; state→DEAD once it hits _DEAD_K (terminal)
    DEAD is terminal in ALL branches: `WHERE host_health.state <> 'DEAD'` on every UPDATE means once dead the row is
    never touched again (== the memory branch's early `if h['state']=='DEAD': return`). The INSERT arm handles a
    brand-new host (== dict.setdefault) with its first attempt already applied. {HOST_HEALTH.PY:71-90 memory transitions}
    [CONFIDENCE: CONFIRMED — each CASE mirrors the exact counter/threshold/state line of the memory branch]."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        if path == "direct" and ok:
            # direct success → fully healthy, counter cleared. INSERT seeds a HEALTHY row for a never-seen host.
            cur.execute(
                """
                INSERT INTO host_health (host, direct_fail, proxy_fail, state, blocked_at)
                VALUES (%s, 0, 0, 'HEALTHY', NULL)
                ON CONFLICT (host) DO UPDATE
                   SET direct_fail = 0, state = 'HEALTHY'
                 WHERE host_health.state <> 'DEAD'          -- DEAD is terminal: never revive
                """,
                (host,),
            )
        elif path == "direct" and not ok:
            # direct failure → self-increment direct_fail; flip to DIRECT_BLOCKED (stamp blocked_at) the moment the
            # incremented value reaches _DIRECT_K AND we're not already blocked (matches `and h['state']!='DIRECT_BLOCKED'`).
            # A first-ever host inserts with direct_fail=1 (state stays UNKNOWN unless K==1).
            cur.execute(
                """
                INSERT INTO host_health (host, direct_fail, proxy_fail, state, blocked_at)
                VALUES (%s, 1, 0,
                        CASE WHEN 1 >= %s THEN 'DIRECT_BLOCKED' ELSE 'UNKNOWN' END,
                        CASE WHEN 1 >= %s THEN now() ELSE NULL END)
                ON CONFLICT (host) DO UPDATE
                   SET direct_fail = host_health.direct_fail + 1,
                       state = CASE WHEN host_health.direct_fail + 1 >= %s
                                     AND host_health.state <> 'DIRECT_BLOCKED'
                                    THEN 'DIRECT_BLOCKED' ELSE host_health.state END,
                       blocked_at = CASE WHEN host_health.direct_fail + 1 >= %s
                                          AND host_health.state <> 'DIRECT_BLOCKED'
                                         THEN now() ELSE host_health.blocked_at END
                 WHERE host_health.state <> 'DEAD'
                """,
                (host, _DIRECT_K, _DIRECT_K, _DIRECT_K, _DIRECT_K),
            )
        elif path == "proxy" and ok:
            # proxy success → reset proxy_fail only (state unchanged; a working proxy keeps a DIRECT_BLOCKED host blocked).
            cur.execute(
                """
                INSERT INTO host_health (host, direct_fail, proxy_fail, state, blocked_at)
                VALUES (%s, 0, 0, 'UNKNOWN', NULL)
                ON CONFLICT (host) DO UPDATE
                   SET proxy_fail = 0
                 WHERE host_health.state <> 'DEAD'
                """,
                (host,),
            )
        elif path == "proxy" and not ok:
            # proxy failure → self-increment proxy_fail; flip to DEAD (terminal) the moment it reaches _DEAD_K. This is
            # the CONCURRENCY-CRITICAL path (the test hammers it from M threads): the `+ 1` runs INSIDE the UPDATE under
            # the row lock, so M concurrent fails on one host increment 1..M with no lost update, and the Mth crosses
            # _DEAD_K → DEAD exactly once. A first-ever host inserts with proxy_fail=1.
            cur.execute(
                """
                INSERT INTO host_health (host, direct_fail, proxy_fail, state, blocked_at)
                VALUES (%s, 0, 1,
                        CASE WHEN 1 >= %s THEN 'DEAD' ELSE 'UNKNOWN' END,
                        NULL)
                ON CONFLICT (host) DO UPDATE
                   SET proxy_fail = host_health.proxy_fail + 1,
                       state = CASE WHEN host_health.proxy_fail + 1 >= %s THEN 'DEAD'
                                    ELSE host_health.state END
                 WHERE host_health.state <> 'DEAD'
                """,
                (host, _DEAD_K, _DEAD_K),
            )
    finally:
        conn.close()


def _db_snapshot() -> dict:
    """DB analog of snapshot(): SELECT the whole table into {host: {"direct_fail","proxy_fail","state","blocked_at"}}.
    blocked_at is returned as an epoch float (extract(epoch ...)) so the dict shape matches the memory backend's float
    exactly (memory stores time.time()); a NULL blocked_at → 0.0 (== the dict default). Used for logging/health probes."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        # coalesce NULL blocked_at → 0.0 epoch so the shape equals the memory dict's default {"blocked_at": 0.0}.
        cur.execute(
            "SELECT host, direct_fail, proxy_fail, state, "
            "COALESCE(EXTRACT(EPOCH FROM blocked_at), 0.0) FROM host_health"
        )
        out: dict = {}
        for host, direct_fail, proxy_fail, state, blocked_at in cur.fetchall():
            out[host] = {"direct_fail": direct_fail, "proxy_fail": proxy_fail,
                         "state": state, "blocked_at": float(blocked_at)}
        return out
    finally:
        conn.close()
