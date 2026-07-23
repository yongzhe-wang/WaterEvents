"""test_live — end-to-end on REAL urls via officeall (Docling) + audio_extract. SKIPS when a dep is absent, so
Mac-without-docling runs only the pure-logic tests; the VM/H100 with deps runs the real network+model paths.

用一句话讲完: officeall.extract 走完整链(fetch + Docling)在真实 IR PDF 上抽出干净 markdown + 结构化表格;
audio 用真实 mp3(env 提供)。URL 用环境变量覆盖(WATEREVENTS_TEST_PDF_URL / _PPTX_URL / _AUDIO_URL)。

跑(VM 上装好依赖):
  pip install curl_cffi docling faster-whisper
  pytest tests/ -v
  WATEREVENTS_TEST_PPTX_URL=<deck> pytest tests/test_live.py -v
"""
import importlib.util
import os

import pytest


def _has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


needs_curl = pytest.mark.skipif(not _has("curl_cffi"), reason="curl_cffi not installed (live fetch)")
needs_docling = pytest.mark.skipif(not _has("docling"), reason="docling not installed (office extract)")
needs_whisper = pytest.mark.skipif(not _has("faster_whisper"), reason="faster-whisper not installed (transcribe)")

_PDF_URL = os.environ.get("WATEREVENTS_TEST_PDF_URL", "https://www.berkshirehathaway.com/letters/2022ltr.pdf")
_PPTX_URL = os.environ.get("WATEREVENTS_TEST_PPTX_URL", "")
_AUDIO_URL = os.environ.get("WATEREVENTS_TEST_AUDIO_URL", "")


@needs_curl
@needs_docling
def test_live_office_pdf():
    from tools.officeall import extract
    r = extract(_PDF_URL)
    assert r.ok, f"office pdf extract failed: error={r.error} bytes={r.n_bytes}"
    assert r.format == "pdf" and r.n_pages > 0 and len(r.text) > 200
    print(f"\n[live pdf] pages={r.n_pages} bytes={r.n_bytes} md_chars={len(r.text)} tables={r.n_tables}")


@needs_curl
@needs_docling
def test_live_office_pptx():
    if not _PPTX_URL:
        pytest.skip("set WATEREVENTS_TEST_PPTX_URL to run the live pptx test")
    from tools.officeall import extract
    r = extract(_PPTX_URL)
    assert r.ok, f"office pptx extract failed: error={r.error}"
    assert r.format == "pptx" and len(r.text) > 50
    print(f"\n[live pptx] bytes={r.n_bytes} md_chars={len(r.text)} tables={r.n_tables}")


@needs_curl
@needs_whisper
def test_live_audio():
    if not _AUDIO_URL:
        pytest.skip("set WATEREVENTS_TEST_AUDIO_URL to run the live audio test (GPU recommended)")
    from tools.audio_extract import extract
    r = extract(_AUDIO_URL)
    assert r.ok, f"audio extract failed: error={r.error}"
    assert r.duration > 0 and len(r.transcript) > 20
    print(f"\n[live audio] dur={r.duration:.0f}s lang={r.language} chars={len(r.transcript)} segs={len(r.segments)}")
