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
import os
import re

from openai import AsyncOpenAI

from . import config

_JSON_RE = re.compile(r"\{.*\}", re.S)                        # outermost {...} if the model wraps JSON in stray prose
_DUMP_N = itertools.count(1)                                  # request counter for debug-dump filenames

# Cap the screenshot to this many pixels before sending. Qwen-VL image tokens scale ~1 per 28×28 patch, so a tall
# full-page shot (e.g. 1280×8000 ≈ 13k image tokens) alone blows past a 16384-ctx server. 1.2M px ≈ ~1500 image
# tokens, leaving room for text + output. Downscale preserves aspect ratio (all page content stays, just lower-res).
# {POD 2026-07-23 E2E coca-cola: "Input length (18518) exceeds model's maximum context length (16384)" on a tall page}
# [CONFIDENCE: CONFIRMED 100% — the 400 came from a full_page shot; bounding pixels is the direct fix, A5000 can't fit 32k ctx]
_MAX_SHOT_PIXELS = int(os.environ.get("QWEN_MAX_SHOT_PIXELS", "1200000"))


def _bound_image_b64(b64: str) -> str:
    """Downscale a base64 JPEG so its pixel count ≤ _MAX_SHOT_PIXELS, preserving aspect ratio — keeps the whole page
    visible but caps the VL image-token cost so a tall full-page screenshot can't overflow the server context. PIL is
    lazy-imported (only the vision path needs it). Best-effort: on any decode/resize failure return the input
    unchanged (an oversized image that MIGHT overflow beats a dropped page)."""
    try:
        import base64                                          # lazy — only the vision path pays the import
        import io

        from PIL import Image
        raw = base64.b64decode(b64)
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
        px = w * h
        if px <= _MAX_SHOT_PIXELS:                            # already small enough — send untouched
            return b64
        scale = (_MAX_SHOT_PIXELS / px) ** 0.5                # area ∝ scale² → linear shrink factor is the sqrt
        im = im.convert("RGB").resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=70)               # re-encode JPEG q70 (matches watercrawl's shot format)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:                                         # noqa: BLE001 — never sink a page over an image resize
        return b64


