"""Automatic intake for TRACKED channels via the public YouTube RSS feed (no cookies).

Off by default per channel (`auto_ingest`); when switched on, only videos published
after that moment are queued — never a backlog — and shorts/live replays shorter
than the channel's minimum duration are skipped after RESOLVE. Older videos come in
only when picked on the channel's 看板 (补录), through their own line: one at a time,
while nothing else waits and the quota has room (design §R6-5).

The RSS endpoint is not reliable (2026-10: 404/500 for every channel), so a failed
feed falls back to yt-dlp's flat listing of the channel's videos and streams tabs.
Its dates are approximate (day precision, ``approx``): those are compared by day.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from . import config, db
from .util import now

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def feed(channel_id: str, limit: int = 15) -> list[dict]:
    try:
        return rss(channel_id)
    except Exception as e:  # noqa: BLE001
        out = listing(channel_id, limit)
        if not out:
            raise RuntimeError(f"RSS 与频道列表都读不到：{e}") from e
        return out


def listing(channel_id: str, limit: int = 15) -> list[dict]:
    """Latest uploads and live replays through yt-dlp (flat, no download)."""
    from .cookies import ytdlp_args
    from .youtube import _base
    out: dict[str, dict] = {}
    for tab in ("videos", "streams"):
        with ytdlp_args() as auth:
            r = subprocess.run(_base(config.get()) + auth + ["--flat-playlist", "--playlist-end", str(limit),
                                                              "--extractor-args", "youtubetab:approximate_date", "-J",
                                                              f"https://www.youtube.com/channel/{channel_id}/{tab}"],
                               capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        for e in json.loads(r.stdout).get("entries") or []:
            if not e.get("id") or e["id"] in out:
                continue
            ts = e.get("timestamp")
            out[e["id"]] = {"id": e["id"], "title": e.get("title"), "duration": e.get("duration"), "approx": True,
                            "published": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""}
    return sorted(out.values(), key=lambda x: x["published"], reverse=True)[:limit]


def rss(channel_id: str) -> list[dict]:
    r = httpx.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        out.append({"id": vid, "title": e.findtext("a:title", namespaces=NS),
                    "published": e.findtext("a:published", namespaces=NS)})
    return out


EVERY = 1800  # seconds between two looks at the channels' feeds (the worker loop calls poll())


def next_round() -> str | None:
    """When the worker looks at the channels next: its last round plus EVERY (later while it is busy or stopped)."""
    last = db.setting("ingest_round")
    if not last:
        return None
    from datetime import timedelta
    return (datetime.fromisoformat(last) + timedelta(seconds=EVERY)).isoformat(timespec="seconds")


SCANS_KEPT = 10  # the last scans shown on the channel's 看板


def poll() -> dict:
    """The worker's round over every channel with intake on (each EVERY seconds; a 立即扫描 does not move it)."""
    db.set_setting("ingest_round", now())
    queued = []
    for c in db.q("SELECT * FROM channels WHERE mode='TRACKED' AND youtube_channel_id IS NOT NULL"):
        cfg = db.setting(f"ingest:{c['id']}") or {}
        if not cfg.get("enabled"):
            continue
        try:
            queued += scan(c, cfg)["ids"]
        except Exception as e:  # noqa: BLE001 — one channel's trouble does not hold the others back
            db.event("ingest", f"{c['name']} 扫描出错：{e}", level="warn")
    return {"queued": queued}


