#!/usr/bin/env bash
# launch_fleet — start the EVENT scheduling fleet: N omnipotent queue_worker processes (taskset-pinned, one core each)
# draining the unified work_queue (incremental deep=1 first by priority, then full BFS backlog) + ONE pacer --loop
# (the packing solver that re-solves T* and re-spaces due_at hourly). Forever-recurring queue → NO drain-stop.
#
# 用一句话讲完: N 个 queue_worker 各绑一个核并行 render + 1 个 pacer 每小时重解 T*/重排 due_at → 整条 loop { claim →
# scan(incremental hash-gate deep=1 / full 多前沿 BFS) → flush events → full 完成 rederive 补 hub → complete 自 re-arm }
# 常驻跑。14B 无视觉 → NO_SHOT=1 + USE_IMAGE=0(强制 text-only)。PORTABLE: 路径/venv/核数全 env 驱动,RunPod 和 GCP VM 通用。
# {USER 2026-07-26 "launch everything"; 2026-07-27 "move pipeline to GCP media vm"} [CONFIDENCE: CONFIRMED].
#
# RunPod:   N=6 bash deploy/launch_fleet.sh
# GCP VM:   EVENTINC_PY=~/venv/bin/python EVENTINC_HOME=~/WaterEvents EVENTINC_RESERVE=2 N=6 bash deploy/launch_fleet.sh
set -u
# OWNERSHIP GUARD — runs before anything here has a side effect. BOTH target names are checked because the repo ships
# two installers that disagree on the name (systemd/install.sh writes waterevents-fleet.target, install_systemd.sh
# writes waterevents.target) and BOTH are currently installed on the live host, so checking one would miss the other.
. "$(dirname "$0")/_owner_guard.sh"
guard_owner waterevents.target        "sudo systemctl restart waterevents.target"
guard_owner waterevents-fleet.target  "sudo systemctl restart waterevents-fleet.target"
HOME_DIR="${EVENTINC_HOME:-/workspace/WaterEvents}"   # REPO ROOT — unchanged contract, callers still pass ~/WaterEvents
# CODE_DIR = the python import root. Since the 2026-07-28 restructure the top level holds only frontend/ backend/ tests/,
# so every python package (agent, providers, tools) sits one level down under backend/. Pointing cwd + PYTHONPATH HERE
# instead of at the repo root is what keeps EVERY import in the tree unchanged — `-m agent.event_agent.scheduler.worker`
# and `from providers import watercrawl` resolve exactly as before, so the restructure needed ZERO import rewrites.
# {RESTRUCTURE 2026-07-28 "TOP LEVEL = frontend/ backend/ tests/; agent|providers|deploy|scripts|supabase|tools → backend/"}
# [CONFIDENCE: CONFIRMED 100% — first-party import counts (agent 22 / providers 32 / tools 3) identical before and after].
CODE_DIR="$HOME_DIR/backend"
PY="${EVENTINC_PY:-/root/venv/bin/python}"
cd "$CODE_DIR" || exit 2
N="${N:-4}"
NPROC=$(nproc 2>/dev/null || echo 8)
RESERVE="${EVENTINC_RESERVE:-8}"              # cores reserved for system + vLLM(+tunnel); workers pin to cores RESERVE.. (clamped)
LOGD="${EVENTINC_LOGD:-$HOME/eventinc_fleet}"
mkdir -p "$LOGD"

