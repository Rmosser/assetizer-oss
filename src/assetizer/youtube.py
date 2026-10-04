"""YouTube acquisition through yt-dlp with classified failures.

Credentials come from the persistent server Chromium profile the user logs in
to through the noVNC remote screen. We never prompt for login unless the
failure really is an auth problem (research/youtube-session-reliability).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import config


class AcquireError(RuntimeError):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind

    @property
    def needs_human(self) -> bool:
        return self.kind in {"AUTH_REQUIRED", "BOT_CHALLENGE", "COOKIE_UNREADABLE"}


VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str | None:
    m = VIDEO_ID.search(url)
    if m:
        return m[1]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    return None


def classify(stderr: str) -> str:
    t = stderr.lower()
    if "could not copy" in t and "cookie" in t or "failed to decrypt" in t or "cookies database" in t:
        return "COOKIE_UNREADABLE"
    if "confirm you" in t and "bot" in t:
        return "BOT_CHALLENGE"
    if "members-only" in t or "join this channel" in t or "private video" in t or "sign in to confirm your age" in t:
        return "AUTH_REQUIRED"
    if "http error 429" in t or "too many requests" in t:
        return "RATE_LIMITED"
    if "requested format is not available" in t or "no video formats" in t:
        return "FORMAT_UNAVAILABLE"
    if "video unavailable" in t or "has been removed" in t:
        return "SOURCE_UNAVAILABLE"
    if "timed out" in t or "connection" in t or "network" in t or "name resolution" in t:
        return "NETWORK"
    return "UNKNOWN"


def _base(s: config.Settings) -> list[str]:
    cmd = [s.ytdlp_bin, "--ignore-config", "--no-playlist", "--no-cache-dir", "--socket-timeout", "30"]
    if s.deno_bin:
        cmd += ["--js-runtimes", f"deno:{s.deno_bin}"]
    if s.browser_profile:
        cmd += ["--cookies-from-browser", f"chromium:{s.browser_profile}"]
    return cmd


def metadata(url: str) -> dict:
    s = config.get()
    r = subprocess.run(_base(s) + ["-J", "--skip-download", "--", url], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise AcquireError(classify(r.stderr), r.stderr.strip().splitlines()[-1][:300] if r.stderr.strip() else "")
    info = json.loads(r.stdout)
    keep = ("id", "title", "channel", "channel_id", "uploader", "duration", "upload_date", "live_status",
            "webpage_url", "description", "thumbnail", "language", "timestamp")
    return {k: info.get(k) for k in keep}


def download(url: str, dest_dir: Path) -> Path:
    """Download ≤1080p video+audio as mkv into dest_dir/source.mkv."""
    s = config.get()
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "source.mkv"
    if out.exists() and out.stat().st_size > 0:
        return out
    # §R6: prefer h264/aac ≤1080p (avoids CPU-heavy AV1/VP9 decode and 10-bit sources)
    cmd = _base(s) + ["-S", "res:1080,vcodec:h264,acodec:aac", "-f", "bv*+ba/b",
                      "--merge-output-format", "mkv", "--remux-video", "mkv",
                      "--retries", "5", "--fragment-retries", "5", "--no-part",
                      "-o", str(dest_dir / "source.%(ext)s"), "--", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
    (dest_dir / "yt-dlp.log").write_text(r.stdout[-20000:] + "\n--- stderr ---\n" + r.stderr[-20000:])
    if not out.exists():
        alt = sorted(p for p in dest_dir.glob("source.*") if p.suffix in (".mp4", ".webm", ".mkv") and p.stat().st_size > 0)
        if r.returncode == 0 and alt:
            alt[0].rename(out)
    if r.returncode != 0 or not out.exists():
        raise AcquireError(classify(r.stderr), r.stderr.strip().splitlines()[-1][:300] if r.stderr.strip() else "")
    return out
