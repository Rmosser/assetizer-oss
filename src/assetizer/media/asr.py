"""Local ASR with faster-whisper (CPU, int8).

Used for two things only: boundary sanity (word timestamps around a proposed
cut) and best-effort subtitles. It is never the primary understanding path.
The model is loaded lazily and released after use so it never coexists with a
running agy process in memory.
"""
from __future__ import annotations

import gc
import threading
from pathlib import Path

from .. import config

_lock = threading.Lock()
_model = None


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # heavy import

        s = config.get()
        _model = WhisperModel(s.whisper_model, device="cpu", compute_type="int8",
                              download_root=str(s.whisper_root), cpu_threads=4)
    return _model


def release() -> None:
    global _model
    with _lock:
        _model = None
        gc.collect()


def transcribe(wav: Path, offset: float = 0.0, language: str | None = "zh",
               initial_prompt: str | None = None) -> dict:
    """Return {'segments': [{start,end,text}], 'words': [{start,end,word}]} in offset-shifted seconds."""
    with _lock:
        m = _load()
        segs, info = m.transcribe(str(wav), language=language, word_timestamps=True, temperature=0,
                                  condition_on_previous_text=False, vad_filter=False,
                                  initial_prompt=initial_prompt, beam_size=5)
        segments, words = [], []
        for s in segs:
            segments.append({"start": offset + s.start, "end": offset + s.end, "text": s.text})
            for w in s.words or []:
                words.append({"start": offset + w.start, "end": offset + w.end, "word": w.word})
    return {"segments": segments, "words": words, "language": getattr(info, "language", language)}
