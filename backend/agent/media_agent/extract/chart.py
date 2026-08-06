"""media_agent.chart — the per-event ACCUMULATOR: every source (html page / pdf / audio / video) contributes its own
slice, and Chart FILLS-AND-APPENDS it into the right slot. Never re-outputs the whole thing.

用一句话讲完: 一个 event 的 close-loop 里,每处理一个 url 就产生一份 Contribution(这页的正文块、transcript 片段、
音频/文件记录、新 url),Chart 把它 append 到对应槽位 → 循环跑完 `build_documents()` 把每个 url 的缓冲区收成**一对**
(md + blocks json)→ 这就是该 event 的完整档案。**用户明确要 fill-and-append 而不是 diff/re-output**
{USER 2026-07-23 "the logic should be fill and append not output"} [CONFIDENCE: CONFIRMED 100% — direct user instruction],
所以这里没有 JSON-patch,只有确定性 append。

ONE PAIR PER URL: 每个来源产出一份 md(非结构化正文,表格处留 `[[TABLE:n]]` 占位符)+ 一个 blocks json(结构化)。
{USER 2026-08-05 "WE ONLY KEEP ONE MD + PLACEHODLER FOR GRAPHS AND TABELS USING JSON, SO ONE PAIR STURUTE RAND
 UNDSTRUCTRUED FOR ANY URL LEVEL"} [CONFIDENCE: CONFIRMED 100% — direct user directive].

WHY per-block content-hash dedup was REMOVED (it used to live here): the same table/paragraph legitimately appears on
multiple sources, and hashing every block to skip repeats looked like it kept the archive clean. It did not — it made
the stored copy unfaithful to the source. Measured: one page rendered 102 date-time lines and only 94 were stored,
because identical strings belonged to DIFFERENT companies meeting at the same time.
{RENDER /render_detail 2026-08-05 "渲染后 日期时间 出现 102 次"} vs {psql "日期时间块 | 94"}
[CONFIDENCE: CONFIRMED 100% — both numbers read off the live system.]
Dedup now happens where it is safe: at the DOCUMENT level (one row per event+url, a schema constraint) and at the
CHUNK SEAM (a positional trim of the deliberate overlap, not a content-global filter). Cross-source duplication — the
press-release body appearing both on the html page and inside the linked PDF — is now KEPT as two documents, because
they are two sources and collapsing them loses which one said what.
"""
from __future__ import annotations

import hashlib
import json
import re
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


# ── ONE MD + ONE JSON PER URL ───────────────────────────────────────────────────────────────────────────────────────
# 用一句话讲完: 一个 url 产出**一对**东西 —— 一份 md(非结构化正文,表格处只留 `[[TABLE:n]]` 占位符)+ 一个 blocks
# json(结构化,占位符指向的那些表)。读的时候把占位符换回表就是完整原文;做 embedding / chunk 的时候 md 是干净散文,
# 不会被一张几百行的表淹掉。
#
# WHY placeholders instead of inlining the table as GFM: a markdown document with a large table inlined is hostile to
# every downstream use — chunking splits it mid-row, embedding drowns the prose in numbers, and the exact cell
# structure can never be recovered from rendered pipes. Keeping prose clean and structure separate, joined by a
# positional marker, means the document can be read, chunked, or reconstructed in full without losing either view.
# {USER 2026-08-05 "WE ONLY KEEP ONE MD + PLACEHODLER FOR GRAPHS AND TABELS USING JSON, SO ONE PAIR STURUTE RAND
#  UNDSTRUCTRUED FOR ANY URL LEVEL"}
# [CONFIDENCE: CONFIRMED 100% — direct user directive; supersedes the per-block table columns it replaces.]
#
# The marker syntax is `[[TABLE:1]]` / `[[FIGURE:1]]` alone on its own line, numbered per-kind per-document from 1 in
# document order. Double square brackets do not occur in extracted IR prose and survive markdown rendering as literal
# text — so a reader whose blocks json went missing sees a visible marker rather than a silently absent table.
_PLACEHOLDER_RE = re.compile(r"^\s*\[\[(TABLE|FIGURE):(\d+)\]\]\s*$")
# A GFM pipe-table line: starts and ends with `|`. The separator row (`| --- | --- |`) matches this too, which is what
# lets a whole table region be consumed as one run of consecutive matching lines.
_GFM_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _table_block(idx: int, structured, gfm_lines: list[str]) -> dict:
    """Build one structured table object for the blocks json, preferring the extractor's OWN structured table over
    re-parsing the rendered pipes.

    WHY prefer `structured`: Docling and pandas both hand back real cell arrays, while the GFM text is a LOSSY
    rendering of them — a cell containing a `|`, a multi-line cell, or a merged header all survive in the array and
    are destroyed in the pipes. Re-parsing is the fallback for when the counts disagree, not the primary path.
    {CHART.PY "MARKDOWN (DOCLING'S CLEAN FULL TEXT, TABLES RENDERED INLINE = THE READABLE VIEW) + TABLES (STRUCTURED
     [{COLUMNS,ROWS}])"} [CONFIDENCE: CONFIRMED 100% — officeall/types.py DocResult carries both views.]
    """
    if isinstance(structured, dict):
        # Docling's shape is {columns, rows}; extract_html's is {headers, rows}. Accept either name for the header row
        # rather than making the caller normalize — the two extractors are independent and both are authoritative.
        headers = structured.get("headers") or structured.get("columns") or []
        rows = structured.get("rows") or []
        caption = structured.get("caption") or ""
    else:
        # Fallback: parse the rendered pipes. Drop the `| --- |` separator row — it is layout, not data.
        cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in gfm_lines]
        cells = [r for r in cells if not all(set(c) <= set("-: ") for c in r)]
        headers = cells[0] if cells else []
        rows = cells[1:] if len(cells) > 1 else []
        caption = ""
    return {"id": idx, "type": "table",
            "headers": [str(h) for h in headers],
            # Stringify defensively: pandas hands back numpy scalars and NaN, which json.dumps cannot serialize and
            # which would fail the whole event's write at the very last step.
            "rows": [[("" if c is None else str(c)) for c in (r or [])] for r in rows],
            "caption": str(caption)}


