#!/usr/bin/env bash
# Qwen2.5-VL-7B — VISION baseline. Takes a RENDERED PAGE SCREENSHOT (+ optional text) and judges structure by
# LAYOUT: a nav bar / footer / cookie banner LOOKS different from an event list — a visual signal the text-only
# router can't see. Fast VLM (~16GB, 1 GPU). Use for the over-classification-hard pages (bat.com nav-flood etc.).
#
#   ./serve.sh                 # GPU 3, port 8003
# Client sends OpenAI vision messages: {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}.
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen2.5-VL-7B-Instruct}"
GPU="${GPU:-3}"; PORT="${PORT:-8003}"; TP="${TP:-1}"
export HF_HOME="${HF_HOME:-/mnt/data/hf_cache}"; export VLLM_LOGGING_LEVEL=WARNING
mkdir -p /mnt/data/qwen_logs
echo "[VL-7B] GPU $GPU port $PORT model $MODEL"
# --limit-mm-per-prompt image=1: one screenshot per page. mm-processor max_pixels caps image tokens (cost/speed):
# 1280*28*28 ≈ 1M px → ~1300 vision tokens; lower it to spend less. {a full-page screenshot needs enough px to read links}
CUDA_VISIBLE_DEVICES=$GPU nohup python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" --served-model-name qwen-vl \
  --tensor-parallel-size "$TP" --port "$PORT" \
  --max-model-len "${MAXLEN:-32768}" --max-num-seqs 64 --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt image=1 \
  --mm-processor-kwargs '{"max_pixels": 1003520}' \
  --disable-log-requests \
  > "/mnt/data/qwen_logs/vl_7b.log" 2>&1 &
echo "  PID $! → /mnt/data/qwen_logs/vl_7b.log"
