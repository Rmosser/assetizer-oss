"""Automatic intake for TRACKED channels via the public YouTube RSS feed (no cookies).

Off by default per channel (`auto_ingest`); when switched on, only videos published
after that moment are queued — never a backlog — and shorts/live replays shorter
than the channel's minimum duration are skipped after RESOLVE.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from . import db
from .util import now

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def feed(channel_id: str) -> list[dict]:
    r = httpx.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        out.append({"id": vid, "title": e.findtext("a:title", namespaces=NS),
                    "published": e.findtext("a:published", namespaces=NS)})
    return out


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
            if not en["id"] or (en["published"] or "") <= since:
                continue
            if db.one("SELECT id FROM sources WHERE id=?", en["id"]):
                continue
            r = submit(f"https://www.youtube.com/watch?v={en['id']}")
            db.exec("UPDATE sources SET channel_id=?, title=COALESCE(title, ?) WHERE id=?", c["id"], en["title"], en["id"])
            db.event("ingest", f"自动收录：{c['name']} · {en['title']}", r["run_id"])
            queued.append(en["id"])
        db.set_setting(f"ingest:{c['id']}", {**cfg, "last_poll": now()})
    return {"queued": queued}


def set_enabled(channel_id: str, enabled: bool, min_minutes: int = 10) -> dict:
    cfg = db.setting(f"ingest:{channel_id}") or {}
    cfg.update(enabled=enabled, min_minutes=min_minutes)
    if enabled and not cfg.get("since"):
        cfg["since"] = now()  # never backfill
    db.set_setting(f"ingest:{channel_id}", cfg)
    return cfg
