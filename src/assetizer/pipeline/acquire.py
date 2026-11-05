"""RESOLVE → PLAYBOOK → ACQUIRE → CHUNK."""
from __future__ import annotations

import math
import shutil
import subprocess
import threading
from pathlib import Path

from .. import config, db, knobs, youtube
from ..media import ffmpeg
from ..util import fmt_tc, now, read_json, sha256_file, write_json
from .context import RunContext, Waiting

class _Gate:
    """At most `limit()` holders at once. The limit is read on every wait, so a change made in the 机房 applies to
    the next download (a download under way is never cut off when it is lowered)."""

    def __init__(self, limit):
        self.limit, self.n, self.cv = limit, 0, threading.Condition()

    def __enter__(self):
        with self.cv:
            while self.n >= self.limit():
                self.cv.wait(timeout=30)  # the other process (web) may have raised the limit: look again
            self.n += 1

    def __exit__(self, *exc):
        with self.cv:
            self.n -= 1
            self.cv.notify_all()


# Parallel runs share one egress: 7 downloads at once through it got their TLS connections cut mid-stream
# ("UNEXPECTED_EOF_WHILE_READING", 5 of 7 failed, prodbox 2026-10-25), so downloads queue here while the rest
# of each run goes on in parallel: setting `download_slots` (1–4, default 2).
DOWNLOADS = _Gate(lambda: knobs.get("download_slots"))
# what yt-dlp met that no video can help: wait for the fix (retried every 30 minutes) rather than fail the issue
WAITS = {"TOOL_OUTDATED": "YTDLP_OUTDATED", "YOUTUBE_REFUSED": "YOUTUBE_REFUSED"}


def _hours_since(iso: str | None) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return float("inf")


def resolve(ctx: RunContext) -> dict:
    src = ctx.source
    if src.get("duration") and src.get("youtube_channel_id"):
        return {"cached": True}
    try:
        info = youtube.metadata(src["url"] or src["id"])
    except youtube.AcquireError as e:
        if e.needs_human:
            raise Waiting("YOUTUBE_AUTH", str(e))
        if e.kind == "LIVE_UPCOMING":  # yt-dlp refuses a scheduled live outright instead of reporting is_upcoming
            raise Waiting("LIVE_IN_PROGRESS", "直播尚未开始，稍后自动重试")
        if e.kind in ("NETWORK", "RATE_LIMITED"):  # an outage or YouTube holding back: not this video — later
            raise Waiting("NETWORK", str(e))
        if e.kind in WAITS:
            raise Waiting(WAITS[e.kind], str(e))
        raise
    except subprocess.TimeoutExpired as e:  # yt-dlp hung on the network
        raise Waiting("NETWORK", f"读取视频信息超时：{e}")
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise Waiting("LIVE_IN_PROGRESS", "直播尚未结束，稍后自动重试")
    channel = db.one("SELECT * FROM channels WHERE youtube_channel_id=?", info.get("channel_id"))
    cfg = db.setting(f"ingest:{channel['id']}") if channel else None
    intake = cfg and cfg.get("enabled") and db.one("SELECT 1 FROM events WHERE run_id=? AND kind='ingest'", ctx.id)
    if intake and info.get("duration") is None and _hours_since(ctx.run["created_at"]) < 24:
        # a live just over, a premiere not yet on: no length yet — judged once it has one, not taken for too short
        raise Waiting("LIVE_IN_PROGRESS", "还读不到视频时长（直播刚结束或首播还没开始），稍后再判断")
    if intake and info.get("duration") is not None and info["duration"] < 60 * cfg.get("min_minutes", 10):
        raise RuntimeError(f"SKIPPED_SHORT: {info.get('duration')}s below channel minimum")
    db.exec("""UPDATE sources SET youtube_channel_id=?, channel_title=?, title=?, published_at=?, duration=?, info=?,
               channel_id=COALESCE(channel_id, ?), updated_at=? WHERE id=?""",
            info.get("channel_id"), info.get("channel"), info.get("title"), info.get("upload_date"),
            info.get("duration"), db.j(info), channel["id"] if channel else None, now(), src["id"])
    ctx.reload_source()
    return {"title": info.get("title"), "channel": info.get("channel"), "duration": info.get("duration"),
            "matched_channel": channel["id"] if channel else None}


def playbook(ctx: RunContext) -> dict:
    """Bind the playbook at run start (not enqueue time)."""
    if ctx.run.get("playbook_id"):
        row = db.one("SELECT * FROM playbooks WHERE id=?", ctx.run["playbook_id"])
    else:
        row = None
        cid = ctx.source.get("channel_id")
        if cid:
            row = db.one("SELECT * FROM playbooks WHERE channel_id=? AND status='ACTIVE' ORDER BY version DESC LIMIT 1", cid)
            if row is None:
                # a channel still in tuning: its videos are samples, run with the newest draft (never delivered before 定稿)
                row = db.one("SELECT * FROM playbooks WHERE channel_id=? AND status='NEEDS_CONFIRMATION' ORDER BY version DESC LIMIT 1", cid)
                if row is not None:
                    db.exec("INSERT OR IGNORE INTO samples(channel_id, source_id, added_at) VALUES (?,?,?)", cid, ctx.source["id"], now())
        if row is None:
            row = db.one("SELECT * FROM playbooks WHERE channel_id IS NULL AND status='ACTIVE' ORDER BY version DESC LIMIT 1")
        db.exec("UPDATE runs SET playbook_id=? WHERE id=?", row["id"], ctx.id)
        ctx.run["playbook_id"] = row["id"]
    ctx.playbook_row = row
    ctx.playbook = db.unj(row["data"], {})
    ctx.save("playbook.json", {"id": row["id"], "channel_id": row["channel_id"], "version": row["version"], **ctx.playbook})
    return {"playbook_id": row["id"], "channel": row["channel_id"] or "universal", "version": row["version"]}