def blocks_to_pair(blocks: list[dict]) -> tuple[str, list[dict]]:
    """Ordered extract_html blocks → (md with placeholders, structured blocks json).

    Prose and list blocks contribute their markdown verbatim; a table block contributes a placeholder line to the md
    and its cells to the json. Order is preserved, which is what makes the placeholder positional rather than a
    lookup key. {EXTRACT_HTML.PY:85 "READING ORDER ACROSS MD+TABLE+LIST IS PRESERVED — THE INVARIANT
    CHART.APPEND_BASIC_INFO DEPENDS ON"} [CONFIDENCE: CONFIRMED 100% — the invariant this relies on is documented there.]

    NO DEDUP HERE, deliberately. Repeated text inside one document is legitimate and dropping it makes the stored md a
    non-faithful copy of the source. The per-block dedup this replaces was measured destroying real content: a page
    rendered 102 date-time lines and stored only 94, because identical strings belonged to DIFFERENT companies meeting
    at the same time. {RENDER /render_detail 2026-08-05 "渲染后 日期时间 出现 102 次"} vs {psql "日期时间块 | 94"}
    {RENDER TEXT "7| ENABLE INJECTIONS, INC." / "9| AUGUST 05, 2026 | 09:00 AM ET" / "11| KINGSTONE COMPANIES, INC." /
     "13| AUGUST 05, 2026 | 09:00 AM ET"}
    [CONFIDENCE: CONFIRMED 100% — both numbers read off the live system; the two companies are in the render output.]
    The sibling transcript path reached the same conclusion independently and says so:
    {CHART.PY "A TIMESTAMP-LESS INLINE SEGMENT IS NEVER DEDUPED — ELSE A SPEAKER WHO SAYS THE SAME THING TWICE LOSES ONE"}.
    """
    parts: list[str] = []
    structured: list[dict] = []
    n_tab = 0
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "table":
            n_tab += 1
            parts.append(f"[[TABLE:{n_tab}]]")
            structured.append(_table_block(n_tab, b, []))
        else:
            txt = (b.get("md") or b.get("text") or "").strip()
            if txt:                                        # an empty prose block would add a blank gap, nothing else
                parts.append(txt)
    return "\n\n".join(parts), structured


