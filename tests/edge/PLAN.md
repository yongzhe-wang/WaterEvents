# WaterEvents → 图谱:修改计划

用一句话讲完:把 Step 1 的输出从「实体 + 边」两个列表改成 **「实体 + 属性 + 边(带分类)」三个列表** ——
属性落 `node_*_profile`,边落 `edge_claim` 但用 `edge_class` 区分「谁跟谁有关系」(慢变的连接)和
「发生了什么事」(有时点的事件);同时把之前写死的语义规则全部交回给模型判断,程序只保留纯机械的事实校验。

---

## 一、为什么要改(200 条实测的证据)

```
223 条边中
  状态类  62 条 (27%)   employs×41 · owns×7 · manages×4 · operates×3 · is_member_of
  事件类 161 条 (72%)   partners_with×21 · appoints×13 · acquires×6 · merges_with×2

LLM 复核 223 条的结果
  成立         75 (33%)      主体/客体错  19 (8%)
  证据不支持     5 ( 2%)      没给判决    124 (55%)   ← 一次判太多条,顾不过来
```

真错几乎集中在同一个偏差上——**把文章的主角公司当成所有关系的默认主体**:

```
Bitdeer Technologies Group --employs--> Paul Hanson
  「Paul Hanson, Chairman of Bitdeer Industrial」        他是子公司的董事长
Bitdeer Technologies Group --communicates_with--> Taylor Adams
  「Taylor Adams, President and CEO of EDAWN」            他根本不是 Bitdeer 的人
Otter Tail Corporation --receives_approval_for--> Otter Tail Power
  「In May, Otter Tail Power received approval from the Minnesota PUC」   主体是子公司
```

`employs` 恰好是 keep 率最低(39%)、fix 最多、偏差最集中的一类。

---

## 二、核心设计:边要分类,不是砍掉

**org 和 person 必须链接** —— 否则图里查不到「这家公司有哪些人」。但「谁是 CEO」和
「A 收购 B」的用途完全不同,混在一起会让真正重要的信号被淹没。

### 2.1 `edge_class` —— 与 water-graph 的 SEC 线对齐,不另造一套

| edge_class | 是什么 | 例子 | 时间性 |
|---|---|---|---|
| `affiliation` | 谁跟谁有关系(慢变的连接) | 任职、董事会成员、子公司隶属、指数成员 | 状态,有起止 |
| `transaction` | 所有权变动 | 收购、剥离、投资、合并 | 事件,有时点 |
| `commercial` | 商业往来 | 合作协议、供货、客户、授权 | 事件,有时点 |
| `product` | 产品/服务动作 | 发布、上市、停产 | 事件,有时点 |
| `corporate` | 公司自身动作 | 分红、回购、指引、任命 | 事件,有时点 |

SEC 那条线现有的 `holds` / `transacts` / `affiliated` / `terminated` 分别归入
`affiliation`(affiliated)与 `transaction`(holds/transacts/terminated),**两条线因此可比**。

### 2.2 什么进 `attributes` 而不是边

判据:**这句话描述的是一个实体的内在属性,还是两个实体之间的关系。**

```
→ attributes(落 node_*_profile)
   职位名称、总部地址、成立年份、员工数、所属行业、股票代码

→ affiliation 边(落 edge_claim)
   某人任职于某组织、某公司是某公司的子公司、某公司是某指数成员
```

`Paul Hanson, Chairman of Bitdeer Industrial` 拆成两部分:

```
edge  Paul Hanson --affiliation:officer--> Bitdeer Industrial     ← 链接保住了
attr  Paul Hanson.title = "Chairman"                              ← 职位名是他的属性
```

**主体偏差因此消失**:边的主体是 Paul Hanson 本人,客体是他实际任职的 Bitdeer Industrial,
没有「挂到母公司还是子公司」的余地。

### 2.3 `appoints` 这类双重性质的:两者都产

「X 于 8/5 任命 Y 为 CEO」既是有时点的事件,又产生了持续状态:

```
edge  X --corporate:appoints--> Y     valid_at=2026-08-05    ← 动作,可追溯
edge  Y --affiliation:officer--> X    valid_at=2026-08-05    ← 关系,可查询现任
attr  Y.title = "CEO"
```

冗余是**有意的** —— 事件流和当前状态是两种查询,各取所需。

---

## 三、Step 1 输出格式(三列表)

```jsonc
{
  "mentions": [
    { "name": "Paul Hanson", "kind": "person",
      "search_keys": ["Paul Hanson"], "role_in_text": "Chairman of Bitdeer Industrial",
      "aliases_in_text": [], "exchange_tag": null,
      "evidence": "Paul Hanson, Chairman of Bitdeer Industrial", "iter": 1 }
  ],
  "attributes": [                                  // ← 新增
    { "entity": "Paul Hanson", "key": "title", "value": "Chairman",
      "evidence": "Paul Hanson, Chairman of Bitdeer Industrial", "iter": 1 }
  ],
  "edges": [
    { "subject": "Paul Hanson", "object": "Bitdeer Industrial",
      "edge_class": "affiliation", "predicate": "officer_of",
      "valid_at": null, "valid_precision": "unknown",
      "attrs": {}, "evidence": "Paul Hanson, Chairman of Bitdeer Industrial", "iter": 1 }
  ],
  "title_body_mismatch": false,
  "no_edge_reason": null
}
```

