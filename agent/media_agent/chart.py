"""media_agent.chart — the per-event ACCUMULATOR: every source (html page / pdf / audio / video) contributes its own
slice, and Chart FILLS-AND-APPENDS it into the right slot. Never re-outputs the whole thing.

用一句话讲完: 一个 event 的 close-loop 里,每处理一个 url 就产生一份 Contribution(这页的 basic_info blocks、发现的
transcript 片段、音频/文件记录、新 url),Chart 把它 append 到对应槽位并按 content-hash 去重 → 循环跑完就是这个 event
的完整档案 chart。**用户明确要 fill-and-append 而不是 diff/re-output** {USER 2026-07-23 "the logic should be fill and
append not output"} [CONFIDENCE: CONFIRMED 100% — direct user instruction],所以这里没有 JSON-patch,只有确定性 append。

WHY content-hash dedup: the same table/paragraph/url legitimately appears on multiple sources (a press-release body on
the html page AND inside the linked PDF). Append-everything would duplicate it; hashing each block/segment/url and
skipping seen ones keeps the archive clean without the model having to reason about "did I already say this".
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import router

# Tracking / analytics query params that never change WHICH resource a url points at — strip them before dedup so the
# same page linked with different campaign tags collapses to one. Meaningful params (page, id, year, ticker) are KEPT,
# so pagination ?page=2 and a per-item ?id=11107 stay distinct. {common UTM/click-id param set}.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "utm_name",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
    "igshid", "yclid", "wickedid", "twclid", "s_cid", "cmpid", "spm", "ref", "ref_src"})


def _canon(url: str) -> str:
    """Canonical dedup key for a url: lowercase scheme+host, drop #fragment + trailing slash, strip tracking params,
    keep MEANINGFUL query (sorted for stability). Extends event_agent.crawl._canon with tracking-param stripping so a
    utm-tagged link doesn't re-enqueue an already-visited page, while ?page=2 / ?id=… stay distinct."""
    try:
        p = urlsplit(url if url.startswith("http") else "https://" + url)
        # keep only non-tracking query params, sorted → the same url with reordered/tagged params has ONE key
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
        query = urlencode(sorted(kept))
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), query, "")) or url
    except Exception:                          # noqa: BLE001 — unparseable → use the raw string as its own key
        return url


def _hash(obj) -> str:
    """Stable short content hash of any JSON-able block/segment — for append-time dedup. Sorted keys so dict order
    never changes the hash; md5 is fine (dedup, not security)."""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True)   # canonical JSON text of the content
    return hashlib.md5(blob.encode("utf-8")).hexdigest()          # noqa: S324 — non-security dedup key


