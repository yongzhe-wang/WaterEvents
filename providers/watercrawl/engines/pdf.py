"""watercrawl.engines.pdf — a SELF-CONTAINED generic PDF text engine (no project-specific coupling).

用一句话讲完: url 指向一个 PDF 时,别用浏览器渲染(截图一个 PDF 毫无意义)—— 直接用 curl_cffi 的 Chrome 指纹 GET 把
字节拉回来、pypdf 抽出文本 → (text)。WHY 独立且去硬编码: 原 pool.py 的 render_full/render_detail `from
src.agents.company_agent.tools.slides import is_pdf_url / curl_cffi_pypdf` —— 那是旧 ir-event-pipeline(BERT 项目)的
模块路径,搬到 WaterEvents 后 import 直接断,靠 try/except 静默失效。把 is_pdf_url + PDF fetch 收进 watercrawl 自己的
engine,任何项目复用 watercrawl 都不再依赖某个上游项目的目录结构。{USER 2026-07-23 "i want all those related to
crawling but not hardcode to our task in the folder"} [CONFIDENCE: CONFIRMED — 直接指令:去掉 task-hardcoded 耦合].

依赖都是可选的:curl_cffi(impersonate 也用)+ pypdf。任一缺失 → fetch 返回 ""，caller 退回浏览器渲染,永不崩。
"""
from __future__ import annotations

import io
import re

# A url is a PDF when its PATH ends in .pdf (ignoring ?query / #fragment). Kept deliberately simple + general — a
# content-type sniff would need a HEAD request; the extension test covers the IR case (linked filing/deck PDFs).
_PDF_PATH_RE = re.compile(r"\.pdf(?:$|[?#])", re.I)

# Chrome UA to match the impersonated fingerprint so a fingerprint-walled CDN still serves the PDF bytes.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT = 25                                             # PDFs can be large; a bit more headroom than a page GET


def is_pdf_url(url: str) -> bool:
    """True if url points at a PDF (path ends in .pdf, query/fragment ignored). Generic — no per-site logic."""
    return bool(_PDF_PATH_RE.search(url or ""))


def fetch(url: str) -> str:
    """Download the PDF via a Chrome-fingerprint GET and extract its text → the text, or "" on ANY failure
    (curl_cffi/pypdf absent, non-200, encrypted/scanned PDF with no text layer). Best-effort: the caller falls
    back to a normal render when this returns "". WHY curl_cffi (not plain requests): IR PDFs are often behind the
    same Akamai/Imperva fingerprint wall as their pages, so the Chrome TLS fingerprint is what gets the bytes."""
    try:
        from curl_cffi import requests as creq            # optional dep — import here so module import never fails
        r = creq.get(url, impersonate="chrome", timeout=_TIMEOUT, headers={"User-Agent": _UA, "Accept": "*/*"})
        if r.status_code != 200 or not r.content:
            return ""
        from pypdf import PdfReader                        # optional dep — absent → no PDF text lane
        reader = PdfReader(io.BytesIO(r.content))
        parts = []
        for pg in reader.pages:                            # concatenate every page's extractable text
            try:
                parts.append(pg.extract_text() or "")
            except Exception:                              # noqa: BLE001 — one bad page must not sink the whole doc
                pass
        return "\n".join(p for p in parts if p.strip())
    except Exception:                                      # noqa: BLE001 — missing dep / network / corrupt PDF → no text
        return ""
