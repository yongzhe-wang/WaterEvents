"""test_detect — pure URL-detection logic for all three tools. No heavy deps, no network → runs anywhere.

用一句话讲完: 每个 tool 的第一道门(is_*_url / maybe_*_url)只是纯 URL 判断,这里把"该 True 的"和"该 False 的"
都钉死 —— 直扩展名、无扩展名端点(该猜)、明显别的格式(不该猜),外加 pptx 的 Office-viewer 拆包。
"""
from tools.audio_extract import is_audio_url, maybe_audio_url
from tools.pdf_extract import is_pdf_url, maybe_pdf_url
from tools.pptx_extract import is_pptx_url, maybe_pptx_url
from tools.pptx_extract.fetch import _unwrap_office_viewer


# ---- pdf_extract.detect ------------------------------------------------------------------------
def test_pdf_direct_extension():
    assert is_pdf_url("https://s.q4cdn.com/x/files/q3-2024.pdf")
    assert is_pdf_url("https://x.com/a.PDF?download=1")           # case + query ignored
    assert not is_pdf_url("https://x.com/a.html")


def test_pdf_extensionless_endpoints_are_candidates():
    # q4cdn / IR download endpoints have NO extension but ARE pdfs → maybe must say True (fetch verifies %PDF).
    assert maybe_pdf_url("https://s.q4cdn.com/static-files/abc-123-uuid")
    assert maybe_pdf_url("https://x.com/files/doc/9981")
    assert maybe_pdf_url("https://x.com/download")


def test_pdf_obvious_nonpdf_excluded():
    for u in ("https://x.com/p.html", "https://x.com/s.aspx", "https://x.com/d.docx",
              "https://x.com/v.mp4", "https://x.com/i.png", "https://x.com/f.xml"):
        assert not maybe_pdf_url(u), u


# ---- audio_extract.detect ----------------------------------------------------------------------
def test_audio_direct_extensions():
    for u in ("https://x.com/call.mp3", "https://x.com/webcast.mp4", "https://x.com/q3.m4a",
              "https://x.com/live.m3u8", "https://x.com/a.wav"):
        assert is_audio_url(u), u
    assert not is_audio_url("https://x.com/report.pdf")


def test_audio_extensionless_candidate_but_not_other_formats():
    assert maybe_audio_url("https://x.com/media/abc-uuid")        # webcast endpoint, no extension → candidate
    assert not maybe_audio_url("https://x.com/report.pdf")
    assert not maybe_audio_url("https://x.com/deck.pptx")


# ---- pptx_extract.detect -----------------------------------------------------------------------
def test_pptx_direct_and_viewer():
    assert is_pptx_url("https://x.com/q3-deck.pptx")
    assert is_pptx_url("https://x.com/legacy.ppt")
    viewer = "https://view.officeapps.live.com/op/view.aspx?src=https%3A%2F%2Fx.com%2Fq3.pptx"
    assert is_pptx_url(viewer)
    assert not is_pptx_url("https://x.com/report.pdf")


def test_pptx_viewer_unwrap_decodes_src():
    viewer = "https://view.officeapps.live.com/op/view.aspx?src=https%3A%2F%2Fx.com%2Fq3.pptx"
    assert _unwrap_office_viewer(viewer) == "https://x.com/q3.pptx"
    # a non-viewer url passes through unchanged
    assert _unwrap_office_viewer("https://x.com/q3.pptx") == "https://x.com/q3.pptx"


def test_pptx_extensionless_candidate():
    assert maybe_pptx_url("https://x.com/files/doc/123")
    assert not maybe_pptx_url("https://x.com/report.pdf")


# ---- cross-tool: the three detectors are mutually exclusive on their own formats ---------------
def test_formats_dont_cross_claim():
    assert is_pdf_url("https://x.com/a.pdf") and not is_audio_url("https://x.com/a.pdf") and not is_pptx_url("https://x.com/a.pdf")
    assert is_audio_url("https://x.com/a.mp3") and not is_pdf_url("https://x.com/a.mp3") and not is_pptx_url("https://x.com/a.mp3")
    assert is_pptx_url("https://x.com/a.pptx") and not is_pdf_url("https://x.com/a.pptx") and not is_audio_url("https://x.com/a.pptx")
