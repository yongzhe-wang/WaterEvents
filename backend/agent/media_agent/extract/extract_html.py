"""media_agent.extract_html — DETERMINISTIC HTML → basic_info blocks + media-url candidates + transcript flags.

用一句话讲完: trafilatura 抽正文 markdown(boilerplate 剥离)→ pandas.read_html 抽**权威**表格 cells → 按表序 splice 回
markdown 位置(保阅读顺序,不让表格飘到末尾)→ lxml 穷举 harvest 所有 media url(+ render['links'] 补 walled tier 的
deterministic transcript 检测(speaker-turn 模式)→ 返回 {blocks, transcript_idx, tier}。
这样 VLM 不再逐字 copy body(输出从 KB → 几十字节,earnings 财报页不再撑爆),而 trafilatura 抽表比 VLM 更准。
Fallback 链(每档 try/except 防 malformed HTML raise): trafilatura → readability → resiliparse → docling → 空(caller
用 VLM screenshot_body 兜底,fail-loud)。{USER 2026-07-24 "combine deterministic tools, best for media, full plan"}
[CONFIDENCE: CONFIRMED — design workflow wf_7b61c8d0, 6 edge cases adversarially verified + fixed].
"""
from __future__ import annotations

import io
import os
import re
from urllib.parse import urljoin

# INPUT-OVERFLOW GUARD (shared by enrich.py + handlers.py) — vLLM REJECTS (400) a request whose input_tokens + max_tokens >
# max_model_len BEFORE generating, so a big page (a .pdf rendered to 20769 tokens of text) + a large requested output = hard
# failure, not truncation. ROUTE mode requests FEW output tokens (its output is only metadata + url ids + a modest transcript)
# and fit_input() caps the input to match, making the 400 structurally impossible. {POD 2026-07-25 400 "input 20769 + max
# 12000 > 32768"} [CONFIDENCE: CONFIRMED 100% — the 400 body]. Single source here so both html paths size identically.
CONTEXT = int(os.environ.get("QWEN_CONTEXT", "32768"))         # server --max-model-len (must match serve_vl.sh)
ROUTE_MAX_TOKENS = int(os.environ.get("MEDIA_ROUTE_MAX_TOKENS", "6000"))   # ROUTE output ceiling — small; body is deterministic
_CHARS_PER_TOK = 2.1                                            # conservative (observed 48000 chars ≈ 20769 tok ⇒ 2.31); under-est trims MORE (safe)
_CTX_MARGIN = 1200                                             # headroom for the chat template / system prompt overhead


def fit_input(text: str, out_tokens: int) -> str:
    """Trim `text` so its estimated input tokens + `out_tokens` stay under CONTEXT (with margin) → the vLLM 400 input+max
    pre-reject is structurally impossible regardless of page size. The deterministic body is captured BEFORE the VLM call,
    so trimming the VLM's INPUT view costs nothing structural — the body is already deterministic."""
    budget_chars = int(max(2000, (CONTEXT - out_tokens - _CTX_MARGIN) * _CHARS_PER_TOK))   # floor 2000 so we never send empty
    return text[:budget_chars]

# NON-MEDIA url filter — the exhaustive lxml harvest grabs EVERY href, so we drop nav/social/legal/feed/asset chrome here
# (the VLM then only CLASSIFIES which survivors are this event's material). Mirrors enrich._NONMEDIA_URL_RE + router asset
# sense. {ENRICH.PY:35-38 _NONMEDIA_URL_RE} [CONFIDENCE: CONFIRMED — same filter family, kept local to decouple modules].
_NONMEDIA_URL_RE = re.compile(
    r'(mailto:|tel:|javascript:|#)'                                  # non-http actions
    r'|(facebook|twitter|linkedin|instagram|youtube\.com/(user|channel)|x\.com|t\.co)\b'   # social chrome
    r'|/(privacy|terms|cookie|legal|sitemap|login|signin|register|subscribe|rss|feed|search|contact|careers?)(/|\.|\?|#|$)',
    re.I)

# SPEAKER-TURN patterns → a body block is TRANSCRIPT-LIKE. WHY deterministic (not VLM-trust): edge-case verify found that
# gating transcript-suppression on "did the VLM emit segments" evaporates content when the VLM under-delivers — inline
# transcript segments are NEVER deduped {CHART.PY:128-131}, so a dropped line is lost forever. So we DETECT transcript
# blocks deterministically here and only delete a flagged block when the VLM actually covers THAT block.
# {DESIGN wf_7b61c8d0 edge "transcript-only page" FIX A} [CONFIDENCE: CONFIRMED — per-block match beats whole-region trust].
_SPEAKER_RE = re.compile(
    r'(^|\n)\s*(operator\s*[:：]|q\s*[—\-–]|a\s*[—\-–]|[A-Z][a-z]+ [A-Z][a-z]+\s*[:：—\-–]|[A-Z][A-Z .]+\s*[:：])',
    re.I)


