"""FastAPI app: JSON API + media + the built SPA."""
from __future__ import annotations

import bisect
import mimetypes
import functools
import json
import os
import re
import shutil
import threading
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, db
from ..util import now

app = FastAPI(title="Assetizer", docs_url="/api/docs")
app.add_middleware(GZipMiddleware, minimum_size=1024)


class ImmutableStatic(StaticFiles):
    """Vite emits content-hashed file names: cache them for a year."""

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return r
STATIC = Path(__file__).with_name("static").resolve()
mimetypes.add_type("application/manifest+json", ".webmanifest")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
PUBLIC_HOST = os.environ.get("ASSETIZER_PUBLIC_HOST", "").lower()          # e.g. kan.example.com
ACCESS_TEAM = os.environ.get("ASSETIZER_ACCESS_TEAM", "")                  # e.g. example-team
ACCESS_AUD = os.environ.get("ASSETIZER_ACCESS_AUD", "")
_certs: dict = {"at": 0.0, "keys": None}


def _access_ok(token: str | None) -> bool:
    """Verify the Cloudflare Access JWT (RS256, aud, issuer, expiry) — defence in depth behind the tunnel."""
    if not token or not ACCESS_TEAM or not ACCESS_AUD:
        return False
    import time

    import jwt
    issuer = f"https://{ACCESS_TEAM}.cloudflareaccess.com"
    try:
        if _certs["keys"] is None or time.time() - _certs["at"] > 3600:
            _certs["keys"] = jwt.PyJWKClient(f"{issuer}/cdn-cgi/access/certs")
            _certs["at"] = time.time()
        key = _certs["keys"].get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["RS256"], audience=ACCESS_AUD, issuer=issuer)
        return True
    except Exception:  # noqa: BLE001 — any failure means no access
        return False


@app.middleware("http")
async def guard(request: Request, call_next):
    """Loopback app: local Host via SSH tunnel, or the public Host only with a valid Access JWT.
    Also blocks DNS rebinding and cross-site form posts."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if host in LOCAL_HOSTS:
        pass
    elif PUBLIC_HOST and host == PUBLIC_HOST:
        if not _access_ok(request.headers.get("cf-access-jwt-assertion")):
            return JSONResponse({"error": "access required"}, status_code=403)
    else:
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    if request.method == "POST" and not (request.headers.get("content-type") or "").startswith("application/json"):
        return JSONResponse({"error": "json required"}, status_code=415)
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 echoes what was submitted (`input`, `ctx`): answer with where and why only."""
    return JSONResponse({"detail": [{k: e[k] for k in ("type", "loc", "msg") if k in e} for e in exc.errors()]},
                        status_code=422)

STAGE_LABELS = {
    "RESOLVE": "解析来源", "PLAYBOOK": "绑定频道规则", "ACQUIRE": "获取媒体", "CHUNK": "切分", "UNDERSTAND": "多模态理解",
    "TRANSCRIBE": "全片转写", "MERGE": "合并", "KNOWLEDGE": "知识单元", "ARTICLE": "撰写文章", "SELECT": "选片",
    "SCREENSHOTS": "截图", "BOUNDARY": "边界精修", "CLIPS": "剪辑", "SUBTITLES": "字幕", "MEDIA_QA": "媒体质检",
    "REVIEW_CHECK": "校注预审", "COMPOSE": "编排", "PACKAGE": "发布素材包", "CLEANUP": "清理",
}


def _run_view(r: dict | None) -> dict | None:
    if not r:
        return None
    stages = db.q("SELECT name, status, started_at, finished_at, wall_seconds, detail FROM stages WHERE run_id=? ORDER BY ord", r["id"])
    for s in stages:
        s["label"] = STAGE_LABELS.get(s["name"], s["name"])
        s["detail"] = db.unj(s["detail"], None)
    return {**r, "summary": db.unj(r.get("summary"), {}), "pins": db.unj(r.get("pins"), {}), "stages": stages}


def _cover(run_id: str | None) -> int | None:
    if not run_id:
        return None
    r = db.one("""SELECT a.id FROM assets a JOIN units u ON u.id=a.unit_id WHERE a.run_id=? AND a.kind='screenshot'
                  ORDER BY u.learning_priority DESC, a.start LIMIT 1""", run_id)
    return r["id"] if r else None


def _issue(s: dict, n: int) -> dict:
    run = db.one("SELECT * FROM runs WHERE id=?", s["current_run_id"]) if s.get("current_run_id") else None
    # experiment runs share the source but are not part of its production history
    latest = db.one("SELECT * FROM runs WHERE source_id=? AND kind!='EXPERIMENT' ORDER BY created_at DESC LIMIT 1", s["id"])
    summ = db.unj(run["summary"], {}) if run else {}
    # 校注: what the system doubts in this issue — information, not a to-do (design §R2-3)
    notes = db.one("SELECT COUNT(*) n FROM flags WHERE source_id=? AND run_id IS ? AND severity!='INFO'",
                   s["id"], s.get("current_run_id"))["n"]
    ch = db.one("SELECT stage FROM channels WHERE id=?", s["channel_id"]) if s.get("channel_id") else None
    must = db.one("SELECT COUNT(*) n FROM units WHERE run_id IS ? AND learning_priority=3", s.get("current_run_id"))["n"]
    clips = sum(v for k, v in (summ.get("assets") or {}).items() if k == "clip:GENERATED")
    musts = db.q("SELECT id, title FROM units WHERE run_id IS ? AND learning_priority=3 ORDER BY start LIMIT 3",
                 s.get("current_run_id"))
    return {"no": n, "id": s["id"], "title": s.get("title") or s["id"], "headline": s.get("headline"), "channel": s.get("channel_title"),
            "channel_id": s.get("channel_id"), "published_at": s.get("published_at"), "duration": s.get("duration"),
            "delivery": s["delivery"], "notion_url": s.get("notion_url"), "tuning": bool(ch and ch["stage"] == "TUNING"),
            "units": summ.get("units"), "must": must, "clips": clips, "notes": notes, "cover": _cover(s.get("current_run_id")), "musts": musts,
            "production": latest["production"] if latest else None, "stage": latest["stage"] if latest else None,
            "latest_run": latest["id"] if latest else None, "updated_at": s["updated_at"]}


