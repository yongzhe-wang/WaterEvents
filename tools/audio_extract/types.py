"""AudioResult — the single return shape for the whole audio_extract tool (mirrors officeall.DocResult).

用一句话讲完: 一个音频(URL 或 bytes)进来 → detect → fetch → transcribe 三步 → 汇成这一个 AudioResult 出去。
transcript 是拼好的全文,segments 是带时间戳的分段(方便对齐/引用),language 是检测到的语种。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AudioResult:
    """The verdict for ONE audio. `ok` is the single truth: True iff we transcribed real speech.

    transcript — the full transcript text (segments joined), '' if none.
    segments   — [{start, end, text}] timestamped segments (start/end in seconds), [] if none.
    language   — detected language code ('en', 'ja', …), '' on failure.
    duration   — audio length in seconds (0.0 on failure), for provenance / cost accounting.
    n_bytes    — size of the fetched audio bytes (0 on failure).
    source     — 'url' (fetched) | 'bytes' (caller-supplied) | '' (failed).
    error      — a short reason when ok=False ('not-audio-url', 'fetch-empty', 'transcribe-failed'); '' on success.
    """
    transcript: str = ""
    segments: list[dict] = field(default_factory=list)
    language: str = ""
    duration: float = 0.0
    n_bytes: int = 0
    source: str = ""
    via: str = ""                                                  # which model produced it: 'whisper' | 'fallback:medium' | ''
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when we got real transcript text."""
        return bool(self.transcript.strip())
