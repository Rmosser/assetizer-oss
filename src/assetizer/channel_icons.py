"""A channel's YouTube avatar as the icon of its Notion pages (Notion setting ``icon: "channel"``).

The icon is the avatar's address on YouTube, kept in the setting ``channel_avatar:{channel_id}`` and asked for again once
a week beside the worker's tick (yt-dlp takes up to 90 s): nothing else waits for YouTube, a page made before the address
is known goes without and is caught up. New pages get it when they are made; the worker brings existing pages up to it a
few at a time, and again when the address changes. ``sources.notion_icon`` says which address a page has.
"""
from __future__ import annotations

import time

from . import db, youtube
from .notion import mapping as M
from .notion.client import NotionError

REFRESH = 7 * 86400  # seconds between asking YouTube for an avatar it gave
RETRY = 3600         # seconds before asking again when there is none yet
BATCH = 20           # pages per sync: the worker waits for it


def _key(channel_id: str) -> str:
    return f"channel_avatar:{channel_id}"


def external(url: str) -> dict:
    return {"type": "external", "external": {"url": url}}


def kept(channel_id: str) -> str | None:
    """The avatar's address as last heard, without asking anyone."""
    return (db.setting(_key(channel_id)) or {}).get("url")


def refresh() -> None:
    """Ask YouTube for the avatars that are due, one channel after another."""
    for ch in db.q("SELECT id FROM channels"):
        avatar(ch["id"])


def state(channel_id: str, cfg: dict | None) -> dict:
    """For the channel's Notion settings: the avatar, the pages in the target database, how many of them do not carry
    it yet, and whether Notion refused this address."""
    held = db.setting(_key(channel_id)) or {}
    url = held.get("url")
    pages = db.one("""SELECT COUNT(*) total, SUM(COALESCE(notion_icon, '')<>?) left_ FROM sources
                      WHERE channel_id=? AND notion_page_id IS NOT NULL AND notion_database_id=?""",
                   url or "", channel_id, (cfg or {}).get("database_id"))
    return {"url": url, "total": pages["total"], "left": (pages["left_"] or 0) if url else pages["total"],
            "refused": bool(url) and held.get("refused") == url}


def avatar(channel_id: str) -> str | None:
    """The avatar's address, asked for when due. YouTube not answering leaves the one already kept."""
    kept = db.setting(_key(channel_id)) or {}
    if time.time() - kept.get("tried", 0) < (REFRESH if kept.get("url") else RETRY):
        return kept.get("url")
    row = (db.one("SELECT youtube_channel_id c FROM channels WHERE id=?", channel_id)
           or db.one("SELECT youtube_channel_id c FROM sources WHERE channel_id=? AND youtube_channel_id IS NOT NULL", channel_id))
    if not row or not row["c"]:
        return kept.get("url")
    kept["tried"] = time.time()
    try:
        kept["url"] = youtube.channel_avatar(row["c"]) or kept.get("url")
    except Exception as e:  # noqa: BLE001 — a page without an icon is no reason to hold up its delivery
        db.event("notion", f"读不到「{channel_id}」的频道头像：{str(e)[:160]}", level="warn")
    db.set_setting(_key(channel_id), kept)
    return kept.get("url")


def note(source_id: str, page: dict) -> None:
    """Which avatar a page just made carries (none when Notion refused it and the page went without)."""
    icon = page.get("icon") or {}
    db.exec("UPDATE sources SET notion_icon=? WHERE id=?",
            icon["external"]["url"] if icon.get("type") == "external" else None, source_id)


def sync(notion, batch: int = BATCH) -> int:
    """Give up to ``batch`` existing pages their channel's avatar; how many were changed. A page that is gone is not
    tried again. An address Notion refuses is not tried again until it changes."""
    changed = 0
    for ch in db.q("SELECT id FROM channels"):
        cfg = M.get(ch["id"])
        if not cfg or cfg.get("icon") != "channel":
            continue
        held = db.setting(_key(ch["id"])) or {}
        url = held.get("url")
        if not url or held.get("refused") == url:
            continue
        for s in db.q("""SELECT id, notion_page_id FROM sources WHERE channel_id=? AND notion_page_id IS NOT NULL
                         AND notion_database_id=? AND COALESCE(notion_icon, '')<>? ORDER BY id LIMIT ?""",
                      ch["id"], cfg.get("database_id"), url, batch - changed):
            try:
                notion._req("PATCH", f"/pages/{s['notion_page_id']}", idempotent=True, json={"icon": external(url)})
                changed += 1
            except NotionError as e:
                if e.status == 400 and "icon" in e.body:
                    db.set_setting(_key(ch["id"]), {**held, "refused": url})
                    db.event("notion", f"Notion 不接受「{ch['id']}」的频道头像：{e.body[:160]}", level="warn")
                    break
                if e.status not in (400, 403, 404):  # Notion is having a moment: the rest wait for the next sync
                    raise
            db.exec("UPDATE sources SET notion_icon=? WHERE id=?", url, s["id"])
        if changed >= batch:
            break
    if changed:
        db.event("notion", f"已给 {changed} 个页面换上频道头像")
    return changed
