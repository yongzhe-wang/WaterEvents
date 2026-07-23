#!/usr/bin/env bash
# WaterEvents discovery worker launcher (GCE worker VM). Sources the env file, PREFLIGHTS the two things that silently
# broke real runs (VLM down → every page fails; chromium version-mismatch → every render fails), then runs the worker.
# WHY preflight: fail FAST + LOUD at launch instead of discovering the outage one company at a time. {DEBUG 2026-07-23:
# run1 chromium build 1223≠1228 → 4 pages failed_render; run2 RunPod 502 → all extract failed — both are launch-time
# detectable}. Usage: bash deploy/run_discovery.sh
set -euo pipefail

ENV_FILE="${WATEREVENTS_ENV_FILE:-$HOME/.waterevents.env}"
[ -f "$ENV_FILE" ] || { echo "[run] ✗ missing $ENV_FILE — copy deploy/waterevents.env.example, fill secrets, retry"; exit 1; }
set -a; source "$ENV_FILE"; set +a
CODE="${WATEREVENTS_CODE:-$HOME/WaterEvents}"
cd "$CODE"

# ── preflight 1: RunPod VLM reachable (auth'd) ── else every extract 400/502s
code=$(curl -s -m12 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${QWEN_API_KEY}" "${QWEN_BASE_URLS%/}/models" || echo 000)
[ "$code" = "200" ] && echo "[run] ✓ VLM reachable (${QWEN_BASE_URLS})" \
  || { echo "[run] ✗ VLM NOT reachable — HTTP $code at ${QWEN_BASE_URLS}. Pod down / port 8000 not exposed / vLLM not --host 0.0.0.0. ABORT."; exit 1; }

# ── preflight 2: chromium actually launches ── else every render fails (playwright build mismatch)
PYTHONPATH="$CODE" python3 - <<'PY' || { echo "[run] ✗ chromium won't launch — run: python3 -m playwright install chromium. ABORT."; exit 1; }
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); b.close()
print("[run] ✓ chromium launches")
PY

# ── preflight 3: DB reachable AND the worker's schema really has the companies table ──
# MUST pin search_path to the SAME schema the pool uses (WATEREVENTS_DB_SCHEMA, default waterevents) — else this counts
# `public.companies` while the worker reads `waterevents.companies` → "0 queued" false-green vs a full queue. {AUDIT
# 2026-07-23 HIGH: preflight connected with no search_path → checked the wrong schema}.
PYTHONPATH="$CODE" python3 - <<'PY' || { echo "[run] ✗ DB not reachable / wrong schema at WATEREVENTS_DB_DSN. ABORT."; exit 1; }
import asyncio, os, asyncpg
_schema = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")   # same default as db.py _SCHEMA
async def _c():
    c = await asyncpg.connect(os.environ["WATEREVENTS_DB_DSN"], statement_cache_size=0,
                              server_settings={"search_path": _schema})
    n = await c.fetchval("SELECT count(*) FROM companies WHERE status='queued'"); await c.close()
    print(f"[run] ✓ DB reachable — schema={_schema}, {n} companies queued")
asyncio.run(_c())
PY

echo "[run] all preflights passed → starting discovery worker"
PYTHONPATH="$CODE" exec python3 -m agent.event_agent.worker
