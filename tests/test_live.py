"""test_live — end-to-end on REAL urls. Needs the heavy deps + network, so each test SKIPS when its dep is
absent (that's why they no-op on the Mac and only really run on the VM/H100).

用一句话讲完: 拿真实的 IR PDF / deck / 音频 URL,走完整 extract(url) → 断言 .ok 且抓到真内容。URL 用环境变量
覆盖(WATEREVENTS_TEST_PDF_URL / _PPTX_URL / _AUDIO_URL),没设就用一个默认公开 URL。依赖缺失(curl_cffi/pypdf/
python-pptx/faster-whisper)→ 自动 skip,所以 Mac 上跑 pytest 只跑纯逻辑,VM 上装了依赖才真跑网络。

跑法(VM 上,装好依赖后):
  pip install curl_cffi pypdf pdfplumber python-pptx faster-whisper torch
  pytest tests/ -v                      # 全跑
  pytest tests/ -v -k "not live"        # 只纯逻辑
  WATEREVENTS_TEST_PDF_URL=<url> pytest tests/test_live.py -v
"""
import importlib.util
import os

import pytest


def _has(mod: str) -> bool:
    """True if an optional heavy dep is importable — SKIP the live test when it's absent."""
    return importlib.util.find_spec(mod) is not None


needs_curl = pytest.mark.skipif(not _has("curl_cffi"), reason="curl_cffi not installed (live fetch)")
needs_pypdf = pytest.mark.skipif(not _has("pypdf"), reason="pypdf not installed (pdf text)")
needs_pptx = pytest.mark.skipif(not _has("pptx"), reason="python-pptx not installed (slides)")
needs_whisper = pytest.mark.skipif(not _has("faster_whisper"), reason="faster-whisper not installed (transcribe)")

# Default public urls (override via env when they go stale — IR urls rotate).
_PDF_URL = os.environ.get("WATEREVENTS_TEST_PDF_URL",
                          "https://www.berkshirehathaway.com/letters/2022ltr.pdf")
_PPTX_URL = os.environ.get("WATEREVENTS_TEST_PPTX_URL", "")   # no stable default deck → set via env to run
_AUDIO_URL = os.environ.get("WATEREVENTS_TEST_AUDIO_URL", "")  # set via env to run (a short public mp3)


@needs_curl
@needs_pypdf
def test_live_pdf_extract():
    from tools.pdf_extract import extract
    r = extract(_PDF_URL)
    assert r.ok, f"pdf extract failed: error={r.error} bytes={r.n_bytes}"
    assert r.n_pages > 0 and len(r.text) > 200                # a real annual report has many pages + lots of text
    print(f"\n[live pdf] pages={r.n_pages} bytes={r.n_bytes} text_chars={len(r.text)} tables={len(r.tables)}")


@needs_curl
@needs_pptx
def test_live_pptx_extract():
    if not _PPTX_URL:
        import pytest
        pytest.skip("set WATEREVENTS_TEST_PPTX_URL to run the live pptx test")
    from tools.pptx_extract import extract
    r = extract(_PPTX_URL)
    assert r.ok, f"pptx extract failed: error={r.error} bytes={r.n_bytes}"
    assert r.n_slides > 0 and len(r.text) > 50
    print(f"\n[live pptx] slides={r.n_slides} bytes={r.n_bytes} text_chars={len(r.text)}")


@needs_curl
@needs_whisper
def test_live_audio_extract():
    if not _AUDIO_URL:
        import pytest
        pytest.skip("set WATEREVENTS_TEST_AUDIO_URL to run the live audio test (GPU strongly recommended)")
    from tools.audio_extract import extract
    r = extract(_AUDIO_URL)
    assert r.ok, f"audio extract failed: error={r.error} bytes={r.n_bytes}"
    assert r.duration > 0 and len(r.transcript) > 20
    print(f"\n[live audio] dur={r.duration:.0f}s lang={r.language} bytes={r.n_bytes} chars={len(r.transcript)} segs={len(r.segments)}")
