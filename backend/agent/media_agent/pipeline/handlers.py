"""media_agent.handlers — one handler per url KIND. Each opens/parses its url with the right tool and FILLS the
chart's matching slot; only html yields new urls (the close-loop engine). dispatch() routes a (url,kind) to its handler.

用一句话讲完: router 判 kind → dispatch 把 url 交给对应 handler → handle_html 用 watercrawl 渲染 + qwen-VL 出
{basic_info blocks, 内联 transcript, metadata};handle_office 调 tools/officeall(Docling);handle_audio 调
tools/audio_extract(faster-whisper)。**office/audio 复用你已建的 tools 包,不重写。**

DESIGN CORE — FAIL LOUDLY, ALWAYS A FALLBACK, NEVER SILENT QUALITY LOSS {USER 2026-07-23 "fail loudly is the core all
of the things should have fallback we dont want quality issue"} [CONFIDENCE: CONFIRMED 100% — direct user directive]:
  • html output truncated (finish_reason=length) → CHUNK-and-retry, loudly logged; still truncating at max depth → REPORT.
  • html input over the char cap → CHUNK full text UP-FRONT (never silently trim the tail's content), loudly logged.
  • a relative/unresolvable url → urljoin against the page base; still non-http → drop LOUDLY, don't poison the ledger.
  • an extension-less download url mis-typed as html → HEAD content-type refine; HEAD fails → keep html but SAY SO.
  • a video PLATFORM (youtube) the audio tool can't fetch → mark `skipped:needs-ytdlp`, not a silent `failed`.
"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlsplit, urlunsplit

from providers.qwen_llm import QwenClient            # VLM transport (reused, unchanged)

# html render — via the bounded-retry wrapper, NOT a bare watercrawl.render_shot. A single-shot render turns a transient
# concurrency timeout into a permanent failure (see render_retry's module docstring for the measured event-side evidence).
from .render_retry import render_with_retry

from ..extract import prompts, router
# The page-level misroute check reuses the SAME sniffer the block guard uses, so "is this binary" has exactly one
# definition in the codebase and the two checks can never disagree about the same bytes.
from ..extract.chart import _looks_binary
# DETERMINISTIC body + shared input-overflow guard — the production worker path (dispatch→handle_html) gets the SAME
# trafilatura+pandas body extraction as enrich.enrich_page, so an earnings page's financial tables never overflow the VLM.
# suppress_transcript_blocks is no longer imported: with transcript_segments gone from SCHEMA_ROUTE the
# suppressor had nothing to suppress, and its own fail-safe branch (empty segments → keep every block)
# is exactly the behaviour we now want unconditionally — a transcript stays in the body.
from ..extract.extract_html import extract_html, fit_input, ROUTE_MAX_TOKENS

# A webcast platform url that is a REGISTRATION / signup form, not a playable stream (wsw.com /register.aspx ×9, Zoom
# /webinar/register/…). yt-dlp has nothing to fetch here — it's a gate — so we record it and skip WITHOUT a doomed
# download attempt. {AUDIT 2026-07-23} [CONFIDENCE: CONFIRMED 100% — workflow-verified against real webcast urls].
_REGISTER_GATE_RE = re.compile(r'/register\.aspx|/webinar/register/|/reg(?:ister)?(?:/|\.|$)', re.I)

# Truncation-fallback knobs (mirror event_agent.extract so both agents chunk the same way). A screenshot page sends
# less text (image tokens are scarce); text-only chunks send more. {EXTRACT.PY _VISION_TEXT_CHARS/_CHUNK_* knobs}.
_MAX_INPUT_CHARS = int(os.environ.get("MEDIA_MAX_INPUT_CHARS", "48000"))     # text-only page_text cap
_VISION_TEXT_CHARS = int(os.environ.get("MEDIA_VISION_TEXT_CHARS", "24000"))  # WITH a screenshot, smaller text cap
_CHUNK_TARGET_CHARS = int(os.environ.get("MEDIA_CHUNK_TARGET_CHARS", "18000"))  # aim each chunk's input ≈ this
_CHUNK_OVERLAP = int(os.environ.get("MEDIA_CHUNK_OVERLAP", "1500"))          # backward overlap so no block splits
_CHUNK_MAX_DEPTH = int(os.environ.get("MEDIA_CHUNK_MAX_DEPTH", "3"))         # re-split a still-truncating chunk, ≤3 deep
_HEAD_TIMEOUT_S = int(os.environ.get("MEDIA_HEAD_TIMEOUT_S", "10"))          # content-type sniff timeout

# ── WHICH EXTRACTOR LANES ARE ON ────────────────────────────────────────────────────────────────────────────────────
# 用一句话讲完: 只有列在这里的 url kind 会被真正处理,其余记 `skipped:kind-disabled` —— 不是 done(那等于谎称读过),
# 也不是 failed(那等于把我们的配置问题赖给来源)。默认只开 html,因为另外两条车道现在是单进程瓶颈。
#
# WHY html-only by default: the three lanes have wildly different capacity and two of them stall the queue for the
# third. Measured on the live fleet with all lanes enabled:
#   {POD /health 2026-08-06 "WHISPER CONCURRENCY=1 INFLIGHT=12"}   — 11 audio urls queued behind ONE slot
#   {POD /health 2026-08-06 "DOCLING CONCURRENCY=4 INFLIGHT=0"} with {ps "225% (2.2 CORES)"} on {nproc "96"}
#   {POD /proc/loadavg "6.49"} — 93% of a 96-core machine idle behind those two limits
# The per-event cost of leaving them on:
#   {RUN LOG 2026-08-06 "EV_016_HTML_ONLY 21.6S" · "EV_018_HTML_ONLY 32.9S"}  vs
#   {RUN LOG 2026-08-06 "EV_007_AUDIO 557.7S" · "EV_006_AUDIO 1039.5S"}       — a 26-48x spread, nearly all queueing.
# [CONFIDENCE: CONFIRMED 100% — every figure read off the running fleet or its own run log.]
#
# Turning a lane back on is an env change, not a code change: MEDIA_KINDS="html,pdf,pptx,docx,xlsx" once docling runs
# multi-process, plus "audio,video" once whisper has VRAM or CPU headroom. Until then, enabling them buys nothing and
# makes the html lane wait behind them.
# {USER 2026-08-06 "add infra so we can only extract basic info right now, no docling no whipser just info"}
_ENABLED_KINDS = frozenset(
    k.strip() for k in os.environ.get("MEDIA_KINDS", "html").split(",") if k.strip())

# Cap on documents adopted from ONE page. A press-release page carries its own file (usually 1, occasionally the same
# content as pdf+doc+xls); an archive page carries dozens that are not this event's. The model is asked to discriminate,
# and this is the backstop for when it does not — measured shape: pages with a document had a median of 1, while the
# archive-shaped ones ran to 8+ candidates of which none belonged.
# {psql/probe 2026-08-06 — single_doc n=12 (1 doc each) vs many_docs n=10 (5..8 docs, site furniture)}
# [CONFIDENCE: CONFIRMED 100% — counted over the 47-page labelled sample.]
_MAX_ADOPTED_DOCS = int(os.environ.get("MEDIA_MAX_ADOPTED_DOCS", "8"))

# An SEC filing DOCUMENT, recognised by the EDGAR CDN's CIK path or by a filings-page url. Kept beside the claim-side
# SEC_URL_EXCLUDE rather than merged with it: that one matches the PAGE an event came from, this one matches a FILE a
# page links to, and the two see different url shapes (d18rn0p25nwr6d.cloudfront.net/CIK-0001318220/<uuid>.pdf has no
# "sec-filings" segment at all).
# {psql/REST 2026-08-06 — ledger rows like "d18rn0p25nwr6d.cloudfront.net/CIK-0001318220/e0574656-….pdf"}
# [CONFIDENCE: CONFIRMED 100% — url shape read from event_media_urls on the live database.]
# `sec\.irpass\.cc` is matched by SUBDOMAIN, not by domain, and the distinction is load-bearing. irpass.cc is B2i
# Technologies' content CDN for IR websites, and it serves two different things from the same S3 bucket family:
#   sec.irpass.cc        → SEC filings, named by accession number — {psql "…/2476/0001104659-26-070536.htm"}
#   b2icontent.irpass.cc → ordinary IR material — {psql "…/653/200672.pdf  CMC Q3 FISCAL 2026 EARNINGS CONFERENCE CALL"
#                          · "…/2475/rl162995.pdf  BBVA ARGENTINA ANNOUNCES FOURTH QUARTER"}
# Blocking the domain would take the earnings decks and press releases with the filings; blocking the subdomain takes
# only what EDGAR already serves better.
# [CONFIDENCE: CONFIRMED 100% — both url shapes and their event titles read from the live database.]
# CLICK-TRACKING REDIRECTS. Same pattern as event_agent/crawl/extract.py's — the crawler now refuses to RECORD these,
# this gate stops stage-2 FETCHING the 3,945 already in the database {psql 2026-08-07 "追踪url总数 | 3945"}.
#
# 用一句话讲完: 追踪端点按设计不指向内容 —— 它记一次点击,然后把你送到某个泛化的地方(通常是公司首页),
# 所以渲染它拿回来的是站点导航,而那份导航同时把标题污染成 url 的最后一段。
#
# Measured end to end on the case that exposed it:
# {CURL 2026-08-07 "https://www.globenewswire.com/Tracker?data=nppKI9POHN_…" → "HTTP/2 302 / location:
#  https://investors.csx.com/"} — the company HOMEPAGE, not the press release it was linked from.
# {psql event_documents.md FOR THAT EVENT → "SKIP TO MAIN CONTENT / OVERVIEW / FINANCIALS / QUARTERLY RESULTS /
#  ANNUAL REPORTS / SEC FILINGS / METRICS / …" — 2,124 chars, zero prose}
# {psql "TITLE='TRACKER' → 86 EVENTS", every one with meta_fixed IS NULL — the VLM was asked to repair the title
#  against that navigation menu and had nothing to replace it with.}
# [CONFIDENCE: CONFIRMED 100% — redirect followed live; stored body and both counts read off production.]
#
# `utm_` is deliberately absent: it is a campaign tag bolted onto otherwise-real content urls, so matching it would
# discard the document along with the tag.
_TRACKER_URL_RE = re.compile(
    r'/Tracker\?'                                     # GlobeNewswire — the endpoint measured above
    r'|/track(er)?/[^/]*\?'                           # generic /track/<id>? and /tracker/<id>?
    r'|//(link|click|ct|email|e|mailer)\.[^/]+/'      # tracking subdomains used by mail / PR distributors
    r'|/(redirect|goto|linkclick)\.(aspx|php|jsp)'    # classic redirector endpoints
    r'|doubleclick\.net|/pagead/',                    # ad-network click counters
    re.I)

_SEC_DOC_RE = re.compile(r"/CIK-\d|/sec-filings/|/edgar/|sec\.gov/|sec\.irpass\.cc/", re.I)


def _canon_link(u: str) -> str:
    """Compare-key for "is this url on the page". Only case+fragment+trailing-slash are normalised — query strings are
    KEPT, because IR download endpoints routinely carry the document id there (?FilingId=…, ?docid=…) and stripping it
    would let a proposal for one filing match a link to another."""
    try:
        p = urlsplit(u.strip())
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))
    except Exception:                                    # noqa: BLE001 — unparseable → compare raw, never crash the gate
        return (u or "").strip()


# A `file-magic:` reason from _looks_binary → the kind that header identifies. Only headers that name ONE kind are
# listed: `PK\x03\x04` is the zip container shared by docx/xlsx/pptx and `\xd0\xcf\x11\xe0` the OLE2 container
# shared by their legacy forms, so neither can be resolved from the header alone and both are deliberately absent —
# an unresolvable header falls through to the honest "unidentifiable" outcome rather than a coin flip.
# {CHART.PY "_BINARY_MAGIC = (B\"%PDF-\", B\"PK\\X03\\X04\\", B\"\\XD0\\XCF\\X11\\XE0\", …)"}
# [CONFIDENCE: CONFIRMED 100% — the reason strings are produced by _looks_binary in that same module.]
_MAGIC_KIND = {
    "%PDF": router.KIND_PDF,
    "ID3": router.KIND_AUDIO, "OggS": router.KIND_AUDIO, "fLaC": router.KIND_AUDIO,
}


def _kind_from_magic(reason: str) -> str:
    """Map a `_looks_binary` reason to a url kind, or "" when the bytes do not name one kind.

    The reason format is `file-magic:b'%PDF'` (header found) or `undecodable:54%` (no header, just garbage). Only the
    first form can identify anything; the second means we know it is not text and nothing more.
    """
    if not reason.startswith("file-magic:"):
        return ""
    for needle, kind in _MAGIC_KIND.items():
        if needle in reason:
            return kind
    return ""


# ── OFFICE (pdf / pptx / docx / xlsx) → tools/officeall (Docling) ──────────────────────────────────────────────
# 送给元数据那一步的开头长度。标题和日期都在文件最前面,再多送只是白付 prefill —— 而 prefill 是 GPU 的全部成本
# {POD 2026-08-08 实测 "每请求 prompt 3,601 token → 生成 155 token = 23:1,GPU 时间几乎全在 prefill"}。
# 2000 字符 ≈ 500 token,够覆盖封面页/抬头/第一段,比一次完整页面调用便宜一个数量级。
_DOC_META_HEAD_CHARS = int(os.environ.get("MEDIA_DOC_META_CHARS", "2000"))
# 这些后缀之外的 url 就是「有网页可读」——那时 SYSTEM_ROUTE 的元数据任务已经在做这件事,不必再花一次调用。
_DOC_ONLY_RE = re.compile(r"\.(pdf|xlsx?|docx?|pptx?|mp[34]|wav|m4a)(\?|#|$)", re.I)


async def _doc_metadata(url: str, text: str, chart, client) -> None:
    """从文档正文开头取真标题/真日期,写回 chart —— 只对「没有网页可读」的事件做。

    用一句话讲完: 一个事件如果只挂着一个 pdf 链接,它的 title/date 是 stage-1 从列表页抄来的**链接文字**,
    而这条管线原本修复元数据的唯一途径是读 html 详情页 —— 这类事件根本没有详情页,于是永远修不到。

    {psql 2026-08-09 "事件总数 235166 | 只有文档无html 56198"} = 24% 的事件走的是这条盲路。
    {psql 2026-08-09 抽样 "[Half-Year 2021 PresentationPDF] | 2021-Half-Year",
     "[Presentation Q4 2007] | 2007-Q4", "[Interim Report Q1 2015] | 2015-Q1"} —— 方括号、"PresentationPDF"
    粘连、日期粒度是从标题里猜出来的。它们都**非空**,所以任何「为空才修」的判据都放过了它们。
    [CONFIDENCE: CONFIRMED 100% — 计数与样本均取自生产库。]

    只在 event 的 url 全是文档时才调用: 有网页的事件由 SYSTEM_ROUTE 的元数据任务负责,重复调一次是白花
    最紧张的那份算力。写回走 chart.confirm_metadata,和 html 路径同一个出口 —— 非空即替换,空即保留。
    """
    if not client or not text.strip():
        return
    body = (f"<<<UNTRUSTED_PAGE_CONTENT>>>\n{text[:_DOC_META_HEAD_CHARS]}\n"
            f"<<<END_UNTRUSTED_PAGE_CONTENT>>>")
    try:
        out = await client.send_one(prompts.SYSTEM_DOC_META, body, image_b64=None,
                                    guided_json=prompts.SCHEMA_DOC_META, max_tokens=256)
    except Exception as e:                                    # noqa: BLE001 — 元数据是增量收益,失败不该毁掉这份文档
        print(f"[media] ⚠️ doc-meta failed {url[:60]} — {type(e).__name__}: {str(e)[:60]}", flush=True)
        return
    t, d, ty = (out or {}).get("title", ""), (out or {}).get("date", ""), (out or {}).get("type", "")
    if t or d or ty:
        chart.confirm_metadata(str(t or ""), str(d or ""), str(ty or ""))
        print(f"[media] 📄 doc-meta {url[-52:]} → title={str(t)[:44]!r} date={d!r}", flush=True)


async def handle_office(url: str, chart, proxy: str | None = None, client=None,
                        event_urls: list | None = None) -> None:
    """Parse an office document via tools/officeall and append it to chart.files[kind]. {OFFICEALL extract(url)->DocResult}."""
    from tools.officeall import extract as office_extract     # lazy: docling is a heavy GPU dep, only load on use
    res = await asyncio.to_thread(office_extract, url, proxy)  # blocking Docling call off the event loop
    if not res.ok:                                            # unreachable / not-a-doc / unparseable → say why, loudly
        chart.set_status(url, f"failed:{res.error or 'no-content'}")
        print(f"[media] ⛔ office parse failed {url[:70]} — {res.error or 'no-content'}", flush=True)
        return
    kind = res.format or router.classify(url)                 # trust the magic-confirmed format from the fetch
    chart.append_file(kind, url, markdown=res.text, tables=res.tables, n_pages=res.n_pages)
    chart.set_status(url, "done")
    # 这个事件有没有网页可读?有 → 元数据归 SYSTEM_ROUTE 管,这里不重复花调用。
    urls = event_urls or []
    if urls and not any(u for u in urls if isinstance(u, str) and not _DOC_ONLY_RE.search(u)):
        await _doc_metadata(url, res.text or "", chart, client)
    return


# ── AUDIO (mp3 / wav / direct video file) → tools/audio_extract (faster-whisper) ──────────────────────────────
async def handle_audio(url: str, chart, proxy: str | None = None) -> None:
    """Transcribe an audio url via tools/audio_extract and append speaker-tagged segments to chart.transcript +
    the audio artifact to chart.audio."""
    from tools.audio_extract import extract as audio_extract  # lazy: faster-whisper + torch are heavy GPU deps
    res = await asyncio.to_thread(audio_extract, url, proxy)   # blocking whisper transcription off the event loop
    if not res.ok:
        chart.set_status(url, f"failed:{res.error or 'transcribe-failed'}")
        print(f"[media] ⛔ transcribe failed {url[:70]} — {res.error or 'transcribe-failed'}", flush=True)
        return
    # audio_extract.segments are {start,end,text} with NO speaker — diarization is a documented TODO in that tool, and
    # the user wants SPEAKER_00 for now. {AUDIO_EXTRACT __init__ "留待后续... pyannote 说话人 diarization"} {USER
    # 2026-07-23 "first use speaker_00 for now"} [CONFIDENCE: CONFIRMED 100% — tool docstring + direct user instruction].
    segs = [{"speaker": "SPEAKER_00", "start": s.get("start"), "end": s.get("end"), "text": s.get("text")}
            for s in (res.segments or [])]
    chart.append_transcript(segs, source_url=url)
    chart.append_audio(url, duration_s=res.duration)
    chart.set_status(url, "done")
    return


# ── VIDEO (youtube / webcast / .mp4) → three tiers, each measured ─────────────────────────────────────────────
# A real earnings call is megabytes; a player's UI chrome is kilobytes. This threshold is the whole discriminator,
# and it is deliberately a SIZE check rather than a url blocklist: the first version of _pick_stream preferred any
# .mp3 and duly picked YouTube's own "search failed" beep —
# {LOG 2026-08-05 "🎯 captured 4 media urls on youtube.com/watch?v=jAkSyqggEUY →
#  https://www.youtube.com/s/search/audio/failure.mp3"} — and a blocklist would then have needed an entry for the
# next platform's differently-named beep, forever. 200 KB is ~12 seconds of speech at a low bitrate: below any real
# call, above every ui asset.
# [CONFIDENCE: CONFIRMED — the url and its selection are both in the live log].
_MIN_STREAM_BYTES = int(os.environ.get("MEDIA_MIN_STREAM_BYTES", "200000"))


def _rank_streams(media: list[str]) -> list[str]:
    """Every candidate the browser requested, best first — audio-only, then manifest, then progressive video.

    A LIST, not one pick: the best-looking url can turn out to be junk, and the caller needs somewhere to go next.
    Segment urls (.ts/.m4s) are dropped outright — they are pieces of a stream, not a stream."""
    out: list[str] = []
    for pat in (r'\.(mp3|m4a|wav|aac)(\?|#|$)', r'\.(m3u8|mpd)(\?|#|$)', r'\.(mp4|webm|mov)(\?|#|$)'):
        for u in media:
            if u in out or re.search(r'\.(ts|m4s)(\?|#|$)', u, re.I):
                continue
            if re.search(pat, u, re.I):
                out.append(u)
    # Anything unmatched still beats giving up — EXCEPT segments, which stay excluded here too. They were skipped by
    # the pattern loop above and a catch-all that added them back would undo that: a .ts is one slice of a stream, so
    # handing it to yt-dlp yields a few seconds of audio that looks like a successful transcription.
    for u in media:
        if u not in out and not re.search(r'\.(ts|m4s)(\?|#|$)', u, re.I):
            out.append(u)
    return out


def _stream_too_small(url: str) -> int:
    """Content-Length in bytes when the server gives one and it is BELOW the floor, else 0 (meaning: proceed).

    A manifest is exempt — an .m3u8 is a few hundred bytes of text that points at hours of audio, so size says
    nothing about it. Unknown length also proceeds: refusing what we cannot measure would drop every chunked
    response, which is most real streams."""
    if re.search(r'\.(m3u8|mpd)(\?|#|$)', url, re.I):
        return 0
    try:
        from curl_cffi import requests as creq
        r = creq.head(url, impersonate="chrome", timeout=_HEAD_TIMEOUT_S, allow_redirects=True)
        n = int(r.headers.get("content-length") or 0)
    except Exception:                                          # noqa: BLE001 — HEAD refused / no curl_cffi → proceed
        return 0
    return n if 0 < n < _MIN_STREAM_BYTES else 0


def _pick_stream(media: list[str]) -> str:
    """The best candidate that is actually plausible as a recording. Kept as a single-value entry point so existing
    callers are unchanged; the size check is what makes it more than a pattern match."""
    for u in _rank_streams(media):
        small = _stream_too_small(u)
        if small:
            print(f"[media] ⏭ skipping {u[:70]} — {small}B, below {_MIN_STREAM_BYTES}B (ui asset, not a recording)",
                  flush=True)
            continue
        return u
    return ""


async def _transcribe_bytes(url: str, data: bytes, chart, duration: float = 0.0) -> bool:
    """Audio bytes → whisper → chart. Shared by the yt-dlp and capture tiers so both land segments identically."""
    from tools.audio_extract import extract_bytes              # lazy: faster-whisper + torch are heavy deps
    res = await asyncio.to_thread(extract_bytes, data)
    if not res.ok:
        chart.set_status(url, f"failed:{res.error or 'transcribe-failed'}")
        print(f"[media] ⛔ transcribe failed {url[:70]} — {res.error or 'transcribe-failed'}", flush=True)
        return False
    segs = [{"speaker": "SPEAKER_00", "start": s.get("start"), "end": s.get("end"), "text": s.get("text")}
            for s in (res.segments or [])]
    chart.append_transcript(segs, source_url=url)
    chart.append_audio(url, duration_s=res.duration or duration)
    chart.set_status(url, "done")
    return True


async def handle_video(url: str, chart, proxy: str | None = None) -> None:
    """A video/webcast url → transcript, via the first tier that works.

    用一句话讲完: 三档,按成本从低到高 —— ① 直链媒体文件(.mp4/.mp3)直接转写;② YouTube 交 yt-dlp;③ 企业 webcast
    页面用 watercrawl.capture 打开浏览器、从 network log 抓出它自己请求的 .m3u8/.mp4,**再把那条流地址**交给 yt-dlp
    下载。每一档失败都写明原因,绝不静默丢弃。

    WHY tier ③ exists — the measured reason: yt-dlp returns `Unsupported URL` for the webcast PAGES of choruscall /
    webcasts.com / q4inc / webcast-eqs / irwebcasting, and those corporate platforms are 83.5% of all video/webcast
    urls (23,690 / 28,385) while YouTube is 5.3% (1,492). 17,952 events carry such a webcast and NOTHING else, so
    skipping them leaves 7% of the whole event table permanently empty. The page is unsupported; the stream it loads
    is not. {PROBE 2026-08-03 yt_dlp --simulate over the media_100 platforms} {DB 2026-08-03 the two shares}
    [CONFIDENCE: CONFIRMED 100% — both measured]."""
    if router.is_direct_file(url):                             # ① a real file → the existing fetch+transcribe path
        return await handle_audio(url, chart, proxy=proxy)

    if _REGISTER_GATE_RE.search(urlsplit(url).path):           # a signup form is a GATE — there is nothing behind it
        chart.set_status(url, "skipped:register-gate")
        print(f"[media] ⏭ register-gate {url[:70]} — recorded, not fetched", flush=True)
        return

    from tools.youtube import download_audio, download_stream, is_youtube_url   # lazy: yt-dlp optional

    if is_youtube_url(url):                                    # ② YouTube — the one platform yt-dlp handles directly
        yt = await asyncio.to_thread(download_audio, url, proxy)
        if yt.ok:
            await _transcribe_bytes(url, yt.audio, chart, duration=yt.duration)
            return
        print(f"[media] ⚠️ yt-dlp failed {url[:60]} — {yt.error}; falling through to browser capture", flush=True)

    # ③ Any other platform (or a YouTube that yt-dlp refused) — let the browser tell us the real stream url.
    from providers.watercrawl import capture_media
    cap = await asyncio.to_thread(capture_media, url)
    media = cap.get("media") or []
    if not media:
        why = cap.get("error") or "no-media-in-network-log"
        chart.set_status(url, f"skipped:no-stream-captured:{why}")
        print(f"[media] ⏭ no stream captured {url[:70]} — {why} ({cap.get('n_requests', 0)} requests seen)", flush=True)
        return

    stream = _pick_stream(media)
    print(f"[media] 🎯 captured {len(media)} media urls on {url[:55]} → {stream[:70]}", flush=True)
    yt = await asyncio.to_thread(download_stream, stream, proxy)   # yt-dlp DOES handle a bare .m3u8/.mpd/.mp4
    if not yt.ok:
        chart.set_status(url, f"failed:stream-download:{yt.error}")
        print(f"[media] ⛔ stream download failed {stream[:60]} — {yt.error}", flush=True)
        return
    await _transcribe_bytes(url, yt.audio, chart, duration=yt.duration)


# ── HTML → watercrawl render + qwen-VL (the close-loop engine: only this discovers new urls) ──────────────────
def chart_known(chart) -> dict:
    """The event's current metadata, passed to the VLM as reference (confirm & extend, don't blindly trust)."""
    return {"title": chart.title, "date": chart.date, "type": chart.type,
            "urls": [v["url"] for v in chart.urls.values()]}


def _split_blocks(text: str, n: int, overlap: int) -> list[str]:
    """Split text into n contiguous blocks on NEWLINE boundaries (never mid-line → never mid-`[anchor](url)`), each
    block after the first widened BACKWARD by ~overlap chars (snapped to a line start) so no single item is split
    across a cut. Mirror of event_agent.extract._split_blocks (kept local to decouple the two agents)."""
    if n <= 1 or len(text) <= overlap:
        return [text]
    step = len(text) // n
    cuts = []
    for k in range(1, n):
        p = min(step * k, len(text))
        nl = text.rfind("\n", 0, p)
        cuts.append(nl + 1 if nl > 0 else p)
    bounds = [0] + cuts + [len(text)]
    blocks = []
    for k in range(n):
        start = bounds[k]
        if k > 0:
            back = max(0, start - overlap)
            nl = text.rfind("\n", 0, back)
            start = nl + 1 if nl > 0 else back
        blocks.append(text[start:bounds[k + 1]])
    return blocks


def _apply_contribution(contrib: dict, chart, page_url: str) -> None:
    """Apply ONE VLM contribution to the chart (confirm metadata, append basic_info, route inline transcript to the
    transcript slot). Shared by the single-call and chunked paths so both fill the chart identically; the chunked
    path's deliberate overlap is absorbed by Chart's SEAM trim (not by global content dedup, which was removed for
    eating legitimate repeats). Discovers nothing — the url set is event_agent's, fixed."""
    chart.confirm_metadata(contrib.get("title", ""), contrib.get("date", ""), contrib.get("type", ""))
    chart.append_basic_info(contrib.get("basic_info") or [], source_url=page_url)
    chart.append_transcript(contrib.get("transcript_segments") or [], source_url=page_url)   # inline transcript → its slot


