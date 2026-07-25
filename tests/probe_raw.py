"""Minimal probe — isolate the Lnn round-trip from chunking. Feed a SMALL page (header + 20 mega rows, well under cap →
single pass, no chunk, no truncation) and dump the RAW model reply vs the normalized result, so we can see whether the
model emits events at all for this row format and, if so, whether _normalize/resolve_ids drops them."""
import os, sys, asyncio, json

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")
os.environ.setdefault("WATERCRAWL_NO_SHOT", "1")

from agent.event_agent import extract, prompts
from providers.qwen_llm import QwenClient

# REALISTIC IR row structure: headline as PLAIN TEXT + date as PLAIN TEXT + links are SEPARATE short anchors
# ([Press Release]/[PDF]), NOT the whole headline buried inside one anchor. This is what a real IR press-release list
# looks like — and what the SYSTEM prompt's "copy the visible headline verbatim" rule expects.
_TOPICS = ["First Quarter Earnings Conference Call", "Board Declares Quarterly Dividend", "Investor and Analyst Day",
           "Annual Meeting of Stockholders"]
rows = []
for k in range(1, 21):
    rows.append("May " + str(k) + ", 2026 — Acme " + _TOPICS[k % len(_TOPICS)] +
                " [Press Release](https://ir.acme.com/pr/" + str(k) + ") [PDF](https://ir.acme.com/pr/" + str(k) + ".pdf)")
PAGE = ("Investor Relations — Events\n"
        "Feb 05, 2026 — Q1 2026 Earnings Conference Call [Press Release](https://ir.acme.com/q1) [Webcast](https://ir.acme.com/wc/q1)\n"
        "[News and Events](https://ir.acme.com/news-events)\n"
        "ALL PRESS RELEASES:\n" + "\n".join(rows))


async def main():
    print("page chars:", len(PAGE))
    tagged, tag_map = prompts.tag_links(PAGE)
    print("tag_map size:", len(tag_map), " sample:", list(tag_map.items())[:3])
    print("--- tagged text (first 600) ---")
    print(tagged[:600])
    job = extract._build_job(tagged, "https://ir.acme.com", "", None, False)
    res = (await QwenClient().send_many([job]))[0]
    print("--- RAW model reply ---")
    print("keys:", list(res.keys()), " __finish__:", res.get("__finish__"), " _error:", res.get("_error"))
    print("raw events count:", len(res.get("events") or []), " raw routes count:", len(res.get("routes") or []))
    print("first 3 raw events:", json.dumps((res.get("events") or [])[:3], ensure_ascii=False))
    print("first 3 raw routes:", json.dumps((res.get("routes") or [])[:3], ensure_ascii=False))
    norm = extract._normalize(res, tag_map)
    print("--- NORMALIZED ---")
    print("events:", len(norm["events"]), " routes:", len(norm["routes"]))
    print("first 3 norm events:", json.dumps(norm["events"][:3], ensure_ascii=False))


asyncio.run(main())