`iter` 三档不变:`0` 结构化零推断(SEC 专属)/ `1` 文本明说 / `2` 从上下文推断。

---

## 四、把语义规则交回给模型

现有代码里以下都是**语义判断被写成了正则**,按 {USER "you shouldnt check right, use llm to determien"} 全部移除:

| 现有规则 | 处置 |
|---|---|
| `_TOO_GENERIC` 通用词黑名单 | 删。改为 prompt 要求给有区分度的检索词;召回量爆炸时由 `AMBIGUOUS` 兜底 |
| `object` 为空的兜底拦截 | 删。schema 要求 object 非空即可 |
| 端点必须在 mentions 里 | 删。改由验证步骤判断(名字变体是语义问题) |
| 「evidence 里出现非本边端点」检查 | 已删(它误报了 Zebra 的简称) |

**保留的程序校验只有纯机械的事实检查:**

```
evidence 必须是正文的逐字子串     ← 字符串包含,不需要理解任何东西
JSON schema 合法性               ← 结构约束
分块与块数上限                    ← 由长度分布推出,不是判断
not_text(RTF 控制码)检测        ← 字节特征,不是语义
```

---

## 五、验证步骤的修复

`no_verdict` 占 55%,而且出现「判决对了但理由说的是另一条边」——
根因是**一次给模型十几条边**。改法:

- 按 8 条一批分批复核(与召回的 blocking 同一个思路:让每次判断的输入量可控)
- 每批只带该批边涉及的实体上下文
- 仍然不给判决的计入 `no_verdict`,**不默认放行**

---

## 六、落库映射

```
attributes  →  node_person_profile / node_company_profile / node_institution_profile
edges       →  edge_claim(source_type='waterevents', tier=1)
                 content_structured = { edge_class, predicate, attrs, evidence, iter, event_id }
               + 对应的 edge_detail__* 详情表(经现有触发器自动写)
原文        →  entity_note;引用关系  →  edge_note
```

**前置迁移(阻塞项)**:样本里大量实体没有 CIK(`Luminor Holding AS` / `Bitdeer Industrial` /
`Spatialsolutions.ai`)。按 {USER "unified id ... cik and cusip is only for merging and linking,
not the primary key"}:

```sql
alter table node_entity add column entity_id bigserial;
alter table node_entity alter column cik drop not null;
create unique index on node_entity(cik) where cik is not null;
create table node_alias (entity_id, alias, source, evidence);   -- 消歧的记忆,随时间自我增强
```

这会影响 water-graph 那边 session A 的代码,需要协调。**但跑实验不需要它** ——
Step 1/2/3 可以先只读不写。

---

## 七、执行顺序

| # | 做什么 | 需要 LLM | 阻塞项 |
|---|---|---|---|
| 1 | Step 1 改三列表 + `edge_class`,移除四条语义规则 | — | |
| 2 | 重跑 200 条 | 是 | |
| 3 | 验证改分批,重跑 | 是 | 依赖 2 |
| 4 | 读结果:按 `edge_class` 分别看 keep 率 | — | 依赖 3 |
| 5 | `node_entity` 主键迁移 + `node_alias` | — | 影响 session A |
| 6 | Step 2/3 消歧接通,落库 | 是 | 依赖 5 |

---

## 八、验收标准(不需要人工标注)

```
① 交易所标记捕获率                  当前 84%
② iter 分布                        应集中在 1;iter2 占比反映文本本身的明示程度
③ predicate 复用度                 当前只出现一次占 76%,分类后应下降
④ expect_no_edge 层产边率           当前 0.40 vs 正样本 1.52(3.8 倍差) —— 已通过,须保持
⑤ 丢弃原因分布                      哪道闸拦了什么
⑥ 验证 no_verdict 率               当前 55%,分批后应显著下降
⑦ 按 edge_class 的 keep 率          新增:affiliation 与 transaction 应分别评估
```

**仍然只能人读的**:关系本身对不对、消歧对不对、私营机构有没有被错误合并 ——
按 {USER 2026-07-23 "dont rely on ground truth read the output yourself"},靠读 trace 判断。

---

## 九、这份计划纳入的历次要求

| 你提的 | 落在哪 |
|---|---|
| node attribute 进 node 表,一切客观 | §2.2 attributes 列表 + §6 落库映射 |
| 统一 ID,CIK 只作链接键 | §6 主键迁移 |
| 不伪造、保留原始 | evidence 逐字校验;attrs 原样记录不换算 |
| 边层镜像节点层 | §6 edge_claim + detail 表 + note 表 |
| 减少超参数,尽量用 LLM | §4 移除四条语义规则 |
| 不要硬编码,用 LLM 判断 | §4 + §5 验证步骤 |
| iter 三档 | §3 输出格式 |
| 分块不截断,上限由分布定 | 已实现(18k/块,上限 12 块 = p99 之外) |
| evidence 失败要重试 | 已实现(定向重试,救回 11 条) |
| media 类型都要能处理 | 已实现(`doc_kind` 分层);pdf lane 待 `MEDIA_KINDS` 打开 |
| 200 样本测试、实际读结果 | 已完成一轮,§7 步骤 2-4 重跑 |
| 区分 edge vs node attribute | §2 全节 |
| 仍要链接 org-person,但区分重要性 | §2.1 `edge_class` |