class Chart:
    """One event's growing archive. Seeded from the event_agent event (title/date/type/urls), then FILLED by
    Contributions as the close-loop visits each url. Call to_dict() at the end for the final chart JSON."""

    def __init__(self, event: dict):
        # metadata — seed from event_agent, later confirmed/filled by the detail page (keep-first-non-empty, so the
        # crawl-level value is the default and a page only OVERWRITES an EMPTY field, never a good existing one).
        self.title = (event.get("title") or "").strip()
        self.date = (event.get("date") or "").strip()
        self.type = (event.get("type") or "").strip()
        # event_id — stable across reruns: canonical of the event's FIRST url (its detail page), slugified.
        first = (event.get("urls") or [""])[0]
        self.event_id = _canon(first).replace("https://", "").replace("http://", "").replace("/", "-")[:120] or "event"

        self.basic_info: list[dict] = []       # ordered blocks: {"type":"md"|"table"|"list", ...}
        self.transcript_segments: list[dict] = []   # {"speaker","start","end","text"}
        self.transcript_sources: list[str] = []     # which audio/page each segment batch came from
        self.audio: list[dict] = []            # {"url","local_path","duration_s"}
        self.files: dict[str, list] = {router.KIND_PDF: [], router.KIND_PPTX: [],
                                       router.KIND_DOCX: [], router.KIND_XLSX: []}   # xlsx bucket (edge audit H2)
        self.urls: dict[str, dict] = {}        # canonical → {"url","kind","status"} — the url ledger (dedup by canon)

        self._block_hashes: set[str] = set()   # dedup guards
        self._seg_hashes: set[str] = set()

        for u in (event.get("urls") or []):    # seed the ledger with the event's known urls (status starts pending)
            self.add_url(u, status="pending")

    # ── url ledger ────────────────────────────────────────────────────────────────────────────────────────────
    def add_url(self, url: str, kind: str | None = None, status: str = "pending") -> bool:
        """Record/refresh a url in the ledger. Returns True if this url is NEW (caller should enqueue it), False if
        already seen (canonical dedup). kind defaults to router.classify(url) when not given."""
        u = (url or "").strip()
        if not u:
            return False
        if not u.lower().startswith(("http://", "https://")):
            # A non-http url reaching the ledger means a caller failed to resolve a relative link (edge audit H1) —
            # FAIL LOUDLY so the miss is visible, and skip it (a broken canon key would silently poison dedup).
            print(f"[media] ⚠️ add_url got non-http {u!r} — skipping (caller should urljoin against the page base)", flush=True)
            return False
        ck = _canon(u)
        is_new = ck not in self.urls           # newness decided on canonical key, so ?utm= dupes don't re-enqueue
        if is_new:
            self.urls[ck] = {"url": u, "kind": kind or router.classify(u), "status": status}
        else:                                  # already known — only advance its status/kind, keep the first url form
            if kind:
                self.urls[ck]["kind"] = kind
            self.urls[ck]["status"] = status
        return is_new

    def set_status(self, url: str, status: str) -> None:
        """Mark a url done/failed/skipped after its handler ran."""
        self.urls.setdefault(_canon(url), {"url": url, "kind": router.classify(url)})["status"] = status

    # ── fill-and-append slots ────────────────────────────────────────────────────────────────────────────────
    def append_basic_info(self, blocks: list[dict]) -> int:
        """Append ordered content blocks (md/table/list) from an html page, skipping any whose content we already
        have. Returns how many NEW blocks landed. Order is preserved (reading order across sources)."""
        added = 0
        for b in blocks or []:
            if not isinstance(b, dict) or not b.get("type"):   # ignore malformed blocks defensively
                continue
            h = _hash(b)                                       # content hash → cross-source table/paragraph dedup
            if h in self._block_hashes:
                continue
            self._block_hashes.add(h)
            self.basic_info.append(b)
            added += 1
        return added

    def append_transcript(self, segments: list[dict], source_url: str = "") -> int:
        """Append speaker-annotated transcript segments (from WhisperX, OR from a transcript found inline on an html
        page — those route HERE, never into basic_info). Dedup by (speaker,start,text)."""
        added = 0
        for s in segments or []:
            if not isinstance(s, dict) or not (s.get("text") or "").strip():
                continue
            # Dedup ONLY timestamped segments (real audio: same speaker+start+text ⇒ genuinely the same segment).
            # A timestamp-LESS inline segment (html transcript, start=None) is NEVER deduped — else a speaker who says
            # "Thank you." twice would lose one (edge audit M2: quality loss). Better a rare dupe than a dropped line.
            if s.get("start") is not None:
                h = _hash({"sp": s.get("speaker", ""), "st": s.get("start"), "tx": s.get("text")})
                if h in self._seg_hashes:
                    continue
                self._seg_hashes.add(h)
            # coerce missing OR empty speaker → SPEAKER_00 ("" from the VLM must not leak through). Real diarization /
            # name-mapping comes later; SPEAKER_00 is the v1 placeholder. {USER 2026-07-23 "first use speaker_00 for now"}.
            self.transcript_segments.append({"speaker": (str(s.get("speaker") or "").strip() or "SPEAKER_00"),
                                             "start": s.get("start"), "end": s.get("end"),
                                             "text": (s.get("text") or "").strip()})
            added += 1
        if added and source_url and source_url not in self.transcript_sources:
            self.transcript_sources.append(source_url)         # remember which audio/page fed these segments
        return added

    def append_audio(self, url: str, local_path: str = "", duration_s: float | None = None) -> None:
        """Record an audio artifact (the mp3 we downloaded + transcribed)."""
        self.audio.append({"url": url, "local_path": local_path, "duration_s": duration_s})

    def append_file(self, kind: str, url: str, markdown: str = "", tables: list | None = None, n_pages: int = 0) -> None:
        """Record a parsed office document — the native officeall.DocResult shape: `markdown` (Docling's clean full
        text, tables rendered inline = the readable view) + `tables` (structured [{columns,rows}] = the JSON view).
        md-for-prose + JSON-for-tables, same principle as basic_info. kind ∈ {pdf,pptx,docx,xlsx}. {OFFICEALL DocResult
        ".text (markdown)", ".tables ([{columns,rows}])"} [CONFIDENCE: CONFIRMED 100% — officeall/types.py DocResult]."""
        self.files.setdefault(kind, []).append(
            {"url": url, "markdown": markdown, "tables": tables or [], "n_pages": n_pages})

    def confirm_metadata(self, title: str = "", date: str = "", type_: str = "") -> None:
        """Fill any EMPTY metadata field from the detail page. Never overwrites an existing non-empty value — the
        crawl-level title/date/type is trusted; the page only supplies what was missing."""
        if not self.title and title:
            self.title = title.strip()
        if not self.date and date:
            self.date = date.strip()
        if not self.type and type_:
            self.type = type_.strip()

    # ── finalize ─────────────────────────────────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """The final chart JSON for this event. transcript/audio/files omitted-as-empty are still present (stable
        shape for downstream consumers), but transcript is None when nothing was transcribed."""
        return {
            "event_id": self.event_id,
            "title": self.title, "date": self.date, "type": self.type,
            "basic_info": self.basic_info,
            "transcript": ({"segments": self.transcript_segments, "sources": self.transcript_sources}
                           if self.transcript_segments else None),
            "audio": self.audio,
            "files": self.files,
            "urls": list(self.urls.values()),
        }
