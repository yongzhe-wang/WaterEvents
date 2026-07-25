"""Produce a human-reviewable dump of the two-call pipeline on the ONE big synthetic edge-case page. Writes, under
tests/edge_review/:
  routing/routing_NN.txt        — one file per ROUTING call  (SYSTEM+USER prompt + RAW model output + PARSED routes)
  classification/chunk_NN.txt   — one file per EXTRACTION chunk (SYSTEM+USER prompt + RAW output + PARSED events)
  RESULTS.txt                   — the FINAL merged {events, routes} after _combine (post exclusivity)
So the routing pass and the classification pass can be eyeballed separately. Run on the pod: python tests/dump_review.py
"""
import os, sys, glob, shutil, asyncio

sys.path.insert(0, "/workspace/WaterEvents")
BASE = "/workspace/WaterEvents/tests/edge_review"
RAW = BASE + "/_raw"
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(RAW)
# MUST set QWEN_DEBUG_DIR BEFORE importing config (config reads it once at import) so every request dumps prompt+output.
os.environ["QWEN_DEBUG_DIR"] = RAW

import edge_test                                              # sets QWEN_* env + builds the big synthetic page (main guarded)
from agent.event_agent import extract


async def main():
    page = {"page_url": "https://ir.acme.com", "page_text": edge_test.BIG, "image_b64": None, "links_block": ""}
    r = await extract.extract_page(page, use_image=False)     # runs routing + chunked extraction, both dumped to RAW

    route_dir, cls_dir = BASE + "/routing", BASE + "/classification"
    os.makedirs(route_dir); os.makedirs(cls_dir)
    ri = ci = 0
    for f in sorted(glob.glob(RAW + "/req_*.txt")):           # sort each dumped request into its pass by prompt content
        txt = open(f, encoding="utf-8").read()
        if "LINKS ON THIS PAGE" in txt:                       # routing user prompt marker (build_routes_user)
            ri += 1
            shutil.copy(f, f"{route_dir}/routing_{ri:02d}.txt")
        else:                                                 # extraction user prompt marker (build_events_user "PAGE CONTENT")
            ci += 1
            shutil.copy(f, f"{cls_dir}/chunk_{ci:02d}.txt")
    shutil.rmtree(RAW, ignore_errors=True)

    evs, rts = r.get("events", []), r.get("routes", [])
    with open(BASE + "/RESULTS.txt", "w", encoding="utf-8") as o:
        o.write("=== FINAL PAGE RESULT (after _combine: routing + extraction + event⊥route exclusivity) ===\n")
        o.write(f"events={len(evs)}  routes={len(rts)}  err={r.get('_error')}\n")
        o.write(f"routing calls={ri}   classification chunks={ci}\n\n")
        o.write(f"=== ROUTES ({len(rts)}) — links the crawler will FOLLOW (highest score first) ===\n")
        for x in sorted(rts, key=lambda z: -z["score"]):
            o.write(f"  {x['score']:.2f}  {x['url']}\n")
        o.write(f"\n=== EVENTS ({len(evs)}) ===\n")
        for e in evs:
            o.write(f"  [{e['date'] or '-':10s}] ({e['type'] or '-'}) {e['title'][:70]}  ->  {e['urls']}\n")
    print(f"wrote {ri} routing + {ci} classification txts + RESULTS.txt under {BASE}")


asyncio.run(main())