async def _vlm_call(client: QwenClient, page_text: str, page_url: str, image_b64, known: dict) -> dict:
    """One qwen-VL call → the raw contribution dict (carries __finish__ so the caller can detect truncation)."""
    return await client.send_one(system=prompts.SYSTEM,
                                 user=prompts.build_user(page_text, page_url, known),
                                 image_b64=image_b64, guided_json=prompts.SCHEMA)


async def _chunked_html(page_text: str, page_url: str, chart, client: QwenClient, depth: int = 0) -> None:
    """FALLBACK for a truncated html page — split its text into blocks that each fit, extract each TEXT-ONLY (the image
    tokens are what overflowed and add little on a long list/transcript), apply every block to the chart, recurse into
    a block that STILL truncates (≤ max depth), and REPORT loudly if a block is still cut at the cap."""
    n = max(2, -(-len(page_text) // _CHUNK_TARGET_CHARS))     # ceil-div: enough blocks that each block's input is bounded
    blocks = _split_blocks(page_text, n, _CHUNK_OVERLAP)
    print(f"[media] ✂️ chunking {page_url[:70]} → {len(blocks)} blocks (depth {depth}, {len(page_text)} chars)", flush=True)
    known = chart_known(chart)
    for i, b in enumerate(blocks):
        contrib = await _vlm_call(client, b, page_url, None, known)   # text-only sub-call (no image → can't re-overflow)
        if contrib.get("__finish__") == "length":             # this block STILL too big
            if depth < _CHUNK_MAX_DEPTH:                       # → split it further
                await _chunked_html(b, page_url, chart, client, depth + 1)
                continue
            print(f"[media] ⛔ {page_url[:60]} block {i} STILL truncated at max depth {depth} — REPORTING partial "
                  f"(raise MEDIA_CHUNK_MAX_DEPTH)", flush=True)   # never hide it
        if contrib:                                           # apply whatever this block did yield (partial ≠ nothing)
            _apply_contribution(contrib, chart, page_url)


async def _route_html(url: str, page_text: str, img, det: dict, chart, client: QwenClient,
                      page_links: list | None = None) -> None:
    """ROUTE-mode html (deterministic body exists): the trafilatura+pandas blocks ARE the body; a SHRUNK VLM call only
    does the three things that need a model — classify the page, confirm metadata, and pick out the event's own document
    links. Its output is tiny, so NO overflow, NO chunking. {DESIGN wf_7b61c8d0}
    On VLM failure the deterministic body STILL lands (only the three judgements are lost) — recall is never sacrificed
    to a flaky call."""
    fitted = fit_input(page_text[:(_VISION_TEXT_CHARS if img else _MAX_INPUT_CHARS)], ROUTE_MAX_TOKENS)  # cap, then context-fit
    contrib = await client.send_one(system=prompts.SYSTEM_ROUTE,
                                    user=prompts.build_user(fitted, url, chart_known(chart)),
                                    image_b64=img, guided_json=prompts.SCHEMA_ROUTE, max_tokens=ROUTE_MAX_TOKENS)
    if not contrib or contrib.get("_error"):                    # VLM hard-fail → body kept, judgements lost
        chart.append_basic_info(det["blocks"], source_url=url)
        chart.set_status(url, "done:route-vlm-fail")
        print(f"[media] ⚠️ route VLM fail {url[:70]} — deterministic body kept, page_kind/metadata/documents lost",
              flush=True)
        return

    # PAGE KIND — recorded on the chart, acted on by the worker. Deciding here would be wrong: this function owns ONE
    # url, while dropping an event or promoting a hub is an EVENT-level decision that also needs the claim token.
    chart.set_page_kind(url, contrib.get("page_kind") or "")

    chart.confirm_metadata(contrib.get("title", ""), contrib.get("date", ""), contrib.get("type", ""))
    chart.append_basic_info(det["blocks"], source_url=url)      # deterministic reading-order body (real urls, no Lnn)

    # DOCUMENTS — the model SELECTS from links that are on the page; it never GENERATES a url.
    _adopt_documents(url, contrib.get("documents") or [], page_links, chart)
    chart.set_status(url, "done:route")


def _adopt_documents(page_url: str, proposed: list, page_links: list | None, chart) -> int:
    """Add the model's chosen document links to this event's url set. Returns how many were adopted.

    THE HALLUCINATION GATE IS THE POINT. Every proposed url must already appear in the links this page actually
    carries; anything else is dropped loudly. That single check turns the task from generation into selection, so the
    worst case is a MISS (we fail to find a document) rather than a FABRICATION (we fetch a url that never existed).
    Verified on a live page: the model returned the one real pdf and it was present in the page's 194 links.
    {DEMO 2026-08-06 "✅ 在页面上 https://www.veolia.com/sites/g/files/…/Finance_PR_shares_voting_rights_12-03-2025.pdf"}
    [CONFIDENCE: CONFIRMED 100% — run against the production render + VLM path on that page.]

    HTML LINKS ARE REFUSED even if the model proposes one. Following a page to another page is crawling, and stage-2's
    url set is event_agent's by design {USER 2026-08-03 "let's just use the original list from the event agent"}. Only
    FILES are adopted, and a file yields no further links — so the depth of this whole mechanism is 2 by construction,
    not by a budget someone has to remember to enforce.
    """
    allowed = {_canon_link(u) for u in (page_links or [])}
    adopted = 0
    for d in proposed:
        u = (d or {}).get("url") if isinstance(d, dict) else None
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        if _canon_link(u) not in allowed:
            # Loud, because a hallucinated url is a model-behaviour signal worth seeing, not noise to swallow.
            print(f"[media] ⛔ dropped proposed doc NOT on the page: {u[:90]} (from {page_url[:50]})", flush=True)
            continue
        if _SEC_DOC_RE.search(u):
            # Same reason the claim predicate excludes filings pages: EDGAR serves these completely and structurally,
            # so adopting one here would fetch a worse copy of something already reachable in bulk. This check is
            # separate from the claim-side one because a NON-filings press page can still link an EDGAR CDN document.
            # {REST 2026-08-06 "event_media_urls?url=ilike.*CIK-*" -> 1466 of 8068} — 18% of the ledger was this.
            # [CONFIDENCE: CONFIRMED 100% — count read from the live REST endpoint.]
            print(f"[media] ⏭ proposed doc is an SEC filing: {u[:80]} — refused (EDGAR is the systematic route)",
                  flush=True)
            continue
        kind = router.classify(u)
        if kind == router.KIND_HTML or kind == router.KIND_OTHER:
            print(f"[media] ⏭ proposed doc is not a file ({kind}): {u[:80]} — refused (stage-2 does not crawl)",
                  flush=True)
            continue
        if adopted >= _MAX_ADOPTED_DOCS:
            print(f"[media] ⏭ doc cap {_MAX_ADOPTED_DOCS} reached on {page_url[:60]} — remaining proposals ignored",
                  flush=True)
            break
        if chart.add_url(u, kind=kind, status="pending"):      # False = already known → no double work
            adopted += 1
            why = ((d.get("why") or "")[:70]) if isinstance(d, dict) else ""
            print(f"[media] ＋ adopted {kind}: {u[:84]}  ⟵ {why}", flush=True)
    return adopted


async def handle_html(url: str, chart, client: QwenClient, use_image: bool = True,
                      proxy: str | None = None, event_urls: list | None = None) -> None:
    """Render an html page + get THIS page's contribution and FILL the chart. Two paths by extract_html tier: ROUTE
    (deterministic body exists → shrunk VLM, no overflow) vs LEGACY (JS-shell/thin page, tier=='empty' → full-copy VLM
    with the two truncation twins). Discovers no urls — the set is event_agent's, fixed."""
    # BOUNDED RETRY, not a bare single shot. Under fleet concurrency a heavy IR page comes back EMPTY from a load-induced
    # TimeoutError while the SAME page renders fine when run alone, so treating attempt #1's empty as terminal converts a
    # transient timeout into a permanent `failed:empty-render` — and, via the 3-strike counter, into a dead letter.
    # {ENGINE.PY:191-194 "RETRIES AN EMPTY RENDER UP TO _RENDER_TRIES TIMES WITH A SHORT BACKOFF: A LOAD-INDUCED
    # TIMEOUTERROR COMES BACK EMPTY, AND A RETRY ONCE THE BROWSER POOL HAS FREED UP USUALLY LANDS THE PAGE."}
    # [CONFIDENCE: CONFIRMED 100% — event_agent hits the same hosts through the same render stack at the same concurrency.]
    render = await render_with_retry(url)                          # open + full-page screenshot, off the loop, 3 tries
    if not render.get("text") and not render.get("links"):         # STILL empty after every attempt → genuinely walled/dead
        chart.set_status(url, "failed:empty-render")
        print(f"[media] ⛔ empty render {url[:70]} after retries — walled/dead/no content", flush=True)
        return
    page_text = render.get("inline") or render.get("text", "")

    # MISROUTE CHECK — the url said html, the HEAD said html, and the bytes say otherwise. Believe the bytes.
    #
    # Some IR platforms serve documents from extension-less urls AND label them text/html. The HEAD refine cannot see
    # through that; measured on two of them from the VM, using this module's own sniffer:
    #   {VM 2026-08-06 "HTTPS://INVESTOR.LILLY.COM/STATIC-FILES/4AC75EE3-… HEAD CONTENT-TYPE = 'TEXT/HTML; CHARSET=UTF-8'
    #    → REFINE 结果 = HTML"}  and the same for investors.biogen.com — both actually serve PDFs.
    # Rendering one produces a page of undecodable bytes, which the block-level guard then discarded one at a time:
    #   {MEDIA@1 2026-08-06 "[CHART] ⛔ DROPPED MD BLOCK — UNDECODABLE:54% (MIS-ROUTED BINARY, NOT PROSE)"} — 1,162 such
    #   lines in ten minutes across two workers.
    # [CONFIDENCE: CONFIRMED 100% — both the HEAD headers and the drop lines were read off the running fleet.]
    #
    # WHY catch it HERE rather than letting the block guard handle it: the block guard's verdict was correct but its
    # SCOPE was wrong. It dropped the content and left the url recorded `done:route`, so a page we could not read at all
    # was indistinguishable from one that was simply empty — and the ledger claimed success. Deciding at the page level
    # keeps the url's outcome honest and skips an extraction and a VLM call that cannot produce anything.
    why = _looks_binary(page_text)
    if why:
        # NAME the real kind when the bytes identify themselves, and hand the url back to the same gate every other
        # url passes through. A `%PDF-` header is not an error — it is a correct answer to "what is this", arriving
        # later than we would like. Treating it as a failure would put a perfectly good pdf into the retry-and-then-
        # dead-letter path, so that turning the docling lane on later would never reach it.
        # {CHART.PY "_BINARY_MAGIC = (B\"%PDF-\", B\"PK\\X03\\X04\", B\"\\XD0\\XCF\\X11\\XE0\", …)"} — the same table
        # the sniffer matches against, so the two cannot disagree about what a header means.
        # [CONFIDENCE: CONFIRMED 100% — magic list read from the sniffer; the reason string is its own output format.]
        real = _kind_from_magic(why)
        if real and real not in _ENABLED_KINDS:
            chart.set_status(url, "skipped:kind-disabled")
            print(f"[media] ↪ misrouted {url[:70]} — served {real} under an html content-type; "
                  f"deferred with the rest of that lane", flush=True)
            return
        if real:
            # The lane IS enabled — the url was simply mis-typed upstream, so run the handler it should have had.
            print(f"[media] ↪ misrouted {url[:70]} — served {real} under an html content-type; "
                  f"re-routing to its own handler", flush=True)
            if real in (router.KIND_PDF, router.KIND_PPTX, router.KIND_DOCX, router.KIND_XLSX):
                return await handle_office(url, chart, proxy=proxy, client=client, event_urls=event_urls)
            if real == router.KIND_AUDIO:
                return await handle_audio(url, chart, proxy=proxy)
            if real == router.KIND_VIDEO:
                return await handle_video(url, chart, proxy=proxy)
        # Undecodable with no recognisable header — we genuinely do not know what this is, and saying so is the only
        # honest outcome. This stays a failure because there is no lane to defer it to.
        chart.set_status(url, f"failed:misrouted-binary:{why}")
        print(f"[media] ⛔ misrouted {url[:70]} — binary under an html content-type, unidentifiable ({why})", flush=True)
        return

    img = render.get("shot_b64", "") if use_image else None
    text_cap = _VISION_TEXT_CHARS if img else _MAX_INPUT_CHARS

    # DETERMINISTIC body first (trafilatura+pandas). tier!='empty' ⇒ real HTML body → ROUTE mode (the earnings-table overflow
    # is impossible by construction). tier=='empty' ⇒ JS-shell/thin → fall through to the LEGACY full-copy path below.
    det = extract_html(render.get("html", ""), base_url=url, links=render.get("links"))
    if det["tier"] != "empty":
        return await _route_html(url, page_text, img, det, chart, client,
                                 page_links=render.get("links") or [])

    # ── LEGACY full-copy path (empty tier only): the VLM reproduces basic_info from text/screenshot, with both twins ──
    # TWIN A — INPUT over cap: a plain call would SILENTLY trim the tail (and its content/links). Chunk the FULL text.
    if len(page_text) > text_cap:
        print(f"[media] ⚠️ INPUT over cap {url[:70]} — {len(page_text)}>{text_cap} chars → chunking full text "
              f"(else the tail's content vanishes silently)", flush=True)
        await _chunked_html(page_text, url, chart, client)
        chart.set_status(url, "done:chunked-input")
        return

    contrib = await _vlm_call(client, page_text, url, img, chart_known(chart))
    if not contrib:                                           # {} = hard failure after retries (not truncation) → loud
        chart.set_status(url, "failed:no-parse")
        print(f"[media] ⛔ no-parse {url[:70]} — VLM returned nothing usable after retries", flush=True)
        return

    # TWIN B — OUTPUT truncated (finish_reason=length): the reply JSON was cut. Chunk-and-retry, never salvage partial.
    if contrib.get("__finish__") == "length":
        print(f"[media] ⚠️ OUTPUT truncated {url[:70]} → chunking (no silent salvage)", flush=True)
        await _chunked_html(page_text, url, chart, client)
        chart.set_status(url, "done:chunked-output")
        return

    _apply_contribution(contrib, chart, url)
    chart.set_status(url, "done")


# ── extension-less content-type refine (edge audit H3) ────────────────────────────────────────────────────────
async def _head_content_type(url: str) -> str | None:
    """Cheap HEAD → the server's Content-Type, for refining an extension-less url's kind. curl_cffi (browser-impersonated,
    the same fetch stack the tools use) first, stdlib HEAD as fallback; None when BOTH fail (caller then keeps the html
    guess but says so). Never raises into the loop."""
    def _head() -> str | None:
        try:
            from curl_cffi import requests as creq            # browser fingerprint — IR hosts block plain clients
            r = creq.head(url, impersonate="chrome", timeout=_HEAD_TIMEOUT_S, allow_redirects=True)
            return r.headers.get("content-type", "") or ""
        except Exception:                                     # noqa: BLE001 — curl_cffi missing / host rejects HEAD
            try:
                import urllib.request
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=_HEAD_TIMEOUT_S) as resp:   # noqa: S310 — http(s) only, our url
                    return resp.headers.get("Content-Type", "") or ""
            except Exception:                                 # noqa: BLE001 — both HEAD paths failed
                return None
    return await asyncio.to_thread(_head)


async def _refine_kind(url: str, kind: str) -> str:
    """H3: an EXTENSION-LESS url classified `html` may actually be a pdf/audio download (IR `/download`, `/media/<id>`,
    `/static-files/<uuid>`). Do a HEAD content-type sniff to re-route. Only for html + no-extension (an explicit
    extension is trusted; non-html kinds are already certain). FALLBACK: HEAD fails → keep html but LOG it — better to
    render-and-maybe-miss than to guess a wrong parser silently."""
    if kind != router.KIND_HTML:
        return kind                                          # certain kinds: trust classify
    # HEAD-refine when the type ISN'T decidable from the url: extensionless, OR a known opaque-download endpoint
    # (/static-files/<uuid>, /Resource/Download/, /api/asset/.../download) whose bare token gives no hint even though
    # a future extension-trust shortcut might otherwise skip it. {AUDIT 2026-07-23: 375 opaque downloads}.
    if router.has_extension(url) and not router.is_opaque_download(url):
        return kind                                          # explicit extension + not an opaque endpoint: trust it
    ct = await _head_content_type(url)
    if ct is None:
        print(f"[media] ⚠️ HEAD failed {url[:70]} — keeping html guess (extensionless; may be a doc/audio)", flush=True)
        return kind
    refined = router.classify_by_content_type(ct, fallback=kind)
    if refined != kind:
        print(f"[media] ↪ refined {url[:60]} html→{refined} (content-type: {ct[:40]})", flush=True)
    return refined


# ── dispatch: (url, kind) → the right handler ─────────────────────────────────────────────────────────────────
async def dispatch(url: str, kind: str, chart, client: QwenClient | None = None,
                   use_image: bool = True, proxy: str | None = None,
                   event_urls: list | None = None) -> None:
    """Route one url to its handler by kind. Refines an extension-less html guess via HEAD first (H3). `other` is
    recorded-only (feeds/assets/unknown) — never fetched.

    用一句话讲完: URL 集合由 event_agent 一次性给定,dispatch 只负责"这一条交给谁处理",不再产生新 URL —— 闭环
    frontier 已删除。{USER 2026-08-03 "let's just use the original list from the event agent"}
    [CONFIDENCE: CONFIRMED 100% — direct user directive]."""
    # TRACKER GATE — FIRST, before _refine_kind, because refining issues a HEAD that would itself follow the redirect
    # and report the destination's content-type. Gating here spends nothing at all on a url that cannot carry content.
    # Recorded with a reason, not dropped: the link was really on the page, it just does not resolve to this event.
    if _TRACKER_URL_RE.search(url):
        chart.set_status(url, "skipped:tracker-redirect")
        return

    kind = await _refine_kind(url, kind)                      # H3: extensionless html → maybe pdf/audio via content-type

    # SEC GATE — filings never enter this pipeline, whichever door they arrive at. The claim predicate keeps out events
    # whose SOURCE page is a filings list, and _adopt_documents refuses filings the model proposes; this catches the
    # third door — a filing sitting in the ORIGINAL media_urls of an otherwise-ordinary press event. Measured after the
    # first two gates shipped: 14 of 259 newly-written ledger rows were still SEC urls arriving by exactly this route.
    # {psql 2026-08-06 "重启后新增台账行 259 | 其中sec 14"}
    # [CONFIDENCE: CONFIRMED 100% — counted on rows written after the restart that deployed the other two gates.]
    #
    # Recorded as skipped WITH a reason rather than silently dropped: the url is real and belongs to the event, we are
    # simply not the right mechanism for it. EDGAR serves these completely and structurally.
    # {USER 2026-08-06 "we can sysmeticlaly process those url ther is no need for us to do it here"}
    if _SEC_DOC_RE.search(url):
        chart.set_status(url, "skipped:sec-filing")
        return

    # KIND GATE — run only the lanes this deployment has capacity for; record the rest honestly. See _ENABLED_KINDS.
    # `other` is deliberately NOT gated here: it already has its own recorded-only outcome below, and routing it
    # through this branch would relabel a genuinely-unfetchable url (mailto, feed, asset) as a config decision.
    if kind not in _ENABLED_KINDS and kind != router.KIND_OTHER:
        chart.set_status(url, "skipped:kind-disabled")
        return

    if kind == router.KIND_HTML:
        await handle_html(url, chart, client or QwenClient(), use_image=use_image, proxy=proxy,
                          event_urls=event_urls)
    elif kind in (router.KIND_PDF, router.KIND_PPTX, router.KIND_DOCX, router.KIND_XLSX):   # H2: xlsx now routed
        await handle_office(url, chart, proxy=proxy, client=client, event_urls=event_urls)
    elif kind == router.KIND_AUDIO:
        await handle_audio(url, chart, proxy=proxy)
    elif kind == router.KIND_VIDEO:
        await handle_video(url, chart, proxy=proxy)
    else:
        chart.set_status(url, "skipped:other")                # feed / asset / mailto / unknown external
