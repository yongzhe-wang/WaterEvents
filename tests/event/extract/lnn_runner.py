"""tests/lnn_runner — exercise the REAL extract pipeline (Lnn tag + chunk fallback) on saved trace HTML, TEXT-ONLY.

用一句话讲完: 从 hstress_out/trace 每页的 page.html 用 html_inline.to_inline 重建 [anchor](url) inline 文本 → 喂
extract.extract_pages(真 VLM, Lnn-tagged 压缩输出, 超大页 finish=length 自动 chunk) → 每页写一个 txt(events 带 title+
date+resolved url / routes 数 / 截断 err / 延迟)+ 一个 summary → 验证 Lnn+chunk 到底修没修好之前 url 版 4096 output 截断。
WHY 用 saved html 而不 re-render: 输入固定、可复现、干净隔离 extract(去掉 render variance),且不用再烧一遍 browser。
{USER 2026-07-24 "写 runner 用 extract.extract_page 跑真实页, 测 Lnn+chunk"} [CONFIDENCE: CONFIRMED — 直接指令].

Run ON THE POD (vLLM at 127.0.0.1:8000):
  LNN_N=100 LNN_CONC=12 PYTHONPATH=/workspace/WaterEvents/backend /root/venv/bin/python /workspace/WaterEvents/tests/lnn_runner.py
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time
from urllib.parse import urlsplit

from providers.watercrawl import html_inline          # HTML → [anchor](url) reading-order inline (non-browser fallback path)
from providers.qwen_llm import QwenClient              # the real VLM transport (points at the local vLLM by env)
from agent.event_agent.crawl import extract                  # the REAL endpoint under test: extract_page (Lnn tag + chunk)

TRACE = os.environ.get("LNN_TRACE_ROOT", os.path.dirname(__file__))       # search the WHOLE tests/ tree for saved page.html
OUT = os.path.join(os.path.dirname(__file__), "lnn_out")                  # per-page txt + summary land here
N = int(os.environ.get("LNN_N", "100"))                                   # how many pages to test
CONC = int(os.environ.get("LNN_CONC", "12"))                              # concurrent extract calls (matches crawl fanout)


def _slug(url: str) -> str:                                               # stable filename per page (host + path tail)
    s = urlsplit(url if url.startswith("http") else "https://" + url)
    raw = (s.netloc + s.path).lower()
    return re.sub(r"[^a-z0-9.]+", "-", raw).strip("-")[:80] or "x"


def _collect() -> list[tuple[str, str]]:
    """Walk the WHOLE trace tree (recursive) → (url, html_path) pairs, then take the N BIGGEST pages by html size. WHY
    biggest-first: the truncation bug this tests fires on MEGA event-lists (a page with 100+ dated rows), whose Lnn output
    used to blow the 4096 output cap; sorting by size targets exactly those pages instead of wasting the budget on tiny
    nav pages. Each page.html is paired with its sibling meta.json for the url."""
    htmls = glob.glob(os.path.join(TRACE, "**", "page.html"), recursive=True)   # every saved rendered page, any layout
    pairs: list[tuple[int, str, str]] = []
    for html_p in htmls:
        m = os.path.join(os.path.dirname(html_p), "meta.json")            # url lives in the sibling meta
        if not os.path.exists(m):
            continue
        try:
            url = (json.load(open(m, encoding="utf-8")) or {}).get("url") or ""
        except Exception:                                                 # noqa: BLE001 — a corrupt meta must not sink the run
            continue
        if not url:
            continue
        try:
            sz = os.path.getsize(html_p)                                  # sort key: bigger html ≈ richer/longer page
        except OSError:
            sz = 0
        pairs.append((sz, url, html_p))
    pairs.sort(key=lambda p: -p[0])                                       # biggest pages first (most truncation-prone)
    # dedup by url (the deep crawls revisit some pages) so N distinct pages, biggest kept
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for _sz, url, html_p in pairs:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, html_p))
        if len(out) >= N:
            break
    return out


async def _one(url: str, html_p: str, client: QwenClient, sem: asyncio.Semaphore) -> dict:
    """One page → run REAL extract on its reconstructed inline text; record events/routes/err/latency/in-size."""
    async with sem:
        html = open(html_p, encoding="utf-8", errors="ignore").read()    # saved rendered DOM
        inline = html_inline.to_inline(html, url)                        # rebuild [anchor](url) — exactly what tag_links eats
        page = {"page_url": url, "page_text": inline}                    # text-only (NO_SHOT default → no image attached)
        t0 = time.time()
        try:
            res = await extract.extract_page(page, client, use_image=False)   # THE pipeline under test (Lnn + chunk)
        except Exception as e:                                           # noqa: BLE001 — one page must not sink the batch
            return {"url": url, "in_chars": len(inline), "sec": round(time.time() - t0, 1),
                    "hard_error": f"{type(e).__name__}: {str(e)[:160]}"}
        evs = res.get("events") or []
        rts = res.get("routes") or []
        return {"url": url, "in_chars": len(inline), "sec": round(time.time() - t0, 1),
                "n_events": len(evs), "n_routes": len(rts), "err": res.get("_error"),
                "events": [{"title": e.get("title"), "date": e.get("date"), "urls": e.get("urls")} for e in evs]}


def _write_page(rec: dict) -> None:
    """One human-readable txt per page: header stats + each event's title/date/url (so recall is eyeball-checkable)."""
    p = os.path.join(OUT, _slug(rec["url"]) + ".txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"URL       : {rec['url']}\n")
        f.write(f"in_chars  : {rec.get('in_chars')}\n")
        f.write(f"latency_s : {rec.get('sec')}\n")
        if rec.get("hard_error"):
            f.write(f"HARD_ERROR: {rec['hard_error']}\n")
            return
        f.write(f"n_events  : {rec.get('n_events')}\n")
        f.write(f"n_routes  : {rec.get('n_routes')}\n")
        if rec.get("err"):
            f.write(f"_error    : {rec['err']}\n")               # truncation / incomplete marker
        f.write("\n=== EVENTS ===\n")
        for e in rec.get("events") or []:
            f.write(f"- [{e.get('date') or '?'}] {e.get('title') or '(no title)'}\n")
            for u in e.get("urls") or []:
                f.write(f"    {u}\n")


async def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    pairs = _collect()
    print(f"[lnn] {len(pairs)} pages | conc={CONC} | NO_SHOT default (text-only Lnn) | out={OUT}", flush=True)
    client = QwenClient()
    sem = asyncio.Semaphore(CONC)
    t0 = time.time()
    results = await asyncio.gather(*(_one(u, h, client, sem) for (u, h) in pairs))
    dt = time.time() - t0

    for rec in results:                                          # dump each page's txt
        _write_page(rec)

    # aggregate — the numbers that answer "did Lnn+chunk fix truncation?"
    hard = [r for r in results if r.get("hard_error")]
    errd = [r for r in results if r.get("err")]                  # output_truncated / incomplete
    with_ev = [r for r in results if (r.get("n_events") or 0) > 0]
    total_ev = sum(r.get("n_events") or 0 for r in results)
    total_rt = sum(r.get("n_routes") or 0 for r in results)
    big = [r for r in results if (r.get("in_chars") or 0) > 48000]   # pages large enough to trigger the chunk path
    big_ok = [r for r in big if not r.get("err") and not r.get("hard_error")]
    sizes = sorted((r.get("in_chars") or 0) for r in results)
    p50 = sizes[len(sizes) // 2] if sizes else 0
    pmax = sizes[-1] if sizes else 0

    summary = (
        "=== LNN + CHUNK EXTRACT TEST (text-only, real VLM) ===\n"
        f"pages              : {len(results)}\n"
        f"wall time          : {dt:.0f}s   ({len(results)/dt:.2f} pages/s)\n"
        f"total events        : {total_ev}\n"
        f"total routes        : {total_rt}\n"
        f"pages WITH ≥1 event : {len(with_ev)}\n"
        f"in_chars p50 / max  : {p50} / {pmax}\n"
        f"BIG pages (>48k, chunk-path): {len(big)}  → completed clean: {len(big_ok)}\n"
        f"pages with _error (truncated/incomplete): {len(errd)}\n"
        f"HARD errors (transport/exception): {len(hard)}\n"
    )
    print("\n" + summary, flush=True)
    with open(os.path.join(OUT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[lnn] wrote {OUT}/summary.txt + {len(results)} per-page txt", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
