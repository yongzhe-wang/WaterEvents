"""testpdf_1 runner — fetch + download + extract the 20 real IR PDFs via officeall (Docling), report a per-pdf table
and write the FULL structured content (clean markdown + tables) to results/NN.json.

用一句话讲完: 读 urls.txt 的 20 个真实 IR PDF → officeall.fetch_bytes 抓(存 pdfs/NN.pdf)→ officeall.extract_bytes
用 Docling 抽(干净 markdown + TableFormer 结构化表格)→ 每个 PDF 写一份 results/NN.json(完整内容)→ 汇总。
这是 officeall(Docling 引擎)在真实数据集上的端到端验证,取代之前 pdfplumber 切碎的输出。

跑: .venv/bin/python tests/testpdf_1/run.py     (需 curl_cffi + docling;首次会下载 Docling 模型)
"""
import json
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[2]               # WaterEvents/
sys.path.insert(0, str(_ROOT))
from tools.officeall import extract_bytes, fetch_bytes            # noqa: E402

D = pathlib.Path(__file__).parent
PDFS = D / "pdfs"; PDFS.mkdir(exist_ok=True)                       # raw downloaded .pdf files
RESULTS = D / "results"; RESULTS.mkdir(exist_ok=True)             # one JSON per pdf with the FULL Docling content


def main() -> None:
    urls = [u for u in (D / "urls.txt").read_text().splitlines() if u.strip().startswith("http")]
    results = []
    print(f"fetching + extracting {len(urls)} pdfs via officeall (docling)...\n")
    print(f"{'#':>2} {'ok':>2} {'fmt':>4} {'bytes':>9} {'pg':>3} {'md_chars':>8} {'tbl':>3} {'fetch':>5} {'ext':>6}  url")
    for i, url in enumerate(urls, 1):
        t0 = time.time()
        data, fmt = fetch_bytes(url)                              # curl_cffi Chrome fingerprint + magic-confirmed format
        fetch_s = time.time() - t0
        if data:
            (PDFS / f"{i:02d}.pdf").write_bytes(data)
        t1 = time.time()
        res = extract_bytes(data, fmt=fmt) if data else None      # Docling: clean markdown + structured tables
        ext_s = time.time() - t1
        row = {
            "n": i, "url": url, "format": fmt, "bytes": len(data),
            "ok": bool(res and res.ok), "pages": res.n_pages if res else 0,
            "md_chars": len(res.text) if res else 0, "tables": res.n_tables if res else 0,
            "error": (res.error if res else "fetch-empty") or "",
            "fetch_s": round(fetch_s, 1), "extract_s": round(ext_s, 1),
        }
        results.append(row)
        # PER-PDF JSON: the FULL Docling content — clean markdown + structured {columns, rows} tables.
        per_pdf = {
            "n": i, "url": url, "format": fmt, "ok": row["ok"], "error": row["error"],
            "n_pages": row["pages"], "n_bytes": row["bytes"], "n_tables": row["tables"],
            "markdown": (res.text if res else ""),                # clean full text, tables inline
            "tables": (res.tables if res else []),                # structured [{columns, rows}] — TableFormer
        }
        (RESULTS / f"{i:02d}.json").write_text(json.dumps(per_pdf, indent=2, ensure_ascii=False))
        print(f"{i:>2} {'OK' if row['ok'] else 'XX':>2} {fmt:>4} {row['bytes']:>9} {row['pages']:>3} "
              f"{row['md_chars']:>8} {row['tables']:>3} {row['fetch_s']:>5} {row['extract_s']:>6}  {url[-46:]}")
    ok = sum(1 for r in results if r["ok"])
    n_tables = sum(r["tables"] for r in results)
    print(f"\n{ok}/{len(results)} extracted OK | {n_tables} tables total | {sum(r['bytes'] for r in results)/1e6:.1f} MB")
    if ok < len(results):
        print("failures:", [(r["n"], r["error"]) for r in results if not r["ok"]])
    (D / "results.json").write_text(json.dumps(results, indent=2))
    print(f"→ results.json + results/*.json + pdfs/ written to {D}")


if __name__ == "__main__":
    main()
