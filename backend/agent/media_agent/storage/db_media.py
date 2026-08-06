"""media_agent.db_media — persist ONE enriched event into the NORMALIZED media schema (event_documents /
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


async def mark_enriched_media(pool, event_id, claim_token, documents: list[dict],
                              transcript_segments: list[dict], urls: list[str], source_url: str = "",
                              meta: dict | None = None,
                              audio: list[dict] | None = None,
                              url_status: dict | None = None) -> bool:
    """Flip event→enriched AND write its normalized media rows, fenced on claim_token, in ONE transaction. Returns True
    if we still owned the row (the flip landed); False if a re-claim stole it (nothing written). `documents` = Chart.build_documents()
    output, one (md, blocks) pair per source url; `transcript_segments` = speaker/start/end/text; `urls` = the event's media urls;
    `source_url` = the detail page these came from (stamped on each block/segment for provenance).

    `files` is GONE as a separate argument — office documents are folded into `documents` by
    Chart.build_documents(), which swaps Docling's inlined GFM tables back out to [[TABLE:n]] markers so an html page
    and a pdf are stored in the identical shape.
    `audio` = Chart.audio [{url, local_path, duration_s}]            → event_audio.
    `url_status` = Chart's ledger {url: status} so the ledger records what ACTUALLY happened per resource
    ('done' / 'failed:…' / 'skipped:…') instead of stamping every row 'done'.

    用一句话讲完: 之前这里只写 3 张表(content_blocks / transcript_segments / media_urls),event_media_files 和
    event_audio **在整个仓库里没有任何写入方** —— Docling 解析出的 pdf markdown 和转写出的音频记录无处可去。这次把
    5 张表补齐,并且让 url 账本记录真实状态而不是一律 'done'。
    [CONFIDENCE: CONFIRMED — `grep -rn "event_media_files|event_audio" --include=*.py agent/` returned nothing before]."""
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
                    ),
                    -- METADATA REPAIR. Each field is written only when the model supplied a NON-EMPTY value that
                    -- differs from what is stored — the prompt instructs it to return "" to keep a good existing
                    -- value, so an empty string means "no opinion", not "blank it out".
                    -- {PROMPTS.PY SYSTEM_ROUTE "LEAVE A FIELD \"\" TO KEEP THE ALREADY-KNOWN VALUE — DO NOT OVERWRITE GOOD INFO"}
                    -- [CONFIDENCE: CONFIRMED 100% — the instruction is in the prompt this value comes from.]
                    title      = CASE WHEN coalesce($4,'') <> '' AND $4 <> title      THEN $4 ELSE title      END,
                    event_date = CASE WHEN coalesce($5,'') <> '' AND $5 <> event_date THEN $5 ELSE event_date END,
                    event_type = CASE WHEN coalesce($6,'') <> '' AND $6 <> event_type THEN $6 ELSE event_type END,
                    -- ...and record what was REPLACED, keyed by field. Storing the previous value rather than a flag
                    -- is what makes a repair auditable later and a bad repair recoverable.
                    meta_fixed = NULLIF(coalesce(events.meta_fixed, '{}'::jsonb) || (
                        (CASE WHEN coalesce($4,'') <> '' AND $4 <> title      THEN jsonb_build_object('title', title)      ELSE '{}'::jsonb END) ||
                        (CASE WHEN coalesce($5,'') <> '' AND $5 <> event_date THEN jsonb_build_object('date',  event_date) ELSE '{}'::jsonb END) ||
                        (CASE WHEN coalesce($6,'') <> '' AND $6 <> event_type THEN jsonb_build_object('type',  event_type) ELSE '{}'::jsonb END)
                    ), '{}'::jsonb)
                WHERE id=$1 AND claim_token=$2 RETURNING id;
                """,
                event_id, claim_token, json.dumps(urls or []),
                (meta or {}).get('title') or None, (meta or {}).get('date') or None,
                (meta or {}).get('type') or None,
            )
            if row is None:                               # re-claimed by another worker → do NOT write orphan child rows
                return False

            # documents — ONE ROW PER (event, source url): the md (prose, tables replaced by [[TABLE:n]] markers)
            # plus the blocks json those markers point at. This replaces the old two-table split (per-block rows in
            # event_content_blocks + one row per file in event_media_files); both extraction paths now produce the
            # identical shape, so this loop has no branch on where a document came from.
            # {USER 2026-08-05 "ONE MD + PLACEHODLER FOR GRAPHS AND TABELS USING JSON, SO ONE PAIR ... FOR ANY URL LEVEL"}
            # [CONFIDENCE: CONFIRMED 100% — direct user directive.]
            #
            # ON CONFLICT (event_id, url) DO UPDATE, not DO NOTHING: a re-enrich of the same event must REFRESH what it
            # extracted. DO NOTHING would pin the first (possibly broken) extraction forever and make every later fix
            # invisible for exactly the events that needed it — which is the failure mode this whole change exists to
            # undo. The unique key is the SOURCE, so refreshing cannot cross-contaminate two different urls.
            for d in (documents or []):
                if not isinstance(d, dict) or not (d.get("md") or "").strip():
                    continue                              # an empty md is not a document; never store a hollow row
                md = d["md"]
                await conn.execute(
                    """INSERT INTO event_documents
                         (event_id, url, kind, md, blocks, n_chars, n_blocks, content_hash)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                       ON CONFLICT (event_id, url) DO UPDATE SET
                         kind = excluded.kind, md = excluded.md, blocks = excluded.blocks,
                         n_chars = excluded.n_chars, n_blocks = excluded.n_blocks,
                         content_hash = excluded.content_hash;""",
                    event_id, d.get("url") or "", d.get("kind") or "html", md,
                    json.dumps(d.get("blocks") or []), len(md), len(d.get("blocks") or []),
                    _hash({"u": _canon(d.get("url") or ""), "md": md[:4000]}),
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

            # audio artifacts — one row per transcribed source (unique on (event_id, url)). local_path is transient
            # (the bytes are not kept); duration_s is what makes whisper cost auditable after the fact.
            for a in (audio or []):
                if not isinstance(a, dict) or not a.get("url"):
                    continue
                await conn.execute(
                    """INSERT INTO event_audio (event_id, url, local_path, duration_s)
                       VALUES ($1,$2,$3,$4)
                       ON CONFLICT (event_id, url) DO NOTHING;""",
                    event_id, a["url"], a.get("local_path") or None, a.get("duration_s"),
                )

            # url ledger — one row per distinct resource (canon_key dedup); kind from the router. The status is what
            # ACTUALLY happened to that resource ('done' / 'failed:…' / 'skipped:…'), taken from Chart's ledger, so a
            # webcast we could not capture is visibly skipped rather than silently indistinguishable from a parsed pdf.
            # The column has a CHECK constraint on (pending|done|failed|skipped), so a detailed reason is truncated to
            # its prefix here — the full reason already went to the log, loudly.
            for u in urls or []:
                if not isinstance(u, str) or not u.lower().startswith(("http://", "https://")):
                    continue
                raw = (url_status or {}).get(u) or "done"
                # `status` is the coarse enum the CHECK constraint allows; `reason` carries the full string. Splitting
                # them is what lets the ledger answer BOTH "how many were skipped" and "skipped for what" — before
                # `reason` existed the suffix was truncated away and those two questions collapsed into one.
                # {MIGRATION 20260723145355 "CHECK (STATUS IN ('PENDING','DONE','FAILED','SKIPPED'))"}
                # [CONFIDENCE: CONFIRMED 100% — the constraint is why this truncation exists at all.]
                st = next((s for s in ("done", "failed", "skipped", "pending") if raw.startswith(s)), "done")
                await conn.execute(
                    """INSERT INTO event_media_urls (event_id, url, canon_key, kind, status, reason)
                       VALUES ($1,$2,$3,$4,$5,$6)
                       ON CONFLICT (event_id, canon_key) DO NOTHING;""",
                    event_id, u, _canon(u), classify(u), st, (raw if raw != st else None),
                )
        return True
