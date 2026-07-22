"""test_results — the unified Result contract + magic gates. No heavy deps (fake bytes fail the magic gate
BEFORE any pypdf/whisper/pptx import), no network → runs anywhere.

用一句话讲完: 三个 tool 都返回一个统一 Result(.ok + .error + source)。这里钉死:非法 bytes / 非候选 URL 一定
ok=False 且给出对的 error 字符串,magic gate(%PDF- / PK\\x03\\x04)在喂进解析器前就挡住脏 bytes。
"""
from tools.audio_extract import extract_bytes as audio_bytes, extract as audio_extract, AudioResult
from tools.pdf_extract import extract_bytes as pdf_bytes, extract as pdf_extract, PdfResult
from tools.pptx_extract import extract_bytes as pptx_bytes, extract as pptx_extract, PptxResult


# ---- magic gates reject non-format bytes without touching a heavy parser -----------------------
def test_pdf_rejects_non_pdf_bytes():
    r = pdf_bytes(b"this is not a pdf")
    assert isinstance(r, PdfResult) and not r.ok and r.error == "not-a-pdf" and r.source == "bytes"


def test_pdf_rejects_empty():
    assert pdf_bytes(b"").error == "not-a-pdf"


def test_pptx_rejects_non_zip_bytes():
    r = pptx_bytes(b"not a zip")
    assert isinstance(r, PptxResult) and not r.ok and r.error == "not-a-pptx"


def test_audio_rejects_empty_bytes():
    r = audio_bytes(b"")
    assert isinstance(r, AudioResult) and not r.ok and r.error == "empty-bytes"


# ---- extract(url) short-circuits on a non-candidate url BEFORE any network ---------------------
def test_pdf_extract_rejects_nonpdf_url():
    r = pdf_extract("https://x.com/page.html")
    assert not r.ok and r.error == "not-pdf-url" and r.source == "url"


def test_audio_extract_rejects_nonaudio_url():
    r = audio_extract("https://x.com/report.pdf")
    assert not r.ok and r.error == "not-audio-url"


def test_pptx_extract_rejects_nonpptx_url():
    r = pptx_extract("https://x.com/report.pdf")
    assert not r.ok and r.error == "not-pptx-url"


# ---- Result.ok truth-table -----------------------------------------------------------------------
def test_ok_is_content_presence():
    assert PdfResult(text="real earnings text").ok
    assert PdfResult(tables=[{"columns": ["a"], "rows": [["1"]]}]).ok      # tables alone → ok
    assert not PdfResult().ok
    assert AudioResult(transcript="hello everyone").ok
    assert not AudioResult(segments=[{"start": 0, "end": 1, "text": ""}]).ok  # segments w/o transcript text → not ok
    assert PptxResult(text="slide 1 body").ok
    assert not PptxResult(slides=[{"n": 1, "title": "", "text": "", "tables": [], "notes": ""}]).ok


# ---- SSRF guard refuses private / non-http hosts (pure, no network round-trip) -----------------
def test_ssrf_guard_blocks_private_and_scheme():
    from tools.pdf_extract.fetch import fetch_bytes as pdf_fetch
    assert pdf_fetch("http://127.0.0.1/x.pdf") == b""             # loopback → refuse
    assert pdf_fetch("http://169.254.169.254/latest/meta-data") == b""  # cloud metadata → refuse
    assert pdf_fetch("file:///etc/passwd") == b""                 # non-http scheme → refuse
    assert pdf_fetch("http://localhost/x.pdf") == b""             # localhost → refuse
