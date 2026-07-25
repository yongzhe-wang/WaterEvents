"""Run full extract_page (events + routes) on named page slugs from ir_official_variants and print the events — for
A/B-testing the model (AWQ vs FP16) on the pages that hallucinated. Usage: python tests/check_pages.py acadiarealty hp
"""
import os, sys, json, glob, asyncio

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")
os.environ.setdefault("WATERCRAWL_NO_SHOT", "1")

from providers.watercrawl import html_inline
from agent.event_agent import extract
from providers.qwen_llm import QwenClient


async def go(sub, c):
    d = [p for p in glob.glob("tests/ir_official_variants/sample_*/*/") if sub in p][0]
    url = json.load(open(d + "meta.json"))["url"]
    inline = html_inline.to_inline(open(d + "page.html", encoding="utf-8").read(), url)
    r = await extract.extract_page({"page_url": url, "page_text": inline, "image_b64": None}, client=c, use_image=False)
    evs, rts = r.get("events", []), r.get("routes", [])
    print(f"#### {sub:14s} events={len(evs):3d} routes={len(rts):3d}  ({url})")
    for e in evs[:8]:
        print(f"     [{e['date'] or '-':10s}] ({e['type'] or '-'}) {e['title'][:62]}")


async def main():
    c = QwenClient()
    for s in sys.argv[1:]:
        await go(s, c)


asyncio.run(main())
