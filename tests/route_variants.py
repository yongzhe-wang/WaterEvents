"""Brainstorm + PARALLEL test of routing-prompt variants against the real routing failures found in sample_10:
  skyperfectjsat — bulk-routes 60 links all @0.90 (no discrimination)
  stone          — routes 3 api.mziq.com file downloads (assets, not nav)
  autodesk       — mostly correct (news-events/filings) — a variant must NOT regress this
Each variant is a SYSTEM_ROUTES string tested WITHOUT touching prompts.py (the user owns that file). For every
(variant × page) we fire the routing call, normalize, and report route count + junk leaks + real-section hits, so the
best wording can be picked and then adopted into prompts.py. Run on the pod: python tests/route_variants.py
"""
import os, sys, glob, json, asyncio

sys.path.insert(0, "/workspace/WaterEvents")
os.environ.setdefault("QWEN_BASE_URLS", "http://127.0.0.1:8000/v1")
os.environ.setdefault("QWEN_SERVED_NAME", "qwen-vl")
os.environ["QWEN_API_KEY"] = [l for l in open("/workspace/vllm.env") if "QWEN_API_KEY" in l][0].split("=", 1)[1].strip()
os.environ.setdefault("QWEN_MAX_TOKENS", "4096")

from providers.watercrawl import html_inline
from providers.qwen_llm import QwenClient
from agent.event_agent import prompts, extract

SRC = "/workspace/WaterEvents/tests/ir_official_variants/sample_10"

# ── variant SYSTEM_ROUTES prompts (V0 = current baseline; V1-V4 = the brainstormed directions) ──
BASE = prompts.SYSTEM_ROUTES
V1 = BASE + "\n\nSELECTION CAP — Emit AT MOST ~12 routes. A real IR page has ~5-12 event SECTIONS, not 60. If you are " \
     "about to score more than ~12 links you are BULK-ROUTING chrome — STOP and keep ONLY the clearest event-section " \
     "hubs. Never emit dozens of links at the same score."
V2 = BASE + "\n\nNEVER ROUTE (hard): (a) a link to ONE specific dated item / press release / a single filing or a " \
     "single presentation PDF — that is a LEAF; (b) ANY file download or API/CDN url (.pdf, /files/, /filemanager/, " \
     "/static-files/, /documents/, api.*, cdn.*, *.zip) — files are not navigation; (c) a DEEP SUB-PAGE of a section " \
     "you already routed — route the SECTION hub ONCE, not each of its children."
V3 = """You are given a company Investor-Relations page's URL and a LIST of its links as `Lnn — anchor text`. Output \
STRICT JSON {"routes":[{"ref","score"}]} — the section links a crawler should FOLLOW to reach MORE events. Copy each \
Lnn verbatim; never invent one.

ROUTE ONLY links whose anchor names one of these EVENT SECTIONS (the crawler opens these to find event listings):
  Events · Events & Presentations · Events & Webcasts · IR Calendar / Investor Calendar · Webcasts · News · \
Press Releases · Financial Results / Quarterly Results / Financial Reports · SEC Filings / Regulatory Filings / Filings \
· Presentations · Annual Meeting / Shareholder Meeting · pagination (?page=2) · year/archive links (/2023).
Score these 0.8-1.0 (SEC Filings always HIGH). A Governance / Board page → MID 0.4-0.6 (leads to the proxy + AGM).

OMIT EVERYTHING ELSE — do not emit it even at a low score. In particular OMIT: About / Company / Leadership / \
Careers / Contact / Login / Search / Privacy / Terms / Cookies / Sitemap / FAQ / Email-Alerts / Disclosure-policy / \
individual-investor explainers / stock-chart / analyst-coverage widgets / a single dated press release or filing or \
PDF (that is a LEAF, not a section) / any file download or API/CDN url (.pdf, /files/, /filemanager/, /static-files/, \
api.*, cdn.*) / any link that leaves the IR site for the marketing/consumer/product www site.

A page's real event sections number ~5-12, not 60. If you cannot tell a link is one of the sections above, OMIT it.
Output STRICT JSON only: {"routes": [{"ref": "L7", "score": 0.9}]}  — empty {"routes": []} if none qualify."""
V4 = BASE + "\n\nSCORE SPREAD (mandatory): 0.8-1.0 ONLY for PRIMARY event sections (Events, Webcasts, Press Releases, " \
     "News, Financial Results, SEC Filings, Presentations, IR Calendar, Annual Meeting); 0.3-0.5 for a Governance/Board " \
     "or Annual-Report hub; OMIT everything else. Do NOT give the SAME score to more than ~8 links — if you are, you " \
     "failed to discriminate. Route each SECTION ONCE, never its individual sub-pages or dated items."
