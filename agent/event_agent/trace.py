"""event_agent.trace — full per-page AUDIT TRAIL. Everything the agent saw + decided on every page is saved to disk,
so any page is reproducible/inspectable after the fact.

用一句话讲完: crawl 每导航一页, Tracer 就在 run 目录下建一个页面文件夹, 存下 ①content.txt(喂给 LLM 的正文)
②page.html(渲染后 html)③screenshot.jpg(VL 模型实际看的那张图, 从 b64 解出)④links.txt(页上所有链接)
⑤result.json(LLM 返回的 {events, routes}, routes 带 go_deeper)⑥meta.json(url + 哪个 watercrawl 方法成功了 +
时间戳 + 各计数)。加上 run 根的 summary.json(全部事件 + 页面列表)—— 打开任何一页就能看到 agent 用了什么内容
+ 什么截图、判了什么、哪个 fetch 方法拿到的。

Layout:
  <run_dir>/
    pages/0001_<slug>/{content.txt, page.html, screenshot.jpg, links.txt, result.json, meta.json}
    pages/0002_<slug>/...
    summary.json
"""
from __future__ import annotations

import base64
import json
import os
import re
import time


def _slug(url: str) -> str:
    """Filesystem-safe short slug from a url — for a human-readable page folder name."""
    return re.sub(r"[^a-z0-9]+", "-", (url or "").lower()).strip("-")[:60] or "page"


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


class Tracer:
    """Writes the per-page audit trail. One Tracer per crawl run. save_page() is called once per navigated page with
    the raw render result (from watercrawl.render_shot) + the LLM result ({events, routes})."""

    def __init__(self, run_dir: str):
        self.dir = run_dir
        self.pages_dir = os.path.join(run_dir, "pages")
        os.makedirs(self.pages_dir, exist_ok=True)
        self._n = 0
        self._index: list[dict] = []                         # one row per page for summary.json

    def save_page(self, url: str, render: dict, result: dict) -> str:
        """render = watercrawl.render_shot dict {text, links, html, shot_b64, method}; result = {events, routes}.
        Writes the 6 artifacts into pages/NNNN_<slug>/ and returns that folder path."""
        self._n += 1
        d = os.path.join(self.pages_dir, f"{self._n:04d}_{_slug(url)}")
        os.makedirs(d, exist_ok=True)

        _write(os.path.join(d, "content.txt"), render.get("text", ""))            # the text fed to the LLM
        _write(os.path.join(d, "page.html"), render.get("html", ""))              # the rendered html
        _write(os.path.join(d, "links.txt"), "\n".join(render.get("links", [])))  # every link on the page

        shot_b64 = render.get("shot_b64", "")                                     # the EXACT screenshot the VL saw
        if shot_b64:
            try:
                with open(os.path.join(d, "screenshot.jpg"), "wb") as f:
                    f.write(base64.b64decode(shot_b64))
            except Exception:                                                     # noqa: BLE001 — bad b64 → skip the image
                pass

        _write_json(os.path.join(d, "result.json"), result)                       # {events, routes} incl. go_deeper

        go_deeper = [r["url"] for r in (result.get("routes") or []) if r.get("go_deeper")]
        meta = {
            "url": url,
            "method": render.get("method", ""),              # WHICH watercrawl fetch worked: render/residential/impersonate
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "has_screenshot": bool(shot_b64),
            "n_links": len(render.get("links", [])),
            "n_events": len(result.get("events") or []),
            "n_routes": len(result.get("routes") or []),
            "go_deeper": go_deeper,                           # which links we DECIDED to crawl deeper
        }
        _write_json(os.path.join(d, "meta.json"), meta)
        self._index.append({"folder": os.path.basename(d), **{k: meta[k] for k in
                            ("url", "method", "has_screenshot", "n_events", "n_routes")}})
        return d

    def save_summary(self, events: list[dict], pages: int) -> None:
        """Write the run-level summary: all deduped events + a per-page index (url, method, counts)."""
        _write_json(os.path.join(self.dir, "summary.json"),
                    {"n_events": len(events), "n_pages": pages, "pages": self._index, "events": events})
