"""ASR with faster-whisper: on a LAN GPU box when the `asr_url` setting names one, else locally (CPU, int8).

Used for two things only: boundary sanity (word timestamps around a proposed
cut) and best-effort subtitles. It is never the primary understanding path.

Each transcription runs in a child process (``python -m assetizer.media.asr``).
In the worker itself, ctranslate2's threads left ~2 GB in glibc arenas after
every video even once the model was dropped, and a long-lived worker never got
it back (2026-10-22: OOM while loading the 4th video's model); the child's exit
returns all of it. Long audio is transcribed in windows cut at quiet moments,
because faster-whisper extracts features for the whole array at once (a
94-minute video peaked at +7 GB).

With `asr_url` set (e.g. http://192.168.1.50:8790, the 4070 Ti box running
scripts/gpu_asr/server.py) each window goes there instead; the windows, offsets
and stitching stay here. When the box is off or fails mid-video, the child
falls back to the CPU model for the whole video.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from .. import config

SR = 16000
WINDOW = 900.0  # seconds of audio per transcription window
SEARCH = 30.0   # cut at the quietest moment within this many seconds of the nominal boundary
CHILD = "assetizer.media.asr"

_lock = threading.Lock()


def _read_pcm(path: Path):
    """16 kHz mono PCM wav (as produced by ffmpeg.extract_wav) → int16 numpy array."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, "expect 16k mono s16"
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16)


