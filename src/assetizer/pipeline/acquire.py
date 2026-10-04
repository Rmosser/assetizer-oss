"""RESOLVE → PLAYBOOK → ACQUIRE → CHUNK."""
from __future__ import annotations

import math
import shutil
from pathlib import Path

from .. import config, db, youtube
from ..media import ffmpeg
from ..util import fmt_tc, now, sha256_file
from .context import RunContext, Waiting


def resolve(ctx: RunContext) -> dict:
    src = ctx.source
    if src.get("duration") and src.get("youtube_channel_id"):
        return {"cached": True}
    try:
        info = youtube.metadata(src["url"] or src["id"])
    except youtube.AcquireError as e:
        if e.needs_human:
            raise Waiting("YOUTUBE_AUTH", str(e))
        raise
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise Waiting("LIVE_IN_PROGRESS", "直播尚未结束，稍后自动重试")
    channel = db.one("SELECT * FROM channels WHERE youtube_channel_id=?", info.get("channel_id"))
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
            if row is None and db.one("SELECT id FROM playbooks WHERE channel_id=? AND status='NEEDS_CONFIRMATION'", cid):
                raise Waiting("PLAYBOOK_CONFIRMATION", "频道 Playbook 待确认")
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
        p = youtube.download(src["url"] or src["id"], dest)
    except youtube.AcquireError as e:
        if e.needs_human:
            raise Waiting("YOUTUBE_AUTH", str(e))
        raise
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
        out = ctx.path("work", "chunks", f"{pid}.mp4")
        try:
            path, enc = ffmpeg.fit_proxy(ctx.media, offset, length, out)
        except ffmpeg.MediaError:
            raise
        length = ffmpeg.duration(path)
        chunks.append({"part_id": pid, "path": str(path), "offset": offset, "length": length, "encoding": enc,
                       "source_start": fmt_tc(offset), "source_end": fmt_tc(offset + length)})
    ctx.save("machine/chunk-plan.json", {"chunks": chunks, "duration": dur})
    return {"chunks": len(chunks), "offsets": [c["offset"] for c in chunks]}
