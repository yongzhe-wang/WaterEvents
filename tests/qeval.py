"""Model-size comparison for the LONG-TEXT COLLAPSE. Runs the SAME extraction prompt (no chunking, single pass) that a
7B collapses on, against whatever Qwen the vLLM endpoint serves, and measures WHERE it collapses:
  SWEEP  — synthetic uniform IR list at 10..60 rows → % events that keep their date (collapse = dated drops to ~0).
  REAL   — the N longest real IR pages (eval_set.jsonl) in ONE pass → events / dated / finish_reason.
Point: does a BIGGER Qwen keep filling date/title on long lists where the 7B bails? Env: EVAL_MODEL, EVAL_BASE, EVAL_KEY.
"""
import os, sys, json, asyncio
from openai import AsyncOpenAI

sys.path.insert(0, os.environ.get("WE_ROOT", "/mnt/data/yongzhe/WaterEvents"))
from agent.event_agent import prompts                          # WaterEvents prompts.py (SYSTEM_EVENTS, tag_links, EVENTS_SCHEMA)

MODEL = os.environ.get("EVAL_MODEL", "qwen")
cli = AsyncOpenAI(base_url=os.environ.get("EVAL_BASE", "http://127.0.0.1:8000/v1"),
                  api_key=os.environ.get("EVAL_KEY", "x"), timeout=900)
RF = {"type": "json_schema", "json_schema": {"name": "s", "schema": prompts.EVENTS_SCHEMA, "strict": True}}

_CO = ["Acme", "Globex", "Initech", "Umbrella", "Stark", "Wayne", "Wonka", "Cyberdyne", "Soylent", "Hooli"]
_EV = ["First Quarter Earnings Conference Call", "Board Declares Quarterly Dividend", "Investor and Analyst Day",
       "Annual Meeting of Stockholders", "Files Annual Report on Form 10-K", "Capital Markets Day",
       "Fireside Chat at the Goldman Sachs Tech Conference", "Fourth Quarter Results Webcast"]
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def make_block(n):
    rows = []
    for k in range(1, n + 1):
        rows.append(_MON[k % 12] + " " + str((k % 27) + 1) + ", " + str(2024 + k % 3) + " — " +
                    _CO[k % len(_CO)] + " " + _EV[(k * 7) % len(_EV)] +
                    " [Press Release](https://ir.x.com/pr/" + str(k) + ") [PDF](https://ir.x.com/pr/" + str(k) + ".pdf)")
    return "\n".join(rows)


async def call(text, max_tokens=4000):
    tagged, _m = prompts.tag_links(text)
    try:
        r = await cli.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": prompts.SYSTEM_EVENTS},
                      {"role": "user", "content": prompts.build_events_user(tagged, "https://ir.example.com")}],
            temperature=0.0, max_tokens=max_tokens, response_format=RF)
        obj = json.loads(r.choices[0].message.content)
        return obj.get("events") or [], r.choices[0].finish_reason
    except Exception as e:                                     # noqa: BLE001
        return [], f"ERR:{type(e).__name__}:{str(e)[:90]}"


async def main():
    print(f"===== MODEL: {MODEL} =====")
    print("--- SWEEP (synthetic uniform list; dated≈rows = no collapse) ---")
    for n in (10, 15, 20, 25, 30, 40, 60):
        evs, fin = await call(make_block(n))
        dated = sum(1 for e in evs if (e.get("date") or "").strip())
        titled = sum(1 for e in evs if (e.get("title") or "").strip())
        flag = "OK" if dated >= n * 0.9 else ("PARTIAL" if dated > 0 else "COLLAPSE")
        print(f"  rows={n:3d}  events={len(evs):3d}  dated={dated:3d}  titled={titled:3d}  finish={fin:6s}  {flag}")

    print("--- REAL (longest pages, ONE pass, no chunking) ---")
    pages = [json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "eval_set.jsonl"))]
    for p in pages[:20]:                                       # 20 longest real pages
        evs, fin = await call(p["inline"][:12000], max_tokens=3500)   # 8192-ctx server: input≤~4k tok + 3.5k out
        dated = sum(1 for e in evs if (e.get("date") or "").strip())
        print(f"  chars={p['chars']:6d}  events={len(evs):3d}  dated={dated:3d}  finish={fin:6s}  {p['url'][:48]}")


asyncio.run(main())
