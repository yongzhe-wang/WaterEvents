"""test_results — the unified Result contract + SSRF guards for officeall + audio_extract. No heavy deps, no network.

用一句话讲完: officeall 返回 DocResult(.ok/.error/format),audio 返回 AudioResult。这里钉死:空 bytes / 非候选
URL 一定 ok=False 且给对的 error;SSRF guard 拒私网/非 http。docling 缺失时 extract_bytes 报 'docling-unavailable'
而不是崩。
"""
from tools.audio_extract import extract as audio_extract, extract_bytes as audio_bytes, AudioResult
from tools.officeall import extract as office_extract, extract_bytes as office_bytes, DocResult


# ---- officeall Result contract ---------------------------------------------------------------
def test_office_empty_bytes():
    r = office_bytes(b"")
    assert isinstance(r, DocResult) and not r.ok and r.error == "empty-bytes" and r.source == "bytes"


def test_office_extract_rejects_non_office_url():
    r = office_extract("https://x.com/photo.png")
    assert not r.ok and r.error == "not-office-url" and r.source == "url"


def test_office_docresult_ok_is_content_presence():
    assert DocResult(text="# clean markdown\nreal content").ok
    assert DocResult(tables=[{"columns": ["a"], "rows": [["1"]]}]).ok       # tables alone → ok
    assert not DocResult().ok
    assert not DocResult(format="pdf", n_pages=5).ok                        # metadata w/o content → not ok


# ---- audio Result contract -------------------------------------------------------------------
def test_audio_empty_and_nonaudio():
    assert audio_bytes(b"").error == "empty-bytes"
    assert audio_extract("https://x.com/report.pdf").error == "not-audio-url"
    assert AudioResult(transcript="hello everyone").ok
    assert not AudioResult(segments=[{"start": 0, "end": 1, "text": ""}]).ok


# ---- SSRF guards refuse private / non-http hosts (pure, no network round-trip) ---------------
def test_office_ssrf_guard():
    # fetch FAILS LOUDLY: (b'', reason) with a SPECIFIC reason, never a silent (b'', '').
    from tools.officeall.fetch import fetch_bytes
    b, reason = fetch_bytes("http://127.0.0.1/x.pdf")
    assert b == b"" and reason == "ssrf-blocked"
    assert fetch_bytes("http://169.254.169.254/latest/meta-data") == (b"", "ssrf-blocked")   # cloud metadata
    assert fetch_bytes("file:///etc/passwd") == (b"", "bad-scheme")                          # non-http scheme
    assert fetch_bytes("http://localhost/x.pdf") == (b"", "ssrf-blocked")


def test_audio_ssrf_guard():
    # audio fetch also FAILS LOUDLY: (b'', reason).
    from tools.audio_extract.fetch import fetch_bytes
    assert fetch_bytes("http://127.0.0.1/x.mp3") == (b"", "ssrf-blocked")
    assert fetch_bytes("file:///etc/passwd") == (b"", "bad-scheme")
