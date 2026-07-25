"""event_agent.extract — THE ENDPOINT: (page text + optional screenshot + url) → build the event prompt → send to
Qwen IN PARALLEL (via the qwen_llm provider transport) → {events, routes}.

用一句话讲完: 给一批页面(每个 = url + 全文 text + 可选截图)→ 每页拼一个 job(event SYSTEM + user + 图 + 强制
schema)→ providers.qwen_llm.QwenClient.send_many 一次并行全发 → 拿回 {events, routes} → 确定性兜底(feed/资产 URL
regex 秒杀、去重、强制 event⊥route 互斥)。**event 逻辑在这里,传输在 provider —— 换模型/换 provider 不动这层。**
"""
from __future__ import annotations

import asyncio
import os
import random
import re

# RENDER/CRAWL BASELINE mode — when EVENT_FAKE_EXTRACT=1, the extract step SKIPS the VLM entirely and returns a RANDOM
# selection of the page's own links as routes (events=[]). This drives the REAL crawl loop (render + frontier + BFS)
# with NO VLM, so we can measure whether the browser render + the crawl pipeline are reliable in isolation — the clean
# baseline. {USER 2026-07-24 "use the loop, just replace the vlm with a random selector ... no need to call the VLM"}.
_FAKE_EXTRACT = os.environ.get("EVENT_FAKE_EXTRACT", "") in ("1", "true", "yes")

# THE ONE SWITCH for the whole screenshot pipeline — same env var render.py/config.py read (config.NO_SHOT). When
# WATERCRAWL_NO_SHOT=1: (render side) no screenshot is captured, AND (this side) no image is attached to the VLM request
# even if one somehow exists — so BOTH ends are governed by ONE button. WHY read the env directly instead of importing
# watercrawl.config: keeps extract's existing flag idiom (_FAKE_EXTRACT above) and avoids an agent→provider config import;
# the ENV VAR itself is the single source of truth, so render and VLM can never disagree. {USER 2026-07-24 "add a button
# to disable vlm image take in so that both sides are controlled by one"} [CONFIDENCE: CONFIRMED — user directive].
_NO_SHOT = os.environ.get("WATERCRAWL_NO_SHOT", "1") in ("1", "true", "yes")   # DEFAULT ON — MUST match config.py NO_SHOT default (both read this same env var; if the two defaults drift, render and VLM disagree). Set WATERCRAWL_NO_SHOT=0 to re-enable the screenshot end-to-end.

from providers.qwen_llm import QwenClient          # the PROVIDER transport — generic parallel sender, no event logic

from . import prompts                              # the EVENT logic — instruction + schema

# 20000 (not 48000) is the CHUNK TRIGGER for text-only pages: a page over this is split into ~2000-char blocks instead
# of extracted in ONE pass. WHY lowered: a large single pass is UNSTABLE — the model's per-row date/title extraction
# collapses past ~15 rows (see _CHUNK_TARGET_CHARS), and that same collapse hits a big SINGLE pass, so a 35k-char
# event-dense page (nice.com /upcoming-event) swung 52→3 events run-to-run. Over-cap now routes the FULL text through
# chunking (raw_text, not truncated) → each ~14-row block is under the cliff → stable, complete extraction. Normal
# pages (< 20k, the vast majority) still single-pass. {TEST 2026-07-24 nice.com 35k single-pass 52 vs 3 variance}
# [CONFIDENCE: CONFIRMED — chunking a large page is what stabilizes it, same fix as the 400-row mega-list].
MAX_INPUT_CHARS = int(os.environ.get("EVENT_MAX_INPUT_CHARS", "20000"))   # text-only: over this → chunk (stable), not one big pass
# Vision pages: a high safety cap so a truly pathological page can't blow past even a 32768 ctx — but generous enough
# that a normal rich IR page (coca-cola/apple ≈ 11-15k inline chars) is NEVER trimmed → its screenshot is ALWAYS kept.
# {DEBUG: 8000 was catastrophically low — it trimmed normal pages into the shot-dropping chunk path}. Requires the server
# at --max-model-len 32768 (image ~1.5k tok + this ~7k + system ~1k + output ≤8k ≈ 17.5k < 32768).
_VISION_TEXT_CHARS = int(os.environ.get("EVENT_VISION_TEXT_CHARS", "24000"))

