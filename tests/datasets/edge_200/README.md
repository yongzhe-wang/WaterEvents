# edge_200 — 事件文本 → 图谱节点与边的实验集(200 个真实事件)

用一句话讲完: 从生产库 240,707 个事件里(其中 22,547 个已有正文)分层抽 200 个,**按「这条能不能自动
判对错」分层**,把评测切成机器能算准确率的一半和只能人读的一半 —— 因为边抽取没有现成的 ground truth,
而完全靠人读 200 条又太慢。

```bash
WATEREVENTS_DB_DSN=<supavisor pooler dsn> \
EDGE_SAMPLE_OUT=tests/datasets/edge_200 \
  python3 tests/edge/sample.py
```

## 分层与理由

| stratum | n | 是什么 | 怎么评测 |
|---|---:|---|---|
| `auto_verifiable` | 100 | 正文里写了 `(NYSE: HCA)` 这类交易所标记的 press_release | **机器**:ticker 即 ground truth |
| `hard_no_tag` | 60 | 不带标记的 press_release —— 私营机构 / 子公司 / 外国实体全在这层 | **人读** |
| `other_types` | 25 | earnings / filing / conference / dividend / shareholder_meeting | 人读 |
| `expect_no_edge` | 15 | 纯日程类(webcast / presentation,标题 < 60 字符) | 人读,**预期产不出边** |

### 为什么第一层能做到「不用标注也能算准确率」

`(NYSE: HCA)` 写在正文里时,这个 mention 的正确答案是**确定的** —— ticker 能直接对到 water-graph 的
`node_company_profile.primary_symbol → cik`。这是调 prompt 时唯一的客观标尺,而且**完全免费**。

生产库里这种标记的覆盖率:

```
press_release 正文  10,138 条
含交易所标记        5,684 条 (56.1%)
平均每份 1.33 个,最多 21 个
```

### 但它只覆盖简单案例 —— 所以必须有第二层

带 ticker 的都是上市公司,库里大概率已有。真正难的恰恰没有 ticker:

```
Luminor Holding AS      挪威/波罗的海控股公司,库里 0 条
DNB Baltic Invest AB    子公司
OTP Bank Plc            匈牙利银行
```

而同一段文字里还会同时出现 `Luminor Holding AS`(控股)、`Luminor Bank AS`(它 100% 持有的银行)、
简称 `Luminor` —— **正确答案是两个实体加一个指代**,纯字符串匹配必错。这类案例只能人读,所以第二层
占了 60 条。

### 为什么 press_release 占 80%

总体里有正文的事件中它只占 42%(9,554 / 22,547),这里被显著过采样,因为**边几乎只出现在它里面**。
实测 22 条美股事件标题,只有 3–4 条能读出明确的边,而那几条全是 press_release;其余是
`First Quarter 2023` / `Annual General Meeting` / `2023 Annual Webcast` 这类纯日程。

同 `media_100` 的原则:数据集的作用是**覆盖要测的情况**,不是描述总体。

## 每条记录的 schema

```jsonc
{
  "id": "ev_001_auto_verifiable",
  "input": {                        // 整包喂给 Step 1 的 LLM
    "company": {"ticker": "WLYB", "ir_url": "…"},
    "title": "…", "type": "press_release",
    "date": "2026-06-09", "date_precision": "day",
    "body": "…"                     // event_documents.md 全文,不截断(截断策略交给 runner)
  },
  "meta": {                         // 仅供分层与评测,抽取时忽略
    "stratum": "auto_verifiable",
    "db_event_id": "<uuid>",
    "n_chars": 6213,
    "doc_url": "…", "source_url": "…",
    "exchange_tags": ["(NYSE: WLY)"]   // 自动评测锚点,整块保留不解析
  }
}
```

## 已知性质与坑

- **正文长度**:中位 5,012 字 · 均值 7,374 · 最短 978 · 最长 47,737
- **日期粒度**:day 175 · quarter 16 · month 4 · year 2 · unknown 3 —— 是文本不是日期类型,
  `edge_claim` 的 `valid_at + valid_precision` 能原样接住,**不要把 `2023-Q1` 补成某一天**
- **⚠ 正文 ticker ≠ `companies.ticker`**:`(NYSE: WLY)` 而 `companies.ticker` 是 `WLYB`;
  `(Euronext Paris: GET)` 而 ticker 是 `GRPTY`。ADR / 本地上市是两个代码,**评测时不能直接字符串相等**
- **双重上市**:`(NYSE: QSR)` 与 `(TSX: QSP)` 会同时出现,两个都保留
- **标题-正文可能不匹配**:实测有标题写「1月客流」而正文写「2030年EBITDA目标」的情况(抓到了列表页),
  Step 1 的输出里有 `title_body_mismatch` 字段报告它 —— **不自动丢弃,先看数据**
- **不含 ground-truth 标注**:除 `auto_verifiable` 层的 ticker 锚点外,其余按
  {USER 2026-07-23 "dont rely on ground truth read the output yourself"} 靠人读输出判质量

## 复现

抽样确定性:`order by md5(event_id::text)`,非随机种子 —— 同一个库跑两次得到同一批 200 条。
