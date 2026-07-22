"""detect — is this URL a PowerPoint deck? (the cheapest first gate)

用一句话讲完: 给一个 URL → 看是不是 `.pptx`/`.ppt` 结尾,或者是 Office Online 查看器 / aka.ms 短链包着的 deck
(view.officeapps.live.com/op/view.aspx?src=…、aka.ms/…)—— IR 的投资者 deck 常常这样发。判 maybe=True 只让 fetch
去试;fetch 用 ZIP(PK)magic + python-pptx 兜底,猜错=零成本 no-op。
"""
from __future__ import annotations

import os.path
import re
from urllib.parse import urlparse

_PPTX_RE = re.compile(r"\.pptx?($|\?|#)", re.I)                    # .pptx or legacy .ppt
_OFFICE_VIEWER_RE = re.compile(r"view\.officeapps\.live\.com|/op/view\.aspx|aka\.ms/", re.I)
# Obvious NON-pptx extensions — exclude from the extensionless guess.
_NOT_PPTX_RE = re.compile(
    r"\.(html?|aspx|jsp|php|pdf|docx?|xlsx?|csv|json|xml|txt|zip"
    r"|jpe?g|png|gif|svg|webp|mp4|mov|mp3|m4a|wav|m3u8)($|\?|#)", re.I)


def is_pptx_url(url: str) -> bool:
    """True iff the URL points directly at a `.pptx`/`.ppt`, OR is an Office-viewer / aka.ms link wrapping a deck."""
    u = url or ""
    return bool(_PPTX_RE.search(u) or _OFFICE_VIEWER_RE.search(u))


def maybe_pptx_url(url: str) -> bool:
    """is_pptx_url OR an EXTENSIONLESS path that COULD be a deck (IR download endpoints: `/files/doc/<id>`,
    `/download`). A wrong guess is cheap — fetch verifies the ZIP magic and python-pptx rejects a non-deck, so the
    caller just falls through. NOTE the Office-viewer `?src=` real url is unwrapped in fetch, not here."""
    if is_pptx_url(url):
        return True
    path = urlparse((url or "").split("#")[0].split("?")[0]).path
    ext = os.path.splitext(os.path.basename(path))[1]
    return ext == "" and bool(path) and not _NOT_PPTX_RE.search(url or "")