# Deterministic non-event URL filter — the structural traps (feeds / asset stores). Cheaper + more reliable than any
# model, so we strip them post-hoc even though the prompt forbids them too (belt + suspenders).
_NONEVENT_URL_RE = re.compile(
    r'\.(xml|rss|atom|css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|#|$)'
    r'|/(rss|feeds?|atom|sitemap)(/|\.|\?|#|$)'
    r'|/content/dam/|/sites/[^/]+/files/|/media/documents?/',
    re.I)


# The page_text feeds links INLINE as `[anchor](url)`, and the model SOMETIMES copies that whole markdown wrapper into
# a url field instead of the bare url — e.g. "[Webcast](https://...)". A bare startswith("http") check then rejects it,
# the event loses all its urls, and _normalize DROPS the event → 0 events for a page the model actually read correctly.
# {DEBUG 2026-07-23 tests/output/www_microsoft_com.txt: model emitted "urls":["[Webcast](https://.../earnings-fy-2026-q4)"]
# → _clean_urls dropped all → 0 events, while Coca-Cola (bare urls) got 120}. [CONFIDENCE: CONFIRMED 100% — Microsoft +
# Apple debug dumps show good events lost purely to the markdown wrapper]. Unwrap it before the http check.
_MD_LINK_RE = re.compile(r'\[[^\]]*\]\((https?://[^)\s]+)\)')


def _unwrap_url(u: str) -> str:
    """If u is a markdown link `[text](http…)` return just the url; else return u stripped. Belt for the model copying
    the inline `[anchor](url)` form verbatim into a url/route field."""
    u = (u or "").strip()
    m = _MD_LINK_RE.match(u)
    return m.group(1) if m else u


def _clean_urls(raw: list) -> list[str]:
    """http(s) only, drop structural junk, dedup — preserving order. Used for an event's `urls` list. Unwraps a markdown
    `[text](url)` wrapper first (the model sometimes emits the inline link verbatim → would otherwise drop the event)."""
    out, seen = [], set()
    for u in raw or []:
        u = _unwrap_url(u)
        if u.startswith("http") and u not in seen and not _NONEVENT_URL_RE.search(u):
            seen.add(u)
            out.append(u)
    return out


# CHUNKING knobs (re-added 2026-07-24, text-only-safe). WHY chunking is BACK: it was deleted because the chunk path
# went text-only + dropped the screenshot → VL model bagged nav junk. But the pipeline now DEFAULTS to NO_SHOT (text/
# DOM-only, no screenshot at all), so a chunk drops NOTHING the single call had — the objection is gone. Chunking is
# the ONLY real fix for a mega-list page whose events-with-titles output exceeds even the Lnn-compacted budget: split
# the input → each block's output is ~1/N → fits → merge. {USER 2026-07-24 "let's do chunk + using l1 l2 l3 again"}.
# 2000 chars ≈ ~14 IR rows. WHY THIS SMALL: the bind is NOT the output-token budget — it is a SHARP CLIFF in the
# text-only model's per-row extraction. A block-size sweep (tests/probe_fix.py, temp=0, guided-decoding) found:
# 10 rows → 10/10 dates, 15 rows → 15/15 dates, but 20/25/31 rows → 0 dates — at ≥~20 uniform rows Qwen2.5-VL-7B (no
# screenshot to anchor it) DETERMINISTICALLY collapses to url-REFS ONLY ({title:"",date:"",type:X,urls:[Lx,Ly]}), and
# the footer-chrome guard (no date AND no title) then drops EVERY row → 0 events. Neither temperature (0→1 all 0 dated)
# nor sampler penalties (broke the JSON) lift it — the ONLY lever is keeping each block UNDER the cliff. 2000 chars
# lands ~14 rows, safely below the 15-row reliable ceiling with margin for real rows longer than the synthetic ones.
# {TEST 2026-07-24 probe_fix.py sweep: 15 rows RELIABLE, 20 rows COLLAPSED} [CONFIDENCE: CONFIRMED 100% — clean cliff].
_CHUNK_TARGET_CHARS = int(os.environ.get("EVENT_CHUNK_TARGET_CHARS", "2000"))    # aim each block's INPUT ≈ this (~14 rows, under the cliff)
# 0 overlap — rows are SINGLE LINES and _split_text snaps every cut to a "\n" boundary, so no event ever straddles a
# cut; a backward overlap would only DUPLICATE whole rows AND inflate each block past the ~15-row reliability cliff
# (the exact bug that dropped recall to 83/400: overlap=1500 pushed ~14-row blocks to ~31 rows → collapse). Cutting
# cleanly AT the limit with no overlap is also literally what "simple chunking, cut each at the limit" asked for.
# {USER 2026-07-24 "simple chunking, cut each at the limit"} [CONFIDENCE: CONFIRMED 100% — overlap was pure harm here].
_CHUNK_OVERLAP = int(os.environ.get("EVENT_CHUNK_OVERLAP", "0"))                 # no backward overlap — single-line rows never split

