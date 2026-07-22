"""qwen_llm — the PARALLEL Qwen TRANSPORT (provider). Generic: send N chat jobs to a vLLM OpenAI endpoint at once,
get JSON back. Knows NOTHING about events/routes — that's event_agent's job. Reusable for any task/prompt/schema.

用一句话讲完: vLLM 起一个 OpenAI 兼容 server(serve.sh)→ QwenClient 把 N 个请求一次性并发打进去(vLLM
continuous-batching 在 GPU 上批处理)→ 每个返回解析好的 JSON。纯传输层,不含任何业务/prompt 逻辑。

Layout:
  config.py   — model / endpoint / concurrency knobs (all env-overridable)
  serve.sh    — launch vLLM: ONE high-concurrency replica, REPLICAS=N data-parallel, or TP=N for big models
  client.py   — QwenClient: schema-agnostic parallel sender (text + Qwen-VL image), semaphore-bounded, retry, JSON parse
"""
from .client import QwenClient

__all__ = ["QwenClient"]
