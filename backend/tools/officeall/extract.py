"""extract — the Docling engine: office-doc bytes → clean markdown + structured tables + lossless dict.

用一句话讲完: 拿文档 bytes + 格式 → 用 Docling 的 DocumentConverter 转换 → 导出 ①markdown(干净全文,表格内联)
②每个表的 {columns, rows}(TableFormer 出的干净结构,列/数字对齐)③lossless dict(完整层级)。converter 很重
(加载 layout + TableFormer 模型),单例缓存只建一次;有 GPU(H100)自动用,Mac 上退 CPU。缺 docling → 返回 None
让上层优雅降级。

WHY Docling (not pdfplumber/pypdf/python-pptx): one engine, all formats (pdf/pptx/xlsx/docx/html), 97.9% table
accuracy on financial docs, built-in OCR for scanned pages — replaces the whole per-format zoo with clean structured
JSON. {benchmark 2026-07: pdfplumber garbled the Wells Fargo table into fragmented columns; Docling got clean rows}.
"""
from __future__ import annotations

import io
import os
import re
import sys
import threading


def _loud(msg: str) -> None:
    """Emit a failure/fallback/quality line to stderr — the 'fail loudly' channel. Docling failing or returning
    empty content on a real document is a QUALITY signal that must be VISIBLE, never swallowed."""
    print(f"[officeall.extract] {msg}", file=sys.stderr, flush=True)


def _pypdf_text(data: bytes) -> str:
    """FALLBACK text extractor for a PDF when Docling failed or returned nothing — pypdf, lenient, capped. '' if
    pypdf is absent or the bytes won't parse (that '' is then a LOUD end-of-chain failure upstream, not silent)."""
    if not data or data[:5] != b"%PDF-":
        return ""
    try:
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)       # silence the broken-xref WARNING flood
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)
        return "\n\n".join((p.extract_text() or "") for p in reader.pages[:60]).strip()
    except Exception as e:                                        # noqa: BLE001
        _loud(f"pypdf fallback FAILED ({type(e).__name__}: {e})")
        return ""

# A Docling markdown table = one or more consecutive lines starting with '|'. We replace each such block with a
# [TABLE n] placeholder so `markdown` holds ONLY prose + headings (structure preserved), and the table DATA lives
# ONLY in the structured `tables` JSON — the two are cleanly separated (option B).
_MD_TABLE_BLOCK = re.compile(r"(?:^[ \t]*\|.*\n?)+", re.M)

_converter = None                                                # heavy singleton, OCR OFF (loads layout + TableFormer)
_converter_ocr = None                                            # second singleton, OCR ON — only built if a doc needs it
_lock = threading.Lock()
# TableFormer ACCURATE, and the FAST alternative is REJECTED on evidence, not left as an untried option.
#
# FAST is 1.6x quicker and reports IDENTICAL surface metrics — same character count, same table count — so a check
# that looked only at n_tables and len(markdown) would have called it free. Comparing the actual cells shows what it
# costs, on a real China Telecom results pdf:
#   ACCURATE  "Operating Revenues | 375,734 | 393,561 | 4.7%"
#   FAST      "Operating Revenues 375,734 | Operating Revenues 375,734 | 393,561 | 4.7%"
# FAST merged the line item into the same cell as its figure, duplicated it, shifted the row, and dropped two detail
# lines entirely ("of which: Mobile Service Revenues", "Wireline Service Revenues"). Row-text similarity 76.5%.
# {MEASURED 2026-08-05 — same document both modes: ACCURATE 91.9s / FAST 56.4s, markdown byte-identical,
#  table 1 rows 76.5% similar, table 2 identical}
# [CONFIDENCE: CONFIRMED — cell-level diff of both outputs].
#
# The numbers in these tables ARE the product. 1.6x for corrupted figures is not a trade, and the per-document cost
# is not where the throughput lever is anyway: TableFormer is serial WITHIN a document, so the box is used by running
# more documents at once, not by making each one cheaper. docling was measured at 3.7 of 96 cores while doing this.
_ACCURATE = os.environ.get("OFFICE_TABLE_ACCURATE", "1") == "1"   # TableFormer ACCURATE mode — best for financial tables
# OCR is OFF on the fast path and used only as a FALLBACK for a document the text path could not read.
# WHY: Docling's PdfPipelineOptions defaults do_ocr=True, so every page of every pdf went through RapidOCR — and IR
# pdfs are overwhelmingly text-native, so it found nothing. The log during the first dataset smoke was a wall of
# "[RapidOCR] The text detection result is empty" / "RapidOCR returned empty result!" while the process sat at 382%
# CPU. That is the entire cost of OCR paid for zero content on the common case.
# Scanned pdfs are real but rare, and they are already DETECTABLE: an empty text extraction is exactly the signal
# {EXTRACT.PY "pypdf fallback also EMPTY — likely image-only/scanned pdf (needs OCR)"}. So: try text-only, and if the
# document comes back empty, retry ONCE with OCR. Fast on the 95% case, correct on the 5%.
# {MEASURED 2026-08-03 media_dispatch_run smoke — RapidOCR invoked on every page, empty result every time}
# [CONFIDENCE: CONFIRMED — the warnings name the exact call and its empty return].
_OCR_FALLBACK = os.environ.get("OFFICE_OCR_FALLBACK", "1") == "1"   # set 0 to disable the scanned-pdf retry entirely


