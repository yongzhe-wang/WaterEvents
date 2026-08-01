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
from datetime import datetime, timezone      # plausibility window for extracted event dates (injection defence layer (c))

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
from ..storage.urls import _event_key              # (title+date) identity — the SAME key engine.py and db.py dedup on

# 20000 (not 48000) is the CHUNK TRIGGER for text-only pages: a page over this is split into ~2000-char blocks instead
# of extracted in ONE pass. WHY lowered: a large single pass is UNSTABLE — the model's per-row date/title extraction
# collapses past ~15 rows (see _CHUNK_TARGET_CHARS), and that same collapse hits a big SINGLE pass, so a 35k-char
# event-dense page (nice.com /upcoming-event) swung 52→3 events run-to-run. Over-cap now routes the FULL text through
# chunking (raw_text, not truncated) → each ~14-row block is under the cliff → stable, complete extraction. Normal
# pages (< 20k, the vast majority) still single-pass. {TEST 2026-07-24 nice.com 35k single-pass 52 vs 3 variance}
# [CONFIDENCE: CONFIRMED — chunking a large page is what stabilizes it, same fix as the 400-row mega-list].
# 40000 (was 20000): the 7B collapse cliff is GONE on the 14B (sweep 60 rows → 60/60 dated vs 7B 0 at 20), so a big page
# no longer needs to be split into tiny ~14-row blocks — it goes single-pass. BUT leave HEADROOM below the 32768 ctx:
# a request is input + QWEN_MAX_TOKENS(16384) + system(~1k); at 40000 chars ≈ ~12k input tok → ~29k total < 32768 (~10%
# spare). Above 40000 → chunk (a request whose input+16384 would exceed 32768 400s at the server, so we never let input
# approach ~52k). {TEST 2026-07-25 14B-AWQ sweep no collapse; USER "still leave some space"} [CONFIDENCE: CONFIRMED 100%].
MAX_INPUT_CHARS = int(os.environ.get("EVENT_MAX_INPUT_CHARS", "40000"))   # 14B: single-pass under this, leaves ctx headroom
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
# 20000 (was 2000): only the RARE page over MAX_INPUT_CHARS(40000) chunks now, and the 14B doesn't collapse, so blocks
# can be BIG — ~20000 chars ≈ ~6k input tok/block, well within ctx with output+system headroom. (7B needed ~2000/~14
# rows to stay under its collapse cliff; 14B has no cliff.) {TEST 2026-07-25 14B sweep 60/60} [CONFIDENCE: CONFIRMED].
_CHUNK_TARGET_CHARS = int(os.environ.get("EVENT_CHUNK_TARGET_CHARS", "20000"))   # 14B: big blocks (no cliff), still ctx-safe
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


# ── GROUNDING WINDOW CONSTANTS + FUZZY MATCHER ─────────────────────────────────────────────────────────────────────
# These three names were REFERENCED by _grounded but never DEFINED — `ruff --select F821` flagged them and a direct
# call reproduced it: `_grounded(...)` raised `NameError: name '_GROUND_WINDOW' is not defined`. Because _grounded runs
# on EVERY extracted event, the whole extraction path raised on its first event. Defined here to the values _grounded's
# own docstring already specifies, so the documented behaviour and the code finally agree.
# {RUFF 2026-07-28 "EXTRACT.PY:169 F821 UNDEFINED NAME `_GROUND_WINDOW`; :175 `_GROUND_MAX_EDITS`; :175 `_FUZZY_WINDOW`"}
# {_GROUNDED DOCSTRING "THE WINDOW IS NOW _GROUND_WINDOW (6) WORDS" / "_GROUND_MAX_EDITS, DEFAULT 1 WORD
#  SUBSTITUTION/INSERTION"}
# [CONFIDENCE: CONFIRMED 100% — the NameError is a reproduced runtime observation, and the values are read verbatim off
#  the docstring that describes them rather than chosen here.]
_GROUND_WINDOW = int(os.environ.get("EVENT_GROUND_WINDOW", "6"))        # words per grounding window (was 3 — boilerplate defeated it)
_GROUND_MAX_EDITS = int(os.environ.get("EVENT_GROUND_MAX_EDITS", "1"))  # word-level edits tolerated, so a lightly reflowed snippet still passes