def _dump_debug(system: str, user: str, image_b64: str | None, guided_json: dict | None,
                raw: str, parsed: dict, error: str | None) -> None:
    """When config.DEBUG_DIR is set, write ONE txt per request holding the FULL prompt (system + user + image info +
    schema) and the model's RAW output + parsed result + any error — for eyeballing exactly what went in and came
    back. Best-effort: never raises into the request path."""
    d = config.DEBUG_DIR
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        n = next(_DUMP_N)
        with open(os.path.join(d, f"req_{n:04d}.txt"), "w", encoding="utf-8") as f:
            f.write(f"=== REQUEST {n} ===\n")
            f.write(f"model={config.SERVED_NAME}  max_tokens={config.MAX_TOKENS}  temp={config.TEMPERATURE}\n")
            f.write(f"image: present={bool(image_b64)}  b64_len={len(image_b64 or '')}\n\n")
            f.write("--- SYSTEM PROMPT ---\n" + (system or "") + "\n\n")
            f.write("--- USER PROMPT (text part) ---\n" + (user or "") + "\n\n")
            if guided_json:
                f.write("--- GUIDED_JSON SCHEMA ---\n" + json.dumps(guided_json, ensure_ascii=False) + "\n\n")
            f.write("=== RAW MODEL OUTPUT ===\n" + (raw if raw else "(empty)") + "\n\n")
            f.write("=== PARSED ===\n" + json.dumps(parsed, ensure_ascii=False, indent=2) + "\n\n")
            if error:
                f.write("=== ERROR ===\n" + str(error) + "\n")
    except Exception:                                        # noqa: BLE001 — debug dump must never sink a request
        pass


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
            image_b64 = _bound_image_b64(image_b64)           # cap pixels FIRST so a tall shot can't overflow context
            # Sniff the real image mime from the base64 magic prefix so the data URI is honest regardless of the
            # producer (watercrawl render_shot emits JPEG `type="jpeg"` → b64 starts "/9j/"; a PNG would start
            # "iVBOR"). Keeps this transport layer format-agnostic — no caller has to declare the mime.
            # {POOL.PY:302 "shot = await page.screenshot(full_page=True, type=\"jpeg\", quality=70)"}
            # [CONFIDENCE: CONFIRMED 95% — JPEG/PNG b64 magic prefixes are fixed by the file-format headers]
            mime = "image/jpeg" if image_b64.startswith("/9j/") else "image/png"
            user_content: object = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
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
                    # 1st try: force the schema via the OpenAI-standard response_format=json_schema. vLLM 0.25.1's
                    # structured-output backend honours THIS; the older extra_body={"guided_json":...} is SILENTLY
                    # IGNORED on 0.25.1 → the model free-generates a markdown bare-array with its own keys
                    # (event_name/…) → _parse_json sees a non-dict → {} → 0 events. Retries drop the constraint so a
                    # grammar failure still yields a best-effort free-form reply to regex-parse.
                    # {POD 2026-07-23 A5000 vllm-0.25.1: "response_format json_schema → {events:[{title,date,type}]} EXACT; extra_body guided_json → ```json [ {event_name,date} ] ``` UNCONSTRAINED"}
                    # [CONFIDENCE: CONFIRMED 95% — live-validated on the A5000 server via test_schema.py; guided_json produced the bare array, response_format produced the exact object]
                    rf = ({"type": "json_schema",
                           "json_schema": {"name": "schema", "schema": guided_json, "strict": True}}
                          if (guided_json and attempt == 0) else None)
                    r = await self._next().chat.completions.create(
                        model=config.SERVED_NAME,
                        messages=self._messages(system, user, image_b64),
                        temperature=config.TEMPERATURE,
                        max_tokens=config.MAX_TOKENS,
                        response_format=rf,                       # None on retries → free-form fallback
                    )
                    raw = r.choices[0].message.content or ""
                    # finish_reason is the PRECISE truncation signal (OpenAI/vLLM contract): "length" ⇒ generation hit
                    # max_tokens and the JSON was cut mid-structure → _parse_json returns {}. We ESCALATE it loudly +
                    # pass it up as transport metadata so the caller SPLITS-and-retries this page — never silently
                    # salvage a partial. Works regardless of response_format vs guided_json. {USER 2026-07-23 "if it
                    # happened to be cut you need to escalate the error and report right now"} [CONFIDENCE: CONFIRMED 100%].
                    finish = getattr(r.choices[0], "finish_reason", "") or ""
                    parsed = _parse_json(raw)
                    if finish == "length":                    # truncated → make noise NOW, never hide it
                        print(f"[qwen] ⚠️ TRUNCATED finish_reason=length — {len(raw)} chars emitted, JSON incomplete "
                              f"(caller must chunk this page)", flush=True)
                    parsed["__finish__"] = finish             # metadata the caller reads to trigger chunking (_normalize strips it)
                    _dump_debug(system, user, image_b64, guided_json, raw, parsed, None)   # full I/O → tests/output
                    return parsed
                except Exception as e:                        # noqa: BLE001 — timeout/5xx/grammar → back off + retry
                    last = e
                    await asyncio.sleep(0.5 * (attempt + 1))
            print(f"[qwen] send failed after {config.MAX_RETRIES} retries: {type(last).__name__}: {last}", flush=True)
            _dump_debug(system, user, image_b64, guided_json, "", {}, str(last))   # capture WHY it failed (e.g. 400 too-long)
            # HARD failure (retries exhausted: server down / GCP→RunPod network drop / 5xx / unrecoverable 400) → return a
            # DISTINGUISHABLE error marker, NOT a bare {} that _normalize launders into {"events":[]} — indistinguishable
            # from a page that genuinely has no events. The caller (crawl) counts these and surfaces them LOUDLY at run
            # end so a network hiccup can't silently drop a page's events. Mirrors the `__finish__` metadata pattern.
            # {USER 2026-07-23 "fail loudly is the core ... we dont want quality issue"} [CONFIDENCE: CONFIRMED 100% — directive]
            return {"_error": f"{type(last).__name__}: {last}"}

    async def send_many(self, jobs: list[dict]) -> list[dict]:
        """THE FAN-OUT. jobs = list of kwargs dicts {system, user, image_b64?, guided_json?}. Submits ALL at once;
        the semaphore keeps in-flight at MAX_CONCURRENCY while vLLM continuous-batches them on the GPU. Result order
        matches job order (asyncio.gather preserves it)."""
        return await asyncio.gather(*(self.send_one(**job) for job in jobs))