def markdown_to_pair(md: str, tables: list | None) -> tuple[str, list[dict], str]:
    """Docling markdown (tables ALREADY inlined as GFM) → (md with placeholders, structured blocks json, warning).

    Docling hands back both views of the same document — prose with tables rendered inline, plus the structured cell
    arrays. This walks the markdown, replaces each GFM table region with a placeholder, and pairs region *k* with
    `tables[k]` BY DOCUMENT ORDER, which is the same positional matching extract_html already relies on in the other
    direction: {EXTRACT_HTML.PY:85 "A GFM PIPE-TABLE REGION IS CONSUMED AS A POSITIONAL MARKER AND REPLACED BY THE NEXT
    AUTHORITATIVE PANDAS DATAFRAME (MATCHED BY DOM ORDER — BOTH MARKDOWN TABLES AND PANDAS TABLES ARE IN DOCUMENT ORDER)"}
    [CONFIDENCE: CONFIRMED 100% — the inverse of this pairing is already in production in extract_html.]

    Returns a non-empty `warning` when the region count and the tables count disagree. It does NOT guess: unmatched
    regions still become placeholders (so md and json stay one-for-one), but the json entry is reconstructed from the
    pipes and the caller is told, because a silent mismatch would mean the structured view quietly disagrees with the
    prose it is supposed to accompany.
    """
    tables = tables or []
    lines = (md or "").split("\n")
    out: list[str] = []
    structured: list[dict] = []
    n_tab, i = 0, 0
    while i < len(lines):
        if _GFM_ROW_RE.match(lines[i]):
            j = i
            while j < len(lines) and _GFM_ROW_RE.match(lines[j]):
                j += 1
            # A single pipe line is not a table — it is prose that happens to contain pipes (a footnote, a path, a
            # "revenue | segment" heading). A real GFM table is at least a header plus a separator or a data row.
            if j - i >= 2:
                n_tab += 1
                out.append(f"[[TABLE:{n_tab}]]")
                src = tables[n_tab - 1] if n_tab - 1 < len(tables) else None
                structured.append(_table_block(n_tab, src, lines[i:j]))
                i = j
                continue
        out.append(lines[i])
        i += 1
    warn = ""
    if n_tab != len(tables):
        warn = f"table-count-mismatch: {n_tab} GFM regions vs {len(tables)} structured tables"
    return "\n".join(out), structured, warn



# ── BINARY GUARD ───────────────────────────────────────────────────────────────────────────────────────────────────
# File magic that must never reach a content block. Routing decides which PARSER to use and it will sometimes guess
# wrong — an extension-less IR url that HEAD reports as text/html and then serves a pdf is a real case, observed on
# http://www.largan.com.tw/en/investor/finance, whose blocks landed in the DB as:
#   "%PDF-1.7 4 0 obj (Identity) endobj 5 0 obj << /Filter /FlateDecode …"
# followed by pages of decoded FlateDecode bytes.
# {DB 2026-08-05 event_content_blocks — 85 blocks of raw pdf on one event, block_type='md'}
# [CONFIDENCE: CONFIRMED — read out of the table].
#
# WHY the guard belongs HERE and not in the router: a routing heuristic can always be wrong about the next url, and
# every wrong guess would need its own fix. "This text begins with %PDF-" is not a guess — it is unambiguous, it is
# checkable at the last moment before persistence, and one check covers every path that can ever append a block.
_BINARY_MAGIC = (
    b"%PDF-",           # pdf
    b"PK\x03\x04",      # zip container: docx / xlsx / pptx
    b"\xd0\xcf\x11\xe0",# OLE2: legacy doc / xls / ppt
    b"\x89PNG", b"GIF8", b"\xff\xd8\xff",   # png / gif / jpeg
    b"ID3", b"OggS", b"fLaC", b"\x1aE\xdf\xa3",  # mp3 / ogg / flac / matroska
)