# ── run env (14B TEXT-ONLY — the hard constraint) ──
# SECRETS ARE NOT DEFAULTED HERE. Each credential uses `${VAR:?msg}` — bash aborts with that message when the var is
# unset or empty, so a missing secret is a LOUD startup failure instead of a silent connect to the wrong place.
# WHY the literals are gone: this file is tracked in git, so a `${VAR:-<literal>}` default published the production
# Supabase DSN, the vLLM api-key and the Webshare proxy credentials to every clone and fork of the repo, permanently.
# A default is also WORSE than useless operationally — a typo'd env var silently falls back to PRODUCTION instead of
# failing, which is exactly how a test run ends up writing to the live database.
# Provision these out-of-band in /etc/waterevents.env; backend/deploy/systemd/install.sh now REQUIRES that file to
# pre-exist and validates each key, rather than sed-scraping the literals back out of this script.
# {GIT GREP 2026-07-28 "LAUNCH_FLEET.SH:31 EXPORT WATEREVENTS_DB_DSN=\"${WATEREVENTS_DB_DSN:-POSTGRESQL://POSTGRES.
#  EZUVMOLYFGSADKEHJNEF:FOCUSALPHA2026@AWS-1-US-EAST-1.POOLER.SUPABASE.COM:6543/POSTGRES}\" — A LIVE PRODUCTION
#  CREDENTIAL COMMITTED IN PLAINTEXT"} [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f].
export WATEREVENTS_DB_DSN="${WATEREVENTS_DB_DSN:?set WATEREVENTS_DB_DSN (Supabase Supavisor pooler DSN, port 6543) — provision in /etc/waterevents.env, never in this file}"
export WATEREVENTS_DB_SCHEMA="waterevents"
export WATEREVENTS_RUN_ID="eventinc"
export QWEN_BASE_URLS="http://127.0.0.1:8000/v1" QWEN_SERVED_NAME="qwen-vl"
export QWEN_API_KEY="${QWEN_API_KEY:?set QWEN_API_KEY (the --api-key the vLLM on the pod was launched with) — provision in /etc/waterevents.env}"
# 14B Qwen2.5-Instruct-AWQ has NO vision encoder → NEVER send a screenshot. NO_SHOT forces DOM/text-only; USE_IMAGE=0 too.
export WATERCRAWL_NO_SHOT="1" EVENT_USE_IMAGE="0"
export WATERCRAWL_HTTP_FIRST="0"
# WEBSHARE_PROXY is OPTIONAL, so it is passed through rather than `:?`-required: unset simply leaves the tier-2
# residential render lane dormant (webshare.playwright_proxy() returns None and the `if runtime._browser_proxy is not
# None` gate stays False), which degrades bot-walled hosts to 0 events but does NOT stop the fleet. Requiring it would
# turn an optional capability into a hard startup failure. The credential literal that used to be the default here is
# removed for the same reason as the DSN and the api-key: it was a live secret in a tracked file.
# {WATEREVENTS.ENV.EXAMPLE:33-35 "UNSET = TIER DORMANT: EVERY BOT-WALLED / IP-TARPITTED HOST ... IS NEVER RETRIED
#  THROUGH A RESIDENTIAL IP → PERMANENT 0-EVENTS FOR THOSE COMPANIES"}
# [CONFIDENCE: CONFIRMED 100% — the dormancy semantics are documented in the repo's own env example].
export WEBSHARE_PROXY="${WEBSHARE_PROXY:-}"
export EVENT_MAX_PAGES="${EVENT_MAX_PAGES:-30}" EVENT_BATCH="${EVENT_BATCH:-5}" EVENT_COMPANY_BUDGET_S="600"
export EVENTINC_WORKERS="${EVENTINC_WORKERS:-3}"
# ── BROWSER POOL CAP (post-incident). Each worker is its OWN python process and therefore builds its OWN watercrawl
# runtime, so these knobs multiply by N: the defaults (BROWSERS=3, MAX_PAGES=24) meant 6 workers × 3 = 18 Chromium
# processes and 6 × 24 = 144 concurrent pages ≈ 8.6GB of page memory alone (config.py:12 "Peak mem ≈ MAX_PAGES×~60MB"),
# on a 32GB box with no swap. On 2026-07-27 22:11 that tipped the VM into a page-cache thrash livelock: disk reads pinned
# at the 3600 IOPS / ~140-176 MB/s instance ceiling for 4.5h with writes starved to ~0, so journald, DHCP renewal and
# every worker blocked — the box stayed up at ~17% CPU (pure iowait) but could do nothing, and the OOM killer never fired
# because with no swap the kernel could always "successfully" reclaim more page cache.
# WHY cutting this is nearly free: the pacer measures RENDER as the NON-binding lane, so render concurrency is not what
# limits throughput — the GPU is. {PACER 2026-07-28 "T*=13.87h binding=vlm | C_R=609p/h C_V=412c/h | T_render=3.77h
# T_vlm=13.87h"} → T_vlm is 3.7× T_render, so we can shed render concurrency without moving the bottleneck.
# {MEASURED 2026-07-28 "155 chromium processes / 9.49GB chrome-headless RSS after only 12 minutes of fleet uptime"}
# [CONFIDENCE: CONFIRMED 100% — disk metrics pinned at exactly 3600.0 read ops/s for 4h; binding=vlm read off the live
#  scheduler_state, so the throughput cost of this cap is bounded by the render lane's 3.7× slack].
export IR_WATERCRAWL_MAX_PAGES="${IR_WATERCRAWL_MAX_PAGES:-8}"   # per worker → 6×8 = 48 pages fleet-wide (was 144)
export IR_WATERCRAWL_BROWSERS="${IR_WATERCRAWL_BROWSERS:-2}"     # per worker → 6×2 = 12 chromium procs (was 18)
export EVENTINC_TOP_K="3" EVENTINC_PROFILE="${EVENTINC_PROFILE:-runpod}"
export PYTHONPATH="$CODE_DIR"                        # backend/ is the import root (see CODE_DIR note above)