# ------------------------------------------------------------------ reads
@app.get("/api/overview")
def overview():
    sources = db.q("SELECT * FROM sources ORDER BY created_at ASC")
    issues = [_issue(s, i + 1) for i, s in enumerate(sources)]
    producing = db.one("SELECT * FROM runs WHERE production='RUNNING' LIMIT 1")
    queue = db.q("""SELECT r.id, r.source_id, r.production, r.wait_reason, s.title, s.url FROM runs r JOIN sources s ON s.id=r.source_id
                    WHERE r.production IN ('QUEUED','WAITING','INTERRUPTED') ORDER BY r.priority DESC, r.created_at""")
    p = _run_view(producing)
    if p:
        p["source"] = db.one("SELECT id, title, channel_title, duration FROM sources WHERE id=?", p["source_id"])
        p["eta_seconds"] = _eta(p)
    return {
        "issues": list(reversed(issues)),
        "producing": p, "queue": queue,
        "alerts": db.q("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY created_at DESC"),
        "decisions": db.q("SELECT * FROM decisions WHERE resolved_at IS NULL ORDER BY created_at DESC"),
        "paused": db.setting("queue_paused"),
        "now": now(),
    }


# rough per-stage cost model (seconds per source-minute), refined from history later
STAGE_COST = {"RESOLVE": 0.1, "PLAYBOOK": 0, "ACQUIRE": 3, "CHUNK": 0.3, "UNDERSTAND": 12, "MERGE": 2, "KNOWLEDGE": 8,
              "ARTICLE": 8, "SELECT": 1, "SCREENSHOTS": 0.5, "BOUNDARY": 14, "CLIPS": 30, "SUBTITLES": 10, "MEDIA_QA": 2,
              "REVIEW_CHECK": 2, "COMPOSE": 0, "PACKAGE": 0.2, "CLEANUP": 0, "TRANSCRIBE": 0}


def _eta(run: dict) -> float | None:
    src = db.one("SELECT duration FROM sources WHERE id=?", run["source_id"])
    mins = (src.get("duration") or 3600) / 60
    left = sum(STAGE_COST.get(s["name"], 0) * mins for s in run["stages"] if s["status"] in ("PENDING", "RUNNING"))
    return round(left)


_NONWORD = re.compile(r"[\W_]+", re.U)


def _norm(t: str) -> str:
    return _NONWORD.sub("", t or "").lower()


@functools.lru_cache(maxsize=4)
def _transcript_chars(path: str, mtime: float) -> tuple[str, tuple[float, ...]]:
    chars, times = [], []
    for w in json.loads(Path(path).read_text())["words"]:
        for ch in _norm(w["word"]):
            chars.append(ch)
            times.append(w["start"])
    return "".join(chars), tuple(times)


def _quote_time(transcript: str | None, quote: str | None, lo: float, hi: float) -> float | None:
    """Where in the source a quoted line was spoken: longest common run inside the unit's window."""
    q = _norm(quote or "")
    if not transcript or len(q) < 4 or not Path(transcript).exists():
        return None
    text, times = _transcript_chars(transcript, Path(transcript).stat().st_mtime)
    i0, i1 = bisect.bisect_left(times, lo), bisect.bisect_right(times, hi)
    hay = text[i0:i1]
    m = SequenceMatcher(None, hay, q, autojunk=False).find_longest_match(0, len(hay), 0, len(q))
    if m.size < max(4, int(len(q) * 0.4)):
        return None
    return times[max(i0, i0 + m.a - m.b)]


