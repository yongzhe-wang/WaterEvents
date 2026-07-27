# One-off verification: crawl ONLY the MSFT IR landing page (the footer-junk page) with the CURRENT
# semantic-gate prompt + date-or-title backstop, and print the event list so we can eyeball whether any
# footer chrome (Surface/Azure/Education/Follow us/Careers) still leaked in as a fake event.
import asyncio, sys
from agent.event_agent import crawl_company

async def main():
    url = "https://www.microsoft.com/en-us/investor/default"
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    r = await crawl_company(url, max_pages=n)          # current code path: semantic HARD GATE + _normalize backstop
    print(f"=== MSFT verify: {len(r['events'])} events over {r['pages']} pages ===", flush=True)
    for e in sorted(r["events"], key=lambda x: x.get("date") or "", reverse=True):
        print(f"{(e.get('date') or '-'):12} | {(e.get('type') or '-'):16} | {(e.get('title') or '')[:72]}", flush=True)
        for u in e.get("urls", [])[:3]:
            print(f"        {u}", flush=True)
    print(f"=== routes: {len(r.get('routes', []))} ===", flush=True)
