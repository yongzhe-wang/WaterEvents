"""officeall.coords — 从 PDF 的文字坐标直接重建表格,不经过渲染和视觉模型。

用一句话讲完: 数字原生 PDF 里每个字形都带精确坐标,一张数据表就是「一组右对齐的数值列 + 一批在这些
列上同时有值的行」;把数值 token 的右边缘 x1 聚成簇当作列边界交给 pdfplumber,它就能把每个数放进
正确的格 —— 而 docling 是把页面渲染成像素再让 TableFormer 看图猜网格,主动丢掉了这份坐标。

WHY: 同一页 ExxonMobil 3Q24 IR Data Summary,两条路径逐格对 PDF 原页的结果:
  {DOCLING "Energy Products | United States | 517 | 450 | '836 540' | '1,329 1,878' | '1,356 1,086'"
   而下一行 Non-U.S. 的对应三格全空 —— 两行的值被挤进一行;
   "Net income attributable to ExxonMobil (U.S. GAAP) | 1.92 | 2.14 | 2.06 | | 2.25" —— 净利润这一行
   拿到的是每股收益,正确值是 8,610 | 9,240 | 8,220 | 7,630 | 9,070}
  {本模块 44 个数值行全部与原页一致,含 (289) (89) (544) 这类括号负数和 '35 %' 这类带单位的格}
维护者已确认这是 TableFormer 的模型边界而非配置问题:
  {DOCLING DISCUSSION #621, collaborator maxmnemonic "such tables are 'out of training distribution',
   meaning the current Tableformer model lacks adequate training examples for this scenario"}
  另有 4 个 open issue 症状完全一致:#2756 "Tightly spaced columns are misinterpreted, causing merging
  or incorrect splitting"、#2134 "Table extraction bug missing one column"、#1678、#2790。
[CONFIDENCE: CONFIRMED 100% — 两条路径的输出都逐格比对过 PDF 原页;issue 原文引自 GitHub。]

不对称性是这个模块存在的真正理由: docling 的错是「值跑到别的行」——不可恢复,下游发现不了;本模块
的错是「一个值被切成两格」——拼回去即可。两种错的代价差一个量级。

上游触发: officeall.extract_bytes(fmt='pdf')。下游连接: 失败(无文字层/无数值网格)则回退 docling。
"""
from __future__ import annotations

import io
import re
import statistics

_EPS = 1e-6
# 财报数值的全部形态:括号负数 (486)、千分位 1,234、小数 1.92、百分比 35%
_NUM = re.compile(r"^[-+(]?[\d][\d,.]*\)?%?$")
_PCT = re.compile(r"^%$")

# 一列至少要有这么多个数才算「列」——滤掉散文里的孤立数字和页脚页码。
_MIN_COL_ROWS = 3
# 一页至少要有这么多列才算有表 —— 单列右对齐数字(目录页码、页脚)不构成数据表。
_MIN_COLS = 2
# 数值 x1 聚类的容差(pt)。列间距通常 20-50pt,字内间隙 <0.2pt,所以 4 落在很宽的安全区间。
_TOL = 4.0
_PAD = 3.0


def _upright(c: dict) -> dict:
    """文字方向只由矩阵的旋转分量(b, c)决定;缩放分量(a, d)不改变方向。

    pdfplumber 的 WordExtractor 在 upright 变化处断词,所以一个被写坏的 upright 会让分词碎成单字。
    {ENI PDF 2026-08-07 "1703 个字符 matrix=(0.591,0,0,0.588) 被标成 upright=False;
     extract_words() 前 6 个 = ['(', 'S', '€', 'm', 'e', 'R'],'1,495' 根本不作为 word 存在"}
    {jsvine/pdfplumber#663 记录了 upright=False 时按字符断词的行为}
    但**不能一刀切成 True** —— 同一页另有 173 个 matrix=(0,0.588,-0.591,0) 的字符是真旋转 90° 的
    竖排列名,强制正立会把它们混进数据行。
    [CONFIDENCE: CONFIRMED 100% — 修正后 upright=True 1714 / False 173,后者正是竖排字符数;
     竖排单独聚类还原出 'Exploration & Production'…'GROUP' 共 11 个列名,而 docling 把
     Refining+Chemicals 粘成 "Chemicals Refining" 且顺序颠倒。]
    """
    m = c.get("matrix")
    if not m:
        return c
    return dict(c, upright=(abs(m[1]) < _EPS and abs(m[2]) < _EPS))


