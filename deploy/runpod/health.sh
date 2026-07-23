#!/bin/bash
# Health check: /v1/models + a tiny completion + an auth-negative probe. AUDIT FIX #5: the old version printed the
# body but ALWAYS returned 0 — a 401/500 read as "healthy". Now every probe checks the HTTP status and the script
# exits non-zero if any fails, so a monitor/onstart can actually detect a sick server.
# {AUDIT 2026-07-23 "health.sh didn't check HTTP status → false-green on 401/500"}
if [ -f /workspace/vllm.env ]; then source /workspace/vllm.env; fi
fail=0

echo "=== /v1/models (auth) ==="
code=$(curl -s -o /tmp/h_models.txt -w "%{http_code}" -H "Authorization: Bearer ${QWEN_API_KEY:-}" http://127.0.0.1:8000/v1/models)
echo "HTTP $code"; head -c 200 /tmp/h_models.txt; echo
[ "$code" = "200" ] || { echo "  -> FAIL (expected 200)"; fail=1; }

echo "=== tiny completion (auth) ==="
code=$(curl -s -o /tmp/h_chat.txt -w "%{http_code}" -H "Authorization: Bearer ${QWEN_API_KEY:-}" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/chat/completions \
  -d '{"model":"qwen-vl","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}')
echo "HTTP $code"; head -c 300 /tmp/h_chat.txt; echo
[ "$code" = "200" ] || { echo "  -> FAIL (expected 200)"; fail=1; }

echo "=== auth-negative (no key MUST be rejected) ==="
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/v1/models)
echo "no-key HTTP $code"
[ "$code" = "401" ] || { echo "  -> FAIL (expected 401 — server may be UNPROTECTED)"; fail=1; }

if [ "$fail" = "0" ]; then echo "HEALTH: OK"; else echo "HEALTH: FAIL"; fi
exit "$fail"
