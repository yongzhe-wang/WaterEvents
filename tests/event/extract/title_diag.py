"""Diagnose WHY incremental events come back title-less: for the 10 hubs with the most empty-title events, pull the
EXACT content the VLM saw (pages.content = the inline text) + re-run extract → compare. Two possible root causes:
(a) content HAS the titles but the VLM returns title="" → VLM ability issue;  (b) content itself has only dates/links
(filings-list rows) → hub-selection issue (the page has no event NAMES to extract). {USER 2026-07-25 "understand why no
title, might be a vlm ability issue, collect a dataset of 10 pages and pass through vlm"}.
"""
from __future__ import annotations

import asyncio
import json

import asyncpg

from agent.event_agent.crawl.extract import extract_page
from providers.qwen_llm import QwenClient

DSN = "postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
OUT = "/workspace/WaterEvents/tests/title_diag.json"


async def go() -> None:
    c = await asyncpg.connect(DSN, statement_cache_size=0, server_settings={"search_path": "waterevents"})
    hubs = await c.fetch(
        "SELECT company_id, source_url, count(*) c FROM events "
        "WHERE run_id='eventinc' AND (title IS NULL OR title='') AND source_url<>'' "
        "GROUP BY 1,2 ORDER BY c DESC LIMIT 10")
    client = QwenClient()
    dataset = []
    for h in hubs:
        pg = await c.fetchrow("SELECT content FROM pages WHERE company_id=$1 AND url=$2 LIMIT 1",
                              h["company_id"], h["source_url"])
        content = pg["content"] if pg and pg["content"] else None
        rec = {"url": h["source_url"], "empty_title_events_stored": h["c"]}
        if not content:
            rec["note"] = "NO stored content in pages table"
            dataset.append(rec); continue
        # re-run the REAL extract (VLM, text-only) on the exact content the incremental fed it
        res = await extract_page({"page_url": h["source_url"], "page_text": content}, client, use_image=False)
        evs = res.get("events") or []
        rec["content_len"] = len(content)
        rec["content_head"] = content[:1800]                 # eyeball: does the page text actually CONTAIN event names?
        rec["n_events"] = len(evs)
        rec["empty_title_now"] = sum(1 for e in evs if not (e.get("title") or "").strip())
        rec["events"] = [{"title": e.get("title"), "date": e.get("date"), "url": (e.get("urls") or [None])[0]} for e in evs[:15]]
        dataset.append(rec)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    print("=== TITLE DIAGNOSTIC (10 empty-title hubs) ===")
    for r in dataset:
        if r.get("note"):
            print(f"  {r['url'][:60]} → {r['note']}"); continue
        et = r["empty_title_now"]; n = r["n_events"]
        # heuristic: does the CONTENT contain title-like words (letters, not just dates/urls)?
        head = r["content_head"]
        has_words = sum(1 for w in head.split() if w.isalpha() and len(w) > 3) > 20
        verdict = "CONTENT-has-text→VLM-dropped-title" if (has_words and et > 0) else ("CONTENT-thin(dates/links only)" if not has_words else "ok")
        print(f"  {r['url'][:55]:55} | {n} ev, {et} empty now | content {r['content_len']}c | {verdict}")
    print(f"\nfull dataset (content + VLM events) → {OUT}")
    await c.close()


if __name__ == "__main__":
    asyncio.run(go())
