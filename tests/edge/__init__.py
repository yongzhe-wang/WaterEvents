"""tests/edge — WaterEvents 事件文本 → 图谱节点与边的抽取实验。

三步: sample.py 抽 200 条分层样本 → prompts.py 的 Step 1 让 LLM 读文字出 mention/edge →
recall.py 做 blocking 召回 → Step 3 让 LLM 带候选消歧。validate.py 是唯一不需要人工标注的自动校验。
"""
