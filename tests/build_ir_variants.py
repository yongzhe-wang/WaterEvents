"""Build NON-OVERLAPPING raw page datasets (10 / 100 / 1000) from the hstress render traces → tests/ir_official_variants/.

用一句话讲完: 从 hstress_out/trace 里所有成功渲染页(每页 page.html + content.txt + links.txt + meta.json + result.json
原始未处理产物)固定 seed=42 洗牌 → 顺序切成 DISJOINT 三份 [0:10] / [10:110] / [110:1110] → 原样 copy 到
ir_official_variants/sample_{10,100,1000}/<slug>/,每份写 manifest.json(url 清单,非重叠可审计)。RAW ONLY —— 不抽取、
不处理、不发 VLM,就是渲染落盘的原始文件。{USER 2026-07-24 "create dataset 10 100 1000 non-overlap, ir_official_variants,
keep raw unprocessed"} [CONFIDENCE: CONFIRMED — 直接指令].

Run ON THE POD:  PYTHONPATH=/workspace/WaterEvents /root/venv/bin/python /workspace/WaterEvents/tests/build_ir_variants.py
"""
from __future__ import annotations

import glob
import json
import os
import random
import shutil

TRACE = "/workspace/WaterEvents/tests/hstress_out/trace"          # source: the 2668-page render's per-page traces
OUT = "/workspace/WaterEvents/tests/ir_official_variants"         # target dataset root
SIZES = [("sample_10", 10), ("sample_100", 100), ("sample_1000", 1000)]   # three DISJOINT variant sizes
RAW_FILES = ["page.html", "content.txt", "links.txt", "meta.json", "result.json"]   # the raw unprocessed render products
SEED = 42                                                        # fixed → reproducible partition, no Math.random drift
MIN_LINKS = 20            # CLEAN gate: every dataset page must carry ≥20 links (audit p50=67; JS shells / link-poor fail this)
MIN_CHARS = 800           # CLEAN gate: ≥800B rendered text (drops the thin empty-shell pages curl couldn't JS-render)


def _collect() -> tuple[list[tuple[str, str]], int]:
    """Sampleable CLEAN raw pages → ([(dir,url)], n_dropped). A page qualifies only if it has a url AND ≥MIN_LINKS links
    AND ≥MIN_CHARS of content — the JS-shell / link-poor pages the curl audit flagged are excluded so the dataset is clean
    ('everything has links'). {USER 2026-07-24 "make sure your dataset is clean meaning all the things have links"}."""
    out: list[tuple[str, str]] = []
    dropped = 0
    for h in glob.glob(os.path.join(TRACE, "**", "page.html"), recursive=True):
        d = os.path.dirname(h)
        m = os.path.join(d, "meta.json")
        if not os.path.exists(m):                                # need meta for the url + link count
            continue
        try:
            meta = json.load(open(m, encoding="utf-8")) or {}
        except Exception:                                        # noqa: BLE001 — a corrupt meta must not sink the build
            continue
        url = meta.get("url") or ""
        nlinks = int(meta.get("n_links") or 0)                   # link count recorded at render time
        c_p = os.path.join(d, "content.txt")
        chars = os.path.getsize(c_p) if os.path.exists(c_p) else 0
        if not url:
            continue
        if nlinks < MIN_LINKS or chars < MIN_CHARS:              # UNCLEAN — shell / link-poor → exclude
            dropped += 1
            continue
        out.append((d, url))
    return out, dropped


def _slug(d: str) -> str:
    """Unique per-page folder name = the page dir's path relative to TRACE, '/'→'__' (collision-proof by construction)."""
    return os.path.relpath(d, TRACE).replace(os.sep, "__")


def main() -> None:
    pages, dropped = _collect()
    random.seed(SEED)                                            # deterministic shuffle → same split every run
    random.shuffle(pages)
    total_needed = sum(n for _, n in SIZES)
    print(f"[variants] {len(pages)} CLEAN pages (≥{MIN_LINKS} links, ≥{MIN_CHARS}B) | dropped {dropped} unclean | "
          f"need {total_needed} for disjoint 10+100+1000", flush=True)
    if len(pages) < total_needed:                                # fail LOUD — never silently overlap to hit the counts
        raise SystemExit(f"NOT ENOUGH pages: have {len(pages)}, need {total_needed} for non-overlapping sets")

    os.makedirs(OUT, exist_ok=True)
    cursor = 0                                                   # sequential slice → the three sets share NO page
    grand: dict[str, list] = {}
    for name, n in SIZES:
        chunk = pages[cursor:cursor + n]                         # disjoint slice
        cursor += n
        dst_root = os.path.join(OUT, name)
        if os.path.exists(dst_root):                             # rebuild cleanly each run
            shutil.rmtree(dst_root)
        os.makedirs(dst_root)
        manifest = []
        for d, url in chunk:
            sl = _slug(d)
            pdst = os.path.join(dst_root, sl)
            os.makedirs(pdst, exist_ok=True)
            for fn in RAW_FILES:                                 # copy the RAW files verbatim — no processing
                src = os.path.join(d, fn)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(pdst, fn))
            manifest.append({"slug": sl, "url": url})
        with open(os.path.join(dst_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"variant": name, "count": len(manifest), "pages": manifest}, f, ensure_ascii=False, indent=2)
        grand[name] = [m["url"] for m in manifest]
        print(f"[variants] {name}: {len(manifest)} pages → {dst_root}", flush=True)

    # audit: prove the three sets are truly non-overlapping (by url)
    s10, s100, s1000 = set(grand["sample_10"]), set(grand["sample_100"]), set(grand["sample_1000"])
    overlaps = {"10∩100": len(s10 & s100), "10∩1000": len(s10 & s1000), "100∩1000": len(s100 & s1000)}
    with open(os.path.join(OUT, "OVERLAP_AUDIT.json"), "w", encoding="utf-8") as f:
        json.dump({"sizes": {k: len(v) for k, v in grand.items()}, "pairwise_overlap": overlaps,
                   "seed": SEED, "source": TRACE, "raw_files": RAW_FILES,
                   "clean_gate": {"min_links": MIN_LINKS, "min_chars": MIN_CHARS, "dropped_unclean": dropped}},
                  f, ensure_ascii=False, indent=2)
    print(f"[variants] OVERLAP AUDIT (all should be 0): {overlaps}", flush=True)
    print(f"[variants] done → {OUT}/ (sample_10, sample_100, sample_1000, OVERLAP_AUDIT.json)", flush=True)


if __name__ == "__main__":
    main()
