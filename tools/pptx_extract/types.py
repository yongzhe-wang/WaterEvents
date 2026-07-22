"""PptxResult — the single return shape for the whole pptx_extract tool (mirrors PdfResult / AudioResult).

用一句话讲完: 一个 .pptx(URL 或 bytes)进来 → detect → fetch → slides 三步 → 汇成这一个 PptxResult。
text 是所有页拼成的全文;slides 是每页的结构({n, title, text, tables, notes}),方便按页引用/对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PptxResult:
    """The verdict for ONE deck. `ok` is the single truth: True iff we extracted real slide text.

    text     — all slides' text concatenated (title + body per slide), '' if none.
    slides   — [{n, title, text, tables, notes}] per-slide structure (n = 1-based slide index), [] if none.
    n_slides — number of slides in the deck (0 on failure).
    n_bytes  — size of the fetched .pptx bytes (0 on failure).
    source   — 'url' (fetched) | 'bytes' (caller-supplied) | '' (failed).
    error    — a short reason when ok=False ('not-pptx-url', 'fetch-empty', 'not-a-pptx'); '' on success.
    """
    text: str = ""
    slides: list[dict] = field(default_factory=list)
    n_slides: int = 0
    n_bytes: int = 0
    source: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the deck yielded real slide text."""
        return bool(self.text.strip())
