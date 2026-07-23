"""tests/run10.py — run the discovery crawl over 10 real companies → ONE fully-debuggable .txt per company in 10media/.

Same as run5.py but 10 large-cap IR pages + output dir tests/10media/. Each .txt has the company summary + events +
每页完整 prompt + 模型 raw output + trace meta (你要的 debug 格式).

Run ON the box with browser + a live VLM (RunPod), scaled server (--max-model-len 32768):
  QWEN_MAX_TOKENS=12000 EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=6 python3 tests/run10.py
"""
from __future__ import annotations

import asyncio
import glob
import os

from providers.qwen_llm import config as qcfg          # mutate DEBUG_DIR per-company (read at call-time in client._dump_debug)
from agent.event_agent.crawl import crawl_company

# 10 large-cap IR pages (diverse hosts / structures / anti-bot). PepsiCo uses the correct /investors {USER 2026-07-23}.
COMPANIES = [
    "https://www.coca-colacompany.com/investors",
    "https://www.pepsico.com/investors",
    "https://investor.apple.com",
    "https://www.microsoft.com/en-us/investor",
    "https://www.investor.jnj.com",
    "https://investor.nvidia.com",
    "https://investor.visa.com",
    "https://investor.cisco.com",
    "https://investors.pfizer.com",
    "https://www.verizon.com/about/investors",
]

_OUT = os.path.join(os.path.dirname(__file__), "10media")   # tests/10media/
_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "6"))


def _slug(url: str) -> str:
    return url.split("//")[-1].split("/")[0].replace(".", "_")


def _assemble(url: str, result: dict, dbg_dir: str) -> str:
    """Per-company debug txt: summary → events → full prompt+output per page → per-page trace meta."""
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
        dbg_dir = os.path.join(_OUT, f"_dbg_{i}_{slug}")
        os.makedirs(dbg_dir, exist_ok=True)
        qcfg.DEBUG_DIR = dbg_dir                              # per-company debug dumps (client._dump_debug reads at call-time)
        trace_dir = os.path.join(_OUT, f"_trace_{i}_{slug}")
        print(f"\n[run10] === {i+1}/10 {url} (max_pages={_MAX_PAGES}) ===", flush=True)
        try:
            result = await crawl_company(url, max_pages=_MAX_PAGES, trace_dir=trace_dir)
        except Exception as e:                               # noqa: BLE001 — one bad company must not sink the batch
            result = {"events": [], "pages": 0, "trace_dir": trace_dir, "status": "error",
                      "failed_render": 0, "failed_extract": 0, "_error": f"{type(e).__name__}: {e}"}
            print(f"[run10] ⛔ {url} CRAWL ERROR: {e}", flush=True)
        txt = _assemble(url, result, dbg_dir)
        out_txt = os.path.join(_OUT, f"{slug}.txt")
        with open(out_txt, "w", encoding="utf-8") as fh:
            fh.write(txt)
        print(f"[run10] ✅ wrote {out_txt} ({len(txt)} chars, {len(result['events'])} events)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
