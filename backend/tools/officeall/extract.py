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

_converter = None                                                # heavy singleton (loads layout + TableFormer models once)
_lock = threading.Lock()
_ACCURATE = os.environ.get("OFFICE_TABLE_ACCURATE", "1") == "1"   # TableFormer ACCURATE mode — best for financial tables


def _get_converter():
    """Build the Docling DocumentConverter ONCE (thread-safe) and cache it. None if docling is absent so the caller
    degrades instead of crashing. ACCURATE table mode is the default (financial tables); flip OFFICE_TABLE_ACCURATE=0
    for the faster FAST mode."""
    global _converter
    if _converter is not None:
        return _converter
    with _lock:
        if _converter is not None:
            return _converter
        try:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
            opts = PdfPipelineOptions()
            opts.do_table_structure = True
            opts.table_structure_options.mode = TableFormerMode.ACCURATE if _ACCURATE else TableFormerMode.FAST
            _converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
            print(f"[officeall] docling converter ready (table_mode={'ACCURATE' if _ACCURATE else 'FAST'})", flush=True)
        except Exception as e:                                    # noqa: BLE001 — docling absent / import error → None
            print(f"[officeall] docling unavailable ({type(e).__name__}: {e})", flush=True)
            _converter = None
    return _converter


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


def docling_extract(data: bytes, fmt: str, want_structured: bool = False) -> dict:
    """Office-doc bytes → {markdown, tables, n_pages, n_tables, via, warnings, structured?}. FALLBACK CHAIN with
    LOUD failures: Docling (primary) → pypdf (for a PDF when Docling is absent, crashes, or returns EMPTY content on
    a real doc = a quality gate). `via` names what actually produced the text ('docling' | 'pypdf-fallback' |
    'none'); `warnings` collects the loud reasons. Never raises. A truly empty result comes back with a loud
    warning explaining WHY (not a silent '')."""
    warnings: list[str] = []
    conv = _get_converter()

    # PRIMARY — Docling.
    if conv is not None:
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
            # success-with-empty — escalate to the pypdf fallback (for pdf) and say so LOUDLY.
            if not markdown.strip() and not tables and n_pages > 0 and fmt == "pdf":
                _loud(f"QUALITY: docling returned 0 text + 0 tables on a {n_pages}-page pdf → pypdf fallback")
                warnings.append(f"docling-empty-on-{n_pages}p")
            else:
                out = {"markdown": markdown, "tables": tables, "n_pages": n_pages,
                       "n_tables": len(tables), "via": "docling", "warnings": warnings}
                if want_structured:
                    try:
                        out["structured"] = doc.export_to_dict()
                    except Exception:                            # noqa: BLE001
                        out["structured"] = {}
                return out
        except Exception as e:                                   # noqa: BLE001 — docling crashed → LOUD + fall back
            _loud(f"docling convert FAILED ({type(e).__name__}: {str(e)[:100]}) → pypdf fallback")
            warnings.append(f"docling-crash:{type(e).__name__}")
    else:
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
