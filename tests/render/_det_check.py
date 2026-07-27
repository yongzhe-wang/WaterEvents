"""_det_check — throwaway: render an event url + run extract_html deterministically (NO VLM) to eyeball the body/table/
transcript detection before the full pipeline run. Usage: DET_URL=<url> python tests/_det_check.py"""
import os
from providers import watercrawl
from agent.media_agent.extract_html import extract_html

url = os.environ.get("DET_URL",
    "https://ir.prologis.com/news-events/press-releases/detail/1044/prologis-reports-second-quarter-2026-results")
r = watercrawl.render_shot(url)                                  # real browser render → {html,text,links,method,...}
html = r.get("html", "")
print(f"render: method={r.get('method')} html_chars={len(html)} text_chars={len(r.get('text') or '')} links={len(r.get('links') or [])}")

det = extract_html(html, base_url=url, links=r.get("links"))     # the deterministic body extractor under test
blocks = det["blocks"]
tabs = [b for b in blocks if b.get("type") == "table"]
mds = [b for b in blocks if b.get("type") == "md"]
lists = [b for b in blocks if b.get("type") == "list"]
print(f"tier={det['tier']}  blocks={len(blocks)} (md={len(mds)} list={len(lists)} table={len(tabs)})  "
      f"transcript_idx={det['transcript_idx']}  candidate_urls={len(det['candidate_urls'])}")
print("--- first 4 blocks (head) ---")
for b in blocks[:4]:
    if b.get("type") == "table":
        print(f"  [TABLE] headers={b['headers'][:4]} rows={len(b['rows'])}")
    else:
        print(f"  [{b['type']}] {(b.get('md') or '')[:100]!r}")
if tabs:
    t = tabs[0]
    print("--- first financial TABLE (headers + first 2 rows) ---")
    print("  headers:", t["headers"][:6])
    for row in t["rows"][:2]:
        print("  row:", row[:6])
