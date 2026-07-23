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
# 16384 output cap. Truncated JSON = invalid = _parse_json→{}→0 events (silent data loss), so this MUST exceed the
# largest real page's output. History: 4096 truncated on any normal IR page; 8192 still truncated on ~10% of pages —
# the SEC-filings-archive / press-release mega-lists that emit hundreds of events (a full 128-page Coca-Cola crawl had
# 13/128 pages come back {} with 18k-28k chars of *truncated* raw output, all at exactly the 8192-token wall). Vision
# input is now bounded (image ~7-9k tok + 4000-char text + prompt ≈ 11k), and --max-model-len is 32768, so 16384 output
# leaves ~5k headroom while covering those mega-list pages. {DUMPS req_0019/0020/0051/... 2026-07-23: raw_chars 18k-28k,
# has_error=0 — pure length truncation, not a request error}. [CONFIDENCE: CONFIRMED 100% — 13 truncated dumps measured].
MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "16384"))
MAX_INPUT_CHARS = int(os.environ.get("QWEN_MAX_INPUT_CHARS", "48000"))   # ~12-16k tokens; truncate huge pages

# DEBUG: when set, the client dumps EVERY request's full prompt (system + user + image info) and the model's RAW
# output + parsed result + any error to one txt per request under this dir. For eyeballing exactly what the model
# saw and returned. Off by default (empty). Set QWEN_DEBUG_DIR=tests/output to capture a run.
DEBUG_DIR = os.environ.get("QWEN_DEBUG_DIR", "")
