"""tests/edge/prompts.py — 两个 LLM 环节的 prompt 与输出 schema。

用一句话讲完: Step 1 让模型**只读文字**, 输出这段文字里出现了哪些实体(mention)和哪些关系(edge);
Step 3 让模型**带着库里的候选**判断每个 mention 是复用已有节点还是新建。中间夹一层纯 SQL 召回, 不做任何判断。

## 为什么把「检索词」也交给模型给(Step 1 的 search_keys)

原本这里要写一套归一化规则(去掉 Inc / Ltd / LLC / AS / Plc / S.a r.l 后缀、去标点、大写…)。那是一堆
需要调的东西, 而且永远调不全 —— 各国法人后缀写法太多。改成让模型直接给检索词, 这套规则就不存在了:
模型天生知道 "DNB Baltic Invest AB" 该用 "DNB Baltic" 去搜。

但检索词给得好不好直接决定成本 —— 实测库里 ILIKE 召回量:
{PSQL 2026-08-08  Luminor 0 · Nordea 1 · Rubrik 1 · DNB 3 · Wiley 11 · Advanced 19 · Blackstone 72
                  · Bank 246 · Holdings 1,304 · Capital 1,474}
[CONFIDENCE: CONFIRMED 100% — 对 node_entity 逐词 count(*) ILIKE 实测]

所以 prompt 里明确要求: **给能把这家机构和别家区分开的词, 不要给 Capital / Holdings / Group / Bank
这类所有公司都有的通用词**。给对了 90% 的 mention 只需一次调用; 给错了会召回上千条, 直接判 AMBIGUOUS
挂起, 白跑一趟。

## 为什么 Step 3 一次看整个事件, 而不是逐个 mention 判

样本 1(DNBBY)同一段文字里同时出现 `Luminor Holding AS`(控股公司)、`Luminor Bank AS`(它 100% 持有的
银行)、以及简称 `Luminor`。逐个判断的话三者会召回到同一批候选、被判成同一个实体; **只有同时看才能分开**
—— 正确答案是两个实体加一个指代。这一条比任何参数调优都重要。
{event_documents 实文 "Luminor Holding AS is a regulated holding company owning 100% of the shares in Luminor Bank AS"}
[CONFIDENCE: CONFIRMED 100% — 原文逐字]

## 不做 ground truth 标注

按 {USER 2026-07-23 "dont rely on ground truth read the output yourself"}, 质量靠人读输出判断。
唯一的自动校验是**证据逐字校验**(见 validate.py)—— 那不需要标注, 是纯字符串包含检查。
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — 读文字, 出 mention 和 edge
# ─────────────────────────────────────────────────────────────────────────────

STEP1_SCHEMA = {
    "type": "object",
    "required": ["mentions", "edges", "title_body_mismatch"],
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "kind", "search_keys", "role_in_text", "evidence"],
                "properties": {
                    "name": {"type": "string", "description": "文中出现的最完整写法"},
                    "kind": {"enum": ["company", "person", "institution", "fund", "product", "other"]},
                    # 检索词。给能区分的部分, 不给通用后缀 —— 见模块 docstring 的召回量实测。
                    "search_keys": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    # 这个实体在本文中扮演什么。Step 3 消歧时它是主要判据 ——
                    # 光看名字分不出两个同名的 Blackstone Family Tactical Opportunities。
                    "role_in_text": {"type": "string"},
                    # 文中出现的其它写法/简称。新建节点时一并写进 node_alias, 让下次能召回到。
                    "aliases_in_text": {"type": "array", "items": {"type": "string"}},
                    # 正文里若写了 (NYSE: XXX) 这类标记, 抄在这 —— 它是最强的锚点。
                    "exchange_tag": {"type": ["string", "null"]},
                    "evidence": {"type": "string", "description": "正文中的逐字片段"},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["subject", "predicate", "object", "evidence"],
                "properties": {
                    # subject/object 必须是上面 mentions 里的 name —— 代码强制校验引用完整性
                    "subject": {"type": "string"},
                    "object": {"type": "string"},
                    # 不预设 taxonomy: 让模型用自然的动词短语, 跑完 200 条再从实际输出归纳。
                    # 现在拍脑袋定一套关系类型, 一定会漏掉真实数据里的形态。
                    "predicate": {"type": "string"},
                    "valid_at": {"type": ["string", "null"]},
                    "valid_precision": {"enum": ["day", "month", "quarter", "year", "unknown"]},
                    # 比例/金额/条件状态等挂在边上的属性, 原样记录不做换算
                    "attrs": {"type": "object"},
                    "evidence": {"type": "string"},
                },
            },
        },
        # 标题和正文说的不是一回事时报出来。样本 2(GRPTY)标题「1月客流」正文「2030年EBITDA目标」,
        # 抓到的正文很可能不属于这个标题。不设闸门自动丢弃 —— 先报出来看数据。
        # {event_documents 实测 标题 "[Shuttle traffic in January 2026]" vs 正文
        #  "Getlink announces a new phase of medium-term growth, and targets €1 billion EBITDA by 2030"}
        "title_body_mismatch": {"type": "boolean"},
        # 读不出边时必填。不允许沉默返回空数组。
        "no_edge_reason": {"type": ["string", "null"]},
    },
}

STEP1_PROMPT = """你在读一家公司的投资者关系页面, 任务是抽出这段文字**明确说了**的实体和关系。

