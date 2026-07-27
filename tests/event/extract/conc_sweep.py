"""Measure 14B-AWQ throughput on the A5000 at increasing concurrency. Fires N identical extraction requests (a
median-size real IR page) concurrently, measures wall time → req/s, pages/hour, output tok/s, avg latency. Shows where
the A5000 saturates (req/s stops rising = compute/KV bound). Run on runpod against the live :8000."""
import os, sys, asyncio, time, json
sys.path.insert(0, "/workspace/WaterEvents")
from agent.event_agent.crawl import prompts
from openai import AsyncOpenAI

K = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
cli = AsyncOpenAI(base_url="http://127.0.0.1:8000/v1", api_key=K, timeout=600)
RF = {"type": "json_schema", "json_schema": {"name": "s", "schema": prompts.EVENTS_SCHEMA, "strict": True}}

# HEAVY workload: a 40-event list so each request generates real output (~40 events ≈ 2-2.5k tokens) — measures the
# actual decode-bound extraction cost, not an empty-response landing page.
_CO = ["Acme", "Globex", "Initech", "Umbrella", "Stark", "Wayne", "Wonka", "Cyberdyne", "Soylent", "Hooli"]
_EV = ["First Quarter Earnings Conference Call", "Board Declares Quarterly Dividend", "Investor and Analyst Day",
       "Annual Meeting of Stockholders", "Files Annual Report on Form 10-K", "Capital Markets Day"]
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
rows = [f"{_MON[k % 12]} {(k % 27) + 1}, {2024 + k % 3} — {_CO[k % 10]} {_EV[(k * 7) % 6]} "
        f"[Press Release](https://ir.x.com/pr/{k}) [PDF](https://ir.x.com/pr/{k}.pdf)" for k in range(1, 41)]
tagged, _ = prompts.tag_links("\n".join(rows))
MSGS = [{"role": "system", "content": prompts.SYSTEM_EVENTS},
        {"role": "user", "content": prompts.build_events_user(tagged, "https://ir.example.com")}]


async def one():
    t = time.time()
    r = await cli.chat.completions.create(model="qwen-vl", messages=MSGS, temperature=0.0,
                                          max_tokens=3000, response_format=RF)
    ct = r.usage.completion_tokens if r.usage else 0
    return time.time() - t, ct


async def sweep(n):
    t = time.time()
    res = await asyncio.gather(*[one() for _ in range(n)])
    wall = time.time() - t
    lat = [x[0] for x in res]
    toks = sum(x[1] for x in res)
    print(f"conc={n:3d}  wall={wall:6.1f}s  req/s={n / wall:5.2f}  pages/hr={n / wall * 3600:6.0f}  "
          f"avg_lat={sum(lat) / len(lat):5.1f}s  out_tok={toks:6d}  tok/s={toks / wall:6.0f}", flush=True)


async def main():
    print(f"workload: 40-event list ({len(tagged)} chars tagged) | model=14B-AWQ on A5000")
    for n in (1, 2, 4, 8, 16, 24, 32):
        await sweep(n)


asyncio.run(main())