def _numeric_words(page) -> tuple[list, list]:
    """(全部 word, 数值 word)。数值 word 带 x1e = 含尾随 '%' 的右边缘。

    '35 %' 在 PDF 里是两个 token(中间有空格)。不把 % 算进右边缘的话,列边界会落在数字和百分号之间,
    把 % 挤到下一列:{实测 "Effective Income Tax Rate, % | 35 | % 34 | % 36 | % 30 | % 34"}。
    但**聚类必须用数字自己的 x1**:延伸后的边缘离本列约 8pt,超过 _TOL 会自成一簇,而那簇只有一行、
    随即被 _MIN_COL_ROWS 滤掉,于是 % 又落回列外。所以聚类和定界用两个不同的字段。
    [CONFIDENCE: CONFIRMED 100% — 分离前后同一行的输出对比;修正后读作 '35 % | 34 % | 36 % …'。]
    """
    ws = page.extract_words(x_tolerance=1.5, y_tolerance=1, use_text_flow=False)
    nums = []
    for i, w in enumerate(ws):
        if not _NUM.match(w["text"]):
            continue
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        x1e = w["x1"]
        if nxt and _PCT.match(nxt["text"]) and abs(nxt["top"] - w["top"]) < 2 and nxt["x0"] - w["x1"] < 4:
            x1e = nxt["x1"]
        nums.append(dict(w, x1e=x1e))
    return ws, nums


def _column_lines(page):
    """数值列的右边缘 → 显式竖分割线;None 表示这页没有可辨认的数值网格。

    第 i 列的单元格跨 (right[i-1], right[i]],所以边界就是右边缘本身往右让 _PAD ——
    **不是相邻簇的中点**。中点算法在右对齐的 x1 上是错的:{实测把 '1,329' 切成 '1' + ',329'}。
    最左界取**文字左边距**而不是 0:x=0 处没有文字时 pdfplumber 会把整个标签列裁掉。
    {实测 "左界=0 → 5 列,净利润行 = ['8,610',…] 标签丢失;左界=文字左边距 → 6 列,
     ['Net income attributable to ExxonMobil (U.S. GAAP)', '8,610', …]"}
    [CONFIDENCE: CONFIRMED 100% — 两种边界算法的输出直接对比过。]
    """
    ws, nums = _numeric_words(page)
    if not ws or len(nums) < _MIN_COLS * _MIN_COL_ROWS:
        return None
    pairs = sorted((w["x1"], w["x1e"]) for w in nums)
    groups, cur = [], [pairs[0]]
    for p in pairs[1:]:
        if p[0] - cur[-1][0] > _TOL:
            groups.append(cur)
            cur = []
        cur.append(p)
    groups.append(cur)
    rights = [max(e for _, e in g) for g in groups if len(g) >= _MIN_COL_ROWS]
    if len(rights) < _MIN_COLS:
        return None
    pitch = statistics.median(rights[i + 1] - rights[i] for i in range(len(rights) - 1))
    return [min(w["x0"] for w in ws) - 2, rights[0] - pitch + _PAD] + [r + _PAD for r in rights]


def _clean(c) -> str:
    return re.sub(r"\s+", " ", (str(c) if c else "").replace("\n", " ")).strip()


