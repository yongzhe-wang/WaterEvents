"""download — a YouTube url → its best audio stream bytes via yt-dlp. FAIL LOUDLY: a failure returns a SPECIFIC
reason (never a silent b'') and logs it — age-gated / geo-blocked / private / unavailable / yt-dlp-missing.

用一句话讲完: yt-dlp 抓 `bestaudio` 流(纯音频轨,m4o/webm,不用 ffmpeg 合并)→ 读 bytes + 元数据。**改动**:
不再"失败静默 b''",而是把 yt-dlp 的失败原因(需要 cookies / 地区限制 / 私有 / 已删)**分类 + 大声 log**,并
塞进 meta['reason'],让调用方看清"为什么这条 YouTube 下不了"。cookies(YT_COOKIEFILE)可解 age/bot-gated。
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile

_COOKIEFILE = os.environ.get("YT_COOKIEFILE", "")                # cookies.txt for age/bot-gated videos (optional)


def _loud(msg: str) -> None:
    print(f"[youtube.download] {msg}", file=sys.stderr, flush=True)


def _classify(err: str) -> str:
    """yt-dlp error message → a SPECIFIC reason tag. yt-dlp puts the cause in the message text; map the common ones."""
    e = err.lower()
    if "sign in" in e or "bot" in e or "cookies" in e or "age" in e:
        return "needs-cookies-or-age-gated"
    if "private" in e:
        return "private-video"
    if "not available in your" in e or "geo" in e or "country" in e:
        return "geo-blocked"
    if "removed" in e or "unavailable" in e or "does not exist" in e or "terminated" in e:
        return "unavailable"
    if "live event" in e or "premiere" in e:
        return "live-or-premiere"
    return f"yt-dlp:{err[:70]}"


def download_audio(url: str, proxy: str | None = None) -> tuple[bytes, dict]:
    """YouTube url → (audio_bytes, meta). meta carries title/duration/id/ext on success, or {'reason': <specific>} on
    failure — and (b'', {'reason': ...}) is a LOUD failure, never a silent empty. proxy: optional proxy for IP-blocks."""
    try:
        import yt_dlp
    except Exception:                                            # noqa: BLE001
        _loud("yt-dlp not installed")
        return b"", {"reason": "yt-dlp-missing"}
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "nocheckcertificate": True, "retries": 3, "socket_timeout": 30,
        }
        if _COOKIEFILE and os.path.isfile(_COOKIEFILE):
            opts["cookiefile"] = _COOKIEFILE
        if proxy:
            opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:                                   # noqa: BLE001 — LOUD + classified reason
            reason = _classify(str(e))
            _loud(f"download FAILED ({reason}) for {url[:60]}")
            return b"", {"reason": reason}
        vid = info.get("id", "")
        matches = glob.glob(os.path.join(tmp, f"{vid}.*")) or glob.glob(os.path.join(tmp, "*"))
        if not matches:
            _loud(f"downloaded but no file found for {url[:60]}")
            return b"", {"reason": "no-file-after-download"}
        try:
            with open(matches[0], "rb") as f:
                audio = f.read()
        except Exception as e:                                   # noqa: BLE001
            _loud(f"read of downloaded file FAILED ({type(e).__name__})")
            return b"", {"reason": f"read-failed:{type(e).__name__}"}
        return audio, {
            "title": info.get("title", "") or "",
            "duration": float(info.get("duration", 0) or 0),
            "id": vid,
            "ext": os.path.splitext(matches[0])[1].lstrip("."),
        }
