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
from urllib.parse import urljoin, urlsplit

from providers.qwen_llm import QwenClient            # VLM transport (reused, unchanged)

# html render — via the bounded-retry wrapper, NOT a bare watercrawl.render_shot. A single-shot render turns a transient
# concurrency timeout into a permanent failure (see render_retry's module docstring for the measured event-side evidence).
from .render_retry import render_with_retry

from ..extract import prompts, router
# DETERMINISTIC body + shared input-overflow guard — the production worker path (dispatch→handle_html) gets the SAME
# trafilatura+pandas body extraction as enrich.enrich_page, so an earnings page's financial tables never overflow the VLM.
from ..extract.extract_html import extract_html, suppress_transcript_blocks, fit_input, ROUTE_MAX_TOKENS

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


# ── OFFICE (pdf / pptx / docx / xlsx) → tools/officeall (Docling) ──────────────────────────────────────────────
async def handle_office(url: str, chart, proxy: str | None = None) -> None:
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
    transcript slot). Shared by the single-call and chunked paths so both fill the chart identically; chart's own
    content-hash dedup absorbs overlap between chunks. Discovers nothing — the url set is event_agent's, fixed."""
    chart.confirm_metadata(contrib.get("title", ""), contrib.get("date", ""), contrib.get("type", ""))
    chart.append_basic_info(contrib.get("basic_info") or [])
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


async def _route_html(url: str, page_text: str, img, det: dict, chart, client: QwenClient) -> None:
    """ROUTE-mode html (deterministic body exists): the trafilatura+pandas blocks ARE the body; a SHRUNK VLM call only
    confirms metadata + parses transcript + lists media-url ids → its output is tiny, so NO overflow, NO chunking. Mirrors
    enrich.enrich_page's route path. {DESIGN wf_7b61c8d0} [CONFIDENCE: CONFIRMED — validated on prologis: 14 tables, no length].
    On VLM failure the deterministic body STILL lands (only metadata/routing lost) and the exhaustive url harvest backstops
    the frontier — recall is never sacrificed to a flaky call."""
    fitted = fit_input(page_text[:(_VISION_TEXT_CHARS if img else _MAX_INPUT_CHARS)], ROUTE_MAX_TOKENS)  # cap, then context-fit
    contrib = await client.send_one(system=prompts.SYSTEM_ROUTE,
                                    user=prompts.build_user(fitted, url, chart_known(chart)),
                                    image_b64=img, guided_json=prompts.SCHEMA_ROUTE, max_tokens=ROUTE_MAX_TOKENS)
    if not contrib or contrib.get("_error"):                    # VLM hard-fail → body kept, urls fall back to the harvest
        chart.append_basic_info(det["blocks"])
        chart.set_status(url, "done:route-vlm-fail")
        print(f"[media] ⚠️ route VLM fail {url[:70]} — deterministic body kept, only metadata/transcript lost", flush=True)
        return
    chart.confirm_metadata(contrib.get("title", ""), contrib.get("date", ""), contrib.get("type", ""))
    chart.append_transcript(contrib.get("transcript_segments") or [], source_url=url)   # transcript → its slot (needed for suppress)
    # STAGE 8: drop a det transcript-flagged block ONLY where the VLM actually routed it (no double-listing; uncovered stays body)
    body = suppress_transcript_blocks(det["blocks"], det["transcript_idx"], contrib.get("transcript_segments") or [])
    chart.append_basic_info(body)                               # deterministic reading-order body (real urls, no Lnn to resolve)
    chart.set_status(url, "done:route")


async def handle_html(url: str, chart, client: QwenClient, use_image: bool = True) -> None:
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
    img = render.get("shot_b64", "") if use_image else None
    text_cap = _VISION_TEXT_CHARS if img else _MAX_INPUT_CHARS

    # DETERMINISTIC body first (trafilatura+pandas). tier!='empty' ⇒ real HTML body → ROUTE mode (the earnings-table overflow
    # is impossible by construction). tier=='empty' ⇒ JS-shell/thin → fall through to the LEGACY full-copy path below.
    det = extract_html(render.get("html", ""), base_url=url, links=render.get("links"))
    if det["tier"] != "empty":
        return await _route_html(url, page_text, img, det, chart, client)

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
                   use_image: bool = True, proxy: str | None = None) -> None:
    """Route one url to its handler by kind. Refines an extension-less html guess via HEAD first (H3). `other` is
    recorded-only (feeds/assets/unknown) — never fetched.

    用一句话讲完: URL 集合由 event_agent 一次性给定,dispatch 只负责"这一条交给谁处理",不再产生新 URL —— 闭环
    frontier 已删除。{USER 2026-08-03 "let's just use the original list from the event agent"}
    [CONFIDENCE: CONFIRMED 100% — direct user directive]."""
    kind = await _refine_kind(url, kind)                      # H3: extensionless html → maybe pdf/audio via content-type
    if kind == router.KIND_HTML:
        await handle_html(url, chart, client or QwenClient(), use_image=use_image)
    elif kind in (router.KIND_PDF, router.KIND_PPTX, router.KIND_DOCX, router.KIND_XLSX):   # H2: xlsx now routed
        await handle_office(url, chart, proxy=proxy)
    elif kind == router.KIND_AUDIO:
        await handle_audio(url, chart, proxy=proxy)
    elif kind == router.KIND_VIDEO:
        await handle_video(url, chart, proxy=proxy)
    else:
        chart.set_status(url, "skipped:other")                # feed / asset / mailto / unknown external
