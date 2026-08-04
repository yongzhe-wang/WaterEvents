"""officeall — ONE tool for every office document: pdf / pptx / xlsx / docx / html → structured content.

用一句话讲完: 把原来分开的 pdf_extract / pptx_extract 统一成一个 tool,底层换成 Docling(一个引擎全格式,97.9%
财务表准确率,自带 OCR)。流程照旧对称:detect(判格式)→ fetch(SSRF + curl_cffi + magic 确认真实格式)→
extract(Docling → 干净 markdown + 结构化表格)→ 统一 DocResult。audio 不是 office 文档,留独立的 audio_extract。

组织:
  detect.py   — detect_format / is_office_url / maybe_office_url        (pdf/pptx/xlsx/docx/html)
  fetch.py    — fetch_bytes(url, proxy) -> (bytes, real_format)         (Office-viewer 拆包 + magic 嗅探)
  extract.py  — docling_extract(bytes, fmt) -> {markdown, tables, ...}  (Docling 引擎,单例,H100 GPU)
  types.py    — DocResult                                              (统一返回形状)

依赖(全 lazy):curl_cffi(fetch)、docling(extract,缺则 extract_bytes 返回 ok=False)。proxy 由调用方给。
"""
from __future__ import annotations

from .detect import detect_format, is_office_url, maybe_office_url
from .extract import docling_extract
from .fetch import fetch_bytes
from .types import DocResult

# ── REMOTE DOCLING SPLIT ─────────────────────────────────────────────────────────────────────────────────────────
# Setting DOCLING_REMOTE_URL rebinds docling_extract to an HTTP client that runs the SAME function on the RunPod pod,
# where 96 vCPU sit idle instead of 8 shared with Chromium. extract_bytes() below resolves the name at CALL time, so
# rebinding this module global is enough — no call site changes, and unsetting the var restores the local path.
#
# WHY the pod's CPU and not a GPU: the A40 has 5.1 GB free of 46 GB and is pinned at 100% serving vLLM
# {NVIDIA-SMI 2026-08-04 "NVIDIA A40, 46068 MIB, 40299 MIB USED, 5190 MIB FREE, 100%"}, so Docling could not fit there
# even if we wanted it to. The win is core COUNT, not device class: 118s per document at concurrency 4 is ~50 days for
# the 146,254 pdf urls we hold; the same 118s at concurrency 24 is ~8 days.
# {MEASURED 2026-08-03 A/B — OCR-OFF MEDIAN 118.0s PER DOCUMENT, CONCURRENCY 4, ON 8 SHARED CORES}
# [CONFIDENCE: CONFIRMED — matched-pair A/B over identical events, both runs on ir-media-8].
import os as _os                                              # noqa: E402 — deliberately after the local imports above

if _os.environ.get("DOCLING_REMOTE_URL", "").strip():
    from providers.tools_remote import docling_extract        # noqa: F811,E402 — intentional rebind, see block comment

__all__ = ["extract", "extract_bytes", "detect_format", "is_office_url", "maybe_office_url",
           "fetch_bytes", "docling_extract", "DocResult"]


def extract_bytes(data: bytes, fmt: str = "pdf", want_structured: bool = False) -> DocResult:
    """Office-doc bytes → DocResult. fmt = the format hint ('pdf'|'pptx'|'xlsx'|'docx'|'html'). Runs the Docling→pypdf
    fallback chain (see docling_extract); `via`/`warnings` carry which path won + any LOUD quality flags. When the
    whole chain yields nothing, `error` names WHY (the accumulated warnings), never a silent empty."""
    if not data:
        return DocResult(source="bytes", error="empty-bytes")
    content = docling_extract(data, fmt, want_structured=want_structured)   # always a dict (fallback chain inside)
    res = DocResult(format=fmt, text=content["markdown"], tables=content["tables"],
                    structured=content.get("structured", {}), n_pages=content["n_pages"],
                    n_tables=content["n_tables"], n_bytes=len(data), source="bytes",
                    via=content.get("via", ""), warnings=content.get("warnings", []))
    if not res.ok:                                              # loud: name the chain's failure, not a bare 'no-content'
        res.error = "; ".join(res.warnings) if res.warnings else "no-content"
    return res


def extract(url: str, proxy: str | None = None, want_structured: bool = False) -> DocResult:
    """A (maybe-)office-doc url → DocResult. Flow: maybe_office_url gate → fetch_bytes (Office-viewer unwrap, Chrome
    fingerprint + residential-proxy fallback, magic-confirmed format) → docling_extract (Docling→pypdf fallback).
    proxy: residential proxy url used for fetch's fallback leg. FAIL LOUDLY: a fetch failure carries the SPECIFIC
    reason (http-403 / ssrf-blocked / oversized-30MB / …) into `error`, never a silent 'fetch-empty'."""
    candidate, fmt_guess = maybe_office_url(url)
    if not candidate:
        return DocResult(source="url", error="not-office-url")
    data, fmt_or_reason = fetch_bytes(url, proxy=proxy, fmt_guess=fmt_guess)
    if not data:                                                 # fmt_or_reason is the LOUD failure reason here
        return DocResult(source="url", error=f"fetch-failed:{fmt_or_reason}")
    res = extract_bytes(data, fmt=fmt_or_reason, want_structured=want_structured)   # on success it's the real format
    res.source = "url"
    return res

# ── REMOTE FETCH SPLIT ───────────────────────────────────────────────────────────────────────────────────────────
# FETCH_REMOTE_URL moves the DOWNLOAD itself to ir-render-16: extract(url) becomes one HTTP call, and the file bytes
# never touch this box. Three reasons, memory being the weakest:
#   1. Egress IP coherence — a site currently sees one IP render the page and a DIFFERENT IP download the file it
#      links to, seconds later. That is a bot signature we manufacture ourselves.
#      {GCLOUD 2026-08-04 — ir-media-8 EXTERNAL 35.254.161.69 / ir-render-16 EXTERNAL 136.112.158.156}
#   2. Rate limiting — the download path consults politeness ZERO times while the render path consults it 20 times,
#      so two uncoordinated channels hit the same host. On the render VM both run in ONE process and share one
#      per-host pacing cursor. {GREP 2026-08-04 — render.py 15, capture.py 5, both fetch.py files 0}
#   3. Memory — fetch reads the whole file into RAM before forwarding, capped at 300MB each; 24 slots is 7.2GB worst
#      case, which is what decides how small this box can get. {one choruscall mp3 = 91,723,583 bytes}
# [CONFIDENCE: CONFIRMED — IPs, grep counts and file size all read from live sources].
import os as _os                                              # noqa: E402 — deliberately after the local imports above

if _os.environ.get("FETCH_REMOTE_URL", "").strip():
    from providers.fetch_remote import office_extract as extract        # noqa: F811,E402 — intentional rebind
