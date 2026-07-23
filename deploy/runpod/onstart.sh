#!/bin/bash
# RunPod "Container Start Command" — runs on EVERY pod boot. WHY: /root is an ephemeral overlay (wiped on restart);
# only /workspace persists. A restart wipes /root/venv (vllm+playwright) + the server process (authorized_keys is
# handled by the account-level SSH key in RunPod Settings). This heals venv + server: rebuild venv if gone, then
# launch the supervised server. Idempotent: venv present → skip rebuild; supervisor is a flock singleton → double
# launch is a no-op. {AUDIT 2026-07-23: recover failure now propagates (recover.sh fixed) instead of launching a
# server on a broken venv.}
export PATH=/root/venv/bin:/usr/local/cuda/bin:$PATH

if ! /root/venv/bin/python -c "import vllm" 2>/dev/null; then
  echo "[onstart $(date -u +%FT%TZ)] venv missing — running recover.sh (~10min)" >> /workspace/onstart.log
  # AUDIT FIX: recover.sh now returns a REAL exit code — if it fails, do NOT launch a server on a broken venv.
  if ! bash /workspace/recover.sh >> /workspace/onstart.log 2>&1; then
    echo "[onstart $(date -u +%FT%TZ)] recover.sh FAILED — NOT starting server; check /workspace/onstart.log" >> /workspace/onstart.log
    exit 1
  fi
fi

# supervisor is a flock singleton — safe to launch unconditionally (a second one exits cleanly).
setsid nohup /workspace/supervise_vl.sh >> /workspace/supervisor.log 2>&1 < /dev/null &
echo "[onstart $(date -u +%FT%TZ)] supervisor launched" >> /workspace/onstart.log