# HARD CAP on the NUMBER of chunk blocks per page. WHY: at a 2000-char target a runaway page explodes the block count —
# a 6.3M-char document (energytransfer /static-files/<uuid> that slipped the frontier gate) → ceil(6.3M/2000)=3172 blocks
# = 3172 VLM calls for ONE page, and one company had 4 of them (~12k calls). A cap bounds VLM cost per page: above it, we
# chunk only the TOP cap×target chars (a real events LIST lives near the top — a 150-block × 2000 = 300k-char window covers
# any genuine IR events/archive page; only served DOCS exceed it) and DROP + FAIL-LOUD the tail. 150 covers real pages
# generously (a normal IR page < ~100k = 50 blocks) while killing the 3172-block explosion. {SMOKE 2026-07-24 energytransfer
# 6.3M → 3172 blocks} [CONFIDENCE: CONFIRMED 100% — user: "we need a cap for number of blocks"]. Env-overridable to tune.
_MAX_CHUNK_BLOCKS = int(os.environ.get("EVENT_MAX_CHUNK_BLOCKS", "150"))
# Links per ROUTING call. WHY SMALL (40, not 120): two reasons. (1) TRUNCATION — routing a link-heavy page (an
# 800-link mega-list) in one call hit finish_reason=length → 0 routes. (2) DISCRIMINATION — the bigger lever: with a
# whole page's links in one block the model STOPS discriminating and BULK-ROUTES everything at a flat 0.9 (or bails to
# 0); in SMALL blocks it judges each link on its own url and correctly drops product/chrome. {TEST 2026-07-24 cintas:
# 180 links in 1 block → 0 (anchor) / 103 bulk (with url); the SAME links in 40-link blocks → 14 with the real
# /investors/* sections on top} [CONFIDENCE: CONFIRMED 100% — chunking is what forces per-link judgement]. Routes merge
# across blocks (dedup by url, keep max score); a normal IR page (dozens of links) is still one block.
_ROUTE_TARGET_LINKS = int(os.environ.get("EVENT_ROUTE_TARGET_LINKS", "40"))      # links per routing call (small → forces discrimination)