@app.get("/api/sources/{sid}")
def source(sid: str, run: str | None = None):
    s = db.one("SELECT * FROM sources WHERE id=?", sid)
    if not s:
        raise HTTPException(404)
    rid = run or s.get("current_run_id") or (db.one("SELECT id FROM runs WHERE source_id=? ORDER BY created_at DESC LIMIT 1", sid) or {}).get("id")
    r = db.one("SELECT * FROM runs WHERE id=?", rid) if rid else None
    units = []
    for u in db.q("SELECT * FROM units WHERE run_id=? ORDER BY start", rid):
        d = db.unj(u["data"], {})
        d.update(decision=u["decision"], note=u["note"], overlay=db.unj(u["overlay"], None))
        units.append(d)
    assets = db.q("SELECT id, unit_id, kind, status, start, end, selection, publication_priority, embed, standalone, decision, meta, bytes FROM assets WHERE run_id=?", rid)
    for a in assets:
        a["meta"] = db.unj(a["meta"], {})
    # several units can come from one topic; once a clip exists, its refined span is the unit's precise location
    clip_span = {a["unit_id"]: (a["start"], a["end"]) for a in assets if a["kind"] == "clip" and a["status"] == "GENERATED"}
    for u in units:
        if u["id"] in clip_span:
            u["topic_start"], u["topic_end"] = u["start"], u["end"]
            u["start"], u["end"] = clip_span[u["id"]]
    units.sort(key=lambda u: u["start"])
    flags = [{**f, "evidence": db.unj(f["evidence"], None)} for f in db.q("SELECT * FROM flags WHERE run_id=? ORDER BY severity, id", rid)]
    spans = {u["id"]: (min([g["start"] for g in u.get("segments") or []] + [u["start"]]),
                       max([g["end"] for g in u.get("segments") or []] + [u["end"]])) for u in units}
    for f in flags:
        ev = f["evidence"]
        if isinstance(ev, dict) and ev.get("quote") and f.get("target_id") in spans:
            lo, hi = spans[f["target_id"]]
            try:
                ev["t"] = _quote_time(s.get("transcript_path"), ev["quote"], lo - 90, hi + 90)
            except (OSError, ValueError, KeyError):
                ev["t"] = None
    playbook = db.one("SELECT id, channel_id, version, status, data FROM playbooks WHERE id=?", r["playbook_id"]) if r and r.get("playbook_id") else None
    if playbook:
        playbook["facets"] = db.unj(playbook.pop("data"), {}).get("facets", [])
    return {
        "source": {k: v for k, v in s.items() if k not in ("info",)} | {"info": db.unj(s.get("info"), {})},
        "run": _run_view(r), "units": units, "assets": assets, "playbook": playbook,
        "sections": [{**x, "unit_ids": db.unj(x["unit_ids"], [])} for x in db.q("SELECT * FROM sections WHERE run_id=? ORDER BY ord", rid)],
        "flags": flags,
        "speakers": db.q("SELECT * FROM speakers WHERE run_id=?", rid),
        "runs": db.q("SELECT id, kind, production, created_at, finished_at FROM runs WHERE source_id=? AND kind!='EXPERIMENT' ORDER BY created_at DESC", sid),
        "channel": db.one("SELECT id, name, mode, stage FROM channels WHERE id=?", s.get("channel_id")),
        "no": db.one("SELECT COUNT(*) n FROM sources WHERE created_at <= ?", s["created_at"])["n"],
    }


@app.get("/api/sources/{sid}/words")
def words(sid: str, start: float, end: float):
    s = db.one("SELECT transcript_path, current_run_id FROM sources WHERE id=?", sid)
    p = Path(s["transcript_path"]) if s and s.get("transcript_path") else None
    if not p or not p.exists():
        return {"words": []}
    import json
    w = json.loads(p.read_text())["words"]
    return {"words": [x for x in w if x["end"] >= start and x["start"] <= end]}


def _playbook_view(p: dict) -> dict:
    d = db.unj(p.pop("data"), {})
    p["audit"] = db.unj(p.get("audit"), {}) or {}
    p["rules"] = d.get("rules", [])
    p["facets"] = d.get("facets", [])
    p["identity"] = d.get("identity", {})
    p["article_style"] = d.get("article_style")
    return p


def _compile_state(cid: str, prompt: dict | None, pbs: list[dict]) -> dict | None:
    """Is the newest prompt version compiled? (compiles run in the background)"""
    if not prompt:
        return None
    if any(p.get("prompt_id") == prompt["id"] for p in pbs):
        return {"state": "done"}
    err = db.one("""SELECT ts, message FROM events WHERE kind='compile' AND level='error' AND ts>=? AND message LIKE ?
                    ORDER BY id DESC LIMIT 1""", prompt["created_at"], f"%（{cid}）%")
    return {"state": "failed", "error": err["message"], "at": err["ts"]} if err else {"state": "running", "since": prompt["created_at"]}


@app.get("/api/channels")
def channels():
    out = []
    pending = {d["ref"]: d for d in db.q("SELECT * FROM decisions WHERE resolved_at IS NULL AND kind='CONFIRM_PLAYBOOK'")}
    for c in db.q("SELECT * FROM channels ORDER BY mode DESC, created_at"):
        pbs = [_playbook_view(p) for p in db.q("""SELECT id, prompt_id, version, status, origin, created_at, activated_at, data, audit
                                                   FROM playbooks WHERE channel_id=? ORDER BY version DESC""", c["id"])]
        prompts = db.q("SELECT id, version, sha256, origin, origin_edited_at, note, created_at, text FROM channel_prompts WHERE channel_id=? ORDER BY version DESC", c["id"])
        stats = db.one("SELECT COUNT(*) n, MAX(created_at) last FROM sources WHERE channel_id=?", c["id"])
        ncfg = db.setting(f"notion:{c['id']}") or {}
        busy = db.one("""SELECT COUNT(*) n FROM runs r JOIN samples sm ON sm.source_id=r.source_id AND sm.channel_id=?
                         WHERE r.production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')""", c["id"])["n"]
        out.append({**c, "playbooks": pbs, "prompts": prompts, "sources": stats["n"], "last_source": stats["last"],
                    "ingest": db.setting(f"ingest:{c['id']}") or {"enabled": False},
                    "notion": {"configured": bool(ncfg.get("database_id")), "database_title": ncfg.get("database_title"),
                               "template": ncfg.get("template")},
                    "samples": db.one("SELECT COUNT(*) n FROM samples WHERE channel_id=?", c["id"])["n"], "samples_busy": busy,
                    "compile": _compile_state(c["id"], prompts[0] if prompts else None, pbs),
                    "pending_playbook": next((p["id"] for p in pbs if str(p["id"]) in pending), None)})
    uni = db.one("SELECT id, prompt_id, version, status, origin, created_at, activated_at, data, audit FROM playbooks "
                 "WHERE channel_id IS NULL AND status='ACTIVE' ORDER BY version DESC LIMIT 1")
    return {"channels": out, "universal": _playbook_view(uni) if uni else None,
            "decisions": db.q("SELECT * FROM decisions WHERE resolved_at IS NULL")}


