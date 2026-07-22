"""tables — PDF bytes → financial tables ([{columns, rows}]) via pdfplumber, or [] on failure.

用一句话讲完: 拿 PDF bytes → pdfplumber 逐页 extract_tables → 只保留"财务数据块"(每行 ≥2 个 ATOMIC 数字单元的行)+
它上面最近的表头行 → 输出 {columns, rows}。核心难点是判"这一格是不是一个纯数值"—— 先 NFKC 把全角 ０-９（），．％
折成半角,再剥掉 $¥%(),.- 和 CJK 单位(百万/円/△▲),剩下全是数字才算。没这步,日本決算短信/損益計算書 全被判 0
数值单元而丢弃。$0、无 LLM、绝不抛。
"""
from __future__ import annotations

import io
import os
import re
import time
import unicodedata

_MAX_PAGES = int(os.environ.get("PDF_TABLES_MAX_PAGES", "8"))      # extract_tables on a dense page is a slow C call — cap pages
_MAX_BYTES = int(os.environ.get("PDF_TABLES_MAX_BYTES", "6000000"))  # pdfplumber explodes on a huge PDF (~59s); skip it
_TIME_BUDGET_S = float(os.environ.get("PDF_TABLES_TIME_BUDGET_S", "6.0"))  # per-PDF scan budget
_MAX_TABLES = 12                                                  # per PDF, so a table-heavy filing can't blow up the payload
_MAX_ROWS = 200                                                  # per-table row cap
_CELL_CAP = 500                                                 # per-cell char cap
_MIN_NUMERIC_CELLS = 2                                          # a row needs ≥2 atomic-number cells to be "financial data"
_MIN_DATA_ROWS = 2                                             # <2 numeric rows ⇒ not a data table

# Strip $ ¥ ( ) % , . - whitespace AND CJK units/signs (百万千億兆円銭株 △▲) so a value cell reduces to bare digits.
# NFKC (applied first, in _is_atomic_num) has already folded full-width ０-９（），．％ to half-width, so only the CJK
# kanji units + △▲ negative markers need adding here — without them a 決算短信 '百万円 2,313,051' scored 0 numeric cells.
_DECOR_RE = re.compile(r"[\$¥\(\)\%,\.\-\s　百万千億兆円銭株△▲]")


def _norm(c) -> str:
    """One grid cell → collapsed, trimmed, capped string. pdfplumber cells carry embedded newlines from wrapped
    text ('Japan Subadvis\\nory'); one visual cell must read as one string."""
    return re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip()[:_CELL_CAP]


def _is_atomic_num(c: str) -> bool:
    """True when the cell is a standalone numeric VALUE (money/count/percent), not prose that contains a number.
    NFKC-fold full→half-width first (（166）→(166)), strip decoration, what's left must be non-empty all-digits."""
    s = _DECOR_RE.sub("", unicodedata.normalize("NFKC", c or ""))
    return bool(s) and s.isdigit()


def _is_data_row(cells: list[str]) -> bool:
    """True when ≥_MIN_NUMERIC_CELLS cells are ATOMIC NUMBERS — a real financial data row. Requiring ATOMIC (not
    just 'contains a digit') rejects prose rows like 'reported $98.0 billion as of April 30, 2022'."""
    return sum(1 for c in cells if _is_atomic_num(c)) >= _MIN_NUMERIC_CELLS


def _drop_empty_cols(rows: list[list[str]]) -> list[list[str]]:
    """Remove columns empty across EVERY row — the text strategy over-segments into more columns than the table uses,
    leaving all-blank columns that would render as empty cells."""
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]           # pad ragged rows to a rectangle
    keep = [ci for ci in range(width) if any(r[ci] for r in rows)]
    return [[r[ci] for ci in keep] for r in rows]


def _financial_table(grid: list[list]) -> dict | None:
    """One pdfplumber grid → {columns, rows} keeping ONLY the financial data block + its header, or None if the
    grid has no data block. The header is the nearest row ABOVE the data with ≥3 non-empty cells (a 1-cell row is
    a section sub-label like 'Institutional Accounts:', not the column header)."""
    norm = [[_norm(c) for c in (r or [])] for r in grid]
    data_idx = [i for i, r in enumerate(norm) if _is_data_row(r)]
    if len(data_idx) < _MIN_DATA_ROWS:
        return None
    lo, hi = data_idx[0], data_idx[-1]                          # first..last numeric row = the data span
    block = norm[lo:hi + 1][:_MAX_ROWS]
    header = None
    for i in range(lo - 1, max(lo - 6, -1), -1):               # scan up to 5 rows up for the column header
        if sum(1 for c in norm[i] if c) >= 3:
            header = norm[i]
            break
    rows = ([header] + block) if header else block
    rows = _drop_empty_cols(rows)
    rows = [r for r in rows if any(c for c in r)]              # drop all-empty rows left after column pruning
    if len(rows) < _MIN_DATA_ROWS or max((len(r) for r in rows), default=0) < 2:
        return None
    return {"columns": rows[0], "rows": rows[1:]}


def pdf_to_tables(data: bytes) -> list[dict]:
    """PDF bytes → [{columns, rows}] financial tables. Per page tries the `lines` strategy first (clean for ruled
    tables — 決算短信/Form 4), falls back to `text` coordinate-clustering (borderless tables — monthly AUM). $0,
    never raises. Bounded by _MAX_PAGES / _TIME_BUDGET_S / _MAX_TABLES so one bad doc can't stall a parallel pool."""
    if not data or data[:5] != b"%PDF-" or len(data) > _MAX_BYTES:
        return []
    try:
        import pdfplumber                                       # lazy — heavy import, only when a real PDF arrives
    except Exception:                                           # noqa: BLE001 — pdfplumber absent → no tables
        return []
    out: list[dict] = []
    try:
        t0 = time.perf_counter()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:_MAX_PAGES]:
                if time.perf_counter() - t0 > _TIME_BUDGET_S:   # budget spent → return what we found
                    break
                found = False
                for settings in (None, {"vertical_strategy": "text", "horizontal_strategy": "text"}):
                    try:
                        grids = page.extract_tables(settings) if settings else page.extract_tables()
                    except Exception:                            # noqa: BLE001 — bad page/strategy → try the next
                        continue
                    for g in (grids or []):
                        tbl = _financial_table(g)
                        if tbl:
                            out.append(tbl)
                            found = True
                        if len(out) >= _MAX_TABLES:
                            return out
                    if found:                                    # `lines` already yielded a table → skip the `text` pass
                        break
    except Exception:                                            # noqa: BLE001 — malformed PDF → return what we have
        return out
    return out
