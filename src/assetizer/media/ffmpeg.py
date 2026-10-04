"""ffmpeg / ffprobe wrappers. All times are Source-global seconds."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args],
                       capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise MediaError(r.stderr[-2000:])
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
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    return out


def burn_subtitles(clip: Path, ass: Path, out: Path, fonts_dir: Path | None) -> Path:
    vf = f"scale=-2:720,ass={_escape(ass)}"
    if fonts_dir:
        vf += f":fontsdir={_escape(fonts_dir)}"
    run(["-i", str(clip), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)])
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
