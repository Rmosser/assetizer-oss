"""本地内容的实际清理期限；两条线、孤立底稿及首页提醒只使用这一份计算。"""
from __future__ import annotations

import datetime as dt
import shutil

from .. import config, db, knobs
from . import backup, backup_rules as rules, draft_dir


def plan(vid: str, *, low: bool | None = None, clock: dt.datetime | None = None) -> dict:
    from .run import BACKUP_GRACE_DAYS, KEEP_DRAFTS_DAYS, KEEP_REMOVED_DAYS, MIN_FREE
    clock = clock or dt.datetime.now(dt.timezone.utc)
    enabled = backup.available()
    low = enabled and (shutil.disk_usage(config.get().home).free < MIN_FREE if low is None else low)
    choice = rules.decision(vid) if enabled else False
    saved = db.one("SELECT * FROM video_backups WHERE id=?", vid) or {}
    unresolved = bool(saved.get("delete_intent"))
    paths = {}
    normal = {}

    def stamp(value, *, invalid=None):
        try:
            t = dt.datetime.fromisoformat(value)
            return t if t.tzinfo else invalid
        except (TypeError, ValueError):
            return invalid

    def deadline(ref, days, *, draft=False, invalid=None):
        t = stamp(ref, invalid=invalid)
        if t is None or unresolved:
            return None
        missing = choice is True and not backup.verified(vid, draft=draft)
        if draft:
            missing = missing and ((draft_dir(vid) / "full.md").is_file() or saved.get("backup_due"))
            missing = missing or (choice is True and v["media_state"] == "CACHED" and not backup.verified(vid))
        grace = enabled and not low and (choice is None or missing)
        return t + dt.timedelta(days=days + (BACKUP_GRACE_DAYS if grace else 0))

    with db.read_tx():
        v = db.one("SELECT * FROM videos WHERE id=?", vid)
        if v:
            ref = v["used_at"] or v["created_at"]
            days = knobs.get("retention_days")
            active = db.one("SELECT 1 FROM topic_videos WHERE video_id=? AND state IN ('QUEUED','RUNNING','WAITING') AND excluded=0", vid)
            if v["media_state"] == "CACHED" and not active:
                paths["topic_media"] = deadline(ref, days, invalid=clock)
                normal["topic_media"] = (stamp(ref, invalid=clock) + dt.timedelta(days=days))
            if not db.one("SELECT 1 FROM topic_videos WHERE video_id=?", vid):
                removed = db.q("SELECT removed_at FROM topic_removed WHERE video_id=?", vid)
                # 仍在撤销期：最早也要等最后一个暂存关联到期后，才成为孤立行。
                removal = max((stamp(r["removed_at"], invalid=clock) + dt.timedelta(days=KEEP_REMOVED_DAYS) for r in removed), default=None)
                drop = deadline(ref, KEEP_DRAFTS_DAYS, draft=True, invalid=clock)
                if drop is not None and removal is not None:
                    drop = max(drop, removal)
                paths["topic_drop"] = drop
                normal["topic_drop"] = max(stamp(ref, invalid=clock) + dt.timedelta(days=KEEP_DRAFTS_DAYS), removal or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        s = db.one("SELECT * FROM sources WHERE id=?", vid)
        if s and s["media_state"] == "CACHED" and s["media_path"] and not db.one("SELECT 1 FROM runs WHERE source_id=? AND production IN ('QUEUED','RUNNING','WAITING')", vid):
            delivered = s["delivery"] == "DELIVERED" and s["delivered_at"]
            ref = s["delivered_at"] if delivered else s["updated_at"]
            days = knobs.get("delivered_retention_days" if delivered else "retention_days")
            paths["source_media"] = deadline(ref, days)
            t = stamp(ref)
            if t:
                normal["source_media"] = t + dt.timedelta(days=days)
    # topic_drop 同时删原片与底稿；原片即使 retention_days 很长，也不能晚报这个期限。
    media = [t for key,t in paths.items() if t is not None and
             (key != "topic_drop" or (v and v["media_state"] == "CACHED" and not active))]
    earliest = min(media, default=None)
    due_normal = min((normal[k] for k,t in paths.items() if t is not None and k in normal and
                      (k != "topic_drop" or (v and v["media_state"] == "CACHED" and not active))), default=None)
    return {"paths": paths, "media_at": earliest, "draft_at": paths.get("topic_drop"),
            "pending": enabled and choice is None, "choice": choice, "low": low,
            "unresolved": unresolved, "in_grace": bool(earliest and due_normal and due_normal <= clock < earliest),
            "clock": clock}


def due(p: dict, path: str) -> bool:
    at = p["paths"].get(path)
    return at is not None and p["clock"] >= at


def mark_missed(vid: str, p: dict) -> None:
    from ..util import now
    if p["choice"] is True and not backup.verified(vid):
        rules.track(vid)
        db.exec("UPDATE video_backups SET backup_missed_at=COALESCE(backup_missed_at,?) WHERE id=?", now(), vid)
