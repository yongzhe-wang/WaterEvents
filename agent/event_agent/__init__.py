"""event_agent — company URL → list of events. The AGENT logic (event definition, prompt, extraction, routing).
Uses providers/watercrawl to open pages and providers/qwen_llm as the parallel Qwen transport.

Layout (bottom-up):
  prompts.py  — SYSTEM instruction + SCHEMA: {events:[{title,date,type,urls[]}], routes:[{url,go_deeper}]}
  extract.py  — THE ENDPOINT: extract_page / extract_pages — (page text + optional screenshot) → {events, routes}, parallel
  smoke.py    — run after qwen_llm serve.sh to verify parallelism + precision/recall + exclusivity
"""
from .extract import extract_page, extract_pages

__all__ = ["extract_page", "extract_pages"]
