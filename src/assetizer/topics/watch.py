"""盯着的频道和关键词 (design §R8-11): a topic can watch some of its videos' channels and a few words. What is new
there and fits is offered on the topic's page (要加吗) and told on the phone; nothing is added or read by itself. A look
is yt-dlp's list of a channel's uploads or a search: no model, no quota. What a channel or a search has when it is
first looked at is not new (SEEN); a video taken or dropped is not offered again."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess

from .. import config, db, notify
from ..util import now

EVERY = 12 * 3600   # seconds: a topic's channels and words are looked at twice a day
MOST = 15           # the newest uploads of a channel (or results of a search) a look takes
VID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def listing(target: str, most: int = MOST) -> list[dict]:
    """The newest videos of a channel's uploads (its page's URL) or of a search (ytsearchdateN:words)."""
    from ..cookies import ytdlp_login
    from ..youtube import _base
    with ytdlp_login() as (auth, _how):
        r = subprocess.run(_base(config.get()) + auth + ["--flat-playlist", "--playlist-items", f"1-{most}", "-J", "--", target],
                           capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[-300:] or f"yt-dlp {r.returncode}")
    return [e for e in json.loads(r.stdout).get("entries") or [] if e and VID.match(str(e.get("id") or ""))]


def channels(tid: int) -> list[dict]:
    """The channels of the topic's videos that can be watched: yt-dlp told their page."""
    out, seen = [], set()
    for r in db.q("""SELECT v.channel, v.info FROM topic_videos tv JOIN videos v ON v.id=tv.video_id
                     WHERE tv.topic_id=? ORDER BY tv.no""", tid):
        info = db.unj(r["info"], {}) or {}
        url = info.get("channel_url") or (f"https://www.youtube.com/channel/{info['channel_id']}" if info.get("channel_id") else None)
        if url and url not in seen:
            seen.add(url)
            out.append({"name": r["channel"] or info.get("channel") or info.get("uploader") or url, "url": url})
    return out


def view(t: dict) -> dict:
    w = db.unj(t["watch"], None) or {}
    return {"channels": w.get("channels") or [], "words": w.get("words") or [], "at": t["watched_at"], "can": channels(t["id"])}


def candidates(tid: int) -> list[dict]:
    """What is offered on the page: new, and neither taken nor dropped."""
    return [{"id": r["video_id"], "title": r["title"], "channel": r["channel"], "duration": r["duration"],
             "published": r["published"], "why": r["why"], "thumb": f"https://i.ytimg.com/vi/{r['video_id']}/mqdefault.jpg"}
            for r in db.q("SELECT * FROM topic_candidates WHERE topic_id=? AND state='NEW' ORDER BY found_at DESC, video_id", tid)]


def _fits(title: str | None, words: list[str]) -> str | None:
    """The watch's word a title has; with no words every new upload of a watched channel fits ("")."""
    if not words:
        return ""
    low = (title or "").lower()
    return next((w for w in words if w.lower() in low), None)


def check(tid: int) -> int:
    """One look at a topic's channels and words. Returns how many new videos it offers."""
    t = db.one("SELECT * FROM topics WHERE id=?", tid)
    if not t or t["archived_at"] or not t["watch"]:
        return 0
    w = db.unj(t["watch"], {}) or {}
    words = w.get("words") or []
    looked = set(w.get("looked") or [])
    looks = [(c["url"].rstrip("/") + "/videos", c["name"], None) for c in w.get("channels") or []]
    if not looks:  # words alone: a search for each, newest first
        looks = [(f"ytsearchdate{MOST}:{x}", None, x) for x in words]
    have = {r["video_id"] for r in db.q("SELECT video_id FROM topic_videos WHERE topic_id=?", tid)}
    have |= {r["video_id"] for r in db.q("SELECT video_id FROM topic_candidates WHERE topic_id=?", tid)}
    found: list[dict] = []
    for target, name, word in looks:
        try:
            entries = listing(target)
        except Exception as e:  # noqa: BLE001 — YouTube, the network, yt-dlp: the next look tries again
            db.event("topic", f"专题 {tid} 盯频道没看成：{e}"[:300], None, "warn")
            continue
        first = target not in looked
        looked.add(target)
        for e in entries:
            if e["id"] in have:
                continue
            have.add(e["id"])
            hit = word if word is not None else _fits(e.get("title"), words)
            new = not first and hit is not None
            why = f"搜「{word}」" if word is not None else name + (f" · {hit}" if hit else "")
            v = {"id": e["id"], "title": e.get("title"), "channel": e.get("channel") or e.get("uploader") or name,
                 "duration": e.get("duration"), "published": e.get("upload_date")}
            db.exec("""INSERT OR IGNORE INTO topic_candidates(topic_id, video_id, title, channel, duration, published, why, state, found_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""", tid, v["id"], v["title"], v["channel"], v["duration"], v["published"], why,
                    "NEW" if new else "SEEN", now())
            if new:
                found.append(v)
    with db.tx() as c:  # the page may have changed the watch meanwhile: keep its channels and words, add what was looked at
        cur = db.unj(c.execute("SELECT watch FROM topics WHERE id=?", (tid,)).fetchone()["watch"], None)
        if cur:
            cur["looked"] = sorted(set(cur.get("looked") or []) | looked)
        c.execute("UPDATE topics SET watch=?, watched_at=? WHERE id=?", (db.j(cur) if cur else None, now(), tid))
    if found:
        notify.topic_candidates(tid, t["question"], found)
    return len(found)


def due() -> None:
    """Look at the topics whose look is due (EVERY), one after another (run.housekeeping starts it in the background)."""
    for t in db.q("SELECT id, watched_at FROM topics WHERE watch IS NOT NULL AND archived_at IS NULL ORDER BY id"):
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(t["watched_at"])).total_seconds() if t["watched_at"] else None
        except ValueError:
            age = None
        if age is None or age >= EVERY:
            try:
                check(t["id"])
            except Exception as e:  # noqa: BLE001
                db.event("topic", f"专题 {t['id']} 盯频道异常：{e}"[:300], None, "warn")