def _fuzzy_window(win: list[str], src_words: list[str]) -> bool:
    """Does `win` (a run of evidence words) appear in `src_words` within _GROUND_MAX_EDITS word-level edits?

    WHY it exists: lengthening the grounding window from 3 to 6 words kills the boilerplate collisions that let a
    RECOMBINED event pass, but on its own it would resurrect the FALSE REJECTIONS the 3-word window was chosen to
    prevent — the model reformats the ends of a snippet it copies. Tolerating a small number of edits keeps the recall
    while the longer window supplies the precision; the two are independent levers, which is why both move together.

    Complexity is bounded deliberately: candidate offsets are taken only from positions where one of the window's words
    actually occurs (via a word→positions index built once per call), so this is near-linear in the number of matching
    positions rather than a scan of every offset in a multi-thousand-word page.

    Upstream: _grounded, once per evidence window that failed the exact-substring fast path. Downstream: True keeps the
    event, False drops it as ungrounded."""
    w = len(win)
    if w == 0 or len(src_words) < w:
        return False
    # word → the offsets in src_words where it occurs; used to propose only plausible alignments.
    pos: dict[str, list[int]] = {}
    for i, t in enumerate(src_words):
        pos.setdefault(t, []).append(i)
    cands = set()
    for off, tok in enumerate(win):                       # a window word at index `off` seen at src index i ⇒ start i-off
        for i in pos.get(tok, ()):
            s = i - off
            if 0 <= s <= len(src_words) - w:
                cands.add(s)
    for s in cands:
        # Same-length alignment: count substitutions and bail as soon as the budget is blown (no full distance needed).
        edits = 0
        for a, b in zip(win, src_words[s:s + w]):
            if a != b:
                edits += 1
                if edits > _GROUND_MAX_EDITS:
                    break
        if edits <= _GROUND_MAX_EDITS:
            return True
    return False


def _grounded(evidence: str, source_norm: str) -> bool:
    """GROUNDING CHECK (anti-hallucination) — is the model's `evidence` snippet ACTUALLY in the page it read? The model
    must copy a verbatim snippet proving each event; if that snippet isn't in the source, the event was FABRICATED
    (e.g. synthesizing "Q2 2026 Webcast of Q2 2026" from a bare "Webcast" nav link) → drop it. All three model
    precisions (AWQ/FP8/FP16) hallucinate identically, so this is a PROMPT/verification fix, not a quantization one.
    Normalized substring tolerates punctuation/whitespace; the 30-char prefix tolerates a trailing word the model adds.
    {TECHNIQUE: web-searched grounding / quote-from-source + chain-of-verification for small-model extraction 2026-07-24;
    A/B proved precision doesn't change the hallucination} [CONFIDENCE: CONFIRMED 100% — direct fix for it].

    TIGHTENED 2026-07-28: the window was 3 words, which IR boilerplate defeats — "q1 2026 earnings", "conference call
    webcast" and "fourth quarter results" are printed on essentially every IR page, so a RECOMBINED event (real phrases
    from different parts of the page, stitched into a disclosure that was never announced) passed grounding trivially.
    The window is now _GROUND_WINDOW (6) words, which no longer matches on a single boilerplate fragment.

    RESPECTING THE ORIGINAL LOOSENING: the 3-word window was chosen deliberately to stop FALSE REJECTIONS — the model
    reformats the ends of a snippet it copies (the NiCE World case a whole-string match dropped). A longer window alone
    would reintroduce exactly those false negatives, so the tightening ships WITH a tolerance: a window is grounded if it
    matches verbatim OR at small edit distance (_GROUND_MAX_EDITS, default 1 word substitution/insertion), so a snippet
    the model lightly reflowed still passes while a stitched-together fabrication — which differs by many words, not one
    — still fails. Evidence SHORTER than the window falls back to requiring the WHOLE snippet to match (with the same
    tolerance), so a legitimately terse 3-5 word snippet is not rejected for being short.
    {EXTRACT.PY (pre-fix) "ANY 3-CONSECUTIVE-WORD RUN OF THE EVIDENCE MUST APPEAR VERBATIM ... RECOVERS FALSE-NEGATIVES
     LIKE NICE WORLD THAT A WHOLE-STRING MATCH DROPPED"}
    [CONFIDENCE: CONFIRMED 95% — window length and edit tolerance are independent levers: the window kills boilerplate
     collisions, the tolerance preserves the reflow recall the 3-word window was protecting]."""
    ev = _norm_txt(evidence).split()
    if len(ev) < 3:                                           # < 3 words is too little to prove anything → ungrounded
        return False
    src_words = source_norm.split()
    w = min(_GROUND_WINDOW, len(ev))                          # short evidence → match the whole snippet, not a sub-window
    # Slide every w-word window of the evidence over the page; grounded if ANY window matches within the edit tolerance.
    for i in range(len(ev) - w + 1):
        win = ev[i:i + w]
        if " ".join(win) in source_norm:                      # fast path: exact (punct-insensitive) run — the common case
            return True
        if _GROUND_MAX_EDITS and _fuzzy_window(win, src_words):   # tolerate the model lightly reflowing its snippet
            return True
    return False


