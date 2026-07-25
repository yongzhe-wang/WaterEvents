"""Audit one real page's routing: rebuild inline from page.html, show how many links link_list() found, then run the
routing blocks and print each block's finish_reason + raw route count, so we can tell "0 routes = model returned empty"
from "0 routes = truncated" from "0 routes = all filtered". Usage: python tests/audit_page.py <slug-substring>"""
import os, sys, json, glob, asyncio

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")

from providers.watercrawl import html_inline
from providers.qwen_llm import QwenClient
from agent.event_agent import prompts, extract

SRC = "/workspace/WaterEvents/tests/ir_official_variants/sample_10"


async def main(sub):
    d = [p for p in sorted(glob.glob(SRC + "/*/")) if sub in p][0]
    url = json.load(open(d + "meta.json"))["url"]
    inline = html_inline.to_inline(open(d + "page.html", encoding="utf-8").read(), url)
    link_block, route_map = prompts.link_list(inline)
    lines = [ln for ln in link_block.split("\n") if ln.strip()]
    print(f"URL: {url}\ninline_chars={len(inline)}  links_found={len(lines)}")
    print("first 12 link anchors:")
    for ln in lines[:12]:
        print("   ", ln[:90])
    c = QwenClient()
    step = extract._ROUTE_TARGET_LINKS
    groups = [lines[i:i + step] for i in range(0, len(lines), step)]
    print(f"\nrouting in {len(groups)} block(s) of ≤{step} links:")
    for gi, g in enumerate(groups):
        res = (await c.send_many([extract._build_routes_job("\n".join(g), url)]))[0]
        raw_n = len(res.get("routes") or [])
        norm = extract._normalize_routes(res, route_map)
        print(f"  block {gi}: links={len(g)}  finish={res.get('__finish__')}  raw_routes={raw_n}  kept_after_filter={len(norm)}  err={res.get('_error')}")
        for r in norm[:5]:
            print(f"      {r['score']:.2f} {r['url'][:80]}")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "cintas"))