def _looks_binary(text: str) -> str:
    """'' when the text is real prose, else a short reason. Two independent tells, because they catch different
    failure shapes: a magic prefix catches a whole file decoded as text, while a high ratio of replacement/control
    characters catches a partially-decoded or wrongly-decoded body that has no recognisable header."""
    if not text:
        return ""
    head = text[:16].encode("utf-8", "surrogateescape")[:8]
    for magic in _BINARY_MAGIC:
        if head.startswith(magic):
            return f"file-magic:{magic[:4]!r}"
    sample = text[:2000]
    if not sample:
        return ""
    # U+FFFD is what a mis-decoded byte becomes; C0 controls other than tab/newline do not occur in extracted prose.
    bad = sum(1 for c in sample if c == "\ufffd" or (ord(c) < 32 and c not in "\t\n\r"))
    if bad / len(sample) > 0.05:
        return f"undecodable:{100*bad//len(sample)}%"
    return ""


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

        # ONE PAIR PER URL. `_html_bufs` collects a url's ordered blocks while its handler runs; `build_documents()`
        # joins each buffer into the (md, blocks-json) pair at the end. Buffering rather than joining on every append
        # is what lets ONE url append MORE THAN ONCE — a chunked page, or a VLM retry — and still land in ONE document
        # with placeholder numbering continuous across the appends instead of restarting at 1 each time.
        # {HANDLERS.PY "TWIN A — INPUT OVER CAP: ... CHUNK THE FULL TEXT" — that path calls append per chunk}
        # [CONFIDENCE: CONFIRMED 100% — the chunked path is in handlers.py and issues one append per chunk.]
        self._html_bufs: dict[str, list[dict]] = {}   # source url → its ordered blocks, awaiting the join
        self._html_order: list[str] = []              # visit order, so documents come out in dispatch order
        self.transcript_segments: list[dict] = []   # {"speaker","start","end","text"}
        self.transcript_sources: list[str] = []     # which audio/page each segment batch came from
        self.audio: list[dict] = []            # {"url","local_path","duration_s"}
        self.files: dict[str, list] = {router.KIND_PDF: [], router.KIND_PPTX: [],
                                       router.KIND_DOCX: [], router.KIND_XLSX: []}   # xlsx bucket (edge audit H2)
        self.urls: dict[str, dict] = {}        # canonical → {"url","kind","status"} — the url ledger (dedup by canon)

        # NO _block_hashes any more. Per-block content dedup was deleted, not relocated — see blocks_to_pair's docstring
        # for the measurement that condemned it. Dedup now happens at the DOCUMENT level only: one row per (event, url),
        # which is a property of the schema rather than a filter that silently eats repeated prose.
        self._seg_hashes: set[str] = set()     # transcript segments keep their own guard (timestamped segments only)
        self.page_kind: dict[str, str] = {}    # canon(url) → 'event' | 'hub' | 'dead', as judged by the VLM
        self._page_kind_order: list[str] = []  # judgement order, so verdict() can prefer the detail page

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

    def set_page_kind(self, url: str, kind: str) -> None:
        """Record what the model judged this page to BE (event / hub / dead). Stored per url, not per event, because
        one event can carry several urls and only the DETAIL page's verdict is meaningful.

        WHY the Chart only records and never acts: dropping an event or promoting a hub is an EVENT-level decision that
        needs the claim token and the DB pool, neither of which belong in an accumulator. Keeping the judgement and the
        consequence apart is also what makes the verdict inspectable in a trace before anything irreversible happens.
        """
        if kind:
            k = _canon(url)
            if k not in self.page_kind:
                self._page_kind_order.append(k)
            self.page_kind[k] = kind

    def verdict(self) -> str:
        """The event-level page verdict: the DETAIL page's kind. '' when no page was judged.

        Takes the FIRST recorded verdict rather than a vote: the detail page is dispatched first and is the page the
        event claims to be about, so a later asset's verdict must not override it.
        """
        for k in self._page_kind_order:
            v = self.page_kind.get(k)
            if v:
                return v
        return ""

    def pending_urls(self) -> list[str]:
        """Urls recorded on this chart that no handler has run yet — i.e. the documents adopted from a rendered page.

        Reads the ledger's own `status` rather than keeping a separate list, so a url can never be pending in one place
        and done in another. Order follows insertion, which is the order the model named them.
        """
        return [v["url"] for v in self.urls.values() if (v.get("status") or "pending") == "pending"]

    def set_status(self, url: str, status: str) -> None:
        """Mark a url done/failed/skipped after its handler ran."""
        self.urls.setdefault(_canon(url), {"url": url, "kind": router.classify(url)})["status"] = status

    # ── fill-and-append slots ────────────────────────────────────────────────────────────────────────────────
    def append_basic_info(self, blocks: list[dict], source_url: str = "") -> int:
        """Buffer an html page's ordered content blocks under the url they came from. Returns how many landed.

        The blocks are NOT joined here — `build_documents()` does that once the url's handler has finished, so a url
        that appends more than once (a chunked page, a VLM retry) still produces ONE document with continuous
        placeholder numbering. Order within the buffer is the reading order the extractor produced.

        `source_url` is explicit rather than tracked as "current url" state on the Chart: the three call sites all know
        their url, and implicit state here would silently mis-file a document the first time two handlers ever overlap.
        {HANDLERS.PY:250 / :292 / :300 — the three append_basic_info call sites, each inside a handler that has `url`}
        [CONFIDENCE: CONFIRMED 100% — counted by grep; all three are url-scoped.]

        NO CONTENT DEDUP. See blocks_to_pair's docstring for the measurement that removed it.
        """
        added = 0
        key = _canon(source_url) if source_url else ""
        buf = self._html_bufs.setdefault(key, [])
        if key not in self._html_order:
            self._html_order.append(key)                       # first write for this url fixes its document order

        # SEAM TRIM — the one place duplicate blocks are still dropped, and it is positional, not content-global.
        # The chunked path splits an over-cap page into pieces that deliberately OVERLAP backwards so no single item is
        # cut in half: {HANDLERS.PY:223 "EACH BLOCK AFTER THE FIRST WIDENED BACKWARD BY ~OVERLAP CHARS (SNAPPED TO A
        # LINE START) SO NO SINGLE ITEM IS SPLIT ACROSS A CUT"} with {HANDLERS.PY:44 "_CHUNK_OVERLAP ... 1500"}.
        # That overlap re-delivers the tail of the previous chunk, which the deleted global dedup used to absorb.
        # Trimming the SEAM — the longest prefix of the incoming blocks that equals the buffer's suffix — removes
        # exactly the re-delivered run and nothing else, so a page that legitimately repeats a line elsewhere keeps
        # both copies. That distinction is the whole reason the global dedup had to go.
        # [CONFIDENCE: CONFIRMED 100% — the overlap is deliberate and its size is a named constant in handlers.py.]
        incoming = [b for b in (blocks or []) if isinstance(b, dict) and b.get("type")]
        if buf and incoming:
            for k in range(min(len(buf), len(incoming)), 0, -1):
                if buf[-k:] == incoming[:k]:
                    incoming = incoming[k:]
                    break

        for b in incoming:                                     # already filtered to well-formed dict blocks above
            # Reject binary before it is stored. Dropped LOUDLY, not silently: a page that turns out to be a
            # mis-routed file is a routing signal worth seeing, and a silent drop looks identical to a page that
            # genuinely had nothing on it. This guard STAYS — dropping mis-routed binary is a different act from
            # dropping repeated prose, and only the latter was the bug.
            why = _looks_binary(str(b.get("md") or b.get("text") or ""))
            if why:
                print(f"[chart] ⛔ dropped {b.get('type')} block — {why} (mis-routed binary, not prose)", flush=True)
                continue
            buf.append(b)
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

    # ── the pair, per url ────────────────────────────────────────────────────────────────────────────────────
    def build_documents(self) -> list[dict]:
        """Every source this event produced, as ONE (md, blocks) pair each: [{url, kind, md, blocks, n_chars, n_blocks}].

        This is the single place the two extraction paths converge. Html pages arrive as ordered blocks and are joined
        with placeholders; office documents arrive from Docling as markdown that ALREADY has its tables inlined, and
        those inlined regions are swapped BACK OUT to placeholders so both paths store the identical shape. Callers
        (the DB writer, the worker's counters, the trace dumps) see one list and never branch on where a document
        came from.

        Documents with no text at all are omitted — an empty md is not a document, and emitting one would make an
        event look enriched while carrying nothing. The event-level "nothing usable" decision stays with the caller,
        which is the only place that can see the transcript and audio slots too.
        """
        docs: list[dict] = []
        for key in self._html_order:                           # html pages, in the order their handlers ran
            md, blocks = blocks_to_pair(self._html_bufs.get(key) or [])
            if not md.strip():
                continue
            docs.append({"url": self.urls.get(key, {}).get("url", key), "kind": "html",
                         "md": md, "blocks": blocks, "n_chars": len(md), "n_blocks": len(blocks)})
        for kind, entries in (self.files or {}).items():       # office documents, one per parsed file
            for f in entries or []:
                md, blocks, warn = markdown_to_pair(f.get("markdown") or "", f.get("tables"))
                if warn:
                    # Loud, not silent: a mismatch means the structured view disagrees with the prose it accompanies,
                    # and the only honest response is to say so with both counts rather than pick a winner.
                    print(f"[chart] ⚠️ {warn} on {str(f.get('url'))[:70]} — placeholders kept, "
                          f"unmatched tables reconstructed from the rendered pipes", flush=True)
                if not md.strip():
                    continue
                docs.append({"url": f.get("url") or "", "kind": kind, "md": md, "blocks": blocks,
                             "n_chars": len(md), "n_blocks": len(blocks), "n_pages": f.get("n_pages") or 0})
        return docs

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
            # One (md, blocks) pair per source url — replaces the old flat block list. Callers that used to walk
            # `basic_info` now walk documents and get the prose and the structure together, per source.
            "documents": self.build_documents(),
            "transcript": ({"segments": self.transcript_segments, "sources": self.transcript_sources}
                           if self.transcript_segments else None),
            "audio": self.audio,
            "files": self.files,
            "urls": list(self.urls.values()),
        }
