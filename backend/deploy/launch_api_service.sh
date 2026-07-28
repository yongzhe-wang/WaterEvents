#!/usr/bin/env bash
# launch_api_service — start the PUBLIC read-only API (:8090). Detached, like launch_webapp.sh.
#
# 用一句话讲完: 起一个独立 aiohttp 进程对外开放 /today/pulse;它只调 PostgREST,不碰 Postgres,所以起停它对
# 爬虫 fleet(Supavisor) 和 webapp(:8080) 都没有任何影响 —— 三者互不干扰,可以单独重启。
#
# Port map on ir-media-8:  8000 vLLM tunnel → RunPod · 8080 webapp (node) · 8090 THIS.
# {USER 2026-07-28 "we need to separate the supavisor"} [CONFIDENCE: CONFIRMED — no DB pool is opened by this process].
#
# Run: EVENTINC_HOME=~/WaterEvents EVENTINC_PY=~/venv/bin/python PORT=8090 bash backend/deploy/launch_api_service.sh
set -u
HOME_DIR="${EVENTINC_HOME:-/workspace/WaterEvents}"   # REPO ROOT — same contract as launch_fleet.sh
CODE_DIR="$HOME_DIR/backend"                          # python import root since the 2026-07-28 restructure
PY="${EVENTINC_PY:-/root/venv/bin/python}"
cd "$CODE_DIR" || exit 2
LOGD="${EVENTINC_LOGD:-$HOME/eventinc_fleet}"
mkdir -p "$LOGD"

# Only ever ONE instance: a second process would bind the same port and die, leaving a confusing half-state.
pkill -f 'api_service[.]main' 2>/dev/null || true
sleep 1

export PYTHONPATH="$CODE_DIR"
export PORT="${PORT:-8090}"
# Tunables — all optional, defaults live in api_service/main.py.
export API_CACHE_TTL_S="${API_CACHE_TTL_S:-10}"
export API_RATE_MAX="${API_RATE_MAX:-60}"
# Shared secret for the WRITE endpoint (/queue/boost). Passed through from the caller's environment and NOT stored in
# this file — leave it unset and the write endpoint stays disabled, so a routine restart can never silently re-arm a
# queue-steering surface. Must equal waterevents.api_tokens.token WHERE name='queue_boost'.
export QUEUE_BOOST_TOKEN="${QUEUE_BOOST_TOKEN:-}"

# APPEND, never truncate: a restart must not destroy the log that explains why the previous run stopped. Same lesson as
# launch_fleet.sh, where `>` wiped the crash-window worker logs during the 2026-07-27 22:11 incident recovery.
nohup "$PY" -m api_service.main >> "$LOGD/api_service.log" 2>&1 < /dev/null &
disown -a 2>/dev/null || true
echo "[api_service] launched pid=$! PORT=$PORT → $LOGD/api_service.log"
echo "[api_service] stop: pkill -f '[a]pi_service.main'"