def _free_bytes(path: Path) -> int:
    st = shutil.disk_usage(path)
    return st.free


def acquire(ctx: RunContext) -> dict:
    s = config.get()
    src = ctx.source
    if src.get("media_state") == "CACHED" and src.get("media_path") and Path(src["media_path"]).exists():
        if sha256_file(Path(src["media_path"])) == src["media_sha256"]:
            return {"cached": True, "bytes": src["media_bytes"]}
    est = (src.get("duration") or 3600) * 0.25e6
    need = max(15e9, 6 * est)
    if _free_bytes(s.home) < need:
        raise Waiting("DISK_LOW", f"剩余空间不足 {need / 1e9:.0f} GB")
    dest = s.media_dir / src["id"]
    try:
        with DOWNLOADS:
            p = youtube.download(src["url"] or src["id"], dest)
    except youtube.AcquireError as e:
        if e.needs_human:
            raise Waiting("YOUTUBE_AUTH", str(e))
        if e.kind in ("NETWORK", "RATE_LIMITED"):  # the connection kept dropping, or YouTube held back: resume it later
            raise Waiting("NETWORK", str(e))
        if e.kind in WAITS:
            raise Waiting(WAITS[e.kind], str(e))
        raise
    except subprocess.TimeoutExpired as e:  # a download throttled past its time: not this video's fault — again later
        raise Waiting("NETWORK", f"下载超时：{e}")
    pr = ffmpeg.probe(p)
    v = [x for x in pr["streams"] if x["codec_type"] == "video"]
    height = v[0].get("height") if v else 0
    if not height or height < 720:
        raise RuntimeError(f"DEGRADED_FORMAT: height={height}")
    digest = sha256_file(p)
    db.exec("""UPDATE sources SET media_path=?, media_sha256=?, media_bytes=?, media_height=?, media_state='CACHED',
               duration=COALESCE(?, duration), updated_at=? WHERE id=?""",
            str(p), digest, p.stat().st_size, height, float(pr["format"]["duration"]), now(), src["id"])
    ctx.reload_source()
    return {"bytes": p.stat().st_size, "height": height, "sha256": digest}


def _proxy(ctx: RunContext, pid: str, offset: float, length: float) -> tuple[Path, dict]:
    """The model proxy for one chunk, kept with the source media so later runs of the same video
    (other playbook versions) reuse it instead of re-encoding; removed with the media by retention."""
    s = config.get()
    sha = ctx.source.get("media_sha256")
    cache = s.media_dir / ctx.source["id"] / "proxies"
    name = f"{pid}_{offset:.0f}_{length:.0f}_{s.chunk_minutes}_{s.chunk_overlap_minutes}"
    mp4, meta = cache / f"{name}.mp4", cache / f"{name}.json"
    from .. import providers
    limit = getattr(providers.get(), "media_size_limit", ffmpeg.VIEW_FILE_LIMIT)
    # a proxy made under an older, larger agy limit is encoded again
    if sha and mp4.exists() and meta.exists() and (limit is None or mp4.stat().st_size <= limit):
        m = read_json(meta)
        if m.get("media_sha256") == sha:
            return mp4, m.get("encoding") or {}
    path, enc = ffmpeg.fit_proxy(ctx.media, offset, length, ctx.path("work", "chunks", f"{pid}.mp4"), limit=limit)
    if not sha:
        return path, enc
    cache.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), mp4)
    write_json(meta, {"media_sha256": sha, "encoding": enc, "offset": offset, "length": length})
    return mp4, enc


def chunk(ctx: RunContext) -> dict:
    s = config.get()
    dur = ctx.duration
    plan = ctx.load("machine/chunk-plan.json")
    if plan and all(Path(c["path"]).exists() for c in plan["chunks"]):
        return {"chunks": len(plan["chunks"]), "cached": True}
    size, overlap = s.chunk_minutes * 60, s.chunk_overlap_minutes * 60
    if dur <= size + 120:
        starts = [0.0]
    else:
        step = size - overlap
        starts = [i * step for i in range(math.ceil((dur - overlap) / step))]
    chunks = []
    for i, st in enumerate(starts):
        pid = f"CH{i + 1:02}"
        # model proxy: 1 fps, full resolution, re-encoded → exact offset and < view_file limit
        offset = st
        length = dur - offset if len(starts) == 1 else min(size, dur - offset)
        path, enc = _proxy(ctx, pid, offset, length)
        length = ffmpeg.duration(path)
        chunks.append({"part_id": pid, "path": str(path), "offset": offset, "length": length, "encoding": enc,
                       "source_start": fmt_tc(offset), "source_end": fmt_tc(offset + length)})
    ctx.save("machine/chunk-plan.json", {"chunks": chunks, "duration": dur})
    return {"chunks": len(chunks), "offsets": [c["offset"] for c in chunks]}
