"""FastAPI app: JSON API + media + the built SPA."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, db
from ..util import now

app = FastAPI(title="Assetizer", docs_url="/api/docs")
STATIC = Path(__file__).with_name("static").resolve()
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


@app.middleware("http")
async def guard(request: Request, call_next):
    """Loopback app behind an SSH tunnel: block DNS rebinding and cross-site form posts."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    if request.method == "POST" and not (request.headers.get("content-type") or "").startswith("application/json"):
        return JSONResponse({"error": "json required"}, status_code=415)
    return await call_next(request)

STAGE_LABELS = {
    "RESOLVE": "解析来源", "PLAYBOOK": "绑定频道规则", "ACQUIRE": "获取媒体", "CHUNK": "切分", "UNDERSTAND": "多模态理解",
    "TRANSCRIBE": "全片转写", "MERGE": "合并", "KNOWLEDGE": "知识单元", "ARTICLE": "撰写文章", "SELECT": "选片",
    "SCREENSHOTS": "截图", "BOUNDARY": "边界精修", "CLIPS": "剪辑", "SUBTITLES": "字幕", "MEDIA_QA": "媒体质检",
    "REVIEW_CHECK": "朱批预审", "COMPOSE": "编排", "PACKAGE": "发布素材包", "CLEANUP": "清理",
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
    latest = db.one("SELECT * FROM runs WHERE source_id=? ORDER BY created_at DESC LIMIT 1", s["id"])
    summ = db.unj(run["summary"], {}) if run else {}
    flags = db.one("SELECT COUNT(*) n FROM flags WHERE source_id=? AND run_id IS ? AND resolution='OPEN' AND severity!='INFO'",
                   s["id"], s.get("current_run_id"))["n"]
    must = db.one("SELECT COUNT(*) n FROM units WHERE run_id IS ? AND learning_priority=3", s.get("current_run_id"))["n"]
    clips = sum(v for k, v in (summ.get("assets") or {}).items() if k == "clip:GENERATED")
    return {"no": n, "id": s["id"], "title": s.get("title") or s["id"], "channel": s.get("channel_title"),
            "channel_id": s.get("channel_id"), "published_at": s.get("published_at"), "duration": s.get("duration"),
            "review": s["review"], "delivery": s["delivery"], "notion_url": s.get("notion_url"),
            "units": summ.get("units"), "must": must, "clips": clips, "flags": flags, "cover": _cover(s.get("current_run_id")),
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
        "open_flags": db.one("""SELECT COUNT(*) n FROM flags f JOIN sources s ON s.id=f.source_id AND s.current_run_id=f.run_id
                                WHERE f.resolution='OPEN' AND f.severity!='INFO'""")["n"],
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
    playbook = db.one("SELECT id, channel_id, version, status, data FROM playbooks WHERE id=?", r["playbook_id"]) if r and r.get("playbook_id") else None
    if playbook:
        playbook["facets"] = db.unj(playbook.pop("data"), {}).get("facets", [])
    return {
        "source": {k: v for k, v in s.items() if k not in ("info",)} | {"info": db.unj(s.get("info"), {})},
        "run": _run_view(r), "units": units, "assets": assets, "playbook": playbook,
        "sections": [{**x, "unit_ids": db.unj(x["unit_ids"], [])} for x in db.q("SELECT * FROM sections WHERE run_id=? ORDER BY ord", rid)],
        "flags": [{**f, "evidence": db.unj(f["evidence"], None)} for f in db.q("SELECT * FROM flags WHERE run_id=? ORDER BY severity, id", rid)],
        "speakers": db.q("SELECT * FROM speakers WHERE run_id=?", rid),
        "runs": db.q("SELECT id, production, created_at, finished_at FROM runs WHERE source_id=? ORDER BY created_at DESC", sid),
        "channel": db.one("SELECT id, name, mode FROM channels WHERE id=?", s.get("channel_id")),
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


@app.get("/api/channels")
def channels():
    out = []
    for c in db.q("SELECT * FROM channels ORDER BY mode DESC, created_at"):
        pbs = db.q("SELECT id, version, status, origin, created_at, activated_at, data, audit FROM playbooks WHERE channel_id=? ORDER BY version DESC", c["id"])
        for p in pbs:
            d = db.unj(p.pop("data"), {})
            p["audit"] = db.unj(p["audit"], {})
            p["rules"] = d.get("rules", [])
            p["facets"] = d.get("facets", [])
            p["identity"] = d.get("identity", {})
            p["article_style"] = d.get("article_style")
        prompts = db.q("SELECT id, version, sha256, origin, origin_edited_at, created_at, text FROM channel_prompts WHERE channel_id=? ORDER BY version DESC", c["id"])
        stats = db.one("SELECT COUNT(*) n, MAX(created_at) last FROM sources WHERE channel_id=?", c["id"])
        out.append({**c, "playbooks": pbs, "prompts": prompts, "sources": stats["n"], "last_source": stats["last"],
                    "ingest": db.setting(f"ingest:{c['id']}") or {"enabled": False}})
    uni = db.one("SELECT id, version, status FROM playbooks WHERE channel_id IS NULL AND status='ACTIVE'")
    return {"channels": out, "universal": uni,
            "decisions": db.q("SELECT * FROM decisions WHERE resolved_at IS NULL")}


@app.get("/api/engine")
def engine():
    s = config.get()
    du = shutil.disk_usage(s.home)
    def size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink()) if p.exists() else 0
    runs = [_run_view(r) for r in db.q("SELECT * FROM runs ORDER BY created_at DESC LIMIT 30")]
    for r in runs:
        r["title"] = (db.one("SELECT title FROM sources WHERE id=?", r["source_id"]) or {}).get("title")
    calls = db.one("""SELECT COUNT(*) n, SUM(wall_seconds) wall, SUM(input_tokens) inp, SUM(output_tokens) outp,
                      SUM(thinking_tokens) think, MAX(peak_rss_kib) rss FROM model_calls""")
    return {
        "disk": {"total": du.total, "free": du.free, "media": size(s.media_dir), "runs": size(s.runs_dir), "packages": size(s.packages_dir)},
        "agy": db.setting("agy_health"), "model": s.agy_model,
        "notion": {"token": bool(s.secret("notion-token")), "database": db.setting("notion_database_id"),
                   "parent": db.setting("notion_parent_page_id")},
        "youtube": {"profile": str(s.browser_profile) if s.browser_profile else None,
                    "profile_ok": bool(s.browser_profile and s.browser_profile.exists())},
        "paused": db.setting("queue_paused"),
        "alerts": db.q("SELECT * FROM alerts ORDER BY resolved_at IS NOT NULL, created_at DESC LIMIT 20"),
        "runs": runs, "calls": calls,
        "events": db.q("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
        "capacity_note": "串行处理：60 分钟视频约 1.5–2 小时；每日上限约 10–12 个视频小时。",
    }


@app.get("/api/runs/{rid}")
def run_detail(rid: str):
    r = db.one("SELECT * FROM runs WHERE id=?", rid)
    if not r:
        raise HTTPException(404)
    return {"run": _run_view(r), "calls": db.q("SELECT * FROM model_calls WHERE run_id=? ORDER BY id", rid),
            "events": db.q("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 200", rid)}


@app.get("/media/{asset_id}")
def media(asset_id: int):
    a = db.one("SELECT path, kind FROM assets WHERE id=?", asset_id)
    if not a or not a["path"] or not Path(a["path"]).exists():
        raise HTTPException(404)
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
    f = db.one("SELECT source_id FROM flags WHERE id=?", fid)
    if f:
        db.exec("UPDATE sources SET review=CASE WHEN review='UNREVIEWED' THEN 'IN_REVIEW' ELSE review END WHERE id=?", f["source_id"])
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


class Review(BaseModel):
    review: str
    notes: str | None = None


@app.post("/api/sources/{sid}/review")
def review(sid: str, body: Review):
    if body.review not in ("UNREVIEWED", "IN_REVIEW", "APPROVED", "CHANGES_REQUESTED", "REJECTED"):
        raise HTTPException(400)
    db.exec("UPDATE sources SET review=?, review_notes=COALESCE(?, review_notes), review_at=?, updated_at=? WHERE id=?",
            body.review, body.notes, now(), now(), sid)
    from ..delivery import sync_review
    threading.Thread(target=sync_review, args=(sid,), daemon=True).start()
    return db.one("SELECT id, review, review_at FROM sources WHERE id=?", sid)


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
    _bg(compile_prompt, cid, pid, label="Playbook 编译")
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
    _bg(compile_prompt, cid, pid, label="Playbook 编译")
    return {"channel_id": cid, "prompt_id": pid}


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
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


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
