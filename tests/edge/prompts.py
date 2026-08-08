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

# 等级定义单独成文件: 抽取自评、消歧自评、落库分流三处引用同一套, 避免描述漂移。
from tests.edge.levels import LEVEL_SCHEMA, prompt_block

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
                "required": ["name", "kind", "search_keys", "role_in_text", "evidence", "level"],
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
                    # 确定度。node 和 edge 共用同一把尺子(levels.py), 0=结构化零推断 ~ 5=无法确定。
                    # WaterEvents 这条线产不出 0 —— 它读的是自然语言;保留 0 是为了和 SEC 边同尺。
                    "level": LEVEL_SCHEMA,
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["subject", "predicate", "object", "evidence", "level"],
                "properties": {
                    # subject/object 必须是上面 mentions 里的 name —— 代码强制校验引用完整性
                    "subject": {"type": "string"},
                    "object": {"type": "string"},
                    # 不预设 taxonomy: 让模型用自然的动词短语, 跑完 200 条再从实际输出归纳。
                    # 现在拍脑袋定一套关系类型, 一定会漏掉真实数据里的形态。
                    #
                    # ★ 约束是「可聚合」而不是「够短」。首版写的是「最多 4 个词」, 那是错的代理指标 ——
                    # 真正要的是能 group by(图谱的价值在于能查「所有 acquires 关系」), 而长度只是它的
                    # 一个副产品。而且限制词数和「不预设 taxonomy、跑完再归纳」自相矛盾。
                    # 模型违反时不靠这里挡, 靠跑完 200 条后看 predicate 的实际分布:
                    # 出现次数为 1 的 predicate 占比高 = 模型在写句子而不是给类型。
                    "predicate": {"type": "string"},
                    "valid_at": {"type": ["string", "null"]},
                    "valid_precision": {"enum": ["day", "month", "quarter", "year", "unknown"]},
                    # 比例/金额/条件状态等挂在边上的属性, 原样记录不做换算
                    "attrs": {"type": "object"},
                    "evidence": {"type": "string"},
                    # 边的等级取 max(关系本身的等级, 两端实体的等级) —— 短板决定成色:
                    # 关系读得再准, 主语指错了实体这条边照样是错的。
                    "level": LEVEL_SCHEMA,
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
- level: 确定度 0-5(见下面「确定度」一节)。**每个 mention 都必须给**

## edges —— 实体之间的关系
- subject / object: 必须是上面 mentions 里的 name
- predicate: 关系的**类型**, 不是这句话的复述。判断标准是「换一篇文章里同类的事,
  能不能用同一个 predicate」—— 能, 才是类型; 不能, 那是细节, 应该放进 attrs。
  例: acquires / divests_shareholding_in / launches / appoints / partners_with / supplies
  反例: "proposes a dividend of EUR0.80 per share in 2026 and a progressive annual increase..."
        —— 这是一句话不是类型, 换一家公司就复用不了。predicate 该是 proposes_dividend,
        金额和递增计划进 attrs
- object: **必须是 mentions 里另一个实体的名字。不允许 null / 空 / "None"。**
  如果一句话只是公司在说自己(上调指引、宣布分红、公布业绩、发布财报), 它没有客体 ——
  **整条边都不要出现在 edges 里**, 而不是产一条 object 为空的边。那是公司的属性不是关系
- valid_at + valid_precision: 关系发生的时间。文中说 "2017" 就填 "2017"+year, 说
  "since 2019" 就填 "2019"+year。**不要把年份补成某一天**
- attrs: 比例、金额、条件状态等原样记录, 如 {{"stake":"19.95%","status":"pending_regulatory_approval"}}
- evidence: 正文里的**逐字片段**
- level: 确定度 0-5(见下面「确定度」一节)。**每条 edge 都必须给**

# 确定度

{level_block}

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
                "required": ["mention", "decision", "level", "reason"],
                "properties": {
                    "mention": {"type": "string"},
                    # MATCH = 就是候选里的某一个;NEW = 候选里都不是。
                    # ★ 不再有 UNSURE 这个第三态 —— 「有多不确定」由 level 表达。
                    # 原因: UNSURE 是二元的, 它把「几乎肯定是这个但差一点证据」和「完全没头绪」
                    # 压成了同一个值, 而这两者的正确去向完全不同。改成等级后, 判断和确定度
                    # 是两个正交的维度: 你必须给一个判断, 同时诚实说明它有多确定。
                    "decision": {"enum": ["MATCH", "NEW"]},
                    "entity_id": {"type": ["integer", "null"]},
                    # 与 mention/edge 共用同一把尺子(levels.py)。level=5 → 挂起人工, 不写库。
                    "level": LEVEL_SCHEMA,
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
- decision: MATCH + entity_id(候选中某一条**就是**它)  或  NEW(候选里都不是它)
- level:    这个判断有多确定

{level_block}

# 注意
1. 名字像不等于是同一个。母公司和子公司名字往往只差几个字, 但**是两个不同的实体** ——
   "Luminor Holding AS"(控股公司)和 "Luminor Bank AS"(它持有的银行)不能合并。
2. 反过来, 同一个实体在不同文章里写法会不同("DNB Baltic Invest AB" / "DNB Baltic Invest"),
   结合它在文中的角色判断, 不要只看字面。
3. 候选里若有多条名字几乎一样但 cik 不同的, 说明数据库里本身可能有重复 —— 这种情况选证据最匹配的
   那条并给 level 4;实在分不清就给 level 5(会被挂起等人看)。
4. **必须给一个 decision, 但要诚实给 level。** 分不清就给 level 5 —— 它会被挂起等人看, 不写进库。
   判错成 MATCH 会污染已有节点, 判错成 NEW 会持续制造重复节点, 两者都比挂起更糟;
   但「不给判断」也没有用, 所以判断和确定度分开表达。

只输出 JSON, 不要任何解释文字。
"""


# ─────────────────────────────────────────────────────────────────────────────
# Step 1-R — 逐字校验失败后的定向重试
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY 要重试而不是直接丢: 逐字校验失败有两种完全不同的原因 ——
#   ① 抄写手滑: 内容读对了, 但 evidence 是复述/拼接/跨段落合并, 找不到原样片段
#   ② 凭空编造: 这件事正文里根本没有
# 直接丢弃等于把这两者当成同一件事, 而 ① 是可以救回来的 —— 让模型回去把原句找出来即可。
# 只有给了明确反馈仍然找不到, 才能判定为 ②。
#
# 重试**只针对失败的条目**, 不重跑整篇 —— 通过的部分没必要再问一次, 也避免模型
# 在第二轮改动已经正确的答案。
_RETRY_MAX = 1     # 一轮就够: 给了具体反馈还找不到原句, 基本就是编的

STEP1_RETRY_PROMPT = """你刚才从这篇文章里抽出的下面这些条目, 它们的 evidence 在正文里**找不到原样的片段**。

正文:
---
{body}
---

# 需要重新给 evidence 的条目

{failed_block}

# 要求

失败分两类, 分别处理:

**evidence 找不到原文** —— 如果这件事正文里确实说了, 把**原文那一句**原样抄出来
(可以更短, 只要能逐字找到; 不要跨段落拼接, 不要改写标点或大小写)。
正文里其实没有这件事, 就不要再返回它 —— 从结果里去掉即可, 这不是错误。

**边的端点不在 mentions 里** —— 两种情况:
- 端点是空的(object 为 null/None): 说明这句话没有客体, 它是公司在说自己 ——
  **不要再返回这条边**, 公司自述不是关系
- 端点写的名字和 mentions 里的对不上(比如边里写 "Zebra" 而 mention 是
  "Zebra Technologies Corporation"): 把边的端点改成 mentions 里的**完整名字**

只返回你能给出合格 evidence 的条目, 格式与上一轮相同(mentions / edges 两个列表)。
只输出 JSON, 不要任何解释文字。
"""
