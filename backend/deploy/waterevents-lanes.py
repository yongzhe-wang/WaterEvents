import json, sys
try:
    t = json.load(sys.stdin).get("tenants") or {}
    b = sum((t.get("browser") or {}).get("inflight", {}).values() or [0])
    f = sum((t.get("fetch") or {}).get("inflight", {}).values() or [0])
    print("br=%g fe=%g" % (b, f))
except Exception:
    print("br=? fe=?")