def _cookie_status() -> dict:
    from ..cookies import status
    return status()


@app.get("/api/engine")
def engine():
    s = config.get()
    du = shutil.disk_usage(s.home)
    def size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink()) if p.exists() else 0
    runs = [_run_view(r) for r in db.q("SELECT * FROM runs ORDER BY created_at DESC LIMIT 30")]
    for r in runs:
        src = db.one("SELECT title, headline FROM sources WHERE id=?", r["source_id"]) or {}
        r["title"], r["headline"] = src.get("title"), src.get("headline")
    calls = db.one("""SELECT COUNT(*) n, SUM(wall_seconds) wall, SUM(input_tokens) inp, SUM(output_tokens) outp,
                      SUM(thinking_tokens) think, MAX(peak_rss_kib) rss FROM model_calls""")
    return {
        "disk": {"total": du.total, "free": du.free, "media": size(s.media_dir), "runs": size(s.runs_dir), "packages": size(s.packages_dir)},
        "agy": db.setting("agy_health"), "agy_media": db.setting("agy_media_health"), "model": s.agy_model,
        "notion": {"token": bool(s.secret("notion-token")), "database": db.setting("notion_database_id"),
                   "parent": db.setting("notion_parent_page_id")},
        "youtube": {"profile": str(s.browser_profile) if s.browser_profile else None,
                    "profile_ok": bool(s.browser_profile and s.browser_profile.exists()), **_cookie_status()},
        "paused": db.setting("queue_paused"),
        "alerts": db.q("SELECT * FROM alerts ORDER BY resolved_at IS NOT NULL, created_at DESC LIMIT 20"),
        "runs": runs, "calls": calls,
        "events": db.q("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
        "capacity_note": "串行处理：60 分钟视频约 1.5–2 小时；每日上限约 10–12 个视频小时。",
        "stage_labels": STAGE_LABELS,
    }


@app.get("/api/runs/{rid}")
def run_detail(rid: str):
    r = db.one("SELECT * FROM runs WHERE id=?", rid)
    if not r:
        raise HTTPException(404)
    return {"run": _run_view(r), "calls": db.q("SELECT * FROM model_calls WHERE run_id=? ORDER BY id", rid),
            "events": db.q("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 200", rid)}


THUMB_WIDTHS = (320, 480, 640, 960, 1280)


def _thumb(src: Path, w: int) -> Path:
    """Downscaled JPEG next to the original, made once (screenshots are full-HD; lists show them at 120–640 px)."""
    w = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
    out = src.with_name(f"{src.stem}.w{w}.jpg")
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > w:
            im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
        tmp = out.with_name(f"{out.name}.{os.getpid()}.{threading.get_ident()}.part")
        im.save(tmp, "JPEG", quality=80, optimize=True, progressive=True)
    tmp.replace(out)
    return out


@app.get("/media/{asset_id}")
def media(asset_id: int, w: int | None = None):
    a = db.one("SELECT path, kind FROM assets WHERE id=?", asset_id)
    if not a or not a["path"] or not Path(a["path"]).exists():
        raise HTTPException(404)
    if w and a["kind"] in ("screenshot", "poster"):
        return FileResponse(_thumb(Path(a["path"]), w), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})
    mt = {"clip": "video/mp4", "clip_subtitled": "video/mp4", "screenshot": "image/jpeg", "poster": "image/jpeg",
          "srt": "text/plain; charset=utf-8", "ass": "text/plain; charset=utf-8"}.get(a["kind"])
    return FileResponse(a["path"], media_type=mt, headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ writes
class Submit(BaseModel):
    url: str
    priority: int = 0


@app.post("/api/submit")
def submit(body: Submit):
    from ..pipeline.runner import submit as do
    try:
        return do(body.url.strip(), priority=body.priority)
    except ValueError as e:
        raise HTTPException(400, str(e))


class FlagUpdate(BaseModel):
    resolution: str
    note: str | None = None


@app.post("/api/flags/{fid}")
def flag_update(fid: int, body: FlagUpdate):
    if body.resolution not in ("OPEN", "ACCEPTED", "FIXED", "DISMISSED"):
        raise HTTPException(400)
    db.exec("UPDATE flags SET resolution=?, resolution_note=?, resolved_at=? WHERE id=?", body.resolution, body.note,
            None if body.resolution == "OPEN" else now(), fid)
    return db.one("SELECT * FROM flags WHERE id=?", fid)


class UnitUpdate(BaseModel):
    decision: str | None = None
    note: str | None = None


@app.post("/api/units/{uid}")
def unit_update(uid: str, body: UnitUpdate):
    db.exec("UPDATE units SET decision=?, note=COALESCE(?, note) WHERE id=?", body.decision, body.note, uid)
    return {"ok": True}


class AssetUpdate(BaseModel):
    decision: str | None = None
    publication_priority: str | None = None
    embed: bool | None = None


@app.post("/api/assets/{aid}")
def asset_update(aid: int, body: AssetUpdate):
    if body.publication_priority:
        db.exec("UPDATE assets SET publication_priority=? WHERE id=?", body.publication_priority, aid)
    if body.embed is not None:
        db.exec("UPDATE assets SET embed=? WHERE id=?", int(body.embed), aid)
    if body.decision is not None:
        db.exec("UPDATE assets SET decision=? WHERE id=?", body.decision or None, aid)
    return {"ok": True}


class Nudge(BaseModel):
    start: float
    end: float


@app.post("/api/assets/{aid}/nudge")
def nudge(aid: int, body: Nudge):
    """Minor Edit: request a deterministic re-cut of one clip (no model call). The worker applies it."""
    a = db.one("SELECT a.*, s.duration, s.media_state FROM assets a JOIN sources s ON s.id=a.source_id WHERE a.id=? AND a.kind='clip'", aid)
    if not a:
        raise HTTPException(404)
    if a["media_state"] != "CACHED":
        raise HTTPException(409, "原始媒体已清理，无法重剪")
    if not (0 <= body.start < body.end <= (a["duration"] or 1e9)) or body.end - body.start < 3:
        raise HTTPException(400, "边界无效")
    meta = db.unj(a["meta"], {}) | {"pending_nudge": {"start": body.start, "end": body.end, "requested_at": now()}}
    db.exec("UPDATE assets SET meta=? WHERE id=?", db.j(meta), aid)
    db.event("review", f"请求微调片段边界 {body.start:.1f}–{body.end:.1f}", a["run_id"])
    return {"ok": True}


@app.post("/api/sources/{sid}/deliver")
def redeliver(sid: str):
    db.exec("UPDATE sources SET delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END, delivery_reason=NULL WHERE id=?", sid)
    return {"ok": True, "note": "Worker 会在空闲时交付"}


@app.post("/api/runs/{rid}/retry")
def retry(rid: str):
    r = db.one("SELECT * FROM runs WHERE id=?", rid)
    if not r or r["production"] not in ("FAILED", "WAITING", "INTERRUPTED", "COMPLETE_PARTIAL"):
        raise HTTPException(400, "run not retryable")
    first = db.one("SELECT MIN(ord) o FROM stages WHERE run_id=? AND status IN ('FAILED','WARN','RUNNING')", rid)["o"]
    if first is not None:  # everything downstream of the first problem is rebuilt
        db.exec("UPDATE stages SET status='PENDING' WHERE run_id=? AND ord>=? AND name NOT IN ('TRANSCRIBE')", rid, first)
        db.exec("UPDATE stages SET status='PENDING' WHERE run_id=? AND name='TRANSCRIBE' AND status!='DONE'", rid)
    db.exec("UPDATE runs SET production='QUEUED', wait_reason=NULL, error=NULL, error_class=NULL, attempts=0 WHERE id=?", rid)
    return {"ok": True}


@app.post("/api/playbooks/{pid}/confirm")
def confirm_playbook(pid: int):
    p = db.one("SELECT * FROM playbooks WHERE id=?", pid)
    if not p:
        raise HTTPException(404)
    db.exec("UPDATE playbooks SET status='SUPERSEDED' WHERE channel_id=? AND status='ACTIVE'", p["channel_id"])
    db.exec("UPDATE playbooks SET status='ACTIVE', activated_at=? WHERE id=?", now(), pid)
    db.exec("UPDATE decisions SET resolved_at=?, outcome='CONFIRMED' WHERE kind='CONFIRM_PLAYBOOK' AND ref=?", now(), str(pid))
    db.exec("""UPDATE runs SET production='QUEUED', wait_reason=NULL WHERE production='WAITING' AND wait_reason='PLAYBOOK_CONFIRMATION'
               AND source_id IN (SELECT id FROM sources WHERE channel_id=?)""", p["channel_id"])
    # 定稿 (design §R2-1): the channel leaves tuning, its held samples go to Notion, new uploads come in by RSS
    ch = db.one("SELECT * FROM channels WHERE id=?", p["channel_id"]) if p["channel_id"] else None
    if ch:
        db.exec("UPDATE channels SET stage='LOCKED', updated_at=? WHERE id=?", now(), ch["id"])
        if ch["mode"] == "TRACKED" and ch["youtube_channel_id"]:
            from ..ingest import set_enabled
            set_enabled(ch["id"], True)
    return {"ok": True}


@app.post("/api/youtube/remote-browser")
def remote_browser():
    """Start the server-side Chromium + noVNC used for interactive YouTube login (loopback only)."""
    import os
    import subprocess
    script = os.environ.get("ASSETIZER_REMOTE_BROWSER", "/home/debian/video-jobs/start-browser.sh")
    if not Path(script).exists():
        raise HTTPException(404, "remote browser script not found")
    r = subprocess.run(["bash", script], capture_output=True, text=True, timeout=60)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-500:],
            "tunnel": "ssh -N -L 6080:127.0.0.1:6080 ovh-investment", "url": "http://localhost:6080/vnc.html"}


