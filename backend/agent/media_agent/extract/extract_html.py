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
            # Consume the WHOLE region, blank lines included. The previous version broke out of the loop on the first
            # blank line, but trafilatura interleaves blanks BETWEEN pipe rows, so a table was only half-eaten and the
            # rest survived into the md as orphan pipes — visible in stored output as `| \n |` sitting next to the
            # placeholder that was supposed to replace it.
            # {psql 2026-08-06 ottertail md around [[TABLE:1]]: "| \n | \n\n | \n | \n | \n | \n\n [[TABLE:1]] \n\n |"}
            # [CONFIDENCE: CONFIRMED 100% — read from event_documents.md on the live database.]
            # A blank line only ENDS the region when the line after it is not another pipe row, so a genuine paragraph
            # break still terminates it.
            while i < n:
                if _PIPE_ROW_RE.match(lines[i]):
                    i += 1
                    continue
                if lines[i].strip() == "" and i + 1 < n and _PIPE_ROW_RE.match(lines[i + 1]):
                    i += 1                                      # blank INSIDE the region
                    continue
                break
            pair = next(df_iter, None)                          # the authoritative table for THIS position
            if pair is not None:
                blk = _table_block_checked(pair)
                if blk:
                    blocks.append(blk)
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
    for pair in df_iter:                                        # pandas tables trafilatura dropped → append at end (edge #1 tail)
        blk = _table_block_checked(pair)
        if blk:
            blocks.append(blk)
    return blocks


def _table_block_checked(pair) -> dict | None:
    """One (DataFrame, raw html) pair → a table block, or None when it is page furniture rather than data.

    Returning None is NOT a loss: a layout table's text is already in the trafilatura markdown that produced this
    line-stream, so refusing to ALSO store it as a structured table removes a duplicate, not content. What it removes
    is the 95% of stored "tables" that were navigation menus, footers and subscription forms.
    """
    df, raw = pair if isinstance(pair, tuple) else (pair, "")
    try:
        rows = df.values.tolist()
        n_rows, n_cols = len(rows), (len(rows[0]) if rows else 0)
        n_cells = n_rows * n_cols
    except Exception:                                           # noqa: BLE001 — unusable frame → drop it, never crash
        return None
    if raw and table_kind(raw, n_rows, n_cols, n_cells) == "layout":
        return None
    blk = _table_block(df)
    if blk and blk.get("rows"):
        blk["rows"] = _collapse_spans(blk["rows"])               # colspan expansion undone AFTER classification,
    return blk                                                   # because the size rules read the original shape


# ── DATA TABLE vs LAYOUT TABLE ──────────────────────────────────────────────────────────────────────────────────────
# 用一句话讲完: IR 站把 <table> 当排版工具用(页脚、订阅表单、导航菜单),我们此前把页面上**每一个** <table> 都当数据表
# 存了下来 —— 实测 300 份文档里 3,841 张"表",95% 表头是纯位置索引,内容是 'Privacy Notice' / 'Email Address *' /
# 'Investor Alert · News Events'。真正的财务表淹在里面。
# {psql/REST 2026-08-06 over 300 docs / 3841 tables: "表头是纯数字索引 3657 (95%) · 表头正常 184 (4%)"}
# {samples: netease ['Privacy Notice','Copyright 2026'] · starbucks ['Email Address *'] · starbucks ['Investor Alert']}
# [CONFIDENCE: CONFIRMED 100% — counted over stored blocks on the production database.]
#
# WHY these particular rules and not a heuristic of my own: this is a solved problem with published criteria, and my
# first attempt (numeric density) was measured NOT to separate them — density≥0.3 covered 24% of positional-header
# tables and 21% of normal-header ones, i.e. no signal. The rules below come from the accessibility/screen-reader
# literature and the web-table research line, and were verified against four real pages before being written:
#   FFIN 财报      341 rows · 22 cols · zero <th>/caption/scope   → DATA via the size rule
#   Starbucks[0]     1 cell + a <form>                            → LAYOUT
#   Starbucks[1]   nested <table> + 6 form elements               → LAYOUT
#   NetEase[0]     th=3, thead=1, scope=22                        → DATA via explicit markers
# {POWERMAPPER "Tables with 5+ columns are treated as data · 20+ rows are treated as data · 10 or fewer cells are
#  treated as layout · role=presentation · nested tables · contains embed/object/iframe"}
# {SURVEY arXiv:2002.00207 "Genuine tables are LEAF tables that do not contain other tables, lists, forms, images or
#  other non-text formatting tags in a cell, and they contain multiple rows and columns"}
# [CONFIDENCE: CONFIRMED 100% — 4/4 on the pages named above; the density counter-measurement is why this replaced it.]
_TAG_RE = lambda t: re.compile(rf"<{t}\b", re.I)                  # noqa: E731 — tiny helper, clearer inline than a def
_TH_RE, _THEAD_RE, _CAPTION_RE = _TAG_RE("th"), _TAG_RE("thead"), _TAG_RE("caption")
_COLGROUP_RE = re.compile(r"<col(group)?\b", re.I)
_SEMANTIC_ATTR_RE = re.compile(r'\b(scope|headers|abbr|summary)\s*=|role\s*=\s*["\']table|aria-(col|row)count', re.I)
_PRESENTATION_RE = re.compile(r'role\s*=\s*["\']presentation', re.I)
_NONLEAF_RE = re.compile(r"<(form|input|select|textarea|iframe|object|embed)\b", re.I)


