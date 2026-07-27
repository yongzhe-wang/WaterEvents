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
HOME_DIR="${EVENTINC_HOME:-/workspace/WaterEvents}"
PY="${EVENTINC_PY:-/root/venv/bin/python}"
cd "$HOME_DIR" || exit 2
N="${N:-4}"
NPROC=$(nproc 2>/dev/null || echo 8)
RESERVE="${EVENTINC_RESERVE:-8}"              # cores reserved for system + vLLM(+tunnel); workers pin to cores RESERVE.. (clamped)
LOGD="${EVENTINC_LOGD:-$HOME/eventinc_fleet}"
mkdir -p "$LOGD"

# ── run env (14B TEXT-ONLY — the hard constraint) ──
export WATEREVENTS_DB_DSN="${WATEREVENTS_DB_DSN:-postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres}"
export WATEREVENTS_DB_SCHEMA="waterevents"
export WATEREVENTS_RUN_ID="eventinc"
export QWEN_BASE_URLS="http://127.0.0.1:8000/v1" QWEN_SERVED_NAME="qwen-vl"
export QWEN_API_KEY="${QWEN_API_KEY:-sk-waterevents-0b1307fdf041607d7e55838c277320498bbee722867cad78}"
# 14B Qwen2.5-Instruct-AWQ has NO vision encoder → NEVER send a screenshot. NO_SHOT forces DOM/text-only; USE_IMAGE=0 too.
export WATERCRAWL_NO_SHOT="1" EVENT_USE_IMAGE="0"
export WATERCRAWL_HTTP_FIRST="0"
export WEBSHARE_PROXY="${WEBSHARE_PROXY:-http://nknjgkpv:36oo15uctfhl@192.46.200.43:5713}"
export EVENT_MAX_PAGES="${EVENT_MAX_PAGES:-30}" EVENT_BATCH="${EVENT_BATCH:-5}" EVENT_COMPANY_BUDGET_S="600"
export EVENTINC_WORKERS="${EVENTINC_WORKERS:-3}"
export EVENTINC_TOP_K="3" EVENTINC_PROFILE="${EVENTINC_PROFILE:-runpod}"
export PYTHONPATH="$HOME_DIR"

echo "[fleet] launching $N queue_worker + 1 pacer on $(hostname) (${NPROC} cores, reserve ${RESERVE}) home=$HOME_DIR"
for i in $(seq 1 "$N"); do
  core=$(( RESERVE + i - 1 )); [ "$core" -ge "$NPROC" ] && core=$(( NPROC - 1 ))   # clamp to a real core
  nohup taskset -c "$core" "$PY" -m agent.event_agent.scheduler.worker > "$LOGD/w$i.log" 2>&1 &
  echo "[fleet]   worker $i → core $core (pid $!)"
  sleep 2
done
nohup "$PY" -m agent.event_agent.scheduler.solver.pacer --loop > "$LOGD/pacer.log" 2>&1 &
disown -a 2>/dev/null || true
echo "[fleet]   pacer --loop (pid $!)"
echo "[fleet] UP. logs $LOGD/  stop: pkill -f '[e]vent_agent.queue_worker'; pkill -f '[e]vent_agent.pacer'"
