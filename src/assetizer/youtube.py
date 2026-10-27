"""YouTube acquisition through yt-dlp with classified failures.

Credentials come from the user's own Chrome through CookieCloud, else from the
persistent server Chromium profile the user logs in to through the noVNC remote
screen. We never prompt for login unless the failure really is an auth problem
(research/youtube-session-reliability).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import config, cookies
from .cookies import ytdlp_args
from .util import now


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
    if "live event will begin" in t or "premieres in" in t:  # a scheduled live or premiere: nothing to fetch yet
        return "LIVE_UPCOMING"
    if "http error 429" in t or "too many requests" in t:
        return "RATE_LIMITED"
    if "requested format is not available" in t or "no video formats" in t:
        return "FORMAT_UNAVAILABLE"
    if "video unavailable" in t or "has been removed" in t:
        return "SOURCE_UNAVAILABLE"
    if "timed out" in t or "connection" in t or "network" in t or "name resolution" in t or "unexpected_eof" in t:
        return "NETWORK"
    return "UNKNOWN"


def _base(s: config.Settings) -> list[str]:
    """yt-dlp without the login; callers add cookies.ytdlp_args() (CookieCloud, else the server Chromium)."""
    cmd = [s.ytdlp_bin, "--ignore-config", "--no-playlist", "--no-cache-dir", "--socket-timeout", "30"]
    if s.deno_bin:
        cmd += ["--js-runtimes", f"deno:{s.deno_bin}"]
    return cmd


def metadata(url: str) -> dict:
    s = config.get()
    with ytdlp_args() as auth:
        r = subprocess.run(_base(s) + auth + ["-J", "--skip-download", "--", url], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise AcquireError(classify(r.stderr), r.stderr.strip().splitlines()[-1][:300] if r.stderr.strip() else "")
    info = json.loads(r.stdout)
    keep = ("id", "title", "channel", "channel_id", "uploader", "duration", "upload_date", "live_status",
            "webpage_url", "description", "thumbnail", "language", "timestamp", "was_live", "availability")
    return {k: info.get(k) for k in keep}


# the 机房 video test: what each outcome says about the CookieCloud login
VERDICT = {
    "member_ok": "能读到这个会员视频：YouTube 认这份登录态，会员权限有效。",
    "login_ok": "能读到这个视频，YouTube 认这份登录态。想确认会员权限，换一个会员视频再测。",
    "not_signed_in": "CookieCloud 里的 youtube.com cookies 没有登录信息：Chrome 里的 YouTube 没有登录，或插件还没同步。",
    "cookies_rejected": "YouTube 不认这份登录态：cookies 已在浏览器里轮换失效。在 Chrome 里刷新一次 YouTube，在插件里点「立即同步」，再测。",
    "not_member": "这份登录态没有这个频道的会员权限：确认 Chrome 里登录的是买了会员的账号，在插件里点「立即同步」，再测。",
    "bot_challenge": "YouTube 要求人机验证，说明它没把这份 cookies 当成已登录。在 Chrome 里刷新一次 YouTube，在插件里点「立即同步」，再测。",
    "age_restricted": "这个视频需要年龄验证：登录的账号要先在 YouTube 完成年龄验证。",
    "private": "这是私享视频，只有受邀的账号能看。换一个视频测试。",
    "unavailable": "这个视频不存在、已删除，或在服务器所在地区看不了。换一个视频测试。",
    "rate_limited": "YouTube 暂时限制了服务器的请求（HTTP 429）：等几分钟再测。",
    "network": "服务器连不上 YouTube：稍后再测。",
    "error": "测试没有完成，原因见 YouTube 的原话。",
}


def _video_verdict(r: subprocess.CompletedProcess) -> tuple[str, dict | None, str | None]:
    """(verdict, video, YouTube's own words) for one `yt-dlp -J` run with CookieCloud cookies."""
    lines = (r.stderr or "").strip().splitlines()
    said = next((ln for ln in reversed(lines) if ln.startswith(("ERROR", "WARNING"))), lines[-1] if lines else "")[:300] or None
    t = (r.stderr or "").lower()
    # yt-dlp: "The provided YouTube account cookies are no longer valid. They have likely been rotated ..."
    rejected = "cookies are no longer valid" in t
    if r.returncode == 0:
        try:
            info = json.loads(r.stdout)
        except ValueError:
            return "error", None, said
        video = {k: info.get(k) for k in ("id", "title", "channel", "availability", "duration", "live_status")}
        if rejected:
            return "cookies_rejected", video, said
        return ("member_ok" if video["availability"] == "subscriber_only" else "login_ok"), video, None
    for verdict, needles in (("cookies_rejected", ("cookies are no longer valid",)),
                             ("not_member", ("members-only", "join this channel", "members on level")),
                             ("bot_challenge", ("not a bot",)),
                             ("age_restricted", ("confirm your age", "age-restricted")),
                             ("private", ("private video",)),
                             ("rate_limited", ("http error 429", "too many requests")),
                             ("unavailable", ("video unavailable", "has been removed", "not available in your country")),
                             ("network", ("timed out", "connection", "name resolution"))):
        if any(n in t for n in needles):
            return verdict, None, said
    return "error", None, said


def probe_video(url: str) -> dict:
    """Read one video's metadata with the CookieCloud login only: never the server Chromium, never media. A
    members-only video that reads proves the membership login works. Nothing returned carries a cookie value."""
    vid = video_id(url)
    if not vid:
        raise ValueError("not a YouTube video")
    out: dict = {"ok": False, "url": f"https://www.youtube.com/watch?v={vid}", "video": None, "detail": None,
                 "code": None, "cookies": None, "at": now()}
    try:
        with cookies.cookiecloud_file() as (path, info):
            out["cookies"] = info
            if not info["signed_in"]:
                verdict = "not_signed_in"  # YouTube would only see a signed-out visitor: no need to ask it
            else:
                r = subprocess.run(_base(config.get()) + ["--cookies", path, "-J", "--skip-download", "--", out["url"]],
                                   capture_output=True, text=True, timeout=150)
                verdict, out["video"], out["detail"] = _video_verdict(r)
    except cookies.CookieError as e:
        verdict, out["code"], out["detail"] = "cookiecloud", e.code, e.detail
    except subprocess.TimeoutExpired:
        verdict, out["detail"] = "network", "150 秒内没有结果"
    out.update(verdict=verdict, ok=verdict in ("member_ok", "login_ok"),
               message=cookies.fix(out["code"]) if verdict == "cookiecloud" else VERDICT[verdict])
    return out


def download(url: str, dest_dir: Path) -> Path:
    """Download ≤1080p video+audio as mkv into dest_dir/source.mkv."""
    s = config.get()
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "source.mkv"
    if out.exists() and out.stat().st_size > 0:
        return out
    # §R6: prefer h264/aac ≤1080p (avoids CPU-heavy AV1/VP9 decode and 10-bit sources)
    with ytdlp_args() as auth:
        cmd = _base(s) + auth + ["-S", "res:1080,vcodec:h264,acodec:aac", "-f", "bv*+ba/b",
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