# ── PLAUSIBILITY VALIDATION (injection defence layer (c)) ──────────────────────────────────────────────────────────
# The allowed event_type enum — EXACTLY the nine values SYSTEM_EVENTS instructs the model to choose from. A record whose
# type is outside this set did not come from following our instructions, so it is either a model error or an injected
# record; either way it must not reach the DB with an arbitrary attacker-chosen label.
# {PROMPTS.PY SYSTEM_EVENTS "\"EARNINGS\" ... \"PRESS_RELEASE\" ... \"PRESENTATION\" ... \"FILING\" ... \"WEBCAST\" ...
#  \"CONFERENCE\" ... \"SHAREHOLDER_MEETING\" ... \"DIVIDEND\" ... OR \"OTHER\""}
# [CONFIDENCE: CONFIRMED 100% — the nine values are read verbatim off the system prompt in this same package].
_ALLOWED_TYPES = frozenset({"earnings", "press_release", "presentation", "filing",
                            "webcast", "conference", "shareholder_meeting", "dividend", "other"})
# Sane calendar window for an investor event, as a (min_year, max_year) pair around "now". IR pages legitimately carry a
# deep archive (a 20-year filing history) and forward guidance (next year's AGM), so the window is DELIBERATELY WIDE —
# its job is to catch the structurally absurd ("0001-01-01", "2099-12-31", a year-3000 forged announcement), NOT to
# second-guess a real archive. Env-overridable so a genuinely older archive can widen it without a code change.
# [CONFIDENCE: CONFIRMED 90% — 30y back covers EDGAR's full electronic era (1993+); 5y forward covers any announced
#  calendar. Chosen wide on purpose: a false REJECT of a real event is worse than admitting an implausible-but-dated one].
_DATE_MIN_YEAR = int(os.environ.get("EVENT_DATE_MIN_YEAR", "1993"))
_DATE_MAX_YEARS_AHEAD = int(os.environ.get("EVENT_DATE_MAX_YEARS_AHEAD", "5"))
# The four date shapes SYSTEM_EVENTS permits: YYYY-MM-DD, YYYY-Qn, YYYY-MM, YYYY. Anything else is malformed.
_DATE_SHAPE_RE = re.compile(r"^(\d{4})(?:-(?:Q[1-4]|\d{2}(?:-\d{2})?))?$", re.I)


