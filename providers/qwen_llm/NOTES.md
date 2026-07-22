# qwen_llm — model candidates & how to A/B them

Three model configs live in subfolders, each with its own `serve.sh`. They launch on **different GPUs + ports** so
all three can run at once on the 8×H20 box (h20-1038) and be compared on the SAME pages.

5 个候选:3 个纯文本(HTML+链接)+ 2 个 VLM(渲染截图,靠视觉布局判结构)。8×H20 一次全起,GPU 0-4 / 端口 8000-8004。

| # | Subfolder | Model | 类型 | Size | 批量 tok/s | $/1M | 质量 | GPU/port |
|---|---|---|---|---|---|---|---|---|
| 1 | `qwen3_8b/` | Qwen3-8B | 文本 | ~16GB | ~5000-7000 | ~$0.15 | 够用 | 0 / 8000 |
| 2 | `qwen3_14b/` | Qwen3-14B | 文本 | ~28GB | ~3500-5000 | ~$0.22 | 强 | 1 / 8001 |
| 3 | `qwen3_30b_a3b/` | Qwen3-30B-A3B (MoE,AWQ) | 文本 | ~18GB | **~6000-9000 ⭐** | ~$0.16 | **32B级 ⭐** | 2 / 8002 |
| 4 | `qwen_vl_7b/` | Qwen2.5-VL-7B | **视觉** | ~16GB | 低(图 token 多) | 中 | 视觉基线 | 3 / 8003 |
| 5 | `qwen_vl_32b/` | Qwen2.5-VL-32B | **视觉** | ~64GB | 更低 | 高 | **视觉天花板** | 4 / 8004 |

**⭐ 30B-A3B 是文本首选**:MoE 只激活 ~3B/token → 32B 级质量、8B 级速度,AWQ ~18GB 单卡。

## 为什么加 vision(治过分类)

文本模型分不清"这条链接是 nav 还是 event",因为 anchor 文字一样;**截图里导航条/页脚/cookie 横幅 vs 事件表格,视觉布局一眼可分**。VLM 看渲染后的整页截图 → 结构判得更干净。代价:一张截图 ~1000-1500 vision token,更慢更贵 → **按需兜底,不是默认**。

混合最优:`文本 30B-A3B 打底(90% 页够) → 难页/墙站/视觉复杂 → patchright 截图 → VL-32B 看图`。

## Vision 路径还缺的(client 侧)

现有 `client.py`/`extract.py` 是**纯文本**。VLM 需要一个 vision 变体:`extract_events_vision(screenshot_png, page_url)` —— 把 base64 截图塞进 OpenAI vision message(`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`)+ 同一套 event JSON schema。截图来源:watercrawl 的 patchright render(已能 render,加个 `page.screenshot()`)。这层建议并发 session 补,或我来。

## Qwen3 thinking mode — 抽取时必须关

Qwen3 默认开 **thinking(思维链)**。做 JSON 事件抽取要的是**直接输出**,不是 CoT —— thinking 会拖慢 + 污染 JSON。
关法(client 端,每个请求带):
```python
extra_body={"chat_template_kwargs": {"enable_thinking": False}}
```
`client.py` 的 `chat_json` 应加上这个(和现有的 `guided_json` 并存)。或用 soft switch:user 文本末尾加 `/no_think`。

## 起三个做 A/B

```bash
# 在 h20-1038 上 (装完 vLLM 后)
cd ~/WaterEvents/providers/qwen_llm
bash qwen3_8b/serve.sh          # GPU 0 → :8000
bash qwen3_14b/serve.sh         # GPU 1 → :8001
bash qwen3_30b_a3b/serve.sh     # GPU 2 → :8002
# 等三个都 "Application startup complete" (tail /mnt/data/qwen_logs/*.log)

# client 分别指到每个端口, 同一批真实 IR 页跑 extract_events_batch, 比:
#   ① 过分类率 (feed/asset/hub 混入率)  ② 抽取准确 (title/date/type)  ③ 吞吐 tok/s  ④ 单页延迟
QWEN_BASE_URLS=http://127.0.0.1:8000/v1 python3 -m ...    # 8B
QWEN_BASE_URLS=http://127.0.0.1:8001/v1 python3 -m ...    # 14B
QWEN_BASE_URLS=http://127.0.0.1:8002/v1 python3 -m ...    # 30B-A3B
```

## 最大吞吐(选定模型后)

单模型铺满 8 卡:`REPLICAS=8 GPU=0 bash qwen3_8b/serve.sh` → 8 replica ports 8000-8007,
client `QWEN_BASE_URLS` 传 8 个逗号分隔 URL,round-robin。30B-A3B(~18GB)也能一卡一 replica 铺 8 个。