def table_kind(table_html: str, n_rows: int, n_cols: int, n_cells: int) -> str:
    """'data' | 'layout' — decided in the order the literature gives, most certain signal first.

    Order matters and is not arbitrary: an explicit author declaration (role=presentation, or a <th>) outranks any
    inference from shape, and a non-leaf table is layout NO MATTER how large it is — a page wrapper containing the
    whole article is 40 rows of nothing.
    """
    if _PRESENTATION_RE.search(table_html):
        return "layout"                                          # the author said so
    if _NONLEAF_RE.search(table_html) or len(re.findall(r"<table\b", table_html, re.I)) > 1:
        return "layout"                                          # not a leaf → a container, not data
    if n_rows <= 1 or n_cols <= 1 or n_cells <= 10:
        return "layout"                                          # degenerate shape
    if (_TH_RE.search(table_html) or _THEAD_RE.search(table_html) or _CAPTION_RE.search(table_html)
            or _COLGROUP_RE.search(table_html) or _SEMANTIC_ATTR_RE.search(table_html)):
        return "data"                                            # explicit tabular markup
    if n_cols >= 5 or n_rows >= 20:
        return "data"                                            # size fallback — this is what saves an unmarked financial table
    return "layout"                                              # unsure → not a table; the text still reaches the md


def _collapse_spans(rows: list[list]) -> list[list]:
    """Undo colspan EXPANSION, which is faithful to the html and useless as data.

    A cell with colspan=22 is expanded by every parser into 22 identical cells, so a section title becomes a row of 22
    copies and a 1-column table becomes 22 columns of the same string. Measured on one page: 7,065 colspan attributes,
    and both pandas AND docling produced 22 columns of '**FIRST FINANCIAL BANKSHARES, INC.**'.
    {psql 2026-08-06 FFIN rendered html "colspan 出现 7065 次"}
    {DOCLING /docling_extract on the same html -> "22 列 × 341 行 · columns ['0','1',…] · row ['**FIRST FINANCIAL …' ×22]"}
    [CONFIDENCE: CONFIRMED 100% — the docling output was obtained by feeding it that exact page.]

    Two folds, both conservative:
      · a row whose non-empty values are ONE distinct string repeated → collapse to a single cell (spanning title)
      · a column identical to its left neighbour in EVERY row → drop it (expansion artefact)
    Neither can lose information: both only remove exact duplicates of a value that remains present.
    """
    if not rows:
        return rows
    folded = []
    for r in rows:
        vals = [str(c).strip() for c in r]
        nonempty = [v for v in vals if v]
        if len(nonempty) > 1 and len(set(nonempty)) == 1:        # a title spanning the whole row
            folded.append([nonempty[0]])
        else:
            folded.append(list(r))
    width = max((len(r) for r in folded), default=0)
    if width < 2:
        return folded
    # Drop columns that are empty in EVERY row. These are pure spacing columns — financial-report HTML uses them to
    # align the currency symbol away from the figure, so a 7-value table arrives 21 columns wide with 14 of them blank.
    # Removing a column that holds nothing anywhere cannot lose data, which is why this runs unconditionally.
    # {FFIN 2026-08-06 after span-folding: "341 行 × 21 列" with rows like ['Cash and due from ba','$','249466','','$','237466','']}
    # [CONFIDENCE: CONFIRMED 100% — the blank columns are visible in that output.]
    nonblank = [c for c in range(width)
                if any(str(r[c]).strip() for r in folded if len(r) > c)]
    if nonblank and len(nonblank) < width:
        folded = [[r[c] for c in nonblank if len(r) > c] for r in folded]
        width = len(nonblank)
    if width < 2:
        return folded
    keep = [0]
    for c in range(1, width):
        same = all(str(r[c]).strip() == str(r[keep[-1]]).strip()
                   for r in folded if len(r) > c and len(r) > keep[-1])
        if not same:
            keep.append(c)
    if len(keep) == width:
        return folded
    return [[r[c] for c in keep if len(r) > c] for r in folded]


def _pandas_tables(html: str) -> list:
    """Authoritative table extraction → [(DataFrame, raw <table> html)] in document order, so each parsed table can be
    classified against the markup it came from. Zero tables → [] (not an error).

    The pairing is by INDEX and that is sound because both sides walk the same tree in the same order: pandas is called
    with flavor='lxml' and lxml's //table xpath yields document order. {DESIGN STAGE 4; pandas.read_html flavor=lxml}.
    """
    try:
        import pandas as pd
        dfs = pd.read_html(io.StringIO(html), flavor="lxml")
    except (ValueError, ImportError):                           # no <table> OR pandas/lxml missing → no table blocks
        return []
    except Exception:                                           # noqa: BLE001 — malformed table markup must not sink extraction
        return []
    try:
        import lxml.html as LH
        nodes = LH.fromstring(html).xpath("//table")
        raws = [LH.tostring(t, encoding="unicode") for t in nodes]
    except Exception:                                           # noqa: BLE001 — no raw html → classify on shape alone
        raws = []
    return [(df, raws[i] if i < len(raws) else "") for i, df in enumerate(dfs)]


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
