"""media_agent.handlers — one handler per url KIND. Each opens/parses its url with the right tool and FILLS the
chart's matching slot; only html yields new urls (the close-loop engine). dispatch() routes a (url,kind) to its handler.

用一句话讲完: router 判 kind → dispatch 把 url 交给对应 handler → handle_html 用 watercrawl 渲染 + qwen-VL 出
{basic_info blocks, 内联 transcript, new_urls, metadata};handle_office 调 tools/officeall(Docling);handle_audio 调
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

from providers import watercrawl                     # html render (reused, unchanged)
from providers.qwen_llm import QwenClient            # VLM transport (reused, unchanged)

from . import prompts, router

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
async def handle_office(url: str, chart, proxy: str | None = None) -> list[str]:
    """Parse an office document via tools/officeall and append it to chart.files[kind]. A document is a LEAF — it
    yields no new urls to crawl, so returns []. {OFFICEALL extract(url)->DocResult}."""
    from tools.officeall import extract as office_extract     # lazy: docling is a heavy GPU dep, only load on use
    res = await asyncio.to_thread(office_extract, url, proxy)  # blocking Docling call off the event loop
    if not res.ok:                                            # unreachable / not-a-doc / unparseable → say why, loudly
        chart.set_status(url, f"failed:{res.error or 'no-content'}")
        print(f"[media] ⛔ office parse failed {url[:70]} — {res.error or 'no-content'}", flush=True)
        return []
    kind = res.format or router.classify(url)                 # trust the magic-confirmed format from the fetch
    chart.append_file(kind, url, markdown=res.text, tables=res.tables, n_pages=res.n_pages)
    chart.set_status(url, "done")
    return []


# ── AUDIO (mp3 / wav / direct video file) → tools/audio_extract (faster-whisper) ──────────────────────────────
async def handle_audio(url: str, chart, proxy: str | None = None) -> list[str]:
    """Transcribe an audio url via tools/audio_extract and append speaker-tagged segments to chart.transcript +
    the audio artifact to chart.audio. Returns [] (audio is a leaf)."""
    from tools.audio_extract import extract as audio_extract  # lazy: faster-whisper + torch are heavy GPU deps
    res = await asyncio.to_thread(audio_extract, url, proxy)   # blocking whisper transcription off the event loop
    if not res.ok:
        chart.set_status(url, f"failed:{res.error or 'transcribe-failed'}")
        print(f"[media] ⛔ transcribe failed {url[:70]} — {res.error or 'transcribe-failed'}", flush=True)
        return []
    # audio_extract.segments are {start,end,text} with NO speaker — diarization is a documented TODO in that tool, and
    # the user wants SPEAKER_00 for now. {AUDIO_EXTRACT __init__ "留待后续... pyannote 说话人 diarization"} {USER
    # 2026-07-23 "first use speaker_00 for now"} [CONFIDENCE: CONFIRMED 100% — tool docstring + direct user instruction].
    segs = [{"speaker": "SPEAKER_00", "start": s.get("start"), "end": s.get("end"), "text": s.get("text")}
            for s in (res.segments or [])]
    chart.append_transcript(segs, source_url=url)
    chart.append_audio(url, duration_s=res.duration)
    chart.set_status(url, "done")
    return []


# ── VIDEO (youtube / webcast / .mp4) → audio path OR flagged for yt-dlp ────────────────────────────────────────
async def handle_video(url: str, chart, proxy: str | None = None) -> list[str]:
    """A DIRECT video FILE (.mp4/.mov/…) → transcribe its audio track (audio_extract decodes it via ffmpeg). A video
    PLATFORM url (youtube/vimeo/webcast — no file extension) → audio_extract can't fetch it yet (yt-dlp adapter is a
    TODO there), so mark it `skipped:needs-ytdlp` LOUDLY rather than letting it gate-pass and die as a vague `failed`.
    {edge audit M1: maybe_audio_url(youtube)=True → it would fetch the HTML page and fail confusingly}."""
    if not router.is_direct_file(url):                        # extension-less video host → platform page, not a file
        # A registration/signup form is a GATE (nothing to fetch); a real player page → flag for the yt-dlp step.
        # Both are RECORDED (never dropped), just with a status that tells the yt-dlp step whether to bother.
        gated = bool(_REGISTER_GATE_RE.search(urlsplit(url).path))
        chart.set_status(url, "skipped:register-gate" if gated else "skipped:needs-ytdlp")
        print(f"[media] ⏭ webcast {'register-gate' if gated else 'platform'} {url[:70]} — recorded, not fetched", flush=True)
        return []
    return await handle_audio(url, chart, proxy=proxy)        # direct media file → same transcribe path


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


def _resolve_new_urls(raw: list, page_url: str) -> list[str]:
    """Turn the VLM's new_urls into absolute http(s) urls (edge audit H1). A relative `/files/x.pdf` or protocol-
    relative `//cdn/a.mp3` is urljoin'd against the page; anything still not http after that is DROPPED LOUDLY (it
    can't be fetched and would poison the ledger's canonical dedup)."""
    out = []
    for u in raw or []:
        if not isinstance(u, str) or not u.strip():
            continue
        absu = urljoin(page_url, u.strip())                   # relative / protocol-relative → absolute against the page
        if not absu.lower().startswith(("http://", "https://")):
            print(f"[media] ⚠️ dropping non-resolvable link {u!r} on {page_url[:60]}", flush=True)
            continue
        out.append(absu)
    return out


def _apply_contribution(contrib: dict, chart, page_url: str) -> list[str]:
    """Apply ONE VLM contribution to the chart (confirm metadata, append basic_info, route inline transcript to the
    transcript slot) and return this contribution's resolved new_urls. Shared by the single-call and chunked paths so
    both fill the chart identically; chart's own content-hash dedup absorbs overlap between chunks."""
    chart.confirm_metadata(contrib.get("title", ""), contrib.get("date", ""), contrib.get("type", ""))
    chart.append_basic_info(contrib.get("basic_info") or [])
    chart.append_transcript(contrib.get("transcript_segments") or [], source_url=page_url)   # inline transcript → its slot
    return _resolve_new_urls(contrib.get("new_urls"), page_url)