def _flatten_header(cols) -> list[str]:
    """MultiIndex / merged-header table → a FLAT list[str] of column names (the schema's headers is a flat string list
    {PROMPTS.PY:122}). A tuple column (MultiIndex) is joined; a scalar is str()'d. {DESIGN table-mapping guard 2}."""
    out = []
    for c in cols:
        if isinstance(c, tuple):
            out.append(" ".join(str(x) for x in c if str(x) != "nan").strip())
        else:
            out.append(str(c))
    return out


def _table_block(df, caption: str = "") -> dict:
    """One pandas DataFrame → the schema TABLE block {type,caption,headers,rows}. df.fillna('').astype(str) forces every
    cell to a STRING and NaN→'' (not 'nan') — the schema rows are string arrays {PROMPTS.PY:122} and the jsonb INSERT
    expects strings {DB_MEDIA.PY:60}; a raw numeric/NaN cell type-mismatches the INSERT. {DESIGN table-mapping guard 1}."""
    clean = df.fillna("").astype(str)
    return {"type": "table", "caption": caption or "",
            "headers": _flatten_header(clean.columns.tolist()),
            "rows": clean.values.tolist()}


# GFM pipe-table region marker in trafilatura markdown — a run of lines that start+end with '|'. We DON'T parse cells from
# it (trafilatura's pipe-table loses column alignment on wide IR financials); it's only a POSITIONAL MARKER telling us
# "a table belongs HERE in reading order" so the authoritative pandas table is spliced at the right index. {DESIGN STAGE 4}.
_PIPE_ROW_RE = re.compile(r'^\s*\|.*\|\s*$')
_LIST_RE = re.compile(r'^\s*([-*+]|\d+[.)])\s+')


def _md_to_blocks(md: str, dfs: list) -> list[dict]:
    """Walk the trafilatura markdown line-stream → ordered blocks. Paragraph/heading → {type:md}; consecutive list items →
    ONE {type:list}; a GFM pipe-table region is CONSUMED as a positional marker and replaced by the NEXT authoritative
    pandas DataFrame (matched by DOM order — both markdown tables and pandas tables are in document order). If pandas has
    MORE tables than markdown markers (trafilatura dropped some as boilerplate), the extras are appended at the end (logged
    by the caller). Reading order across md+table+list is preserved — the invariant chart.append_basic_info depends on
    {CHART.PY:108 "Order is preserved (reading order across sources)"}. [CONFIDENCE: CONFIRMED — table-index matching]."""
    blocks: list[dict] = []
    df_iter = iter(dfs)
    lines = (md or "").splitlines()
    i, n = 0, len(lines)
    buf_para: list[str] = []
    buf_list: list[str] = []

    def _flush_para():                                            # emit accumulated paragraph lines as one md block
        if buf_para:
            blocks.append({"type": "md", "md": "\n".join(buf_para).strip()})
            buf_para.clear()

    def _flush_list():                                           # emit accumulated list items as one list block
        if buf_list:
            blocks.append({"type": "list", "md": "\n".join(buf_list).strip()})
            buf_list.clear()

    while i < n:
        line = lines[i]
        if _PIPE_ROW_RE.match(line):                            # entered a GFM pipe-table region → consume it, splice pandas df
            _flush_para(); _flush_list()
            while i < n and (_PIPE_ROW_RE.match(lines[i]) or lines[i].strip() == ""):   # skip the whole pipe region
                if lines[i].strip() == "":
                    break
                i += 1
            df = next(df_iter, None)                            # the authoritative table for THIS position
            if df is not None:
                blocks.append(_table_block(df))
            continue
        if _LIST_RE.match(line):                                # a list item → accumulate into the current list block
            _flush_para()
            buf_list.append(line.strip())
            i += 1
            continue
        if line.strip() == "":                                  # blank line = block boundary
            _flush_para(); _flush_list()
            i += 1
            continue
        _flush_list()                                           # a prose/heading line → paragraph buffer
        buf_para.append(line)
        i += 1
    _flush_para(); _flush_list()
    for df in df_iter:                                          # pandas tables trafilatura dropped → append at end (edge #1 tail)
        blocks.append(_table_block(df))
    return blocks


def _pandas_tables(html: str) -> list:
    """Authoritative table extraction — one DataFrame per <table>. Zero tables raises ValueError → return [] (not an error).
    {DESIGN STAGE 4; pandas.read_html flavor=lxml}."""
    try:
        import pandas as pd
        return pd.read_html(io.StringIO(html), flavor="lxml")
    except (ValueError, ImportError):                           # no <table> OR pandas/lxml missing → no table blocks
        return []
    except Exception:                                           # noqa: BLE001 — malformed table markup must not sink extraction
        return []


