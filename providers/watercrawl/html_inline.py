"""watercrawl.html_inline — stdlib-only HTML → reading-order text with links INLINE as `[anchor](url)`, for the
NON-browser engines (impersonate / camoufox) whose fetch returns raw HTML but no DOM-serialized `inline`.

用一句话讲完: 拿一段 HTML,按阅读顺序吐出纯文本,遇到 `<a href>` 就地写成 `[anchor](url)`,跳过 script/style,块级标签
处换行 —— 给非浏览器引擎一份"够用的" inline 文本。WHY: crawl 喂给模型的是 INLINE-linked 文本(链接就地嵌在正文里),
模型靠"链接紧挨事件标题"的 locality 把一个 event 和它的 urls 归组;浏览器 render 这份 inline 来自 extract_js(DOM 里的
JS),但 impersonate/camoufox 没浏览器 —— 没有这个的话它们只给纯文本、url 被剥掉 → 模型抽不出 event 的 url → event 被
丢 → 0 events。有了它,HTTP-first 直取的静态页 + camoufox 破墙的页 也能产出带 url 的 events。
{DEBUG 2026-07-23: HTTP-first 把静态页走 impersonate(无 inline)→ 不修的话 0 events} [CONFIDENCE: CONFIRMED 100% —
the crawl's _to_page uses render['inline'] first; a non-browser engine leaving it empty drops every url].

比 extract_js 简单(不做链接聚类/表格/heading struct),但保住了核心的"链接就地内联"—— 这正是模型分组事件所需。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

# tags whose CONTENT is never page text — skip their subtree entirely.
_SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
# block-level tags → a newline so reading order survives (a nav item / list row / table row stays on its own line).
_BLOCK = {"p", "div", "li", "tr", "ul", "ol", "table", "section", "article", "header", "footer",
          "nav", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr"}


class _InlineParser(HTMLParser):
    """Walk the DOM in order; emit text tokens, turn `<a href=http…>text</a>` into `[text](url)` in place, newline at
    block boundaries, drop skip-subtrees. Anchor text is buffered so the link renders as one `[anchor](url)` unit."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self._base = base_url or ""
        self._parts: list[str] = []
        self._skip = 0                                       # >0 while inside a skip-subtree
        self._href: str | None = None                        # current <a> href (absolute), or None when not in a link
        self._atext: list[str] = []                          # buffered anchor text

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "a":
            href = next((v for k, v in attrs if k == "href"), "") or ""
            self._href = urljoin(self._base, href) if href else None   # resolve relative hrefs (GM etc. use /events/...)
            self._atext = []
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "a":
            text = " ".join("".join(self._atext).split())    # collapse anchor-internal whitespace
            if self._href and self._href.startswith("http"):
                self._parts.append(f" [{text or self._href}]({self._href}) ")   # the inline link the model groups on
            elif text:
                self._parts.append(" " + text + " ")
            self._href, self._atext = None, []
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:                           # inside an <a> → buffer for the [anchor] label
            self._atext.append(data)
        else:
            t = data.strip()
            if t:
                self._parts.append(" " + t + " ")


def to_inline(html: str, base_url: str = "") -> str:
    """HTML → reading-order text with links inline as `[anchor](url)`. Best-effort; "" on any parse failure (caller
    then falls back to the engine's plain text). Whitespace collapsed so the model sees clean lines."""
    if not html:
        return ""
    try:
        p = _InlineParser(base_url)
        p.feed(html)
        text = "".join(p._parts)
        text = re.sub(r"[ \t]+", " ", text)                  # runs of spaces → one
        text = re.sub(r" *\n *", "\n", text)                 # trim spaces around newlines
        text = re.sub(r"\n{3,}", "\n\n", text)               # 3+ blank lines → one blank line
        return text.strip()
    except Exception:                                        # noqa: BLE001 — a malformed page must not sink the fetch
        return ""
