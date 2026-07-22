#!/usr/bin/env bash
# Launch vLLM OpenAI-compatible server(s) for Qwen event extraction on the H20 box.
#
# Two modes:
#   ./serve.sh                 → ONE server, TP=1, GPU 0, port 8000 (vLLM continuous-batches ~256 concurrent reqs).
#   REPLICAS=8 ./serve.sh      → 8 independent replicas (1 GPU each) on ports 8000..8007 for max aggregate
#                                throughput; point the client at all 8 URLs (QWEN_BASE_URLS csv) for round-robin.
#   TP=2 ./serve.sh            → ONE server tensor-parallel across 2 GPUs (for a 32B/72B model).
set -euo pipefail
MODEL=${QWEN_MODEL:-Qwen/Qwen2.5-7B-Instruct}
TP=${TP:-1}
REPLICAS=${REPLICAS:-1}
BASE_PORT=${BASE_PORT:-8000}
export HF_HOME=${HF_HOME:-/mnt/data/hf_cache}
export VLLM_LOGGING_LEVEL=WARNING
mkdir -p /mnt/data/qwen_logs

launch() {                                                   # $1=replica index → GPUs, port, logfile
  local i=$1
  local gpus port
  gpus=$(seq -s, $((i * TP)) $((i * TP + TP - 1)))           # replica i owns TP consecutive GPUs
  port=$((BASE_PORT + i))
  echo "[serve] replica $i → GPU $gpus port $port model $MODEL (TP=$TP)"
  CUDA_VISIBLE_DEVICES=$gpus nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" --served-model-name qwen \
    --tensor-parallel-size "$TP" --port "$port" \
    --max-num-seqs 256 --gpu-memory-utilization 0.90 \
    --disable-log-requests --enable-prefix-caching \
    > "/mnt/data/qwen_logs/replica_${i}.log" 2>&1 &
  echo "  PID $! → /mnt/data/qwen_logs/replica_${i}.log"
}

for ((i = 0; i < REPLICAS; i++)); do launch "$i"; done
echo "[serve] launched $REPLICAS replica(s). Client QWEN_BASE_URLS:"
urls=""; for ((i = 0; i < REPLICAS; i++)); do urls+="http://127.0.0.1:$((BASE_PORT + i))/v1,"; done
echo "  ${urls%,}"
echo "[serve] tail a log:  tail -f /mnt/data/qwen_logs/replica_0.log"