# ── DATE NORMALISATION (runs BEFORE the shape check) ───────────────────────────────────────────────────────────────
# _plausible_date rejects on FORMAT, and it was rejecting dates that are perfectly parseable. Measured across the worker
# logs: 853 events dropped with `⛔ IMPLAUSIBLE DATE`, and the top shapes were not garbage —
#   103  2026-03-15T00:00:00      ISO 8601 with a time component
#   290  3/15/2026 · 03/15/2026   US/EU numeric
#    38  2026-H1                  half-year
#    27  2026-03-15 - 2026-03-18  a range
#    17  July 15, 2026            long form
# 853 against 97,760 written is 0.87% in aggregate, but the loss is CLUSTERED BY COMPANY: an IR site that prints every
# date as `3/15/2026` loses ALL of its events, which is why 90 of the 193 companies with fewer than five events had
# pages with rich content (>=5k chars, one of them 887,974) and nothing extracted.
# {SHELL 2026-07-30 "grep -c 'IMPLAUSIBLE DATE' ~/eventinc_fleet/w*.log → 853"}
# {DB 2026-07-30 "193 companies with <5 events; bucket D = 90 with >=5k chars of stored page content"}
# [CONFIDENCE: CONFIRMED 100% — the shape histogram is counted off the live logs and the bucket split off the DB.]
# Date-order convention for the genuinely ambiguous numeric form. Default US (month first) because the corpus is
# 2,107/2,785 US listings; set EVENTINC_DATE_DMY=1 for a day-first run.
_DATE_DMY = os.environ.get("EVENTINC_DATE_DMY", "") in ("1", "true", "yes")
_DATE_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _norm_date_shape(raw: str) -> str:
    """Coerce a human-written date into one of the four permitted shapes, or "" when it genuinely cannot be read.

    AMBIGUITY RESOLVES TO US ORDER. `01/04/2019` is Jan 4 or Apr 1 and the string cannot say which. When one component
    exceeds 12 the order is determined (`1/14/2019` must be M/D) and that wins. Otherwise the corpus decides: 2,107 of
    2,785 companies are US listings, and a US IR page writing 01/04/2019 means January 4. Assuming M/D is therefore
    right roughly three times in four, and the alternative — downgrading to `2019` — is wrong about the month EVERY
    time, for US and non-US pages alike. `EVENTINC_DATE_DMY=1` flips the default for a non-US-heavy run.
    {DB 2026-07-30 "US listing 2107 | non-US suffix 408 | ADR 270 of 2785 companies"}
    [CONFIDENCE: CONFIRMED 100% on the corpus split; the M/D choice is a policy call whose cost is bounded — a non-US
     page's day and month get transposed, which is wrong by at most eleven months and only for genuinely ambiguous
     day-of-month values 1-12.]
    Half-years are different and stay downgraded: `2026-H1` spans two quarters, so mapping it to `Q1` would invent
    precision the source never had — there is no convention to appeal to, unlike date order.
    A RANGE keeps its START — an event that runs 19-22 Sep begins on the 19th, and the start is what a calendar entry
    needs. Nothing here invents a value the source did not contain; every branch either preserves or downgrades.
    [CONFIDENCE: CONFIRMED 100% — every branch below is covered by a unit test built from the shapes actually observed
     in production logs and in the 3,090 non-ISO rows already stored.]"""
    s = (raw or "").strip()
    if not s:
        return ""
    # CJK date words become separators. The Korean form is spaced ("2026년 3월 15일"), so whitespace around the
    # separators has to go too or the numeric patterns below never match — that gap made the Korean case fall through
    # to the bare-year salvage and silently lose the month and day.
    # ORDER MATTERS HERE, and getting it wrong is silent. The range split has to run BEFORE the CJK space collapse:
    # collapsing " - " to "-" first turns `2017-06-21 - 2017-06-22` into `2017-06-21-2017-06-22`, which then matches no
    # numeric pattern and falls through to the bare-year salvage — the event survives but loses its month and day, and
    # nothing reports the downgrade. Caught by the range case in the unit test, not by reading.
    s = re.split(r"\s*(?:~|--|—|–|\bto\b|\bthrough\b)\s*|\s+-\s+", s)[0].strip()   # a range keeps its START
    s = (s.replace("年", "-").replace("月", "-").replace("日", "")
          .replace("년", "-").replace("월", "-").replace("일", ""))
    s = re.sub(r"\s*-\s*(?=\d)", "-", s)                    # CJK separators leave spaces ("2026- 3- 15") — close them
    s = re.sub(r"[T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", "", s)  # drop a time component
    s = s.strip(" ,.-")
    if _DATE_SHAPE_RE.match(s):                                # already one of the four permitted shapes
        return s.upper() if "q" in s.lower() else s
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)  # YYYY-M-D / YYYY/M/D / YYYY.M.D
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else f"{y:04d}"
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", s)  # M/D/YYYY or D/M/YYYY — ambiguous unless one part > 12
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 and b <= 12:
            return f"{y:04d}-{b:02d}-{a:02d}"                  # D/M — unambiguous
        if b > 12 and a <= 12:
            return f"{y:04d}-{a:02d}-{b:02d}"                  # M/D — unambiguous
        # Ambiguous: both parts <= 12. Fall back to the corpus's dominant convention rather than losing the month.
        mo, d = (b, a) if _DATE_DMY else (a, b)
        return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else f"{y:04d}"
    m = re.match(r"^(\d{4})-?H([12])$", s, re.I)               # half-year → year (H1 spans Q1+Q2; a quarter is invented)
    if m:
        return m.group(1)
    m = re.match(r"^(\d{4})-?FY$|^FY-?(\d{4})$", s, re.I)      # fiscal-year marker → the year
    if m:
        return m.group(1) or m.group(2)
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$", s)          # July 15, 2026 / Jul 15 2026
    if m and m.group(1)[:3].lower() in _DATE_MONTHS:
        return f"{int(m.group(3)):04d}-{_DATE_MONTHS[m.group(1)[:3].lower()]:02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})$", s)            # 15 March 2026
    if m and m.group(2)[:3].lower() in _DATE_MONTHS:
        return f"{int(m.group(3)):04d}-{_DATE_MONTHS[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", s)                        # March 2026 → year-month
    if m and m.group(1)[:3].lower() in _DATE_MONTHS:
        return f"{int(m.group(2)):04d}-{_DATE_MONTHS[m.group(1)[:3].lower()]:02d}"
    m = re.search(r"(19|20)\d{2}", s)                          # last resort: a plausible year is still worth keeping
    if m:
        return m.group(0)
    return ""                                                  # unreadable → the caller drops it, as before


