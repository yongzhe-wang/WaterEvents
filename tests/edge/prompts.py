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
from tests.edge.iters import ITER_SCHEMA, prompt_block

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — 读文字, 出 mention 和 edge
# ─────────────────────────────────────────────────────────────────────────────

STEP1_SCHEMA = {
    "type": "object",
    "required": ["mentions", "attributes", "edges", "title_body_mismatch"],
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "kind", "search_keys", "role_in_text", "evidence", "iter"],
                "properties": {
                    "name": {"type": "string", "description": "文中出现的最完整写法"},
                    # asset 是 2026-08-08 新增的一档: 矿山/厂房/航线/牌照这类**资产**被抽成了 company,
                    # 于是产出 "Endeavour Silver --operates--> Guanaceví mine" 这种边 ——
                    # 资产是公司的财产不是独立主体, 不该作为边的端点。
                    "kind": {"enum": ["company", "person", "institution", "fund",
                                      "product", "asset", "other"]},
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
                    # 确定度。node 和 edge 共用同一把尺子(iters.py):
                    # 0=结构化零推断(SEC 专属) / 1=文本明说 / 2=从上下文推断。
                    # 三档是 200 条实测收敛的结果 —— 首版六档里 level3 只用了 1 次、level5 零次。
                    "iter": ITER_SCHEMA,
                },
            },
        },
        # ★ 新增: 实体自身的属性, 落 node_*_profile 而不是产边。
        # 200 条实测里 employs 独占 41 条(18%), 而「某人是某职位」本就是那个人的属性 ——
        # 把它写成 "公司 employs 某人" 会逼模型去选一个主体, 而它总选文章的主角公司,
        # 于是 Paul Hanson(Bitdeer Industrial 的董事长)被挂到了 Bitdeer Technologies Group 上。
        # 拆成 attribute(title=Chairman) + affiliation 边(Paul Hanson → Bitdeer Industrial)后,
        # 边的主体是本人, 母公司根本没有出现的位置 —— 偏差在结构上消失, 不靠 prompt 去劝。
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["entity", "key", "value", "evidence", "iter"],
                "properties": {
                    "entity": {"type": "string", "description": "必须是 mentions 里的 name"},
                    "key": {"enum": ["title", "headquarters", "founded", "industry",
                                     "employee_count", "ticker", "website", "other"]},
                    "value": {"type": "string"},
                    "evidence": {"type": "string"},
                    "iter": ITER_SCHEMA,
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["subject", "predicate", "object", "edge_class", "evidence", "iter"],
                "properties": {
                    # subject/object 必须是上面 mentions 里的 name
                    "subject": {"type": "string"},
                    "object": {"type": "string"},
                    # ★ 新增: 边的类别。与 water-graph 的 SEC 线对齐, 不另造一套 ——
                    # 现有 affiliated 归入 affiliation, holds/transacts 归入 transaction,
                    # 于是两条来源线可比, 查询方能按类别选要什么。
                    "edge_class": {"enum": ["affiliation", "transaction", "commercial",
                                            "product", "corporate"]},
                    # ★ MVP 阶段【不做 predicate 归类】。如实记录模型读出的关系措辞,
                    # 分类只靠 edge_class 五选一。
                    # {USER 2026-08-08 "we dont want to cateoy now as a mvp, we just want to
                    #  honestly report eveyrthing and then the only cateaogiresize is the 5 types"}
                    #
                    # 200 条实测出现 110 种 predicate、80 种只出现一次 —— 这个数字本身不是问题,
                    # 它是真实分布。等数据量足够时再从实际输出归纳 taxonomy, 现在归纳是过早优化。
                    #
                    # 唯一的约束是【数字/金额/日期不要写进 predicate】, 那与分类无关 ——
                    # "announces_loss_of_$610_million" 把金额埋进了字符串, 查询时取不出来;
                    # "reports_earnings" + attrs{"net_loss":"$610 million"} 才让它成为可查询的值。
                    "predicate": {"type": "string"},
                    "valid_at": {"type": ["string", "null"]},
                    "valid_precision": {"enum": ["day", "month", "quarter", "year", "unknown"]},
                    # 比例/金额/条件状态等挂在边上的属性, 原样记录不做换算
                    "attrs": {"type": "object"},
                    "evidence": {"type": "string"},
                    # 边的 iter 取 max(关系, 主体, 客体) —— 短板决定成色:
                    # 关系读得再准, 主语指错了实体这条边照样是错的。
                    "iter": ITER_SCHEMA,
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
- kind: company / person / institution / fund / product / asset / other
  · asset = 矿山、厂房、航线、牌照、物业这类**资产** —— 它是某家公司的财产, 不是独立主体。
    抽出来记录没问题, 但**不要拿它当边的端点**("公司 operates 某矿" 不是关系, 是资产归属)
- search_keys: **用来去数据库里找它的词, 最多 3 个**
  给能把这家机构和别家区分开的部分。不要给 Capital / Holdings / Group / Bank / Partners
  这类几乎每家公司都有的通用词 —— 那会召回上千条无关结果。
  例: "DNB Baltic Invest AB" → ["DNB Baltic", "DNB"]
      "Blackstone Capital Partners" → ["Blackstone Capital", "Blackstone"]   不要给 ["Capital"]
- role_in_text: 它在这段文字里是什么角色、和别的实体什么关系(一句话)
- aliases_in_text: 文中用到的其它写法或简称(如 "Luminor" 是 "Luminor Holding AS" 的简称)
- exchange_tag: 文中若写了 (NYSE: XXX) / (NASDAQ: XXX) 这类标记, 原样抄下来; 没有填 null
- evidence: 正文里的**逐字片段**(必须能在正文中原样找到)
- iter: 确定度 0/1/2(见下面「iter」一节)。**每个 mention 都必须给**

## attributes —— 实体自身的属性(**不是关系**)

某个实体「是什么」, 而不是它和别人之间发生了什么:
- title(职位名)/ headquarters / founded / industry / employee_count / ticker / website

每条给: entity(必须是 mentions 里的 name)· key · value · evidence(逐字)· iter

★ 「Paul Hanson, Chairman of Bitdeer Industrial」要拆成两部分:
    attribute: entity="Paul Hanson", key="title", value="Chairman"
    edge:      Paul Hanson --affiliation:officer_of--> Bitdeer Industrial
  **不要**写成 "Bitdeer employs Paul Hanson" —— 职位名是他的属性, 任职关系的主体是他本人。

## edges —— 实体之间的关系
- subject / object: 必须是上面 mentions 里的 name
- edge_class: 五选一
  · affiliation  谁跟谁有关系(任职 / 董事 / 子公司隶属 / 指数成员)——【状态】
    ★ **方向固定: 被隶属的一方做 subject, 所属的组织做 object。**
      人 → 公司 · 子公司 → 母公司 · 成员公司 → 指数
      对: Paul Hanson --officer_of--> Bitdeer Industrial
      错: Bitdeer Industrial --employs--> Paul Hanson   (方向反了, 查询时会漏一半)
    ★ **职位名不要写进 predicate** —— 它已经在 attributes 里了。
      predicate 统一用 officer_of / director_of / subsidiary_of / member_of,
      具体是 CEO 还是 Chairman 由 attributes 的 title 承载。
      对: Catherine Guo --officer_of--> Bitdeer Industrial  +  attr(title="CEO")
      错: Catherine Guo --is CEO of--> Bitdeer Industrial   (职位存了两份, predicate 也碎了)
  · transaction  所有权变动(收购 / 剥离 / 投资 / 合并)——【事件】
  · commercial   商业往来(合作 / 供货 / 客户 / 授权)——【事件】
    ★ 必须有一个**发生的动作**: 签了合同 / 达成协议 / 开始供货 / 授予许可。
      仅仅描述身份("是我们的 partner"、"是我们的客户"、"属于我们的生态")
      → 那是 affiliation(状态), 不是 commercial(事件)。
      例: 「signed a contract with Fluor to proceed with FEED Phase 2」  → commercial ✓
          「Zebra independent software vendor (ISV) partner, Spatialsolutions.ai」
          → 这是身份标签, 归 affiliation, 不是 commercial
  · product      产品动作(发布 / 上市 / 停产)——【事件】
  · corporate    公司自身动作(分红 / 回购 / 指引 / 任命)——【事件】
- predicate: 用你自己的话描述这个关系是什么, **不要套用固定词表** ——
  我们现在要的是如实记录, 不是提前分类。分类只靠上面的 edge_class 五选一。

  **但数字、金额、比例、日期、期间不要写进 predicate, 放进 attrs。**
  这不是为了归类, 是因为它们本来就是独立的字段, 塞进关系名会让它们查不出来:
    写成 "announces_loss_of_$610_million"     → 金额被埋在字符串里, 没法按金额筛
    写成 "reports_earnings" + attrs {{"net_loss": "$610 million"}}  → 金额是可查询的值
    写成 "reduces_capital_spending_by_30_percent" → 同理
    写成 "guides_capex" + attrs {{"change": "-30%"}}
  判断很简单: **predicate 里出现了数字或日期, 就说明有东西该挪进 attrs**。

- ★ predicate **不要带 edge_class 前缀**。写 "officer_of" 而不是 "affiliation:officer_of" ——
  类别已经在 edge_class 字段里了, 重复写进 predicate 会让同一种关系出现两种写法。
- object: **必须是 mentions 里另一个实体的名字。不允许 null / 空 / "None", 也不允许与 subject 相同。**
  ★ **股票代码、网址、总部地址、成立年份、行业**这些不是实体, 不能当 object ——
    它们是 attributes 里已有的 key(ticker / website / headquarters / founded / industry)。
    错: MaxLinear --stock_symbol--> NASDAQ:MXL      对: attr(MaxLinear.ticker = "MXL")
  公司自己的动作(发布财报、宣布派息、回购股票)没有第二个实体作客体 ——
  **整条边都不要出现在 edges 里**。这类信息若要保留, 走 attributes。
  例: 「Qnity Electronics today reported results for the first quarter」→ 不产边
      「Allegion's board declared a quarterly dividend of $0.41」→ 不产边(除非文中写明派给谁)
  如果一句话只是公司在说自己(上调指引、宣布分红、公布业绩、发布财报), 它没有客体 ——
  **整条边都不要出现在 edges 里**, 而不是产一条 object 为空的边。那是公司的属性不是关系
- valid_at + valid_precision: 关系发生的时间。文中说 "2017" 就填 "2017"+year, 说
  "since 2019" 就填 "2019"+year。**不要把年份补成某一天**
- attrs: 比例、金额、条件状态等原样记录, 如 {{"stake":"19.95%","status":"pending_regulatory_approval"}}
- evidence: 正文里的**逐字片段**
- iter: 确定度 0/1/2(见下面「iter」一节)。**每条 edge 都必须给**

# iter

{level_block}

# 硬性要求
1. evidence 必须是正文的逐字子串。改写、翻译、概括一律不接受 —— 会被程序当场丢弃。
2. ★ **先问「这句话的施动者是谁」, 再决定 subject。**
   最常见的错误是把**文章在讲的那家公司**当成所有关系的主体。文里提到的人和机构
   未必属于它 —— 可能属于它的子公司, 也可能属于完全无关的第三方。

   错: Bitdeer Technologies Group --employs--> Paul Hanson
       原文「Paul Hanson, Chairman of Bitdeer Industrial」
       → 他是 **Bitdeer Industrial**(子公司)的董事长。母公司和子公司是两个实体。
       对: attribute(Paul Hanson.title="Chairman")
           + edge(Paul Hanson --affiliation:officer_of--> Bitdeer Industrial)

   错: Bitdeer Technologies Group --communicates_with--> Taylor Adams
       原文「Taylor Adams, President and CEO of the Economic Development Authority of Western Nevada」
       → 他是另一个机构的负责人, 和这家公司没有这层关系。
       对: 不产这条边;要记就记 Taylor Adams --affiliation:officer_of--> EDAWN

   错: Otter Tail Corporation --receives_approval_for--> Otter Tail Power
       原文「In May, Otter Tail Power received approval from the Minnesota PUC」
       → 获批的是 **Otter Tail Power**。
       对: Otter Tail Power --corporate:receives_approval_from--> Minnesota PUC

3. 只写这段文字**说了**的。不要补充你知道但文中没说的事(比如你知道某公司在纽交所上市, 但文中没写, 就不要写)。
4. 读不出任何关系时, edges 给空数组, 并在 no_edge_reason 里说明原因。**不要为了凑数硬造边。**
   大多数事件(季报、年会、网播预告)本来就没有关系可抽, 那是正常的。
5. title_body_mismatch: 对比标题和正文**讲的是不是同一件事**。
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
                "required": ["mention", "decision", "iter", "unsure", "reason"],
                "properties": {
                    "mention": {"type": "string"},
                    # MATCH = 就是候选里的某一个;NEW = 候选里都不是。
                    # ★ 不再有 UNSURE 这个第三态 —— 「有多不确定」由 level 表达。
                    # 原因: UNSURE 是二元的, 它把「几乎肯定是这个但差一点证据」和「完全没头绪」
                    # 压成了同一个值, 而这两者的正确去向完全不同。改成等级后, 判断和确定度
                    # 是两个正交的维度: 你必须给一个判断, 同时诚实说明它有多确定。
                    "decision": {"enum": ["MATCH", "NEW"]},
                    "entity_id": {"type": ["integer", "null"]},
                    # 与 mention/edge 共用同一把尺子(iters.py)。分不清 → 走 unsure 字段挂起, 不占 iter 档位。
                    "iter": ITER_SCHEMA,
                    # 消歧分不清时置 true → 挂起人工, 不写库。它不是一个 iter 档位 ——
                    # 「不确定」不该作为一种确定度写进事实层。
                    "unsure": {"type": "boolean"},
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
- iter:     这个判断是照着候选信息直接对上的(1)还是推出来的(2)
- unsure:   分不清就置 true —— 会被挂起等人看, 不写进库

{level_block}

# 注意
1. 名字像不等于是同一个。母公司和子公司名字往往只差几个字, 但**是两个不同的实体** ——
   "Luminor Holding AS"(控股公司)和 "Luminor Bank AS"(它持有的银行)不能合并。
2. 反过来, 同一个实体在不同文章里写法会不同("DNB Baltic Invest AB" / "DNB Baltic Invest"),
   结合它在文中的角色判断, 不要只看字面。
3. 候选里若有多条名字几乎一样但 cik 不同的, 说明数据库里本身可能有重复 —— 这种情况选证据最匹配的
   那条并给 iter 2;实在分不清就把 unsure 置 true(会被挂起等人看)。
4. **必须给一个 decision, 分不清就把 unsure 置 true。** 挂起的会等人看, 不写进库。
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


# ─────────────────────────────────────────────────────────────────────────────
# Step 1.5 — 验证:让模型自己复核抽出来的边
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY 用模型验证而不是写规则:
# 首版我写了一条程序化检查「evidence 里出现了非本边端点的实体 → 张冠李戴嫌疑」,
# 200 条上报出 42 条。实际读下来一半是**误报** —— 比如
#   Zebra Technologies Corporation --partners_with--> Spatialsolutions.ai
#   证据「In first place is Zebra independent software vendor (ISV) partner, Spatialsolutions.ai」
# 规则把 "Zebra" 当成了另一个实体, 而它只是同一家公司的简称。
# 我的下意识反应是再加一条「用 aliases 归一」的规则去补 —— 那是给规则打补丁,
# 补丁还会有新的边界情况(缩写、旧称、译名、带法人后缀与否…)永远补不完。
#
# 分界线应该是:
#   · 纯机械的事实检查 → 代码。evidence 是不是正文的逐字子串, 这是字符串包含, 不需要理解任何东西
#   · 需要理解语义的判断 → 模型。这个简称指不指同一家、这句话的施动者是母公司还是子公司
# {USER 2026-08-08 "you shouldnt check right, use llm to determien"}
# {USER 2026-08-08 "DECREASE THE NUMBER OF HYPERPARAMETER, AND USE LLM AS POSSIBLE"}
#
# 验证要抓的真错(200 条实测, 同一个系统性偏差):
#   Bitdeer Technologies Group --employs--> Paul Hanson
#     证据「Paul Hanson, Chairman of Bitdeer Industrial」—— 他是子公司的董事长
#   Bitdeer Technologies Group --communicates_with--> Taylor Adams
#     证据「Taylor Adams, President and CEO of the Economic Development Authority of Western Nevada」
#     —— 他根本不是 Bitdeer 的人
#   Otter Tail Corporation --receives_approval_for--> Otter Tail Power
#     证据「In May, Otter Tail Power received approval from the Minnesota PUC」—— 主体是子公司
# 模型把「文章的主角公司」当成了所有关系的默认主体。验证这一步就是让它回头看这件事。

VERIFY_SCHEMA = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["i", "verdict", "reason"],
                "properties": {
                    "i": {"type": "integer", "description": "边的序号"},
                    # keep   = 这条边成立, 主体客体都对
                    # fix    = 关系成立但主体或客体写错了 —— 给出改正后的
                    # drop   = 证据不支持这条关系
                    "verdict": {"enum": ["keep", "fix", "drop"]},
                    "subject": {"type": ["string", "null"], "description": "verdict=fix 时给改正后的主体"},
                    "object": {"type": ["string", "null"], "description": "verdict=fix 时给改正后的客体"},
                    "iter": ITER_SCHEMA,
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

VERIFY_PROMPT = """下面是从一篇文章里抽出来的关系。请对照原文逐条复核。

# 原文
---
{body}
---

# 待复核的关系

{edges_block}

# 对每一条给出

- verdict:
  · keep — 这条关系成立, 主体和客体都对
  · fix  — 关系成立, 但**主体或客体写错了**。给出改正后的 subject / object
  · drop — 原文不支持这条关系
- iter: 0/1/2(见下)
- reason: 一句话说明

# 复核时重点看这两件事

**① 主体是不是被写成了文章的主角公司**

这是最常见的错误。文章通常围绕一家公司写, 但文里提到的人和机构未必都属于它:

  错: "Bitdeer Technologies Group --employs--> Paul Hanson"
      证据「Paul Hanson, Chairman of Bitdeer Industrial」
      → Paul Hanson 是 **Bitdeer Industrial** 的董事长, 不是 Bitdeer Technologies Group 的。
        母公司和子公司是两个实体。verdict=fix, subject 改成 Bitdeer Industrial

  错: "Bitdeer Technologies Group --communicates_with--> Taylor Adams"
      证据「Taylor Adams, President and CEO of the Economic Development Authority of Western Nevada」
      → 他是另一个机构的负责人, 和主角公司没有这层关系。verdict=drop

**② 简称和全称是同一个实体, 不要因为写法不同就判错**

  对: "Zebra Technologies Corporation --partners_with--> Spatialsolutions.ai"
      证据「In first place is Zebra independent software vendor (ISV) partner, Spatialsolutions.ai」
      → "Zebra" 就是 "Zebra Technologies Corporation" 的简称, 同一家。verdict=keep

# iter

{level_block}

只输出 JSON, 不要任何解释文字。
"""


def _selftest_format() -> None:
    """用假参数把三个 prompt 各 format 一遍 —— 任何未转义的字面花括号都会当场 KeyError。

    WHY 需要它: prompt 里经常要写 JSON 例子({"key": "value"}), 而 str.format 会把单花括号
    当成占位符。2026-08-08 加「数字进 attrs」的例子时就踩了这个坑, 报 KeyError: '"net_loss"',
    而且是在跑批开始后才炸 —— 白等一轮。
    这个自检在 import 时不跑, 由 edge_run/verify 启动时调用, 失败即刻停, 不浪费 LLM 调用。
    """
    STEP1_PROMPT.format(ticker="", ir_url="", title="", etype="", date="",
                        precision="", body="", level_block="")
    STEP1_RETRY_PROMPT.format(body="", failed_block="")
    VERIFY_PROMPT.format(body="", edges_block="", level_block="")
