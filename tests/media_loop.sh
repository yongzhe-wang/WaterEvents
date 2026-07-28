#!/usr/bin/env bash
# One media-enrichment event per PROCESS → each render gets a BRAND-NEW resident Chromium, so the
# "TargetClosedError after 2-3 heavy renders" (browser gpu-process contends with vLLM/AWQ on the single A5000) never
# triggers — N is always 1. Clears 10media once, then appends each event (MEDIA_RUN_CLEAR=0). {POD 2026-07-23 crash@ev3}.
set -u
cd /workspace/WaterEvents
rm -rf tests/10media
export PYTHONPATH=/workspace/WaterEvents/backend
export QWEN_BASE_URLS=http://127.0.0.1:8000/v1
export QWEN_API_KEY=sk-waterevents-0b1307fdf041607d7e55838c277320498bbee722867cad78
export QWEN_MAX_TOKENS=12000 MEDIA_VISION_TEXT_CHARS=24000 QWEN_RETRIES=3
export MEDIA_RUN_DATASET=tests/datasets/pickset MEDIA_RUN_OUT=tests/10media MEDIA_RUN_CLEAR=0
for id in ev01 ev02 ev03 ev04 ev05 ev06 ev07 ev08 ev09 ev10; do
  echo "########## $id ##########"
  MEDIA_RUN_ONLY="$id" /root/venv/bin/python tests/media_run.py
done
echo "===ALL10_DONE==="
