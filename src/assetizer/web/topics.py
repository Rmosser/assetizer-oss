"""The 专题 pages' API (topics/): a topic is one question and a batch of videos, each read for it, then put together.

Nothing here calls a model: creating a topic, adding videos, changing the question or asking for the answer only puts
work in the worker's queue (topics/run.py). /peek reads what a link holds through yt-dlp, with no download."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import config, db, knobs, playback, youtube
from ..topics import draft_dir, root, work_dir
from ..topics.run import changed, held, settle, signature
from ..util import now

router = APIRouter(prefix="/api/topics")
VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Gemini weekly quota a read takes, measured on 31 videos (2026-11): the picture 0.20% and the text pass 0.14% per hour
# of video, the looks 0.47% per video. A video drafted already spends only the looks.
PER_HOUR, PER_VIDEO = 0.0034, 0.0047
PEEK_TTL, PEEK_MOST = 600, 50
_peeks: dict[str, tuple[float, dict]] = {}
_peek_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _thumb(vid: str) -> str:
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def _video_view(v: dict) -> dict:
    info = db.unj(v.get("info"), {}) or {}
    return {"id": v["id"], "url": v["url"], "title": v["title"], "channel": v["channel"], "duration": v["duration"],
            "published": v["published_at"], "thumb": _thumb(v["id"]), "drafted": bool(v.get("draft")),
            "media": v["media_state"], "availability": info.get("availability")}


def _progress(r: dict) -> dict | None:
    return db.unj(r.get("progress"), None)


def _item_view(r: dict, rev: int) -> dict:
    return {"no": r["no"], "id": r["video_id"], "state": r["state"], "step": r["step"], "progress": _progress(r),
            "wait": r["wait_reason"], "error_class": r["error_class"], "error": (r["error"] or "")[:300] or None,
            "fit": r["fit"], "gist": r["gist"], "excluded": bool(r["excluded"]), "force": bool(r["force"]),
            "stale": r["state"] != "DONE" and r["done_rev"] is not None and r["done_rev"] != rev,
            "done_rev": r["done_rev"], "finished_at": r["finished_at"], "added_at": r["added_at"]}


def _items(tid: int) -> list[dict]:
    return db.q("""SELECT tv.*, v.url, v.title, v.channel, v.duration, v.published_at, v.info, v.draft, v.media_state, v.id
                   FROM topic_videos tv JOIN videos v ON v.id=tv.video_id WHERE tv.topic_id=? ORDER BY tv.no""", tid)


def _counts(items: list[dict]) -> dict:
    live = [r for r in items if not r["excluded"]]
    c = {k: sum(1 for r in live if r["state"] == k) for k in ("QUEUED", "RUNNING", "WAITING", "DONE", "FAILED")}
    fits = {k: sum(1 for r in live if r["state"] == "DONE" and r["fit"] == k) for k in ("high", "some", "none")}
    return {"videos": len(live), "excluded": len(items) - len(live), **{k.lower(): v for k, v in c.items()}, **fits,
            "hours": round(sum((r["duration"] or 0) for r in live) / 3600, 2)}


def _budget() -> dict:
    """What the 新专题 sheet shows of the Gemini week: what is left, and what the channels keep."""
    from ..providers import is_agy, resolve
    from ..providers.agy import quota_group
    q = db.setting("agy_quota") or {}
    left = None
    video = resolve(knobs.topic_routes()["draft"])
    if q.get("groups") and is_agy(video) and (g := quota_group(q["groups"], video[len("agy/"):])):
        left = next((b["remaining"] for b in g["buckets"] if b["window"] == "weekly" and not b.get("disabled")), None)
    return {"weekly_left": left, "floor": knobs.get("topic_min_weekly"), "per_hour": PER_HOUR, "per_video": PER_VIDEO,
            "gemini": is_agy(video)}


def _cost(videos: list[dict]) -> float:
    return sum((0 if v.get("drafted") else PER_HOUR * (v.get("duration") or 0) / 3600) + PER_VIDEO for v in videos)


def _topic(tid: int) -> dict:
    t = db.one("SELECT * FROM topics WHERE id=?", tid)
    if not t:
        raise HTTPException(404, "没有这个专题")
    return t


def _first_line(md: str | None, most: int = 90) -> str | None:
    if not md:
        return None
    text = re.sub(r"\s*\[[^\]]*?(?:说|帧|画面|L\d+|R\d+|V\d+)[^\]]*\]", "", md)
    text = re.sub(r"[#*_`>]+", "", text).strip()
    first = re.split(r"(?<=[。！？])", text, maxsplit=1)[0].strip()
    return first if len(first) <= most else first[:most].rstrip() + "…"


# ------------------------------------------------------------------ reads
@router.get("")
def topics():
    out = []
    for t in db.q("SELECT * FROM topics ORDER BY updated_at DESC, id DESC"):
        items = _items(t["id"])
        synth = db.unj(t["synth"], None)
        out.append({"id": t["id"], "question": t["question"], "paused": bool(t["paused"]), "created_at": t["created_at"],
                    "updated_at": t["updated_at"], **_counts(items), "answer": _first_line((synth or {}).get("answer")),
                    "synth_state": t["synth_state"], "thumbs": [_thumb(r["video_id"]) for r in items[:4]]})
    return {"topics": out, "budget": _budget(), "held": held()}


@router.get("/peek")
def peek(url: str = Query(..., min_length=2, max_length=2000)):
    """What a line of the 新专题 sheet holds: a video, or the videos of a playlist, a channel's page or a search (a line
    that is not a link searches YouTube), at most PEEK_MOST. Answered from a cache for ten minutes."""
    line = url.strip()
    with _peek_lock:
        hit = _peeks.get(line)
        if hit and time.time() - hit[0] < PEEK_TTL:
            return hit[1]
    vid = youtube.video_id(line)
    out = None
    if vid and ("list=" not in line or "v=" in line):
        try:
            info = youtube.metadata(f"https://www.youtube.com/watch?v={vid}")
        except youtube.AcquireError as e:
            # eleven letters that are no video's id (`investments`) are words to search for; a link says what is wrong
            if vid != line or e.kind not in ("VIDEO_REMOVED", "SOURCE_UNAVAILABLE", "UNKNOWN"):
                raise HTTPException(422, _peek_error(e.kind))
            info = None
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "YouTube 没有回应")
        except OSError:  # no yt-dlp
            raise HTTPException(503, "取不到视频信息")
        if info is not None:
            if info.get("live_status") in ("is_upcoming", "is_live"):
                raise HTTPException(422, "直播还没结束")
            out = {"kind": "video", "videos": [_peeked(info)]}
    if out is None:
        target = line if re.match(r"https?://", line) else f"ytsearch{PEEK_MOST}:{line}"
        s = config.get()
        from ..cookies import ytdlp_login
        from ..youtube import _base
        try:
            with ytdlp_login() as (auth, _how):
                r = subprocess.run(_base(s) + auth + ["--flat-playlist", "--playlist-items", f"1-{PEEK_MOST}", "-J", "--", target],
                                   capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "YouTube 没有回应")
        except OSError:
            raise HTTPException(503, "取不到视频信息")
        if r.returncode != 0:
            raise HTTPException(422, _peek_error(youtube.classify(r.stderr)))
        info = json.loads(r.stdout)
        entries = [e for e in info.get("entries") or [] if e and VID.match(str(e.get("id") or ""))]
        if not entries:
            raise HTTPException(422, "这里没有视频" if re.match(r"https?://", line) else "没有搜到视频")
        out = {"kind": "search" if target.startswith("ytsearch") else "list", "title": info.get("title"),
               "videos": [_peeked(e) for e in entries]}
    drafted = {r["id"] for r in db.q(f"SELECT id FROM videos WHERE draft IS NOT NULL AND id IN ({','.join('?' * len(out['videos']))})",
                                     *[v["id"] for v in out["videos"]])}
    for v in out["videos"]:
        v["drafted"] = v["id"] in drafted
    with _peek_lock:
        _peeks[line] = (time.time(), out)
        for k in [k for k, (at, _) in _peeks.items() if time.time() - at > PEEK_TTL]:
            _peeks.pop(k, None)
    return out


def _peeked(e: dict) -> dict:
    vid = e["id"]
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader"), "duration": e.get("duration"),
            "published": e.get("upload_date"), "thumb": _thumb(vid), "availability": e.get("availability"),
            "live": e.get("live_status") in ("is_upcoming", "is_live")}


def _peek_error(kind: str) -> str:
    return {"VIDEO_PRIVATE": "私享视频", "VIDEO_MEMBERS": "会员视频，这个账号看不了", "VIDEO_REMOVED": "视频已删除",
            "SOURCE_UNAVAILABLE": "视频看不了", "AUTH_REQUIRED": "要登录 YouTube 才能看", "BOT_CHALLENGE": "YouTube 要人机验证",
            "NETWORK": "连不上 YouTube", "RATE_LIMITED": "YouTube 暂时限流", "LIVE_UPCOMING": "直播还没开始",
            "TOOL_OUTDATED": "yt-dlp 需要更新"}.get(kind, "读不出这个链接")


@router.get("/{tid}")
def topic(tid: int):
    t = _topic(tid)
    rows = _items(tid)
    items = [{**_video_view(r), **_item_view(r, t["rev"])} for r in rows]
    synth = db.unj(t["synth"], None)
    left = [r for r in rows if not r["excluded"] and r["state"] != "DONE"]
    return {"topic": {"id": t["id"], "question": t["question"], "notes": t["notes"], "rev": t["rev"], "paused": bool(t["paused"]),
                      "created_at": t["created_at"], "updated_at": t["updated_at"]},
            "items": items, "counts": _counts(rows),
            "synth": synth, "synth_state": t["synth_state"], "synth_error": t["synth_error"], "synth_at": t["synth_at"],
            "synth_n": len((db.unj(t["synth_of"], None) or [0, []])[1]),
            "synth_stale": bool(synth) and t["synth_of"] != signature(tid),
            "held": held(), "cost_left": round(_cost([_video_view(r) for r in left]), 4), "budget": _budget()}


def _read_dir(tid: int, r: dict) -> tuple[Path, int]:
    """The folder of the read the page shows: the one done (the latest question's once it is done), else the one under way."""
    rev = r["done_rev"] if r["state"] != "DONE" and r["done_rev"] else r["rev"]
    return work_dir(tid, rev, r["video_id"]), rev


@router.get("/{tid}/videos/{vid}")
def topic_video(tid: int, vid: str):
    t = _topic(tid)
    rows = _items(tid)
    r = next((x for x in rows if x["video_id"] == vid), None)
    if not r:
        raise HTTPException(404, "这个专题里没有这期")
    d, rev = _read_dir(tid, r)
    plan = json.loads((d / "plan.json").read_text()) if (d / "plan.json").exists() else None
    notes = next(((d / n).read_text() for n in ("notes.md", "notes-1.md") if (d / n).exists()), None)
    looks = []
    for name, round_ in (("looks", 1), ("looks2", 2)):
        if (d / f"{name}.json").exists():
            for x in json.loads((d / f"{name}.json").read_text()):
                looks.append({k: x.get(k) for k in ("id", "mode", "start", "end", "times", "times_shown", "question", "why",
                                                      "answer", "evidence", "sure", "answer2", "evidence2", "sure2", "error",
                                                      "second", "target")}
                             | {"round": round_, "media": [f"/api/topics/{tid}/videos/{vid}/files/{m}?r={rev}" for m in x.get("media") or []]})
    near = [x for x in rows if not x["excluded"] or x["video_id"] == vid]
    j = next((k for k, x in enumerate(near) if x["video_id"] == vid), None)
    return {"topic": {"id": t["id"], "question": t["question"], "rev": t["rev"], "paused": bool(t["paused"])},
            "video": _video_view(r), "item": _item_view(r, t["rev"]),
            "plan": {"fit": plan.get("fit"), "reason": plan.get("reason")} if plan else None,
            "notes": notes, "looks": looks, "playback": playback.ready(vid), "rev": rev,
            "draft": (draft_dir(vid) / "full.md").exists(), "frames": r["media_state"] == "CACHED",
            "held": held(), "prev": near[j - 1]["video_id"] if j else None,
            "next": near[j + 1]["video_id"] if j is not None and j + 1 < len(near) else None}


@router.get("/{tid}/videos/{vid}/draft")
def topic_video_draft(tid: int, vid: str):
    _topic(tid)
    p = draft_dir(vid) / "full.md"
    if not VID.match(vid) or not p.exists():
        raise HTTPException(404, "这期还没有底稿")
    return {"text": p.read_text()}


@router.get("/{tid}/videos/{vid}/files/{path:path}")
def topic_video_file(tid: int, vid: str, path: str, r: int = 1):
    """What a look showed the model (media/<id>/…): frames, a crop, a clip."""
    if not VID.match(vid):
        raise HTTPException(404)
    base = work_dir(tid, r, vid).resolve()
    f = (base / path).resolve()
    if not f.is_relative_to(base / "media") or not f.is_file():
        raise HTTPException(404)
    mt = {".jpg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4"}.get(f.suffix.lower())
    if not mt:
        raise HTTPException(404)
    return FileResponse(f, media_type=mt, headers={"Cache-Control": "private, max-age=604800"})


@router.get("/{tid}/videos/{vid}/frame")
def topic_video_frame(tid: int, vid: str, t: float = Query(..., ge=0, le=86400)):
    """The original's frame at t (a tenth of a second), as the notes cite it: [12:34.5 帧]."""
    _topic(tid)
    v = db.one("SELECT media_path, media_state, duration FROM videos WHERE id=?", vid) if VID.match(vid) else None
    if not v or v["media_state"] != "CACHED" or not v["media_path"] or not Path(v["media_path"]).exists():
        raise HTTPException(404, "原片已清理")
    t = round(min(t, max(0.0, (v["duration"] or t) - 0.2)), 1)
    out = draft_dir(vid) / "shots" / f"{t:.1f}.jpg"
    if not out.exists():
        from ..topics.draft import grab
        try:
            grab(Path(v["media_path"]), t, out)
        except Exception:  # noqa: BLE001 — ffmpeg could not take it
            raise HTTPException(404, "取不到这一帧")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


# ------------------------------------------------------------------ writes
class PickedVideo(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    title: str | None = Field(None, max_length=500)
    channel: str | None = Field(None, max_length=200)
    duration: float | None = Field(None, ge=0, le=86400)
    published: str | None = Field(None, max_length=20)


class NewTopic(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    notes: str | None = Field(None, max_length=4000)
    videos: list[PickedVideo] = Field(min_length=1, max_length=100)


class TopicEdit(BaseModel):
    question: str | None = Field(None, min_length=2, max_length=500)
    notes: str | None = Field(None, max_length=4000)


class MoreVideos(BaseModel):
    videos: list[PickedVideo] = Field(min_length=1, max_length=100)


class ItemAction(BaseModel):
    action: str = Field(pattern=r"^(exclude|include|force|retry|remove)$")


class Pause(BaseModel):
    paused: bool


def _question(q: str) -> str:
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(422, "问题至少两个字")
    return q


def _add(tid: int, videos: list[PickedVideo]) -> int:
    at = now()
    t = db.one("SELECT rev, last_no FROM topics WHERE id=?", tid)
    # numbers are not given twice: the answer cites V2 until it is put together again, even after V2 is taken out
    n = max(db.one("SELECT MAX(no) n FROM topic_videos WHERE topic_id=?", tid)["n"] or 0, t["last_no"])
    rev = t["rev"]
    added = 0
    for v in videos:
        db.exec("""INSERT INTO videos(id, url, title, channel, duration, published_at, created_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=COALESCE(videos.title, excluded.title),
                   channel=COALESCE(videos.channel, excluded.channel), duration=COALESCE(videos.duration, excluded.duration),
                   published_at=COALESCE(videos.published_at, excluded.published_at)""",
                v.id, f"https://www.youtube.com/watch?v={v.id}", v.title, v.channel, v.duration, v.published, at)
        if db.one("SELECT 1 FROM topic_videos WHERE topic_id=? AND video_id=?", tid, v.id):
            continue
        n += 1
        db.exec("""INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (?,?,?,?,?,?)""",
                tid, v.id, n, "QUEUED", rev, at)
        added += 1
    db.exec("UPDATE topics SET last_no=? WHERE id=?", n, tid)
    return added


@router.post("")
def create(body: NewTopic):
    at, q = now(), _question(body.question)
    tid = db.exec("INSERT INTO topics(question, notes, created_at, updated_at) VALUES (?,?,?,?)",
                  q, (body.notes or "").strip() or None, at, at)
    _add(tid, body.videos)
    db.event("topic", f"新专题：{q[:60]}（{len(body.videos)} 期）")
    return {"id": tid}


@router.post("/{tid}")
def edit(tid: int, body: TopicEdit):
    """A new question (or new notes) reads every video again from its plan; the drafts stay."""
    t = _topic(tid)
    q = _question(body.question) if body.question is not None else t["question"]
    n = ((body.notes or "").strip() or None) if body.notes is not None else t["notes"]
    if q == t["question"] and n == t["notes"]:
        return topic(tid)
    rev = t["rev"] + 1
    db.exec("UPDATE topics SET question=?, notes=?, rev=?, updated_at=? WHERE id=?", q, n, rev, now(), tid)
    db.exec("""UPDATE topic_videos SET rev=?, state='QUEUED', step=NULL, progress=NULL, attempts=0, wait_reason=NULL,
               error=NULL, error_class=NULL, force=0 WHERE topic_id=?""", rev, tid)
    return topic(tid)


@router.post("/{tid}/videos")
def add_videos(tid: int, body: MoreVideos):
    _topic(tid)
    _add(tid, body.videos)
    db.exec("UPDATE topics SET updated_at=? WHERE id=?", now(), tid)
    return topic(tid)


@router.post("/{tid}/videos/{vid}")
def item_action(tid: int, vid: str, body: ItemAction):
    t = _topic(tid)
    r = db.one("SELECT * FROM topic_videos WHERE topic_id=? AND video_id=?", tid, vid)
    if not r:
        raise HTTPException(404, "这个专题里没有这期")
    a = body.action
    if a == "exclude":
        db.exec("UPDATE topic_videos SET excluded=1 WHERE topic_id=? AND video_id=?", tid, vid)
    elif a == "include":
        db.exec("""UPDATE topic_videos SET excluded=0, state=CASE WHEN state='DONE' AND done_rev=rev THEN 'DONE'
                   WHEN state='RUNNING' THEN 'QUEUED' ELSE state END WHERE topic_id=? AND video_id=?""", tid, vid)
    elif a == "force":
        if r["fit"] != "none" or r["state"] != "DONE":
            raise HTTPException(409, "这期不是判为无关的")
        db.exec("""UPDATE topic_videos SET force=1, state='QUEUED', attempts=0, rev=? WHERE topic_id=? AND video_id=?""",
                t["rev"], tid, vid)
    elif a == "retry":
        if not changed("""UPDATE topic_videos SET state='QUEUED', attempts=0, wait_reason=NULL, error=NULL, error_class=NULL
                          WHERE topic_id=? AND video_id=? AND state IN ('FAILED','WAITING')""", tid, vid):
            raise HTTPException(409, "这期没有失败或在等")
    else:  # the video's draft stays with it (run.retention), for the next topic that wants it
        db.exec("DELETE FROM topic_videos WHERE topic_id=? AND video_id=?", tid, vid)
        for d in (root() / str(tid)).glob(f"r*/{vid}"):
            shutil.rmtree(d, ignore_errors=True)
    db.exec("UPDATE topics SET updated_at=? WHERE id=?", now(), tid)
    settle(tid)
    return topic(tid)


@router.post("/{tid}/synth")
def synth_now(tid: int):
    """Put the answer together now, from the videos read so far."""
    _topic(tid)
    if not db.one("SELECT 1 FROM topic_videos WHERE topic_id=? AND state='DONE' AND excluded=0", tid):
        raise HTTPException(409, "还没有读完的视频")
    db.exec("UPDATE topics SET synth_state='QUEUED', synth_error=NULL WHERE id=? AND COALESCE(synth_state,'') NOT IN ('QUEUED','RUNNING')", tid)
    return topic(tid)


@router.post("/{tid}/pause")
def pause(tid: int, body: Pause):
    _topic(tid)
    db.exec("UPDATE topics SET paused=?, updated_at=? WHERE id=?", int(body.paused), now(), tid)
    return topic(tid)


@router.post("/{tid}/delete")
def delete(tid: int):
    t = _topic(tid)
    db.exec("DELETE FROM topics WHERE id=?", tid)
    shutil.rmtree(root() / str(tid), ignore_errors=True)
    db.event("topic", f"删除专题：{t['question'][:60]}")
    return {"ok": True}
