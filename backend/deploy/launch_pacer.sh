#!/usr/bin/env bash
# launch_pacer — start the packing-solver controller (pacer --loop) as a detached nohup process. Separate from
# launch_fleet.sh so the pacer can be (re)started independently of the worker pool (e.g. after a code change) without
# touching the running workers. {USER 2026-07-26 full end-to-end launch}.
#
# PORTABLE like launch_fleet.sh — path/venv are env-driven so RunPod and the GCP VM share one script.
#   RunPod:  bash deploy/launch_pacer.sh
#   GCP VM:  EVENTINC_PY=~/venv/bin/python EVENTINC_HOME=~/WaterEvents bash deploy/launch_pacer.sh
set -u
# OWNERSHIP GUARD — a SECOND pacer is the worst duplicate of all: two solvers write scheduler_state and re-space the
# same due_at column, so T* oscillates. On 2026-07-28 that took T* from 13.87h to 89.95h, which drained the incremental
# pool and left workers claiming almost nothing but `full` (~16 VLM calls each) until the vLLM queue hit 391 waiting.
. "$(dirname "$0")/_owner_guard.sh"
guard_owner waterevents-pacer.service "sudo systemctl restart waterevents-pacer.service"
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
# NO DSN DEFAULT — `${VAR:?msg}` aborts loudly when unset. This file is tracked in git, so the literal that used to sit
# here published the production Supabase credential to every clone; and a silent fallback to production is the exact
# mechanism by which a mis-set env var turns a test pacer into one that re-spaces the LIVE queue's due_at. Same
# treatment as launch_fleet.sh:31. Provision in /etc/waterevents.env.
# {GIT GREP 2026-07-28 "LAUNCH_PACER.SH:25 EXPORT WATEREVENTS_DB_DSN=\"${WATEREVENTS_DB_DSN:-POSTGRESQL://POSTGRES.
#  EZUVMOLYFGSADKEHJNEF:FOCUSALPHA2026@...}\" — THE SAME LIVE CREDENTIAL, SECOND COPY"}
# [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f].
export WATEREVENTS_DB_DSN="${WATEREVENTS_DB_DSN:?set WATEREVENTS_DB_DSN (Supabase Supavisor pooler DSN, port 6543) — provision in /etc/waterevents.env, never in this file}"
export EVENTINC_PROFILE="${EVENTINC_PROFILE:-runpod}"
export PYTHONPATH="$CODE_DIR"                        # backend/ is the import root
nohup "$PY" -m agent.event_agent.scheduler.solver.pacer --loop > "$LOGD/pacer.log" 2>&1 &
disown
echo "[pacer] launched (pid $!) → $LOGD/pacer.log"
