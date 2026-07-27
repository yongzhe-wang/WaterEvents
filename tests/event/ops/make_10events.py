"""make_10events — pick 10 DIVERSE real events from the discovery DB → the ev*.json dataset media_run.py enriches.

用一句话讲完: 从 waterevents.events(run_id=killerdeal)挑 10 个不同公司、带 media_urls、标题非空的 event → 每个写一个
ev{NN}.json {id, event_url(=第一个 http media url,即 detail 页), known_event:{title,date,type,media_urls}} → 供
tests/media_run.py 逐个 render+enrich 出 per-event txt。{USER 2026-07-24 "create a 10 event list and run the media on those"}.

Run:  OUT=<dir> WATEREVENTS_DB_DSN=<dsn> python tests/make_10events.py
"""
from __future__ import annotations

import asyncio
import json
import os

import asyncpg

DSN = os.environ["WATEREVENTS_DB_DSN"]
OUT = os.environ.get("OUT", "/home/thebigsun/10events_dataset")
RUN_ID = os.environ.get("SRC_RUN_ID", "killerdeal")


async def main() -> None:
    c = await asyncpg.connect(DSN, statement_cache_size=0, server_settings={"search_path": "waterevents"})
    # DISTINCT ON company → 10 different companies; media-rich first (jsonb_array_length desc) so there's real material to
    # enrich; title non-empty + ≥1 media url so the detail page actually renders to something.
    # MEDIA-RICH, RENDERABLE types only — earnings/presentation/webcast/conference pages carry transcripts + slides + audio
    # (the real enrichment material) and live on NORMAL IR detail pages that render fine. EXCLUDE 'filing'/'press_release':
    # SEC-filing pages are on Q4/edgar platforms that ERR_HTTP2 / Akamai-wall and are documents, not media-rich events.
    rows = await c.fetch(
        """
        SELECT DISTINCT ON (company_id) title, event_date, event_type, media_urls, source_url, company_id
        FROM events
        WHERE run_id = $1 AND title <> '' AND jsonb_array_length(media_urls) >= 1
          AND (event_type ILIKE '%earning%' OR event_type ILIKE '%webcast%' OR event_type ILIKE '%presentation%'
               OR event_type ILIKE '%conference%' OR event_type ILIKE '%investor%day%')
        ORDER BY company_id, jsonb_array_length(media_urls) DESC, event_date DESC
        LIMIT 60
        """, RUN_ID)
    # sort the 60 distinct-company candidates by media richness and take the top 10
    picked = sorted(rows, key=lambda r: -len(json.loads(r["media_urls"]) if isinstance(r["media_urls"], str) else r["media_urls"]))[:10]

    os.makedirs(OUT, exist_ok=True)
    for i, r in enumerate(picked, 1):
        media = json.loads(r["media_urls"]) if isinstance(r["media_urls"], str) else (r["media_urls"] or [])
        event_url = next((u for u in media if isinstance(u, str) and u.startswith("http")), r["source_url"] or "")
        entry = {
            "id": f"ev{i:02d}",
            "event_url": event_url,                                   # the detail page media_run renders + enriches
            "known_event": {"title": r["title"], "date": r["event_date"], "type": r["event_type"], "media_urls": media},
        }
        with open(os.path.join(OUT, f"ev{i:02d}.json"), "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
        print(f"  ev{i:02d}: [{r['event_type']}] {r['title'][:45]} → {event_url[:70]} ({len(media)} media)")
    print(f"[make_10events] wrote {len(picked)} events to {OUT}")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
