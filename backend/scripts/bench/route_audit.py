import json, sys
from collections import Counter
r = json.load(open(sys.argv[1]))
print("events:", len(r["events"]))
sc = Counter(round(x.get("score", -1), 2) for x in r["routes"])
print("n_routes:", len(r["routes"]), " score distribution:", dict(sc))
print("--- real EVENT-detail urls that got dumped into ROUTES (should be event.urls) ---")
for x in r["routes"]:
    u = x["url"]
    if any(k in u for k in ["earnings", "press-release", "news.microsoft", "dividend", "events/fy"]):
        print("  score=%s  %s" % (x.get("score"), u))
print("--- footer/off-site urls still in routes (should be filtered/low) ---")
for x in r["routes"]:
    u = x["url"]
    if any(k in u for k in ["facebook", "x.com", "linkedin", "youtube", "computershare", "icsdelivery"]):
        print("  score=%s  %s" % (x.get("score"), u))
