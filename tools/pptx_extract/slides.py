"""slides — .pptx bytes → per-slide text + tables + notes, or ('', []) on failure.

用一句话讲完: 拿 .pptx bytes → python-pptx 逐页遍历 shapes → 收每页的 title / 正文文本 / 表格(shape.has_table)/
演讲备注(notes)→ 返回(全文, 每页结构列表)。python-pptx 缺失时退回 stdlib zipfile:直接读 ppt/slides/slideN.xml
把 <a:t> 文本节点拼出来(只有文本,没表格)—— 保证缺依赖也有基础产出,不 break。全本地、绝不抛。
"""
from __future__ import annotations

import io
import re


def _table_to_dict(table) -> dict:
    """A python-pptx table → {columns, rows} (row 0 = header). Cells are stripped, newline-collapsed strings."""
    grid = []
    for row in table.rows:
        grid.append([re.sub(r"\s+", " ", (cell.text or "")).strip() for cell in row.cells])
    if not grid:
        return {}
    return {"columns": grid[0], "rows": grid[1:]}


def _slide_content(slide) -> dict:
    """One python-pptx slide → {title, text, tables, notes}. Walks every shape: text frames contribute body text,
    has_table shapes contribute structured tables; the title placeholder (if any) is pulled out separately."""
    title = ""
    try:
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = (slide.shapes.title.text or "").strip()
    except Exception:                                            # noqa: BLE001 — some slides have no title placeholder
        title = ""
    body_parts: list[str] = []
    tables: list[dict] = []
    for shape in slide.shapes:
        try:
            if getattr(shape, "has_table", False) and shape.has_table:
                tbl = _table_to_dict(shape.table)
                if tbl:
                    tables.append(tbl)
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                txt = (shape.text or "").strip()
                if txt and txt != title:                        # don't duplicate the title into the body
                    body_parts.append(txt)
        except Exception:                                        # noqa: BLE001 — one odd shape must not lose the slide
            continue
    notes = ""
    try:
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
    except Exception:                                            # noqa: BLE001
        notes = ""
    return {"title": title, "text": "\n".join(body_parts).strip(), "tables": tables, "notes": notes}


def _fallback_zip_text(data: bytes) -> tuple[str, list[dict]]:
    """python-pptx-absent fallback: read the .pptx zip's ppt/slides/slideN.xml directly and pull the <a:t> text runs.
    Text-only (no tables/notes), but keeps the tool useful without the dep. Slides ordered by their numeric index."""
    try:
        import zipfile
        _T_RE = re.compile(r"<a:t>(.*?)</a:t>", re.S)             # DrawingML text run node
        _SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
        slides: list[dict] = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted((n for n in z.namelist() if _SLIDE_RE.search(n)),
                           key=lambda n: int(_SLIDE_RE.search(n).group(1)))   # slide1, slide2, … numeric order
            for i, name in enumerate(names, 1):
                xml = z.read(name).decode("utf-8", "ignore")
                runs = [re.sub(r"\s+", " ", m).strip() for m in _T_RE.findall(xml)]
                text = "\n".join(r for r in runs if r).strip()
                slides.append({"title": "", "text": text, "tables": [], "notes": ""})
        full = "\n\n".join(s["text"] for s in slides if s["text"]).strip()
        return full, slides
    except Exception:                                            # noqa: BLE001 — not a valid pptx zip → nothing
        return "", []


def pptx_to_slides(data: bytes) -> tuple[str, list[dict]]:
    """.pptx bytes → (full_text, [slide dicts]). Each slide dict = {n, title, text, tables, notes}, n = 1-based index.
    Uses python-pptx (proper: tables + notes); falls back to a stdlib-zip text extractor if python-pptx is absent.
    ('', []) on non-pptx / unparseable bytes."""
    if not data or data[:4] != b"PK\x03\x04":                    # a .pptx is a ZIP → must start with the PK magic
        return "", []
    try:
        from pptx import Presentation
    except Exception:                                            # noqa: BLE001 — python-pptx absent → stdlib fallback
        return _fallback_zip_text(data)
    try:
        prs = Presentation(io.BytesIO(data))
    except Exception:                                            # noqa: BLE001 — a PK zip that isn't a deck → fallback (or empty)
        return _fallback_zip_text(data)
    slides: list[dict] = []
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        c = _slide_content(slide)
        c["n"] = i
        slides.append(c)
        seg = "\n".join(x for x in (c["title"], c["text"]) if x)   # title + body per slide, in reading order
        if seg.strip():
            parts.append(seg.strip())
    return "\n\n".join(parts).strip(), slides