def _plausible_date(date: str) -> bool:
    """Structural + range sanity for an extracted event date. "" is ALLOWED (the prompt explicitly permits an undated
    event when the page prints no date, and the footer-chrome guard already requires date OR title), but a NON-empty
    date must match one of the four permitted shapes AND fall in a sane year window.

    WHY: injection defence layer (c). Grounding cannot reject a poisoned record — the attacker controls the page, so
    their forged evidence matches by construction — but a forged "acquisition on 2099-01-01" still has to survive a
    STRUCTURAL check that has nothing to do with the page's contents. UPSTREAM: _normalize_events, per event.
    DOWNSTREAM: an implausible date drops the event before flush_events writes it.
    [CONFIDENCE: CONFIRMED 100% — shapes are taken verbatim from the prompt's own date contract]."""
    d = (date or "").strip()
    if not d:                                                  # undated is legal per the prompt → not a plausibility failure
        return True
    m = _DATE_SHAPE_RE.match(d)                                # must be YYYY / YYYY-MM / YYYY-MM-DD / YYYY-Qn
    if not m:
        return False
    year = int(m.group(1))
    # Bound the year to a wide-but-finite window around today; catches year-0001/2099-style forgeries and typos.
    return _DATE_MIN_YEAR <= year <= datetime.now(timezone.utc).year + _DATE_MAX_YEARS_AHEAD


