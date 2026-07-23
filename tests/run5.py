"""tests/run5.py — run the discovery crawl over 5 real companies and write ONE fully-debuggable .txt per company.

用一句话讲完: 对 5 家真公司,每家把 config.DEBUG_DIR 指到自家目录 → crawl_company 开页+截图+VLM 抽事件(BFS)→
把「company 汇总 + events 列表 + 每页的完整 prompt+模型原始输出(debug dump)+ 每页 trace meta(哪个 fetch 方法/
go_deeper/计数)」拼成 tests/output/<company>.txt —— 打开一个 txt 就能看清这家公司整条抽取链,方便 debug。

Run ON the box that has browser + a live VLM (h20-1039 / RunPod), NOT the Mac:
  QWEN_SERVED_NAME=qwen-vl EVENT_MAX_PAGES=6 python3 tests/run5.py
"""
from __future__ import annotations

import asyncio
import glob
import json
import os

from providers.qwen_llm import config as qcfg          # mutate DEBUG_DIR per-company (read at call-time in client._dump_debug)
from agent.event_agent.crawl import crawl_company

# 5 large-cap IR pages — a diverse set (some render clean, some may wall → exercises the fail-loud path too).
COMPANIES = [
    "https://www.coca-colacompany.com/investors",
    "https://www.pepsico.com/investors/investor-relations",
    "https://investor.apple.com",
    "https://www.microsoft.com/en-us/investor",
    "https://www.investor.jnj.com",
]

_OUT = os.path.join(os.path.dirname(__file__), "output")   # tests/output/
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "6"))   # small per-company so the txt stays readable/debuggable


def _slug(url: str) -> str:
    return url.split("//")[-1].split("/")[0].replace(".", "_")


def _assemble(url: str, result: dict, dbg_dir: str) -> str:
    """Build the per-company debug txt: summary → events → full prompt+output per page → per-page trace meta."""
    L = []
    L.append("=" * 90)
    L.append(f"COMPANY: {url}")
    L.append("=" * 90)
    L.append(f"RESULT: {len(result['events'])} events over {result['pages']} pages | status={result['status']} | "
             f"failed_render={result['failed_render']} failed_extract={result['failed_extract']}")
    L.append(f"trace_dir: {result['trace_dir']}")
    L.append("")
    L.append("=== EVENTS (final, deduped) ===")
    for e in result["events"]:
        L.append(f"  [{e['date'] or '-'}] [{e['type'] or '-'}] {e['title'][:90]}  ({len(e['urls'])} urls)")
        for u in e["urls"]:
            L.append(f"        {u}")
    L.append("")
    L.append("#" * 90)
    L.append("# FULL PROMPT + RAW MODEL OUTPUT PER PAGE  (the exact I/O for every VLM call)")
    L.append("#" * 90)
    for f in sorted(glob.glob(os.path.join(dbg_dir, "req_*.txt"))):
        L.append(f"\n\n----------------- {os.path.basename(f)} -----------------")
        with open(f, encoding="utf-8") as fh:
            L.append(fh.read())
    L.append("")
    L.append("#" * 90)
    L.append("# PER-PAGE TRACE META  (which watercrawl method won / go_deeper routes / counts)")
    L.append("#" * 90)
    for meta_f in sorted(glob.glob(os.path.join(result["trace_dir"], "pages", "*", "meta.json"))):
        with open(meta_f, encoding="utf-8") as fh:
            L.append(f"\n{os.path.relpath(meta_f, result['trace_dir'])}:\n{fh.read()}")
    return "\n".join(L)


async def main() -> None:
    os.makedirs(_OUT, exist_ok=True)
    for i, url in enumerate(COMPANIES):
        slug = _slug(url)
        dbg_dir = os.path.join(_OUT, f"_dbg_{i}_{slug}")     # per-company debug-dump dir
        os.makedirs(dbg_dir, exist_ok=True)
        qcfg.DEBUG_DIR = dbg_dir                              # client._dump_debug reads this at call-time → per-company dumps
        trace_dir = os.path.join(_OUT, f"_trace_{i}_{slug}")
        print(f"\n[run5] === {i+1}/5 {url} (max_pages={_MAX_PAGES}) ===", flush=True)
        try:
            result = await crawl_company(url, max_pages=_MAX_PAGES, trace_dir=trace_dir)
        except Exception as e:                               # noqa: BLE001 — one bad company must not sink the batch
            result = {"events": [], "pages": 0, "trace_dir": trace_dir, "status": "error",
                      "failed_render": 0, "failed_extract": 0, "_error": f"{type(e).__name__}: {e}"}
            print(f"[run5] ⛔ {url} CRAWL ERROR: {e}", flush=True)
        txt = _assemble(url, result, dbg_dir)
        out_txt = os.path.join(_OUT, f"{slug}.txt")
        with open(out_txt, "w", encoding="utf-8") as fh:
            fh.write(txt)
        print(f"[run5] ✅ wrote {out_txt} ({len(txt)} chars, {len(result['events'])} events)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
