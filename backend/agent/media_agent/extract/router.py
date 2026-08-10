"""media_agent.router — URL TYPE ROUTER: classify a url into the handler kind that will process it.

用一句话讲完: 给一个 url → 看它的扩展名 / host / (可选) HEAD content-type → 判成 html | pdf | pptx | docx |
audio | video | other,这样 gather 的 close-loop 就知道把它派给哪个 handler(html→VLM 渲染, pdf/pptx/docx→
Docling, audio→WhisperX, video→yt-dlp→WhisperX, other→跳过只记 url)。**这是纯函数地基 —— 不做 I/O, 不碰网络,
先可单测**;需要更准时再用可选的 async HEAD content-type 兜底一层。

WHY this is component #1: every url the media pipeline touches must first be typed — the whole per-event close-loop is
"pop a url → route by kind → handle → append". Get the classifier rock-solid + unit-testable first, then layer the
handlers on top. Extension + host cover ~95% of real IR links; the HEAD refine is for the extension-less detail urls.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

# The 8 kinds a url can route to. `other` = record the url but do not fetch (feeds, assets, unknown externals).
KIND_HTML, KIND_PDF, KIND_PPTX, KIND_DOCX = "html", "pdf", "pptx", "docx"
KIND_XLSX, KIND_AUDIO, KIND_VIDEO, KIND_OTHER = "xlsx", "audio", "video", "other"

# Extension → kind. Grouped by the handler that will consume it. `.ppt`/`.doc`/`.xls` (legacy) route to the same
# Docling handler as their modern forms — Docling parses all of them. xlsx WAS MISSING (edge-audit H2): an .xlsx url
# fell through to html and was rendered instead of parsed, even though officeall handles it. {OFFICEALL/detect.py:17
# '".xlsx":"xlsx", ".xls":"xlsx"'} [CONFIDENCE: CONFIRMED 100% — officeall detect table; fixed per edge audit H2].
_EXT_KIND = {
    "pdf": KIND_PDF,
    "pptx": KIND_PPTX, "ppt": KIND_PPTX,
    "docx": KIND_DOCX, "doc": KIND_DOCX,
    "xlsx": KIND_XLSX, "xls": KIND_XLSX,
    # audio containers WhisperX (faster-whisper) decodes via ffmpeg
    "mp3": KIND_AUDIO, "wav": KIND_AUDIO, "m4a": KIND_AUDIO, "aac": KIND_AUDIO,
    "flac": KIND_AUDIO, "ogg": KIND_AUDIO, "opus": KIND_AUDIO, "wma": KIND_AUDIO,
    # direct video files — yt-dlp/ffmpeg pull the audio track
    "mp4": KIND_VIDEO, "mov": KIND_VIDEO, "webm": KIND_VIDEO, "mkv": KIND_VIDEO, "m4v": KIND_VIDEO,
}

# Non-content file extensions → `other` (record the url, NEVER render/parse). Empirically present in the real IR
# corpus: 130 .zip (SEC XBRL/filing archives), 7 .ics/.vcs (calendar invites). Rendering a calendar/zip in a headless
# browser = junk output + wasted render. .txt is DELIBERATELY excluded — a .txt is renderable text (a transcript /
# release), and skipping it would LOSE data. {AUDIT 2026-07-23 over 13,981 real URLs: .zip×130 .ics×5 .vcs×2, all
# currently → html} [CONFIDENCE: CONFIRMED 100% — measured on the ir-pipeline bert_media + events corpus].
_NONCONTENT_EXT = frozenset({"zip", "7z", "rar", "tar", "gz", "ics", "vcs", "ical", "dmg", "exe", "pkg"})

# Video/webcast-hosting sites where the url has NO file extension but is still a stream (route to yt-dlp → audio →
# WhisperX). The 2nd+3rd rows were added from a 2026-07-23 audit of 13,981 real IR URLs: webcast platforms that were
# being rendered-as-html and thus losing the call audio entirely — media-server(62 urls) / wsw(22) / teletogether(15)
# / choruscall(14) / on24(9) / kvgo(8) etc. The host-check runs BEFORE _EXT_KIND, so a webcast player page carrying a
# .html/.php tail (teletogether .php, choruscall webcast.html) still routes to video. {AUDIT 2026-07-23: webcast
# html-misroute 214→100 after adding these} [CONFIDENCE: CONFIRMED 100% — workflow-verified over the real corpus].
_VIDEO_HOSTS = re.compile(
    r'(?:^|\.)(?:youtube\.com|youtu\.be|vimeo\.com|wistia\.com|brightcove\.net|'
    r'q4inc\.com|webcasts?\.com|veracast\.com|open-exchange\.net|issuerdirect\.com|'
    r'media-server\.com|on24\.com|choruscall\.com|wsw\.com|teletogether\.com|kvgo\.com|'
    r'metameetings\.net|webinar\.net|irwebmeeting\.com|irwebcasting\.com|summitcast\.com|'
    r'viavid\.com|webcaster4\.com|webcast-eqs\.com|talkpoint\.com|royalcast\.com|'
    r'c-conf\.com|vevent\.com|zoom\.us)$', re.I)

# Deterministic PDF-export signals — an EXTENSIONLESS url whose path/query nonetheless says "this is a PDF", so no HEAD
# is needed. `/pdf$` = Drupal /node/<id>/pdf export (70 real urls, all bert filing/financials). The path regex is
# ANCHORED to the WHOLE final segment (`/pdf/?$`) so it does NOT catch slug pages like `.../earnings-release-pdf`
# (verified 0 false). The query regex matches only render-as-pdf TOGGLES (asPDF/format=pdf/…), never a bare filename
# token inside `?f=x.pdf` (verified 0 false). {AUDIT 2026-07-23} [CONFIDENCE: CONFIRMED 100% — workflow-verified].
_PDF_EXPORT_PATH = re.compile(r'/pdf/?$', re.I)
_PDF_EXPORT_QUERY = re.compile(r'(?:^|&)(?:as[_-]?pdf|format=pdf|type=pdf|download=pdf)(?:=[^&]*)?(?:&|$)', re.I)

# Opaque-download endpoints whose PATH alone CANNOT decide the file type — the tail is a bare uuid/token and the SAME
# shape serves pdf/slides/audio/image across companies (Q4 /static-files/<uuid> spans 6 media_types; globenewswire
# /Resource/Download/<uuid>; DAM /api/asset/<token>/download). classify() returns html for these; is_opaque_download()
# lets dispatch FORCE the HEAD content-type refine so they are never silently rendered-as-html when they are PDFs.
# {AUDIT 2026-07-23: 375 opaque-download urls} [CONFIDENCE: CONFIRMED 100% — workflow-verified: type NOT URL-inferable].
_OPAQUE_DOWNLOAD_RE = re.compile(r'/static-files/|/resource/download/|/api/asset/|/download/?$', re.I)

# Structural non-content urls — feeds / sitemaps / static assets. Mirrors event_agent's _NONEVENT_URL_RE so the two
# agents agree on "never a real content page". These route to `other` (recorded, not fetched).
_ASSET_RE = re.compile(
    r'\.(xml|rss|atom|css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|#|$)'
    r'|/(rss|feeds?|atom|sitemap)(/|\.|\?|#|$)',
    re.I)


def _ext(path: str) -> str:
    """Lowercased final path extension without the dot, or "" if none. Query/fragment already stripped by caller."""
    tail = path.rsplit("/", 1)[-1]            # last path segment (the filename)
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""   # text after the last dot, else no extension


# A VIEWER WRAPPER IS A PAGE WHOSE ONLY CONTENT IS A DOCUMENT IT EMBEDS. `/pdf-viewer.aspx?src=/…/h1-13-report.pdf`
# has no extension the router can read, so it classified as html, went to the browser, and what came back was the
# PDF.js toolbar stored as an investor document — "Skip to main content / PDF.js viewer / Find / Zoom In / Zoom Out /
# Page Fit / 50% / 75% / 100%", 234 characters, zero financials, while the actual 68-page results deck sat one query
# parameter away.
# {DB 2026-08-10 event_documents kind='html' matching a viewer pattern → 539 rows, 479 of them under 1000 chars;
#  by host "WWW.VODAFONE.COM 421 (avg 1192 chars) | WWW.DIAGEO.COM 64 (339) | WWW.TXNMENERGY.COM 39 (319) |
#  INVESTORS.TRANSUNION.COM 10 (234)"}
# {DB 2026-08-10 the modal body of those rows, verbatim: "SKIP TO MAIN CONTENT PDF.JS VIEWER FIND 11 PREVIOUS NEXT
#  HIGHLIGHT ALL MATCH CASE MATCH DIACRITICS WHOLE WORDS … ZOOM OUT ZOOM IN PAGE FIT AUTOMATIC ZOOM ACTUAL SIZE
#  PAGE WIDTH 0% 50% 75% 100% 125% 150% 200% 300% 400% SAVE" — the viewer's own chrome, 438 rows share this prefix}
# [CONFIDENCE: CONFIRMED 100% — counts from the live table; the same wrapper url opened in a browser renders
#  "Vodafone Group Plc Preliminary Results, 68 pages", so the document is reachable and only the routing was wrong.]
#
# Generalises the officeapps.live.com unwrap that fetch.py already does for exactly this shape — same idea, same
# `src=` convention, applied here instead so the KIND is decided from the real document rather than from the wrapper.
_VIEWER_PATH_RE = re.compile(r"(pdf-?viewer|/viewer\.(aspx|html?|php)|/web/viewer\.html|officeapps\.live\.com)", re.I)
_VIEWER_PARAMS = ("src", "file", "url", "document", "pdf")     # the param names these viewers carry the target in


def unwrap_viewer(url: str) -> str:
    """A document-viewer wrapper url → the document it embeds; anything else → unchanged (pure, no network).

    Only unwraps when BOTH halves agree: the path looks like a viewer AND the extracted target names a real document
    extension. Requiring the extension is what keeps a generic `?url=` on an ordinary page from being hijacked — an
    unwrap that guesses would turn one bad document into a wrong one, which is harder to notice than the toolbar."""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")) or not _VIEWER_PATH_RE.search(u):
        return url
    q = parse_qs(urlsplit(u).query)
    for p in _VIEWER_PARAMS:
        raw = (q.get(p) or [""])[0]
        if not raw:
            continue
        target = unquote(raw)
        # urljoin handles both forms these viewers use: officeapps passes an absolute url, the Sitecore-style
        # `/~/media/Files/…` ones pass a host-relative path that only means anything against the wrapper's own host.
        joined = urljoin(u, target)
        if _ext(urlsplit(joined).path) in _EXT_KIND:
            return joined
    return url


def classify(url: str) -> str:
    """url → one of the 7 KIND_* strings. Pure + deterministic (no network). Order of judgment:
    0. document-viewer wrapper → judge the document it embeds, not the wrapper
    1. non-http → other (mailto:, tel:, javascript:, #anchor)
    2. asset/feed regex → other
    3. known video host → video   (extension-less webcast links)
    4. file extension → its kind  (pdf/pptx/docx/audio/video)
    5. default → html             (an IR detail page with no extension)
    The extension-less HTML-vs-something ambiguity that this can't resolve is handled by the optional async HEAD
    refine (classify_by_content_type), used only when a downstream fetch returns a surprising content-type."""
    u = unwrap_viewer((url or "").strip())
    if not u.lower().startswith(("http://", "https://")):   # mailto/tel/js/bare-fragment → nothing to fetch
        return KIND_OTHER
    parts = urlsplit(u)
    if _ASSET_RE.search(u):                                 # feed / sitemap / static asset → record only
        return KIND_OTHER
    host = (parts.netloc or "").lower().split(":")[0]       # host without port
    if _VIDEO_HOSTS.search(host):                           # youtube/vimeo/q4 webcast → video (yt-dlp handles it)
        return KIND_VIDEO
    ext = _ext(parts.path)                                  # final path-segment extension (lowercased), "" if none
    if ext in _NONCONTENT_EXT:                              # calendar / archive / binary → record only, never fetch
        return KIND_OTHER
    kind = _EXT_KIND.get(ext)                               # extension → kind (pdf/pptx/docx/xlsx/audio/video file)
    if kind:
        return kind
    if ext == "":                                          # EXTENSIONLESS: check deterministic pdf-export signals
        if _PDF_EXPORT_PATH.search(parts.path) or _PDF_EXPORT_QUERY.search(parts.query):
            return KIND_PDF                                # /node/<id>/pdf export or ?asPDF toggle → a PDF, no HEAD
    return KIND_HTML                                        # no extension, not a known video host → treat as a page


def is_opaque_download(url: str) -> bool:
    """True for an extensionless opaque-download endpoint whose path alone can't decide the file type (Q4
    /static-files/<uuid>, globenewswire /Resource/Download/<uuid>, DAM /api/asset/<token>/download). classify() returns
    html for these; dispatch consults this to FORCE the HEAD content-type refine so a real PDF is never rendered-as-html.
    Kept separate from the deterministic /pdf$ rule BECAUSE these tails carry NO type token — only Content-Type decides."""
    try:
        return bool(_OPAQUE_DOWNLOAD_RE.search(urlsplit(url if url.startswith("http") else "https://" + url).path))
    except Exception:                                       # noqa: BLE001 — unparseable → not an opaque download
        return False


# Content-type → kind, for the async HEAD refine. Only the ambiguous cases matter (a detail url with no extension that
# actually serves a PDF, or a redirect to a media file). Keyed by content-type substring.
_CT_KIND = [
    ("application/pdf", KIND_PDF),
    ("presentationml", KIND_PPTX), ("vnd.ms-powerpoint", KIND_PPTX),
    ("wordprocessingml", KIND_DOCX), ("msword", KIND_DOCX),
    # SPREADSHEETS — absent until 2026-08-06, which meant an extension-less xlsx download refined to... html, and was
    # then rendered as a web page. The HEAD sniff worked; its answer was discarded for want of a table row:
    # {VM 2026-08-06 "HTTPS://IR.SYMBOTIC.COM/STATIC-FILES/3466E2D6-… HEAD CONTENT-TYPE = 'APPLICATION/VND.MS-EXCEL'
    #  → REFINE 结果 = HTML"}
    # Both spellings are needed: `spreadsheetml` is the OOXML .xlsx type and `vnd.ms-excel` the legacy OLE2 .xls one;
    # IR platforms serve both. [CONFIDENCE: CONFIRMED 100% — the ms-excel header was read off the live url from the VM.]
    ("spreadsheetml", KIND_XLSX), ("vnd.ms-excel", KIND_XLSX),
    ("audio/", KIND_AUDIO),
    ("video/", KIND_VIDEO),
    ("text/html", KIND_HTML), ("application/xhtml", KIND_HTML),
    # DELIBERATELY ABSENT: application/octet-stream and application/zip. Both are honest but useless — octet-stream
    # means "bytes", and zip is the container for xlsx/pptx/docx alike. Mapping either would be a guess dressed up as a
    # classification; leaving them unmatched returns the caller's fallback and lets the content itself decide later.
]


def classify_by_content_type(content_type: str, fallback: str = KIND_HTML) -> str:
    """Refine a kind from an HTTP Content-Type header (obtained via a cheap HEAD, done by the caller — this stays
    pure). Used when classify() said `html` but the server actually serves a pdf/media (extension-less download urls).
    Returns `fallback` (the classify() guess) when the content-type is unrecognized."""
    ct = (content_type or "").lower()
    for needle, kind in _CT_KIND:            # first substring match wins (ordered specific → generic)
        if needle in ct:
            return kind
    return fallback


def has_extension(url: str) -> bool:
    """True if the url's final path segment carries a file extension (`.pdf`, `.mp4`, …). Used to decide whether the
    HEAD content-type refine is worth doing (only extension-LESS urls are ambiguous) — an explicit extension is trusted."""
    try:
        return bool(_ext(urlsplit(url if url.startswith("http") else "https://" + url).path))
    except Exception:                        # noqa: BLE001 — unparseable → treat as no-extension (let the refine try)
        return False


def is_direct_file(url: str) -> bool:
    """True if the url NAMES a file by a known extension we handle (pdf/pptx/docx/xlsx/audio/video-file) — as opposed
    to an extension-less platform/page url. Lets handle_video tell a DIRECT .mp4 (transcribe its audio) from a youtube
    PAGE (needs yt-dlp). {edge audit M1 — youtube must route to yt-dlp, not the audio-fetch that gate-passes it}."""
    try:
        return _ext(urlsplit(url if url.startswith("http") else "https://" + url).path) in _EXT_KIND
    except Exception:                        # noqa: BLE001 — unparseable → not a direct file
        return False
