#!/usr/bin/env bash
# Qwen2.5-VL-32B — the STRONG vision model. Best layout reasoning of the two VLMs: reads a full-page screenshot and
# reliably separates event tables/cards from nav/footer/promo blocks. ~64GB bf16 fits ONE H20 (96GB); set an AWQ
# repo + QUANT=awq_marlin to run it in ~18GB instead. Slower/pricier than 7B — the accuracy ceiling for the vision
# route, use on the visually-hardest pages.
#
#   ./serve.sh                                  # GPU 4, port 8004, bf16
#   QWEN_MODEL=<awq-repo> QUANT=awq_marlin ./serve.sh   # ~18GB
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen2.5-VL-32B-Instruct}"
QUANT="${QUANT:-}"                                # set awq_marlin + an AWQ repo to shrink 64GB→~18GB
GPU="${GPU:-1}"; PORT="${PORT:-8001}"; TP="${TP:-1}"
export HF_HOME="${HF_HOME:-/mnt/data/hf_cache}"; export VLLM_LOGGING_LEVEL=WARNING
mkdir -p /mnt/data/qwen_logs
QFLAG=""; [ -n "$QUANT" ] && QFLAG="--quantization $QUANT"
echo "[VL-32B] GPU $GPU port $PORT model $MODEL quant=${QUANT:-none}"
CUDA_VISIBLE_DEVICES=$GPU nohup python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" --served-model-name qwen-vl $QFLAG \
  --tensor-parallel-size "$TP" --port "$PORT" \
  --max-model-len "${MAXLEN:-32768}" --max-num-seqs 32 --gpu-memory-utilization 0.92 \
  --limit-mm-per-prompt image=1 \
  --mm-processor-kwargs '{"max_pixels": 1003520}' \
  --disable-log-requests \
  > "/mnt/data/qwen_logs/vl_32b.log" 2>&1 &
echo "  PID $! → /mnt/data/qwen_logs/vl_32b.log"
