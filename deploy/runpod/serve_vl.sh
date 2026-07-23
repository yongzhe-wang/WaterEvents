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

# OOM GATE (2026-07-23): a crashed/killed prior server can orphan its `VLLM::EngineCore` multiprocessing child, which
# keeps ALL ~22.6GB VRAM → the new engine dies "Free memory 0.96 GiB < utilization 0.92" and the supervisor crash-loops.
# supervise_vl.sh only calls serve_vl.sh AFTER the prior server exited, so ANY GPU compute process here is an orphan →
# kill it (killing the PID fully frees its VRAM — no torch cache-clear needed), then WAIT until VRAM is actually free
# before launching, so we never start into a starved GPU. {AUDIT 2026-07-23: orphan EngineCore held 22914 MiB → startup
# OOM loop}. [CONFIDENCE: CONFIRMED — nvidia-smi showed pid 10094 VLLM::EngineCore 22914 MiB blocking every retry].
for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
  kill -9 "$p" 2>/dev/null                                   # anything on the GPU now is a leftover — reclaim it
done
for i in $(seq 1 15); do                                     # wait up to 30s for the VRAM to actually free
  free_mib=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)
  [ "${free_mib:-0}" -gt 20000 ] && break                    # >20GB of 24 free → safe to launch at 0.92 util
  echo "[serve] GPU only ${free_mib:-?} MiB free — waiting for orphan VRAM to release ($i/15)" >&2
  sleep 2
done

exec /root/venv/bin/python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-VL-7B-Instruct-AWQ \
  --served-model-name qwen-vl \
  --host 0.0.0.0 --port 8000 \
  --api-key "$QWEN_API_KEY" \
  --quantization awq_marlin \
  --max-model-len 32768 \
  --max-num-seqs 48 \
  --gpu-memory-utilization 0.92 \
  --scheduling-policy priority
# MULTI-SOURCE POOL (2026-07-23): --scheduling-policy priority + --max-num-seqs 48. vLLM is ONE shared engine/queue/
# KV-pool, so every service hitting :8000 auto-pools + continuous-batches together. `priority` scheduling lets a
# latency-sensitive source (e.g. the always-ready incremental monitor) preempt a bulk discovery crawl — a client sets
# a LOWER `priority` value per request to jump ahead (default 0 = bulk). max-num-seqs 32→48: AWQ freed ~10GB (weights
# 16→6.5GB) → the KV pool is far larger, so 48 concurrent sequences fit and one source is less likely to starve
# another. {USER 2026-07-23 "优先级最该加 ... max-num-seqs 32→48"} [CONFIDENCE: CONFIRMED — direct instruction; AWQ KV
# headroom verified (pool held 105k tokens even at FP16 16GB weights, AWQ 6.5GB weights gives much more)].
#
# NO server-side --mm-processor-kwargs max_pixels downscale (removed 2026-07-23 per USER "dont do this"): shrinking
# the screenshot resolution trades READING ACCURACY for speed, and AWQ already gave the 3x speed — so we keep the
# image at full resolution for accurate layout/text reading (the whole reason we use a VL model). The image still
# passes the client-side overflow guard in client.py (_MAX_SHOT_PIXELS, ~1.2M px) which exists ONLY to stop a
# pathological tall full-page shot from overflowing the 32768 ctx — that's a correctness guard, not a quality/speed
# downscale, and at 32768 ctx + AWQ's freed KV it's generous. {USER 2026-07-23 "dont do this" re: mm-processor-kwargs
# max_pixels} [CONFIDENCE: CONFIRMED — direct user instruction; quality (reading accuracy) > marginal prefill speed].
# AWQ 4-bit (2026-07-23): measured 3.07x faster DECODE than FP16 on this A5000 — 512 tokens 12.87s(FP16,40 tok/s)
# → 4.19s(AWQ,122 tok/s). decode is memory-bandwidth-bound on Ampere (768 GB/s); AWQ reads ~1/4 the weight bytes/token
# so it's ~3x. A rich event page (output-dominated ~21s FP16) drops to ~7s. Quality spot-check held: synthetic 4-event
# IR page extracted all 4 correctly + zero nav/footer/RSS leak. Model 16GB→6.5GB also frees ~10GB for a bigger KV pool.
# {BENCH 2026-07-23 pod A5000: FP16 512tok=12.87s vs AWQ 512tok=4.19s; AWQ vision LEAK_chrome=False events=4/4}
# [CONFIDENCE: CONFIRMED 95% — decode speed measured directly; quality is a single spot-check, run the 10-event dataset
#  AWQ-vs-FP16 for a rigorous quality A/B before fully trusting on rare/hard pages]. Model served-name stays qwen-vl
# so the GCP client is UNCHANGED. --quantization awq_marlin = the Ampere-optimized AWQ kernel.
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
