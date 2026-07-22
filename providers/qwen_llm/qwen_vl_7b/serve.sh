#!/usr/bin/env bash
# Qwen2.5-VL-7B — VISION baseline. Takes a rendered-page screenshot (+ optional text) and judges structure by
# LAYOUT (nav/footer/cookie-banner vs event list) — the signal the text-only router can't see. ~16GB, 1 GPU.
#
#   ./serve.sh                 # GPU 0, port 8000
#
# HARD-WON CONFIG (this box: 8×H20 96GB, ~25GB/GPU already used by other jobs → only ~70GB free per GPU):
#   1) PRE-DOWNLOAD weights via hf-mirror (huggingface.co hangs from CN; vllm's IN-PROCESS download also hung —
#      pre-fetching to HF_HOME then serving with HF_HUB_OFFLINE=1 is the reliable path).
#   2) --gpu-memory-utilization 0.70 — 0.9×95=86GB > 70GB free → "Free memory < desired util" ValueError. 0.70 fits.
#   3) NO --limit-mm-per-prompt image=1 — vllm 0.10.1.1 wants JSON ('{"image":1}'); the key=value form is 0.11+.
#      Omitted (VL defaults to 1 image/prompt). PYTHONUNBUFFERED=1 so engine logs actually flush to the logfile.
set -euo pipefail
MODEL="${QWEN_MODEL:-Qwen/Qwen2.5-VL-7B-Instruct}"
GPU="${GPU:-0}"; PORT="${PORT:-8000}"; TP="${TP:-1}"; GPU_UTIL="${GPU_UTIL:-0.70}"; MAXLEN="${MAXLEN:-16384}"
export HF_HOME="${HF_HOME:-/mnt/data/yongzhe/hf_cache}"; export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
mkdir -p /mnt/data/yongzhe/qwen_logs

# 1) pre-download weights (idempotent — skips if already cached) via the CN mirror
echo "[VL-7B] ensuring weights cached ($MODEL) via $HF_ENDPOINT ..."
huggingface-cli download "$MODEL" >/dev/null 2>&1 || { echo "[VL-7B] weight download failed — check hf-mirror reachability"; exit 1; }

# 2) serve offline (weights are local now → no network hang), unbuffered logs
echo "[VL-7B] launching on GPU $GPU port $PORT (util $GPU_UTIL)"
CUDA_VISIBLE_DEVICES="$GPU" PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 \
  nohup python3 -u -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" --served-model-name qwen-vl \
  --tensor-parallel-size "$TP" --port "$PORT" \
  --max-model-len "$MAXLEN" --max-num-seqs 32 --gpu-memory-utilization "$GPU_UTIL" \
  > "/mnt/data/yongzhe/qwen_logs/vl_7b.log" 2>&1 < /dev/null &
echo "  PID $! → /mnt/data/yongzhe/qwen_logs/vl_7b.log (wait ~100s for 'Application startup complete')"
