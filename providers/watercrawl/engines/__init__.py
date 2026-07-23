"""watercrawl.engines — the pluggable FETCH ENGINES that the fallback chain escalates through.

用一句话讲完: 每个"能不能把页面抓下来"的独立手段各自一个文件 —— `impersonate`(curl_cffi TLS 指纹绕过,最低成本)、
`camoufox`(C++-级 stealth Firefox + 住宅代理,专破 Akamai/Incapsula sensor.js 的最后一招)、`pdf`(url 是 PDF 时直接
curl_cffi + pypdf 取文本,不走浏览器)。WHY 独立子目录: firecrawl 把 fetch/playwright/fire-engine/pdf 各做成一个 engine
并按 quality-score 排 fallback —— 我们照抄这个"一个引擎一个文件"的结构,orchestrator 只管按顺序试,engines 只管各自抓。
{RESEARCH firecrawl `scrapeURL/engines/` 子目录 fetch/playwright/fire-engine/pdf} [CONFIDENCE: CONFIRMED].
"""
