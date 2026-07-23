r"""watercrawl.extract_js — the browser-side DOM → (text, links, inline-markdown) serializer, as ONE shared constant.

用一句话讲完: 这段在页面里 `page.evaluate()` 跑的 JS 把渲染后的 DOM 走一遍,吐出三样东西 —— ①`text`(body innerText
+ 每条链接的上下文簇,喂 wall 检测)②`links`(去重的绝对 http(s) url)③`inline`(保留 [anchor](url)/#heading/表格
row|cell/<time datetime> 这些高信号结构的 markdown-ish 视图,喂 LLM)。WHY 独立成常量文件: render.py 的每条渲染路径
AND drivers/ 的每个翻页驱动都 evaluate 同一段抽取 JS —— 抽取逻辑只此一份,换 render 路径不用碰它、改它不用碰 render。
{RESEARCH crawl4ai `content_scraping_strategy.py` 把内容抽取独立成 strategy} [CONFIDENCE: CONFIRMED].

这段 JS 的边界处理(全部保留自原 pool.py，逐条有据）：
  - walk-up 到最近的 row/card 祖先找 date+title 簇（icon/"Read more" 链接的 title 在兄弟块里）{USER 2026-07-05
    "there might be cases where the title is far away"}。
  - CJK-aware 信号阈值（12 CJK 字 ≈ 40 latin，CJK 日期 年月日/년월일）{AUDIT wf_7e9d7ce1 M11 "CJK render 信号漏判"}。
  - 保留 <a>/<time datetime>/<h*>/<tr><td>/<li> 结构，丢 script/style/svg 噪音 {USER "keep more info and tags from
    the raw content — you determine which is useful for the llm"}。
[CONFIDENCE: CONFIRMED — 全部 verbatim 迁移自 pool.py:34-134，行为零改动].
"""
from __future__ import annotations