@app.post("/api/cookiecloud/credentials")
async def cookiecloud_credentials(request: Request):
    """Set or clear the CookieCloud origin and secrets (docs/cookiecloud-credentials-ui.md). Write-only: the answer is
    the status, never a value. The body is parsed by hand because a pydantic model's 422 would echo rejected values."""
    from .. import cookies
    raw = await request.body()
    if len(raw) > 16_384:
        return JSONResponse({"detail": "请求体太大"}, status_code=413)
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        return JSONResponse({"detail": "请求体必须是 JSON 对象"}, status_code=400)
    try:
        await run_in_threadpool(cookies.save_credentials, body)
    except cookies.CredentialError as e:
        return JSONResponse({"detail": e.message, "field": e.field}, status_code=e.status)
    except OSError as e:
        return JSONResponse({"detail": f"写入服务器上的凭据文件失败（{type(e).__name__}）"}, status_code=500)
    return _cookie_status()


COOKIE_TEST_INTERVAL = 10.0  # seconds: Cloudflare allows 5 reads per 10 s per IP, shared with downloads
VIDEO_TEST_INTERVAL = 20.0   # seconds: each video test is a CookieCloud read plus a YouTube page from the server
_throttle_at: dict[str, float] = {}
_throttle_lock = threading.Lock()
_video_test_lock = threading.Lock()


