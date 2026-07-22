"""transcribe — audio bytes → transcript via LOCAL faster-whisper (runs on the H100), or ('', [], '') on failure.

用一句话讲完: 拿音频 bytes → 写进临时文件(whisper/ffmpeg 需要文件)→ faster-whisper 转写 → 返回
(全文, 带时间戳的 segments, 语种)。模型 lazy 加载 + 单例缓存(large-v3 很重,只加载一次常驻),有 GPU 就
用 CUDA float16(H100 上极快),没 GPU 退回 CPU int8。全本地、无 API、绝不抛。

WHY faster-whisper (not the old Gemini API): WaterEvents is LOCAL infra on the H100 — same $0/local philosophy as
pdf_extract's pypdf. faster-whisper (CTranslate2) is the fast local whisper; large-v3 is near-SOTA ASR quality.
Speaker DIARIZATION (who-said-what) is a follow-up (needs whisperx/pyannote); this returns the raw diarization-free
transcript + timestamped segments.
"""
from __future__ import annotations

import os
import tempfile
import threading

_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")          # near-SOTA; override to e.g. 'medium'/'small' for speed
_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")                # 'auto' → cuda if available else cpu
_COMPUTE = os.environ.get("WHISPER_COMPUTE", "")                  # '' → float16 on gpu, int8 on cpu

_model = None                                                   # lazy singleton — the model is heavy, load ONCE and keep resident
_model_lock = threading.Lock()                                 # serialize the one-time load across threads


def _resolve_device() -> tuple[str, str]:
    """(device, compute_type). 'auto' picks cuda when torch sees a GPU, else cpu; compute_type defaults to the fast
    dtype for each (float16 on gpu, int8 on cpu) unless WHISPER_COMPUTE overrides."""
    device = _DEVICE
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:                                        # noqa: BLE001 — no torch → assume cpu
            device = "cpu"
    compute = _COMPUTE or ("float16" if device == "cuda" else "int8")
    return device, compute


def _get_model():
    """Load the faster-whisper model ONCE (thread-safe) and cache it. Returns None if faster-whisper is absent so
    the caller degrades to an empty transcript instead of crashing."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:                                  # another thread loaded it while we waited
            return _model
        try:
            from faster_whisper import WhisperModel
            device, compute = _resolve_device()
            _model = WhisperModel(_MODEL_NAME, device=device, compute_type=compute)
            print(f"[audio_extract] whisper {_MODEL_NAME} loaded on {device}/{compute}", flush=True)
        except Exception as e:                                  # noqa: BLE001 — missing dep / no model → stay None
            print(f"[audio_extract] whisper load failed ({type(e).__name__}: {e}) — transcription disabled", flush=True)
            _model = None
    return _model


def transcribe(data: bytes) -> tuple[str, list[dict], str, float]:
    """Audio bytes → (transcript, segments, language, duration_s). ('', [], '', 0.0) on empty / model-absent /
    unparseable audio. segments = [{start, end, text}] in seconds. Writes bytes to a temp file because whisper
    decodes via ffmpeg from a path."""
    if not data:
        return "", [], "", 0.0
    model = _get_model()
    if model is None:                                          # faster-whisper unavailable → no transcription
        return "", [], "", 0.0
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".media", delete=False) as tf:
            tf.write(data)                                     # ffmpeg (inside whisper) needs a real file to decode
            tmp_path = tf.name
        # beam_size=5 = the standard quality setting; vad_filter trims long silences (dead air before a call starts).
        segments_iter, info = model.transcribe(tmp_path, beam_size=5, vad_filter=True)
        segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                    for s in segments_iter]                    # materialize the lazy generator (this runs the ASR)
        transcript = " ".join(s["text"] for s in segments).strip()
        language = getattr(info, "language", "") or ""
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        return transcript, segments, language, duration
    except Exception as e:                                     # noqa: BLE001 — decode / ASR failure → empty
        print(f"[audio_extract] transcribe failed ({type(e).__name__}: {e})", flush=True)
        return "", [], "", 0.0
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)                            # always clean up the temp file
            except Exception:                                 # noqa: BLE001
                pass
