"""ONE synthetic big-txt edge-case test for the Lnn + simple-chunking extract pipeline. Text-only (NO_SHOT). Covers:
clean events, multi-url events, empty-title-with-date (case c), the 7 route traps (sitemap/rss/shop/about/careers/
login/social/asset), real IR-section routes, the have-url rule (url-less rows dropped), an over-cap mega-list that
MUST trigger fixed-size chunking, and a feed-url-only event that must drop. Run on the pod:  python tests/edge_test.py
"""
import os
import sys
import asyncio
import time

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")
os.environ.setdefault("WATERCRAWL_NO_SHOT", "1")
os.environ.setdefault("EVENT_MAX_INPUT_CHARS", "48000")     # cap → the mega-list below exceeds it → over-cap chunking fires
os.environ.setdefault("QWEN_CONCURRENCY", "16")

from agent.event_agent.crawl import extract

# --- controlled edge-case header (small, one pass would handle it — but the mega-list below pushes the whole page over cap) ---
HEADER = "\n".join([
    "Investor Relations — Events, News & Filings",
    # clean multi-url event (earnings): page + webcast + slides
    "[Q1 2026 Earnings Conference Call](https://ir.acme.com/q1) Feb 05, 2026 [Webcast](https://ir.acme.com/wc/q1) [Slides](https://ir.acme.com/f/q1.pdf)",
    # dividend event
    "[Acme Declares Quarterly Dividend](https://ir.acme.com/div-q1) March 10, 2026",
    # empty-title-but-date (case c) — should SURVIVE (has a date)
    "[details](https://ir.acme.com/e/uncat-1) April 22, 2026",
    # feed-url-only 'event' — its ONLY url is an rss feed → _clean_urls drops it → 0 urls → event DROPPED (have-url rule)
    "[Subscribe to our RSS](https://acme.com/rss/news.xml) May 1, 2026 Some Feed",
    # url-less dated row — no link at all → no url → DROPPED (have-url rule)
    "Some Undated Boilerplate Text With No Link And No Real Event",
    "",
    "SECTIONS:",
    "[News and Events](https://ir.acme.com/news-events)",          # real IR route
    "[IR Calendar](https://ir.acme.com/calendar)",                # real IR route
    "[SEC Filings](https://ir.acme.com/filings)",                 # real IR route
    "TRAPS (must ALL be dropped from routes):",
    "[Sitemap](https://acme.com/sitemap.xml)",                    # feed/asset trap
    "[RSS Feed](https://acme.com/rss/feed.xml)",                  # feed trap
    "[Shop iPhone](https://acme.com/shop/iphone)",                # product/marketing trap
    "[About Us](https://acme.com/about)",                         # nav trap
    "[Careers](https://acme.com/careers)",                        # nav trap
    "[Login](https://acme.com/account/login)",                    # account trap
    "[Follow us on Facebook](https://facebook.com/acme)",         # social/external trap
    "[company logo](https://acme.com/content/dam/img/logo.png)",  # asset trap
    "",
    "ALL PRESS RELEASES:",
])

# --- mega-list: 400 dated events WITH links, each title UNIQUE (company × event × venue × date rotate) so the model
# can't repetition-loop on near-identical rows the way an 8-topic rotation did → this exercises real over-cap chunking,
# not a synthetic degenerate-generation artifact. Total ≈ 62k chars > 48000 cap → SIMPLE fixed-size chunking must fire.
_CO = ["Acme", "Globex", "Initech", "Umbrella", "Stark", "Wayne", "Wonka", "Cyberdyne", "Soylent", "Hooli",
       "Vandelay", "Massive", "Gekko", "Prestige", "Oscorp", "Tyrell", "Aviato", "Piedpiper", "Duff", "Bluth"]
_EV = ["First Quarter Earnings Conference Call", "Second Quarter Earnings Conference Call",
       "Third Quarter Earnings Conference Call", "Fourth Quarter and Full Year Results Webcast",
       "Annual Meeting of Stockholders", "Investor and Analyst Day", "Board Declares Quarterly Cash Dividend",
       "Presentation at the JPMorgan Healthcare Conference", "Fireside Chat at the Goldman Sachs Tech Conference",
       "Keynote at the Morgan Stanley Consumer Conference", "Files Annual Report on Form 10-K",
       "Files Quarterly Report on Form 10-Q", "Capital Markets Day", "Preliminary Sales Results Announcement",
       "New Product Launch Investor Briefing", "Strategic Acquisition Conference Call"]
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_rows = []
for k in range(1, 401):
    co = _CO[k % len(_CO)]
    ev = _EV[(k * 7) % len(_EV)]                 # *7 stride → event type decorrelates from company → maximally varied
    yr = 2024 + (k % 3)
    day = (k % 28) + 1
    title = co + " " + ev                         # headline as PLAIN TEXT (real IR row shape), not buried in the anchor
    date = _MON[k % 12] + " " + str(day) + ", " + str(yr)   # date as PLAIN TEXT before the headline
    row = (date + " — " + title +                 # date + headline in the open → model extracts date+type (survives guard)
           " [Press Release](https://ir." + co.lower() + ".com/pr/" + str(k) + ")" +   # links are SEPARATE short anchors
           " [PDF](https://ir." + co.lower() + ".com/pr/" + str(k) + ".pdf)")
    _rows.append(row)
BIG = HEADER + "\n" + "\n".join(_rows)


async def main():
    print("=== EDGE TEST: one big synthetic page ===")
    print("total chars:", len(BIG), "(cap is", os.environ["EVENT_MAX_INPUT_CHARS"], "→ over-cap chunking should fire)")
    t = time.time()
    r = await extract.extract_page({"page_url": "https://ir.acme.com", "page_text": BIG, "image_b64": None, "links_block": ""}, use_image=False)
    dt = time.time() - t
    evs, rts = r.get("events", []), r.get("routes", [])
    print("latency=%.1fs  events=%d  routes=%d  err=%s" % (dt, len(evs), len(rts), r.get("_error")))

    # every event MUST carry a url (have-url rule)
    no_url = [e for e in evs if not e.get("urls")]
    print("events with NO url (should be 0 — have-url rule):", len(no_url))

    # traps must NOT appear in routes; real IR sections should
    route_urls = [x["url"] for x in rts]
    TRAPS = ["sitemap.xml", "rss/feed", "shop/iphone", "/about", "/careers", "/login", "facebook.com", "content/dam"]
    leaked = [t for t in TRAPS if any(t in u for u in route_urls)]
    REAL = ["news-events", "calendar", "filings"]
    kept = [t for t in REAL if any(t in u for u in route_urls)]
    print("TRAP routes leaked (should be []):", leaked)
    print("REAL IR routes kept (of 3):", kept)

    # feed-only event dropped? url-less row dropped? mega-list captured?
    all_ev_urls = " ".join(u for e in evs for u in e.get("urls", []))
    print("feed-only event survived? (should be False):", "rss/news.xml" in all_ev_urls)
    mega_hits = sum(1 for e in evs if "/pr/" in " ".join(e.get("urls", [])))
    print("mega-list events captured (of 400):", mega_hits)
    print("sample events:")
    for e in evs[:4]:
        print("   T:", e["title"][:40], "| d:", e["date"], "| ty:", e["type"], "| urls:", e["urls"])


if __name__ == "__main__":                                    # guard so dump_review.py can import BIG without running the test
    asyncio.run(main())
