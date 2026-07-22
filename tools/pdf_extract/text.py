"""text — PDF bytes → concatenated page text via pypdf, or '' on failure.

用一句话讲完: 拿 PDF bytes → pypdf 逐页 extract_text → 拼起来。三个硬化点缺一不可:①静音 pypdf 日志(坏 xref 的
PDF 会刷几万条 WARNING,同步 stderr 洪水本身就能卡死 drain)②strict=False(容忍坏 xref,不抛不自旋)③页数封顶
(IR 文档正文都在前面,绝不遍历 31791 页)。单页解析失败就跳过那一页,不丢整份。
"""
from __future__ import annotations

import io
import logging
import os

_MAX_PAGES = int(os.environ.get("PDF_TEXT_MAX_PAGES", "60"))       # IR docs' material text is up front; cap so a broken PDF can't spin


def pdf_to_text(data: bytes) -> tuple[str, int]:
    """PDF bytes → (concatenated text, n_pages). ('', 0) on empty / non-PDF / unparseable bytes.

    Returns n_pages too (for provenance) — the number of pages the reader saw, even if some yielded no text.
    """
    if not data or data[:5] != b"%PDF-":                          # empty or not a PDF → nothing to extract
        return "", 0
    try:
        # A malformed PDF with a broken xref makes pypdf log THOUSANDS of "entry N invalid" WARNINGs while it
        # rebuilds the xref — the synchronous stderr flood alone can stall a worker. Silence the logger first.
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)        # lenient: tolerate a broken xref instead of raising/spinning
        pages = reader.pages
        n_pages = len(pages)
        out = []
        for page in pages[:_MAX_PAGES]:                           # cap: never iterate a 31791-page broken doc
            try:
                out.append(page.extract_text() or "")
            except Exception:                                     # noqa: BLE001 — one bad page must not lose the whole doc
                continue
        return "\n\n".join(out).strip(), n_pages
    except Exception:                                             # noqa: BLE001 — unparseable bytes → no text
        return "", 0