def _throttled(key: str, interval: float) -> JSONResponse | None:
    """A 429 when `key` last ran less than `interval` seconds ago; otherwise None, and this run is recorded."""
    import time
    with _throttle_lock:
        wait = _throttle_at.get(key, float("-inf")) + interval - time.monotonic()
        if wait > 0:
            secs = int(wait) + 1
            return JSONResponse({"detail": f"测试太频繁：{secs} 秒后再试"}, status_code=429, headers={"Retry-After": str(secs)})
        _throttle_at[key] = time.monotonic()
    return None


@app.post("/api/cookiecloud/test")
def cookiecloud_test():
    """One CookieCloud read for the 机房 button; at most one every COOKIE_TEST_INTERVAL seconds (429 otherwise)."""
    from .. import cookies
    if busy := _throttled("connection", COOKIE_TEST_INTERVAL):
        return busy
    out = cookies.probe()
    db.event("config", f"测试 CookieCloud 连接：读到 {out['count']} 条" if out["ok"]
             else f"测试 CookieCloud 连接失败（错误码 {out['code']}）", level="info" if out["ok"] else "warn")
    return out


class VideoTest(BaseModel):
    url: str


@app.post("/api/cookiecloud/test-video")
def cookiecloud_test_video(body: VideoTest):
    """The 机房 video test: does YouTube accept the CookieCloud login for this video (ideally a members-only one)?
    A pass also wakes the runs waiting for a YouTube login instead of leaving them to the 30-minute retry."""
    from .. import youtube
    url = body.url.strip()
    if not youtube.video_id(url):
        return JSONResponse({"detail": "请粘贴一个 YouTube 视频链接（watch、youtu.be、shorts、live 链接都可以）"}, status_code=400)
    if not _video_test_lock.acquire(blocking=False):
        return JSONResponse({"detail": "上一次视频测试还没结束"}, status_code=429)
    try:
        if busy := _throttled("video", VIDEO_TEST_INTERVAL):
            return busy
        out = youtube.probe_video(url)
        if out["ok"]:
            out["woken"] = db.connect().execute(
                "UPDATE runs SET production='QUEUED' WHERE production='WAITING' AND wait_reason='YOUTUBE_AUTH'").rowcount
        db.set_setting("youtube_video_test", out)
        title = (out["video"] or {}).get("title") or out["url"]
        db.event("config", (f"视频测试通过：{title}" + (f"；{out['woken']} 个等待登录的视频已重新排队" if out.get("woken") else ""))
                 if out["ok"] else f"视频测试未通过（{out['verdict']}）：{title}", level="info" if out["ok"] else "warn")
        return out
    finally:
        _video_test_lock.release()


class Ingest(BaseModel):
    enabled: bool
    min_minutes: int = 10


@app.post("/api/channels/{cid}/ingest")
def channel_ingest(cid: str, body: Ingest):
    from ..ingest import set_enabled
    if not db.one("SELECT id FROM channels WHERE id=? AND mode='TRACKED'", cid):
        raise HTTPException(404)
    return set_enabled(cid, body.enabled, body.min_minutes)


def _bg(fn, *args, label: str = "") -> None:
    def run():
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            db.event("compile", f"{label}失败：{e}", level="error")
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ 频道调试台
def _tuning_error(e: ValueError):
    raise HTTPException(400, str(e))


@app.get("/api/channels/{cid}/workbench")
def channel_workbench(cid: str):
    from .. import tuning
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    return tuning.workbench(cid)