def windows(pcm, window: float = WINDOW, search: float = SEARCH) -> list[tuple[int, int]]:
    """Split PCM into contiguous [start, end) sample ranges of about ``window`` seconds, each cut at the quietest
    quarter second within ``search`` seconds of the nominal boundary, so a cut falls between words."""
    import numpy as np

    n, step, slack, hop = len(pcm), int(window * SR), int(search * SR), SR // 4
    cuts = [0]
    while n - cuts[-1] > step + slack:
        lo = cuts[-1] + step - slack
        seg = pcm[lo:lo + 2 * slack].astype(np.float32)
        energy = (seg[:len(seg) // hop * hop].reshape(-1, hop) ** 2).mean(axis=1)
        cuts.append(lo + int(energy.argmin()) * hop + hop // 2)
    cuts.append(n)
    return [(a, b) for a, b in zip(cuts[:-1], cuts[1:]) if b > a]


def transcribe_pcm(model, pcm, offset: float = 0.0, language: str | None = "zh",
                   initial_prompt: str | None = None) -> dict:
    """Transcribe window by window with ``model`` (a faster-whisper WhisperModel); times are offset-shifted seconds."""
    import numpy as np

    segments, words, lang = [], [], None
    for a, b in windows(pcm):
        audio = pcm[a:b].astype(np.float32) / 32768.0
        segs, info = model.transcribe(audio, language=language, word_timestamps=True, temperature=0,
                                      condition_on_previous_text=False, vad_filter=False,
                                      initial_prompt=initial_prompt, beam_size=1)
        base = offset + a / SR
        for s in segs:
            segments.append({"start": base + s.start, "end": base + s.end, "text": s.text})
            for w in s.words or []:
                words.append({"start": base + w.start, "end": base + w.end, "word": w.word})
        lang = lang or getattr(info, "language", None)
    return {"segments": segments, "words": words, "language": lang or language}


class RemoteWhisper:
    """faster-whisper's ``model.transcribe(audio, ...)`` answered by scripts/gpu_asr/server.py, which decodes with
    the same options as transcribe_pcm (large-v3-turbo, float16 on the GPU)."""

    def __init__(self, url: str, token: str | None):
        self.url, self.token = url.rstrip("/"), token
        # the worker's HTTP(S)_PROXY points at Clash; the box is on the LAN
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.opener.open(self.url + "/healthz", timeout=5) as r:
            json.load(r)

    def transcribe(self, audio, *, language=None, initial_prompt=None, **_):
        import numpy as np

        pcm = np.round(audio * 32768.0).clip(-32768, 32767).astype(np.int16).tobytes()
        query = urllib.parse.urlencode({k: v for k, v in (("language", language), ("prompt", initial_prompt)) if v})
        req = urllib.request.Request(f"{self.url}/transcribe?{query}", data=pcm, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with self.opener.open(req, timeout=900) as r:
            body = json.load(r)
        segs = [SimpleNamespace(start=s["start"], end=s["end"], text=s["text"],
                                words=[SimpleNamespace(**w) for w in s["words"]]) for s in body["segments"]]
        return segs, SimpleNamespace(language=body.get("language"))



def probe(url: str, token: str | None) -> dict:
    """The 设置 page's test of the GPU box (scripts/gpu_asr/server.py): does it answer, and does it take our token? No audio
    is sent: an empty body is refused after the token check, 401 for a wrong token and 400 otherwise."""
    from ..util import now

    url = url.rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # on the LAN, not through Clash
    out: dict = {"ok": False, "at": now(), "url": url}
    try:
        with opener.open(url + "/healthz", timeout=5) as r:
            health = json.load(r)
        req = urllib.request.Request(url + "/transcribe", data=b"", method="POST")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            opener.open(req, timeout=5).close()
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
    except (OSError, ValueError) as e:  # the error text is left out: it would repeat the address, nothing more
        return {**out, "code": "UNREACHABLE", "detail": type(e).__name__}
    if code == 401:
        return {**out, "code": "BAD_TOKEN", "model": health.get("model")}
    return {**out, "ok": True, "code": "OK", "model": health.get("model"), "loaded": bool(health.get("loaded"))}

def _command(wav: Path, out: Path, offset: float, language: str | None, initial_prompt: str | None) -> list[str]:
    cmd = [sys.executable, "-m", CHILD, str(wav), str(out), "--offset", repr(offset)]
    from .. import db

    if url := db.setting("asr_url"):
        cmd += ["--remote", url]
    if language:
        cmd += ["--language", language]
    if initial_prompt:
        cmd += ["--prompt", initial_prompt]
    return cmd


def transcribe(wav: Path, offset: float = 0.0, language: str | None = "zh",
               initial_prompt: str | None = None) -> dict:
    """Return {'segments': [{start,end,text}], 'words': [{start,end,word}]} in offset-shifted seconds."""
    from ..providers.agy import LIVE_PGIDS, LIVE_RUNS, run_of  # the worker kills these groups on shutdown, or a10's stop

    out = wav.with_name(wav.name + ".asr.json")
    with _lock:
        out.unlink(missing_ok=True)
        proc = subprocess.Popen(_command(wav, out, offset, language, initial_prompt), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        LIVE_PGIDS.add(proc.pid)
        if rid := run_of(wav):
            LIVE_RUNS[proc.pid] = rid
        try:
            _, err = proc.communicate()
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        finally:
            LIVE_PGIDS.discard(proc.pid)
            LIVE_RUNS.pop(proc.pid, None)
        if proc.returncode != 0 or not out.exists():
            how = "killed (out of memory?)" if proc.returncode == -signal.SIGKILL else f"exit {proc.returncode}"
            raise RuntimeError(f"whisper child {how}: {err.decode(errors='replace')[-800:].strip()}")
        r = json.loads(out.read_text())
        out.unlink()
    return r


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog=CHILD)
    p.add_argument("wav", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--offset", type=float, default=0.0)
    p.add_argument("--language")
    p.add_argument("--prompt")
    p.add_argument("--remote")
    a = p.parse_args(argv)
    s = config.get()
    pcm, r = _read_pcm(a.wav), None
    if a.remote:
        try:
            r = transcribe_pcm(RemoteWhisper(a.remote, s.secret("asr-token")), pcm, a.offset, a.language, a.prompt)
            r["device"] = f"remote {a.remote}"
        except (OSError, ValueError, KeyError) as e:  # urllib errors are OSErrors
            print(f"remote asr {a.remote} failed, using the CPU: {e}", file=sys.stderr)
            fallback = f"{type(e).__name__}: {e}"[:300]
    if r is None:
        from faster_whisper import WhisperModel  # heavy import

        model = WhisperModel(s.whisper_model, device="cpu", compute_type="int8",
                             download_root=str(s.whisper_root), cpu_threads=6)
        r = transcribe_pcm(model, pcm, a.offset, a.language, a.prompt)
        r["device"] = "cpu"
        if a.remote:
            r["remote_failed"] = fallback
    tmp = a.out.with_name(a.out.name + ".tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False))
    tmp.replace(a.out)


if __name__ == "__main__":
    main()