# 输入
公司: {ticker} ({ir_url})
标题: {title}
类型: {etype}
日期: {date} (粒度: {precision})

正文:
---
{body}
---

# 输出两个列表

## mentions —— 文中出现的实体
每个实体给:
- name: 文中最完整的写法(如 "Luminor Holding AS", 不要缩成 "Luminor")
- kind: company / person / institution / fund / product / other
- search_keys: **用来去数据库里找它的词, 最多 3 个**
  给能把这家机构和别家区分开的部分。不要给 Capital / Holdings / Group / Bank / Partners
  这类几乎每家公司都有的通用词 —— 那会召回上千条无关结果。
  例: "DNB Baltic Invest AB" → ["DNB Baltic", "DNB"]
      "Blackstone Capital Partners" → ["Blackstone Capital", "Blackstone"]   不要给 ["Capital"]
- role_in_text: 它在这段文字里是什么角色、和别的实体什么关系(一句话)
- aliases_in_text: 文中用到的其它写法或简称(如 "Luminor" 是 "Luminor Holding AS" 的简称)
- exchange_tag: 文中若写了 (NYSE: XXX) / (NASDAQ: XXX) 这类标记, 原样抄下来; 没有填 null
- evidence: 正文里的**逐字片段**(必须能在正文中原样找到)

## edges —— 实体之间的关系
- subject / object: 必须是上面 mentions 里的 name
- predicate: **一个简短的动词短语, 最多 4 个词**(divests_shareholding_in / acquires /
  launches / appoints / partners_with ...), 不必套用固定词表。
  不要把整句话写成 predicate —— 细节放进 attrs
- object: **必须是另一个实体**。如果一句话只是公司在说自己(上调指引、宣布分红、公布业绩),
  那它没有客体, **不要产这条边** —— 那是公司的属性不是关系
- valid_at + valid_precision: 关系发生的时间。文中说 "2017" 就填 "2017"+year, 说
  "since 2019" 就填 "2019"+year。**不要把年份补成某一天**
- attrs: 比例、金额、条件状态等原样记录, 如 {{"stake":"19.95%","status":"pending_regulatory_approval"}}
- evidence: 正文里的**逐字片段**

# 硬性要求
1. evidence 必须是正文的逐字子串。改写、翻译、概括一律不接受 —— 会被程序当场丢弃。
2. 只写这段文字**说了**的。不要补充你知道但文中没说的事(比如你知道某公司在纽交所上市, 但文中没写, 就不要写)。
3. 读不出任何关系时, edges 给空数组, 并在 no_edge_reason 里说明原因。**不要为了凑数硬造边。**
   大多数事件(季报、年会、网播预告)本来就没有关系可抽, 那是正常的。
4. title_body_mismatch: 对比标题和正文**讲的是不是同一件事**。
   例: 标题 "[Shuttle traffic in January 2026]" 而正文通篇在讲 "targets EUR1 billion EBITDA by 2030"
   —— 这是两件不同的事, 填 true。抓取时可能抓到了列表页或另一篇稿, 这个标记是唯一的线索。

只输出 JSON, 不要任何解释文字。
"""


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — 带库里的候选做消歧
# ─────────────────────────────────────────────────────────────────────────────

STEP3_SCHEMA = {
    "type": "object",
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["mention", "decision", "reason"],
                "properties": {
                    "mention": {"type": "string"},
                    # MATCH  = 就是候选里的某一个
                    # NEW    = 候选里都不是, 库里没有这个实体
                    # UNSURE = 分不清 —— 挂起进人工队列, 不写库。
                    #          UNSURE 是一等公民: 消歧错了比不做更糟, 因为错误会沿着图传播。
                    "decision": {"enum": ["MATCH", "NEW", "UNSURE"]},
                    "entity_id": {"type": ["integer", "null"]},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

STEP3_PROMPT = """你要判断一篇文章里提到的机构, 是不是数据库里已有的那些。

# 文章上下文
标题: {title}
公司: {ticker}

# 本文提到的全部实体(一起看, 因为它们之间可能有母子/简称关系)
{mentions_block}

# 待判断的实体
名称: {mention_name}
它在文中的角色: {role_in_text}
原文依据: {evidence}
文中的其它写法: {aliases}
交易所标记: {exchange_tag}

# 数据库候选(第 {block_i}/{block_n} 块, 共 {n_cand} 条)
{candidates_block}

# 判断
对「{mention_name}」给出:
- MATCH  + entity_id: 候选中某一条**就是**它
- NEW:    候选里都不是它
- UNSURE: 分不清

# 注意
1. 名字像不等于是同一个。母公司和子公司名字往往只差几个字, 但**是两个不同的实体** ——
   "Luminor Holding AS"(控股公司)和 "Luminor Bank AS"(它持有的银行)不能合并。
2. 反过来, 同一个实体在不同文章里写法会不同("DNB Baltic Invest AB" / "DNB Baltic Invest"),
   结合它在文中的角色判断, 不要只看字面。
3. 候选里若有多条名字几乎一样但 cik 不同的, 说明数据库里本身可能有重复 —— 这种情况选证据最匹配的
   那条; 实在分不清就 UNSURE。
4. **宁可 UNSURE 也不要乱猜。** 判错成 MATCH 会污染已有节点, 判错成 NEW 会持续制造重复节点 ——
   两者都比挂起等人看更糟。

只输出 JSON, 不要任何解释文字。
"""
