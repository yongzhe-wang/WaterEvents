#!/usr/bin/env bash
# Qwen3-8B — the fast baseline (~16GB fp16, 1 GPU). Highest throughput of the three; good enough for most IR pages.
# Launches ONE replica on GPU $GPU / port $PORT so it can run side-by-side with 14B and 30B-A3B for A/B compare.
#
#   ./serve.sh                 # GPU 0, port 8000
#   GPU=3 PORT=8010 REPLICAS=4 ./serve.sh   # 4 replicas GPU 3-6, ports 8010-8013 (max throughput)
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen3-8B}"
GPU="${GPU:-0}"; PORT="${PORT:-8000}"; TP="${TP:-1}"; REPLICAS="${REPLICAS:-1}"
export HF_HOME="${HF_HOME:-/mnt/data/hf_cache}"; export VLLM_LOGGING_LEVEL=WARNING
mkdir -p /mnt/data/qwen_logs
for ((i=0;i<REPLICAS;i++)); do
  g=$(seq -s, $((GPU + i*TP)) $((GPU + i*TP + TP - 1))); p=$((PORT + i))
  echo "[8B] replica $i → GPU $g port $p"
  # NOTE Qwen3 defaults to THINKING mode — for JSON extraction we want it OFF (direct output, no chain-of-thought).
  # The client must send extra_body={"chat_template_kwargs":{"enable_thinking":false}} (see ../NOTES.md).
  CUDA_VISIBLE_DEVICES=$g nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" --served-model-name qwen \
    --tensor-parallel-size "$TP" --port "$p" \
    --max-model-len "${MAXLEN:-32768}" --max-num-seqs 256 --gpu-memory-utilization 0.90 \
    --disable-log-requests --enable-prefix-caching \
    > "/mnt/data/qwen_logs/8b_${i}.log" 2>&1 &
  echo "  PID $! → /mnt/data/qwen_logs/8b_${i}.log"
done
