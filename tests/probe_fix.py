"""Test whether SAMPLER-level anti-repetition breaks the autoregressive date lock-in on a uniform IR list. Build ONE
~31-row block (same shape as the failing edge_test blocks), send it 4 ways, count how many events come back with a
real (non-empty) date. If repetition_penalty / temperature lifts the count, the lock-in is a sampler rut we can fix in
client.py WITHOUT touching prompts.py or re-enabling the screenshot."""
import os, sys, asyncio, json

sys.path.insert(0, "/workspace/WaterEvents")
KEY = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()

from openai import AsyncOpenAI
from agent.event_agent import prompts

# 31 uniform rows, each with a plain-text date + headline + two short link anchors — the exact shape that locked to
# "date":"" in 17/20 edge_test blocks.
_CO = ["Stark", "Wayne", "Wonka", "Cyberdyne", "Soylent", "Hooli", "Vandelay", "Massive", "Gekko", "Oscorp"]
_EV = ["Fireside Chat at the Goldman Sachs Tech Conference", "Strategic Acquisition Conference Call",
       "Board Declares Quarterly Cash Dividend", "Investor and Analyst Day", "Files Annual Report on Form 10-K",
       "First Quarter Earnings Conference Call", "Capital Markets Day", "Annual Meeting of Stockholders"]
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
def make_block(nrows):
    rows = []
    for k in range(1, nrows + 1):
        rows.append(_MON[k % 12] + " " + str((k % 27) + 1) + ", " + str(2024 + k % 3) + " — " +
                    _CO[k % len(_CO)] + " " + _EV[(k * 7) % len(_EV)] +
                    " [Press Release](https://ir.x.com/pr/" + str(k) + ") [PDF](https://ir.x.com/pr/" + str(k) + ".pdf)")
    return "\n".join(rows)


RF = {"type": "json_schema", "json_schema": {"name": "schema", "schema": prompts.EVENTS_SCHEMA, "strict": True}}


async def run(nrows):
    # temp=0 (production default). Sweep BLOCK SIZE to find the row count at which the model reliably extracts each
    # row's date instead of collapsing to url-only. dated≈nrows = reliable zone.
    tagged, _ = prompts.tag_links(make_block(nrows))
    user = prompts.build_events_user(tagged, "https://ir.x.com")
    cli = AsyncOpenAI(base_url="http://127.0.0.1:8000/v1", api_key=KEY, timeout=180)
    try:
        r = await cli.chat.completions.create(
            model="qwen-vl",
            messages=[{"role": "system", "content": prompts.SYSTEM_EVENTS}, {"role": "user", "content": user}],
            temperature=0.0, max_tokens=4096, response_format=RF)
        obj = json.loads(r.choices[0].message.content)
        evs = obj.get("events") or []
        dated = sum(1 for e in evs if (e.get("date") or "").strip())
        print(f"rows={nrows:3d}   events={len(evs):3d}  dated={dated:3d}  ({'RELIABLE' if dated >= nrows * 0.9 else 'COLLAPSED'})")
    except Exception as e:                                     # noqa: BLE001
        print(f"rows={nrows:3d}   ERROR {type(e).__name__}: {str(e)[:80]}")


async def main():
    print("sweep BLOCK SIZE at temp=0 — find the row count where date extraction stays reliable (dated≈rows):")
    for n in (10, 15, 20, 25, 31):
        await run(n)


asyncio.run(main())
