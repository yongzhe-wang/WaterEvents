#!/bin/bash
# Rebuild /root/venv after a pod restart wiped it (/root is ephemeral; only /workspace persists). pip cache on /root
# (/workspace's ~20G quota is full with the 16G model). Model stays cached on /workspace/hf.
# AUDIT FIX #1 (fail-loud): the old version ended with `... | tail -3` then `echo RECOVER_EXIT=$?` — $? was the
# TAIL's exit (always 0), so a failed pip/playwright install reported SUCCESS. Now `set -euo pipefail` makes ANY
# step's failure abort loudly, and an explicit import check is the final proof before declaring success.
# {AUDIT 2026-07-23 "recover.sh RECOVER_EXIT=$? captured tail, not the install → masked failures"}
set -euo pipefail

rm -rf /root/venv
python3 -m venv /root/venv
/root/venv/bin/pip install -q --upgrade pip
# PIP_CACHE_DIR on /workspace (PERSISTENT) now that the volume is resized to 50G (16G model + 9.5G venv-build + ~6G
# wheel cache fits). WHY here not /root: /root is wiped on every restart, so caching wheels there is useless; caching
# on /workspace means the ~6G of vllm+CUDA wheels DOWNLOAD ONCE and every future restart's reinstall is INSTALL-ONLY
# (no re-download) → cold restart drops from ~15min to ~3-4min. venv itself stays on /root (fast local import; a venv
# on MooseFS network FS has slow small-file import). {2026-07-23: volume resized 20->50G enables the persistent cache}
mkdir -p /workspace/.pipcache
PIP_CACHE_DIR=/workspace/.pipcache /root/venv/bin/pip install vllm playwright
PATH=/root/venv/bin:/usr/local/cuda/bin:$PATH /root/venv/bin/playwright install chromium
# WaterEvents tools deps (curl_cffi/patchright/openai/asyncpg/pdf/…). WHY here: recover ONLY installed vllm+playwright, so
# curl_cffi — the impersonate lane that cracks Akamai's ERR_HTTP2 fingerprint wall — was NEVER in the venv, silently
# 0-linking every gcs-web/Q4 IR page (headless got net::ERR_HTTP2_PROTOCOL_ERROR, impersonate.available()=False so the
# fingerprint fallback never fired). Installing requirements.txt recovered 27/28 previously-failing URLs and dropped a
# 28-URL render from 187s→15s. {DEBUG 2026-07-24: `pip install curl_cffi` → impersonate.fetch(copa.gcs-web)=text 1763,
# links 40, walled=False} [CONFIDENCE: CONFIRMED 100% — live re-test 13→27 ok once curl_cffi present]. Idempotent: cached
# wheels reinstall in seconds from the persistent /workspace/.pipcache.
# Install curl_cffi EXPLICITLY (not `-r requirements.txt`): requirements has heavy optional tools (faster-whisper/pymupdf)
# that the crawl render path doesn't need, and under `set -e` a heavy dep's install failure would abort the whole venv
# rebuild → no server. curl_cffi is the one render-critical tools dep; installing just it is fail-loud-safe.
PIP_CACHE_DIR=/workspace/.pipcache /root/venv/bin/pip install curl_cffi

# Never declare success on a half-built venv — the install must actually import (curl_cffi is the render-critical one).
if ! /root/venv/bin/python -c "import vllm, playwright, curl_cffi" 2>&1; then
  echo "RECOVER FAILED: import check after install — venv is broken" >&2
  exit 1
fi
echo "RECOVER_EXIT=0"