# Threads PER DOCUMENT. This and the service's concurrency are one dial with two halves: their PRODUCT must land near
# the core count, or the jobs spend their time context-switching instead of converting.
#
# We were never passing AcceleratorOptions at all, so docling's own default (num_threads=4) never took effect and torch
# used ITS default instead — 48 on the pod. At the docling service's concurrency of 24 that is 1,152 threads contending
# for 96 cores: a 12x oversubscription, paid on every document.
# {POD 2026-08-04, asked inside the service venv — "docling AcceleratorOptions 默认: num_threads=4"
#  vs "torch.get_num_threads() = 48"; nproc = 96; docling process held 233 threads while completely idle}
# [CONFIDENCE: CONFIRMED — both numbers read from the pod's own python, not from docs].
#
# 4 × 24 = 96 exactly, which is also docling's own default — that pairing is the intended design, not a coincidence.
_NUM_THREADS = int(os.environ.get("OFFICE_NUM_THREADS", "4"))


def _build(do_ocr: bool):
    """One Docling converter with OCR on or off. Kept separate because the pipeline options are baked in at
    construction — you cannot flip do_ocr per document on a built converter."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (AcceleratorDevice, AcceleratorOptions,
                                                    PdfPipelineOptions, TableFormerMode)
    opts = PdfPipelineOptions()
    opts.do_table_structure = True                                # tables are the point — always on
    opts.do_ocr = do_ocr
    opts.table_structure_options.mode = TableFormerMode.ACCURATE if _ACCURATE else TableFormerMode.FAST
    # device=CPU is STATED, not left on AUTO. The pod's A40 has ~3 GB free of 46 GB — vLLM owns 40.3 and whisper the
    # rest — so an AUTO that resolved to CUDA would OOM mid-document. Saying CPU makes the placement a decision.
    # {NVIDIA-SMI 2026-08-04 — 2985 MiB FREE OF 46068}
    opts.accelerator_options = AcceleratorOptions(num_threads=_NUM_THREADS, device=AcceleratorDevice.CPU)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def _get_converter(ocr: bool = False):
    """The cached Docling converter. `ocr=False` (default) is the fast text-only path; `ocr=True` builds — lazily, on
    first real need — the scanned-pdf fallback. None if docling is absent so the caller degrades instead of crashing."""
    global _converter, _converter_ocr
    cached = _converter_ocr if ocr else _converter
    if cached is not None:
        return cached
    with _lock:
        cached = _converter_ocr if ocr else _converter
        if cached is not None:
            return cached
        try:
            c = _build(ocr)
            if ocr:
                _converter_ocr = c
            else:
                _converter = c
            print(f"[officeall] docling converter ready (ocr={'ON' if ocr else 'OFF'}, "
                  f"table_mode={'ACCURATE' if _ACCURATE else 'FAST'})", flush=True)
            return c
        except Exception as e:                                    # noqa: BLE001 — docling absent / import error → None
            print(f"[officeall] docling unavailable ({type(e).__name__}: {e})", flush=True)
            if ocr:
                _converter_ocr = None
            else:
                _converter = None
    return None


def _prose_markdown(full_md: str) -> str:
    """Replace every markdown table block (|...| lines) with a numbered [TABLE n] placeholder, in reading order, so
    the returned markdown is PROSE + HEADINGS only. The n matches the position in the `tables` JSON array (Docling
    emits tables in the same reading order in both the markdown and doc.tables). Structure preserved, table DATA moved
    out to JSON (option B)."""
    counter = [0]

    def _repl(_m):
        counter[0] += 1
        return f"[TABLE {counter[0]}]"

    md = _MD_TABLE_BLOCK.sub(_repl, full_md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()                  # collapse the blank runs the removal leaves behind


def _table_to_dict(tbl, doc) -> dict:
    """One Docling table → {columns, rows} of clean strings. Uses export_to_dataframe (TableFormer's structured grid)."""
    try:
        df = tbl.export_to_dataframe(doc)
        columns = [str(c) for c in df.columns.tolist()]
        rows = [[("" if v is None else str(v)) for v in r] for r in df.values.tolist()]
        return {"columns": columns, "rows": rows}
    except Exception:                                            # noqa: BLE001 — one odd table must not sink the doc
        return {}


def _sheet_title(rows: list[list[str]]) -> str:
    """The sheet's own title — its first cell that reads like a heading — or '' when it has none.

    WHY: see the call site. A tab named '1(J)' is a page number; the statement's name is inside the sheet. Only the
    first two rows are considered, because a match further down is a data label, not a title, and only a cell with no
    digit-dominant content qualifies, because a figure is never a heading. `2018-05-11 00:00:00` on a cover sheet is
    the case that rules out a plain 'first non-empty cell' — it is first, and it is not the title."""
    for r in rows[:2]:                                       # a title lives on the first line or two, never deeper
        for c in r:
            s = (c or "").strip()
            if len(s) < 4 or len(s) > 60:
                continue
            digits = sum(ch.isdigit() for ch in s)
            if digits * 2 >= len(s):                         # mostly digits → a date or a figure, not a heading
                continue
            return s.replace("\n", " ")
    return ""


def _xls_tables(data: bytes) -> dict | None:
    """FALLBACK for legacy .xls when Docling refuses it. None when this is not an OLE2 file or the read fails.

    WHY it is needed at all: .xls and .xlsx share a name and nothing else — .xlsx is zip+XML, .xls is the OLE2
    compound-document format from 1997, and Docling's MsExcelDocumentBackend only speaks the former. The router
    classifies both as kind='xlsx', so every legacy file went to a parser that cannot read it AND had no fallback
    behind it, unlike the pdf path:
      {POD 2026-08-05 — b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' -> "MsExcelDocumentBackend could not ...",
       via='none', 0 chars, 0 tables, warnings=['docling-text-crash:ConversionError', 'no-fallback-xlsx']}
      {DB 2026-08-05 event_media_urls — kind='xlsx': 23 failed, 1 done. The one that worked was a real .xlsx}
    [CONFIDENCE: CONFIRMED — magic bytes, the backend's own error, and the ledger all agree].

    These are not a rare edge: they are SEC EDGAR's financial-report exhibits, served from
    d18rn0p25nwr6d.cloudfront.net, and every one of them is a table of numbers — exactly the content this pipeline
    exists to capture.

    pandas reads them through xlrd. Output is shaped like Docling's so the caller cannot tell which produced it."""
    if not data or data[:4] != b"\xd0\xcf\x11\xe0":     # OLE2 magic — not a legacy xls, nothing to do here
        return None
    try:
        import io
        import pandas as pd
        # header=None — NEVER let pandas promote a row to column names. An IR workbook is a LAYOUT, not a data frame:
        # its first row is a title, a logo row, or blank, so header=0 both DESTROYED a real row and manufactured column
        # names out of it. Where the row was blank the names came back as the placeholder pandas invents for unnamed
        # positions, which then travelled all the way to the dashboard as if they were the sheet's own headers.
        # {LIVE 2026-08-10 group.ntt H3003hosoku0511.xls sheet '1(J)' with header=0 → columns
        #  ['Unnamed: 0','Unnamed: 1','Unnamed: 2','Unnamed: 3','Unnamed: 4','Unnamed: 5'], and the row it consumed was
        #  the sheet's own title '1.連結サマリー（NTT連結業績概要）'}
        # [CONFIDENCE: CONFIRMED 100% — same file read both ways, side by side.]
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
    except Exception as e:                                # noqa: BLE001 — xlrd absent / corrupt file → LOUD, no crash
        _loud(f"legacy .xls fallback FAILED ({type(e).__name__}: {str(e)[:110]})")
        return None
    tables, parts = [], []
    for name, df in (sheets or {}).items():
        if df is None or df.empty:
            continue
        # TRIM THE CANVAS. These sheets are laid out for printing, so the used range is padded with wholly-empty rows
        # and columns — spacer gutters between figure groups, margins around the block. They carry no information and
        # they dominate the grid, which is what turned a readable statement into a wall of blanks on the page.
        # {LIVE 2026-08-10 same workbook — '表紙(J)' 22x12 → 11x4, '1(J)' 56x21 → 52x16, '2(J)' 53x21 → 47x16}
        # [CONFIDENCE: CONFIRMED 100% — shapes measured before and after on all 11 sheets.]
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            continue
        rows = [[("" if pd.isna(v) else str(v).strip()) for v in r] for r in df.values.tolist()]
        # No column names at all, rather than invented ones. With header=None there is no header row to name, and the
        # sheet's real header (`4月～6月（第1四半期）` …) is simply one of the rows — which is the truth of the file.
        tables.append({"columns": [], "rows": rows})
        # THE TAB NAME IS NOT ALWAYS THE TITLE. It is for EDGAR exhibits, which label the statement on the tab; it is
        # not for a Japanese supplementary-data workbook, whose tabs are page numbers — '表紙(J)', '1(J)', '2(J)' — while
        # the statement's actual name sits in the sheet's first text cell. Heading every table `## 1(J)` told a reader
        # nothing about which statement they were looking at.
        # {LIVE 2026-08-10 group.ntt H3003hosoku0511.xls — tabs '1(J)' / '2(J)' vs first cells
        #  '1.連結サマリー（NTT連結業績概要）' / '1.連結サマリー（設備投資）'}
        # [CONFIDENCE: CONFIRMED 100% — both read from the same workbook.]
        # Falls back to the tab name, so the EDGAR case keeps the behaviour that was right for it.
        parts.append(f"## {_sheet_title(rows) or name}\n\n[TABLE {len(tables)}]")
    if not tables:
        _loud("legacy .xls read but every sheet was empty")
        return None
    return {"markdown": "\n\n".join(parts), "tables": tables, "n_pages": len(tables),
            "n_tables": len(tables), "via": "xlrd-fallback", "warnings": ["docling-cannot-read-legacy-xls"]}


def docling_extract(data: bytes, fmt: str, want_structured: bool = False) -> dict:
    """Office-doc bytes → {markdown, tables, n_pages, n_tables, via, warnings, structured?}. FALLBACK CHAIN with
    LOUD failures: Docling (primary) → pypdf (for a PDF when Docling is absent, crashes, or returns EMPTY content on
    a real doc = a quality gate). `via` names what actually produced the text ('docling' | 'pypdf-fallback' |
    'none'); `warnings` collects the loud reasons. Never raises. A truly empty result comes back with a loud
    warning explaining WHY (not a silent '')."""
    warnings: list[str] = []

    def _try(conv, tag: str):
        """One Docling pass → the out dict, or None when it produced nothing usable (so the caller escalates).
        `tag` names which converter ran, so `via` tells you afterwards whether OCR was needed."""
        if conv is None:
            return None
        try:
            from docling.datamodel.base_models import DocumentStream
            stream = DocumentStream(name=f"document.{fmt or 'pdf'}", stream=io.BytesIO(data))
            doc = conv.convert(stream).document
            markdown = _prose_markdown(doc.export_to_markdown() or "")   # prose + [TABLE n]; data lives in `tables`
            tables = [t for t in (_table_to_dict(tbl, doc) for tbl in doc.tables) if t]
            try:
                n_pages = doc.num_pages()
            except Exception:                                    # noqa: BLE001 — non-paginated (xlsx/html)
                n_pages = len(getattr(doc, "pages", []) or [])
            # QUALITY GATE: a real doc (has pages) that yields NO text AND NO tables is a quality failure, not a
            # success-with-empty — return None so the caller escalates (OCR, then pypdf) and say so LOUDLY.
            if not markdown.strip() and not tables and n_pages > 0 and fmt == "pdf":
                _loud(f"QUALITY: docling[{tag}] returned 0 text + 0 tables on a {n_pages}-page pdf → escalate")
                warnings.append(f"docling-{tag}-empty-on-{n_pages}p")
                return None
            out = {"markdown": markdown, "tables": tables, "n_pages": n_pages,
                   "n_tables": len(tables), "via": f"docling:{tag}", "warnings": warnings}
            if want_structured:
                try:
                    out["structured"] = doc.export_to_dict()
                except Exception:                                # noqa: BLE001
                    out["structured"] = {}
            return out
        except Exception as e:                                   # noqa: BLE001 — docling crashed → LOUD + escalate
            _loud(f"docling[{tag}] convert FAILED ({type(e).__name__}: {str(e)[:100]}) → escalate")
            warnings.append(f"docling-{tag}-crash:{type(e).__name__}")
            return None

    # PRIMARY — Docling with OCR OFF. IR pdfs are overwhelmingly text-native, and OCR on a text-native page costs the
    # full OCR price for zero content {MEASURED 2026-08-03: a wall of "RapidOCR returned empty result!" at 382% CPU}.
    out = _try(_get_converter(ocr=False), "text")
    if out is not None:
        return out

    # ESCALATION 1 — the text path found nothing on a real pdf, which is exactly the scanned-document signal. Retry
    # ONCE with OCR. Rare by construction, so the expensive converter is also built lazily, only when first needed.
    if out is None and fmt == "pdf" and _OCR_FALLBACK:
        _loud("text-only docling was empty → retrying WITH OCR (image-only/scanned pdf)")
        out = _try(_get_converter(ocr=True), "ocr")
        if out is not None:
            return out

    # ESCALATION 1b — legacy .xls. Docling only reads OOXML, so an OLE2 workbook reaches here with nothing extracted
    # and, before this, no fallback at all. Checked by MAGIC rather than by the url's extension because the router
    # already collapsed .xls and .xlsx into one kind and the bytes are the only reliable discriminator.
    if fmt in ("xlsx", "xls"):
        xls = _xls_tables(data)
        if xls is not None:
            xls["warnings"] = warnings + xls["warnings"]
            return xls

    if _get_converter(ocr=False) is None:
        _loud("docling unavailable → pypdf fallback (pdf only)")
        warnings.append("docling-unavailable")

    # FALLBACK — pypdf (PDF text only). For non-pdf with no docling there is no fallback → loud empty.
    if fmt == "pdf":
        text = _pypdf_text(data)
        if text:
            return {"markdown": text, "tables": [], "n_pages": 0, "n_tables": 0,
                    "via": "pypdf-fallback", "warnings": warnings}
        _loud("pypdf fallback also EMPTY — likely image-only/scanned pdf (needs OCR)")
        warnings.append("pypdf-empty-likely-scanned")
    else:
        _loud(f"no fallback for fmt={fmt!r} without docling")
        warnings.append(f"no-fallback-{fmt}")
    return {"markdown": "", "tables": [], "n_pages": 0, "n_tables": 0, "via": "none", "warnings": warnings}