def _norm_txt(s: str) -> str:
    """Lowercase + collapse every non-alphanumeric run to a single space — so an evidence snippet can be matched against
    the page ignoring the punctuation / whitespace / case differences the model introduces when copying."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _grounded(evidence: str, source_norm: str) -> bool:
    """GROUNDING CHECK (anti-hallucination) — is the model's `evidence` snippet ACTUALLY in the page it read? The model
    must copy a verbatim snippet proving each event; if that snippet isn't in the source, the event was FABRICATED
    (e.g. synthesizing "Q2 2026 Webcast of Q2 2026" from a bare "Webcast" nav link) → drop it. All three model
    precisions (AWQ/FP8/FP16) hallucinate identically, so this is a PROMPT/verification fix, not a quantization one.
    Normalized substring tolerates punctuation/whitespace; the 30-char prefix tolerates a trailing word the model adds.
    {TECHNIQUE: web-searched grounding / quote-from-source + chain-of-verification for small-model extraction 2026-07-24;
    A/B proved precision doesn't change the hallucination} [CONFIDENCE: CONFIRMED 100% — direct fix for it]."""
    ev = _norm_txt(evidence).split()
    if len(ev) < 3:                                           # < 3 words is too little to prove anything → ungrounded
        return False
    # ANY 3-consecutive-word run of the evidence must appear VERBATIM (punct-insensitive) in the page. This keeps real
    # events even when the model reformats the ends of its snippet (recovers false-negatives like NiCE World that a
    # whole-string match dropped), while still killing a fabricated title assembled from words scattered across the page
    # (acadiarealty's "Q1 2026 Earnings Conference Call" — no contiguous run of it exists on that nav-only page).
    return any(" ".join(ev[i:i + 3]) in source_norm for i in range(len(ev) - 2))


def _normalize_events(result: dict, tag_map: dict, source: str = "") -> dict:
    """EXTRACTION reply → {"events":[...], "_event_urls":set, "_error":?}. Lnn ids → urls (resolve_ids drops unknowns) +
    _clean_urls drops feed/asset junk. GROUNDING: each event must carry an `evidence` snippet that appears in `source`
    (the page the model read) — an event whose evidence is not in the page was hallucinated and is DROPPED. Then every
    kept event has ≥1 clean url + (a date OR a title). `_event_urls` feeds _combine's event⊥route exclusivity. {USER
    2026-07-24 "web search ... tricks on these small models" → evidence-grounding} [CONFIDENCE: CONFIRMED 100%]."""
    src = _norm_txt(source)                                    # normalized page text the model saw — matched vs each evidence
    events, event_urls = [], set()
    for e in (result.get("events") or []):
        if src and not _grounded(e.get("evidence") or "", src):   # evidence not in the page → fabricated → drop
            continue
        urls = _clean_urls(prompts.resolve_ids(e.get("urls") or [], tag_map))   # Lnn ids → real urls → drop junk + dedup
        if not urls:                                          # an event must have at least one real url, else drop it
            continue
        title = (e.get("title") or "").strip()[:300]
        date = (e.get("date") or "").strip()
        etype = (e.get("type") or "").strip()
        # FOOTER-CHROME GUARD: a genuine IR event ALWAYS has a DATE or a nameable TITLE. A footer/nav link cluster the
        # model mis-read as an event arrives {title:"", date:"", urls:[...]} — url present, no date, no title → chrome,
        # drop it. {USER 2026-07-23 "top events 全是 footer 导航被当成假 event"} [CONFIDENCE: CONFIRMED 95%].
        if not date and not title:
            continue
        events.append({"title": title, "date": date, "type": etype, "urls": urls})
        event_urls.update(urls)
    out = {"events": events, "_event_urls": event_urls}
    if result.get("_error"):                                  # HARD transport failure → "extraction FAILED" not "0 events"
        out["_error"] = result["_error"]
    return out


def _normalize_routes(result: dict, tag_map: dict) -> list[dict]:
    """ROUTING reply → [{"url","score"}] resolved via tag_map, feed/asset dropped, deduped, score clamped to [0,1].
    Order preserved. event⊥route exclusivity is applied LATER in _combine (it needs the extraction half's urls)."""
    routes, seen = [], set()
    for r in (result.get("routes") or []):
        # each route is {"ref": Lnn id, "score": 0.0-1.0}. Accept a legacy "url" key or a bare string defensively.
        if isinstance(r, dict):
            ref, score = (r.get("ref") or r.get("url") or ""), r.get("score", 0.5)
        else:
            ref, score = r, 0.5
        resolved = prompts.resolve_ids([ref], tag_map)        # Lnn id → real url (unknown id → dropped, never guessed)
        if not resolved:
            continue
        u = resolved[0]
        if u in seen or _NONEVENT_URL_RE.search(u):           # feed/asset is never a go-deeper target
            continue
        seen.add(u)
        try:
            sc = max(0.0, min(1.0, float(score)))             # clamp confidence to [0,1]
        except (TypeError, ValueError):
            sc = 0.5
        routes.append({"url": u, "score": sc})                # {url, score} — frontier is a priority queue on score
    return routes


def _combine(events: list, event_urls: set, routes: list, error: str | None) -> dict:
    """Merge the EXTRACTION half (events) + the ROUTING half (routes) into the final page result, enforcing event⊥route
    EXCLUSIVITY across the two independent calls: a route whose resolved url is ALSO an event's url is DROPPED (an event
    is a leaf — never re-follow it). Propagates any hard error so the crawl can tell "FAILED" from "genuinely empty".
    {USER 2026-07-24 "separate the routing and the classification"} [CONFIDENCE: CONFIRMED 100% — post-hoc exclusivity is
    what makes two separate calls safe]."""
    kept = [r for r in routes if r["url"] not in event_urls]   # exclusivity now lives here (two calls can't self-enforce it)
    out = {"events": events, "routes": kept}
    if error:
        out["_error"] = error
    return out


def _text_cap(page: dict, use_image: bool) -> int:
    """The char cap page_text is limited to before tagging: smaller WITH an image (image carries layout + its tokens are
    scarce), full budget text-only. One source of truth for the input bound."""
    has_img = bool(use_image and page.get("image_b64"))
    return _VISION_TEXT_CHARS if has_img else MAX_INPUT_CHARS


def _build_events_job(tagged_text: str, page_url: str, image_b64, use_image: bool) -> dict:
    """EXTRACTION job from ALREADY-Lnn-TAGGED page text → events only. Attaches the screenshot only when use_image."""
    return {
        "system": prompts.SYSTEM_EVENTS,
        "user": prompts.build_events_user(tagged_text, page_url),
        "image_b64": image_b64 if use_image else None,
        "guided_json": prompts.EVENTS_SCHEMA,
    }


def _build_routes_job(link_block: str, page_url: str) -> dict:
    """ROUTING job from the flat link list (link_list()) → routes only. Never carries an image (links-only, tiny input)."""
    return {
        "system": prompts.SYSTEM_ROUTES,
        "user": prompts.build_routes_user(page_url, link_block),
        "image_b64": None,
        "guided_json": prompts.ROUTES_SCHEMA,
    }


def _split_text(text: str, n: int) -> list[str]:
    """Split text into n contiguous blocks on NEWLINE boundaries (never mid-line → never mid-`[anchor](Lnn)`), each
    block after the first extended BACKWARD by ~_CHUNK_OVERLAP chars (snapped to a line start) so no event straddles a
    cut; the merge dedups the overlap. Each block is tagged INDEPENDENTLY by the caller (its own Lnn map) — a per-block
    map is the OTHER half of the old-Lnn-bug fix (a shared global map with per-block renumbering collided)."""
    if n <= 1 or len(text) <= _CHUNK_OVERLAP:
        return [text]
    step = len(text) // n
    bounds = [0]
    for k in range(1, n):
        p = min(step * k, len(text))
        nl = text.rfind("\n", 0, p)                           # snap the cut back to a line boundary
        bounds.append(nl + 1 if nl > 0 else p)
    bounds.append(len(text))
    blocks = []
    for k in range(n):
        start = bounds[k]
        if k > 0:                                             # widen leftward for overlap, snapped to a line start
            back = max(0, start - _CHUNK_OVERLAP)
            nl = text.rfind("\n", 0, back)
            start = nl + 1 if nl > 0 else back
        blocks.append(text[start:bounds[k + 1]])
    return blocks


async def _extract_events_chunked(text: str, page_url: str, c: QwenClient, use_image: bool) -> dict:
    """EXTRACTION FALLBACK — a page too big for one pass (over cap / output truncated / model bailed empty) → SIMPLE
    fixed-size chunking: cut into ceil(len/_CHUNK_TARGET_CHARS) blocks (each ≈ the char limit, cut cleanly at line
    boundaries, NO overlap), tag EACH block with its OWN Lnn map, extract EVENTS from all in parallel, resolve each via
    ITS map, merge (dedup by url overlap). NO RECURSION, NO ROUTES (routing is a separate single call now). A block that
    STILL truncates is accepted as a partial and flagged (_error). {USER 2026-07-24 "simple chunking, cut each at the
    limit, not using recursion, but keep the have url"} [CONFIDENCE: CONFIRMED 100% — direct instruction]."""
    n = max(2, -(-len(text) // _CHUNK_TARGET_CHARS))          # ceil-div → each block ≈ _CHUNK_TARGET_CHARS (~14 rows, under the cliff)
    cap_note = None
    if n > _MAX_CHUNK_BLOCKS:                                  # runaway page → cap blocks, process the TOP window, drop+flag tail
        kept = _MAX_CHUNK_BLOCKS * _CHUNK_TARGET_CHARS         # the top cap×target chars (real events/list live near the top)
        cap_note = f"block cap: {n} blocks > {_MAX_CHUNK_BLOCKS}; processed top {kept} chars, dropped tail {len(text)-kept} chars"
        print(f"[extract] ⚠️ BLOCK CAP {page_url[:70]} — {cap_note}", flush=True)
        text = text[:kept]                                    # only the top window is chunked → VLM calls bounded to the cap
        n = _MAX_CHUNK_BLOCKS
    blocks = _split_text(text, n)
    print(f"[extract] ✂️  chunking {page_url[:70]} → {len(blocks)} blocks (events-only, ~{_CHUNK_TARGET_CHARS} chars each)", flush=True)
    jobs, maps = [], []
    for b in blocks:
        tg, mp = prompts.tag_links(b)                         # EACH block gets its OWN Lnn id space + map
        jobs.append(_build_events_job(tg, page_url, None, False))   # text-only chunk (no image on the split path)
        maps.append(mp)
    results = await c.send_many(jobs)
    merged_events, seen, event_urls, errs = [], set(), set(), ([cap_note] if cap_note else [])   # cap → fail-loud in _error
    for i, res in enumerate(results):
        if res.get("__finish__") == "length":                # block still truncated at the limit → accept partial, DON'T re-split
            errs.append(f"block {i} truncated at limit")
        ne = _normalize_events(res, maps[i], blocks[i])      # resolve THIS block's ids + ground evidence against THIS block's text
        if ne.get("_error"):
            errs.append(ne["_error"])
        for e in ne["events"]:                                # merge + dedup by ANY url overlap → overlap-region dupes drop
            keys = {u for u in e["urls"]}
            if keys & seen:
                continue
            seen |= keys
            merged_events.append(e)
            event_urls |= keys
    out = {"events": merged_events, "_event_urls": event_urls}
    if errs:                                                  # some block truncated/failed → result is INCOMPLETE (fail loud)
        out["_error"] = "; ".join(errs[:5])
    return out


async def _route_page(raw_text: str, page_url: str, c: QwenClient) -> dict:
    """ROUTING → {"routes":[{url,score}], "_error":?}. Build the flat link list ONCE (one global Lnn→url map); if it is
    long, split into ≤_ROUTE_TARGET_LINKS-link blocks — all resolved via the SAME global map — route each in parallel,
    merge (dedup by url, keep the MAX score). Bounding links/block keeps each routing OUTPUT under the token budget so a
    link-heavy page can't truncate the whole routing result to 0. {USER 2026-07-24 "separate the routing and the
    classification"} [CONFIDENCE: CONFIRMED 100% — 817-link single call truncated; blocking fixes it]."""
    link_block, route_map = prompts.link_list(raw_text)        # one map for the WHOLE page — blocks share its Lnn ids
    lines = [ln for ln in link_block.split("\n") if ln.strip()]
    if not lines:                                              # a page with no links → nothing to route
        return {"routes": [], "_error": None}
    groups = [lines[i:i + _ROUTE_TARGET_LINKS] for i in range(0, len(lines), _ROUTE_TARGET_LINKS)]   # ≤N links each
    jobs = [_build_routes_job("\n".join(g), page_url) for g in groups]

    async def _run() -> tuple[dict, list]:
        results = await c.send_many(jobs)                      # all routing blocks in parallel
        best, errs = {}, []
        for res in results:
            if res.get("_error"):                              # a routing block hard-failed → note it (fail loud)
                errs.append(res["_error"])
            for r in _normalize_routes(res, route_map):        # resolve THIS block's refs via the shared global map
                if r["url"] not in best or r["score"] > best[r["url"]]["score"]:   # merge: keep the highest score per url
                    best[r["url"]] = r
        return best, errs

    best, errs = await _run()
    # WHOLE-PAGE ZERO RETRY: an IR page ALWAYS has some nav — 0 routes over a page WITH links + NO error is a transient
    # bad generation (seen under parallel GPU load: gartner routed 13 in isolation, 0 in a 10-page batch). Re-run once.
    # {DEBUG 2026-07-24 gartner parallel=0 vs isolated=13} [CONFIDENCE: CONFIRMED 95% — routing had no retry unlike events].
    if not best and not errs:
        print(f"[extract] ↻ ZERO routes on a linked page {page_url[:70]} — re-running routing once", flush=True)
        best, errs = await _run()
    return {"routes": list(best.values()), "_error": ("; ".join(errs[:3]) if errs else None)}


async def _extract_one(page: dict, c: QwenClient, use_image: bool) -> dict:
    """ONE page → {events, routes}. TWO focused calls fired IN PARALLEL: ROUTING (one call over the flat link list) +
    EXTRACTION (events-only; chunked if the body is over cap / truncates / bails empty). Combined with event⊥route
    exclusivity in _combine. Splitting the old single call shrinks each prompt + input (saves context) and keeps the
    extraction pass focused so it holds up on long lists. {USER 2026-07-24 "separate the routing and the classification,
    so we can save more context"} [CONFIDENCE: CONFIRMED 100% — direct instruction]."""
    use_img = use_image and not _NO_SHOT                       # THE ONE SWITCH: NO_SHOT forces text/DOM-only
    raw_text = page.get("page_text") or ""
    url = page.get("page_url", "")

    if _FAKE_EXTRACT:                                          # BASELINE: skip VLM, random-select deeper links from THIS page
        urls = list(dict.fromkeys(prompts._INLINE_LINK_RE.findall(raw_text)))
        urls = [u for (_a, u) in urls] if urls and isinstance(urls[0], tuple) else urls
        random.shuffle(urls)
        return {"events": [], "routes": [{"url": u, "score": 0.5} for u in urls[:12]]}

    async def _route() -> dict:
        return await _route_page(raw_text, url, c)             # ROUTING: link-list only, chunked if long, merged

    async def _events() -> dict:
        # EXTRACTION: events-only; chunk on over-cap / truncation / empty-on-large. {events, _event_urls, _error?}.
        cap = _text_cap(page, use_img)
        text = raw_text[:cap]
        if len(raw_text) > cap:                                # over cap → chunk the FULL text (don't drop the tail)
            print(f"[extract] ⚠️ INPUT over cap {url[:70]} — {len(raw_text)} chars > {cap} → chunking full text", flush=True)
            return await _extract_events_chunked(raw_text, url, c, use_img)
        tagged, tag_map = prompts.tag_links(text)              # inline [anchor](url) → [anchor](Lnn) + {Lnn: url}
        job = _build_events_job(tagged, url, page.get("image_b64"), use_img)
        res = (await c.send_many([job]))[0]

        def _empty(r: dict) -> bool:                           # content-full page → un-errored {events:[]} = transient bad gen
            return not r.get("_error") and not (r.get("events") or [])
        if _empty(res) and len(raw_text) > 2000:               # re-send once (else a seed silently zeroes a company)
            print(f"[extract] ↻ EMPTY VLM events for content-full {url[:70]} ({len(raw_text)} chars) — re-sending once", flush=True)
            res = (await c.send_many([job]))[0]
        if _empty(res) and len(text) > _CHUNK_TARGET_CHARS:    # model bailed on a big/dense page (not truncated) → chunk
            print(f"[extract] ✂️  EMPTY on LARGE page {url[:70]} ({len(text)} chars) → chunking (model bailed on one pass)", flush=True)
            return await _extract_events_chunked(raw_text, url, c, use_img)
        if res.get("__finish__") == "length":                  # output cut even with Lnn → chunk (split input, merge)
            print(f"[extract] ✂️  OUTPUT TRUNCATED {url[:70]} — finish=length → chunking", flush=True)
            return await _extract_events_chunked(raw_text, url, c, use_img)
        return _normalize_events(res, tag_map, tagged)         # ground evidence against the page the model read

    routes_r, ev = await asyncio.gather(_route(), _events())   # ROUTING + EXTRACTION concurrently on the GPU
    err = ev.get("_error") or routes_r.get("_error")           # fail loud if EITHER half hard-failed
    return _combine(ev["events"], ev.get("_event_urls") or set(), routes_r["routes"], err)


async def extract_page(page: dict, client: QwenClient | None = None, use_image: bool = False) -> dict:
    """ONE page → {events, routes}. For many pages use extract_pages (true parallel). Truncation → chunk fallback."""
    return await _extract_one(page, client or QwenClient(), use_image)


async def extract_pages(pages: list[dict], client: QwenClient | None = None, use_image: bool = False) -> list[dict]:
    """MANY pages → many {events, routes}, ALL in flight at once (the parallel endpoint). `pages` = list of
    {page_url, page_text, image_b64?, links_block?}. One normalized result per page, order preserved. Each page sends
    Lnn-tagged text (compact output); a mega-list that still truncates falls back to input chunking. The global
    semaphore bounds in-flight requests."""
    c = client or QwenClient()
    return await asyncio.gather(*(_extract_one(p, c, use_image) for p in pages))
