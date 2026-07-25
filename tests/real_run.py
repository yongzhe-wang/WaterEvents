"""Run the two-call pipeline over a whole ir_official_variants sample (sample_10 / sample_100 / sample_1000). For each
page: rebuild inline from page.html, extract, write ONE txt per page under <sample>_out/, and accumulate a SUMMARY.txt
with aggregate audit stats (event/route totals, distribution, and pages FLAGGED for review: 0 routes on a linked page,
bulk routes >30, hard errors). Usage: python tests/real_run.py sample_100
"""
import os, sys, json, glob, asyncio, time

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")
os.environ.setdefault("WATERCRAWL_NO_SHOT", "1")
os.environ.setdefault("QWEN_CONCURRENCY", "16")

from providers.watercrawl import html_inline
from agent.event_agent import extract
from providers.qwen_llm import QwenClient

SAMPLE = sys.argv[1] if len(sys.argv) > 1 else "sample_100"
SRC = f"/workspace/WaterEvents/tests/ir_official_variants/{SAMPLE}"
OUT = f"/workspace/WaterEvents/tests/{SAMPLE}_out"
os.makedirs(OUT, exist_ok=True)


def load_pages():
    pages = []
    for d in sorted(glob.glob(SRC + "/*/")):
        mp = d + "meta.json"
        if not os.path.exists(mp):                            # skip non-page dirs (manifest etc.)
            continue
        try:
            url = json.load(open(mp))["url"]
            inline = html_inline.to_inline(open(d + "page.html", encoding="utf-8").read(), url)
        except Exception as e:                                # noqa: BLE001 — skip an unreadable page, keep going
            print("SKIP", os.path.basename(d.rstrip("/")), e); continue
        pages.append({"slug": os.path.basename(d.rstrip("/")), "page_url": url, "page_text": inline})
    return pages


async def one(p, c, tally):
    r = await extract.extract_page({**p, "image_b64": None}, client=c, use_image=False)
    evs, rts = r.get("events", []), r.get("routes", [])
    with open(f"{OUT}/{p['slug']}.txt", "w", encoding="utf-8") as o:
        o.write(f"URL: {p['page_url']}\ninline_chars: {len(p['page_text'])}\n")
        o.write(f"events={len(evs)}  routes={len(rts)}  err={r.get('_error')}\n\n=== EVENTS ({len(evs)}) ===\n")
        for e in evs:
            o.write(f"  [{e['date'] or '-':12s}] ({e['type'] or '-'}) {e['title'][:80]}\n      urls: {e['urls']}\n")
        o.write(f"\n=== ROUTES ({len(rts)}) ===\n")
        for x in sorted(rts, key=lambda z: -z["score"]):
            o.write(f"  {x['score']:.2f}  {x['url']}\n")
    tally.append((p["page_url"].split("//")[-1].split("/")[0], len(evs), len(rts), bool(r.get("_error")),
                  len(p["page_text"])))
    if len(tally) % 10 == 0:
        print(f"  ...{len(tally)} pages done", flush=True)


async def main():
    pages = load_pages()
    print(f"{SAMPLE}: loaded {len(pages)} pages", flush=True)
    c = QwenClient()
    tally = []
    t = time.time()
    await asyncio.gather(*(one(p, c, tally) for p in pages))   # all pages in flight, bounded by client semaphore
    dt = time.time() - t

    tot_ev = sum(e for _, e, _, _, _ in tally)
    tot_rt = sum(r for _, _, r, _, _ in tally)
    zero_ev = [t for t in tally if t[1] == 0]
    zero_rt = [t for t in tally if t[2] == 0 and t[4] > 500]   # 0 routes on a page that HAD content = suspicious
    bulk_rt = [t for t in tally if t[2] > 30]
    errs = [t for t in tally if t[3]]
    with open(f"{OUT}/SUMMARY.txt", "w", encoding="utf-8") as o:
        o.write(f"{SAMPLE}: {len(pages)} pages in {dt:.0f}s\n")
        o.write(f"total events={tot_ev} (avg {tot_ev/max(1,len(pages)):.1f}/pg)  "
                f"total routes={tot_rt} (avg {tot_rt/max(1,len(pages)):.1f}/pg)\n")
        o.write(f"pages with 0 events={len(zero_ev)}  0 routes(w/ content)={len(zero_rt)}  "
                f"bulk routes(>30)={len(bulk_rt)}  hard errors={len(errs)}\n\n")
        o.write("=== FLAGGED: 0 routes but had content (likely routing miss) ===\n")
        for h, e, r, er, ch in sorted(zero_rt): o.write(f"  {h:40s} ev={e} chars={ch}\n")
        o.write("\n=== FLAGGED: bulk routes >30 (likely over-routing) ===\n")
        for h, e, r, er, ch in sorted(bulk_rt, key=lambda z: -z[2]): o.write(f"  {h:40s} routes={r} ev={e}\n")
        o.write("\n=== FLAGGED: hard errors ===\n")
        for h, e, r, er, ch in errs: o.write(f"  {h}\n")
        o.write("\n=== ALL PAGES (host | ev | rt) ===\n")
        for h, e, r, er, ch in sorted(tally, key=lambda z: -z[1]): o.write(f"  {h:42s} ev={e:3d} rt={r:3d}\n")
    print(f"DONE {SAMPLE}: {len(pages)}pg {dt:.0f}s  events={tot_ev} routes={tot_rt}  "
          f"0ev={len(zero_ev)} 0rt={len(zero_rt)} bulk={len(bulk_rt)} err={len(errs)}", flush=True)


asyncio.run(main())
