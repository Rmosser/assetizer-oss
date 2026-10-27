"""ffmpeg / ffprobe wrappers. All times are Source-global seconds."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


def run(args: list[str], nice: int = 0, **kw) -> subprocess.CompletedProcess:
    """Run ffmpeg; the last arg is the output, written to a temp name and renamed on success
    so a killed encode never leaves a truncated file that later stages would reuse. `nice` > 0 runs it at
    lower CPU priority (background work that must not slow production)."""
    out = Path(args[-1])
    tmp = out.with_name(f".{out.stem}.part{out.suffix}")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *args[:-1], str(tmp)]
    r = subprocess.run(["nice", "-n", str(nice), *cmd] if nice else cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError(r.stderr[-2000:])
    tmp.replace(out)
    return r


def probe(p: Path) -> dict:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)])
    return json.loads(out)


def duration(p: Path) -> float:
    return float(probe(p)["format"]["duration"])


def cut_copy(src: Path, start: float, length: float, out: Path) -> Path:
    """Fast stream-copy window (for model input; keyframe-aligned start is fine)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{max(0, start):.3f}", "-i", str(src), "-t", f"{length:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", str(out)])
    return out


def render_clip(src: Path, start: float, end: float, out: Path, crf: int = 22) -> Path:
    """Frame-accurate re-encode from Original Source-global timestamps."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?",
         "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-profile:v", "high", "-g", "60",
         "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(out)])
    return out


def playback_copy(src: Path, out: Path, height: int = 360) -> Path:
    """The whole video, small enough to keep for in-page playback: 360p, 25 fps, mono AAC, index first so the
    browser can start and seek before it has the file. It runs beside production, so at low priority and on two
    decode and two encode threads: on devbox (2026-10-27) a 28.8-minute 1080p original took 4.2 minutes at
    2.8 cores and came out 41.5 MB (1.44 MB a minute); with only the encoder capped it took 2.7 minutes but 6.7 cores."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-threads", "2", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale=-2:{height}", "-filter_threads", "1",
         "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-profile:v", "main", "-g", "100",
         "-threads", "2", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(out)], nice=10)
    return out


def burn_subtitles(clip: Path, ass: Path, out: Path, fonts_dir: Path | None) -> Path:
    vf = f"scale=-2:720,ass={_escape(ass)}"
    if fonts_dir:
        vf += f":fontsdir={_escape(fonts_dir)}"
    run(["-i", str(clip), "-vf", vf, "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-profile:v", "high", "-g", "60", "-c:a", "copy", "-movflags", "+faststart", str(out)])
    return out


def _escape(p: Path) -> str:
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def frame(src: Path, t: float, out: Path, width: int = 1920) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{max(0, t):.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", str(out)])
    return out


def extract_wav(src: Path, out: Path, start: float | None = None, length: float | None = None) -> Path:
    args = []
    if start is not None:
        args += ["-ss", f"{max(0, start):.3f}"]
    args += ["-i", str(src)]
    if length is not None:
        args += ["-t", f"{length:.3f}"]
    run([*args, "-vn", "-ac", "1", "-ar", "16000", str(out)])
    return out


def silences(p: Path, noise_db: int = -35, min_dur: float = 0.3) -> list[tuple[float, float]]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(p), "-vn", "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
                       capture_output=True, text=True)
    out, start = [], None
    for line in r.stderr.splitlines():
        if m := re.search(r"silence_start: ([\d.]+)", line):
            start = float(m[1])
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            out.append((start, float(m[1])))
            start = None
    return out


def qa(p: Path) -> dict:
    z = probe(p)
    v = [s for s in z["streams"] if s["codec_type"] == "video"]
    a = [s for s in z["streams"] if s["codec_type"] == "audio"]
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"], capture_output=True, text=True)
    return {
        "ok": bool(v) and bool(a) and r.returncode == 0,
        "duration": float(z["format"].get("duration", 0)),
        "bytes": p.stat().st_size,
        "video_codec": v[0]["codec_name"] if v else None,
        "width": v[0].get("width") if v else None,
        "height": v[0].get("height") if v else None,
        "audio_codec": a[0]["codec_name"] if a else None,
        "decode_stderr": r.stderr[-500:],
    }


def keyframe_before(src: Path, t: float) -> float:
    """Return the pts of the last video keyframe at or before ``t`` (stream-copy cut point)."""
    if t <= 0:
        return 0.0
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                          "-read_intervals", f"{max(0, t - 20):.3f}%{t + 0.5:.3f}",
                          "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(src)],
                         capture_output=True, text=True).stdout
    pts = [float(x) for x in out.split() if x.strip() and x.strip() != "N/A"]
    before = [p for p in pts if p <= t + 0.001]
    return max(before) if before else (min(pts) if pts else t)


