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


class _TooLong(Exception):
    """Audio longer than the device can transcribe in reasonable time. Distinct from a decode failure so the caller
    reports 'too-long' (a policy decision) rather than 'transcribe-failed' (a defect) — and so the CUDA-OOM fallback
    to a smaller model is NOT attempted: a smaller model does not make an 8-hour file shorter."""


def _default_max_duration() -> float:
    """Device-aware duration cap in seconds. 0 disables the gate.

    A GPU decodes far faster than realtime, so an hour-long earnings call is fine there. faster-whisper large-v3 at
    cpu/int8 runs well UNDER realtime on this 8-core box while it also shares the box with the crawl fleet, so the
    same file would hold a worker for hours. Defaults therefore differ by an order of magnitude; both are overridable
    with WHISPER_MAX_DURATION_S."""
    env = os.environ.get("WHISPER_MAX_DURATION_S")
    if env is not None:
        return float(env)
    try:
        import torch                                              # noqa: PLC0415 — lazy, torch is a heavy import
        return 7200.0 if torch.cuda.is_available() else 900.0     # 2h on GPU, 15min on CPU
    except Exception:                                             # noqa: BLE001 — torch absent → assume CPU
        return 900.0


_MAX_DURATION_S = _default_max_duration()

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
    dur = float(getattr(info, "duration", 0.0) or 0.0)

    # DURATION GATE — checked HERE because `segments_iter` is a lazy generator: `info.duration` is known after the
    # feature/VAD pass but BEFORE any decoding work happens, so refusing here costs almost nothing while refusing
    # after the loop would cost the entire transcription.
    # WHY a gate exists at all: an IR earnings-call mp3 is routinely over an hour — the choruscall file in
    # tests/datasets/media_100 is 91.7 MB (Content-Length: 91,723,583) — and large-v3 at cpu/int8 runs at well under
    # realtime, so one such file occupies a worker for hours. fetch.py's existing cap is 300 MB and is applied AFTER
    # the download, so it stops nothing here. A worker silently blocked for hours is indistinguishable from a hang.
    # {MEASURED 2026-08-03 — a 3-event smoke over the audio stratum produced 0 completed events in 22 minutes}
    # [CONFIDENCE: CONFIRMED — the file size is from the server's own Content-Length; the stall was observed].
    # The cap is device-aware: a GPU decodes fast enough that an hour-long call is fine, a CPU is not.
    if _MAX_DURATION_S and dur > _MAX_DURATION_S:
        raise _TooLong(f"audio {dur/60:.0f}min exceeds cap {_MAX_DURATION_S/60:.0f}min "
                       f"(device={_resolve_device()[0]}; raise WHISPER_MAX_DURATION_S to override)")

    segments = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments_iter]
    text = " ".join(s["text"] for s in segments).strip()
    return text, segments, (getattr(info, "language", "") or ""), dur


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
        except _TooLong as e:                                    # policy refusal, NOT a defect → no model fallback
            _loud(f"REFUSED: {e}")
            return "", [], "", 0.0, f"too-long:{str(e)[:60]}"
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
