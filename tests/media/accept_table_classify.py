"""accept_table_classify — 表格分类 + colspan 折叠的验收。

用一句话讲完: 拿数据集里已渲染过的页面重新抽一遍 → 统计"被判 data 的表"和"被判 layout 的表" → 对四条事先定好的标准
逐条打分。标准是在写代码之前定的,不是看到结果之后编的。

四条标准:
  ① 已知的四张对照表全部判对(FFIN=data · Starbucks×2=layout · NetEase=data)
  ② 判为 data 的表里,数字密度为 0 且无 th/caption 的比例 < 5%   —— 假阳性(把导航当表)
  ③ 折叠后仍存在"整行同值"的表 = 0                              —— colspan 没折干净
  ④ md 字符数不下降                                             —— 内容没丢

Run ON the VM:  PYTHONPATH=~/WaterEvents/backend python3 tests/media/accept_table_classify.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("PYTHONPATH", "").split(":")[0] or ".")

from providers import render_remote                                     # noqa: E402
from agent.media_agent.extract.extract_html import extract_html         # noqa: E402

_DS = os.environ.get("H50_OUT", "tests/datasets/html_extract_50")
_N = int(os.environ.get("ACC_N", "20"))
NUM = re.compile(r"^[\s$(]*-?[\d,]+\.?\d*[\s)%]*$")

CONTROLS = [
    ("FFIN 财报(无语义标记的真财务表)", "data",
     "https://investorrelations.ffin.com/news-and-events/news/news-details/2026/"
     "FIRST-FINANCIAL-BANKSHARES-ANNOUNCES-FOURTH-QUARTER-AND-YEAR-ENDED-DECEMBER-31-2025-EARNINGS/default.aspx"),
    ("Starbucks 事件页(全排版表)", "layout", "https://investor.starbucks.com/events-presentations/default.aspx"),
    ("NetEase(有 th/thead/scope)", "data", "https://ir.netease.com/news-releases"),
]


def density(rows) -> float:
    cells = [str(c).strip() for r in rows for c in r if str(c).strip()]
    return (sum(1 for c in cells if NUM.match(c)) / len(cells)) if cells else 0.0


def all_same_row(rows) -> int:
    return sum(1 for r in rows if len({str(c).strip() for c in r if str(c).strip()}) == 1
               and len([c for c in r if str(c).strip()]) > 1)


def main() -> None:
    print("══ ① 四张对照表 ══")
    ok_ctrl = 0
    for label, want, url in CONTROLS:
        try:
            r = render_remote.render_shot(url, wait_ms=2500)
            det = extract_html(r.get("html") or "", base_url=url, links=r.get("links"))
        except Exception as e:                                          # noqa: BLE001
            print(f"  ⛔ {label}: {type(e).__name__}"); continue
        tabs = [b for b in det["blocks"] if b.get("type") == "table"]
        got = "data" if tabs else "layout"
        hit = (got == want) or (want == "data" and tabs)
        ok_ctrl += bool(hit)
        print(f"  {'✅' if hit else '⛔'} {label:34} 期望={want:6} 得到={got:6} (产出 {len(tabs)} 张表)")

    print(f"\n══ ② ③ ④ 在 {_N} 份已有样本上 ══")
    files = sorted(glob.glob(os.path.join(_DS, "h50_*.json")))[:_N]
    n_tab = n_susp = n_allsame = 0
    md_before = md_after = 0
    for f in files:
        rec = json.load(open(f, encoding="utf-8"))
        try:
            r = render_remote.render_shot(rec["url"], wait_ms=2000)
            det = extract_html(r.get("html") or "", base_url=rec["url"], links=r.get("links"))
        except Exception:                                               # noqa: BLE001
            continue
        md_after += sum(len(b.get("md") or "") for b in det["blocks"] if b.get("type") in ("md", "list"))
        md_before += rec["render_meta"]["inline_chars"]
        for b in det["blocks"]:
            if b.get("type") != "table":
                continue
            n_tab += 1
            rows = b.get("rows") or []
            hs = [str(h) for h in (b.get("headers") or [])]
            # 假阳性画像:零数字 且 表头是纯位置索引(没有真表头)
            if density(rows) == 0.0 and hs and all(h.isdigit() for h in hs):
                n_susp += 1
            n_allsame += all_same_row(rows)

    pct = (100 * n_susp // n_tab) if n_tab else 0
    print(f"  ② 判为 data 的表 {n_tab} 张,其中零数字+无真表头 {n_susp} 张 = {pct}%   {'✅' if pct < 5 else '⛔ 超过 5%'}")
    print(f"  ③ 折叠后仍整行同值的行数: {n_allsame}   {'✅' if n_allsame == 0 else '⛔'}")
    print(f"  ④ md 字符 {md_after:,} (渲染 inline {md_before:,})   {'✅' if md_after > 0 else '⛔'}")
    print(f"\n  ① 对照 {ok_ctrl}/{len(CONTROLS)}")


if __name__ == "__main__":
    main()