def render_window(src: Path, start: float, length: float, out: Path) -> Path:
    """Re-encoded 720p window with an exact zero point (boundary model input)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    # 2 fps keeps second-level boundary precision; full resolution keeps tickers legible
    run(["-ss", f"{max(0, start):.3f}", "-i", str(src), "-t", f"{length:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?", "-vf", "fps=2,scale=-2:'min(1080,ih)'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-g", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(out)])
    if out.stat().st_size > VIEW_FILE_LIMIT:  # very long unit: trade frame rate and resolution for the limit
        for crf, h, abr in PROXY_LADDER[2:]:
            model_proxy(src, start, length, out, fps=1, crf=crf, max_height=h, audio_kbps=abr)
            if out.stat().st_size <= VIEW_FILE_LIMIT:
                break
    return out


def sharpest_frame(src: Path, t: float, out: Path, spread: float = 1.0, n: int = 5) -> tuple[Path, float]:
    """Grab n frames in [t-spread, t+spread]; keep the sharpest (Laplacian variance)."""
    import numpy as np
    from PIL import Image

    tmp = out.parent / (out.stem + "-cand")
    tmp.mkdir(parents=True, exist_ok=True)
    best: tuple[float, Path, float] | None = None
    for i in range(n):
        tt = max(0.0, t - spread + 2 * spread * i / max(1, n - 1))
        p = frame(src, tt, tmp / f"{i}.jpg")
        g = np.asarray(Image.open(p).convert("L").resize((640, 360)), dtype=np.float32)
        lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
        score = float(lap.var())
        if best is None or score > best[0]:
            best = (score, p, tt)
    assert best
    best[1].replace(out)
    for p in tmp.glob("*.jpg"):
        p.unlink()
    tmp.rmdir()
    return out, best[2]


def moov_first(p: Path) -> bool:
    """True when the moov atom precedes mdat (progressive playback / faststart)."""
    with p.open("rb") as f:
        pos = 0
        size_total = p.stat().st_size
        while pos < size_total:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                return False
            size = int.from_bytes(hdr[:4], "big")
            kind = hdr[4:8]
            if size == 1:
                size = int.from_bytes(hdr[8:16], "big")
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            if size < 8:
                return False
            pos += size
    return False


# Largest media file agy reads. agy 1.2 refused view_file above 100 MB; agy 1.4.5 refuses above ~25 MB
# (2026-10-22, 1-minute clips: 24.5 MB read, 27.4 MB refused; a 26 MB 25-minute chunk proxy was refused).
# The media self-test (providers/agy.py) reads a clip near this size after every agy update.
VIEW_FILE_LIMIT = 24 * 1024 * 1024
# (CRF, max height, audio kbps), tried in order until the proxy fits: compression first, then resolution
# (chart text), then audio. A 25-minute chunk fits at the second step; a 50-minute chunk needs the later ones.
PROXY_LADDER = ((32, 1080, 48), (36, 1080, 48), (34, 720, 48), (38, 720, 32), (38, 540, 32), (42, 540, 24))


def model_proxy(src: Path, start: float, length: float, out: Path, fps: float = 1.0, crf: int = 30,
                max_height: int = 1080, audio_kbps: int = 48) -> Path:
    """Low-frame-rate model input. Gemini samples video at ~1 fps, so resolution (chart text)
    matters and frame rate does not. Frame-accurate start: offset == ``start`` exactly."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{max(0, start):.3f}", "-i", str(src), "-t", f"{length:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?",
         "-vf", f"fps={fps},scale=-2:'min({max_height},ih)'", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", str(crf), "-g", str(int(max(1, fps * 60))), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "1", "-movflags", "+faststart", str(out)])
    return out


_DEFAULT_LIMIT = object()


def fit_proxy(src: Path, start: float, length: float, out: Path, fps: float = 1.0,
              limit: "int | None | object" = _DEFAULT_LIMIT) -> tuple[Path, dict]:
    """Encode a proxy under ``limit``, walking PROXY_LADDER. ``limit=None`` (a provider that takes frames from the
    proxy rather than the file itself) keeps the first, sharpest rung: one decode of the source instead of up to six."""
    if limit is _DEFAULT_LIMIT:
        limit = VIEW_FILE_LIMIT
    # measured 2026-10-05: 50 min @1 fps CRF30 GOP10 = 131 MB (keyframes dominate) → long GOP first
    for crf, h, abr in PROXY_LADDER:
        model_proxy(src, start, length, out, fps=fps, crf=crf, max_height=h, audio_kbps=abr)
        if limit is None or out.stat().st_size <= limit:
            return out, {"fps": fps, "crf": crf, "height": h, "audio_kbps": abr, "bytes": out.stat().st_size}
    raise MediaError(f"proxy over view_file limit: {out.stat().st_size} bytes")
