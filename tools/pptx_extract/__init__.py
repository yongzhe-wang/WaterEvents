"""pptx_extract — one tool: a .pptx url (or bytes) → its per-slide text + tables + notes.

用一句话讲完: WaterEvents 的第三个 tool,和 pdf_extract / audio_extract 同一套 pattern —— detect(是不是 deck)→
fetch(拆 Office 查看器 + curl_cffi 抓 bytes)→ slides(python-pptx 逐页抽文本/表格/备注)→ 汇成统一的 PptxResult。
旧项目其实没真正解析 pptx(只 stdlib 抽 docx),这里用 python-pptx 正经抽,缺依赖时退回 stdlib zip 抽基础文本。

组织(infra,和另两个 tool 对称):
  detect.py   — is_pptx_url / maybe_pptx_url          (含 Office-viewer / aka.ms 识别)
  fetch.py    — fetch_bytes(url, proxy=None) -> bytes  (拆 ?src= 查看器 + Chrome 指纹 + PK zip magic + SSRF)
  slides.py   — pptx_to_slides(bytes) -> (text, slides) (python-pptx: title/body/tables/notes;stdlib fallback)
  types.py    — PptxResult                            (统一返回形状)

依赖(全 lazy,缺了就退化不 break):curl_cffi(fetch)、python-pptx(slides,缺则 stdlib zip 兜底)。proxy 调用方给。
"""
from __future__ import annotations

from .detect import is_pptx_url, maybe_pptx_url
from .fetch import fetch_bytes
from .slides import pptx_to_slides
from .types import PptxResult

__all__ = ["extract", "extract_bytes", "is_pptx_url", "maybe_pptx_url", "fetch_bytes",
           "pptx_to_slides", "PptxResult"]


def extract_bytes(data: bytes) -> PptxResult:
    """.pptx bytes → PptxResult (per-slide text + tables + notes). Use when you already HAVE the bytes (no network)."""
    if not data or data[:4] != b"PK\x03\x04":                     # not even a ZIP → not a pptx
        return PptxResult(source="bytes", error="not-a-pptx")
    text, slides = pptx_to_slides(data)
    res = PptxResult(text=text, slides=slides, n_slides=len(slides), n_bytes=len(data), source="bytes")
    if not res.ok:
        res.error = "no-slide-text"
    return res


def extract(url: str, proxy: str | None = None) -> PptxResult:
    """A (maybe-).pptx url → PptxResult. Flow: maybe_pptx_url gate → fetch_bytes (Office-viewer unwrap, Chrome
    fingerprint, PK-verified) → pptx_to_slides (python-pptx). proxy: pass a residential proxy url when a host
    datacenter-blocks the GET. Best-effort: a non-deck / unreachable url → PptxResult(ok=False) with an `error`."""
    if not maybe_pptx_url(url):
        return PptxResult(source="url", error="not-pptx-url")
    data = fetch_bytes(url, proxy=proxy)
    if not data:
        return PptxResult(source="url", error="fetch-empty")
    res = extract_bytes(data)
    res.source = "url"                                            # the bytes came from the network here
    return res
