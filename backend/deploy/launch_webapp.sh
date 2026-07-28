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
# BASIC AUTH credential — sourced from the environment or from the root-only /etc/waterevents/webapp.env, NEVER written
# into this repo. The dashboard listens on 0.0.0.0 behind a 0.0.0.0/0 firewall rule, so an unauthenticated start would
# publish every crawled event and the whole queue state to the internet. dev-api-server.mjs is fail-closed: with no
# password it answers 503 rather than serving, so a forgotten variable is loud instead of silently open.
# WHY a separate env file rather than fleet.env: the webapp runs as an unprivileged process and needs exactly this one
# secret; reusing the fleet's file would hand it the database DSN and the vLLM key for no reason.
# {AUDIT 2026-07-28 "PORT 8080 IS INTERNET-REACHABLE WITH NO AUTHENTICATION"}
# [CONFIDENCE: CONFIRMED 100% — `gcloud compute firewall-rules list` shows allow-webapp-8080 sourced 0.0.0.0/0].
if [ -z "${WEBAPP_PASSWORD:-}" ] && [ -r /etc/waterevents/webapp.env ]; then
  set -a; . /etc/waterevents/webapp.env; set +a
fi
export WEBAPP_USER="${WEBAPP_USER:-focusalpha}"
export WEBAPP_PASSWORD="${WEBAPP_PASSWORD:-}"
[ -n "$WEBAPP_PASSWORD" ] || echo "[webapp] ⚠️  WEBAPP_PASSWORD unset — the dashboard will answer 503 until it is set" >&2
nohup node dev-api-server.mjs > /tmp/webserver.log 2>&1 < /dev/null &
disown
echo "[webapp] launched pid=$! PORT=$PORT DIST=$WEBAPP_DIST auth=$([ -n "$WEBAPP_PASSWORD" ] && echo on || echo OFF) → /tmp/webserver.log"
