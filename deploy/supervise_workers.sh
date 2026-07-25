#!/usr/bin/env bash
# supervise_workers — the parallelization infra for the WaterEvents discovery fleet (8-company test AND the 1000 stress
# run). Launches N discovery workers (staggered to avoid a Chromium cold-start thundering-herd), each in its OWN process
# group (setsid) so a hung worker + its whole browser subtree can be killed as one. STALL-WATCHDOG: if a worker's log
# stops advancing for STALL_S — the signature of a browser-event-loop DEADLOCK that render_shot's in-process timeout can
# NOT recover (the timeout runs on the very loop that's wedged) — kill its process group; the company's DB lease then
# expires and ANOTHER worker reclaims it (self-heal). Restart the slot so the fleet stays at N. Stop when the queue is
# drained. FULL QUALITY throughout — no token / char / max-pages reduction. {USER 2026-07-23 "build infra for
# parallelization ... 12 hours ... 1000 ... never compromise"}.
set -u
cd /workspace/WaterEvents || exit 2
N="${WORKERS:-8}"
STALL_S="${STALL_S:-240}"                 # kill a worker whose log has been silent this long (deadlock signature)
LOGD=tests/stress1000/logs
mkdir -p "$LOGD" tests/stress1000/traces

# ── DEPENDENCY PREFLIGHT ── guarantee the pod venv actually HAS the declared deps + browser binaries before launching the
# fleet. WHY this exists: a RunPod pod can be created/rebuilt/volume-reset WITHOUT anyone running `pip install -r
# requirements.txt`, and a missing dep then fails SILENTLY at runtime — the 2026-07-24 audit found the pod venv had NO
# curl_cffi despite `curl_cffi>=0.7` being in requirements.txt, so the impersonate fingerprint-bypass lane never armed and
# every TLS/HTTP2-fingerprint-walled IR site (gcs-web/tjx/ti/... = 261 hosts, ~28% of the corpus) returned 0 events instead
# of recovering in ~0.2s. Installing curl_cffi took render-fail 28%→1.5% AND throughput 0.42→2.33 pages/s on the 2668-page
# hstress. This preflight makes the half-armed launch impossible: install declared deps + the two chromium binaries (NOT
# pip pkgs) + a fail-LOUD import smoke test so the fleet never starts missing a lane. {AUDIT 2026-07-24 curl_cffi_missing;
# hstress_full2000 591 FAILED / 549 ERR_HTTP2 → hstress_fixed 5 FAILED / 1 ERR_HTTP2} [CONFIDENCE: CONFIRMED 100% —
# before/after measured]. Set SKIP_PREFLIGHT=1 to bypass on a pod you KNOW is already provisioned.
preflight() {
  [ "${SKIP_PREFLIGHT:-0}" = "1" ] && { echo "[preflight] SKIPPED (SKIP_PREFLIGHT=1)"; return 0; }
  echo "[preflight] pip install -r requirements.txt (idempotent — installed deps skip fast) ..."
  /root/venv/bin/pip install -q -r requirements.txt || { echo "[preflight] FATAL: pip install failed"; exit 3; }
  # browser binaries are NOT pip packages — the default render lane uses playwright's chromium, the residential lane uses
  # patchright's; each needs its own `install chromium` (idempotent — a present browser is reported and skipped).
  /root/venv/bin/playwright install chromium >/dev/null 2>&1 || echo "[preflight] WARN: playwright install chromium issue"
  /root/venv/bin/patchright install chromium >/dev/null 2>&1 || echo "[preflight] WARN: patchright install chromium issue"
  # SYSTEM libs for camoufox's Firefox (tier4 anti-Akamai). WHY here: camoufox-bin is a Firefox fork that needs libgtk-3 +
  # friends; the pod shipped WITHOUT libgtk-3.so.0 → every camoufox launch died "Couldn't load XPCOM, exitCode=255" →
  # tier4 was SILENTLY dead for the whole 2668-run → the 43 Akamai-walled big-caps (tesla/homedepot/nestle) never
  # recovered. apt is idempotent (present libs skip fast). {AUDIT 2026-07-24 camoufox_libgtk_missing; rewall recovered
  # 39/45 walls once installed} [CONFIDENCE: CONFIRMED — before: libgtk absent → 45 walled; after: 39 recovered].
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libgtk-3-0 libasound2 libdbus-glib-1-2 libx11-xcb1 libxt6 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libpango-1.0-0 libcairo2 >/dev/null 2>&1 \
    || echo "[preflight] WARN: apt install of camoufox/Firefox libs had an issue"
  # fail-LOUD smoke test — the exact deps whose silent absence caused the 28% failure (curl_cffi) + the dead tier4
  # (camoufox). Missing AFTER install → STOP, don't launch the fleet half-armed (hours of 0-event companies). find_spec
  # avoids importing heavy modules just to probe them.
  /root/venv/bin/python - <<'PY' || { echo "[preflight] FATAL: a critical dep still missing after install — refusing to launch"; exit 3; }
import importlib.util, sys
missing = [m for m in ("curl_cffi", "patchright", "playwright", "openai", "asyncpg", "camoufox") if importlib.util.find_spec(m) is None]
if missing:
    print("MISSING:", ",".join(missing)); sys.exit(1)
print("[preflight] deps OK: curl_cffi patchright playwright openai asyncpg camoufox")
PY
  # camoufox LAUNCH smoke test — find_spec only proves the pip pkg is present; it does NOT prove the Firefox BINARY
  # launches (the libgtk failure is a runtime launch crash, invisible to an import check). Launch it once → if it can't,
  # WARN loud (not fatal: tier4 is a last-resort lane; the fleet still crawls every non-Akamai host). {AUDIT 2026-07-24:
  # import OK but BrowserType.launch died on libgtk-3} [CONFIDENCE: CONFIRMED — the exact gap a find_spec check misses].
  /root/venv/bin/python - <<'PY' || echo "[preflight] ⚠ camoufox Firefox WON'T launch — tier4 anti-Akamai DORMANT (check libgtk-3 / apt libs above)"
import asyncio
from camoufox.async_api import AsyncCamoufox
async def _t():
    async with AsyncCamoufox(headless=True) as b:
        p = await b.new_page(); await p.goto("https://example.com/"); assert await p.content()
asyncio.run(_t()); print("[preflight] camoufox Firefox launches OK (tier4 armed)")
PY
}
preflight