def extract(data: bytes) -> dict | None:
    """PDF bytes → {markdown, tables, n_pages, n_tables, via, warnings},或 None 表示不适用。

    None 的两种情形,调用方都应回退到 docling:
      1. 没有文字层(扫描件)—— 坐标路径无从下手
      2. 有文字层但没有任何页含数值网格 —— 这份文档没有可提取的表,交给 docling 抽正文
    返回的形状和 docling_extract 完全一致,所以调用点不需要分支。
    """
    try:
        import pdfplumber
    except ImportError:
        return None

    md_parts: list[str] = []
    tables: list[dict] = []
    n_pages = 0
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            n_pages = len(pdf.pages)
            for page in pdf.pages:
                _ = page.chars
                objs = page._objects.get("char") or []
                if not objs:
                    continue                                  # 这一页没文字层(整份都没有 → 下面返回 None)
                page._objects["char"] = [_upright(c) for c in objs]
                lines = _column_lines(page)
                text = page.extract_text() or ""
                if not lines:
                    if text.strip():
                        md_parts.append(text.strip())          # 没表的页:正文照收
                    continue
                # find_tables 而不是 extract_tables:前者返回带 .bbox 的对象,后者只给数据。bbox 是把
                # 「表内」和「表外正文」分开的唯一依据 —— 少了它,同页既有正文又有表的新闻稿(IR 里最常见
                # 的形态)会连正文一起丢掉,而那正是这条管线要抓的内容。
                found = page.find_tables({"vertical_strategy": "explicit",
                                          "explicit_vertical_lines": lines,
                                          "horizontal_strategy": "text",
                                          "text_y_tolerance": 1})
                if not found:
                    if text.strip():
                        md_parts.append(text.strip())
                    continue
                tbl = max(found, key=lambda x: len(x.rows))
                grid = [[_clean(c) for c in r] for r in tbl.extract()]
                # horizontal_strategy='text' 把页面上**每一行文字**都当成表格行,所以表的上下缘会吞掉
                # 页首/页脚的散文(标题、导语、脚注)。定位真正的「数值区」——第一条和最后一条含数值的行。
                # {实测 ExxonMobil p1 表首吞掉 "To assist investors in assessing 3Q24 results…" 整段}
                numeric = [i for i, r in enumerate(grid) if any(_NUM.match(c) for c in r[1:])]
                if not numeric:
                    if text.strip():
                        md_parts.append(text.strip())
                    continue
                first, last = numeric[0], numeric[-1]
                # 数值区上方紧邻的那一行是列头(3Q24 / 2Q24 …),它不含纯数字所以不在 numeric 里,
                # 但它属于表而不是散文 —— 少了它,第一行数据会被当成表头。
                hdr = grid[first - 1] if first > 0 and sum(1 for c in grid[first - 1][1:] if c) >= 2 else []
                rows = [r for r in grid[first:last + 1] if any(r)]
                if not rows:
                    if text.strip():
                        md_parts.append(text.strip())
                    continue
                tables.append({"columns": hdr or rows[0], "rows": rows if hdr else rows[1:]})

                def _band(y0: float, y1: float) -> str:
                    """页面上 [y0, y1) 这条横带里的文字;越界或过窄则返回 ''。

                    散文一律用 crop + extract_text 取,**不从表格单元格拼回**:表的竖分割线会把一行
                    散文切成若干格,再用空格拼起来就会得到 'ava ilable in th is 8-K filing' 这种碎文本。
                    """
                    if y1 - y0 < 4:
                        return ""
                    try:
                        return (page.crop((0, max(y0, 0), page.width,
                                           min(y1, page.height))).extract_text() or "").strip()
                    except Exception:                              # noqa: BLE001 — 无效 crop → 当作空带
                        return ""

                # 阅读顺序:数值区上方的正文 → [TABLE n] → 下方的正文。用表格行自己的 bbox 定上下缘,
                # 所以正文和表的先后关系与原页一致,而不是把正文全堆在页末。
                hi = tbl.rows[max(first - (1 if hdr else 0), 0)].bbox[1]
                lo = tbl.rows[min(last, len(tbl.rows) - 1)].bbox[3]
                above = _band(0, hi)
                if above:
                    md_parts.append(above)
                md_parts.append(f"[TABLE {len(tables)}]")
                below = _band(lo, page.height)
                if below:
                    md_parts.append(below)
    except Exception as e:                                     # noqa: BLE001 — 任何解析异常都回退,不崩
        # 回退本身是安全的(docling 接手),但**必须留痕**:静默返回 None 会让一类系统性解析失败
        # 表现为「坐标路径覆盖率低」,而不是「坐标路径在这类文档上有 bug」——两者的修法完全不同。
        print(f"[coords] 解析失败,回退 docling — {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None

    if not any(md_parts) and not tables:
        return None
    return {"markdown": "\n\n".join(p for p in md_parts if p).strip(),
            "tables": tables, "n_pages": n_pages, "n_tables": len(tables),
            "via": "coords", "warnings": []}
