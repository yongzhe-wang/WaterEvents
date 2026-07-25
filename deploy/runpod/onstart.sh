#!/bin/bash
# RunPod "Container Start Command" — runs on EVERY pod boot. It REPLACES the image's default CMD (/start.sh), so it
# MUST first do what /start.sh does or SSH + the HTTP proxy break.
#
# ROOT CAUSE we hit (2026-07-23): setting the start command to a bare heal script REPLACED /start.sh → no sshd (direct
# SSH "Connection refused"), no nginx (the :8000 HTTP proxy 404/502-flapped), no $PUBLIC_KEY injection (locked out).
# /start.sh runs: start_nginx (serves the :8000 proxy) → setup_ssh (injects $PUBLIC_KEY + `service ssh start`) →
# start_jupyter → export_env_vars → execute_script /post_start.sh → `sleep infinity` (keeps the container alive).
# {POD /start.sh:88-102 "start_nginx ... setup_ssh ... sleep infinity"} [CONFIDENCE: CONFIRMED — read the script + the
# refused→"Permission denied"→connect transition proved sshd only comes back when /start.sh runs].
#
# FIX: run /start.sh in the BACKGROUND FIRST (brings up sshd/nginx/key immediately + its sleep infinity keeps the pod
# alive), THEN self-heal the venv (wiped from ephemeral /root on every restart) + launch the vLLM server, then `wait`
# on /start.sh so this process never exits (container stays up). SSH works within seconds; the server heals in parallel.
set +e

# 1) RunPod default startup in the background — sshd + nginx(:8000 proxy) + $PUBLIC_KEY inject + jupyter + sleep infinity.
/start.sh &
_START_PID=$!

# 2) Belt-and-suspenders SSH key inject (in case the account $PUBLIC_KEY is unset — that was the recurring lockout).
mkdir -p ~/.ssh
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINU2xSaa+GusfIAWBu6khpHoCPtP76qReHCA8lPG/Jt5 yongzhe@sas.upenn.edu" >> ~/.ssh/authorized_keys
sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys      # dedup so repeats don't grow the file
chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys

export PATH=/root/venv/bin:/usr/local/cuda/bin:$PATH

# 3) Heal the venv if the restart wiped it (rebuild from the PERSISTENT /workspace/.pipcache wheel cache → ~5min, no
#    re-download). If recover fails, do NOT launch a server on a broken venv (fail loud) — but DON'T kill the pod
#    (sshd/proxy from /start.sh stay up so we can debug).
if ! /root/venv/bin/python -c "import vllm" 2>/dev/null; then
  echo "[onstart $(date -u +%FT%TZ)] venv missing — rebuilding from /workspace/.pipcache" >> /workspace/onstart.log
  if ! bash /workspace/recover.sh >> /workspace/onstart.log 2>&1; then
    echo "[onstart $(date -u +%FT%TZ)] recover.sh FAILED — NOT launching server; SSH/proxy stay up for debug" >> /workspace/onstart.log
    wait "$_START_PID"                                         # keep pod alive on /start.sh even if heal failed
    exit 0
  fi
fi

# 3.5) Ensure the WaterEvents tools deps are present EVEN WHEN the venv survived the restart — step 3 only rebuilds when
#      vllm is missing, but a venv WITH vllm and WITHOUT curl_cffi silently 0-links every Akamai/Q4 IR page (the exact
#      state that made gcs-web/avnet/mcdonalds render fail with ERR_HTTP2 while impersonate.available()=False). Idempotent
#      check-then-install: only pip-installs when curl_cffi is actually absent, reinstalling from /workspace/.pipcache in
#      seconds. {DEBUG 2026-07-24: curl_cffi absent → impersonate lane dead → 15/28 URLs failing; installing it → 27/28 ok}
#      [CONFIDENCE: CONFIRMED 100% — root cause was the missing dep, not any render-code bug].
if ! /root/venv/bin/python -c "import curl_cffi" 2>/dev/null; then
  echo "[onstart $(date -u +%FT%TZ)] curl_cffi missing — installing (render-critical impersonate lane)" >> /workspace/onstart.log
  PIP_CACHE_DIR=/workspace/.pipcache /root/venv/bin/pip install curl_cffi >> /workspace/onstart.log 2>&1
fi

# 3b) SYSTEM libs for camoufox's Firefox (tier4 anti-Akamai). A pod rebuild/volume-reset ships WITHOUT libgtk-3.so.0 → the
#     camoufox-bin (a Firefox fork) crashes at launch "Couldn't load XPCOM, exitCode=255" → tier4 SILENTLY dead → the 43
#     Akamai/Incapsula-walled big-caps (tesla/homedepot/nestle) never recover (the 2668-run's whole failed tail). apt is
#     idempotent — present libs skip in <1s. {AUDIT 2026-07-24 camoufox_libgtk_missing; rewall recovered 39/45 once
#     installed} [CONFIDENCE: CONFIRMED — before: 45 walled; after: 39 recovered → ~99.8%].
if ! ldconfig -p 2>/dev/null | grep -q "libgtk-3.so.0"; then
  echo "[onstart $(date -u +%FT%TZ)] libgtk-3 missing — installing camoufox/Firefox libs (tier4 anti-Akamai lane)" >> /workspace/onstart.log
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libgtk-3-0 libasound2 libdbus-glib-1-2 libx11-xcb1 libxt6 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libpango-1.0-0 libcairo2 >> /workspace/onstart.log 2>&1
fi

# 4) Launch the supervised (flock-singleton, OOM-gated, auto-restarting) vLLM server.
setsid nohup /workspace/supervise_vl.sh >> /workspace/supervisor.log 2>&1 < /dev/null &
echo "[onstart $(date -u +%FT%TZ)] supervisor launched; venv OK; SSH+proxy via /start.sh" >> /workspace/onstart.log

# 5) Keep the container alive by waiting on /start.sh's `sleep infinity`. Without this, the Container Start Command
#    would exit → RunPod would consider the pod finished and stop it.
wait "$_START_PID"