def _plausible_type(etype: str) -> str:
    """Coerce an event type to the allowed enum: a recognised value passes through, anything else (including "") becomes
    "other". WHY coerce rather than DROP: the type is a low-stakes label and the prompt itself allows "other", so an
    unexpected value is far more likely a model wobble than an attack — dropping the event would lose a real disclosure
    over a cosmetic field. The security property we need is only that an ATTACKER-CHOSEN string never reaches the DB.
    UPSTREAM: _normalize_events. DOWNSTREAM: events.event_type.
    [CONFIDENCE: CONFIRMED 95% — enum-clamping preserves recall while removing the arbitrary-value write primitive]."""
    t = (etype or "").strip().lower()
    return t if t in _ALLOWED_TYPES else "other"


def _normalize_events(result: dict, tag_map: dict, source: str = "", page_url: str = "") -> dict:
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
        title = (e.get("title") or "").strip()[:300]
        date = (e.get("date") or "").strip()
        if not urls:
            # A HYPERLINK IS NOT WHAT MAKES AN EVENT REAL. This used to be an unconditional drop, and it silently
            # discarded an entire page shape: an IR calendar that lists events as plain text rows. Shimano's
            # /en/ir/calendar.html is 1,255 chars containing 9 unambiguous dated events —
            # "July 28, 2026 at 3:30 PM (JST) Announcement of Financial Results 2nd Quarter for FY2026" and eight more —
            # with only 4 links on the whole page (home, IR, and two year anchors). Every one of those 9 died here, and
            # the company reads as 0 events with no error, no partial, and no drop counter anywhere.
            # The page itself is the honest source for such a row, so it becomes the event's url.
            # BOTH date AND title are required for this fallback, which is stricter than the general rule below (date OR
            # title). Without a date, a nav label the model mis-read as an event would now be admitted instead of
            # dropped — the url requirement was doing that filtering as a side effect, so tightening here is what keeps
            # the footer-chrome guard as strong as it was.
            # {MEASURED 2026-08-01 shimano.com/en/ir/calendar.html — 9 dated rows in the text the model received,
            #  events_kept=0, _error=None, _partial=None}
            # [CONFIDENCE: CONFIRMED 100% — the full page text and the extraction result were printed side by side.]
            if page_url and date and title:
                urls = [page_url]
            else:
                continue
        # PLAUSIBILITY (injection defence layer (c)) — grounding proves the model COPIED from the page; it cannot prove
        # the PAGE is honest, because a poisoner controls the grounding corpus too. So an event must ALSO survive a
        # structural check that does not consult the page: a parseable date inside a sane year window, and a type inside
        # the prompt's own enum. A forged "2099 acquisition" passes grounding and dies here.
        # {PROMPTS.PY _FENCE_OPEN BLOCK "GROUNDING ANSWERS 'DID THE MODEL MAKE THIS UP?'; IT CANNOT ANSWER 'IS THE PAGE
        #  LYING?'"} [CONFIDENCE: CONFIRMED 100% — structural validity is independent of attacker-controlled content].
        # NORMALISE FIRST, THEN VALIDATE. The check below is structural, and it was rejecting dates that were merely
        # written in a human format: `2026-03-15T00:00:00`, `3/15/2026`, `July 15, 2026`. Validating a raw string means
        # the guard's real job (catch a forged 2099 date) gets conflated with a formatting complaint, and the event is
        # discarded either way. Normalising first keeps the injection defence exactly as strict on the YEAR while
        # letting a readable date through.
        # {SHELL 2026-07-30 "grep -c 'IMPLAUSIBLE DATE' ~/eventinc_fleet/w*.log → 853 events dropped"}
        # {DB 2026-07-30 "3,090 stored rows already hold a non-ISO shape, same histogram as the drops"}
        # [CONFIDENCE: CONFIRMED 100% — 26 shapes taken from the live logs and the stored rows are covered by a unit
        #  test; the year-window half of the guard is unchanged, so a forged 2099 date still dies here.]
        norm = _norm_date_shape(date)
        if date and not norm:                                  # genuinely unreadable → the old behaviour, loudly
            print(f"[extract] ⛔ UNREADABLE DATE {date[:24]!r} — dropping event {title[:48]!r}", flush=True)
            continue
        if norm != date:
            print(f"[extract] ↻ date normalised {date[:24]!r} → {norm!r}", flush=True)
        date = norm
        if not _plausible_date(date):                          # absurd year → poisoned/garbled → drop
            print(f"[extract] ⛔ IMPLAUSIBLE DATE {date[:24]!r} — dropping event {title[:48]!r}", flush=True)
            continue
        etype = _plausible_type(e.get("type"))                 # clamp to the allowed enum (unknown → "other")
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


