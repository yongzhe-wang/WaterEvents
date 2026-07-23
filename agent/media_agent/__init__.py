"""media_agent — event → complete media archive. Given event_agent's events, for EACH event run an internal url
close-loop that routes every url by type (html→VLM, pdf/pptx/docx→Docling, audio→WhisperX, video→yt-dlp→WhisperX)
and FILLS-AND-APPENDS each source's contribution into one 'chart' per event.

event_agent = 横向发现有哪些 event;media_agent = 纵向把每个 event 的所有素材聚合成完整可读档案。

Layout (bottom-up — built smallest-first):
  router.py    — classify(url) → html|pdf|pptx|docx|xlsx|audio|video|other  (pure)                ✅
  chart.py     — Chart accumulator: fill-and-append each source's contribution, content-hash dedup (pure) ✅
  prompts.py   — media-VLM contribution schema: basic_info blocks + inline transcript + new_urls + metadata ✅
  enrich.py    — THE ENRICHMENT ENDPOINT: (known event + detail page) → enriched record, parallel + twin-truncation gate ✅
  smoke.py     — run enrich over the real 10-event dataset ON RUNPOD, dump full prompt + trace txt          ✅
  handlers.py  — handle_html(VLM) / handle_office(officeall) / handle_audio(audio_extract) / handle_video → dispatch ✅
  gather.py    — one event's url close-loop (queue/dedup/cap) → a chart                                  (todo)
  run.py       — THE MAIN LOOP: all events in parallel → charts.json + trace                             (todo)
  trace.py     — per-event audit trail                                                                   (todo)

Providers/tools used: providers/watercrawl (html render) + providers/qwen_llm (VLM) [reused];
                tools/officeall (Docling: pdf/pptx/docx/xlsx) + tools/audio_extract (faster-whisper) [wired];
                youtube/yt-dlp platform adapter is a TODO inside tools/audio_extract. GPU tools run on RunPod.
"""
from . import chart, enrich, prompts, router
from .enrich import enrich_page, enrich_pages

__all__ = ["router", "chart", "prompts", "enrich", "enrich_page", "enrich_pages"]
