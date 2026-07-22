#!/usr/bin/env bash
# Qwen2.5-VL-32B — the STRONG vision model (best layout reasoning). ~64GB bf16 does NOT fit one H20's ~70GB free
# (other jobs hold ~25GB/GPU), so default TP=2 across GPU 1+2 (~32GB weights/GPU + KV). Or set an AWQ repo
# (QWEN_MODEL=<awq> QUANT=awq_marlin GPU=1 TP=1) to run in ~18GB on a single GPU.
#
#   ./serve.sh                 # TP=2, GPU 1+2, port 8001
#
# Same hard-won config as qwen_vl_7b: pre-download via hf-mirror → serve HF_HUB_OFFLINE=1, gpu-util 0.70,
# no --limit-mm-per-prompt (0.10.1.1 wants JSON), PYTHONUNBUFFERED=1. See qwen_vl_7b/serve.sh for the why.
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen2.5-VL-32B-Instruct}"
QUANT="${QUANT:-}"
GPU="${GPU:-1}"; PORT="${PORT:-8001}"; TP="${TP:-2}"; GPU_UTIL="${GPU_UTIL:-0.70}"; MAXLEN="${MAXLEN:-16384}"
export HF_HOME="${HF_HOME:-/mnt/data/hf_cache}"; export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
mkdir -p /mnt/data/qwen_logs
QFLAG=""; [ -n "$QUANT" ] && QFLAG="--quantization $QUANT"
gpus=$(seq -s, "$GPU" $((GPU + TP - 1)))          # TP consecutive GPUs starting at $GPU

echo "[VL-32B] ensuring weights cached ($MODEL) via $HF_ENDPOINT ..."
huggingface-cli download "$MODEL" >/dev/null 2>&1 || { echo "[VL-32B] weight download failed"; exit 1; }

echo "[VL-32B] launching on GPU $gpus port $PORT (TP=$TP util $GPU_UTIL quant=${QUANT:-none})"
CUDA_VISIBLE_DEVICES="$gpus" PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 \
  nohup python3 -u -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" --served-model-name qwen-vl $QFLAG \
  --tensor-parallel-size "$TP" --port "$PORT" \
  --max-model-len "$MAXLEN" --max-num-seqs 16 --gpu-memory-utilization "$GPU_UTIL" \
  > "/mnt/data/qwen_logs/vl_32b.log" 2>&1 < /dev/null &
echo "  PID $! → /mnt/data/qwen_logs/vl_32b.log (wait ~3-5min for 'Application startup complete')"
