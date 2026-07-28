"""_det_check_all — deterministic-extraction quality sweep across the 10-event dataset, WITHOUT touching the VLM. For each
event: classify the url; a .pdf/office url is reported as OFFICE (Docling would handle it); an html url is rendered (CPU
browser, NO GPU) and run through extract_html → report tier / block mix / table count / transcript flags / candidate urls.
用一句话讲完: 读 dataset → 每个 event 只做 render + trafilatura/pandas(deterministic),打印 body 抽取质量,证明
basic_info 不再依赖 VLM copy —— 全程零 vLLM 调用。 Usage: DET_DATASET=/home/thebigsun/10events_dataset python tests/_det_check_all.py"""
import glob
import json
import os

# Paths repaired 2026-07-28. The a4542aa restructure moved both modules into the extract/ subpackage and this harness
# kept the pre-restructure paths, so it raised ModuleNotFoundError on import and had not run since — silently, because
# nothing in CI (there is no CI) or in any script invokes it.
# {AST IMPORT SCAN 2026-07-28 "TESTS/RENDER/_DET_CHECK_ALL.PY — LEGACY IMPORT AGENT.MEDIA_AGENT.EXTRACT_HTML"}
from agent.media_agent.extract import router
from agent.media_agent.extract.extract_html import extract_html

_DATASET = os.environ.get("DET_DATASET", "/home/thebigsun/10events_dataset")
_OFFICE = (router.KIND_PDF, router.KIND_PPTX, router.KIND_DOCX, router.KIND_XLSX)


def main() -> None:
    from providers import watercrawl                            # lazy: browser only where render runs
    files = sorted(glob.glob(os.path.join(_DATASET, "ev*.json")))
    print(f"[det] {len(files)} events | dataset={_DATASET} | NO VLM — render + trafilatura/pandas only\n")
    for fp in files:
        entry = json.load(open(fp, encoding="utf-8"))
        url = entry["event_url"]
        kind = router.classify(url)
        if kind in _OFFICE:                                     # a document → Docling's job, not extract_html; skip render
            print(f"  {entry['id']:6} [{kind:5}] OFFICE → Docling handles it (no html render)  {url[:70]}")
            continue
        r = watercrawl.render_shot(url)                         # CPU browser render — NO vLLM
        html = r.get("html", "")
        det = extract_html(html, base_url=url, links=r.get("links"))
        blocks = det["blocks"]
        nt = sum(1 for b in blocks if b.get("type") == "table")
        nm = sum(1 for b in blocks if b.get("type") == "md")
        nl = sum(1 for b in blocks if b.get("type") == "list")
        rows = sum(len(b.get("rows") or []) for b in blocks if b.get("type") == "table")
        print(f"  {entry['id']:6} [{kind:5}] tier={det['tier']:11} blocks={len(blocks):3} "
              f"(md={nm} list={nl} table={nt}/{rows}rows)  transcript_idx={det['transcript_idx']}  "
              f"cand_urls={len(det['candidate_urls'])}  method={r.get('method','')}  html={len(html)}c")


if __name__ == "__main__":
    main()
