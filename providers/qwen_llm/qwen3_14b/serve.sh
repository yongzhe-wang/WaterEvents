#!/usr/bin/env bash
# Qwen3-14B — the strong dense mid-tier (~28GB fp16, 1 GPU). Better reasoning on hard/ambiguous IR pages than 8B,
# lower throughput. Launches ONE replica on GPU $GPU / port $PORT (default GPU 1 / 8001 so it sits next to 8B/30B).
#
#   ./serve.sh                 # GPU 1, port 8001
#   GPU=1 PORT=8001 ./serve.sh
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen3-14B}"
GPU="${GPU:-1}"; PORT="${PORT:-8001}"; TP="${TP:-1}"; REPLICAS="${REPLICAS:-1}"
export HF_HOME="${HF_HOME:-/mnt/data/hf_cache}"; export VLLM_LOGGING_LEVEL=WARNING
mkdir -p /mnt/data/qwen_logs
for ((i=0;i<REPLICAS;i++)); do
  g=$(seq -s, $((GPU + i*TP)) $((GPU + i*TP + TP - 1))); p=$((PORT + i))
  echo "[14B] replica $i → GPU $g port $p"
  # Qwen3 thinking mode OFF for extraction — client sends chat_template_kwargs.enable_thinking=false (../NOTES.md).
  CUDA_VISIBLE_DEVICES=$g nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" --served-model-name qwen \
    --tensor-parallel-size "$TP" --port "$p" \
    --max-model-len "${MAXLEN:-32768}" --max-num-seqs 256 --gpu-memory-utilization 0.90 \
    --disable-log-requests --enable-prefix-caching \
    > "/mnt/data/qwen_logs/14b_${i}.log" 2>&1 &
  echo "  PID $! → /mnt/data/qwen_logs/14b_${i}.log"
done