# ── run env for every worker (FULL QUALITY — never lowered) ──
export WATEREVENTS_DB_DSN="postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
export WATEREVENTS_DB_SCHEMA="waterevents"
export WATEREVENTS_RUN_ID="${WATEREVENTS_RUN_ID:-stress}"
# Short soft lease so a KILLED worker's company becomes reclaimable fast. The heartbeat renews every 60s (< 180s) while a
# worker is healthy, so a live crawl never lapses; but once the watchdog kills a stalled worker the heartbeat stops and
# the lease expires within 3 min → claim_company (which reclaims lease-expired 'discovering' rows) hands that company to
# another worker instead of it waiting the default 30 min. {infra for self-heal — killed → reclaimed fast}.
export WATEREVENTS_LEASE_MIN="${WATEREVENTS_LEASE_MIN:-3}"
export WATEREVENTS_HEARTBEAT_S="60"
# Workers NEVER self-exit on an empty queue — the SUPERVISOR owns lifecycle (kills on stall, kills all when the queue is
# truly drained). Without this, a worker that momentarily finds no claimable row exits after MAX_IDLE_ROUNDS and the
# supervisor relaunch-churns it. A high cap = "poll forever, idle-backoff, wait for the supervisor". {infra: no idle churn}.
export WATEREVENTS_MAX_IDLE_ROUNDS="${WATEREVENTS_MAX_IDLE_ROUNDS:-100000}"
export QWEN_BASE_URLS="http://127.0.0.1:8000/v1" QWEN_SERVED_NAME="qwen-vl"
export QWEN_API_KEY="sk-waterevents-0b1307fdf041607d7e55838c277320498bbee722867cad78"
export QWEN_MAX_TOKENS="12000" EVENT_VISION_TEXT_CHARS="24000" EVENT_MAX_PAGES="40" EVENT_USE_IMAGE="1"
# Bigger per-request VLM timeout: a full-quality call (image + 24k chars + up to 12k output) under batch load can take
# well over the old 120s → it spuriously TIMED OUT → the client retried (×3, exponential backoff) → the worker sat in
# the retry chain past the stall-watchdog window and got false-killed. 300s lets a legit heavy call finish. {DEBUG
# 2026-07-23 stalled workers were waiting in the VLM phase, not deadlocked}.
export QWEN_TIMEOUT_S="${QWEN_TIMEOUT_S:-300}"
# WEBSHARE residential proxy — ARMS tier2 (patchright residential render) + tier4 (camoufox anti-Akamai). WITHOUT this the
# whole 2668-run's 43 Akamai/Incapsula-walled big-caps (tesla/homedepot/nestle/allianz/...) fail unrecovered (98.3%). WITH
# it + libgtk (preflight above) the rewall test recovered 39/45 (→ ~99.8%). Override-style single static endpoint (webshare
# STATIC residential plan); swap the ip:port for any of the 20 in the plan, or set WEBSHARE_USERNAME/PASSWORD/PROXIES for
# the rotating gateway. {AUDIT 2026-07-24 residential_dormant + camoufox_libgtk} [CONFIDENCE: CONFIRMED — rewall 39/45].
export WEBSHARE_PROXY="${WEBSHARE_PROXY:-http://nknjgkpv:36oo15uctfhl@192.46.200.43:5713}"
# PER-WORKER render parallelism — the user's "20 workers × 5 pages, browser-level isolation" architecture. Each worker
# has ONE browser (BROWSERS=1) pinned to its own cores (taskset, below), streaming EVENT_BATCH pages continuously (no
# round barrier — see crawl.py). MAX_PAGES (the render semaphore) and SHOT_CONCURRENCY (concurrent full-page screenshots,
# the RAM hog) must both be ≥ EVENT_BATCH or they throttle the 5-way parallelism below the target. {USER 2026-07-23}.
export IR_WATERCRAWL_BROWSERS="${IR_WATERCRAWL_BROWSERS:-1}" IR_WATERCRAWL_MAX_PAGES="${IR_WATERCRAWL_MAX_PAGES:-6}"
export WATERCRAWL_SHOT_CONCURRENCY="${WATERCRAWL_SHOT_CONCURRENCY:-5}"
export IR_WATERCRAWL_NAV_TIMEOUT_MS="${IR_WATERCRAWL_NAV_TIMEOUT_MS:-25000}"
export EVENT_BATCH="${EVENT_BATCH:-5}" EVENT_RENDER_TRIES="${EVENT_RENDER_TRIES:-2}"
export EVENT_COMPANY_BUDGET_S="${EVENT_COMPANY_BUDGET_S:-600}"
export EVENT_TRACE_DIR="/workspace/WaterEvents/tests/stress1000/traces"