@app.get("/api/channels/{cid}/candidates")
def channel_candidates(cid: str):
    """Recent uploads from the channel feed, marked 已是样片 / 已生产 / 库里已有."""
    from ..delivery import registered_many
    from ..ingest import feed
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not ch or not ch.get("youtube_channel_id"):
        raise HTTPException(404)
    try:
        entries = feed(ch["youtube_channel_id"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"读不到频道最近的视频：{e}")
    samples = {r["source_id"] for r in db.q("SELECT source_id FROM samples WHERE channel_id=?", cid)}
    try:
        lib = registered_many({"channel_id": cid}, [e["id"] for e in entries])
    except Exception:  # noqa: BLE001
        lib = {}
    out = []
    for e in entries:
        s = db.one("SELECT current_run_id FROM sources WHERE id=?", e["id"])
        out.append({**e, "sample": e["id"] in samples, "produced": bool(s and s["current_run_id"]), "library": lib.get(e["id"])})
    return {"candidates": out}


class SampleBody(BaseModel):
    url: str
    run: bool = False
    playbook_id: int | None = None


@app.post("/api/channels/{cid}/samples")
def channel_sample_add(cid: str, body: SampleBody):
    from .. import tuning
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not ch:
        raise HTTPException(404)
    if ch["stage"] != "TUNING":
        raise HTTPException(400, "已定稿的频道请先「回到调试期」再添加样片")
    try:
        sid = tuning.add_sample(cid, body.url.strip())
        runs = []
        if body.run:
            pb = body.playbook_id or (tuning.draft_playbook(cid) or {}).get("id")
            if not pb:
                raise ValueError("这个频道还没有规程，先写学习目标并编译")
            runs = tuning.trial(cid, [pb], [sid])
    except ValueError as e:
        _tuning_error(e)
    return {"source_id": sid, "runs": runs, **tuning.workbench(cid)}


@app.post("/api/channels/{cid}/samples/{sid}/remove")
def channel_sample_remove(cid: str, sid: str):
    from .. import tuning
    db.exec("DELETE FROM samples WHERE channel_id=? AND source_id=?", cid, sid)
    return tuning.workbench(cid)


class TrialBody(BaseModel):
    playbook_id: int | None = None
    playbook_ids: list[int] | None = None
    source_ids: list[str] | None = None


@app.post("/api/channels/{cid}/trial")
def channel_trial(cid: str, body: TrialBody):
    from .. import tuning
    pids = body.playbook_ids or ([body.playbook_id] if body.playbook_id else [])
    try:
        runs = tuning.trial(cid, pids, body.source_ids)
    except ValueError as e:
        _tuning_error(e)
    return {"runs": runs, **tuning.workbench(cid)}


@app.post("/api/channels/{cid}/trial-queue/clear")
def channel_trial_clear(cid: str):
    from .. import tuning
    n = tuning.clear_queue(cid)
    return {"cancelled": n, **tuning.workbench(cid)}


class VoteBody(BaseModel):
    liked: bool
    note: str | None = None


@app.post("/api/runs/{rid}/vote")
def run_vote(rid: str, body: VoteBody):
    from .. import tuning
    try:
        return tuning.vote(rid, body.liked, body.note)
    except ValueError as e:
        _tuning_error(e)


@app.get("/api/compare")
def compare_runs(a: str, b: str):
    from .. import tuning
    ra, rb = (db.one("SELECT source_id FROM runs WHERE id=?", x) for x in (a, b))
    if not ra or not rb or ra["source_id"] != rb["source_id"]:
        raise HTTPException(400, "只能对比同一期视频的两个版本")
    return tuning.compare(a, b)


class AdoptBody(BaseModel):
    run_id: str


@app.post("/api/sources/{sid}/adopt")
def adopt_run(sid: str, body: AdoptBody):
    from .. import tuning
    try:
        tuning.adopt(sid, body.run_id)
    except ValueError as e:
        _tuning_error(e)
    return {"ok": True}


@app.post("/api/channels/{cid}/retune")
def channel_retune(cid: str):
    from .. import tuning
    try:
        return tuning.retune(cid)
    except ValueError as e:
        _tuning_error(e)


# ------------------------------------------------------------------ Notion target per channel
def _notion():
    from ..delivery import client
    n = client()
    if not n:
        raise HTTPException(409, "还没有配置 Notion token")
    return n


def _notion_view(cid: str, cfg: dict) -> dict:
    from ..notion import mapping as M
    from ..notion.client import NotionError
    n = _notion()
    try:
        dbo = n._req("GET", f"/databases/{cfg['database_id']}")
    except NotionError as e:
        from ..delivery import TOKEN_FIX, token_invalid
        if token_invalid(e):
            raise HTTPException(502, f"Notion token 已失效（401），读不到数据库。{TOKEN_FIX}")
        if e.status in (403, 404):
            raise HTTPException(502, f"Integration 没有这个数据库的权限（{e.status}）：在数据库右上角 ··· → Connections 里添加它。")
        raise HTTPException(502, f"读不到目标数据库：{e}")
    schema = M.schema_of(dbo)
    title = "".join(t.get("plain_text", "") for t in dbo.get("title", []))
    if title and cfg.get("database_title") != title:
        M.save(cid, {**(M.get(cid) or cfg), "database_title": title})
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    s = db.one("SELECT * FROM sources WHERE channel_id=? AND current_run_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1", cid)
    ctx = M.context(s, s["current_run_id"], ch) if s else None
    view = M.preview(cfg, schema, ctx)
    titles = {}
    for r in view["rows"]:
        m = r.get("mapping") or {}
        if r["type"] == "relation" and m.get("source") == "fixed":
            for pid in m.get("value") or []:
                try:
                    titles[pid] = _page_title(n.page(pid))
                except NotionError:
                    titles[pid] = None
    return {"database": {"id": dbo["id"], "url": dbo.get("url"), "title": "".join(t.get("plain_text", "") for t in dbo.get("title", []))},
            "config": {k: cfg.get(k) for k in ("template", "heading", "icon")}, **view, "page_titles": titles,
            "sample_source": s and {"id": s["id"], "title": s["title"]},
            "templates": M.TEMPLATES, "source_labels": {k: v[0] for k, v in M.SOURCES.items()},
            "template_vars": M.TEMPLATE_VARS, "access_labels": M.ACCESS_LABELS}


def _page_title(pg: dict) -> str:
    for p in pg.get("properties", {}).values():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p["title"])
    return ""


@app.get("/api/channels/{cid}/notion")
def channel_notion(cid: str):
    from ..notion import mapping as M
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    cfg = M.get(cid)
    if not cfg or not cfg.get("database_id"):
        return {"configured": False}
    return {"configured": True, **_notion_view(cid, cfg)}


class NotionMapping(BaseModel):
    properties: dict
    template: str = "default"
    heading: str = ""
    icon: str = "none"


@app.post("/api/channels/{cid}/notion")
def channel_notion_save(cid: str, body: NotionMapping):
    from ..notion import mapping as M
    cfg = M.get(cid)
    if not cfg or not cfg.get("database_id"):
        raise HTTPException(409, "这个频道还没有 Notion 目标")
    if body.template not in M.TEMPLATES:
        raise HTTPException(400, "unknown template")
    props = {}
    for pid, m in body.properties.items():
        if not m or not m.get("source"):
            continue  # 不填
        if m["source"] not in M.SOURCES:
            raise HTTPException(400, f"unknown source {m['source']}")
        props[pid] = {k: m[k] for k in ("name", "type", "source", "value") if k in m}
    cfg = {**cfg, "properties": props, "template": body.template, "heading": body.heading.strip(), "icon": body.icon or "none"}
    M.save(cid, cfg)
    # sources paused on the old mapping try again now
    db.exec("""UPDATE sources SET delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END, delivery_reason=NULL
               WHERE channel_id=? AND delivery='BLOCKED' AND delivery_reason LIKE 'NOTION_MAPPING%'""", cid)
    db.event("notion", f"已更新「{cid}」的 Notion 属性映射")
    return {"configured": True, **_notion_view(cid, cfg)}


