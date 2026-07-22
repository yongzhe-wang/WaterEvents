"""pdf_extract — one tool: a PDF url (or bytes) → its text + financial tables.

用一句话讲完: 这是 WaterEvents 的第一个 tool,把旧项目里散在 slides/ 和 basic_info/ 的一堆 PDF 代码整理成
四个单一职责模块 —— detect(是不是 PDF)→ fetch(curl_cffi Chrome 指纹抓 bytes)→ text(pypdf 抽文本)+
tables(pdfplumber 抽财务表)—— 汇成一个 `extract(url)` 入口,返回统一的 PdfResult。

组织(infra):
  detect.py  — is_pdf_url / maybe_pdf_url         (最便宜的第一道门,先判 URL 再动网络)
  fetch.py   — fetch_bytes(url, proxy=None)       (Chrome 指纹 + %PDF magic + SSRF + size cap)
  text.py    — pdf_to_text(bytes) -> (text, npages)(pypdf,静音+lenient+页数封顶)
  tables.py  — pdf_to_tables(bytes) -> [tables]    (pdfplumber 财务表启发式,NFKC + CJK-aware)
  types.py   — PdfResult                          (统一返回形状)

依赖(全 lazy,缺了就 no-op,不 break):curl_cffi(fetch)、pypdf(text)、pdfplumber(tables)。
proxy 由调用方提供(本 tool 不绑定 provider);要走住宅代理时把 providers.watercrawl 的 webshare url 传进来。
"""
from __future__ import annotations

from .detect import is_pdf_url, maybe_pdf_url
from .fetch import fetch_bytes
from .tables import pdf_to_tables
from .text import pdf_to_text
from .types import PdfResult
from .vlm import vlm_ocr

__all__ = ["extract", "extract_bytes", "is_pdf_url", "maybe_pdf_url", "fetch_bytes",
           "pdf_to_text", "pdf_to_tables", "vlm_ocr", "PdfResult"]


def extract_bytes(data: bytes, use_vlm: bool = True) -> PdfResult:
    """PDF bytes → PdfResult (text + tables). Use when you already HAVE the bytes (no network). The single fetched
    body feeds BOTH pypdf (text) and pdfplumber (tables) — one download, both derivations.

    use_vlm — when pypdf yields NO text but the PDF has pages (a scanned/image-only doc), fall back to VLM-OCR
    (Qwen2.5-VL on the H100 via QWEN_VLM_BASE_URL). Off → skip the GPU fallback (pypdf-only). The VLM path is a
    no-op returning '' when its deps/endpoint are absent, so use_vlm=True is always safe."""
    if not data or data[:5] != b"%PDF-":                          # not a PDF → empty result with a reason
        return PdfResult(source="bytes", error="not-a-pdf")
    text, n_pages = pdf_to_text(data)
    text_via = "pypdf" if text.strip() else ""
    if not text.strip() and use_vlm and n_pages > 0:              # image-only PDF (has pages, no text layer) → VLM-OCR
        from .vlm import vlm_ocr
        ocr = vlm_ocr(data)
        if ocr.strip():
            text, text_via = ocr, "vlm"
    tables = pdf_to_tables(data)
    return PdfResult(text=text, tables=tables, n_pages=n_pages, n_bytes=len(data), source="bytes", text_via=text_via)


def extract(url: str, proxy: str | None = None, use_vlm: bool = True) -> PdfResult:
    """A (maybe-)PDF url → PdfResult. Flow: maybe_pdf_url gate → fetch_bytes (Chrome fingerprint, %PDF-verified) →
    text + tables from the SAME bytes, with a VLM-OCR fallback for image-only pages. proxy: pass a residential proxy
    url when a host datacenter-blocks the GET; None = direct. use_vlm: OCR fallback for scanned pdfs (see extract_bytes).
    Best-effort: a non-pdf / unreachable / unparseable url → PdfResult(ok=False) with an `error`."""
    if not maybe_pdf_url(url):                                    # cheapest gate — not even a candidate PDF url
        return PdfResult(source="url", error="not-pdf-url")
    data = fetch_bytes(url, proxy=proxy)
    if not data:                                                 # fetch returned nothing (blocked / non-pdf body / oversized)
        return PdfResult(source="url", error="fetch-empty")
    res = extract_bytes(data, use_vlm=use_vlm)
    res.source = "url"                                           # override: the bytes came from the network here
    return res
