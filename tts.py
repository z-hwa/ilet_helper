"""本地語音合成（Kokoro，ONNX，CPU 即可）。聽力練習的文本由 Gemini 產生，語音在這裡合成。"""
import random
from pathlib import Path

import numpy as np
import soundfile as sf

MODEL_DIR = Path("models")
MODEL_FILE = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_FILE = MODEL_DIR / "voices-v1.0.bin"

# speaker A、C 女聲，B、D 男聲（四個聲音彼此要分得出來）；N 是旁白（雅思聽力開頭的 Section 介紹）；英式與美式口音隨機挑一種，貼近雅思聽力的口音分佈
ACCENTS = {
    "en-us": {"A": "af_heart", "B": "am_michael", "C": "af_nova", "D": "am_adam", "N": "af_sarah"},
    "en-gb": {"A": "bf_emma", "B": "bm_george", "C": "bf_isabella", "D": "bm_lewis", "N": "bf_alice"},
}
PAUSE_SECONDS = 0.5

_engine = None


def available():
    return MODEL_FILE.exists() and VOICES_FILE.exists()


def _get_engine():
    global _engine
    if _engine is None:
        from kokoro_onnx import Kokoro

        _engine = Kokoro(str(MODEL_FILE), str(VOICES_FILE))
    return _engine


def synthesize(segments, out_path, speed=1.0, accent=None):
    """segments: [{"speaker": "A"|"B", "text": str}]。同步執行（CPU 運算），請用 to_thread 呼叫。"""
    engine = _get_engine()
    accent = accent or random.choice(list(ACCENTS))
    voices = ACCENTS[accent]

    chunks, sample_rate = [], 24000
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        voice = voices.get(str(seg.get("speaker", "A")).upper(), voices["A"])
        samples, sample_rate = engine.create(text, voice=voice, speed=speed, lang=accent)
        chunks.append(samples)
        chunks.append(np.zeros(int(sample_rate * PAUSE_SECONDS), dtype=samples.dtype))
    if not chunks:
        raise ValueError("沒有可合成的文字")

    audio = np.concatenate(chunks)
    if Path(out_path).suffix.lower() == ".wav":
        sf.write(str(out_path), audio, sample_rate)
    else:
        # 長音檔一次寫成 .ogg 會讓 libsndfile 的 Vorbis 編碼器當掉（實測 270 秒必當），所以每秒寫一塊
        with sf.SoundFile(str(out_path), "w", samplerate=sample_rate, channels=1) as f:
            for start in range(0, len(audio), sample_rate):
                f.write(audio[start:start + sample_rate])
    return Path(out_path), accent
