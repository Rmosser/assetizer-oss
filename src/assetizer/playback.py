"""原片栏的低清版：调试中频道的样片各转一份 360p mp4，频道页的原片栏就能在本地播放
（会员视频、没登录 YouTube 的设备也能看）。没有低清版时，原片栏用 YouTube 内嵌。

只在调试期需要，所以滚动释放：
- 样片所在频道定稿（样片随定稿写入 Notion）或样片被移出，就删；滚动生产的期数从不生成。
- 样片 `playback_keep_days` 天（默认 KEEP_DAYS）没有动静（加入、试跑、播放）就删。
- 总量超过设置 `playback_max_gb`（默认 MAX_GB）时，从最久没有动静的开始删。
两个设置在机房的磁盘卡里改（knobs.py）。

低清版从缓存的原片转码（不再向 YouTube 下载），原片已被清理的样片不生成。worker 每分钟调一次 tick()，
一次只转一期，低优先级运行，失败只记事件、不影响生产。"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import config, db, knobs
from .media import ffmpeg

KEEP_DAYS = 14
MAX_GB = 5.0
RETRY_AFTER = 6 * 3600  # a failed encode is tried again after this
MIN_FREE = 20e9         # leave the disk to production below this
_SID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_busy = threading.Lock()  # one encode at a time; released by the encode thread
_failed: dict[str, float] = {}
_capped: dict[str, float] = {}  # pushed out by the cap → its activity then: not made again until it has newer activity


def folder() -> Path:
    return config.get().home / "playback"


def path(sid: str) -> Path | None:
    return folder() / f"{sid}.mp4" if _SID.match(sid) else None


def ready(sid: str) -> bool:
    p = path(sid)
    return bool(p and p.exists())


def touch(p: Path) -> None:
    """Being played counts as activity (at most once an hour: a video streams as many range requests)."""
    try:
        if time.time() - p.stat().st_mtime > 3600:
            os.utime(p)
    except OSError:
        pass


def max_bytes() -> float:
    return knobs.get("playback_max_gb") * 1e9


def _ts(iso: str | None) -> float:
    try:
        return dt.datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def wanted() -> dict[str, dict]:
    """The samples of channels still being tuned, with their original media and when they were last added or run."""
    rows = db.q("""SELECT s.id, s.media_state, s.media_path, MAX(sm.added_at) added,
                          (SELECT MAX(r.created_at) FROM runs r WHERE r.source_id = s.id) ran
                   FROM samples sm JOIN channels c ON c.id = sm.channel_id JOIN sources s ON s.id = sm.source_id
                   WHERE c.stage = 'TUNING' GROUP BY s.id""")
    return {r["id"]: r for r in rows}


def _activity(row: dict, p: Path | None) -> float:
    t = max(_ts(row.get("added")), _ts(row.get("ran")))
    try:
        return max(t, p.stat().st_mtime) if p else t  # made or last played
    except OSError:
        return t


def _copies(want: dict[str, dict]) -> list[tuple[float, Path]]:
    d = folder()
    if not d.exists():
        return []
    return [(_activity(want.get(p.stem, {}), p), p) for p in d.glob("*.mp4") if not p.name.startswith(".")]


def sweep() -> dict:
    """Drop copies no longer wanted, idle past `playback_keep_days`, then the least recently active ones over the cap."""
    d = folder()
    if not d.exists():
        return {"removed": 0}
    want = wanted()
    removed = 0
    if not _busy.locked():  # no encode running: a leftover part file is from a killed worker
        for p in d.glob(".*.part.mp4"):
            p.unlink(missing_ok=True)
    cutoff = time.time() - knobs.get("playback_keep_days") * 86400
    keep = []
    for a, p in _copies(want):
        if p.stem not in want or a < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
        else:
            keep.append((a, p))
    total = sum(p.stat().st_size for _, p in keep)
    for a, p in sorted(keep):
        if total <= max_bytes():
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        _capped[p.stem] = a
        removed += 1
    return {"removed": removed}


def _next(want: dict[str, dict]) -> tuple[str, Path] | None:
    """The most recently active sample with its original still cached and no copy yet. Left out: one the cap
    pushed out that has had no activity since, and any older than everything kept while the copies are at the cap
    (making it would only push it, or a newer one, out again)."""
    cands = []
    for sid, r in want.items():
        src, out, a = Path(r["media_path"] or "/nonexistent"), path(sid), _activity(r, None)
        if out and not out.exists() and r["media_state"] == "CACHED" and src.exists() \
                and time.time() - _failed.get(sid, 0) > RETRY_AFTER and a > _capped.get(sid, -1):
            cands.append((a, sid, src))
    if not cands:
        return None
    a, sid, src = max(cands)
    kept = _copies(want)
    if kept and sum(p.stat().st_size for _, p in kept) >= max_bytes() and a <= min(k for k, _ in kept):
        return None
    return sid, src


def _encode(sid: str, src: Path) -> None:
    t0 = time.time()
    try:
        out = ffmpeg.playback_copy(src, path(sid))
        db.event("worker", f"原片低清版：{sid}，{out.stat().st_size / 1e6:.0f} MB，用时 {(time.time() - t0) / 60:.1f} 分钟")
    except Exception as e:  # noqa: BLE001 — a copy that will not encode must not touch production
        _failed[sid] = time.time()
        db.event("worker", f"原片低清版没有生成：{sid}：{str(e)[-300:]}", level="warn")
    finally:
        try:
            sweep()  # the new copy may put the folder over the cap; still holding the lock, so no part file is taken for stale
        except Exception as e:  # noqa: BLE001
            db.event("worker", f"原片低清版清理异常：{e}", level="warn")
        finally:
            _busy.release()


def tick() -> bool:
    """Worker housekeeping: release what is no longer needed, then start one encode if a sample needs a copy."""
    sweep()
    if not _busy.acquire(blocking=False):
        return False
    try:
        nxt = _next(wanted()) if shutil.disk_usage(config.get().home).free > MIN_FREE else None
    except Exception:
        _busy.release()
        raise
    if not nxt:
        _busy.release()
        return False
    folder().mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_encode, args=nxt, name=f"playback-{nxt[0]}", daemon=True).start()
    return True
