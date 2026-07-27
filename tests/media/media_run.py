"""tests/media_run.py — run media enrichment over a CHERRY-PICKED, NON-oracle event set → one fully-debuggable folder
per event (trace.txt + the full-prompt/raw req_*.txt), so we can eyeball exactly what the model saw and produced.

用一句话讲完: 读 dataset 里每个 event {id, event_url, known_event} → watercrawl render 其 detail page(拿 inline text +
截图)→ enrich_page 打真实 RunPod VLM → 为每个 event 写 trace.txt(known event / 渲染信息 / FULL page text / enriched
basic_info + media urls + transcript)+ QWEN_DEBUG_DIR 落的 req_*.txt(完整 prompt + RAW 模型输出)。**跟 smoke.py 的
区别: 无 ground-truth oracle** — 数据集来自真实 discovery、我们自己读 output 判质量, 不跟 2021 oracle 对比。{USER
2026-07-23 "dont rely on ground truth read the output yourself ... structure the output so each page as txt + full prompt
+ all the trace"} [CONFIDENCE: CONFIRMED 100% — direct user directive].

Run ON RUNPOD (browser + VLM co-located), INLINE env (NOT `env $E` — that ate QWEN_API_KEY → 401):
  cd /workspace/WaterEvents && PYTHONPATH=/workspace/WaterEvents QWEN_BASE_URLS=http://127.0.0.1:8000/v1 \
  QWEN_API_KEY=<key> QWEN_MAX_TOKENS=12000 MEDIA_VISION_TEXT_CHARS=24000 QWEN_RETRIES=3 \
  MEDIA_RUN_DATASET=tests/datasets/pick_cross_platform MEDIA_RUN_OUT=tests/10media /root/venv/bin/python tests/media_run.py
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil

from providers.qwen_llm import config as qcfg          # mutate DEBUG_DIR per-event so each event's req_*.txt is separated

from agent.media_agent.enrich import enrich_page
from agent.media_agent import router                   # classify the event_url → route a .pdf/.pptx to Docling, html to the VLM

# dataset = a dir of ev*.json, each {id, event_url, known_event:{title,date,type,media_urls[]}} — NO ground_truth_media.
_DATASET = os.environ.get("MEDIA_RUN_DATASET",
                          os.path.join(os.path.dirname(__file__), "datasets", "pick_cross_platform"))
_OUT = os.environ.get("MEDIA_RUN_OUT", os.path.join(os.path.dirname(__file__), "10media"))
_USE_IMAGE = os.environ.get("MEDIA_USE_IMAGE", "1") not in ("0", "false", "no")
_CLEAR = os.environ.get("MEDIA_RUN_CLEAR", "1") not in ("0", "false", "no")   # wipe _OUT first (fresh run) unless told not to


async def _render(url: str) -> dict | None:
    """Open the event detail page with watercrawl (in a thread — render_shot is sync). Returns {page_url,page_text,
    image_b64,method,n_links} or None when the render came back empty (dead/moved page — recorded loudly, not fatal)."""
    from providers import watercrawl                            # lazy: playwright/chromium only exist on RunPod
    r = await asyncio.to_thread(watercrawl.render_shot, url)
    if not r.get("text") and not r.get("links"):
        return None
    # html + links thread through to enrich → extract_html: the RAW html feeds the deterministic body extractor (trafilatura
    # + pandas), and links[] is the walled/thin-tier url safety net (populated even when html is empty). {DESIGN wf_7b61c8d0}.
    return {"page_url": url, "page_text": r.get("inline") or r.get("text", ""),
            "html": r.get("html", ""), "links": r.get("links", []),
            "image_b64": r.get("shot_b64", ""), "method": r.get("method", ""), "n_links": len(r.get("links", []))}


async def _office_enrich(url: str, known: dict) -> tuple[dict, str]:
    """A .pdf/.pptx/.docx/.xlsx event_url → Docling (tools.officeall), NOT a browser render + VLM. WHY: rendering a PDF to a
    giant screenshot overflows the VLM (ev04/05/07 the q4cdn earnings PDFs) AND resiliparse-on-PDF-bytes produces binary
    junk blocks — Docling is the RIGHT tool (TableFormer extracts clean financial tables). Maps DocResult → the same enriched
    shape enrich_page returns so the trace writer is unchanged. {POD 2026-07-25 4 PDF events FAILED via the html path}."""
    from tools.officeall import extract as office_extract       # lazy: docling is heavy (torch + layout models), CPU on the VM
    res = await asyncio.to_thread(office_extract, url)           # blocking Docling call off the event loop
    blocks = []
    if res.text.strip():                                        # docling markdown (tables already rendered inline) → one md block
        blocks.append({"type": "md", "md": res.text})
    enriched = {"title": known.get("title", ""), "date": known.get("date", ""), "type": known.get("type", ""),
                "urls": list(known.get("media_urls") or [url]),  # a document is a LEAF — no new urls; keep the known media set
                "basic_info": blocks, "transcript_segments": [],
                "output_truncated": not res.ok}                 # ok=False (unreachable/scanned/empty) ⇒ flag it (fail loud)
    note = (f"OFFICE/DOCLING format={res.format} via={res.via} n_pages={res.n_pages} n_tables={res.n_tables} "
            f"ok={res.ok} err={res.error or '-'} warnings={res.warnings or '-'}")
    return enriched, note


def _render_blocks(basic_info: list) -> str:
    """basic_info blocks → readable text for the trace — md/list verbatim, table as a small ascii grid."""
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


def _write_trace(path: str, entry: dict, page: dict | None, enriched: dict | None, note: str = "") -> None:
    """ONE event's full readable trace — known event / render info / FULL page text (what the VLM saw) / enriched
    metadata + media urls + basic_info + transcript. NO oracle section (we judge quality by reading, not ground truth).
    The exact prompt + RAW model output live in the sibling req_*.txt (QWEN_DEBUG_DIR)."""
    L = [f"===== EVENT {entry['id']} =====", f"event_url: {entry['event_url']}"]
    L.append(f"\n--- KNOWN EVENT (from discovery, input reference) ---\n"
             f"{json.dumps(entry['known_event'], ensure_ascii=False, indent=2)}")
    if note:
        L.append(f"\n!!! {note}")
    if page:
        L.append(f"\n--- RENDER ---\nmethod={page['method']}  page_text_chars={len(page['page_text'])}  "
                 f"screenshot={'yes' if page['image_b64'] else 'no'}  links_on_page={page['n_links']}")
        # FULL page text (not a 1200-char excerpt) — the user wants to see exactly what the model was fed for this page.
        L.append(f"\n--- FULL PAGE TEXT FED TO VLM (inline links as [anchor](Lnn) after tagging in enrich) ---\n{page['page_text']}")
        L.append("\n(EXACT prompt + RAW VLM OUTPUT for every request → the req_*.txt in this same folder, via QWEN_DEBUG_DIR)")
    if enriched:
        trunc = enriched["output_truncated"]
        L.append(f"\n--- ENRICHED: metadata ---\ntitle: {enriched['title']}\ndate: {enriched['date']}\ntype: {enriched['type']}")
        L.append(f"output_truncated: {trunc}  {'⛔ FAILED/NOT-COMPLETE (vlm-error or length)' if trunc else '✓ complete'}")
        L.append(f"\n--- ENRICHED: media urls[] ({len(enriched['urls'])}) ---")
        for u in enriched["urls"]:
            L.append(f"  {u}")
        L.append(f"\n--- ENRICHED: transcript_segments ({len(enriched['transcript_segments'])}) ---")
        for s in enriched["transcript_segments"][:20]:
            L.append(f"  [{s['speaker']}] {s['text'][:120]}")
        L.append(f"\n--- ENRICHED: basic_info ({len(enriched['basic_info'])} blocks) ---\n{_render_blocks(enriched['basic_info'])}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


async def main() -> None:
    files = sorted(glob.glob(os.path.join(_DATASET, "ev*.json")))
    # MEDIA_RUN_ONLY=ev04,ev07 → run ONLY those ids. WHY: the resident Chromium crashes (TargetClosedError) after 2-3
    # heavy full-page renders when its gpu-process contends with vLLM/AWQ on the single A5000. Running ONE event per
    # PROCESS (a driver loop invokes this once per id) gives each render a BRAND-NEW browser (launched at process start,
    # torn down at exit), so the crash-after-N never triggers — N is always 1. {POD 2026-07-23 media_run: ev01-03 ok
    # then ev04-10 all TargetClosedError} [CONFIDENCE: CONFIRMED 100% — the snapshot showed the crash boundary at event 3].
    only = [s.strip() for s in os.environ.get("MEDIA_RUN_ONLY", "").split(",") if s.strip()]
    if only:                                                    # keep only the requested ids (substring match on filename)
        files = [f for f in files if any(o in os.path.basename(f) for o in only)]
    if _CLEAR and os.path.isdir(_OUT):                          # fresh run — wipe the old out dir so stale events don't linger
        shutil.rmtree(_OUT)
        print(f"[media_run] cleared {_OUT}", flush=True)
    os.makedirs(_OUT, exist_ok=True)
    print(f"[media_run] {len(files)} events | dataset={_DATASET} | out={_OUT} | use_image={_USE_IMAGE}", flush=True)

    summary = []
    # PARALLEL render+enrich — each event is its OWN asyncio task, so IR_WATERCRAWL_BROWSERS>1 is actually USED (N renders
    # in flight across the resident browser pool at once) and the run is N× faster. WHY task-local debug dir
    # (config.set_debug_dir, a ContextVar) instead of the module-global qcfg.DEBUG_DIR: under concurrency the global RACES
    # (last writer wins → an event's req_*.txt lands in the wrong folder). A ContextVar is snapshotted per task, so each
    # event's dumps stay in ITS folder. {CONFIG.PY:57 set_debug_dir "ContextVar ... task-local, set() is task-local"}
    # {USER 2026-07-23 "use more browser ... set to 4"} [CONFIDENCE: CONFIRMED 100% — contextvars docs + config comment].
    sem = asyncio.Semaphore(int(os.environ.get("MEDIA_RUN_CONCURRENCY", "4")))   # bound in-flight renders to the browser count

    async def _one(fp: str) -> tuple:
        entry = json.load(open(fp, encoding="utf-8"))
        ev_dir = os.path.join(_OUT, entry["id"])
        os.makedirs(ev_dir, exist_ok=True)
        qcfg.set_debug_dir(ev_dir)                              # TASK-LOCAL: this task's req_*.txt land HERE, race-free under gather
        trace_path = os.path.join(ev_dir, "trace.txt")
        # ROUTE by url kind — an office document (.pdf/.pptx/.docx/.xlsx) goes to Docling, NOT a browser render + VLM (which
        # overflowed on the q4cdn earnings PDFs). Only html gets the render+enrich path below. {POD 2026-07-25 PDF fix}.
        kind = router.classify(entry["event_url"])
        if kind in (router.KIND_PDF, router.KIND_PPTX, router.KIND_DOCX, router.KIND_XLSX):
            async with sem:                                    # bound concurrent docling runs (CPU-heavy) like the render pool
                enriched, note = await _office_enrich(entry["event_url"], entry["known_event"])
            _write_trace(trace_path, entry, None, enriched, note=note)
            n_url, n_blk, n_seg = len(enriched["urls"]), len(enriched["basic_info"]), len(enriched["transcript_segments"])
            st, mark = ("FAILED", "⛔") if enriched["output_truncated"] else (("EMPTY", "⚠️ ") if n_blk == 0 else ("ok", "✓ "))
            print(f"  {mark}{entry['id']:16} [DOCLING] urls {n_url} | basic_info {n_blk}blk | {st}", flush=True)
            return (entry["id"], st, n_url, n_blk)
        async with sem:                                        # only N renders touch the browser pool at once (N = browsers)
            page = await _render(entry["event_url"])
        if page is None:                                       # dead/moved/walled/crashed-render — record loudly, keep going
            _write_trace(trace_path, entry, None, None, note="RENDER EMPTY — page dead/moved/walled/browser-crash")
            print(f"  ⛔ {entry['id']:16} render empty — skipped", flush=True)
            return (entry["id"], "render-empty", 0, 0)
        enriched = await enrich_page(entry["known_event"], page, use_image=_USE_IMAGE)   # VLM call — server continuous-batches
        _write_trace(trace_path, entry, page, enriched)
        n_url, n_blk, n_seg = len(enriched["urls"]), len(enriched["basic_info"]), len(enriched["transcript_segments"])
        # STATUS — same fail-loud taxonomy as smoke.py: hard-fail (output_truncated) vs content-empty vs ok. No oracle.
        if enriched["output_truncated"]:
            st, mark = "FAILED", "⛔"
        elif n_blk == 0 and n_seg == 0:
            st, mark = "EMPTY", "⚠️ "
        else:
            st, mark = "ok", "✓ "
        print(f"  {mark}{entry['id']:16} urls {n_url} | basic_info {n_blk}blk | transcript {n_seg}seg | {st}", flush=True)
        return (entry["id"], st, n_url, n_blk)

    summary = list(await asyncio.gather(*(_one(fp) for fp in files)))   # all events concurrently; sem bounds render fan-out

    print("\n===== MEDIA_RUN SUMMARY =====")
    for sid, st, nu, nb in summary:
        m = "⛔" if st in ("FAILED", "render-empty") else ("⚠️" if st == "EMPTY" else "  ")
        print(f"  {m} {sid:16} {st:12} urls={nu} basic_info={nb}blk")
    degraded = [s for s in summary if s[1] != "ok"]
    if degraded:
        print(f"\n⛔⛔ {len(degraded)}/{len(summary)} events NOT clean: "
              f"{', '.join(f'{s[0]}({s[1]})' for s in degraded)} — re-run after server stable", flush=True)
    else:
        print(f"\n✅ CLEAN — all {len(summary)} events enriched with usable content.", flush=True)
    print(f"\n每 event → {_OUT}/<id>/trace.txt (+ req_*.txt 完整 prompt/输出)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
