"""media_agent.db_media — persist ONE enriched event into the NORMALIZED media schema (event_content_blocks /
event_transcript_segments / event_media_urls), replacing the old "one JSON blob in events.basic_info" write.

用一句话讲完: worker 每 enrich 完一个 event → 调 mark_enriched_media,它在**一个事务**里先 fence+flip events.status→
enriched(claim_token 对得上才行,防 stale worker 覆盖)→ 再把 basic_info blocks / transcript segments / url ledger 分别
INSERT 进各自规范化表(content_hash / seg_hash 去重, canon_key 去重)。**dependency 方向: media_agent 依赖 event_agent.db
的 pool + 自己的 chart/router helpers,event_agent 永不反依赖 media_agent。** {USER 2026-07-23 "建独立规范化 media schema"}
[CONFIDENCE: CONFIRMED 100% — direct user directive; the 5 tables were applied via 20260723145355_waterevents_media_enrichment.sql].

WHY one transaction + fence-first: if the lease expired and another worker re-claimed the row (new claim_token), the
UPDATE ... WHERE claim_token=$tok matches NOTHING → we abort BEFORE inserting any child rows, so a stale result never
leaves orphan blocks/segments behind a row another worker owns. All child INSERTs ride the same txn as the flip →
all-or-nothing. {MIRROR db.mark_enriched fencing} [CONFIDENCE: CONFIRMED 100%].
"""
from __future__ import annotations

import json

from ..extract.chart import _canon, _hash          # canonical url dedup key + stable content hash — the SAME helpers Chart uses
from ..extract.router import classify              # url → KIND_* (html/pdf/pptx/docx/xlsx/audio/video/other)


async def mark_enriched_media(pool, event_id, claim_token, basic_info: list[dict],
                              transcript_segments: list[dict], urls: list[str], source_url: str = "") -> bool:
    """Flip event→enriched AND write its normalized media rows, fenced on claim_token, in ONE transaction. Returns True
    if we still owned the row (the flip landed); False if a re-claim stole it (nothing written). `basic_info` = the
    enrich.py ordered blocks; `transcript_segments` = speaker/start/end/text; `urls` = merged media urls; `source_url` =
    the detail page these came from (stamped on each block/segment for provenance)."""
    async with pool.acquire() as conn:
        async with conn.transaction():                    # all-or-nothing: the flip + every child INSERT commit together
            # FENCE + FLIP first — if the lease was lost (claim_token no longer matches), this matches 0 rows → abort
            # before writing any child rows. media_urls is MERGED (union, dedup) not replaced (a re-crawl may have added
            # urls after we read the event). {MIRROR db.mark_enriched}.
            row = await conn.fetchrow(
                """
                UPDATE events SET status='enriched', enriched_at=now(), claim_token=NULL,
                    media_urls = (
                        SELECT coalesce(jsonb_agg(DISTINCT u), '[]'::jsonb)
                        FROM jsonb_array_elements(events.media_urls || $3::jsonb) AS u
                    )
                WHERE id=$1 AND claim_token=$2 RETURNING id;
                """,
                event_id, claim_token, json.dumps(urls or []),
            )
            if row is None:                               # re-claimed by another worker → do NOT write orphan child rows
                return False

            # content blocks — ord preserves reading order; ON CONFLICT (event_id, content_hash) DO NOTHING dedups a
            # block already stored (idempotent re-enrich). headers/rows are jsonb (table blocks); NULL for md/list.
            for i, b in enumerate(basic_info or []):
                if not isinstance(b, dict) or not b.get("type"):
                    continue
                await conn.execute(
                    """INSERT INTO event_content_blocks
                         (event_id, ord, block_type, md, caption, headers, rows, content_hash, source_url)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                       ON CONFLICT (event_id, content_hash) DO NOTHING;""",
                    event_id, i, b.get("type"), b.get("md"), b.get("caption"),
                    json.dumps(b["headers"]) if b.get("headers") is not None else None,
                    json.dumps(b["rows"]) if b.get("rows") is not None else None,
                    _hash(b), source_url or None,
                )

            # transcript segments — ord-ordered. A timestamp-less inline segment (start None) is never deduped (chart.py
            # rule: a repeated "Thank you." must survive); we still record seg_hash for the timestamped ones.
            for i, s in enumerate(transcript_segments or []):
                if not isinstance(s, dict) or not (s.get("text") or "").strip():
                    continue
                seg_hash = (_hash({"sp": s.get("speaker", ""), "st": s.get("start"), "tx": s.get("text")})
                            if s.get("start") is not None else None)
                await conn.execute(
                    """INSERT INTO event_transcript_segments
                         (event_id, ord, speaker, start_s, end_s, "text", source_url, seg_hash)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8);""",
                    event_id, i, (str(s.get("speaker") or "").strip() or "SPEAKER_00"),
                    s.get("start"), s.get("end"), (s.get("text") or "").strip(), source_url or None, seg_hash,
                )

            # url ledger — one row per distinct resource (canon_key dedup); kind from the router; status 'done' since we
            # recorded it as part of a completed enrichment. ON CONFLICT keeps the first row.
            for u in urls or []:
                if not isinstance(u, str) or not u.lower().startswith(("http://", "https://")):
                    continue
                await conn.execute(
                    """INSERT INTO event_media_urls (event_id, url, canon_key, kind, status)
                       VALUES ($1,$2,$3,$4,'done')
                       ON CONFLICT (event_id, canon_key) DO NOTHING;""",
                    event_id, u, _canon(u), classify(u),
                )
        return True
