"""testpdf_1 runner — fetch + download + extract the 20 real IR PDFs in urls.txt, report a per-pdf table.

用一句话讲完: 读 urls.txt 里的 20 个真实 IR PDF → 逐个 fetch_bytes(存进 pdfs/NN.pdf)→ extract_bytes(pypdf 文本
+ pdfplumber 表格)→ 打印一行 {bytes, pages, text_chars, tables, ok, 耗时} → 汇总 OK 率,写 results.json。这是
pdf_extract 在真实数据集上的端到端验证(不是合成 URL)。

跑: python3 tests/testpdf_1/run.py     (需 curl_cffi + pypdf;pdfplumber 缺则 tables=0,文本仍抽)
"""
import json
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[2]               # WaterEvents/
sys.path.insert(0, str(_ROOT))
from tools.pdf_extract import extract_bytes, fetch_bytes           # noqa: E402

D = pathlib.Path(__file__).parent
PDFS = D / "pdfs"
PDFS.mkdir(exist_ok=True)


def main() -> None:
    urls = [u for u in (D / "urls.txt").read_text().splitlines() if u.strip().startswith("http")]
    results = []
    print(f"fetching + extracting {len(urls)} pdfs...\n")
    print(f"{'#':>2} {'ok':>2} {'bytes':>9} {'pg':>3} {'chars':>7} {'tbl':>3} {'fetch':>5} {'ext':>5}  url")
    for i, url in enumerate(urls, 1):
        t0 = time.time()
        data = fetch_bytes(url)                                    # curl_cffi Chrome-fingerprint GET (+ %PDF magic gate)
        fetch_s = time.time() - t0
        if data:
            (PDFS / f"{i:02d}.pdf").write_bytes(data)              # save the raw pdf for inspection / re-runs
        t1 = time.time()
        res = extract_bytes(data) if data else None               # pypdf text + pdfplumber tables from the SAME bytes
        ext_s = time.time() - t1
        row = {
            "n": i, "url": url, "bytes": len(data),
            "ok": bool(res and res.ok),
            "pages": res.n_pages if res else 0,
            "text_chars": len(res.text) if res else 0,
            "tables": len(res.tables) if res else 0,
            "error": (res.error if res else "fetch-empty") or "",
            "fetch_s": round(fetch_s, 1), "extract_s": round(ext_s, 1),
        }
        results.append(row)
        print(f"{i:>2} {'OK' if row['ok'] else 'XX':>2} {row['bytes']:>9} {row['pages']:>3} "
              f"{row['text_chars']:>7} {row['tables']:>3} {row['fetch_s']:>5} {row['extract_s']:>5}  {url[-52:]}")
    ok = sum(1 for r in results if r["ok"])
    n_tables = sum(r["tables"] for r in results)
    total_bytes = sum(r["bytes"] for r in results)
    print(f"\n{ok}/{len(results)} extracted OK | {n_tables} tables total | {total_bytes/1e6:.1f} MB downloaded")
    if ok < len(results):
        print("failures:", [(r["n"], r["error"]) for r in results if not r["ok"]])
    (D / "results.json").write_text(json.dumps(results, indent=2))
    print(f"→ results.json + pdfs/ written to {D}")


if __name__ == "__main__":
    main()
