"""vlm — image-only / scanned PDF pages → text via a VLM (Qwen2.5-VL served on the H100).

用一句话讲完: pypdf 抽不出文本的 PDF(图片型/扫描件,像 #11)→ 用 pymupdf 把每页渲染成 PNG → 把图片喂给
Qwen2.5-VL(通过 OpenAI 兼容的 vLLM 端点)→ 让它逐页 verbatim 抽出文本 → 拼起来。页面并发调用(vLLM 服务端
continuous-batching,客户端并发 = 更高吞吐)。端点/模型/DPI 全走 env,任何一步的依赖或端点缺失都返回 '',绝不 break。

这是 WaterEvents 的第一处"调 VLM"基础设施:客户端 + 页面渲染都在这;VLM 本身由 providers/qwen_llm(vLLM 起
Qwen2.5-VL)提供,本模块只需 QWEN_VLM_BASE_URL 指向它。

依赖(全 lazy):pymupdf(fitz,渲染)、requests(调端点)。缺任一 → vlm_ocr 返回 '',上层退回"无 VLM"。
"""
from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor

# The VLM endpoint = an OpenAI-compatible vLLM server hosting Qwen2.5-VL (providers/qwen_llm brings it up on the H100).
_BASE_URL = os.environ.get("QWEN_VLM_BASE_URL", "http://localhost:8000/v1")
_MODEL = os.environ.get("QWEN_VLM_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct")
_API_KEY = os.environ.get("QWEN_VLM_API_KEY", "EMPTY")             # vLLM ignores it, but the OpenAI schema wants a key
_MAX_PAGES = int(os.environ.get("PDF_VLM_MAX_PAGES", "20"))        # a scanned filing's material text is up front; cap the GPU work
_DPI = int(os.environ.get("PDF_VLM_DPI", "150"))                  # 150 DPI ≈ crisp enough for OCR without huge images
_CONCURRENCY = int(os.environ.get("PDF_VLM_CONCURRENCY", "8"))    # concurrent page requests (server batches them)
_TIMEOUT_S = int(os.environ.get("PDF_VLM_TIMEOUT_S", "120"))
_MAX_TOKENS = int(os.environ.get("PDF_VLM_MAX_TOKENS", "4096"))

# The instruction: OCR, not description — verbatim text in reading order, tables kept as text, no commentary.
_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text visible in this document page VERBATIM, in natural reading order. "
    "Preserve tables as tab-separated rows. Do NOT summarize, translate, describe, or add any commentary — output "
    "ONLY the transcribed text. If the page has no text, output nothing."
)


def _render_pages(pdf_bytes: bytes, max_pages: int, dpi: int) -> list[bytes]:
    """PDF bytes → a PNG per page (up to max_pages) via pymupdf. [] if pymupdf is absent or the pdf won't open."""
    try:
        import fitz                                             # pymupdf — lazy heavy dep
    except Exception:                                          # noqa: BLE001 — pymupdf absent → no rendering
        return []
    out: list[bytes] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        zoom = dpi / 72.0                                      # pymupdf's base is 72 DPI; scale the render matrix
        mat = fitz.Matrix(zoom, zoom)
        for page in doc[:max_pages]:
            out.append(page.get_pixmap(matrix=mat).tobytes("png"))
        doc.close()
    except Exception:                                         # noqa: BLE001 — malformed pdf → whatever rendered so far
        return out
    return out


def _b64_data_url(png: bytes) -> str:
    """PNG bytes → an OpenAI image_url data URL (base64) the VLM message accepts inline."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _ocr_one(png: bytes) -> str:
    """One page PNG → its transcribed text via the VLM chat endpoint, or '' on any failure. OpenAI-compatible
    chat/completions payload with an inline image + the OCR prompt."""
    try:
        import requests                                       # lazy: absent → '' no-op
    except Exception:                                         # noqa: BLE001
        return ""
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": _b64_data_url(png)}},
        ]}],
        "max_tokens": _MAX_TOKENS,
        "temperature": 0.0,                                   # deterministic transcription
    }
    try:
        r = requests.post(f"{_BASE_URL}/chat/completions",
                          headers={"Authorization": f"Bearer {_API_KEY}"},
                          json=payload, timeout=_TIMEOUT_S)
        if r.status_code != 200:
            return ""
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:                                         # noqa: BLE001 — endpoint down / bad response → ''
        return ""


def vlm_ocr(pdf_bytes: bytes, max_pages: int | None = None) -> str:
    """Image-only PDF bytes → transcribed text via the VLM. Renders each page → OCRs pages CONCURRENTLY (the vLLM
    server continuous-batches them) → concatenates in page order. '' if not a pdf / pymupdf absent / endpoint down.
    Use as a FALLBACK when pypdf.pdf_to_text returned '' (a scanned/image page has no text layer)."""
    if not pdf_bytes or pdf_bytes[:5] != b"%PDF-":
        return ""
    pages = _render_pages(pdf_bytes, max_pages or _MAX_PAGES, _DPI)
    if not pages:
        return ""
    with ThreadPoolExecutor(max_workers=min(_CONCURRENCY, len(pages))) as pool:
        texts = list(pool.map(_ocr_one, pages))               # page order preserved; server batches the concurrent calls
    return "\n\n".join(t for t in texts if t).strip()
