"""watercrawl.orchestrator — the multi-engine WATERFALL fallback chains for discovery + read renders.

用一句话讲完: 两个同步入口把"抓一页"升级成一条链 —— `render_full`(发现型,fallback 成功判据 = 链接更多)和
`render_detail`(读取型,成功判据 = 出现真正正文而非导航壳),链路都是 PDF→headless render(wait-retry 阶梯 + HTTP2→
HTTP1 重试)→ curl_cffi impersonate → webshare 住宅渲染 → camoufox FB4。WHY 两个独立函数而非一个 flag: firecrawl 的
"升级决策"是显式判定函数;这里 render_full 的每级用"链接数变多"判成功、render_detail 用"not render_thin(真正 prose)"判
成功 —— 判据不同就是两个函数、code-level 隔离,不是一个参数化入口。{USER 2026-07-22 "i want code level isolation not
just a trigger"; RESEARCH firecrawl scrapeURLLoop + engineOptions score} [CONFIDENCE: CONFIRMED — verbatim 迁移自
pool.py:855-956,唯一改动 = PDF 从 src.agents 硬编码换成自包含 engines.pdf].
"""
from __future__ import annotations

from . import config, detection, render, runtime
from .engines import camoufox, impersonate, pdf


def _render_with_wait_retries(url: str, base_wait: int) -> tuple[str, list, str, bool]:
    """Render url with a GROWING wait (up to config.WAIT_RETRIES tries) so a JS/AJAX-rendered page's real content has
    time to appear — STOP as soon as the page is no longer a thin nav shell. Also runs the ERR_HTTP2 → HTTP/1.1 retry
    and marks a DEAD host. Returns (text, links, html, dead). WHY the ladder: a Q4/Sitecore .aspx DETAIL page loads its
    filing/release content AFTER the load event, so a single wait=0 capture returns only the nav menu. An SSR page (content
    on first paint) passes attempt 0 and never pays the extra waits. {POOL.PY:774-805; USER 2026-07-22 "retry ... each
    time the wait ms is longer"} [CONFIDENCE: CONFIRMED — the content is JS-loaded]."""
    text, links, html, dead = "", [], "", False
    for attempt in range(max(config.WAIT_RETRIES, 1)):
        w = base_wait + attempt * config.WAIT_STEP_MS
        try:
            text, links, html = runtime.run_on_loop(render._render_full_one(url, w),
                                                     (config.NAV_TIMEOUT_MS / 1000) + max(w, 0) / 1000 + 30)
        except Exception as error:                       # noqa: BLE001 — classify the failure (HTTP2 retry vs dead host)
            if "ERR_HTTP2" in str(error) and runtime._browser_h1 is not None:
                try:                                     # Akamai breaks headless HTTP/2 → retry on the HTTP/1.1 lane
                    text, links, html = runtime.run_on_loop(
                        render._render_full_one(url, w, browser=runtime._browser_h1),
                        (config.NAV_TIMEOUT_MS / 1000) + max(w, 0) / 1000 + 30)
                    print(f"[watercrawl] {url[:70]} HTTP2 fail → HTTP1 retry got {len(links)} links", flush=True)
                except Exception as e2:                  # noqa: BLE001
                    print(f"[watercrawl] render_full HTTP1 retry failed for {url[:70]}: {e2}", flush=True)
            else:
                print(f"[watercrawl] render_full failed for {url[:80]}: {error}", flush=True)
                if detection.is_dead_error(str(error)):  # DNS/cert/SSL/aborted → record dead so caller doesn't count it
                    detection.mark_dead(url)
                    dead = True
            break
        if not detection.render_thin(text):              # real content arrived → stop climbing the wait ladder
            if attempt:
                print(f"[watercrawl] {url[:70]} content arrived at wait={w}ms (attempt {attempt + 1}/{config.WAIT_RETRIES})", flush=True)
            break
    return text, links, html, dead