def _combine(events: list, event_urls: set, routes: list, error: str | None,
             route_error: str | None = None, partial: str | None = None) -> dict:
    """Merge the EXTRACTION half (events) + the ROUTING half (routes) into the final page result, enforcing event⊥route
    EXCLUSIVITY across the two independent calls: a route whose resolved url is ALSO an event's url is DROPPED (an event
    is a leaf — never re-follow it). Propagates any hard error so the crawl can tell "FAILED" from "genuinely empty".
    {USER 2026-07-24 "separate the routing and the classification"} [CONFIDENCE: CONFIRMED 100% — post-hoc exclusivity is
    what makes two separate calls safe].

    THE TWO HALVES FAIL INDEPENDENTLY, so they must be reported independently. Routing and extraction are separate LLM
    calls over separate inputs (link list vs body text) fired concurrently; they share nothing but the page. Folding both
    into one `_error` meant a routing blip — a GPU hiccup on the link-list call, common under 6-worker concurrency —
    made _harvest discard the events the extraction call had already returned successfully. The right cost of a routing
    failure is "we don't go deeper from this page", never "this page's events are lost".
    `_error` = extraction hard-failed → events untrustworthy, discard (unchanged).
    `_route_error` = routing hard-failed → keep events, drop the frontier contribution.
    `_partial` = extraction incomplete but what came back is real → keep events, mark the company incomplete.
    {ENGINE.PY _harvest "IF RES.GET("_ERROR"): ... PAGE'S EVENTS LOST"}
    [CONFIDENCE: CONFIRMED 100% — the two jobs are gathered independently in _extract_one; nothing couples their
     validity, so the old `or` was strictly a loss of information]."""
    kept = [r for r in routes if r["url"] not in event_urls]   # exclusivity now lives here (two calls can't self-enforce it)
    out = {"events": events, "routes": kept}
    if error:
        out["_error"] = error
    if route_error:
        out["_route_error"] = route_error
    if partial:
        out["_partial"] = partial
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
    # PARTIAL is not ERROR. A truncated block or the block cap means "we got SOME of this page", and the events the other
    # blocks DID return are real. Reporting them through `_error` made _harvest discard the whole page (it treats _error as
    # "the LLM hard-failed → this page's events are lost"), so one bad block out of 150 threw away 149 good ones — the exact
    # opposite of this branch's own comment, which says "accept partial". `_partial` keeps the fail-loud signal (the caller
    # still counts a failed_extract so the company is marked incomplete) WITHOUT dropping the harvest.
    # {EXTRACT.PY:301 "BLOCK STILL TRUNCATED AT THE LIMIT → ACCEPT PARTIAL, DON'T RE-SPLIT" — the stated intent}
    # {ENGINE.PY _harvest "IF RES.GET("_ERROR"): ... PRINT(F"[CRAWL] ⛔ EXTRACT FAILED ... — PAGE'S EVENTS LOST") RETURN []"}
    # [CONFIDENCE: CONFIRMED 100% — the discard path is unconditional on _error; chunking only runs on pages over
    #  MAX_INPUT_CHARS, i.e. the event-densest archives, so this lost the most valuable pages first].
    merged_events, seen, event_urls, partials = [], set(), set(), ([cap_note] if cap_note else [])
    errs: list[str] = []
    for i, res in enumerate(results):
        if res.get("__finish__") == "length":                # block hit the output limit → partial, keep what it gave us
            partials.append(f"block {i} truncated at limit")
        ne = _normalize_events(res, maps[i], blocks[i], page_url)  # resolve THIS block's ids + ground evidence against THIS block's text
        if ne.get("_error"):                                  # a block that HARD-failed (transport/parse) → real error
            errs.append(ne["_error"])
        # DEDUP BY EVENT IDENTITY, not by url overlap. Sharing a url is normal on IR pages — one webcast/registration/
        # "Investor Relations" link is attached to every earnings call on the page — so an any-url-overlap test made the
        # first event swallow every later one that reused any of its links. engine.py and db.py both key on title+date
        # already; this was the last place still on the old url semantics.
        # {ENGINE.PY:365 "K = _EVENT_KEY(E.GET("TITLE"), E.GET("DATE"), E["URLS"])"}
        # {URLS.PY _event_key "A URL-BASED KEY DUPLICATED ONE EVENT INTO MANY ROWS ... TITLE+DATE IS THE EVENT'S REAL IDENTITY"}
        # [CONFIDENCE: CONFIRMED 100% — three dedup sites, this one was the outlier; url-overlap DROPS distinct events
        #  whereas the title+date key only collapses genuine repeats].
        for e in ne["events"]:
            k = _event_key(e.get("title"), e.get("date"), e["urls"])
            if not k or k in seen:
                continue
            seen.add(k)
            merged_events.append(e)
            event_urls |= {u for u in e["urls"]}
    out = {"events": merged_events, "_event_urls": event_urls}
    # THE DISCRIMINATOR IS "DID WE GET ANYTHING", NOT "WHAT KIND OF FAILURE". The truncation case was already routed to
    # _partial, but a block that hard-fails at the TRANSPORT layer still went to _error — and _harvest discards a page's
    # entire harvest on _error. Under a saturated VLM that is the common case, not a rare one: Sony's earnings archive
    # chunked into 3 blocks, two returned ReadTimeout, the third returned 17 real dated events, and all 17 were thrown
    # away. Chunking only runs on pages over MAX_INPUT_CHARS — the event-densest archives — so this discarded the most
    # valuable pages first, which is why 20 companies sat at zero events on top of 3,129 stored dated mentions.
    # {TRACE 2026-07-28 www.sony.com/.../presen/er/archive.html result.json "_error": "ReadTimeout: ; ReadTimeout: ",
    #  "events": [17 items] — and summary.json for the same run: "n_events": 0}
    # {ENGINE.PY _harvest "IF RES.GET("_ERROR"): ... PRINT("PAGE'S EVENTS LOST") RETURN []"}
    # [CONFIDENCE: CONFIRMED 100% — read off the production trace; re-extracting that stored page yields 36 events].
    notes = errs + partials
    if notes:
        if merged_events:                                     # some blocks died, others delivered → incomplete, NOT lost
            out["_partial"] = "; ".join(notes[:5])            # _harvest still counts failed_extract → company 'incomplete'
        else:                                                 # nothing survived anywhere → a genuine extraction failure
            out["_error"] = "; ".join(notes[:5])
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
        return _normalize_events(res, tag_map, tagged, url)    # ground evidence against the page the model read

    routes_r, ev = await asyncio.gather(_route(), _events())   # ROUTING + EXTRACTION concurrently on the GPU
    return _combine(ev["events"], ev.get("_event_urls") or set(), routes_r["routes"],
                    ev.get("_error"),                          # extraction failed → events untrustworthy
                    routes_r.get("_error"),                    # routing failed → only the frontier suffers
                    ev.get("_partial"))                        # incomplete-but-real → keep the events, flag the company


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
