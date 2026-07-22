"""qwen_llm.client — the SMALLEST component: a PARALLEL request sender to the Qwen vLLM endpoint.

用一句话讲完: 给一批 job(每个 = system + user prompt, 可选一张截图, 可选 guided_json schema)→ 用 asyncio 一次性
全发出去、semaphore 控住在飞数量、round-robin 摊到多个 replica → 返回一批解析好的 JSON。**它只管"并行发+收+解析
JSON",完全不知道 events/routes/prompt 长什么样** —— 那是上层(prompts/extract)的事。这样换 schema、换 prompt、
换任务都不用动这个文件。

WHY this is the smallest component: everything above (prompt building, event parsing, BFS routing) depends on ONE
thing — reliably firing many LLM requests in parallel and getting JSON back. Build this rock-solid first, then layer
on top. Supports BOTH text-only (Qwen3) AND vision (Qwen-VL: pass image_b64) with the same sender, so the model
choice never leaks into the caller.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re

from openai import AsyncOpenAI

from . import config

_JSON_RE = re.compile(r"\{.*\}", re.S)                        # outermost {...} if the model wraps JSON in stray prose


def _parse_json(text: str) -> dict:
    """Best-effort parse a model reply into a dict. Strict parse first; else grab the outermost {...}; else {}.
    Never raises — a malformed reply becomes {} so one bad page can't sink the batch."""
    if not text:
        return {}
    obj = None
    try:
        obj = json.loads(text)
    except Exception:                                        # noqa: BLE001 — model added prose around the JSON
        m = _JSON_RE.search(text)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:                               # noqa: BLE001 — truncated / still malformed
                obj = None
    # MUST be a dict — a model that replies with a bare array [...] (or a scalar) would otherwise reach the caller's
    # result.get("events") and raise AttributeError, sinking the whole batch. Coerce non-dict → {}.
    return obj if isinstance(obj, dict) else {}


class QwenClient:
    """Async parallel sender over one or more vLLM OpenAI-compatible endpoints. Concurrency is capped GLOBALLY by a
    semaphore, so a 10k-page batch keeps exactly MAX_CONCURRENCY requests in flight (a full-but-bounded vLLM queue =
    peak GPU utilisation, no socket/OOM blowup). Round-robins across replicas. One client is reused for a whole run."""

    def __init__(self, base_urls: list[str] | None = None, concurrency: int | None = None):
        urls = base_urls or config.BASE_URLS
        self._clients = [AsyncOpenAI(base_url=u, api_key=config.API_KEY, timeout=config.REQUEST_TIMEOUT_S) for u in urls]
        self._rr = itertools.cycle(range(len(self._clients)))   # round-robin index across replicas
        self._sem = asyncio.Semaphore(concurrency or config.MAX_CONCURRENCY)   # the real throughput dial

    def _next(self) -> AsyncOpenAI:
        return self._clients[next(self._rr)]                  # spread load evenly across replicas

    @staticmethod
    def _messages(system: str, user: str, image_b64: str | None) -> list[dict]:
        """Build the OpenAI messages array. With image_b64 → a MULTIMODAL user turn (text + image) for Qwen-VL; the
        exact same call shape a text model just gets text, so the sender stays model-agnostic."""
        if image_b64:
            user_content: object = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ]
        else:
            user_content = user
        return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]

    async def send_one(self, system: str, user: str, image_b64: str | None = None,
                       guided_json: dict | None = None) -> dict:
        """One chat completion → parsed JSON dict. Held under the global semaphore for the whole in-flight duration.
        Retries transient errors; returns {} on hard failure so one bad page never sinks the batch. guided_json (if
        given) constrains vLLM's sampler to that schema on the first try → output is always valid JSON."""
        async with self._sem:
            last = None
            for attempt in range(config.MAX_RETRIES + 1):
                try:
                    r = await self._next().chat.completions.create(
                        model=config.SERVED_NAME,
                        messages=self._messages(system, user, image_b64),
                        temperature=config.TEMPERATURE,
                        max_tokens=config.MAX_TOKENS,
                        # 1st try: force the schema (vLLM guided decoding). Retries drop it in case the grammar is
                        # what failed, so we still get a best-effort free-form reply to regex-parse.
                        extra_body={"guided_json": guided_json} if (guided_json and attempt == 0) else None,
                    )
                    return _parse_json(r.choices[0].message.content or "")
                except Exception as e:                        # noqa: BLE001 — timeout/5xx/grammar → back off + retry
                    last = e
                    await asyncio.sleep(0.5 * (attempt + 1))
            print(f"[qwen] send failed after {config.MAX_RETRIES} retries: {type(last).__name__}: {last}", flush=True)
            return {}

    async def send_many(self, jobs: list[dict]) -> list[dict]:
        """THE FAN-OUT. jobs = list of kwargs dicts {system, user, image_b64?, guided_json?}. Submits ALL at once;
        the semaphore keeps in-flight at MAX_CONCURRENCY while vLLM continuous-batches them on the GPU. Result order
        matches job order (asyncio.gather preserves it)."""
        return await asyncio.gather(*(self.send_one(**job) for job in jobs))
