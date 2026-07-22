# qwen_llm — VLM event extraction (vision)

Decision: **vision, not text.** A rendered IR page's LAYOUT is the strongest signal for "is this an event vs
nav/feed/asset" — a nav bar / footer / cookie banner *looks* different from an event list, which the text-only
router (BERT) and even a text LLM can't reliably see from anchors alone. So we feed the model a **full-page
screenshot** (from watercrawl's patchright render) and it returns the event list as JSON.

Two model configs, each with its own `serve.sh`. Run both at once (different GPU/port) to A/B on the same pages.

| Subfolder | Model | Size | 特点 | 默认 GPU/port |
|---|---|---|---|---|
| `qwen_vl_7b/` | Qwen2.5-VL-7B | ~16GB | 视觉基线, 快, 高吞吐 | 0 / 8000 |
| `qwen_vl_32b/` | Qwen2.5-VL-32B | ~64GB (bf16) / ~18GB (AWQ) | 视觉天花板, 布局推理最强 | 1 / 8001 |

## 起两个做 A/B

```bash
# on h20-1038 (after vLLM is installed)
cd ~/WaterEvents/providers/qwen_llm
bash qwen_vl_7b/serve.sh        # GPU 0 → :8000
bash qwen_vl_32b/serve.sh       # GPU 1 → :8001
# wait for "Application startup complete" in /mnt/data/qwen_logs/*.log

# feed both the SAME rendered-page screenshots, compare:
#   ① 过分类率 (feed/nav/hub 混入)  ② 抽取准确 (title/date/type)  ③ 单页延迟  ④ 吞吐
```

## Vision 输入

Client sends an OpenAI vision message with the screenshot as base64:
```python
messages=[{"role":"system","content":SYSTEM},
          {"role":"user","content":[
              {"type":"text","text":f"PAGE URL: {url}"},
              {"type":"image_url","image_url":{"url":"data:image/png;base64,<PNG>"}}]}]
```
Screenshot source: watercrawl patchright render + `page.screenshot(full_page=True)`. The shared `prompts.SYSTEM`
event rules apply as-is; `extract.py` needs a `extract_events_vision(screenshot_png, page_url)` variant (vision
message instead of text). `_NONEVENT_URL_RE` deterministic safety-net still applies to whatever urls the model returns.

## Cost note

A full-page screenshot is ~1000-1500 vision tokens (capped by `--mm-processor-kwargs max_pixels`), so vision is
slower/pricier per page than text — but the accuracy on structural over-classification is worth it. Cap `max_pixels`
to trade resolution for speed.

## 最大吞吐(选定模型后)

铺满 8 卡:`REPLICAS`-style multi-GPU (one replica per GPU), client `QWEN_BASE_URLS` = 8 comma-separated ports,
round-robin. VL-7B (~16GB) fits one replica per H20 easily; VL-32B AWQ (~18GB) too.