@app.get("/api/notion/pages")
def notion_pages(database: str, q: str = ""):
    """Pages of a related database for the relation picker (title contains q)."""
    from ..notion import mapping as M
    n = _notion()
    schema = M.schema_of(n._req("GET", f"/databases/{database}"))
    title = next((v["name"] for v in schema.values() if v["type"] == "title"), None)
    body: dict = {"page_size": 20}
    if q.strip() and title:
        body["filter"] = {"property": title, "title": {"contains": q.strip()}}
    r = n._req("POST", f"/databases/{database}/query", idempotent=True, json=body)
    return {"pages": [{"id": p["id"], "title": _page_title(p), "url": p.get("url")} for p in r.get("results", [])]}


class PromptBody(BaseModel):
    text: str
    note: str | None = None


@app.post("/api/channels/{cid}/prompt")
def channel_prompt(cid: str, body: PromptBody):
    """Save a new raw prompt version and compile it (text-only model calls, background)."""
    from ..compiler import add_prompt_version, compile_prompt
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    if len(body.text.strip()) < 30:
        raise HTTPException(400, "Prompt 太短")
    pid = add_prompt_version(cid, body.text.strip(), "manual", body.note)
    _bg(compile_prompt, cid, pid, label=f"Playbook 编译（{cid}）")
    db.event("compile", f"{cid} 原始 Prompt 新版本已保存，开始编译")
    return {"prompt_id": pid, "compiling": True}


class DraftBody(BaseModel):
    name: str
    why: str
    source_id: str | None = None


@app.post("/api/channels/draft")
def channel_draft(body: DraftBody):
    from ..compiler import draft_prompt
    titles = []
    if body.source_id:
        s = db.one("SELECT youtube_channel_id FROM sources WHERE id=?", body.source_id)
        if s and s["youtube_channel_id"]:
            try:
                from ..ingest import feed
                titles = [e["title"] for e in feed(s["youtube_channel_id"])]
            except Exception:  # noqa: BLE001
                titles = []
    return {"raw_prompt": draft_prompt(body.name, body.why, titles)}


class TrackBody(BaseModel):
    name: str
    why: str
    prompt: str
    source_id: str


@app.post("/api/channels/track")
def channel_track(body: TrackBody):
    """Onboarding: turn a source's (unknown) channel into a TRACKED channel and compile its prompt."""
    from ..compiler import add_prompt_version, compile_prompt
    from ..util import slug
    s = db.one("SELECT * FROM sources WHERE id=?", body.source_id)
    if not s or not s.get("youtube_channel_id"):
        raise HTTPException(400, "这一期还没有解析出频道信息")
    ch = db.one("SELECT * FROM channels WHERE youtube_channel_id=?", s["youtube_channel_id"])
    cid = ch["id"] if ch else (slug(body.name) if slug(body.name) != "item" else s["youtube_channel_id"].lower())
    if not ch:
        db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, why, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                cid, body.name, s["youtube_channel_id"], "TRACKED", body.why, now(), now())
    else:
        db.exec("UPDATE channels SET mode='TRACKED', why=?, updated_at=? WHERE id=?", body.why, now(), cid)
    db.exec("UPDATE sources SET channel_id=? WHERE youtube_channel_id=?", cid, s["youtube_channel_id"])
    pid = add_prompt_version(cid, body.prompt.strip(), "onboarding")
    _bg(compile_prompt, cid, pid, label=f"Playbook 编译（{cid}）")
    return {"channel_id": cid, "prompt_id": pid}


class ChannelInfo(BaseModel):
    name: str | None = None
    why: str | None = None


@app.post("/api/channels/{cid}")
def channel_update(cid: str, body: ChannelInfo):
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    if body.name is not None and not body.name.strip():
        raise HTTPException(400, "频道名称不能为空")
    if body.name is not None:
        db.exec("UPDATE channels SET name=?, updated_at=? WHERE id=?", body.name.strip(), now(), cid)
    if body.why is not None:
        db.exec("UPDATE channels SET why=?, updated_at=? WHERE id=?", body.why.strip() or None, now(), cid)
    return db.one("SELECT id, name, why FROM channels WHERE id=?", cid)


@app.post("/api/queue/resume")
def resume():
    paused = db.setting("queue_paused")
    db.set_setting("queue_paused", None)
    reasons = ("AGY_AUTH_REQUIRED", "PROVIDER_QUOTA")
    db.exec(f"UPDATE runs SET production='QUEUED', wait_reason=NULL WHERE production='WAITING' AND wait_reason IN {reasons}")
    for r in reasons:
        db.resolve_alert(r)
    return {"ok": True, "was": paused}


# ------------------------------------------------------------------ SPA
if (STATIC / "assets").exists():
    app.mount("/assets", ImmutableStatic(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}")
def spa(path: str, request: Request):
    if path.startswith(("api/", "media/")):
        raise HTTPException(404)
    f = (STATIC / path).resolve()
    if path and f.is_relative_to(STATIC) and f.is_file():
        return FileResponse(f)
    idx = STATIC / "index.html"
    if idx.exists():
        return FileResponse(idx, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "frontend not built"}, status_code=503)
