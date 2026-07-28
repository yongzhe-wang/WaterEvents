"""transcribe — audio bytes → transcript via LOCAL faster-whisper. FAIL LOUDLY + FALLBACK: a load/decode failure is
logged with its reason (never a silent ''), and a CUDA OOM on the primary model falls back to a smaller model.

用一句话讲完: 写临时文件 → faster-whisper 转写 → (全文, segments, 语种, 时长)。模型 lazy 单例,有 GPU 用 CUDA
float16(A5000/H100),没 GPU 退 CPU int8。**改动**:whisper 缺失 / OOM / 解码失败都**大声 log 具体原因**;主模型
(large-v3)CUDA OOM 时**自动降级到备用模型**(medium)重试 —— 共享 GPU 显存紧张时不至于直接失败。返回第 5 个值
`reason`(''=成功;非空=大声的失败原因)。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
_FALLBACK_MODEL = os.environ.get("WHISPER_FALLBACK_MODEL", "medium")   # smaller model to retry on CUDA OOM
_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
_COMPUTE = os.environ.get("WHISPER_COMPUTE", "")

_models: dict = {}                                              # name → loaded WhisperModel (cache; may hold primary + fallback)
_lock = threading.Lock()


def _loud(msg: str) -> None:
    print(f"[audio_extract.transcribe] {msg}", file=sys.stderr, flush=True)


def _resolve_device() -> tuple[str, str]:
    device = _DEVICE
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:                                        # noqa: BLE001
            device = "cpu"
    return device, (_COMPUTE or ("float16" if device == "cuda" else "int8"))


def _get_model(name: str):
    """Load faster-whisper model `name` ONCE (cached). None + LOUD log if faster-whisper is absent or the load fails."""
    if name in _models:
        return _models[name]
    with _lock:
        if name in _models:
            return _models[name]
        try:
            from faster_whisper import WhisperModel
            device, compute = _resolve_device()
            _models[name] = WhisperModel(name, device=device, compute_type=compute)
            _loud(f"whisper {name} loaded on {device}/{compute}")
        except Exception as e:                                   # noqa: BLE001
            _loud(f"whisper {name} load FAILED ({type(e).__name__}: {str(e)[:100]})")
            _models[name] = None
    return _models[name]


def _run(model, path: str) -> tuple[str, list[dict], str, float]:
    """Run one model over the temp file → (text, segments, language, duration). Raises on OOM/decode error so the
    caller can fall back."""
    segments_iter, info = model.transcribe(path, beam_size=5, vad_filter=True)
    segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments_iter]
    text = " ".join(s["text"] for s in segments).strip()
    return text, segments, (getattr(info, "language", "") or ""), float(getattr(info, "duration", 0.0) or 0.0)


def transcribe(data: bytes) -> tuple[str, list[dict], str, float, str]:
    """Audio bytes → (transcript, segments, language, duration_s, reason). reason='' on success; a SPECIFIC loud
    reason on failure ('empty-bytes' / 'whisper-missing' / '<ExcType>'). On a CUDA OOM with the primary model, falls
    back to the smaller WHISPER_FALLBACK_MODEL and says so loudly. Writes bytes to a temp file (whisper decodes via
    ffmpeg from a path)."""
    if not data:
        return "", [], "", 0.0, "empty-bytes"
    model = _get_model(_MODEL_NAME)
    if model is None:
        return "", [], "", 0.0, "whisper-missing"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".media", delete=False) as tf:
            tf.write(data)
            tmp_path = tf.name
        try:
            text, segs, lang, dur = _run(model, tmp_path)
            return text, segs, lang, dur, ""
        except Exception as e:                                   # noqa: BLE001 — likely CUDA OOM → LOUD + fallback model
            name = type(e).__name__
            _loud(f"{_MODEL_NAME} transcribe FAILED ({name}: {str(e)[:90]}) → fallback {_FALLBACK_MODEL}")
            fb = _get_model(_FALLBACK_MODEL)
            if fb is None or fb is model:                       # no distinct fallback available → loud end-of-chain
                return "", [], "", 0.0, f"transcribe-failed:{name}"
            try:
                text, segs, lang, dur = _run(fb, tmp_path)
                _loud(f"RECOVERED via fallback model {_FALLBACK_MODEL}")
                return text, segs, lang, dur, f"fallback:{_FALLBACK_MODEL}"
            except Exception as e2:                             # noqa: BLE001
                _loud(f"fallback {_FALLBACK_MODEL} ALSO FAILED ({type(e2).__name__})")
                return "", [], "", 0.0, f"transcribe-failed:{name}+fallback:{type(e2).__name__}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:                                   # noqa: BLE001
                pass
