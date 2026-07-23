"""detect — which office format is this URL? (pdf / pptx / xlsx / docx / html), the cheapest first gate.

用一句话讲完: 给一个 URL → 按扩展名判格式(.pdf/.pptx/.xlsx/.docx/.html),识别 Office 查看器包着的 deck,
或"无扩展名但可能是文档"的 IR 下载端点(/static-files/<uuid>、/files/doc/<id>)。返回格式字符串给 fetch/Docling
当 filename hint;无扩展名端点默认按 pdf 猜(IR 里绝大多数是 PDF),fetch 的 magic + Docling 的自动嗅探会兜底。
"""
from __future__ import annotations

import os.path
import re
from urllib.parse import urlparse

# extension → canonical format Docling understands.
_EXT_FMT = {
    ".pdf": "pdf",
    ".pptx": "pptx", ".ppt": "pptx",
    ".xlsx": "xlsx", ".xls": "xlsx",
    ".docx": "docx", ".doc": "docx",
    ".html": "html", ".htm": "html",
}
_OFFICE_VIEWER_RE = re.compile(r"view\.officeapps\.live\.com|/op/view\.aspx|aka\.ms/", re.I)
# Obvious NON-document extensions — exclude from the extensionless-candidate guess.
_NOT_DOC_RE = re.compile(r"\.(css|js|json|xml|csv|txt|zip|jpe?g|png|gif|svg|webp|ico|mp4|mov|mp3|m4a|wav|m3u8)($|\?|#)", re.I)


def detect_format(url: str) -> str:
    """The office format for `url` by extension, or '' if not an office doc. An Office-viewer link → 'pptx'
    (the wrapped deck)."""
    u = url or ""
    path = urlparse(u.split("#")[0].split("?")[0]).path
    ext = os.path.splitext(os.path.basename(path))[1].lower()
    if ext in _EXT_FMT:
        return _EXT_FMT[ext]
    if _OFFICE_VIEWER_RE.search(u):
        return "pptx"
    return ""


def is_office_url(url: str) -> bool:
    """True iff the URL directly names an office doc (any of pdf/pptx/xlsx/docx/html) or an Office-viewer link."""
    return bool(detect_format(url))


def maybe_office_url(url: str) -> tuple[bool, str]:
    """(is-candidate, format-guess). is_office_url OR an EXTENSIONLESS IR endpoint that could be a document. For an
    extensionless candidate the guess is 'pdf' (the overwhelming majority of IR download endpoints are PDFs); fetch's
    magic + Docling's content sniff correct a wrong guess. Returns (False, '') for obvious non-documents."""
    fmt = detect_format(url)
    if fmt:
        return True, fmt
    path = urlparse((url or "").split("#")[0].split("?")[0]).path
    ext = os.path.splitext(os.path.basename(path))[1]
    if ext == "" and path and not _NOT_DOC_RE.search(url or ""):
        return True, "pdf"                                       # extensionless IR endpoint → assume pdf, verify at fetch
    return False, ""
