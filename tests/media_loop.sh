#!/usr/bin/env bash
# One media-enrichment event per PROCESS → each render gets a BRAND-NEW resident Chromium, so the
# "TargetClosedError after 2-3 heavy renders" (browser gpu-process contends with vLLM/AWQ on the single A5000) never
# triggers — N is always 1. Clears 10media once, then appends each event (MEDIA_RUN_CLEAR=0). {POD 2026-07-23 crash@ev3}.
set -u
cd /workspace/WaterEvents
rm -rf tests/10media
export PYTHONPATH=/workspace/WaterEvents/backend
export QWEN_BASE_URLS=http://127.0.0.1:8000/v1
# NO KEY LITERAL — fourth copy of the same committed vLLM api-key. `${VAR:?msg}` aborts before the `rm -rf tests/10media`
# above has any successor work, so a missing key fails immediately instead of after ten no-op per-event subprocesses.
# {GIT GREP 2026-07-28 "MEDIA_LOOP.SH:10 EXPORT QWEN_API_KEY=SK-WATEREVENTS-0B1307FDF041607D7E55838C277320498BBEE722867CAD78"}
# [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f].
# NO APOSTROPHE IN THE MESSAGE. Bash parses the word of `${VAR:?word}` under quote rules EVEN inside double quotes, so
# a lone `'` (the fix originally read "the pod's vLLM key") leaves the whole rest of the file inside an unterminated
# single-quoted string — the script then cannot be parsed at all, let alone run.
# {BASH -N 2026-07-28 "MEDIA_LOOP.SH: LINE 14: UNEXPECTED EOF WHILE LOOKING FOR MATCHING `''"}
# [CONFIDENCE: CONFIRMED 100% — isolated repro: `x="${FOO:?the pod's key}"` fails bash -n; identical line without the
#  apostrophe passes].
export QWEN_API_KEY="${QWEN_API_KEY:?set QWEN_API_KEY (the vLLM --api-key of the pod) before running media_loop.sh}"
export QWEN_MAX_TOKENS=12000 MEDIA_VISION_TEXT_CHARS=24000 QWEN_RETRIES=3
export MEDIA_RUN_DATASET=tests/datasets/pickset MEDIA_RUN_OUT=tests/10media MEDIA_RUN_CLEAR=0
for id in ev01 ev02 ev03 ev04 ev05 ev06 ev07 ev08 ev09 ev10; do
  echo "########## $id ##########"
  MEDIA_RUN_ONLY="$id" /root/venv/bin/python tests/media_run.py
done
echo "===ALL10_DONE==="