async def _vlm_call(client: QwenClient, page_text: str, page_url: str, image_b64, known: dict) -> dict:
    """One qwen-VL call → the raw contribution dict (carries __finish__ so the caller can detect truncation)."""
    return await client.send_one(system=prompts.SYSTEM,
                                 user=prompts.build_user(page_text, page_url, known),
                                 image_b64=image_b64, guided_json=prompts.SCHEMA)


async def _chunked_html(page_text: str, page_url: str, chart, client: QwenClient, depth: int = 0) -> list[str]:
    """FALLBACK for a truncated html page — split its text into blocks that each fit, extract each TEXT-ONLY (the image
    tokens are what overflowed and add little on a long list/transcript), apply every block to the chart, recurse into
    a block that STILL truncates (≤ max depth), and REPORT loudly if a block is still cut at the cap. Returns the union
    of all blocks' new_urls (chart dedup handles the overlap-region duplicates)."""
    n = max(2, -(-len(page_text) // _CHUNK_TARGET_CHARS))     # ceil-div: enough blocks that each block's input is bounded
    blocks = _split_blocks(page_text, n, _CHUNK_OVERLAP)
    print(f"[media] ✂️ chunking {page_url[:70]} → {len(blocks)} blocks (depth {depth}, {len(page_text)} chars)", flush=True)
    known = chart_known(chart)
    new_urls: list[str] = []
    for i, b in enumerate(blocks):
        contrib = await _vlm_call(client, b, page_url, None, known)   # text-only sub-call (no image → can't re-overflow)
        if contrib.get("__finish__") == "length":             # this block STILL too big
            if depth < _CHUNK_MAX_DEPTH:                       # → split it further
                new_urls += await _chunked_html(b, page_url, chart, client, depth + 1)
                continue
            print(f"[media] ⛔ {page_url[:60]} block {i} STILL truncated at max depth {depth} — REPORTING partial "
                  f"(raise MEDIA_CHUNK_MAX_DEPTH)", flush=True)   # never hide it
        if contrib:                                           # apply whatever this block did yield (partial ≠ nothing)
            new_urls += _apply_contribution(contrib, chart, page_url)
    return new_urls


async def handle_html(url: str, chart, client: QwenClient, use_image: bool = True) -> list[str]:
    """Render an html page + ask qwen-VL for THIS page's contribution, FILL the chart, and RETURN the resolved new_urls
    (the caller dedups + enqueues them). Covers BOTH truncation twins loudly: input over the char cap → chunk full text
    up-front; output finish_reason=length → chunk after. This is the only handler that grows the frontier."""
    render = await asyncio.to_thread(watercrawl.render_shot, url)   # open + full-page screenshot, off the loop
    if not render.get("text") and not render.get("links"):         # walled / dead / empty → nothing to contribute
        chart.set_status(url, "failed:empty-render")
        print(f"[media] ⛔ empty render {url[:70]} — walled/dead/no content", flush=True)
        return []
    page_text = render.get("inline") or render.get("text", "")
    img = render.get("shot_b64", "") if use_image else None
    text_cap = _VISION_TEXT_CHARS if img else _MAX_INPUT_CHARS

    # TWIN A — INPUT over cap: a plain call would SILENTLY trim the tail (and its content/links). Chunk the FULL text.
    if len(page_text) > text_cap:
        print(f"[media] ⚠️ INPUT over cap {url[:70]} — {len(page_text)}>{text_cap} chars → chunking full text "
              f"(else the tail's content vanishes silently)", flush=True)
        new_urls = await _chunked_html(page_text, url, chart, client)
        chart.set_status(url, "done:chunked-input")
        return new_urls

    contrib = await _vlm_call(client, page_text, url, img, chart_known(chart))
    if not contrib:                                           # {} = hard failure after retries (not truncation) → loud
        chart.set_status(url, "failed:no-parse")
        print(f"[media] ⛔ no-parse {url[:70]} — VLM returned nothing usable after retries", flush=True)
        return []

    # TWIN B — OUTPUT truncated (finish_reason=length): the reply JSON was cut. Chunk-and-retry, never salvage partial.
    if contrib.get("__finish__") == "length":
        print(f"[media] ⚠️ OUTPUT truncated {url[:70]} → chunking (no silent salvage)", flush=True)
        new_urls = await _chunked_html(page_text, url, chart, client)
        chart.set_status(url, "done:chunked-output")
        return new_urls

    new_urls = _apply_contribution(contrib, chart, url)
    chart.set_status(url, "done")
    return new_urls


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
                   use_image: bool = True, proxy: str | None = None) -> list[str]:
    """Route one url to its handler by kind and return any new urls to enqueue (only html yields them). Refines an
    extension-less html guess via HEAD first (H3). `other` is recorded-only (feeds/assets/unknown) — never fetched."""
    kind = await _refine_kind(url, kind)                      # H3: extensionless html → maybe pdf/audio via content-type
    if kind == router.KIND_HTML:
        return await handle_html(url, chart, client or QwenClient(), use_image=use_image)
    if kind in (router.KIND_PDF, router.KIND_PPTX, router.KIND_DOCX, router.KIND_XLSX):   # H2: xlsx now routed
        return await handle_office(url, chart, proxy=proxy)
    if kind == router.KIND_AUDIO:
        return await handle_audio(url, chart, proxy=proxy)
    if kind == router.KIND_VIDEO:
        return await handle_video(url, chart, proxy=proxy)
    chart.set_status(url, "skipped:other")                    # feed / asset / mailto / unknown external
    return []
