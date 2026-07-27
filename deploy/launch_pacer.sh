#!/usr/bin/env bash
# launch_pacer — start the packing-solver controller (pacer --loop) as a detached nohup process. Separate from
# launch_fleet.sh so the pacer can be (re)started independently of the worker pool (e.g. after a code change) without
# touching the running workers. {USER 2026-07-26 full end-to-end launch}.
set -u
cd /workspace/WaterEvents || exit 2
LOGD=/workspace/eventinc_fleet
mkdir -p "$LOGD"
pkill -f 'event_agent.pacer' 2>/dev/null || true      # kill any prior pacer (only one should run)
sleep 2
export WATEREVENTS_DB_DSN="postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
export EVENTINC_PROFILE="${EVENTINC_PROFILE:-runpod}"
export PYTHONPATH="/workspace/WaterEvents"
nohup /root/venv/bin/python -m agent.event_agent.scheduler.solver.pacer --loop > "$LOGD/pacer.log" 2>&1 &
disown
echo "[pacer] launched (pid $!) → $LOGD/pacer.log"
