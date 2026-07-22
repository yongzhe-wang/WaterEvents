"""qwen_llm config — model + endpoint + parallelism knobs. All env-overridable so the same code runs on 1 GPU
locally or 8×H20 in prod without edits."""
from __future__ import annotations

import os

# Model — Qwen2.5-VL (VISION). The whole point of this project is layout-aware extraction: the model reads a
# rendered-page SCREENSHOT so it can tell an events table from nav/footer/feed chrome — the signal text-only BERT
# over-classifies on. 7B = throughput sweet spot; set QWEN_MODEL=Qwen/Qwen2.5-VL-32B-Instruct for the ceiling.
# {SERVE.SH:27 "--served-model-name qwen-vl"} [CONFIDENCE: CONFIRMED 100% — serve.sh launches vLLM with this name;
# the client asks for SERVED_NAME so it MUST equal the server's --served-model-name or every request 404s].
MODEL = os.environ.get("QWEN_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct")
SERVED_NAME = os.environ.get("QWEN_SERVED_NAME", "qwen-vl")   # MUST match serve.sh's --served-model-name (else 404)

# Endpoint — the vLLM OpenAI-compatible server. One base URL; if you run N replicas, put a router in front and
# point this at it (or pass a comma-list to the client for round-robin).
BASE_URLS = [u.strip() for u in os.environ.get("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1").split(",") if u.strip()]
API_KEY = os.environ.get("QWEN_API_KEY", "EMPTY")            # vLLM ignores the key but the OpenAI client requires one

# Parallelism — how many requests are in flight at once ACROSS the whole client. vLLM continuous-batches these on
# the GPU, so this is the real throughput dial. 256 saturates a single 7B replica on an H20; raise with more replicas.
MAX_CONCURRENCY = int(os.environ.get("QWEN_CONCURRENCY", "256"))
REQUEST_TIMEOUT_S = int(os.environ.get("QWEN_TIMEOUT_S", "120"))
MAX_RETRIES = int(os.environ.get("QWEN_RETRIES", "2"))

# Generation — deterministic extraction (temp 0), bounded output. IR pages need a JSON event list, not prose.
TEMPERATURE = float(os.environ.get("QWEN_TEMPERATURE", "0.0"))
# 4096 output cap. VISION extraction emits ONLY an events array (no route list — the crawl's BFS handles link
# discovery, not the model), so a few dozen events ≈ well under 4k tokens. Critically, max_tokens + input MUST fit
# in the server's --max-model-len (16384): a full-page screenshot is ~1-1.5k image tokens + prompt, so 4096 output
# leaves ample room. {SERVER 2026-07-23 400: "'max_tokens' ... too large: 16384 ... maximum context length is 16384"}
# [CONFIDENCE: CONFIRMED 100% — vLLM rejects max_tokens>=max_model_len pre-flight; observed on the VL-7B smoke run].
MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "4096"))
MAX_INPUT_CHARS = int(os.environ.get("QWEN_MAX_INPUT_CHARS", "48000"))   # ~12-16k tokens; truncate huge pages
