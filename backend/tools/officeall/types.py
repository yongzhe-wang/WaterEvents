"""DocResult — the ONE unified return shape for every office document (pdf / pptx / xlsx / docx / html).

用一句话讲完: 不管进来的是 PDF 还是 PPTX 还是 XLSX,officeall 都用 Docling 抽成同一个 DocResult —— markdown 是
干净的全文(表格已内联),tables 是结构化的 [{columns, rows}],structured 是 Docling 的 lossless dict(要更细
的层级/坐标时用)。一个形状,所有格式通吃。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DocResult:
    """The verdict for ONE office document. `ok` = we extracted real content (markdown text OR at least one table).

    format     — 'pdf' | 'pptx' | 'xlsx' | 'docx' | 'html' (the detected/parsed format), '' on failure.
    text       — Docling markdown export: clean full text with tables rendered inline (the human-readable view).
    tables     — [{columns, rows}] structured financial tables (Docling TableFormer — clean columns/numbers).
    structured — Docling's lossless document dict (export_to_dict) for callers that need the full hierarchy; {} if unused.
    n_pages    — page count (0 on failure / not paginated).
    n_tables   — number of tables found.
    n_bytes    — size of the source bytes.
    source     — 'url' (fetched) | 'bytes' (caller-supplied) | '' (failed).
    error      — a short reason when ok=False; '' on success.
    """
    format: str = ""
    text: str = ""
    tables: list[dict] = field(default_factory=list)
    structured: dict = field(default_factory=dict)
    n_pages: int = 0
    n_tables: int = 0
    n_bytes: int = 0
    source: str = ""
    via: str = ""                                                  # what produced `text`: 'docling' | 'pypdf-fallback' | 'none'
    warnings: list[str] = field(default_factory=list)             # LOUD quality/fallback flags (docling-empty, scanned, …)
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the document yielded real content — markdown text OR at least one table."""
        return bool(self.text.strip() or self.tables)
