"""PdfResult — the single return shape for the whole pdf_extract tool.

用一句话讲完: 一个 PDF(URL 或 bytes)进来 → detect → fetch → text + tables 三步 → 汇成这一个 PdfResult 出去。
每个字段记录一步的产物 + 一点溯源(source/n_pages),这样调用方一眼看清"抓到没、抽到没、从哪抽的"。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PdfResult:
    """The verdict for ONE pdf. `ok` is the single truth: True iff we got a real PDF AND extracted something usable.

    text     — concatenated page text (pypdf), '' if none / not a pdf.
    tables   — list of {columns, rows} financial tables (pdfplumber heuristics), [] if none.
    n_pages  — pages the PDF actually had (0 on failure), for provenance / debugging.
    n_bytes  — size of the fetched PDF bytes (0 on failure).
    source   — where the bytes came from: 'url' (fetched) | 'bytes' (caller-supplied) | '' (failed).
    error    — a short reason string when ok=False (e.g. 'not-a-pdf', 'fetch-empty', 'oversized'); '' on success.
    """
    text: str = ""
    tables: list[dict] = field(default_factory=list)
    n_pages: int = 0
    n_bytes: int = 0
    source: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the extract produced real content — usable text OR at least one table."""
        return bool(self.text.strip() or self.tables)