def render_full(url: str, wait_ms: int = 0) -> tuple[str, list, str]:
    """SYNC entry: render url → (text, links, html) for the DISCOVERY fetch (a caller renders a LISTING page and wants
    its LINKS). Fallback chain: PDF → headless render (wait-retries) → curl_cffi impersonate (FALLBACK 1) → webshare
    residential render (FALLBACK 3) → camoufox (FALLBACK 4). Each fallback's success test is LINK COUNT (more links =
    a better discovery result) — that is what distinguishes render_full from render_detail (whose fallbacks test
    CONTENT PRESENCE). ("", [], "") only when the whole chain yields nothing. {POOL.PY:855-909}

    SITEMAP REMOVED (2026-07-22): the old FALLBACK 2 harvested sitemap.xml — deleted as context-less junk (65% of
    events landed empty-anchor). A walled LISTING now simply yields fewer links. {USER 2026-07-22 "remove the sitemap
    one that is very unreliable and messy"}."""
    if pdf.is_pdf_url(url):                               # a PDF url → extract text directly (no browser), skip the render
        pdf_text = pdf.fetch(url)
        if pdf_text.strip():
            return pdf_text, [], ""
    text, links, html = "", [], ""
    dead = False
    if runtime.ensure_browser():
        text, links, html, dead = _render_with_wait_retries(url, wait_ms)
    if detection.looks_walled(text, links) and not dead:      # FALLBACK 1 — curl_cffi impersonate (fingerprint bypass)
        i_text, i_links, i_html = impersonate.fetch(url)
        if len(i_links) > len(links):
            print(f"[watercrawl] render blocked on {url[:70]} → curl_cffi impersonate got {len(i_links)} links (no proxy)", flush=True)
            text, links, html = (i_text or text), i_links, (i_html or html)
        else:
            print(f"[watercrawl] impersonate ALSO blocked/empty on {url[:70]} (got {len(i_links)} links)", flush=True)
    if detection.looks_walled(text, links) and not dead:      # FALLBACK 3 — webshare residential render (real IP)
        if runtime._browser_proxy is not None:
            try:
                r_text, r_links, r_html = runtime.run_on_loop(
                    render._render_full_one(url, wait_ms, browser=runtime._browser_proxy),
                    (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40)
                if len(r_links) > len(links):
                    print(f"[watercrawl] WALLED → webshare residential render got {len(r_links)} links for {url[:70]}", flush=True)
                    text, links, html = (r_text or text or "watercrawl-webshare"), r_links, (r_html or html)
                else:
                    print(f"[watercrawl] WALLED and webshare residential render got {len(r_links)} links for {url[:70]} too", flush=True)
            except Exception as _wserr:                  # noqa: BLE001
                print(f"[watercrawl] webshare residential render failed for {url[:70]}: {_wserr}", flush=True)
        else:
            print(f"[watercrawl] WALLED and NO webshare proxy for {url[:70]} — set WEBSHARE_USERNAME/PASSWORD to recover", flush=True)
    if detection.looks_walled(text, links) and not dead:      # FALLBACK 4 — CAMOUFOX (beats Akamai/Incapsula sensor.js)
        c_text, c_links, c_html = camoufox.render(url, wait_ms)
        if len(c_links) > len(links):                    # discovery success test = MORE links than the walled result
            print(f"[watercrawl] WALLED → camoufox FB4 got {len(c_links)} links for {url[:70]}", flush=True)
            text, links, html = (c_text or text or "watercrawl-camoufox"), c_links, (c_html or html)
    return text, links, html


def render_detail(url: str, wait_ms: int = 0) -> tuple[str, list, str]:
    """SYNC entry: READ-type render for a DETAIL fetch — render ONE event page's CONTENT (JS) → (text, links, html).
    CODE-LEVEL ISOLATION from render_full (a separate function, not a bool flag): render_full succeeds on LINK COUNT
    (discovery); render_detail succeeds on CONTENT PRESENCE (`not detection.render_thin(...)` — real prose = better
    page). Fallback chain: PDF → render (JS wait-retries) → impersonate → webshare residential → camoufox. Neither
    function does sitemap-harvest (removed 2026-07-22). {POOL.PY:912-956; USER 2026-07-22 "i want code level isolation
    not just a trigger"} [CONFIDENCE: CONFIRMED]."""
    if pdf.is_pdf_url(url):                               # PDF url → pypdf text directly
        pdf_text = pdf.fetch(url)
        if pdf_text.strip():
            return pdf_text, [], ""
    text, links, html, dead = "", [], "", False
    _imp = None                                          # remember the impersonate result to fall back to if all else thin
    if runtime.ensure_browser():
        text, links, html, dead = _render_with_wait_retries(url, wait_ms)
    if detection.looks_walled(text, links) and not dead:      # FALLBACK 1 — impersonate; keep it only if it's real prose
        i_text, i_links, i_html = impersonate.fetch(url)
        if i_html or i_text:
            _imp = (i_text, i_links, i_html)
            if not detection.render_thin(i_text or ""):
                text, links, html = (i_text or text), (i_links or links), (i_html or html)
    if detection.looks_walled(text, links) and not dead and runtime._browser_proxy is not None:   # FB3 residential
        try:
            r_text, r_links, r_html = runtime.run_on_loop(
                render._render_full_one(url, wait_ms, browser=runtime._browser_proxy),
                (config.NAV_TIMEOUT_MS / 1000) + max(wait_ms, 0) / 1000 + 40)
            if (r_html or r_text) and not detection.render_thin(r_text or ""):
                text, links, html = (r_text or text or "watercrawl-webshare"), (r_links or links), (r_html or html)
        except Exception as _we:                         # noqa: BLE001
            print(f"[watercrawl] detail residential render failed for {url[:70]}: {_we}", flush=True)
    if detection.looks_walled(text, links) and not dead:      # FALLBACK 4 — CAMOUFOX (content-presence success test)
        c_text, c_links, c_html = camoufox.render(url, wait_ms)
        if (c_html or c_text) and not detection.render_thin(c_text or ""):   # read success test = real prose, not a shell
            print(f"[watercrawl] WALLED → camoufox FB4 got detail content for {url[:70]}", flush=True)
            text, links, html = (c_text or text), (c_links or links), (c_html or html)
    if detection.looks_walled(text, links) and _imp is not None and (_imp[2] or _imp[0]):   # last resort: the impersonate SSR
        text, links, html = (_imp[0] or text), (_imp[1] or links), (_imp[2] or html)
    return text, links, html