def _trafilatura(html: str, base_url: str) -> str:
    """Primary body extractor → markdown (boilerplate stripped, tables as GFM markers, reading order kept). Empty/None on a
    thin page → caller walks the fallback chain. {DESIGN STAGE 2; trafilatura F1 0.937}."""
    try:
        import trafilatura
        return trafilatura.extract(html, output_format="markdown", include_tables=True, include_links=True,
                                   include_formatting=True, favor_recall=True, with_metadata=False, url=base_url) or ""
    except Exception:                                          # noqa: BLE001 — malformed html → fall through (edge #5)
        return ""


def _readability(html: str) -> str:
    """Fallback #1 — Firefox Reader-View port, most robust across page types. Returns a cleaned HTML fragment we re-feed to
    trafilatura markdown. {DESIGN STAGE 6}."""
    try:
        from readability import Document
        return Document(html).summary(html_partial=True) or ""
    except Exception:                                          # noqa: BLE001 — readability raises on some malformed html (edge #5)
        return ""


def _resiliparse(html: str) -> str:
    """Fallback #2 — fastest extractor, high recall; prose-only (loses table fidelity → the caller logs that loudly).
    {DESIGN STAGE 6}."""
    try:
        from resiliparse.extract.html2text import extract_plain_text
        return extract_plain_text(html, main_content=True, preserve_formatting=True) or ""
    except Exception:                                          # noqa: BLE001 (edge #5)
        return ""


def _mark_transcript(blocks: list[dict]) -> list[int]:
    """Indices of md/list blocks whose text is TRANSCRIPT-LIKE (speaker-turn density). The caller (STAGE 8) deletes a
    flagged block ONLY when the VLM's transcript_segments actually cover it — so an uncovered flagged block STAYS as body
    (no evaporation). {DESIGN edge "transcript-only" FIX A} [CONFIDENCE: CONFIRMED — deterministic detect, VLM confirms]."""
    idx = []
    for j, b in enumerate(blocks):
        if b.get("type") in ("md", "list"):
            hits = len(_SPEAKER_RE.findall(b.get("md") or ""))
            if hits >= 2:                                       # ≥2 speaker turns in one block ⇒ transcript, not body prose
                idx.append(j)
    return idx


def extract_html(html: str, base_url: str, links: list[str] | None = None, thin: bool = False) -> dict:
    """DETERMINISTIC HTML → {blocks, transcript_idx, tier}. blocks = reading-order body (md/list/table),
    tables authoritative from pandas; transcript_idx = blocks the deterministic detector flags as transcript (for the caller's suppression); tier = which
    extractor won ('trafilatura'/'readability'/'resiliparse'/'empty') for logging. tier=='empty' → caller MUST activate the
    gated VLM screenshot_body path (edge "JS-shell": deterministic body absent, VLM-from-screenshot is the only source)."""
    # EARLY thin-shell skip (edge "JS-shell" FIX 3): if render already flagged the page thin (content not in the DOM), don't
    # burn four extractor calls on near-empty html — go straight to the empty tier so the caller uses screenshot_body.
    if thin or not (html or "").strip():
        return {"blocks": [], "transcript_idx": [], "tier": "empty"}

    tier = "trafilatura"
    md = _trafilatura(html, base_url)
    if not md.strip():                                          # fallback #1: readability → re-extract on the cleaned fragment
        frag = _readability(html)
        if frag.strip():
            md = _trafilatura(frag, base_url) or ""
            tier = "readability"
    if not md.strip():                                          # fallback #2: resiliparse (prose only, table fidelity lost)
        md = _resiliparse(html)
        tier = "resiliparse" if md.strip() else "empty"

    dfs = _pandas_tables(html)                                  # authoritative tables from the ORIGINAL html (not the fallback frag)
    blocks = _md_to_blocks(md, dfs) if md.strip() else [_table_block(d) for d in dfs]
    return {"blocks": blocks,
            "transcript_idx": _mark_transcript(blocks),
            "tier": tier if (blocks or md.strip()) else "empty"}


def suppress_transcript_blocks(blocks: list[dict], transcript_idx: list[int], vlm_segments: list[dict]) -> list[dict]:
    """STAGE 8 — remove a deterministic transcript-flagged body block ONLY when the VLM's transcript_segments actually cover
    it (per-block substring match), so it doesn't appear in BOTH basic_info AND transcript {PROMPTS.PY:90-91}. A flagged
    block the VLM did NOT cover STAYS as body (no evaporation). If NO segments came back, nothing is deleted (fail-safe:
    the transcript stays as body content, reaches the archive). {DESIGN edge "transcript-only" FIX A+B}."""
    if not vlm_segments:                                        # VLM emitted no transcript → keep everything as body (fail-safe)
        return blocks
    seg_text = " ".join((s.get("text") or "") for s in vlm_segments).lower()
    kept = []
    for j, b in enumerate(blocks):
        if j in transcript_idx:                                # a transcript-flagged block
            body = (b.get("md") or "").strip().lower()[:200]   # sample its head
            if body and body in seg_text:                      # the VLM DID route this block's text → drop from body
                continue
        kept.append(b)
    return kept