def take(c: dict, en: dict, why: str, priority: int = 0, origin: str = "ingest", register: bool = True) -> tuple[str, str | None]:
    """Queue one video of the channel, unless the channel's Notion database already has it (written by another tool,
    say): then nothing is produced, and with `register` it is kept as 已登记. Returns ("queued", run id) or
    ("registered", None)."""
    from .delivery import registered_in_target
    from .pipeline.runner import submit
    title = en.get("title") or None
    have = registered_in_target({"id": en["id"], "channel_id": c["id"]})
    if have:
        if register:
            db.exec("""INSERT OR IGNORE INTO sources(id, url, channel_id, title, delivery, delivery_reason, notion_url, created_at, updated_at)
                       VALUES (?,?,?,?, 'REGISTERED', ?, ?, ?, ?)""", en["id"], f"https://www.youtube.com/watch?v={en['id']}",
                    c["id"], title, "库里已有", have.get("url"), now(), now())
        db.event("ingest", f"已登记，跳过：{c['name']} · {title or en['id']}（库里已有）")
        return "registered", None
    r = submit(f"https://www.youtube.com/watch?v={en['id']}", priority=priority, origin=origin)
    db.exec("UPDATE sources SET channel_id=?, title=COALESCE(title, ?) WHERE id=?", c["id"], title, en["id"])
    db.event("ingest", f"{why}：{c['name']} · {title or en['id']}", r["run_id"])
    return "queued", r["run_id"]


def scan(c: dict, cfg: dict) -> dict:
    """One look at the channel's feed: what came out after intake was switched on (`since`) is queued, unless it is
    shorter than the channel's minimum or already here. The counts are kept with the channel (its last SCANS_KEPT)."""
    out = {"at": now(), "found": 0, "queued": 0, "registered": 0, "have": 0, "short": 0, "error": None, "ids": []}
    try:
        entries = feed(c["youtube_channel_id"])
    except Exception as e:  # noqa: BLE001
        db.event("ingest", f"{c['name']} RSS 读取失败：{e}", level="warn")
        out["error"] = str(e)[:300]
        entries = []
    since = cfg.get("since") or now()
    for en in entries:
        pub = en["published"] or ""
        # approximate (day-precision) dates compare by day, so a video from the day intake started is not lost
        if not en["id"] or (pub[:10] < since[:10] if en.get("approx") else pub <= since):
            continue
        out["found"] += 1
        if en.get("duration") and en["duration"] < 60 * int(cfg.get("min_minutes") or 0):
            out["short"] += 1
            continue
        try:
            state = None if db.one("SELECT id FROM sources WHERE id=?", en["id"]) else take(c, en, "自动收录")[0]
        except sqlite3.IntegrityError:  # a 立即扫描 and the worker's round took the same video at once: the other has it
            state = None
        if state is None:
            out["have"] += 1
        elif state == "registered":
            out["registered"] += 1
        else:
            out["queued"] += 1
            out["ids"].append(en["id"])
    cur = db.setting(f"ingest:{c['id']}") or cfg  # read again: the switch may have moved while the feed was read
    scans = [{k: v for k, v in out.items() if k != "ids"}] + (cur.get("scans") or [])
    db.set_setting(f"ingest:{c['id']}", {**cur, "last_poll": out["at"], "scans": scans[:SCANS_KEPT]})
    return out


# ---------------------------------------------------------------- 补录: a locked channel's older videos
BACKFILL_MIN_WEEKLY = 0.3  # the production model's weekly quota left that 补录 never dips into (it is for new videos)
BACKFILL_MIN_5H = 0.2      # and of its 5-hour window


def enqueue_backfill(channel_id: str, videos: list[dict]) -> dict:
    """Line up chosen older videos; what is already here or already lined up is left out. They are produced one at a
    time, only while nothing else waits (dispatch_backfill)."""
    out = {"queued": 0, "have": 0}
    for v in videos:
        if db.one("SELECT 1 FROM sources WHERE id=?", v["id"]) or \
                db.one("SELECT 1 FROM backfill_queue WHERE source_id=? AND status='PENDING'", v["id"]):
            out["have"] += 1
            continue
        db.exec("INSERT INTO backfill_queue(channel_id, source_id, title, duration, created_at) VALUES (?,?,?,?,?)",
                channel_id, v["id"], v.get("title") or None, v.get("duration"), now())
        out["queued"] += 1
    return out


def cancel_backfill(channel_id: str) -> int:
    return db.connect().execute("UPDATE backfill_queue SET status='CANCELLED' WHERE channel_id=? AND status='PENDING'",
                                (channel_id,)).rowcount


