"""test_detect — pure URL-detection logic for officeall (all office formats) + audio_extract. No deps, no network.

用一句话讲完: officeall 的 detect_format 把 URL 判成 pdf/pptx/xlsx/docx/html;maybe_office_url 对无扩展名 IR 端点
猜 pdf;audio_extract 判音视频。这里把"该判成什么"钉死,外加 Office-viewer 拆包和格式互不串。
"""
from tools.audio_extract import is_audio_url, maybe_audio_url
from tools.officeall import detect_format, is_office_url, maybe_office_url
from tools.officeall.fetch import _unwrap_office_viewer


# ---- officeall.detect: format by extension --------------------------------------------------
def test_detect_format_by_extension():
    assert detect_format("https://s.q4cdn.com/x/q3-2024.pdf") == "pdf"
    assert detect_format("https://x.com/q3-deck.pptx") == "pptx"
    assert detect_format("https://x.com/legacy.ppt") == "pptx"
    assert detect_format("https://x.com/model.xlsx") == "xlsx"
    assert detect_format("https://x.com/letter.docx") == "docx"
    assert detect_format("https://x.com/page.html") == "html"
    assert detect_format("https://x.com/photo.png") == ""        # not an office doc


def test_office_viewer_is_pptx_and_unwraps():
    viewer = "https://view.officeapps.live.com/op/view.aspx?src=https%3A%2F%2Fx.com%2Fq3.pptx"
    assert detect_format(viewer) == "pptx"
    assert is_office_url(viewer)
    assert _unwrap_office_viewer(viewer) == "https://x.com/q3.pptx"
    assert _unwrap_office_viewer("https://x.com/q3.pptx") == "https://x.com/q3.pptx"   # non-viewer passes through


def test_maybe_office_extensionless_guesses_pdf():
    # IR download endpoints with no extension → candidate, guess 'pdf' (magic confirms at fetch).
    assert maybe_office_url("https://s.q4cdn.com/static-files/abc-uuid") == (True, "pdf")
    assert maybe_office_url("https://x.com/files/doc/9981") == (True, "pdf")


def test_maybe_office_rejects_non_documents():
    for u in ("https://x.com/i.png", "https://x.com/v.mp4", "https://x.com/a.mp3",
              "https://x.com/d.json", "https://x.com/s.css"):
        assert maybe_office_url(u) == (False, ""), u


# ---- audio_extract.detect (stays separate — whisper, not an office doc) ----------------------
def test_audio_direct_extensions():
    for u in ("https://x.com/call.mp3", "https://x.com/webcast.mp4", "https://x.com/live.m3u8"):
        assert is_audio_url(u), u
    assert not is_audio_url("https://x.com/report.pdf")


def test_audio_vs_office_dont_cross_claim():
    assert is_audio_url("https://x.com/a.mp3") and not is_office_url("https://x.com/a.mp3")
    assert is_office_url("https://x.com/a.pdf") and not is_audio_url("https://x.com/a.pdf")
    assert is_office_url("https://x.com/a.pptx") and not is_audio_url("https://x.com/a.pptx")
    assert maybe_audio_url("https://x.com/media/uuid") and not is_office_url("https://x.com/media/uuid")
