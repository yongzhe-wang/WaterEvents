"""Build the model-comparison eval set from the 1038 real IR pages (ir_official_variants/sample_1000). Rebuild inline
from each page.html, keep {url, inline}, and dump the LONGEST pages (most likely to carry the long event lists where
the 7B collapses) + a random spread → eval_set.jsonl. Run on runpod (html_inline available)."""
import sys, json, glob, os, random

sys.path.insert(0, "/workspace/WaterEvents")
from providers.watercrawl import html_inline

SRC = "/workspace/WaterEvents/tests/ir_official_variants/sample_1000"
OUT = "/workspace/WaterEvents/tests/eval_set.jsonl"

rows = []
for d in sorted(glob.glob(SRC + "/*/")):
    mp = d + "meta.json"
    if not os.path.exists(mp):
        continue
    try:
        url = json.load(open(mp))["url"]
        inline = html_inline.to_inline(open(d + "page.html", encoding="utf-8").read(), url)
    except Exception:                                          # noqa: BLE001 — skip unreadable
        continue
    rows.append({"url": url, "chars": len(inline), "inline": inline})

rows.sort(key=lambda r: -r["chars"])
top = rows[:100]                                               # 100 longest = the collapse-prone long-list pages
rest = rows[100:]
random.seed(7)
sample = top + random.sample(rest, min(50, len(rest)))         # + 50 random for coverage
with open(OUT, "w", encoding="utf-8") as o:
    for r in sample:
        o.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"total pages={len(rows)}  wrote {len(sample)} (100 longest + {len(sample)-100} random) → {OUT}")
print("longest 5 (chars):", [r["chars"] for r in rows[:5]], " median:", rows[len(rows)//2]["chars"])
