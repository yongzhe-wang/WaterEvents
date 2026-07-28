#!/usr/bin/env bash
# launch_pacer — start the packing-solver controller (pacer --loop) as a detached nohup process. Separate from
# launch_fleet.sh so the pacer can be (re)started independently of the worker pool (e.g. after a code change) without
# touching the running workers. {USER 2026-07-26 full end-to-end launch}.
#
# PORTABLE like launch_fleet.sh — path/venv are env-driven so RunPod and the GCP VM share one script.
#   RunPod:  bash deploy/launch_pacer.sh
#   GCP VM:  EVENTINC_PY=~/venv/bin/python EVENTINC_HOME=~/WaterEvents bash deploy/launch_pacer.sh
set -u
HOME_DIR="${EVENTINC_HOME:-/workspace/WaterEvents}"   # REPO ROOT — unchanged contract
CODE_DIR="$HOME_DIR/backend"                         # python import root since the 2026-07-28 restructure (see launch_fleet.sh)
PY="${EVENTINC_PY:-/root/venv/bin/python}"
cd "$CODE_DIR" || exit 2
LOGD="${EVENTINC_LOGD:-$HOME/eventinc_fleet}"
mkdir -p "$LOGD"
# Kill any prior pacer — ONLY ONE may run, because two pacers both _respace_incremental() and both publish
# scheduler_state, so they fight over every hub's due_at and the published T* flaps between their two solves.
# The pattern must match the POST-RESTRUCTURE module path `agent.event_agent.scheduler.solver.pacer`; the old
# 'event_agent.pacer' pattern silently matched NOTHING after the 2026-07-26 package split (there is no ".pacer"
# directly under event_agent any more), so every "restart" quietly left the previous pacer alive.
# {PS 2026-07-27 ir-media-8 "55863 ... -m agent.event_agent.scheduler.solver.pacer --loop"}
# [CONFIDENCE: CONFIRMED 100% — 'event_agent.pacer' cannot match 'event_agent.scheduler.solver.pacer'].
pkill -f 'event_agent[.]scheduler[.]solver[.]pacer' 2>/dev/null || true
sleep 2
export WATEREVENTS_DB_DSN="${WATEREVENTS_DB_DSN:-postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres}"
export EVENTINC_PROFILE="${EVENTINC_PROFILE:-runpod}"
export PYTHONPATH="$CODE_DIR"                        # backend/ is the import root
nohup "$PY" -m agent.event_agent.scheduler.solver.pacer --loop > "$LOGD/pacer.log" 2>&1 &
disown
echo "[pacer] launched (pid $!) → $LOGD/pacer.log"