# NOTE: kept BYTE-FOR-BYTE identical to the original pool.py `_EXTRACT_JS` so render/driver behavior is unchanged.
EXTRACT_JS = """() => {
  const body = document.body ? document.body.innerText : '';
  const seen = new Set();
  const links = [];
  const annotated = [];
  // ── items 3+5 (2026-07-23): harvest STRUCTURED signals from JSON-LD + <meta> BEFORE the DOM walk. When present,
  // schema.org Event/NewsArticle is the MOST reliable event source (explicit name/startDate/url), and the
  // ticker/exchange helps the model confirm the company. Boilerplate (Corporation/WebSite/BreadcrumbList) is skipped
  // by the @type filter. {USER 2026-07-23 "JSON-LD Event 抽取(type-filtered) ... 公司 ticker/exchange 头行"}
  // [CONFIDENCE: CONFIRMED — direct user instruction; Q4/Nasdaq IR platforms commonly emit @type Event/NewsArticle].
  const _struct = [];                                       // lines prepended to `inline` (the model's primary context)
  let _ticker = '';                                         // item 5: exchange:ticker, from Organization.tickerSymbol
  const _ldEvents = [];                                     // item 3: {nm,dt,u} from @type Event / NewsArticle
  // item 5: a clean "EXCHANGE: SYMBOL" pattern (e.g. "NYSE: KO") — IR JSON-LD descriptions almost always carry it,
  // and it's the exact form the user wants (beats a bare tickerSymbol that can come back malformed like ": KO").
  const _EXCH_RE = /\\b(NYSE American|NYSE Arca|NYSE|NASDAQ|OTCMKTS|OTCQX|OTCQB|OTC|TSXV|TSX|LSE|HKEX|SEHK|ASX|Euronext|XETRA|FWB|BSE|NSE|SSE|SZSE|KRX|KOSDAQ|TSE|JPX|SGX|TWSE)\\s*:\\s*([A-Z0-9.]{1,7})\\b/;
  const _flatLd = (x, out) => {                             // flatten @graph + nested arrays into a flat object list
    if (Array.isArray(x)) { for (const y of x) _flatLd(y, out); }
    else if (x && typeof x === 'object') { out.push(x); if (x['@graph']) _flatLd(x['@graph'], out); }
  };
  try {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data; try { data = JSON.parse(s.textContent); } catch (e) { continue; }   // one malformed block must not sink the rest
      const objs = []; _flatLd(data, objs);
      for (const o of objs) {
        const types = (Array.isArray(o['@type']) ? o['@type'] : [o['@type']]).filter(Boolean).map(String);
        if (types.some(t => /Event$/.test(t) || t === 'NewsArticle')) {            // item 3: dated event, not boilerplate
          const nm = (o.name || o.headline || '').toString().replace(/\\s+/g, ' ').trim();
          const dt = (o.startDate || o.datePublished || o.dateCreated || '').toString().trim();
          let u = o.url; if (u && typeof u === 'object') u = u.url || '';           // url can be a string or a node
          if (!u && o.mainEntityOfPage) u = (typeof o.mainEntityOfPage === 'string') ? o.mainEntityOfPage : (o.mainEntityOfPage.url || '');
          u = (u || '').toString().trim();
          if (nm || u) _ldEvents.push({nm, dt, u});
        }
        if (!_ticker) {                                    // item 5: prefer "EXCHANGE: SYMBOL" from the description
          const em = (o.description || '').toString().match(_EXCH_RE);
          if (em) _ticker = em[1] + ': ' + em[2];          // e.g. "NYSE: KO"
          else if (o.tickerSymbol) { const tk = String(o.tickerSymbol).replace(/[\\s:]+/g, ' ').trim(); if (tk) _ticker = tk; }   // clean stray ":"
        }
      }
    }
  } catch (e) {}
  if (!_ticker) {                                           // item 5 fallback: scan a <meta> ticker / the visible body
    const m = document.querySelector('meta[name="ticker"],meta[property="ticker"],meta[name="stock:ticker"],meta[property="stock:ticker"]');
    if (m) _ticker = (m.getAttribute('content') || '').replace(/[\\s:]+/g, ' ').trim();
    if (!_ticker) { const bm = (body || '').match(_EXCH_RE); if (bm) _ticker = bm[1] + ': ' + bm[2]; }   // last resort: body text
  }
  if (_ticker) _struct.push('TICKER: ' + _ticker);          // one line at the very top of the body
  if (_ldEvents.length) {                                   // a compact, high-reliability event block from structured data
    _struct.push('STRUCTURED EVENTS (from JSON-LD — reliable):');
    for (const e of _ldEvents.slice(0, 80)) {               // cap so a huge feed can't dominate the context
      _struct.push('- ' + [e.nm, e.dt, (e.u ? '[link](' + e.u + ')' : '')].filter(Boolean).join(' — '));
    }
  }
  // "signal" = the block text is substantial enough to be an event cluster: has a DATE or ≥40 chars. A bare
  // icon / "Read more" link whose immediate container is just the anchor fails this → we WALK UP.
  const DATE_RE = /\\b(20[12]\\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b/i;
  // M11 — CJK-AWARE SIGNAL: the ≥40-char + western-month DATE_RE test is English-biased. A JP/KR/CN event row
  // ("2026年2月4日 第3四半期決算説明会") is <40 chars AND carries no western month, so hasSignal wrongly failed and
  // the walk-up over-expanded / the row was judged thin — CJK pages under-extracted. Add (a) a CJK date pattern
  // (年月日 / 년월일 / 年 月) and (b) a lower length floor when the text CONTAINS CJK (each han/kana/hangul char is
  // ~1 word of info, so 12 CJK chars ≈ 40 latin). {AUDIT wf_7e9d7ce1 M11 "CJK render 信号漏判"} [CONFIDENCE: CONFIRMED].
  const CJK_RE = /[\\u3040-\\u30ff\\u3400-\\u9fff\\uac00-\\ud7af]/;               // kana + CJK ideographs + hangul
  const CJK_DATE_RE = /(19|20)\\d\\d\\s*[年년]|\\d{1,2}\\s*[月월]\\s*\\d{1,2}\\s*[日일]/;  // 2026年 / 2月4日 / 2월4일
  const hasSignal = (t) => DATE_RE.test(t) || CJK_DATE_RE.test(t) || (CJK_RE.test(t) ? t.length >= 12 : t.length >= 40);
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.href;                                  // .href is already absolute-resolved by the browser
    if (!(h.startsWith('http://') || h.startsWith('https://')) || seen.has(h)) continue;
    seen.add(h);
    links.push(h);
    // Start at the nearest ROW/CARD/list-item ancestor = the date+title+description cluster around this link.
    let node = a.closest('li, tr, article, .card, [class*=item], [class*=row], [class*=teaser], [class*=result], [class*=news], [class*=event]') || a.parentElement || a;
    let ctx = (node.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    // HARDENING: icon / bare "Read more" links put the date+title in a SIBLING block, so the immediate container
    // is thin. Walk UP to progressively larger ancestors until the text carries a real signal (a date or enough
    // words) — capped at 4 hops + 280 chars so a huge <section> can't dump the whole page into one link's context.
    // {USER 2026-07-05 "there might be cases where the title is far away" -> expand until the cluster is found]
    // [CONFIDENCE: CONFIRMED via 10-company probe — real events carry a title anchor; the walk-up covers the
    // icon/read-more edge case (MCO empty-anchor) where the title sits in a neighbouring node].
    let hops = 0;
    while (!hasSignal(ctx) && node.parentElement && hops < 4) {
      node = node.parentElement;
      ctx = (node.innerText || '').replace(/\\s+/g, ' ').trim();
      hops++;
    }
    ctx = ctx.slice(0, 280);
    annotated.push(ctx + ' ' + h);                     // cluster THEN url → link_contexts window captures the cluster
  }
  const text = body + '\\n' + annotated.join('\\n');   // innerText first (wall detection) + the link-annotated block
  // INLINE-LINKED + STRUCTURE-PRESERVING serialization: walk the body in READING ORDER and emit a compact MARKDOWN-ish
  // view that keeps the tags an IR-event model actually needs — each <a> inline as [anchor](url); headings as #..######;
  // table rows as "cell | cell | ..."; <li> as "- "; <time datetime> as "visible (ISO)"; and for an icon-only link
  // with no text, its aria-label/title/img-alt becomes the anchor. WHY keep THESE tags (not flatten, not raw HTML): an
  // event's date lives in <time datetime>, its row-grouping in <tr>/<li>, its section in <h*>, and download links are
  // often icon-only — flattening drops exactly the cues that say "this row is one dated event with these files"; raw
  // HTML (class/style/div soup) would be token-bloat noise, so we keep ONLY these high-signal structures.
  // {USER "keep more info and tags from the raw content — you determine which is useful for the llm"}
  // item 4 (2026-07-23): THEAD/TBODY/TFOOT/CAPTION added so a data table's header section separates cleanly from its
  // body rows — many older table-layout IR sites carry column meaning (Date|Title|Type|Download) in a <thead> row.
  const _BLOCK = /^(DIV|P|SECTION|ARTICLE|UL|OL|DL|DD|DT|MAIN|ASIDE|FIGURE|BLOCKQUOTE|HEADER|FOOTER|NAV|FORM|FIELDSET|TABLE|THEAD|TBODY|TFOOT|CAPTION|HR)$/;
  const _anchorText = (a) => {                          // best available label for a link (recovers icon-only links)
    let t = (a.innerText||a.textContent||'').replace(/\\s+/g, ' ').trim();
    if (!t) t = (a.getAttribute('aria-label')||a.getAttribute('title')||'').replace(/\\s+/g, ' ').trim();
    if (!t) { const im = a.querySelector('img[alt]'); if (im) t = (im.getAttribute('alt')||'').replace(/\\s+/g, ' ').trim(); }
    return t.slice(0, 200);
  };
  const _inlineSeen = new Set();                       // dedup links across the whole inline walk (hidden mobile nav + visible desktop nav emit the SAME hub href → keep one)
  const _walk = (node, out) => {
    for (const ch of node.childNodes) {
      if (ch.nodeType === 3) {                          // text node — keep visible text, collapse whitespace
        const t = ch.textContent.replace(/\\s+/g, ' ');
        if (t.trim()) out.push(t);
        continue;
      }
      if (ch.nodeType !== 1) continue;
      const tag = ch.tagName;
      if (tag==='SCRIPT'||tag==='STYLE'||tag==='NOSCRIPT'||tag==='SVG'||tag==='TEMPLATE') continue;
      // SHOW EVERYTHING — do NOT skip hidden (display:none / visibility:hidden) subtrees. WHY the old hidden-skip was
      // wrong: a SPA IR site keeps its section-hub nav (/earnings /news /events /sec-filings /annual-meeting) ONLY in a
      // display:none MOBILE menu, so skipping hidden dropped every hub link from `inline` → the model saw no routes →
      // the crawl STOPPED at ONE page (GOOGL abc.xyz: 9 events / 1 page, deeper archives unreached). We do NOT filter
      // (filtering LOSES the hub links); we render all of it and DEDUP links (below) so a hub link present in BOTH the
      // hidden mobile nav and the visible desktop nav appears exactly ONCE. {USER 2026-07-23 "you need to show everything
      // just dedup those"} [CONFIDENCE: CONFIRMED 100% — page.html: the earnings hub <a> lived only in .nav--mobile--
      // expand display:none; showing + deduping surfaces it with no duplication]. (script/style/svg are still skipped above.)
      if (tag==='A' && ch.href && /^https?:/.test(ch.href)) {           // link → [anchor](url) INLINE (no recurse), deduped
        if (!_inlineSeen.has(ch.href)) { _inlineSeen.add(ch.href); out.push(' [' + _anchorText(ch) + '](' + ch.href + ') '); }
      } else if (tag==='TIME') {                        // <time datetime> → keep the MACHINE-READABLE ISO date too
        const dt = (ch.getAttribute('datetime')||'').trim();
        const tx = (ch.innerText||ch.textContent||'').replace(/\\s+/g, ' ').trim();
        out.push(dt ? ' ' + tx + ' (' + dt + ') ' : ' ' + tx + ' ');
      } else if (/^H[1-6]$/.test(tag)) {                // heading → markdown #.. so section/event-title level shows
        out.push('\\n' + '#'.repeat(+tag[1]) + ' '); _walk(ch, out); out.push('\\n');
      } else if (tag==='LI') {                          // list item → "- " keeps each event row separate
        out.push('\\n- '); _walk(ch, out);
      } else if (tag==='TR') {                          // table row → newline; its cells add " | " separators
        out.push('\\n'); _walk(ch, out);
      } else if (tag==='TD' || tag==='TH') {            // table cell → " | " so date|title|links stay one visible row
        _walk(ch, out); out.push(' | ');
      } else if (tag==='BR') {
        out.push('\\n');
      } else {                                          // generic block → newline-wrap so rows/sections stay separated
        const blk = _BLOCK.test(tag);
        if (blk) out.push('\\n');
        _walk(ch, out);
        if (blk) out.push('\\n');
      }
    }
  };
  const _ip = [];
  try { _walk(document.body, _ip); } catch (e) {}
  let inline = _ip.join('')
    .replace(/[ \\t]+/g, ' ')          // collapse runs of spaces/tabs
    .replace(/ *\\| *\\n/g, '\\n')     // drop a dangling " | " left at a table row's end
    .replace(/ *\\n */g, '\\n')        // trim spaces hugging newlines
    .replace(/\\n{3,}/g, '\\n\\n')     // cap blank runs
    .trim();
  // items 3+5: put the ticker header + JSON-LD structured-event block at the TOP of the model's context (most reliable
  // signals first), above the reading-order body. Empty when the page has neither (no cost on a page like this one).
  if (_struct.length) inline = _struct.join('\\n') + '\\n\\n' + inline;
  return {text, links, inline};
}"""
