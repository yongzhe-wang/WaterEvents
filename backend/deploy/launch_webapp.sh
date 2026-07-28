#!/usr/bin/env bash
# launch_webapp — serve the built SPA + /api from ONE node process (dev-api-server.mjs, extended to serve dist). Detached
# so it survives the ssh session (nohup + </dev/null + disown + script-exit = clean channel close). {USER 2026-07-27 "web
# app on GCP media vm, off Vercel"}. Run: PORT=8080 bash deploy/launch_webapp.sh
set -u
# ../../frontend, not ../frontend: this script moved to backend/deploy/ in the 2026-07-28 restructure, so the repo root
# is now TWO levels up. Left at one level it would resolve to backend/frontend — a path that does not exist — and the
# webapp would refuse to start with "exit 2" while the fleet kept running, i.e. a silent site outage.
# {RESTRUCTURE 2026-07-28 "deploy/ → backend/deploy/"} [CONFIDENCE: CONFIRMED 100% — frontend/ stayed at the repo root].
cd "$(dirname "$0")/../../frontend" || exit 2
pkill -f '[d]ev-api-server' 2>/dev/null || true
sleep 1
export PORT="${PORT:-8080}"
export WEBAPP_DIST="${WEBAPP_DIST:-$(pwd)/dist}"
nohup node dev-api-server.mjs > /tmp/webserver.log 2>&1 < /dev/null &
disown
echo "[webapp] launched pid=$! PORT=$PORT DIST=$WEBAPP_DIST → /tmp/webserver.log"
