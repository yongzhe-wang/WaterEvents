"""Fix routing via CONTEXT ENGINEERING + CHUNKING (no hardcoded keyword filter). The current routing pass shows the
model ONLY the anchor text ("Lnn — anchor") with NO url — so it can't tell /investors/financial-reports from
/uniform-rental, and on a product-heavy page it either bails (cintas → 0) or bulk-routes (skyperfect → 60 @0.90). Test
whether giving the model the url PATH per link (context) + smaller blocks (chunking) lets it discriminate ON ITS OWN,
over the FULL link set (no pre-filtering). Variants per page:
  A anchor-only, all links, 1 block   (current behavior — baseline)
  B anchor + url,  all links, 1 block   (context engineering)
  C anchor + url,  ≤40 links/block      (context + chunking, routes merged)
"""
import os, sys, json, glob, asyncio, re

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")

from providers.watercrawl import html_inline
from providers.qwen_llm import QwenClient
from agent.event_agent import prompts, extract


def uniq_pairs(inline):
    pairs = re.findall(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", inline)
    seen, out = set(), []
    for a, u in pairs:
        if u not in seen:
            seen.add(u); out.append((a.strip() or "(no text)", u))
    return out


async def route(pairs, url, with_url, per_block):
    # build the link lines (optionally with the full url as context) + a fresh Lnn map; chunk into per_block groups.
    lines, tag_map = [], {}
    for i, (a, u) in enumerate(pairs, 1):
        rid = f"L{i}"; tag_map[rid] = u
        lines.append(f"{rid} — {a}" + (f"  {u}" if with_url else ""))
    groups = [lines[i:i + per_block] for i in range(0, len(lines), per_block)]
    jobs = [extract._build_routes_job("\n".join(g), url) for g in groups]
    results = await QwenClient().send_many(jobs)
    best = {}
    for res in results:
        for r in extract._normalize_routes(res, tag_map):
            if r["url"] not in best or r["score"] > best[r["url"]]["score"]:
                best[r["url"]] = r
    return sorted(best.values(), key=lambda z: -z["score"])


async def go(sub):
    d = [p for p in sorted(glob.glob("tests/ir_official_variants/sample_10/*/")) if sub in p][0]
    url = json.load(open(d + "meta.json"))["url"]
    pairs = uniq_pairs(html_inline.to_inline(open(d + "page.html", encoding="utf-8").read(), url))
    print(f"\n#### {sub}: {len(pairs)} unique links")
    for label, wu, pb in [("A anchor-only/1blk", False, 999), ("B anchor+url/1blk", True, 999), ("C anchor+url/40", True, 40)]:
        rts = await route(pairs, url, wu, pb)
        print(f"  {label:20s} -> routes={len(rts):3d}")
        for r in rts[:7]:
            print(f"       {r['score']:.2f} {r['url'][:80]}")


async def main():
    await go("cintas")
    await go("skyperfect")


asyncio.run(main())
