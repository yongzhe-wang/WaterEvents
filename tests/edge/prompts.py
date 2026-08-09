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

STEP1_PROMPT = """你在读一家公司的投资者关系页面。

# 我想要什么

我在建一张**公司关系图**。我关心的是:**这家公司和外界发生了什么关系** ——
它跟谁做了生意、买了谁、投了谁、谁在它那里任职、它发布了什么。

我不关心:这篇稿子的文风、它怎么描述自己的业绩有多好、它的季度数字。
那些信息有别的地方存,不该变成图上的连线。

# 输入
公司: {ticker} ({ir_url})
标题: {title}
类型: {etype}
日期: {date} (粒度: {precision})

正文:
---
{body}
---

# 请给我三样东西

## 1. mentions —— 这段文字里出现了哪些**能独立存在的主体**

公司、人、机构、基金、产品。
矿山、厂房、牌照、航线这类是某家公司的**财产**, 也记下来(kind=asset), 但它们不是主体。

每个给: name(文中最完整的写法)· kind · search_keys(去数据库找它的词, 最多 3 个,
给能把它和别家区分开的部分)· role_in_text(它在文中是什么角色, 一句话)·
aliases_in_text(文中用到的简称)· exchange_tag(文中若写了 (NYSE: XXX) 就原样抄)·
evidence(正文逐字片段)· iter

## 2. attributes —— 某个主体**自己是什么**

职位、总部、成立年份、行业、员工数、股票代码、网址。

判断方法: 如果这条信息只描述一个主体、不牵涉第二个主体, 它就是属性。
「Paul Hanson 是董事长」——「董事长」是他的属性;
「Paul Hanson 是 Bitdeer Industrial 的董事长」—— 属性是「董事长」, 关系是「他在那家公司」。

每条给: entity(必须是 mentions 里的 name)· key · value · evidence · iter

## 3. edges —— 两个主体**之间**的关系

每条给: subject · object · edge_class · predicate · valid_at + valid_precision ·
attrs · evidence · iter

### edge_class —— 我想按这五类来看这张图

- `affiliation`  谁属于谁。人在哪家公司、子公司属于哪个集团、公司是哪个指数的成员。
                 这是**状态**, 通常没有明确的发生时刻。
- `transaction`  所有权动了。收购、剥离、投资、合并。
- `commercial`   做成了生意。签约、供货、授权、达成协议。
- `product`      产品动了。发布、上市、停产。
- `corporate`    公司自己做的动作, 但有明确对象。派息、回购、任命、获得批准。

### 关于 subject 和 object 的方向

方向应该服务于「我想查什么」。

我想查**一个人属于哪里**、**一家子公司属于谁** —— 所以 affiliation 从**属于的一方**出发:
    Paul Hanson --officer_of--> Bitdeer Industrial
    Bitdeer Industrial --subsidiary_of--> Bitdeer Technologies Group

我想查**谁对谁做了什么** —— 所以事件类从**做这件事的一方**出发:
    Palo Alto Networks --acquires--> Portkey

★ 最容易出错的地方: 文章通常围绕一家公司写, 于是你会不自觉地把那家公司当成所有关系的主体。
  但文中出现的人和机构未必属于它 —— 可能属于它的子公司, 也可能属于完全无关的第三方。
  **先问「这句话在说谁」, 那个才是 subject。**
  具体到 affiliation: **人做 subject, 组织做 object**;子公司做 subject, 母公司做 object。
    对: Paul Hanson --officer_of--> Bitdeer Industrial
    错: Bitdeer Industrial --employs--> Paul Hanson
    原文「Paul Hanson, Chairman of Bitdeer Industrial」
      → 说的是 Paul Hanson, 他所属的是 Bitdeer Industrial(子公司), 不是集团
    原文「Taylor Adams, President and CEO of the Economic Development Authority」
      → 说的是 Taylor Adams 和 EDAWN 的关系, 跟这篇文章的主角公司没关系

### 关于 predicate

predicate 只回答**一个**问题: 这是什么关系。

其他所有信息都有它自己的位置。放错了地方就查不出来 ——
埋在关系名字符串里的金额没法按金额筛, 埋在里面的职位也没法按职位查:

    职位(CEO / Chairman / CFO)   → attributes 的 title, 不进 predicate
      写 "is_officer_of" 而不是 "is_chairman_of" / "is the president and ceo of"
    金额 / 比例 / 期间             → attrs
      写 "reports_earnings" + attrs, 而不是 "announces_loss_of_$610_million"
    时间                          → valid_at
    类别                          → edge_class, 不要在 predicate 里重复
      写 "officer_of" 而不是 "affiliation:officer_of"

同一种关系叫同一个名字, 图上才连得起来。

### 什么不该成为一条边

- 两端指的是**同一家** —— 全称、简称、缩写、带不带法人后缀, 那是一个主体的几个名字,
  不是两个主体。它们之间不该有连线, 把别名写进 aliases_in_text 就够了。
  (「Zebra」和「Zebra Technologies Corporation」是同一家)
- 两端是同一个主体 —— 那不是关系, 是这家公司自己的动作
- 另一端不是主体而是一个值(股票代码、网址、地址、年份)—— 那是属性
    错: MaxLinear --stock_symbol--> NASDAQ:MXL    对: attributes 里 MaxLinear.ticker = "MXL"
- 另一端是资产(矿山、厂房)—— 那是财产归属, 记进 attributes
- 只是在描述身份而没有发生什么(「是我们的合作伙伴」「是我们的客户」)
  —— 那是 affiliation 的状态, 不是 commercial 的事件

# iter

{level_block}

# 底线

1. evidence 必须是正文里能**原样找到**的片段。改写、翻译、概括都不接受。
2. 只写这段文字**说了**的。你知道但文里没写的, 不要补。
3. 读不出任何关系是正常的 —— 大多数事件(季报、年会、网播预告)本来就没有。
   那时 edges 给空数组并在 no_edge_reason 里说明。**不要凑数。**
4. title_body_mismatch: 标题和正文讲的不是同一件事时填 true。
   抓取时可能抓到了列表页或另一篇稿, 这个标记是唯一的线索。

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
