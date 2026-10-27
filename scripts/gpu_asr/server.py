"""Whisper on a LAN GPU box: one model.transcribe() per request, for assetizer's remote ASR.

The worker keeps everything else (16 kHz decoding, the 15-minute windows cut at quiet moments, offsets,
simplified Chinese): a request is one window of raw 16 kHz mono s16le PCM, the reply its segments with word
timestamps relative to the window. Requests take turns on the GPU; the model unloads after IDLE_UNLOAD seconds
so the card is free for other work between videos.

    systemctl restart assetizer-asr   (WSL Debian on the 4070 Ti box; unit in scripts/gpu_asr/assetizer-asr.service)

Standard library HTTP only; the env needs faster-whisper and the CUDA 12 libraries. It runs in WSL, where the same
59-minute video transcribed in 39 s against 50 s in native Windows (2026-10-26).
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("HF_HOME", os.path.expanduser("~/voice-lab/models/hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")  # huggingface.co is unreachable from the box; the model is cached

MODEL = "large-v3-turbo"
IDLE_UNLOAD = 600.0
MAX_BODY = 64 * 1024 * 1024  # a 15-minute window is ~29 MB

_lock = threading.Lock()
_model = None
_last_used = 0.0
_token = ""


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device="cuda", compute_type="float16")
    return _model


def _reaper() -> None:
    global _model
    while True:
        time.sleep(30)
        with _lock:
            if _model is not None and time.monotonic() - _last_used > IDLE_UNLOAD:
                _model = None


def transcribe(pcm: bytes, language: str | None, prompt: str | None) -> dict:
    import numpy as np
    global _last_used
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    with _lock:
        t = time.monotonic()
        segs, info = _load().transcribe(audio, language=language, word_timestamps=True, temperature=0,
                                        condition_on_previous_text=False, vad_filter=False,
                                        initial_prompt=prompt, beam_size=1)
        segments = [{"start": s.start, "end": s.end, "text": s.text,
                     "words": [{"start": w.start, "end": w.end, "word": w.word} for w in s.words or []]}
                    for s in segs]
        _last_used = time.monotonic()
    return {"segments": segments, "language": getattr(info, "language", None) or language,
            "seconds": round(_last_used - t, 2), "audio_seconds": round(len(audio) / 16000, 2)}


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return not _token or hmac.compare_digest(got, _token)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/healthz":
            return self._reply(404, {"error": "not found"})
        self._reply(200, {"ok": True, "model": MODEL, "device": "cuda", "loaded": _model is not None})

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/transcribe":
            return self._reply(404, {"error": "not found"})
        if not self._authorized():
            return self._reply(401, {"error": "bad token"})
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= MAX_BODY or n % 2:
            return self._reply(400, {"error": f"body must be 16 kHz mono s16le PCM, 1..{MAX_BODY} bytes"})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            self._reply(200, transcribe(self.rfile.read(n), q.get("language") or None, q.get("prompt") or None))
        except Exception as e:  # noqa: BLE001
            self._reply(500, {"error": f"{type(e).__name__}: {e}"[:500]})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.client_address[0]} {fmt % args}", flush=True)


def main() -> None:
    global _token
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--token-file", type=Path)
    a = p.parse_args()
    if a.token_file:
        _token = a.token_file.read_text().strip()
    threading.Thread(target=_reaper, daemon=True).start()
    print(f"assetizer gpu asr on {a.host}:{a.port}, token={'yes' if _token else 'no'}", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