echo "[fleet] launching $N queue_worker + 1 pacer on $(hostname) (${NPROC} cores, reserve ${RESERVE}) home=$HOME_DIR"
# APPEND, never truncate. `>` destroyed the only copy of the crash-window worker logs during the 2026-07-27 22:11
# incident recovery — the relaunch wiped exactly the evidence the post-mortem needed, so the RCA had to be rebuilt from
# GCE disk metrics and the persistent journal instead. Logs are the incident record; a restart must never erase them.
# Size is bounded by logrotate (deploy/waterevents-logrotate.conf) rather than by truncation-on-start.
# {INCIDENT 2026-07-27 "launch_fleet.sh uses `> $LOGD/w$i.log` → the 16:04-22:11 worker logs were lost on relaunch"}
# [CONFIDENCE: CONFIRMED 100% — the loss happened and directly blocked the memory half of the root-cause analysis].
_stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
for i in $(seq 1 "$N"); do
  core=$(( RESERVE + i - 1 )); [ "$core" -ge "$NPROC" ] && core=$(( NPROC - 1 ))   # clamp to a real core
  echo "=== [fleet] worker $i start $_stamp ===" >> "$LOGD/w$i.log"
  nohup taskset -c "$core" "$PY" -m agent.event_agent.scheduler.worker >> "$LOGD/w$i.log" 2>&1 &
  echo "[fleet]   worker $i → core $core (pid $!)"
  sleep 2
done
echo "=== [fleet] pacer start $_stamp ===" >> "$LOGD/pacer.log"
nohup "$PY" -m agent.event_agent.scheduler.solver.pacer --loop >> "$LOGD/pacer.log" 2>&1 &
disown -a 2>/dev/null || true
echo "[fleet]   pacer --loop (pid $!)"
# Stop patterns must track the POST-RESTRUCTURE module paths (scheduler.worker / scheduler.solver.pacer). The previous
# hint named `queue_worker` and `event_agent.pacer`, neither of which exists since the 2026-07-26 package split — anyone
# following it would have killed nothing and concluded the fleet was unstoppable. {PS 2026-07-27 "-m agent.event_agent
# .scheduler.worker" ×6} [CONFIDENCE: CONFIRMED 100% — module paths read off the live process table].
echo "[fleet] UP. logs $LOGD/  stop: pkill -f '[e]vent_agent.scheduler.worker'; pkill -f '[e]vent_agent.scheduler.solver.pacer'"
