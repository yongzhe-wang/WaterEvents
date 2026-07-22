"""detect — is this URL a PDF? (the cheapest first gate, before any network I/O)

用一句话讲完: 给一个 URL → 先看是不是 `.pdf` 结尾;不是的话,看它是不是"无扩展名但可能是 PDF"的端点
(q4cdn `/static-files/<uuid>`、`/files/doc/<id>`、`/download`)—— 这些 IR 平台的 PDF 常常没扩展名,
只靠 `.pdf` 判会漏掉一大半。判 maybe=True 只是让 fetch 去试;fetch 会用 `%PDF-` magic 兜底,猜错=零成本 no-op。
"""
from __future__ import annotations

import os.path
import re
from urllib.parse import urlparse

# A url whose path ends in `.pdf` (query/fragment ignored) — the unambiguous case.
_PDF_RE = re.compile(r"\.pdf($|\?|#)", re.I)
# Extensions that are OBVIOUSLY not a bare PDF — used to EXCLUDE them from the extensionless guess so we don't
# waste a fetch attempt on an HTML/Office/media/data URL.
_NOT_PDF_RE = re.compile(
    r"\.(html?|aspx|jsp|php|pptx?|docx?|xlsx?|csv|json|xml|txt|zip"
    r"|jpe?g|png|gif|svg|webp|mp4|mov|mp3|m4a|wav|m3u8)($|\?|#)", re.I)


def is_pdf_url(url: str) -> bool:
    """True iff the URL points directly at a `.pdf` file (ignoring ?query / #fragment)."""
    return bool(_PDF_RE.search(url or ""))


def maybe_pdf_url(url: str) -> bool:
    """is_pdf_url OR an EXTENSIONLESS path that COULD be a PDF (so fetch also tries the common IR endpoints:
    q4cdn `/static-files/<uuid>`, `/files/doc/<id>`, `/download`). A wrong guess is free — fetch's `%PDF-` magic
    gate returns b'' on a non-PDF body, so the caller just falls through. Erring toward TRYING beats silently
    skipping the many extensionless IR PDFs."""
    if is_pdf_url(url):
        return True
    path = urlparse((url or "").split("#")[0].split("?")[0]).path   # strip query/fragment, keep the path
    ext = os.path.splitext(os.path.basename(path))[1]               # '' for /static-files/<uuid>
    return ext == "" and bool(path) and not _NOT_PDF_RE.search(url or "")
