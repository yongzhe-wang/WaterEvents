#!/usr/bin/env bash
# Qwen3-30B-A3B — the ⭐ pick: a Mixture-of-Experts (30B total / ~3B ACTIVE per token) so it reasons at 32B-class
# quality but generates at ~8B speed (only 3B params fire per token). AWQ-quantized → ~18GB, fits ONE H20 with room
# for a big KV cache. Best quality/throughput trade of the three.
#
#   ./serve.sh                 # GPU 2, port 8002
#   QWEN_MODEL=<exact-awq-repo> ./serve.sh
#
# NOTE the exact HF repo for the AWQ build must exist — verify on HF (e.g. Qwen/Qwen3-30B-A3B-Instruct-2507-AWQ or a
# community AWQ). If AWQ isn't available, drop --quantization and use the fp8/bf16 repo (needs ~60GB, still 1 H20).
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen3-30B-A3B-Instruct-2507-AWQ}"
QUANT="${QUANT:-awq_marlin}"                     # AWQ Marlin kernel = fast int4 on Hopper; set QUANT="" for a bf16 repo
GPU="${GPU:-2}"; PORT="${PORT:-8002}"; TP="${TP:-1}"; REPLICAS="${REPLICAS:-1}"
export HF_HOME="${HF_HOME:-/mnt/data/hf_cache}"; export VLLM_LOGGING_LEVEL=WARNING
mkdir -p /mnt/data/qwen_logs
QFLAG=""; [ -n "$QUANT" ] && QFLAG="--quantization $QUANT"
for ((i=0;i<REPLICAS;i++)); do
  g=$(seq -s, $((GPU + i*TP)) $((GPU + i*TP + TP - 1))); p=$((PORT + i))
  echo "[30B-A3B] replica $i → GPU $g port $p quant=${QUANT:-none}"
  # Qwen3 thinking mode OFF for extraction — client sends chat_template_kwargs.enable_thinking=false (../NOTES.md).
  CUDA_VISIBLE_DEVICES=$g nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" --served-model-name qwen $QFLAG \
    --tensor-parallel-size "$TP" --port "$p" \
    --max-model-len "${MAXLEN:-32768}" --max-num-seqs 256 --gpu-memory-utilization 0.90 \
    --disable-log-requests --enable-prefix-caching \
    > "/mnt/data/qwen_logs/30b_a3b_${i}.log" 2>&1 &
  echo "  PID $! → /mnt/data/qwen_logs/30b_a3b_${i}.log"
done
