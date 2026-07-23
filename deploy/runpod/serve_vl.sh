#!/bin/bash
# Qwen2.5-VL-7B production serve on A5000 24GB. Auth via --api-key (public RunPod proxy = must NOT be open).
# Fixes baked: HF_HOME on persistent vol, xet disabled (reconstruction hit quota), PATH exposes venv ninja + cuda
# nvcc (flashinfer JIT), host 0.0.0.0 (RunPod proxy needs external bind), max-model-len 16384 (A5000 KV ceiling).
set -euo pipefail

# Load the API key. `if` form (NOT `[ -f ] && source`) so a missing file can't trip `set -e` on the && chain.
if [ -f /workspace/vllm.env ]; then
  # shellcheck disable=SC1091
  source /workspace/vllm.env
fi

# AUDIT FIX #2 (fail-loud + security): NEVER serve unprotected on the public proxy. An empty/unset key would make
# vLLM launch with `--api-key ""` = no auth = anyone with the URL burns the GPU. Refuse to start instead.
# {AUDIT 2026-07-23 "serve_vl.sh: source fails silently → --api-key '' → unprotected server on public proxy"}
if [ -z "${QWEN_API_KEY:-}" ]; then
  echo "FATAL: QWEN_API_KEY empty/unset — refusing to serve UNPROTECTED. Check /workspace/vllm.env" >&2
  exit 1
fi

export HF_HOME=/workspace/hf
export HF_HUB_DISABLE_XET=1
export PATH=/root/venv/bin:/usr/local/cuda/bin:$PATH

exec /root/venv/bin/python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-VL-7B-Instruct \
  --served-model-name qwen-vl \
  --host 0.0.0.0 --port 8000 \
  --api-key "$QWEN_API_KEY" \
  --max-model-len 32768 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.92 \
  --mm-processor-kwargs '{"max_pixels": 1003520, "min_pixels": 200704}'
# NOTE max-model-len 32768 (NOT 16384): the event_agent extract.py REMOVED its chunking fallback and now sends the
# FULL inline page text (_VISION_TEXT_CHARS=24000 chars ~7k tok) + screenshot (~1.3k tok) + system (~0.9k) + output
# (up to ~8k) in ONE call — ~17.5k tokens, which OVERFLOWS a 16384 ctx. 32768 is FEASIBLE on the A5000 because
# max-model-len only caps a SINGLE request's length; the KV cache is a SHARED POOL sized from free VRAM (~7GB after
# 7B weights → ~250k-token pool), so raising the per-request cap does NOT need per-seq*max-len VRAM — it just means
# fewer *simultaneous* full-length sequences. {DIVERGENCE 2026-07-23: extract.py "Requires the server at
# --max-model-len 32768"} [CONFIDENCE: HIGH — KV is pool-allocated in vLLM (PagedAttention), verified by boot test].
# OPT NOTES (research-verified, 2026-07-23):
#  - enable_prefix_caching + enable_chunked_prefill are ON BY DEFAULT in vLLM 0.25.1 (confirmed in engine config log)
#    — the ~866-token static SYSTEM prompt (prompts.SYSTEM, byte-identical every request; per-page vars live in the
#    USER turn) is the reused prefix. Highest-leverage lever. {SqueezeBits benchmark: shared-prefix 0.1->0.9 = +32%
#    throughput; the -36.7% regression only hits at ~0 hit-rate (random no-shared-prefix), which WaterEvents is NOT.}
#  - --max-num-seqs 32: research says sweep toward 40 (A5000 goes COMPUTE-bound ~40-48, not KV-bound — KV pool ~105k
#    tokens >> what 24-32 seqs use). 24-way under-fed the GPU (72% util). {A100 saturates ~conc 50; A5000 ~40% compute}
#  - --mm-processor-kwargs max_pixels=1003520 (~1280 img tokens) caps a tall full-page shot's vision tokens SERVER-side
#    (belt+suspenders with the client-side 1.2M-px bound in render.py). {task research: -16% image tokens, +35% quality}
#  - [CONFIDENCE: HIGH — prefix/chunked defaults confirmed in log; max-num-seqs + mm-kwargs from adversarially-verified research]