def backfill_held() -> str | None:
    """Which window of the production group has no room beyond what new videos need ("weekly", "5h"), or None: 补录
    may go on (unknown or stale quota does not hold it back: a quota that runs out pauses production on its own)."""
    from datetime import timedelta
    from .providers import in_use
    if not any(p.name == "agy" for p in in_use()):
        return None
    q = db.setting("agy_quota") or {}
    g = next((g for g in q.get("groups") or [] if g["name"] == q.get("production_group")), None)
    try:
        fresh = q.get("ok") and datetime.fromisoformat(q["checked_at"]) > datetime.now(timezone.utc) - timedelta(hours=2)
    except (KeyError, TypeError, ValueError):
        fresh = False
    if not (fresh and g):
        return None
    left = {b["window"]: b["remaining"] for b in g["buckets"] if not b["disabled"] and b["remaining"] is not None}
    return "weekly" if left.get("weekly", 1) < BACKFILL_MIN_WEEKLY else "5h" if left.get("5h", 1) < BACKFILL_MIN_5H else None


def dispatch_backfill() -> str | None:
    """The next 补录 video, once nothing else is queued, no trial waits, no other 补录 run is under way and the quota
    has room. The worker calls this only with a free slot."""
    if db.one("SELECT 1 FROM runs WHERE production='QUEUED'") or db.one("SELECT 1 FROM trial_queue WHERE status='PENDING'"):
        return None
    if db.one("SELECT 1 FROM runs WHERE origin='backfill' AND production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')"):
        return None
    if db.one("SELECT 1 FROM runs WHERE production='WAITING' AND wait_reason IN ('YOUTUBE_AUTH','DISK_LOW')"):
        return None
    if not db.one("SELECT 1 FROM backfill_queue WHERE status='PENDING'") or backfill_held():
        return None
    for e in db.q("SELECT * FROM backfill_queue WHERE status='PENDING' ORDER BY id"):
        c = db.one("SELECT * FROM channels WHERE id=?", e["channel_id"])
        if not c or c["stage"] != "LOCKED":
            db.exec("UPDATE backfill_queue SET status='CANCELLED' WHERE id=? AND status='PENDING'", e["id"])
            continue
        if db.one("SELECT 1 FROM sources WHERE id=?", e["source_id"]):
            db.exec("UPDATE backfill_queue SET status='SKIPPED' WHERE id=? AND status='PENDING'", e["id"])
            continue
        # claimed before the library is asked (seconds): a 取消排队 or 回到调试期 meanwhile leaves this one alone, as if
        # it had gone already, instead of being undone by it
        if not db.connect().execute("UPDATE backfill_queue SET status='DISPATCHED', dispatched_at=? WHERE id=? AND status='PENDING'",
                                    (now(), e["id"])).rowcount:
            continue
        try:  # already in the library: skipped without a 已登记 row, so a long 补录 does not flood 往期
            state, rid = take(c, {"id": e["source_id"], "title": e["title"]}, "补录", priority=-1, origin="backfill", register=False)
        except Exception as ex:  # noqa: BLE001 — e.g. intake took the same video at once; not to be tried every tick
            db.exec("UPDATE backfill_queue SET status='SKIPPED' WHERE id=?", e["id"])
            db.event("ingest", f"补录跳过 {e['title'] or e['source_id']}：{ex}", level="warn")
            continue
        db.exec("UPDATE backfill_queue SET status=?, run_id=? WHERE id=?", "DISPATCHED" if rid else "SKIPPED", rid, e["id"])
        if rid:
            return rid
    return None


def set_enabled(channel_id: str, enabled: bool, min_minutes: int | None = None) -> dict:
    """Turn intake on or off. The minimum length changes only when given; switching on starts from now,
    so neither the channel's back catalogue nor what came out while intake was paused is pulled in."""
    cfg = db.setting(f"ingest:{channel_id}") or {}
    was_on = bool(cfg.get("enabled"))
    cfg["enabled"] = enabled
    if min_minutes is not None:
        cfg["min_minutes"] = min_minutes
    cfg.setdefault("min_minutes", 10)
    if enabled and (not was_on or not cfg.get("since")):
        cfg["since"] = now()
    db.set_setting(f"ingest:{channel_id}", cfg)
    return cfg