V5 = """You are given a company IR page's URL and a LIST of its links as `Lnn — anchor text`. Output STRICT JSON \
{"routes":[{"ref","score"}]} — the section links a crawler should FOLLOW to reach MORE events. Copy each Lnn verbatim.

ROUTE a link when EITHER its anchor OR its URL PATH marks it an IR EVENT SECTION (judge the URL PATH too, so a \
non-English or generic anchor still routes): path/anchor contains events, calendar, webcast, news, press / press-release, \
results, financial-results / financial-reports / quarterly, filings / sec-filings, presentations, ir_news, annual or \
shareholder meeting, pagination (?page=), or a year archive (/2023). Score these HIGH 0.8-1.0 (SEC Filings always HIGH). \
A governance / board page → MID 0.4-0.6.

DISCRIMINATE — do NOT give every link the same score. OMIT (do not emit at ANY score):
  • chrome: about, company, leadership, careers, contact, login, search, sitemap, faq, glossary, email-alerts, \
disclosure-policy, individual-investor explainers, stock chart / quote / price-lookup, analyst-coverage widgets, recruit
  • a SINGLE dated press release / filing / presentation (a LEAF, not a section)
  • any file / social / CDN url (.pdf, /files/, /filemanager/, /static-files/, api.*, cdn.*, storage.*, x.com, twitter, \
linkedin, youtube, facebook, instagram)
  • the marketing / consumer www site (products, shop, sustainability-marketing)
ROUTE EACH SECTION ONCE — if you routed a hub (…/ir/library), do NOT also route its children UNLESS a child is itself a \
distinct event section (…/library/presentation, …/library/event → keep; …/library/report, …/library/statement → drop, \
the hub covers them).

A real IR page has ~5-12 sections, not 60. But do NOT return empty on a page that clearly has IR links — if unsure, \
route the 4-8 most section-like links. Output STRICT JSON: {"routes":[{"ref":"L7","score":0.9}]}."""
# (name, system_prompt, use_path_input) — compare anchor-only vs anchor+path input on the whitelist variants
VARIANTS = [("V0_base", BASE, False), ("V3_anchor", V3, False), ("V5_anchor", V5, False),
            ("V3_PATH", V3, True), ("V5_PATH", V5, True)]

import re
# Deterministic STRUCTURAL non-route filter (belt for any prompt): file/asset/CDN/social hosts are NEVER navigation.
_JUNK_RE = re.compile(
    r"\.pdf($|\?)|/static-files/|/filemanager/|/mzfilemanager/|storage\.googleapis|//api\.|//cdn\."
    r"|(x\.com|twitter\.com|linkedin\.com|youtube\.com|facebook\.com|instagram\.com)/"
    r"|/recruit|/faq($|/)|/glossary|/aboutsite|/sns($|/)|/contact_ir", re.I)

# ── test pages (slug substr → junk substrings that must NOT be routed) ──
PAGES = {
    "skyperfectjsat": {"junk": ["/policy/message", "/individual/3minutes", "/ir/mail", "/company/outline", "sustainability"]},
    "investors.stone": {"junk": ["mziq.com", "filemanager"]},
    "investors.autodesk": {"junk": ["static-files", "/static-files/"]},
}


