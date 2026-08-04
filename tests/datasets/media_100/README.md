# media_100 — stage-2 handler-coverage dataset (100 real events)

用一句话讲完: 从生产库 255,608 个真实 event 里分层抽 100 个,**按"这个 event 会走哪条 handler 分支"分层,不按人口比例**
—— 目的是让 `media_agent` 的每一条路径(html / pdf / xlsx / docx / pptx / audio / video / webcast)都至少被跑到十几次,
而不是复刻一个 60% 都是 html 的分布。跑法见 `tests/media/media_run.py`,用 `MEDIA_RUN_DATASET` 指过来。

```bash
MEDIA_RUN_DATASET=tests/datasets/media_100 MEDIA_RUN_OUT=tests/media_output \
  python3 tests/media/media_run.py
```

## 为什么不按比例抽

按人口比例抽 100 条会得到 **0 条 pptx、0 条 docx、~1 条 video** —— 而这三条恰恰是最可能坏、最少被跑过的分支。
数据集的作用是**覆盖代码路径**,不是描述总体。所以稀有 kind 被显著过采样。

| stratum | n | 走哪个 handler | 总体占比(URL 级) |
|---|---:|---|---:|
| `html_only` | 25 | `handle_html` → watercrawl + Qwen-VL | 60.95% |
| `pdf` | 25 | `handle_office` → Docling | 29.93% |
| `webcast` | 12 | ⚠️ **无对应 handler** — 验证是 `skipped` 还是静默丢弃 | 3.68% |
| `audio` | 10 | `handle_audio` → faster-whisper | 0.29% |
| `video` | 10 | yt-dlp → WhisperX(注释称 YouTube 会 `skipped:needs-ytdlp`) | 0.58% |
| `xlsx` | 8 | `handle_office` → Docling | 4.23% |
| `docx` | 5 | `handle_office` → Docling | 0.11% |
| `mixed` | 3 | 含 pdf 且 ≥4 个 url — 验证闭环 fill-and-append + content-hash 去重 | — |
| `pptx` | 2 | `handle_office` → Docling(**全库只有 12 个 pptx url**) | 0.00% |

分层规则:一个 event 按它 media_urls 里**最稀有的那个 kind** 归层(pptx > audio > video > docx > webcast > xlsx > pdf > html),
所以每层的样本一定含有该层要测的资源类型。

## 市场:刻意 50/50

| | 数据集 | 总体 |
|---|---:|---:|
| 美股 / ADR | **52** | 85.3% |
| 非美股 | **48** | 14.7% |

非美股被显著过采样(48% vs 14.7%),因为它们测的是两个不同的风险:
1. **CJK 语言抽取** — 日/台/韩/港/沪的 IR 页是中日韩文,Qwen-VL 在这上面的抽取质量没验证过
2. **反爬** — 外国 IR 站是 Cloudflare / Incapsula 拦截最集中的地方

明细:US 52 · 日本 11 · 台湾 10 · 韩国 10+1 · 香港 7 · 上海 5 · 伦敦 4

## 排除 SEC —— 按 URL 排,不按 event_type 排

任何 `media_urls` 里含 `sec.gov` 的 event **一律排除**(样本内 SEC 泄漏 = 0)。三条理由:

1. **`focusalpha-backend` 已经把 SEC 做得好得多** — `item-splitter.ts` 4,132 行、`SPLITTER_SCHEMA_VERSION=14`,
   做的是 item 级切分且有通过的测试证明 Item-7 MD&A 边界精确。丢进 media_agent 的通用 Docling 路径产出只会更差。
2. **只占 0.23%**(1,144 / 488,710 个 URL),排掉几乎不损失覆盖。
3. **EDGAR 有 fair-access 限速**,而 `focusalpha-backend` 已用 `edgar_rate_bucket` 单例令牌桶正确遵守。
   第二个不协调的爬虫会让整个组织被限速。

⚠️ **但没有排除 `event_type='filing'`(总体 22%,数据集 13 条)** —— 生产库里 `source_url` 含 sec.gov 的是 **0** 条,
说明这些 "filing" 几乎全是**非 SEC** 的外国监管备案 / 交易所公告 / IR 站自托管文档,那正是 WaterEvents 相对
FocusAlpha 的差异化价值所在。

## 每条记录的 schema

```jsonc
{
  "id":        "ev_001_audio",              // 文件名同名;runner 的 glob 是 ev*.json
  "event_url": "https://…",                  // 详情页:media_urls 里第一个非文档 url,退化到 source_url
  "known_event": {                           // 直接喂给 enrich_page(known_event, page)
    "title": "…", "date": "…", "type": "…",
    "media_urls": ["…"]                      // 全部已知资源 — 闭环的 oracle 起点
  },
  "meta": {                                  // 仅供分层分析,runner 忽略
    "db_event_id": "<uuid>", "ticker": "…", "market": "US|T|TW|KS|HK|SS|L|KQ",
    "stratum": "audio", "n_urls": 6,
    "kinds": {"pdf": true, "audio": true, …},
    "source_url": "…"
  }
}
```

`_manifest.json` 存聚合统计(以 `_` 开头,不会被 `ev*.json` 的 glob 读到)。

## 已知性质

- **每个 event 都至少有 1 个 media_url**(生产库里 0 个空的),所以没有"无从下手"的样本
- URL 数分布:1 个 = 56 · 2 个 = 20 · 3 个 = 8 · 4–6 个 = 16
- 事件类型铺开 9 种:press_release 24 · presentation 18 · earnings 17 · conference 13 · filing 13 · webcast 11 · 其余 4
- **不含 ground-truth 标注** —— 跟 `media_smoke.py` 的 oracle 数据集不同,这里按
  {USER 2026-07-23 "dont rely on ground truth read the output yourself"} 的原则,靠人读 trace 判质量

## 复现

抽样是确定性的(`md5(id::text)` 排序,非随机种子),同一个库跑同样的 SQL 会得到同一批 100 条。
SQL 见本次提交的 commit message。
