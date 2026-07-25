"""Run the two-call pipeline on the REAL sample_10 IR pages (ir_official_variants). For each page: rebuild the
inline-linked reading-order text from its saved page.html (via watercrawl.html_inline.to_inline — offline, no browser),
run extract_page, and write ONE output txt per page under tests/real10_out/. Prints a summary table to audit.
"""
import os, sys, json, glob, asyncio, time

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")
os.environ.setdefault("WATERCRAWL_NO_SHOT", "1")            # text-only (no screenshot), the production default
os.environ.setdefault("QWEN_CONCURRENCY", "16")

from providers.watercrawl import html_inline
from agent.event_agent import extract
from providers.qwen_llm import QwenClient

SRC = "/workspace/WaterEvents/tests/ir_official_variants/sample_10"
OUT = "/workspace/WaterEvents/tests/real10_out"
os.makedirs(OUT, exist_ok=True)


def load_pages():
    pages = []
    for d in sorted(glob.glob(SRC + "/*/")):
        try:
            url = json.load(open(d + "meta.json"))["url"]
            html = open(d + "page.html", encoding="utf-8").read()
        except Exception as e:                                # noqa: BLE001 — skip an unreadable page dir, note it
            print("SKIP", d, e); continue
        inline = html_inline.to_inline(html, url)             # offline HTML → inline [anchor](url) reading-order text
        pages.append({"slug": os.path.basename(d.rstrip("/")), "page_url": url, "page_text": inline,
                      "image_b64": None, "links_block": ""})
    return pages


async def main():
    pages = load_pages()
    print(f"loaded {len(pages)} real pages; inline chars: {[len(p['page_text']) for p in pages]}")
    c = QwenClient()
    t = time.time()
    results = await extract.extract_pages(pages, client=c, use_image=False)   # ALL 10 in flight (routing+events parallel)
    dt = time.time() - t

    print(f"\n=== sample_10 real run: {len(pages)} pages in {dt:.1f}s ===")
    print(f"{'page':42s} {'ev':>3s} {'rt':>3s}  err")
    for p, r in zip(pages, results):
        evs, rts = r.get("events", []), r.get("routes", [])
        host = p["page_url"].split("//")[-1].split("/")[0][:40]
        print(f"{host:42s} {len(evs):3d} {len(rts):3d}  {r.get('_error') or ''}")
        # one txt per page: url + inline size + full events + full routes for eyeball audit
        with open(f"{OUT}/{p['slug']}.txt", "w", encoding="utf-8") as o:
            o.write(f"URL: {p['page_url']}\ninline_chars: {len(p['page_text'])}\n")
            o.write(f"events={len(evs)}  routes={len(rts)}  err={r.get('_error')}\n\n")
            o.write(f"=== EVENTS ({len(evs)}) ===\n")
            for e in evs:
                o.write(f"  [{e['date'] or '-':12s}] ({e['type'] or '-'}) {e['title'][:80]}\n      urls: {e['urls']}\n")
            o.write(f"\n=== ROUTES ({len(rts)}) — follow highest-score first ===\n")
            for x in sorted(rts, key=lambda z: -z["score"]):
                o.write(f"  {x['score']:.2f}  {x['url']}\n")
    print(f"\nwrote {len(pages)} per-page txts under {OUT}")


asyncio.run(main())