from urllib.parse import urlsplit


def _link_list_path(inline):
    """Like prompts.link_list but each line is `Lnn — anchor — /url/path` so the model can judge by URL PATH when the
    anchor is non-English / generic (the anchor-only list starved the whitelist → 0 routes on the JP skyperfectjsat page)."""
    tag_map, u2id, lines = {}, {}, []
    for m in prompts._INLINE_LINK_RE.finditer(inline):
        anchor, url = (m.group(1) or "").strip(), m.group(2)
        if url in u2id:
            continue
        rid = f"L{len(u2id) + 1}"; u2id[url] = rid; tag_map[rid] = url
        lines.append(f"{rid} — {anchor or '(no text)'} — {urlsplit(url).path or '/'}")
    return "\n".join(lines), tag_map


def _link_block(slug_sub):
    d = [x for x in glob.glob(SRC + "/*/") if slug_sub in x][0]
    url = json.load(open(d + "meta.json"))["url"]
    inline = html_inline.to_inline(open(d + "page.html", encoding="utf-8").read(), url)
    lb_anchor, tmap = prompts.link_list(inline)               # current format: anchor only
    lb_path, _ = _link_list_path(inline)                      # proposed format: anchor + url path
    return url, lb_anchor, lb_path, tmap


async def run_variant(c, vname, vsys, use_path, slug_sub, url, lb_anchor, lb_path, tmap):
    lb = lb_path if use_path else lb_anchor
    res = await c.send_one(system=vsys, user=prompts.build_routes_user(url, lb), guided_json=prompts.ROUTES_SCHEMA)
    routes = extract._normalize_routes(res, tmap)
    junk = PAGES[slug_sub]["junk"]
    leaked = [r["url"] for r in routes if any(j in r["url"] for j in junk)]
    filt = [r for r in routes if not _JUNK_RE.search(r["url"])]     # + deterministic structural junk removal
    leaked_f = [r["url"] for r in filt if any(j in r["url"] for j in junk)]
    return vname, slug_sub, len(routes), len(leaked), len(filt), len(leaked_f), filt


async def main():
    c = QwenClient()
    pages = {s: _link_block(s) for s in PAGES}
    print("total links per page:", {s: pages[s][1].count(chr(10)) + 1 for s in pages})
    jobs = []
    for vname, vsys, use_path in VARIANTS:
        for slug_sub, (url, lb_a, lb_p, tmap) in pages.items():
            jobs.append(run_variant(c, vname, vsys, use_path, slug_sub, url, lb_a, lb_p, tmap))
    results = await asyncio.gather(*jobs)

    print(f"\n{'variant':14s} {'page':16s} {'raw#':>5s} {'rawJunk':>7s} {'+filt#':>6s} {'filtJunk':>8s}")
    by_v = {}
    for vname, slug, nroutes, nleak, nfilt, nleakf, filt in results:
        print(f"{vname:14s} {slug:16s} {nroutes:5d} {nleak:7d} {nfilt:6d} {nleakf:8d}")
        by_v.setdefault(vname, []).append((slug, nfilt, nleakf, filt))
    # dump each variant's post-filter routes for eyeball audit
    OUT = "/workspace/WaterEvents/tests/route_variants_out"
    os.makedirs(OUT, exist_ok=True)
    for vname, rows in by_v.items():
        with open(f"{OUT}/{vname}.txt", "w", encoding="utf-8") as o:
            for slug, nfilt, nleakf, filt in rows:
                o.write(f"### {slug}  routes_after_filter={nfilt}  junk_leaked={nleakf}\n")
                for r in sorted(filt, key=lambda z: -z["score"]):
                    o.write(f"  {r['score']:.2f}  {r['url']}\n")
                o.write("\n")
    print(f"\nfull routes per variant → {OUT}/<variant>.txt")


asyncio.run(main())