declare -A PGID                            # worker index -> process-group id (== the setsid leader pid)

# CPU ISOLATION. Pin each worker (and every Chromium it spawns — children inherit the affinity) to its OWN disjoint block
# of cores via taskset, so browsers NEVER fight over the same CPUs. WHY: on the shared pool the browsers contended for
# cores, so renders were slow/uneven and starved the GPU feed. Reserve the first RESERVE cores for the OS + the vLLM
# engine loop; split the rest evenly across the N workers. {USER 2026-07-23 "isolate the cpus so we can have several
# browsers completely isolated open"}.
NPROC=$(nproc 2>/dev/null || echo 32)
RESERVE=8                                  # cores 0..7 left for system + vLLM engine
PER=$(( (NPROC - RESERVE) / N )); [ "$PER" -lt 1 ] && PER=1

launch() {                                 # $1 = worker index; starts it as a fresh, CPU-pinned process group
  local i=$1
  local lo=$(( RESERVE + (i-1)*PER ))
  local hi=$(( lo + PER - 1 )); [ "$hi" -ge "$NPROC" ] && hi=$(( NPROC - 1 ))
  # WHY the pidfile: `setsid ... &` returns (in $!) the setsid PARENT pid, which forks the real child and EXITS
  # immediately — tracking $! made the watchdog think every worker died within ms and relaunch-storm them. The setsid
  # child writes its OWN pid ($$, == the new session/group leader) to a pidfile; that pid IS the process-group id, so
  # kill -0 <pid> checks the real worker and kill -9 -<pid> tears down the whole group (worker + its chromium).
  rm -f "$LOGD/worker_$i.pid"
  setsid bash -c "echo \$\$ > $LOGD/worker_$i.pid; exec taskset -c ${lo}-${hi} /root/venv/bin/python -m agent.event_agent.worker" \
    >"$LOGD/worker_$i.log" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$LOGD/worker_$i.pid" ] && break; sleep 0.3; done   # wait for the child to write its pid
  PGID[$i]=$(cat "$LOGD/worker_$i.pid" 2>/dev/null || echo 0)
  echo "[supervise] worker $i pinned to cores ${lo}-${hi}"
}

queue_remaining() {                        # count of companies still to do (queued OR mid-flight)
  /root/venv/bin/python - <<'PY' 2>/dev/null
import asyncio,asyncpg,os
async def t():
    c=await asyncpg.connect(os.environ["WATEREVENTS_DB_DSN"],statement_cache_size=0,server_settings={"search_path":"waterevents"})
    print(await c.fetchval("select count(*) from companies where status in ('queued','discovering')")); await c.close()
asyncio.run(t())
PY
}

for i in $(seq 1 "$N"); do launch "$i"; sleep 3; done   # STAGGER the launch — avoid N×browsers cold-starting at once
echo "[supervise] launched $N workers (stall-watchdog ${STALL_S}s)"

while true; do
  rem=$(queue_remaining); rem=${rem:-1}
  [ "$rem" = "0" ] && { echo "[supervise] queue drained — stopping"; break; }
  now=$(date +%s)
  for i in $(seq 1 "$N"); do
    f="$LOGD/worker_$i.log"
    if ! kill -0 "${PGID[$i]}" 2>/dev/null; then          # worker process gone (clean drain-exit or crash) → relaunch if work left
      echo "[supervise] worker $i exited — relaunch"; launch "$i"; sleep 2
      continue
    fi
    m=$(stat -c %Y "$f" 2>/dev/null || echo "$now")
    if [ $(( now - m )) -gt "$STALL_S" ]; then            # log silent too long → deadlock → kill the whole group + relaunch
      echo "[supervise] worker $i STALLED $((now-m))s — kill pgroup ${PGID[$i]} + relaunch (company lease will expire → reclaimed)"
      kill -9 -"${PGID[$i]}" 2>/dev/null                  # negative pid = kill the entire process GROUP (worker + its chromium)
      sleep 2; launch "$i"
    fi
  done
  sleep 20
done

for i in $(seq 1 "$N"); do kill -9 -"${PGID[$i]}" 2>/dev/null; done   # tear down all worker groups on exit
echo "[supervise] done $(date -u +%H:%M:%S)UTC"
