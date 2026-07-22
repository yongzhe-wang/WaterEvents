"""qwen_llm config — model + endpoint + parallelism knobs. All env-overridable so the same code runs on 1 GPU
locally or 8×H20 in prod without edits."""
from __future__ import annotations

import os

# Model — Qwen2.5-Instruct. 7B is the throughput sweet spot for IR-page event extraction; 14B/32B for harder
# reasoning (set QWEN_MODEL). vLLM downloads to HF_HOME (/mnt/data/hf_cache on the H20 box, 3.1T free).
MODEL = os.environ.get("QWEN_MODEL", "Qwen/Qwen2.5-7B-Instruct")
SERVED_NAME = "qwen"                                          # the --served-model-name the client asks for

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
# 16384 not 4096: the output is {events + routes}, and a link-heavy hub page can have 400 links → 400 route entries
# ≈ 8k+ output tokens. At 4096 the JSON got TRUNCATED → parse fail → {} → the most event-rich hubs yielded nothing.
# {AUDIT 2026-07-22 provider bug: MAX_TOKENS too low for the routes list}.
MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "16384"))
MAX_INPUT_CHARS = int(os.environ.get("QWEN_MAX_INPUT_CHARS", "48000"))   # ~12-16k tokens; truncate huge pages
