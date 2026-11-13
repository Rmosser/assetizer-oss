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
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool

from .. import config, db, knobs, playback, youtube
from ..providers import resolve
from ..topics import draft_dir, root, work_dir
from ..topics import backup, backup_rules
from ..topics import comments as TC
from ..topics import notion as TN
from ..topics import watch as TW
from ..topics import write as W
from ..topics.draft import rate
from ..topics.run import changed, held, settle, signature
from ..util import now

router = APIRouter(prefix="/api/topics")
VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Gemini weekly quota a read takes besides its 底稿 (draft.rate, by the model it is on): the looks, 0.47% per video,
# measured on 31 videos (2026-11). A video drafted already spends only the looks.
PER_VIDEO = 0.0047
PEEK_TTL, PEEK_MOST = 600, 50
# a line that is a link (with https://, or a host and a path) and the hosts that are YouTube's: any other is refused
URLISH = re.compile(r"^(?:https?://\S+|(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?/\S*)$", re.I)
YT_HOST = re.compile(r"^(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.I)
# what YouTube says of a video this account will not open: 422, said on its row and not ticked; anything else is
# YouTube not answering for now (503, tried again)
SHUT = {"VIDEO_PRIVATE", "VIDEO_MEMBERS", "VIDEO_REMOVED", "SOURCE_UNAVAILABLE", "AUTH_REQUIRED", "LIVE_UPCOMING", "UNKNOWN"}
_peeks: dict[str, tuple[float, dict]] = {}
_peek_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _thumb(vid: str) -> str:
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def _video_view(v: dict) -> dict:
    info = db.unj(v.get("info"), {}) or {}
    return {"id": v["id"], "url": v["url"], "title": v["title"], "channel": v["channel"], "duration": v["duration"],
            "published": v["published_at"], "thumb": _thumb(v["id"]), "drafted": bool(v.get("draft")),
            "media": v["media_state"], "availability": info.get("availability"), "gone": v.get("gone"),
            "backup": backup.video_view(v)}


def _progress(r: dict) -> dict | None:
    return db.unj(r.get("progress"), None)


def _item_view(r: dict, rev: int) -> dict:
    return {"no": r["no"], "id": r["video_id"], "state": r["state"], "step": r["step"], "progress": _progress(r),
            "wait": r["wait_reason"], "error_class": r["error_class"], "error": (r["error"] or "")[:300] or None,
            "fit": r["fit"], "gist": r["gist"], "excluded": bool(r["excluded"]), "excluded_why": r["excluded_why"],
            "force": bool(r["force"]),
            "stale": r["state"] != "DONE" and r["done_rev"] is not None and r["done_rev"] != rev,
            "done_rev": r["done_rev"], "finished_at": r["finished_at"], "added_at": r["added_at"]}


def _items(tid: int) -> list[dict]:
    return db.q("""SELECT tv.*, v.url, v.title, v.channel, v.duration, v.published_at, v.info, v.draft, v.media_state, v.gone, v.id, b.restored_at
                   FROM topic_videos tv JOIN videos v ON v.id=tv.video_id LEFT JOIN video_backups b ON b.id=v.id WHERE tv.topic_id=? ORDER BY tv.no""", tid)


def _counts(items: list[dict]) -> dict:
    live = [r for r in items if not r["excluded"]]
    c = {k: sum(1 for r in live if r["state"] == k) for k in ("QUEUED", "RUNNING", "WAITING", "DONE", "FAILED")}
    fits = {k: sum(1 for r in live if r["state"] == "DONE" and r["fit"] == k) for k in ("high", "some", "none")}
    return {"videos": len(live), "excluded": len(items) - len(live), **{k.lower(): v for k, v in c.items()}, **fits,
            "hours": round(sum((r["duration"] or 0) for r in live) / 3600, 2)}


def _budget() -> dict:
    """What the 新专题 sheet shows of the Gemini week: what is left, and what the channels keep."""
    from ..providers import is_agy
    from ..providers.agy import quota_group
    q = db.setting("agy_quota") or {}
    left = None
    video = resolve(knobs.topic_routes()["draft"])
    if q.get("groups") and is_agy(video) and (g := quota_group(q["groups"], video[len("agy/"):])):
        left = next((b["remaining"] for b in g["buckets"] if b["window"] == "weekly" and not b.get("disabled")), None)
    return {"weekly_left": left, "floor": knobs.get("topic_min_weekly"), "per_hour": rate(video), "per_video": PER_VIDEO,
            "gemini": is_agy(video)}


def _cost(videos: list[dict]) -> float:
    per_hour = rate(resolve(knobs.topic_routes()["draft"]))
    return sum((0 if v.get("drafted") else per_hour * (v.get("duration") or 0) / 3600) + PER_VIDEO for v in videos)


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
    for t in db.q("SELECT * FROM topics ORDER BY archived_at IS NOT NULL, updated_at DESC, id DESC"):
        items = _items(t["id"])
        synth = db.unj(t["synth"], None)
        out.append({"id": t["id"], "question": t["question"], "paused": bool(t["paused"]), "archived": _archived(t),
                    "offered": db.one("SELECT COUNT(*) n FROM topic_candidates WHERE topic_id=? AND state='NEW'", t["id"])["n"],
                    "created_at": t["created_at"],
                    "updated_at": t["updated_at"], **_counts(items), "answer": _first_line((synth or {}).get("answer")),
                    "synth_state": t["synth_state"], "thumbs": [_thumb(r["video_id"]) for r in items[:4]]})
    return {"topics": out, "budget": _budget(), "held": held()}


def _link(line: str) -> str | None:
    """The line as a YouTube link (https:// put in front when it was left out), or None: words to search for. A link
    elsewhere is refused, not searched."""
    if not URLISH.match(line):
        return None
    url = line if re.match(r"https?://", line, re.I) else f"https://{line}"
    if not YT_HOST.match(urlparse(url).hostname or ""):
        raise HTTPException(422, "只支持 YouTube 链接")
    return url


@router.get("/peek")
def peek(url: str = Query(..., min_length=2, max_length=2000), start: int = Query(1, ge=1, le=5000)):
    """What a line of the 新专题 sheet holds: a video, or the videos of a playlist, a channel's page or a search (a line
    that is not a link searches YouTube), PEEK_MOST at a time from `start` — with how many there are (`total`, when
    YouTube says) and whether there are `more`. A link that names a video is that video, a list in it or not
    (a phone shares youtu.be/…?list=…). Answered from a cache for ten minutes."""
    line = url.strip()
    key = f"{line}\n{start}"
    with _peek_lock:
        hit = _peeks.get(key)
        if hit and time.time() - hit[0] < PEEK_TTL:
            return hit[1]
    link = _link(line)
    vid = youtube.video_id(link or line)
    out = None
    if vid and start == 1:
        try:
            info = youtube.metadata(f"https://www.youtube.com/watch?v={vid}")
        except youtube.AcquireError as e:
            # eleven letters that are no video's id (`investments`) are words to search for; a link says what is wrong
            if vid != line or e.kind not in ("VIDEO_REMOVED", "SOURCE_UNAVAILABLE", "UNKNOWN"):
                raise HTTPException(422 if e.kind in SHUT else 503, _peek_error(e.kind))
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
        target = link or f"ytsearch{PEEK_MOST}:{line}"
        end = start + PEEK_MOST  # one past what is shown: is there more
        s = config.get()
        from .. import cookies
        from ..youtube import _base
        try:
            with cookies.ytdlp_login() as (auth, _how):
                r = subprocess.run(_base(s) + auth + ["--flat-playlist", "--playlist-items", f"{start}-{end}", "-J", "--", target],
                                   capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "YouTube 没有回应")
        except OSError:
            raise HTTPException(503, "取不到视频信息")
        if r.returncode != 0:
            kind = youtube.classify(r.stderr)
            raise HTTPException(422 if kind in SHUT else 503, _peek_error(kind))
        info = json.loads(r.stdout)
        entries = [e for e in info.get("entries") or [] if e and VID.match(str(e.get("id") or ""))]
        if not entries:
            raise HTTPException(422, "这里没有视频" if link else "没有搜到视频")
        search = target.startswith("ytsearch")
        more = not search and len(entries) > PEEK_MOST
        count = info.get("playlist_count")
        out = {"kind": "search" if search else "list", "title": info.get("title"),
               "videos": [_peeked(e) for e in entries[:PEEK_MOST]], "start": start, "more": more,
               "total": count if isinstance(count, int) and count >= start - 1 + min(len(entries), PEEK_MOST)
               else None if more else start - 1 + len(entries)}
    drafted = {r["id"] for r in db.q(f"SELECT id FROM videos WHERE draft IS NOT NULL AND id IN ({','.join('?' * len(out['videos']))})",
                                     *[v["id"] for v in out["videos"]])}
    for v in out["videos"]:
        v["drafted"] = v["id"] in drafted
    with _peek_lock:
        _peeks[key] = (time.time(), out)
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


# ------------------------------------------------------------------ 存到 Notion
class NotionPlace(BaseModel):
    kind: str = Field(pattern=r"^(page|database)$")
    id: str = Field(min_length=32, max_length=36)
    title: str = Field("", max_length=300)
    icon: str | None = Field(None, max_length=16)


class NotionLink(BaseModel):
    link: str = Field(min_length=8, max_length=2000)


class NotionPick(BaseModel):
    target: NotionPlace | None


class NotionDo(BaseModel):
    how: str = Field("write", pattern=r"^write$")


def _place(p: NotionPlace | None) -> dict | None:
    """A place picked on a page: checked again against what is excluded (a channel may have moved there since)."""
    if p is None:
        return None
    t = p.model_dump()
    if t["kind"] == "database" and (why := TN.excluded().get(TN._norm(t["id"]))):
        raise HTTPException(422, why)
    return t


def _notion_said(e: TN.Problem) -> HTTPException:
    return HTTPException(409 if e.kind == "TOKEN" else 422, str(e))


@router.get("/notion")
def notion_default():
    return {"default": TN.default(), "recent": db.setting("topic_notion_recent") or []}


@router.get("/notion/targets")
def notion_targets(q: str = Query("", max_length=200)):
    try:
        return {"targets": TN.targets(q), "default": TN.default()}
    except TN.Problem as e:
        raise _notion_said(e)


@router.post("/notion/resolve")
def notion_resolve(body: NotionLink):
    try:
        return TN.resolve(body.link)
    except TN.Problem as e:
        raise _notion_said(e)


@router.post("/notion/default")
def notion_set_default(body: NotionPick):
    t = _place(body.target)
    db.set_setting("topic_notion_default", t)
    if t:
        TN.remember(t)
    return {"default": t}


def _archived(t: dict) -> dict | None:
    return {"at": t["archived_at"], "how": t["archived_how"], "why": t["archived_why"]} if t["archived_at"] else None


def _open(t: dict) -> None:
    """What an archived topic does not do: read, put together, watch (design §R8-11)."""
    if t["archived_at"]:
        raise HTTPException(409, "专题已归档")


def _since(t: dict, rows: list[dict]) -> dict:
    """What happened since the answer was put together that it does not take in yet: videos left out (or taken out) it still cites,
    reads it does not have (read since, or counted again), claims the user marked, comments in Notion (design §R8-5, §R8-10,
    §R8-13)."""
    made = {x[0] for x in (db.unj(t["synth_of"], None) or [0, []])[1]}
    marks = db.unj(t["marks"], {}) or {}
    taken = {r["video_id"] for r in db.q("SELECT video_id FROM topic_removed WHERE topic_id=?", t["id"])}
    return {"out": sum(1 for r in rows if r["excluded"] and r["video_id"] in made) + len(taken & made),
            "more": sum(1 for r in rows if not r["excluded"] and r["state"] == "DONE" and r["video_id"] not in made),
            "marks": sum(1 for m in marks.values() if not m.get("seen")),
            "notes": TC.fresh(t["id"])} if t["synth"] else {"out": 0, "more": 0, "marks": 0, "notes": 0}


@router.get("/{tid}")
def topic(tid: int):
    t = _topic(tid)
    rows = _items(tid)
    items = [{**_video_view(r), **_item_view(r, t["rev"])} for r in rows]
    synth = W.apply_marks(db.unj(t["synth"], None) or {}, db.unj(t["marks"], {})) or None
    left = [r for r in rows if not r["excluded"] and r["state"] != "DONE"]
    return {"topic": {"id": t["id"], "question": t["question"], "notes": t["notes"], "rev": t["rev"], "paused": bool(t["paused"]),
                      "archived": _archived(t), "created_at": t["created_at"], "updated_at": t["updated_at"]},
            "items": items, "counts": _counts(rows),
            "synth": synth, "synth_state": t["synth_state"], "synth_error": t["synth_error"], "synth_at": t["synth_at"],
            "synth_n": len((db.unj(t["synth_of"], None) or [0, []])[1]),
            "synth_stale": bool(synth) and t["synth_of"] != signature(tid), "since": _since(t, rows),
            "versions": W.versions(tid), "marks": db.unj(t["marks"], {}) or {},
            "record": W.record({r["channel"] for r in rows if r["channel"]}),
            "watch": TW.view(t), "candidates": TW.candidates(tid),
            "held": held(), "cost_left": round(_cost([_video_view(r) for r in left]), 4), "budget": _budget(),
            "notion": TN.state(t), "backup": backup.topic_view(rows)}


@router.post("/{tid}/backup/retry")
def backup_retry(tid: int):
    _topic(tid)
    n = 0
    for v in db.q("SELECT id FROM video_backups WHERE backup_failed_at IS NOT NULL AND id IN (SELECT video_id FROM topic_videos WHERE topic_id=?)", tid):
        n += int(backup_rules.queue(v["id"], force=True))
    return {"ok": True, "due": n}


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
            "backup": backup.video_view(r), "restored_at": r["restored_at"],
            "video": _video_view(r), "item": _item_view(r, t["rev"]),
            "plan": {"fit": plan.get("fit"), "reason": plan.get("reason")} if plan else None,
            "notes": notes, "looks": looks, "playback": playback.ready(vid), "rev": rev,
            "draft": (draft_dir(vid) / "full.md").exists(), "frames": r["media_state"] == "CACHED",
            "shots": sorted((f.stem for f in (draft_dir(vid) / "shots").glob("*.jpg")), key=float),
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
    """The original's frame at t (a tenth of a second), as the notes cite it: [12:34.5 帧]. Those the notes cite are kept
    when the read ends (run.keep_shots), and show after the original is gone (design §R8-8)."""
    from ..topics.draft import grab, shot
    _topic(tid)
    v = db.one("SELECT media_path, media_state, duration FROM videos WHERE id=?", vid) if VID.match(vid) else None
    if not v:
        raise HTTPException(404)
    t, out = shot(vid, t, v["duration"])
    if not out.exists() and (v["media_state"] != "CACHED" or not v["media_path"] or not Path(v["media_path"]).exists()):
        raise HTTPException(404, "原片已清理")
    if not out.exists():
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
    backup: StrictBool | None = None
    question: str = Field(min_length=2, max_length=500)
    notes: str | None = Field(None, max_length=4000)
    videos: list[PickedVideo] = Field(min_length=1, max_length=100)
    batch: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{8,64}$")  # 扩展选片篮的批次


class TopicEdit(BaseModel):
    question: str | None = Field(None, min_length=2, max_length=500)
    notes: str | None = Field(None, max_length=4000)


class MoreVideos(BaseModel):
    videos: list[PickedVideo] = Field(min_length=1, max_length=100)


class ItemAction(BaseModel):
    action: str = Field(pattern=r"^(exclude|include|force|retry|remove|restore)$")
    why: str | None = Field(None, pattern=r"^(离题|质量差|转述别人)$")   # exclude: why it does not count


class Pause(BaseModel):
    paused: bool


class Archive(BaseModel):
    how: str | None = Field(None, pattern=r"^(应验|落空|部分应验|不判)$")   # None: taken back
    why: str | None = Field(None, max_length=300)


class Watch(BaseModel):
    channels: list[str] = Field(default_factory=list, max_length=20)   # pages of the batch's channels (watch.channels)
    words: list[str] = Field(default_factory=list, max_length=10)


class Take(BaseModel):
    take: bool


class Mark(BaseModel):
    claim: str = Field(pattern=r"^C\d{1,4}$")
    status: str = Field(pattern=r"^(现行|被取代|落空|应验|不算)$")   # 现行: take the mark back
    why: str | None = Field(None, max_length=300)


def _question(q: str) -> str:
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(422, "问题至少两个字")
    return q



def _rules_write(fn):
    import functools
    import inspect
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            with backup_rules.mutation():
                return fn(*args, **kwargs)
        except FileExistsError as e:
            raise HTTPException(409, str(e)) from None
    wrapped.__signature__ = inspect.signature(fn, eval_str=True)
    return wrapped

def _add(tid: int, videos: list[PickedVideo]) -> int:
    at = now()
    t = db.one("SELECT rev, last_no FROM topics WHERE id=?", tid)
    # numbers are not given twice: the answer cites V2 until it is put together again, even after V2 is taken out
    n = max(db.one("SELECT MAX(no) n FROM topic_videos WHERE topic_id=?", tid)["n"] or 0, t["last_no"] or 0,
            db.one("SELECT MAX(json_extract(item, '$.no')) n FROM topic_removed WHERE topic_id=?", tid)["n"] or 0)
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
        if _restore(tid, v.id):  # taken out a while ago: its number and notes again
            added += 1
            continue
        n += 1
        db.exec("""INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (?,?,?,?,?,?)""",
                tid, v.id, n, "QUEUED", rev, at)
        added += 1
    db.exec("UPDATE topics SET last_no=? WHERE id=?", n, tid)
    return added


def _restore(tid: int, vid: str) -> bool:
    """A video taken out in the last KEEP_REMOVED_DAYS back as it was: its number, its notes and looks, nothing read again
    — unless the question changed meanwhile: then it is read for the new one, its old notes shown till then (design §R8-8)."""
    s = db.one("SELECT item FROM topic_removed WHERE topic_id=? AND video_id=?", tid, vid)
    if not s:
        return False
    rev = db.one("SELECT rev FROM topics WHERE id=?", tid)["rev"]
    cols = {r["name"] for r in db.q("PRAGMA table_info(topic_videos)")}
    row = {k: v for k, v in (db.unj(s["item"], {}) or {}).items() if k in cols}
    if row.get("rev") != rev or row.get("state") == "RUNNING":
        row.update(state="QUEUED", step=None, progress=None, attempts=0)
    if row.get("rev") != rev:
        row.update(rev=rev, wait_reason=None, error=None, error_class=None, force=0)
    with db.tx() as c:
        c.execute(f"INSERT INTO topic_videos({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        c.execute("DELETE FROM topic_removed WHERE topic_id=? AND video_id=?", (tid, vid))
    if row["state"] == "DONE":
        backup_rules.queue(vid)
    return True


@router.post("")
@_rules_write
def create(body: NewTopic):
    at, q = now(), _question(body.question)
    if body.backup is not None and not backup.available():
        raise HTTPException(409, "请先完成网盘客户端、登录和文件夹设置")
    if body.batch and (t := db.one("SELECT id FROM topics WHERE batch=?", body.batch)):
        return {"id": t["id"], "again": True}  # the same 选片篮 sent twice (the answer back to the extension was lost)
    tid = db.exec("INSERT INTO topics(question, notes, batch, notion_target, backup, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                  q, (body.notes or "").strip() or None, body.batch, db.j(TN.default()) if TN.default() else None, body.backup, at, at)
    _add(tid, body.videos)
    db.event("topic", f"新专题：{q[:60]}（{len(body.videos)} 期）")
    return {"id": tid}


@router.post("/{tid}")
def edit(tid: int, body: TopicEdit):
    """A new question (or new notes) reads every video again from its plan; the drafts stay."""
    t = _topic(tid)
    _open(t)
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
@_rules_write
def add_videos(tid: int, body: MoreVideos):
    _open(_topic(tid))
    _add(tid, body.videos)
    db.exec("UPDATE topics SET updated_at=? WHERE id=?", now(), tid)
    return topic(tid)


@router.post("/{tid}/videos/{vid}")
@_rules_write
def item_action(tid: int, vid: str, body: ItemAction):
    t = _topic(tid)
    a = body.action
    if a == "restore":
        if not _restore(tid, vid):
            raise HTTPException(404, "没有可撤销的")
        db.exec("UPDATE topics SET updated_at=? WHERE id=?", now(), tid)
        return topic(tid)
    r = db.one("SELECT * FROM topic_videos WHERE topic_id=? AND video_id=?", tid, vid)
    if not r:
        raise HTTPException(404, "这个专题里没有这期")
    if a == "exclude":
        db.exec("UPDATE topic_videos SET excluded=1, excluded_why=? WHERE topic_id=? AND video_id=?", body.why, tid, vid)
    elif a == "include":
        db.exec("""UPDATE topic_videos SET excluded=0, excluded_why=NULL, state=CASE WHEN state='DONE' AND done_rev=rev THEN 'DONE'
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
    else:  # taken out: kept aside KEEP_REMOVED_DAYS with its reads (撤销, or added again), then gone (run.retention)
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO topic_removed(topic_id, video_id, item, removed_at) VALUES (?,?,?,?)",
                      (tid, vid, db.j(dict(r)), now()))
            c.execute("DELETE FROM topic_videos WHERE topic_id=? AND video_id=?", (tid, vid))
    db.exec("UPDATE topics SET updated_at=? WHERE id=?", now(), tid)
    settle(tid)
    return topic(tid)


@router.post("/{tid}/synth")
def synth_now(tid: int):
    """Put the answer together now, from the videos read so far."""
    _open(_topic(tid))
    if not db.one("SELECT 1 FROM topic_videos WHERE topic_id=? AND state='DONE' AND excluded=0", tid):
        raise HTTPException(409, "还没有读完的视频")
    db.exec("UPDATE topics SET synth_state='QUEUED', synth_error=NULL WHERE id=? AND COALESCE(synth_state,'') NOT IN ('QUEUED','RUNNING')", tid)
    return topic(tid)


@router.post("/{tid}/notion")
def notion_pick(tid: int, body: NotionPick):
    """Where this topic goes in Notion (None: 不存). A new place is a new page there; the old one stays in Notion."""
    t = _topic(tid)
    new = _place(body.target)
    old = db.unj(t["notion_target"], None)
    if (new or {}).get("id") != (old or {}).get("id"):
        if not changed("""UPDATE topics SET notion_target=?, notion_page_id=NULL, notion_db=NULL, notion_rows=NULL, notion_state=NULL,
                          notion_error=NULL, notion_at=NULL WHERE id=? AND COALESCE(notion_state,'') != 'RUNNING'""",
                       db.j(new) if new else None, tid):
            raise HTTPException(409, "正在写进 Notion")
        TC.forget(tid)  # the old page's comments are not this topic's any more
        if new:
            TN.remember(new)
    return topic(tid)


@router.post("/{tid}/notion/write")
def notion_write(tid: int, body: NotionDo):
    """写进 Notion / 更新: what 课代表 wrote there is replaced, what anyone else wrote stays (topics/notion.py)."""
    t = _topic(tid)
    if not t["notion_target"]:
        raise HTTPException(409, "还没选存到哪里")
    if not t["synth"]:
        raise HTTPException(409, "还没有回答")
    if t["notion_state"] == "RUNNING":
        raise HTTPException(409, "正在写进 Notion")
    db.exec("UPDATE topics SET notion_state='QUEUED', notion_error=NULL WHERE id=?", tid)
    return topic(tid)


@router.post("/{tid}/marks")
def mark(tid: int, body: Mark):
    """A claim retired (被取代, 不算) or a prediction settled (应验, 落空) by the user: shown at once, kept for every answer
    after (the model is told to keep it); the answer itself is put together again only when asked (design §R8-10)."""
    t = _topic(tid)
    if not any(c.get("id") == body.claim for c in (db.unj(t["synth"], {}) or {}).get("claims") or []):
        raise HTTPException(404, "回答里没有这条")
    with db.tx() as c:
        marks = db.unj(c.execute("SELECT marks FROM topics WHERE id=?", (tid,)).fetchone()["marks"], {}) or {}
        if body.status == "现行":
            marks.pop(body.claim, None)
        else:
            marks[body.claim] = {"status": body.status, "why": (body.why or "").strip() or None, "at": now()}
        c.execute("UPDATE topics SET marks=?, updated_at=? WHERE id=?", (db.j(marks) if marks else None, now(), tid))
    return topic(tid)


@router.post("/{tid}/archive")
@_rules_write
def archive(tid: int, body: Archive):
    """归档 (design §R8-11): the answer stays as it is, with how the question came out; nothing more is read, put together, watched
    or told, and its Notion page says so. `how` None takes it back: it goes on where it was."""
    t = _topic(tid)
    if body.how is None:
        db.exec("UPDATE topics SET archived_at=NULL, archived_how=NULL, archived_why=NULL, updated_at=? WHERE id=?", now(), tid)
        settle(tid)
    else:
        if t["synth_state"] == "RUNNING":
            raise HTTPException(409, "正在综合，好了再归档")
        db.exec("UPDATE topics SET archived_at=?, archived_how=?, archived_why=?, updated_at=? WHERE id=?",
                t["archived_at"] or now(), body.how, (body.why or "").strip() or None, now(), tid)
    if t["notion_page_id"] and t["notion_target"] and t["synth"]:
        db.exec("UPDATE topics SET notion_state='QUEUED', notion_error=NULL WHERE id=? AND COALESCE(notion_state,'') != 'RUNNING'", tid)
    return topic(tid)


@router.post("/{tid}/watch")
def set_watch(tid: int, body: Watch):
    """The channels (of the batch's) and words the topic watches for new videos (design §R8-11); none: it watches nothing."""
    t = _topic(tid)
    _open(t)
    can = {c["url"]: c for c in TW.channels(tid)}
    chans = [can[u] for u in dict.fromkeys(body.channels) if u in can]
    words = list(dict.fromkeys(w.strip()[:40] for w in body.words if w.strip()))
    old = db.unj(t["watch"], None) or {}
    w = {"channels": chans, "words": words, "looked": old.get("looked") or []} if chans or words else None
    db.exec("UPDATE topics SET watch=?, updated_at=? WHERE id=?", db.j(w) if w else None, now(), tid)
    return topic(tid)


@router.post("/{tid}/candidates/{vid}")
@_rules_write
def take(tid: int, vid: str, body: Take):
    """A new video offered (要加吗): 加进来 reads it like any added; 不要 is not offered again."""
    t = _topic(tid)
    r = db.one("SELECT * FROM topic_candidates WHERE topic_id=? AND video_id=? AND state='NEW'", tid, vid)
    if not r:
        raise HTTPException(404, "没有这一期")
    if body.take:
        _open(t)
        _add(tid, [PickedVideo(id=vid, title=r["title"], channel=r["channel"], duration=r["duration"], published=r["published"])])
    db.exec("UPDATE topic_candidates SET state=? WHERE topic_id=? AND video_id=?", "ADDED" if body.take else "DROPPED", tid, vid)
    db.exec("UPDATE topics SET updated_at=? WHERE id=?", now(), tid)
    return topic(tid)


@router.post("/{tid}/pause")
def pause(tid: int, body: Pause):
    _topic(tid)
    db.exec("UPDATE topics SET paused=?, updated_at=? WHERE id=?", int(body.paused), now(), tid)
    return topic(tid)


@router.post("/{tid}/delete")
@_rules_write
def delete(tid: int):
    t = _topic(tid)
    db.exec("DELETE FROM topics WHERE id=?", tid)
    shutil.rmtree(root() / str(tid), ignore_errors=True)
    db.event("topic", f"删除专题：{t['question'][:60]}")
    return {"ok": True}
