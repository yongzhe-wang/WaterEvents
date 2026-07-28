"""media_agent smoke — run the enrichment endpoint over the real 10-event dataset ON RUNPOD and dump the FULL trace to
txt for eyeball debugging.

用一句话讲完: 读 tests/media_dataset/*.json(10 个真实 IR event: known metadata + 真实 media_urls 作 oracle)→ 每个
event 用 watercrawl render 详情页(拿 page_text + 截图)→ enrich_page 打真实 RunPod VLM → 为每个 event 写一份完整
trace.txt(known event / 渲染信息 / FULL PROMPT / RAW VLM OUTPUT / enriched 结果 / oracle 对比:ground-truth media
被 urls[] 覆盖了几个)。**用户要 "full prompt + all the trace for the txt so i can clearly debug"** — QwenClient 的
QWEN_DEBUG_DIR 自动把每请求的完整 prompt+raw 落 req_*.txt, 本文件再叠一层可读汇总。

Run ON RUNPOD (browser + VLM co-located; QWEN_BASE_URLS 指向本机 vLLM):
  cd /workspace/WaterEvents && QWEN_BASE_URLS=http://127.0.0.1:8000/v1 python3 -m agent.media_agent.smoke
Optional: MEDIA_SMOKE_LIMIT=3 只跑前 3 个快速验证。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os

from providers.qwen_llm import config as qcfg      # mutate DEBUG_DIR per-event so each event's req_*.txt is separated

from agent.media_agent.pipeline.enrich import enrich_page    # absolute: this file lives in tests/, not in the package

_DATASET = os.environ.get("MEDIA_DATASET_DIR",              # reusable oracle dataset (moved under tests/datasets/ 2026-07-23)
                          os.path.join(os.path.dirname(__file__), "..", "..", "tests", "datasets", "ir_events_media_oracle"))
_OUTDIR = os.environ.get("MEDIA_SMOKE_OUT",                 # regenerable smoke output (gitignored, NOT a dataset)
                         os.path.join(os.path.dirname(__file__), "..", "..", "tests", "media_output"))
_LIMIT = int(os.environ.get("MEDIA_SMOKE_LIMIT", "0"))       # 0 = all
_USE_IMAGE = os.environ.get("MEDIA_USE_IMAGE", "1") not in ("0", "false", "no")


async def _render(url: str) -> dict | None:
    """Open the event detail page with watercrawl (in a thread — render_shot is sync). Returns {page_url,page_text,
    image_b64,method,n_links} or None when the render came back empty (dead/moved page — logged, not fatal)."""
    from providers import watercrawl                          # lazy: playwright/chromium only exist on RunPod
    r = await asyncio.to_thread(watercrawl.render_shot, url)
    if not r.get("text") and not r.get("links"):
        return None
    return {"page_url": url, "page_text": r.get("inline") or r.get("text", ""),
            "image_b64": r.get("shot_b64", ""), "method": r.get("method", ""), "n_links": len(r.get("links", []))}


def _render_blocks(basic_info: list) -> str:
    """Render basic_info blocks into readable text for the trace — md/list verbatim, table as a small ascii grid."""
    out = []
    for b in basic_info:
        t = b.get("type")
        if t in ("md", "list"):
            out.append(b.get("md", ""))
        elif t == "table":
            hdr = " | ".join(b.get("headers", []))
            rows = "\n".join(" | ".join(map(str, r)) for r in b.get("rows", []))
            out.append(f"[TABLE] {b.get('caption','')}\n{hdr}\n{'-'*len(hdr)}\n{rows}")
    return "\n\n".join(out)


def _oracle(enriched_urls: list, ground_truth: list) -> tuple[int, int, list]:
    """Compare enriched urls[] against the dataset's ground-truth media (from the old crawl). Match by url substring
    (host+path tail) so a re-hosted variant still counts. Returns (found, total, missing-with-label)."""
    got = set(enriched_urls)
    found, missing = 0, []
    for m in ground_truth:
        gu = m["url"]
        hit = gu in got or any(gu.split("://")[-1] in u or u.split("://")[-1] in gu for u in got)
        if hit:
            found += 1
        else:
            missing.append(f"{m.get('label','?')}: {gu}")
    return found, len(ground_truth), missing


def _write_trace(path: str, entry: dict, page: dict | None, enriched: dict | None, note: str = "") -> None:
    """Write ONE event's full readable trace: known event / render info / (the raw prompt+output live in the sibling
    req_*.txt from QWEN_DEBUG_DIR) / enriched metadata+basic_info+transcript+urls / oracle comparison."""
    L = []
    L.append(f"===== EVENT {entry['id']} =====")
    L.append(f"event_url: {entry['event_url']}")
    L.append(f"\n--- KNOWN EVENT (input reference) ---\n{json.dumps(entry['known_event'], ensure_ascii=False, indent=2)}")
    L.append("\n--- GROUND-TRUTH MEDIA (oracle, from old crawl) ---")
    for m in entry["ground_truth_media"]:
        L.append(f"  [{m.get('label','?'):11}] {m['url']}")
    if note:
        L.append(f"\n!!! {note}")
    if page:
        L.append(f"\n--- RENDER ---\nmethod={page['method']}  page_text_chars={len(page['page_text'])}  "
                 f"screenshot={'yes' if page['image_b64'] else 'no'}  links_on_page={page['n_links']}")
        L.append(f"\n--- PAGE TEXT (first 1200 chars fed to VLM) ---\n{page['page_text'][:1200]}")
        L.append("\n(FULL prompt + RAW VLM OUTPUT for every request → the req_*.txt in this same folder, via QWEN_DEBUG_DIR)")
    if enriched:
        L.append(f"\n--- ENRICHED: metadata ---\ntitle: {enriched['title']}\ndate: {enriched['date']}\ntype: {enriched['type']}")
        L.append(f"output_truncated: {enriched['output_truncated']}  {'⛔ NOT COMPLETE' if enriched['output_truncated'] else '✓'}")
        L.append(f"\n--- ENRICHED: urls[] ({len(enriched['urls'])}) ---")
        for u in enriched["urls"]:
            L.append(f"  {u}")
        found, total, missing = _oracle(enriched["urls"], entry["ground_truth_media"])
        L.append(f"\n--- ORACLE: ground-truth media coverage = {found}/{total} ---")
        for mm in missing:
            L.append(f"  MISSING {mm}")
        L.append(f"\n--- ENRICHED: transcript_segments ({len(enriched['transcript_segments'])}) ---")
        for s in enriched["transcript_segments"][:20]:
            L.append(f"  [{s['speaker']}] {s['text'][:120]}")
        L.append(f"\n--- ENRICHED: basic_info ({len(enriched['basic_info'])} blocks) ---\n{_render_blocks(enriched['basic_info'])}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


async def main() -> None:
    files = sorted(glob.glob(os.path.join(_DATASET, "ev*.json")))
    if _LIMIT:
        files = files[:_LIMIT]
    os.makedirs(_OUTDIR, exist_ok=True)
    print(f"[smoke] {len(files)} events | dataset={_DATASET} | out={_OUTDIR} | use_image={_USE_IMAGE}", flush=True)
    summary = []
    for fp in files:
        entry = json.load(open(fp, encoding="utf-8"))
        ev_dir = os.path.join(_OUTDIR, entry["id"])
        os.makedirs(ev_dir, exist_ok=True)
        qcfg.DEBUG_DIR = ev_dir                               # QwenClient dumps THIS event's req_*.txt (full prompt+raw) here
        trace_path = os.path.join(ev_dir, "trace.txt")

        page = await _render(entry["event_url"])
        if page is None:                                     # dead/moved page — record loudly, keep going
            _write_trace(trace_path, entry, None, None, note="RENDER EMPTY — page dead/moved/walled")
            print(f"  ⛔ {entry['id']:13} render empty — skipped", flush=True)
            summary.append((entry["id"], "render-empty", 0, len(entry["ground_truth_media"]), False))
            continue

        enriched = await enrich_page(entry["known_event"], page, use_image=_USE_IMAGE)
        _write_trace(trace_path, entry, page, enriched)
        found, total, _ = _oracle(enriched["urls"], entry["ground_truth_media"])
        # STATUS TAXONOMY — a rendered page can still come back DEGRADED two ways, and BOTH used to print "✓ ok" (silent
        # degradation, exactly what the user's "fallback must NEVER silently degrade quality" directive forbids):
        #   (1) output_truncated=True → enrich.py flagged a HARD FAILURE — either a VLM `_error` (connection/5xx, the
        #       ev04_GILD case: render fine + image present but RAW empty + ERROR="Connection error.") or a mega-page
        #       finish=length. Either way the record is NOT trustworthy. {ENRICH.PY:136 "acc['output_truncated']=True"}.
        #   (2) not truncated but the page rendered WITH content yet the VLM returned ZERO basic_info AND ZERO transcript
        #       AND matched ZERO oracle media → an empty enrichment on a non-empty page = a quality drop worth shouting,
        #       distinct from a page that genuinely has nothing.
        # {USER 2026-07-23 "fail loudly + fallback must NEVER silently degrade quality; a failed result must be
        # distinguishable from a genuine empty and loudly reported"} [CONFIDENCE: CONFIRMED 100% — direct user directive].
        n_blk, n_seg = len(enriched["basic_info"]), len(enriched["transcript_segments"])
        if enriched["output_truncated"]:                      # hard fail (VLM _error / mega-page length) — flagged by enrich
            status, mark, tag = "FAILED", "⛔", "FAILED(truncated/vlm-error)"
        elif n_blk == 0 and n_seg == 0 and found == 0:        # page had content but enrichment produced nothing usable
            status, mark, tag = "EMPTY", "⚠️ ", "EMPTY(rendered-but-0-content)"
        else:
            status, mark, tag = "ok", "✓ ", "ok"
        print(f"  {mark}{entry['id']:13} media {found}/{total} | basic_info {n_blk}blk"
              f" | transcript {n_seg}seg | {tag}", flush=True)
        summary.append((entry["id"], status, found, total, enriched["output_truncated"]))

    print("\n===== SUMMARY =====")
    tot_found = sum(s[2] for s in summary); tot_media = sum(s[3] for s in summary)
    for sid, st, f, t, tr in summary:
        mark = "⛔" if st in ("FAILED", "render-empty") else ("⚠️" if st == "EMPTY" else "  ")
        print(f"  {mark} {sid:13} {st:12} media {f}/{t}")
    # LOUD run-level banner — a degraded run must be UNMISSABLE, never a quiet "10/10 done". Count every non-ok event so
    # a connection-errored / empty enrichment can NEVER be mistaken for a complete one. {USER "fail loudly is the core"}.
    degraded = [s for s in summary if s[1] != "ok"]
    print(f"\n媒体覆盖 {tot_found}/{tot_media} | traces → {_OUTDIR}/<id>/trace.txt (+ req_*.txt full prompt/output)")
    if degraded:
        ids = ", ".join(f"{s[0]}({s[1]})" for s in degraded)
        print(f"⛔⛔ DEGRADED RUN — {len(degraded)}/{len(summary)} events NOT clean: {ids}\n"
              f"   do NOT treat this run as complete — re-run after the VLM server is stable (retries ride out a blip, "
              f"not a sustained outage).", flush=True)
    else:
        print(f"✅ CLEAN RUN — all {len(summary)} events enriched with usable content.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
