"""本地語音轉文字（faster-whisper，CPU）：口說「對話追問」用它聽懂使用者說了什麼，不花 Gemini 額度。

第一次使用會自動下載模型到 models/whisper（small.en 約 480MB）。可用 .env 的 WHISPER_MODEL 換成 base.en（較快較小）。
"""
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("asr")

MODEL_NAME = os.getenv("WHISPER_MODEL", "small.en")
MODEL_DIR = Path(os.getenv("WHISPER_DIR", "models/whisper"))
THREADS = int(os.getenv("WHISPER_THREADS", "4"))

_model = None
_lock = threading.Lock()


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            log.info("載入語音辨識模型 %s（第一次會下載）…", MODEL_NAME)
            _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=THREADS,
                                  download_root=str(MODEL_DIR))
    return _model


def preload():
    """先把模型載好（開始對話前呼叫，避免第一輪等太久）。CPU/網路動作，請用 to_thread。"""
    _load()


def transcribe(path):
    """英文語音檔 -> 文字（可能是空字串）。CPU 運算，請用 to_thread 呼叫。"""
    model = _load()
    segments, _ = model.transcribe(str(path), language="en", beam_size=1, vad_filter=True,
                                   condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()
