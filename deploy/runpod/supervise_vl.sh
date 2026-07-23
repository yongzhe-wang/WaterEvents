#!/bin/bash
# Keep the vLLM server alive: SINGLETON (flock) + restart-on-crash with backoff + LOUD crash-loop alert.
# WHY each guard:
#  - AUDIT FIX #3 (singleton): two supervisors → two vLLM both bind :8000 → one crash-loops forever. flock makes a
#    second launch a harmless no-op. {AUDIT 2026-07-23 "no singleton guard → port :8000 conflict crash-loop (hit in prod)"}
#  - AUDIT FIX #4 (backoff + alert): a serve that dies in <30s (bad venv/config) must NOT silently respin every 5s
#    forever — ramp the backoff and SHOUT after 3 fast fails so a broken server is unmissable. {AUDIT 2026-07-23}.
cd /workspace

# Singleton: hold an exclusive lock on fd 9; a second instance can't get it → exits cleanly.
exec 9>/workspace/.supervisor.lock
if ! flock -n 9; then
  echo "[supervisor $(date -u +%FT%TZ)] another supervisor holds the lock — exiting" >> /workspace/supervisor.log
  exit 0
fi

fails=0                                                    # consecutive FAST (<30s) failures = crash-loop signal
while true; do
  echo "[supervisor $(date -u +%FT%TZ)] starting vllm" >> /workspace/supervisor.log
  t0=$(date +%s)
  /workspace/serve_vl.sh >> /workspace/vllm_vl.log 2>&1    # runs until the server exits (crash / OOM / kill)
  rc=$?
  dt=$(( $(date +%s) - t0 ))                               # how long it stayed up — <30s means it never really served
  if [ "$dt" -lt 30 ]; then fails=$((fails + 1)); else fails=0; fi
  echo "[supervisor $(date -u +%FT%TZ)] vllm exited rc=$rc after ${dt}s (fast-fails=$fails)" >> /workspace/supervisor.log
  if [ "$fails" -ge 3 ]; then                              # 3 instant deaths in a row → not transient, LOUD
    echo "[supervisor $(date -u +%FT%TZ)] WARNING vllm CRASH-LOOPING ($fails fast fails, rc=$rc) — CHECK /workspace/vllm_vl.log" >> /workspace/supervisor.log
  fi
  # backoff: 5s on a clean long run; on fast-fails ramp 10s,20s,30s… capped at 60s so we don't hammer.
  if [ "$fails" -gt 0 ]; then
    s=$(( fails * 10 )); [ "$s" -gt 60 ] && s=60
  else
    s=5
  fi
  sleep "$s"
done
