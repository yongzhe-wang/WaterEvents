"""ir_url_agent.discover — 单公司的 IR-URL 发现 pipeline: 渲首页收 nav 链接(+SERP 兜底)→ Qwen 分类 → event_hubs objects。

用一句话讲完: 对一家公司调 watercrawl **服务**的 `POST /v1/scrape` 渲它的 IR 首页(服务端浏览器执行 JS → 连
mega-menu 里的链接都渲得出来)→ 从返回 markdown 里收链接, 过滤出同站/已知 IR 平台的 IR-ish 候选 → 候选太少
(墙/空壳)才调 `POST /v1/search` 多-query 兜底 → 候选一次性喂 QwenClient guided_json 分类
(kind / belongs_to_company / is_seed)→ 组装成 event_hubs object 数组。
WHY nav-harvest 优先于搜索: 6-9-event 的根因就是"events 子页的链接藏在 JS mega-menu 里, 抽取器没把它当 <a href> 发出来"
—— 但那链接**确实在页面上**, 渲染后就能拿到。benchmark 反复实证 IR 首页导航直接列着 events 子页
{BENCHMARK why1vdvvk SONOCO "导航里明确列出 EVENTS & PRESENTATIONS - /EVENTS-AND-PRESENTATIONS/DEFAULT.ASPX"}。
所以 1 次 scrape 就能拿到大部分答案, 比多次搜索又快又准。
[CONFIDENCE: CONFIRMED — benchmark 100 家里 95 家的 best_events_url 都是 IR 首页同域的 nav 子页].

**硬约束: 所有网页抓取一律走 watercrawl 服务(独立 VM), 绝不用 pod 进程内的浏览器** —— pod 只负责 Qwen 推理。
本模块因此完全不 import providers.watercrawl。{USER 2026-07-26 "are you using the pod to also render? you are only
suppose to use the service endpoint not the pod"} [CONFIDENCE: CONFIRMED — 直接指令].
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx                                                # 调 watercrawl 服务的 HTTP 客户端(pod 已装 0.28.1)

from providers.qwen_llm import QwenClient                   # LLM 分类(guided_json 结构化输出)
from agent.event_agent.storage.urls import _canon                   # 跨 agent 一致的 URL 规范化/dedup key

# IR-ish 路径 —— 候选过滤器。宁松勿严: 让 Qwen 做最终判定, 这里只挡掉明显无关的(careers/legal/products)。
_IR_PATH_RE = re.compile(
    r'/(ir|investor|investors|investor-relations|events?|presentations?|calendar|webcasts?|financial|'
    r'financials|results|earnings|news-and-events|news-events|shareholders?|stock|reports?|library|'
    r'quarterly|annual|disclosure|ri|relacoes|relations)(/|\?|#|$|-|_|\.)', re.I)
# 明显不是 IR 的段 —— 直接丢, 省 Qwen 的 prompt 预算。
_NON_IR_RE = re.compile(
    r'/(careers?|jobs|legal|privacy|terms|cookie|sitemap|support|help|contact|store|shop|products?|'
    r'solutions?|blog|login|signin|account|cart|search)(/|\?|#|$|-)', re.I)
# 二进制文件 —— 是 event 的材料而非入口页, 不当 seed。
_BINARY_RE = re.compile(r'\.(pdf|pptx?|docx?|xlsx?|zip|mp[34]|mov|jpe?g|png|gif|svg|css|js)(\?|#|$)', re.I)
# 已知 off-host IR 平台 —— 这些跟公司主域不同, 但确实是该公司的 IR 站(Q4/GCS 托管), 必须放行。
_IR_PLATFORM_RE = re.compile(r'\.(gcs-web|q4web|q4inc|irwebsite|investorroom|equisolve|issuerdirect)\.com$', re.I)

# SERP 兜底用的 query 后缀(company name + 不同 IR 串)。只在 nav-harvest 拿不到料时才跑。
IR_QUERY_SUFFIXES = [
    "investor relations events and presentations",
    "investor relations financial calendar",
    "IR events webcast earnings call",
    "investor relations",
]
# ── watercrawl SERVICE(独立部署在 GCP VM 的 web-data API)—— 渲染 + 搜索都走它, 而不是 pod 进程内的浏览器 ──
# WHY 用服务而不是本地库: (1) 它跑在**另一台 8 核机器**上, 而 pod 的 cgroup 只有 7.65 核且已被渲染打满(实测 load
# 7.13/7.65, 吞吐卡在 14 家/分)—— 把渲染挪出去, pod 只留 Qwen 分类, 等于白捡一台机器的算力; (2) 它自带四级引擎
# + 住宅代理 + credits/限速, 比我在这里手搓 SERP 抓取稳; (3) `/v1/search` 是正经搜索 API, 直接返回结果 URL 列表,
# 不用刮 HTML(我之前爬 Brave SERP 属于重复造轮子)。
# {PROBE 2026-07-26 服务实测: `/v1/scrape` ir.mccormick.com → markdown 9570 chars, 解析出 109 个绝对链接, 含
#  "HTTPS://IR.MCCORMICK.COM/EVENTS" + "/EVENTS-AND-PRESENTATIONS/PRESENTATIONS", METHOD=IMPERSONATE;
#  `/v1/search` → 8 条结果, 首条即 "IR.MCCORMICK.COM/EVENTS-AND-PRESENTATIONS/PRESENTATIONS"}
# [CONFIDENCE: CONFIRMED 100% — 两个端点都对同一家公司实调过, 返回的正是目标 events 页].
_WC_BASE = os.environ.get("WATERCRAWL_API_BASE", "http://35.255.86.165:8080")
_WC_KEY = os.environ.get("WATERCRAWL_API_KEY", "")           # 必须设置 — 没有 key 就没有抓取能力(本模块无本地渲染)
_WC_TIMEOUT = float(os.environ.get("WATERCRAWL_API_TIMEOUT", "120"))
# scrape 返回的是 markdown(没有 links 字段), 链接以 markdown 语法内嵌 → 用这个正则把绝对 URL 抠出来。
_MD_LINK_RE = re.compile(r'\]\((https?://[^)\s]+)\)')
_MIN_CANDIDATES = 3                                          # nav-harvest 少于这个数 → 认为首页被墙/空壳 → 走 SERP
_MAX_CANDIDATES = 120                                        # 喂 Qwen 的上限(控 prompt 大小, 远低于 32768 ctx)

# Qwen 分类的 guided_json schema —— 每个候选判 kind + 是否属该公司 + 是否当 seed。**无 confidence**(用户砍了)。
_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "urls": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "kind": {"type": "string", "enum": ["homepage", "events", "presentations", "calendar", "news", "irrelevant"]},
                "belongs_to_company": {"type": "boolean"},   # 过滤同名干扰(novatek.ru vs novatek.com.tw)
                "is_seed": {"type": "boolean"},              # 是否值得当 crawl frontier 种子
            },
            "required": ["url", "kind", "belongs_to_company", "is_seed"],
        }},
    },
    "required": ["urls"],
}


def _domain(url: str) -> str:
    """registrable-ish host(去 www)—— 用于同站判定 + site: 检索。"""
    host = urlsplit(url if url.startswith("http") else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _same_or_platform(url: str, domain: str) -> bool:
    """URL 是否属这家公司: 同 registrable 域(含子域), 或落在已知 IR 平台 host 上(Q4/GCS 托管的独立 IR 站)。"""
    h = _domain(url)
    if not h:
        return False
    base = ".".join(domain.split(".")[-2:]) if domain.count(".") >= 1 else domain   # 粗 registrable
    return h == domain or h.endswith("." + base) or h == base or bool(_IR_PLATFORM_RE.search(h))



def _filter_candidates(urls: list[str], domain: str) -> list[str]:
    """把一页的原始链接筛成 IR-ish 候选: 属本公司(同站/IR 平台)+ 路径像 IR + 不是二进制/明显非-IR。按 _canon 去重。"""
    seen, out = set(), []
    for u in urls:
        if _BINARY_RE.search(u) or _NON_IR_RE.search(u):    # 材料文件 / careers-legal-products → 丢
            continue
        if not _same_or_platform(u, domain):                # 别家的域 → 丢(同名公司干扰在这里就挡掉大半)
            continue
        path = urlsplit(u).path or "/"
        if path not in ("/", "") and not _IR_PATH_RE.search(path):   # 路径不像 IR(且不是站根)→ 丢
            continue
        ck = _canon(u)
        if ck in seen:
            continue
        seen.add(ck)
        out.append(u)
    return out[:_MAX_CANDIDATES]


async def _svc_post(path: str, payload: dict) -> dict:
    """POST 一次 watercrawl 服务。失败(超时/非 200/无 key)一律返回 {} —— 这家公司这轮空手, 下次重跑再补,
    服务抖动不能让整个全量 run 崩掉。"""
    if not _WC_KEY:                                          # 没配 key → 视作服务不可用, 走本地
        return {}
    try:
        async with httpx.AsyncClient(timeout=_WC_TIMEOUT) as cli:
            r = await cli.post(_WC_BASE + path, json=payload,
                               headers={"api-key": _WC_KEY, "content-type": "application/json"})
            return r.json() if r.status_code == 200 else {}
    except Exception:                                        # noqa: BLE001 — 服务不可达 → 空手返回, 不重试
        return {}


async def _svc_scrape_links(url: str) -> list[str]:
    """服务 `/v1/scrape` 渲一页 → 从返回的 markdown 里抠出所有绝对链接。服务不可用/无料时返回 []。"""
    d = await _svc_post("/v1/scrape", {"url": url, "formats": ["markdown"]})
    md = (d.get("markdown") or (d.get("data") or {}).get("markdown") or "") if isinstance(d, dict) else ""
    return _MD_LINK_RE.findall(md) if md else []


async def _svc_search(query: str) -> list[str]:
    """服务 `/v1/search` → 结果 URL 列表。返回结构 {success, query, engine, web:[{url,...}], creditsRemaining}。"""
    d = await _svc_post("/v1/search", {"query": query, "limit": 10})
    web = d.get("web") or d.get("results") or [] if isinstance(d, dict) else []
    if isinstance(web, dict):                                # 有的引擎把结果嵌一层 {results:[...]}
        web = web.get("results") or []
    out = []
    for r in web if isinstance(web, list) else []:
        u = r.get("url") or r.get("link") if isinstance(r, dict) else (r if isinstance(r, str) else "")
        if u and str(u).startswith("http"):
            out.append(str(u))
    return out


async def _harvest_nav(ir_url: str, domain: str) -> tuple[list[str], bool]:
    """PRIMARY: 让 watercrawl SERVICE 渲 IR 首页 → 从返回的 markdown 里收链接 → 筛出 IR-ish 候选。
    返回 (候选, 是否拿到过内容)。

    WHY nav-harvest 是主路径: events 子页的链接本来就挂在 IR 首页导航上, 渲染后直接可得 —— 不依赖搜索引擎。
    {BENCHMARK why1vdvvk: 100 家里 95 家的 best_events_url 都是 IR 首页同域的 nav 子页}

    WHY 只用服务、绝不用 pod 本地渲染: 渲染必须跑在独立的 watercrawl 服务上, pod 只负责 Qwen 推理 —— 这是硬约束。
    服务自己就有四级引擎(headless → curl_cffi impersonate → 住宅代理 → camoufox), 实测 `/v1/scrape` 对
    ir.mccormick.com 用 METHOD=IMPERSONATE 成功返回 9570 chars / 109 个链接, 能力不比本地库差。
    {USER 2026-07-26 "are you using the pod to also render? you are only suppose to use the service endpoint not
    the pod"} [CONFIDENCE: CONFIRMED — 直接指令, 覆盖我此前为了吞吐做的 pod/service 分流]."""
    cands = _filter_candidates(await _svc_scrape_links(ir_url), domain)
    return cands, bool(cands)


async def _serp_fallback(name: str, domain: str) -> list[str]:
    """FALLBACK: 首页被墙/空壳时才跑。多-query fan-out(open + site:), 汇总去重 → 候选。
    WHY 兜底而非主路径: SERP 抓取脆(引擎改版/反爬即失效)且一家要 8 次 render; 只有首页拿不到料时才值这个成本。"""
    queries = [f"{name} {sfx}" for sfx in IR_QUERY_SUFFIXES]
    queries += [f"site:{domain} {sfx}" for sfx in IR_QUERY_SUFFIXES[:2]]
    results = await asyncio.gather(*(_svc_search(q) for q in queries), return_exceptions=True)
    urls = [u for r in results if not isinstance(r, Exception) for u in r]
    return _filter_candidates(urls, domain)


async def discover_company(company: dict, client: QwenClient) -> list[dict]:
    """一家公司 → event_hubs object 数组。company={id,ticker,ir_url}。流程见模块 docstring。返回 [] = 没发现可用 seed。
    每个 object: {url, kind, source:'ir_url_agent', is_seed, alive, checked_at}。原 ir_url 始终保留为 homepage seed。"""
    ir_url = (company.get("ir_url") or "").strip()
    if not ir_url:
        return []
    ticker = company.get("ticker") or ""
    domain = _domain(ir_url)

    cands, rendered = await _harvest_nav(ir_url, domain)     # 主路径: 渲首页收 nav
    if len(cands) < _MIN_CANDIDATES:                         # 料太少(墙/JS 空壳/极简站)→ 搜索兜底
        cands = list({_canon(u): u for u in (cands + await _serp_fallback(ticker or domain, domain))}.values())
    if _canon(ir_url) not in {_canon(c) for c in cands}:     # 公司自己的 IR 首页永远在候选里(至少它能当 seed)
        cands.insert(0, ir_url)
    cands = cands[:_MAX_CANDIDATES]

    # Qwen 分类 —— 一次把所有候选喂进去, guided_json 强制结构化输出。
    user = (f"公司 ticker={ticker}, 主域={domain}, 已知 IR 首页={ir_url}\n"
            f"候选 URL(从该公司 IR 页导航渲染 + 搜索得到):\n" + "\n".join(f"- {u}" for u in cands) + "\n\n"
            "对每个 URL 判三件事: (1) kind = homepage/events/presentations/calendar/news/irrelevant; "
            "(2) belongs_to_company = 是否确属这家公司(排除同名不同公司); "
            "(3) is_seed = 是否值得当爬虫起爬种子 —— events/presentations/calendar 列表页和 IR homepage 为 true, "
            "单条新闻/单个 event 详情页/irrelevant 为 false。只回 JSON, 绝不编造不在列表里的 URL。")
    res = await client.send_one(
        system="你是 IR 网站结构分类器。只输出 schema 规定的 JSON。绝不编造不在候选列表里的 URL。",
        user=user, guided_json=_CLASSIFY_SCHEMA)
    if not isinstance(res, dict) or res.get("_error"):       # send_one 硬失败返 {_error} 而非 raise → 防御
        print(f"[ir_url_agent] classify failed {ticker}: {res.get('_error') if isinstance(res, dict) else res}", flush=True)
        return []
    classified = res.get("urls") or []                       # guided_json 在 retry 时会掉约束 → .get 防御

    # 组装 hub object。alive 语义: 候选来自"我们刚渲染成功的那一页的导航"→ 它是真链接, 记 True; 走搜索兜底的记 None(未知)。
    # WHY 不再逐个 render 校验: benchmark 显示 IR events 页**大量返 403**(Q4/Cloudflare 反爬)但 URL 是真的
    # {BENCHMARK why1vdvvk "WEBFETCH 全 403 是 Q4 平台 ANTI-BOT, 非 URL 不存在"} —— 按"渲不出正文=dead"会把真 seed 全丢掉,
    # 而 crawl 端本来就有 HTTP_FIRST=0 + WEBSHARE 住宅代理破墙。所以这里只记来源可信度, 不做会误杀的存活判定。
    # [CONFIDENCE: CONFIRMED — benchmark 多家(EA/ADT/CIGNA/TIMKEN)events 页 403 但经 search 二次证实真实存在].
    cand_set = {_canon(c) for c in cands}
    hubs, seen = [], set()
    for c in classified:
        u = (c.get("url") or "").strip()
        if not u.startswith("http") or _canon(u) not in cand_set:     # 不在候选里 = 模型编的 → 丢
            continue
        if not c.get("belongs_to_company") or c.get("kind") == "irrelevant":
            continue
        ck = _canon(u)
        if ck in seen:
            continue
        seen.add(ck)
        hubs.append({"url": ck, "kind": c.get("kind"), "source": "ir_url_agent",
                     "is_seed": bool(c.get("is_seed")),
                     "alive": True if rendered else None,   # nav-harvest 来源 = 页面刚渲染成功 → 链接可信
                     "checked_at": datetime.now(timezone.utc).isoformat()})
    return hubs
