"""Automatic intake for TRACKED channels via the public YouTube RSS feed (no cookies).

Off by default per channel (`auto_ingest`); when switched on, only videos published
after that moment are queued — never a backlog — and shorts/live replays shorter
than the channel's minimum duration are skipped after RESOLVE.

The RSS endpoint is not reliable (2026-10: 404/500 for every channel), so a failed
feed falls back to yt-dlp's flat listing of the channel's videos and streams tabs.
Its dates are approximate (day precision, ``approx``): those are compared by day.
"""
from __future__ import annotations

import json
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


def poll() -> dict:
    from .pipeline.runner import submit
    queued = []
    for c in db.q("SELECT * FROM channels WHERE mode='TRACKED' AND youtube_channel_id IS NOT NULL"):
        cfg = db.setting(f"ingest:{c['id']}") or {}
        if not cfg.get("enabled"):
            continue
        try:
            entries = feed(c["youtube_channel_id"])
        except Exception as e:  # noqa: BLE001
            db.event("ingest", f"{c['name']} RSS 读取失败：{e}", level="warn")
            continue
        since = cfg.get("since") or now()
        for en in entries:
            pub = en["published"] or ""
            # approximate (day-precision) dates compare by day, so a video from the day intake started is not lost
            if not en["id"] or (pub[:10] < since[:10] if en.get("approx") else pub <= since):
                continue
            if en.get("duration") and en["duration"] < 60 * int(cfg.get("min_minutes") or 0):
                continue
            if db.one("SELECT id FROM sources WHERE id=?", en["id"]):
                continue
            from .delivery import registered_in_target
            have = registered_in_target({"id": en["id"], "channel_id": c["id"]})
            if have:  # already in the library (e.g. written by another tool): 已登记, no production
                db.exec("""INSERT INTO sources(id, url, channel_id, title, delivery, delivery_reason, notion_url, created_at, updated_at)
                           VALUES (?,?,?,?, 'REGISTERED', ?, ?, ?, ?)""", en["id"], f"https://www.youtube.com/watch?v={en['id']}",
                        c["id"], en["title"], "库里已有", have.get("url"), now(), now())
                db.event("ingest", f"已登记，跳过：{c['name']} · {en['title']}（库里已有）")
                continue
            r = submit(f"https://www.youtube.com/watch?v={en['id']}")
            db.exec("UPDATE sources SET channel_id=?, title=COALESCE(title, ?) WHERE id=?", c["id"], en["title"], en["id"])
            db.event("ingest", f"自动收录：{c['name']} · {en['title']}", r["run_id"])
            queued.append(en["id"])
        db.set_setting(f"ingest:{c['id']}", {**cfg, "last_poll": now()})
    return {"queued": queued}


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
