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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config, db, knobs, notify, playback
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
# On LazyCat the gateway logs the user in and forwards X-HC-User-Id to the `web` service by name (Host: web:8770,
# X-Forwarded-Host: assetizer.<box>.heiyu.space); that name only resolves inside the app's own network.
TRUST_LAZYCAT = os.environ.get("ASSETIZER_TRUST_LAZYCAT", "") == "1"
LAZYCAT_SERVICE = "web"
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
    """Loopback app: local Host via SSH tunnel, the LazyCat gateway with a logged-in user, or the public Host only
    with a valid Access JWT. Also blocks DNS rebinding and cross-site form posts."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    path = request.scope["path"]
    if host == "notion-hook":
        if (request.method != "POST" or path != "/api/notion/webhook"
                or request.scope.get("raw_path", b"").split(b"?", 1)[0] != b"/api/notion/webhook"):
            return JSONResponse({}, status_code=404)
    elif path == "/api/notion/webhook" and host not in LOCAL_HOSTS:
        return JSONResponse({}, status_code=404)
    elif host in LOCAL_HOSTS:
        pass
    elif TRUST_LAZYCAT and host == LAZYCAT_SERVICE:
        forwarded = (request.headers.get("x-forwarded-host") or "").rsplit(":", 1)[0].lower()
        if not forwarded.endswith(".heiyu.space") or not request.headers.get("x-hc-user-id"):
            return JSONResponse({"error": "lazycat login required"}, status_code=403)
    elif PUBLIC_HOST and host == PUBLIC_HOST:
        if path.startswith("/api/notion/hook"):
            return JSONResponse({}, status_code=404)
        if not _access_ok(request.headers.get("cf-access-jwt-assertion")):
            return JSONResponse({"error": "access required"}, status_code=403)
    else:
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    if request.method in {"POST", "PUT"} and not (request.headers.get("content-type") or "").startswith("application/json"):
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
    return {**r, "summary": db.unj(r.get("summary"), {}), "pins": db.unj(r.get("pins"), {}), "retry": db.unj(r.get("retry"), None), "stages": stages}


# a sample: a video of a channel still being tuned that is not in Notion — tried out, not an issue (it is not counted);
# for queries over sources s joined to channels c
NOT_SAMPLE = "NOT (COALESCE(c.stage, '') = 'TUNING' AND s.delivery NOT IN ('DELIVERED','REGISTERED'))"


def _issues(where: str = "1=1", *args) -> list[dict]:
    """The issues (sources ``s``, oldest first) that match ``where``, for the caller to number: a few queries over all of
    them rather than a round per issue (2000 issues took 13,923 queries for one look at the 刊, a19)."""
    sources = db.q(f"SELECT s.* FROM sources s WHERE {where} ORDER BY s.created_at ASC", *args)
    runs = {r["id"]: r for r in db.q(f"SELECT r.* FROM runs r JOIN sources s ON s.current_run_id=r.id WHERE {where}", *args)}
    # the newest run (the first made of those made at once); experiment runs share the source but are not its production
    latest = {r["source_id"]: r for r in db.q(f"""SELECT * FROM (SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.source_id
                  ORDER BY r.created_at DESC, r.rowid) rn FROM runs r JOIN sources s ON s.id=r.source_id
                  WHERE r.kind!='EXPERIMENT' AND {where}) WHERE rn=1""", *args)}
    # 校注: what the system doubts in this issue — information, not a to-do (design §R2-3)
    notes = {(r["source_id"], r["run_id"]): r["n"] for r in db.q(f"""SELECT f.source_id, f.run_id, COUNT(*) n FROM flags f
                 JOIN sources s ON s.id=f.source_id WHERE f.severity!='INFO' AND {where} GROUP BY f.source_id, f.run_id""", *args)}
    stage = {r["id"]: r["stage"] for r in db.q("SELECT id, stage FROM channels")}
    musts: dict[str, list[dict]] = {}
    for u in db.q(f"""SELECT u.run_id, u.id, u.title FROM units u JOIN sources s ON s.current_run_id=u.run_id
                      WHERE u.learning_priority=3 AND {where} ORDER BY u.start, u.rowid""", *args):
        musts.setdefault(u["run_id"], []).append({"id": u["id"], "title": u["title"]})
    # the cover: a still of the most important unit, the earliest; a clip's first frame where the issue has no still
    # (setting screenshots:<channel>=off). One index lookup per issue, inside the one query
    covers = {r["run_id"]: r["id"] for r in db.q(f"""SELECT s.current_run_id run_id, (SELECT a.id FROM assets a
                  JOIN units u ON u.id=a.unit_id WHERE a.run_id=s.current_run_id AND a.kind IN ('screenshot','poster')
                  ORDER BY a.kind='screenshot' DESC, u.learning_priority DESC, a.start, a.id LIMIT 1) id FROM sources s
                  WHERE s.current_run_id IS NOT NULL AND {where}""", *args)}
    out = []
    for s in sources:
        rid = s.get("current_run_id")
        run, last = runs.get(rid), latest.get(s["id"])
        summ = db.unj(run["summary"], {}) if run else {}
        tuning = stage.get(s.get("channel_id")) == "TUNING"
        must = musts.get(rid) or []
        out.append({
            "no": None, "id": s["id"], "title": s.get("title") or s["id"], "headline": s.get("headline"), "channel": s.get("channel_title"),
            "channel_id": s.get("channel_id"), "published_at": s.get("published_at"), "duration": s.get("duration"),
            "delivery": s["delivery"], "delivery_reason": s.get("delivery_reason"), "notion_url": s.get("notion_url"),
            "tuning": tuning, "sample": tuning and s["delivery"] not in ("DELIVERED", "REGISTERED"),  # NOT_SAMPLE
            "units": summ.get("units"), "must": len(must), "notes": notes.get((s["id"], rid), 0), "cover": covers.get(rid),
            "clips": sum(v for k, v in (summ.get("assets") or {}).items() if k == "clip:GENERATED"), "musts": must[:3],
            "production": last["production"] if last else None, "stage": last["stage"] if last else None,
            "error_class": last["error_class"] if last else None, "origin": last["origin"] if last else None,
            "retry": db.unj(last.get("retry"), None) if last else None,
            # 库里可能已有 (a05): what the 刊 asks about
            "maybe": db.setting(f"maybe_have:{s['id']}") if s.get("delivery_reason") == "MAYBE_HAVE" else None,
            "latest_run": last["id"] if last else None, "updated_at": s["updated_at"],
            "ready_at": run["finished_at"] if run else None})
    return out


# ------------------------------------------------------------------ reads
@app.get("/api/status")
def status():
    """What the masthead and the pages' warning lamps need, without the issue list (/api/overview builds every issue)."""
    c = db.one("SELECT SUM(production='RUNNING') running, SUM(production IN ('QUEUED','WAITING','INTERRUPTED')) queued FROM runs")
    return {"alerts": db.q("SELECT key, severity, title FROM alerts WHERE resolved_at IS NULL ORDER BY created_at DESC"),
            "decisions": db.one("SELECT COUNT(*) n FROM decisions WHERE resolved_at IS NULL")["n"],
            "running": c["running"] or 0, "queued": c["queued"] or 0, "paused": db.setting("queue_paused")}


@app.get("/api/overview")
def overview():
    from ..pipeline.runner import worker_slots
    issues, n = [], 0
    for it in _issues():  # samples take no issue number
        if not it["sample"]:
            n += 1
            it["no"] = n
        issues.append(it)
    queue = db.q("""SELECT r.id, r.source_id, r.production, r.wait_reason, s.title, s.url FROM runs r JOIN sources s ON s.id=r.source_id
                    WHERE r.production IN ('QUEUED','WAITING','INTERRUPTED') ORDER BY r.priority DESC, r.created_at""")
    running = []  # every run in production at once (setting worker_slots), the one furthest along first
    for r in db.q("SELECT * FROM runs WHERE production='RUNNING' ORDER BY started_at"):
        p = _run_view(r)
        p["source"] = db.one("SELECT id, title, channel_title, duration FROM sources WHERE id=?", p["source_id"])
        p["eta_seconds"] = _eta(p)
        running.append(p)
    running.sort(key=lambda p: p["eta_seconds"] if p["eta_seconds"] is not None else float("inf"))
    decisions = db.q("SELECT * FROM decisions WHERE resolved_at IS NULL ORDER BY created_at DESC")
    for d in decisions:  # 定稿 opens the channel it is about
        pb = db.one("SELECT channel_id FROM playbooks WHERE id=?", d["ref"]) if d["kind"] == "CONFIRM_PLAYBOOK" and d["ref"] else None
        d["channel_id"] = pb["channel_id"] if pb else None
    # plain JSON already: sent as it is, not walked through FastAPI's encoder (55 of 237 ms for 2000 issues, a19)
    return JSONResponse({
        "issues": list(reversed(issues)),
        "running": running, "slots": worker_slots(), "queue": queue, "drain_seconds": _drain(running),
        "alerts": db.q("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY created_at DESC"),
        "decisions": decisions,
        "paused": db.setting("queue_paused"),
        "now": now(),
    })


# rough per-stage cost model (seconds per source-minute), refined from history later
from ..tuning import STAGE_COST  # noqa: E402


def _drain(running: list[dict] | None = None) -> int:
    """Seconds until everything running or queued is through, roughly: each slot works off what is left of its run, and
    each queued run (an hour of video when its length is not known yet), in queue order, goes to the slot free first.
    So it is never shorter than the longest run in production."""
    import heapq
    from ..pipeline.runner import worker_slots
    if running is None:
        running = [{**r, "stages": db.q("SELECT name, status FROM stages WHERE run_id=?", r["id"])}
                   for r in db.q("SELECT * FROM runs WHERE production='RUNNING'")]
    queued = db.q("""SELECT s.duration FROM runs r JOIN sources s ON s.id=r.source_id
                     WHERE r.production IN ('QUEUED','INTERRUPTED') ORDER BY r.priority DESC, r.created_at""")
    if not running and not queued:
        return 0
    free = [float(_eta(r) or 0) for r in running] + [0.0] * max(0, worker_slots() - len(running))
    heapq.heapify(free)
    for q in queued:
        heapq.heappush(free, heapq.heappop(free) + sum(STAGE_COST.values()) * (q["duration"] or 3600) / 60)
    return round(max(free))


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
    from ..notion.mapping import run_headline
    head = run_headline(rid) if rid and rid != s.get("current_run_id") else s.get("headline")  # a trial run has its own 题眼
    return {
        "source": {k: v for k, v in s.items() if k not in ("info",)} | {"info": db.unj(s.get("info"), {}), "headline": head},
        "run": _run_view(r), "units": units, "assets": assets, "playbook": playbook,
        "sections": [{**x, "unit_ids": db.unj(x["unit_ids"], [])} for x in db.q("SELECT * FROM sections WHERE run_id=? ORDER BY ord", rid)],
        "flags": flags,
        "speakers": db.q("SELECT * FROM speakers WHERE run_id=?", rid),
        "runs": db.q("SELECT id, kind, production, created_at, finished_at FROM runs WHERE source_id=? AND kind!='EXPERIMENT' ORDER BY created_at DESC", sid),
        "channel": db.one("SELECT id, name, mode, stage FROM channels WHERE id=?", s.get("channel_id")),
        "no": db.one("SELECT COUNT(*) n FROM sources WHERE created_at <= ?", s["created_at"])["n"],
        "playback": playback.ready(sid),
    }


@app.get("/api/sources/{sid}/playback")
def playback_file(sid: str):
    """原片栏的低清版 (playback.py): the whole video at 360p, streamed with range requests."""
    p = playback.path(sid)
    if not p or not p.exists():
        raise HTTPException(404)
    playback.touch(p)
    return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


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
    def pending(since: str) -> dict:
        err = db.one("""SELECT ts, message FROM events WHERE kind='compile' AND level='error' AND ts>=? AND message LIKE ?
                        ORDER BY id DESC LIMIT 1""", since, f"%（{cid}）%")
        return {"state": "failed", "error": err["message"], "at": err["ts"]} if err else {"state": "running", "since": since}
    again = db.setting(f"recompile:{cid}")
    if again and not any(p["id"] > again["after"] for p in pbs):  # its goal already has a version: done is a newer one
        return pending(again["since"])
    if not prompt:
        return None
    if any(p.get("prompt_id") == prompt["id"] for p in pbs):
        return {"state": "done"}
    return pending(prompt["created_at"])


@app.get("/api/channels")
def channels():
    from ..ingest import EVERY, next_round
    from .. import channel_icons
    from ..notion import mapping as M
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
        held = db.one("""SELECT SUM(delivery='BLOCKED' AND delivery_reason LIKE 'NOTION_MAPPING%') blocked, SUM(delivery='HELD') held
                         FROM sources WHERE channel_id=?""", c["id"])
        out.append({**c, "avatar": channel_icons.kept(c["id"]), "playbooks": pbs, "prompts": prompts, "sources": stats["n"], "last_source": stats["last"],
                    "ingest": {**(db.setting(f"ingest:{c['id']}") or {"enabled": False}), "every_minutes": EVERY // 60, "next_at": next_round()},
                    "blocked": held["blocked"] or 0, "held": held["held"] or 0,
                    "notion": {"configured": bool(ncfg.get("database_id")), "database_title": ncfg.get("database_title"),
                               "template": ncfg.get("template"), "test_copy": M.test_copy(ncfg)},
                    "samples": db.one("SELECT COUNT(*) n FROM samples WHERE channel_id=?", c["id"])["n"], "samples_busy": busy,
                    "stats": _channel_stats(c["id"]),
                    "compile": _compile_state(c["id"], prompts[0] if prompts else None, pbs),
                    "pending_playbook": next((p["id"] for p in pbs if str(p["id"]) in pending), None)})
    uni = db.one("SELECT id, prompt_id, version, status, origin, created_at, activated_at, data, audit FROM playbooks "
                 "WHERE channel_id IS NULL AND status='ACTIVE' ORDER BY version DESC LIMIT 1")
    return {"channels": out, "universal": _playbook_view(uni) if uni else None,
            "decisions": db.q("SELECT * FROM decisions WHERE resolved_at IS NULL")}


def _cookie_status() -> dict:
    from ..cookies import status
    return status()


@app.get("/healthz")
def healthz():
    """Liveness for the container healthcheck: the web process answers. No database or agy work."""
    return {"ok": True}


_sizes: dict = {}
_sizes_lock = threading.Lock()


def _dir_sizes(s) -> dict:
    """Bytes under media / runs / packages / topics. The 机房 and 设置 pages ask every 10 seconds and on the box these hold tens of GB in
    tens of thousands of files, so after the first count the last one is answered and a recount runs in the
    background, at most once a minute."""
    import time

    def count() -> dict:
        def size(p: Path, keep: set | None = None, skip: set | None = None) -> int:
            total = 0
            for f in p.rglob("*") if p.exists() else ():
                try:  # the worker removes files all the time: one gone between listing and stat is skipped
                    if f.is_file() and not f.is_symlink():
                        st = f.stat()
                        if skip is not None and (st.st_dev, st.st_ino) in skip:
                            continue
                        if keep is not None:
                            keep.add((st.st_dev, st.st_ino))
                        total += st.st_size
                except OSError:
                    pass
            return total
        originals: set = set()
        media = size(s.media_dir, keep=originals)
        # a topic's original is often a channel's one under a second name (topics/run.py acquire): counted once, as media
        return {"media": media, "runs": size(s.runs_dir), "packages": size(s.packages_dir),
                "topics": size(s.topics_dir, skip=originals)}

    def recount() -> None:
        try:
            v = count()
            with _sizes_lock:
                _sizes["v"] = v
        finally:
            with _sizes_lock:
                _sizes["busy"] = False
    with _sizes_lock:
        if _sizes.get("home") != str(s.home):
            _sizes.clear()
        if "v" in _sizes:
            if time.monotonic() - _sizes["at"] > 60 and not _sizes.get("busy"):
                _sizes.update(at=time.monotonic(), busy=True)
                threading.Thread(target=recount, name="dir-sizes", daemon=True).start()
            return _sizes["v"]
    v = count()
    with _sizes_lock:
        _sizes.update(v=v, at=time.monotonic(), home=str(s.home), busy=False)
    return v


def _providers() -> dict:
    from ..providers import in_use, resolve
    return {"default": resolve(knobs.get("llm_provider")), "stages": {k: resolve(v) for k, v in knobs.get("stage_providers").items()},
            "in_use": list(dict.fromkeys(p.name for p in in_use())),
            # 专题's steps: where each goes now, and the ones set on the page (the rest follow their defaults)
            "topic": {k: resolve(v) for k, v in knobs.topic_routes().items()},
            "topic_set": {k: resolve(v) for k, v in knobs.get("topic_providers").items()}}


def _box() -> dict:
    """What the box is set to and how each part of it is doing: all the 设置 page reads, and the 机房 reads it too."""
    from .. import ingest
    from ..topics import backup
    from ..providers import magpie_state
    from ..providers.agy import groups_in_use, quota_logs, route_models
    s = config.get()
    magpie_state.read_if_stale()
    du = shutil.disk_usage(s.home)
    q = db.setting("agy_quota")
    return {
        "disk": {"total": du.total, "free": du.free, **_dir_sizes(s)},
        "agy": db.setting("agy_health"), "agy_media": db.setting("agy_media_health"), "model": s.agy_model, "agy_models": route_models(),
        # agy's quota with the groups of it the stages draw on now, and a week of each Gemini group
        "quota": {"agy": {**q, "in_use": [g["name"] for g in groups_in_use(q)]} if q else None, "agy_log": quota_logs()},
        "magpie": magpie_state.view(), "gdrive": backup.view(),
        "providers": _providers(),
        "notion": _notion_box(),
        "knobs": knobs.view(), "backfill": ingest.backfill_view(), "asr": _asr_view(), "telegram": _telegram_view(),
        "youtube": {"profile": str(s.browser_profile) if s.browser_profile else None,
                    "profile_ok": bool(s.browser_profile and s.browser_profile.exists()), **_cookie_status()},
        "paused": db.setting("queue_paused"),
        "alerts": db.q("SELECT * FROM alerts ORDER BY resolved_at IS NOT NULL, created_at DESC LIMIT 20"),
    }


def _retries_since(since: str) -> dict:
    """What trying again came to, by the kind of error: the calls that needed it, the tries (asking another model is one), the
    calls that then got through."""
    return {r["kind"]: {"calls": r["n"], "tried": r["tried"], "rescued": r["rescued"]} for r in db.q(
        "SELECT kind, COUNT(*) n, SUM(tries + (fallback IS NOT NULL)) tried, SUM(rescued) rescued FROM retries WHERE created_at >= ? GROUP BY kind", since)}


@app.get("/api/settings")
def settings():
    from datetime import datetime, timedelta, timezone
    month = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    return {**_box(), "retry_defaults": knobs.RETRY_DEFAULTS, "retry_max": knobs.RETRY_MAX, "retries": _retries_since(month)}


@app.get("/api/engine")
def engine():
    from ..pipeline.runner import worker_slots
    runs = [_run_view(r) for r in db.q("SELECT * FROM runs ORDER BY created_at DESC LIMIT 30")]
    for r in runs:
        src = db.one("SELECT title, headline FROM sources WHERE id=?", r["source_id"]) or {}
        r["title"], r["headline"] = src.get("title"), src.get("headline")
    calls = db.one("""SELECT COUNT(*) n, SUM(wall_seconds) wall, SUM(input_tokens) inp, SUM(output_tokens) outp,
                      SUM(thinking_tokens) think, MAX(peak_rss_kib) rss FROM model_calls""")
    counts = db.one("""SELECT SUM(production='RUNNING') running, SUM(production='QUEUED') queued,
                       SUM(production IN ('WAITING','INTERRUPTED')) waiting FROM runs""")
    return {
        **_box(),
        "slots": worker_slots(), "drain_seconds": _drain(),
        "counts": {k: counts[k] or 0 for k in ("running", "queued", "waiting")},
        # 专题's reads and answers under way: each holds one of the slots 在印 counts against (topics/run.py dispatch)
        "topics": {"reading": db.one("""SELECT (SELECT COUNT(*) FROM topic_videos WHERE state='RUNNING')
                                         + (SELECT COUNT(*) FROM topics WHERE synth_state='RUNNING') n""")["n"]},
        "runs": runs, "calls": calls,
        "events": db.q("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
        "stage_labels": STAGE_LABELS,
    }


def _family(model: str | None) -> str:
    """Which subscription a model call drew on, by its model id: Gemini (agy, or the 兔子 gateway), or magpie's."""
    m = (model or "").lower()
    if "gemini" in m:
        return "gemini"
    if "gpt" in m or m.startswith("codex"):
        return "chatgpt"
    return next((k for k in ("claude", "grok") if k in m), "other")


@app.get("/api/engine/stats")
def engine_stats():
    """How production went over the last 7 and 30 days, with a bar per day: issues done and failed, hours of video,
    the median time from intake to Notion, what failed most, the median minutes per hour of video of each stage, and
    the model calls by subscription (Gemini, ChatGPT, …) with where each one's tokens went (Gemini's quota is the bottleneck).
    Days are the server's local time (TZ=Asia/Shanghai in the package). Imported and experiment runs are left out; a
    run's stages are counted without TRANSCRIBE (it runs in the background) and CLEANUP."""
    from datetime import datetime, timedelta, timezone
    from statistics import median
    from ..providers import stage_key
    now_l = datetime.now().astimezone()
    since = (now_l - timedelta(days=30)).astimezone(timezone.utc).isoformat(timespec="seconds")
    cuts = {"7d": now_l - timedelta(days=7), "30d": now_l - timedelta(days=30)}
    days = [(now_l - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    bars = {k: {"t": k, "done": 0, "failed": 0, "video_seconds": 0.0} for k in days}

    def local(ts: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(ts).astimezone() if ts else None
        except ValueError:
            return None

    def blank() -> dict:
        return {"done": 0, "partial": 0, "failed": 0, "video_seconds": 0.0, "reasons": {}, "latency": [], "per_hour": {},
                "calls": {"n": 0, "failed": 0, "wall": 0.0, "tokens": 0, "by_family": {}, "by_stage": {}}}
    win = {k: blank() for k in cuts}
    kinds = "r.kind NOT IN ('IMPORT','EXPERIMENT')"

    stages: dict[str, dict[str, float]] = {}
    for st in db.q(f"""SELECT st.run_id, st.name, st.wall_seconds FROM stages st JOIN runs r ON r.id=st.run_id
                       WHERE r.finished_at >= ? AND {kinds} AND r.production IN ('COMPLETE','COMPLETE_PARTIAL')
                       AND st.wall_seconds IS NOT NULL AND st.name NOT IN ('TRANSCRIBE','CLEANUP')""", since):
        stages.setdefault(st["run_id"], {})[st["name"]] = st["wall_seconds"]
    for r in db.q(f"""SELECT r.id, r.production, r.error_class, r.error, r.finished_at, s.duration FROM runs r JOIN sources s ON s.id=r.source_id
                      WHERE r.finished_at >= ? AND {kinds} AND r.production IN ('COMPLETE','COMPLETE_PARTIAL','FAILED')""", since):
        t = local(r["finished_at"])
        if t is None:
            continue
        ok = r["production"] != "FAILED"
        b = bars.get(t.strftime("%Y-%m-%d"))
        if b:
            b["done" if ok else "failed"] += 1
            b["video_seconds"] += (r["duration"] or 0) if ok else 0
        for w, cut in cuts.items():
            if t < cut:
                continue
            v = win[w]
            if not ok:
                v["failed"] += 1
                why = r["error_class"] or "?"
                v["reasons"][why] = v["reasons"].get(why, 0) + 1
                continue
            v["done"] += 1
            v["partial"] += r["production"] == "COMPLETE_PARTIAL"
            v["video_seconds"] += r["duration"] or 0
            hours = (r["duration"] or 0) / 3600
            if hours > 0.05:  # minutes each stage took per hour of this video
                for name, secs in stages.get(r["id"], {}).items():
                    v["per_hour"].setdefault(name, []).append(secs / 60 / hours)
    # intake to Notion: from when the video came in to when its page was written
    for s in db.q("""SELECT created_at, delivered_at FROM sources WHERE delivered_at >= ? AND delivery='DELIVERED'""", since):
        a, d = local(s["created_at"]), local(s["delivered_at"])
        for w, cut in cuts.items():
            if a and d and d >= cut and d > a:
                win[w]["latency"].append((d - a).total_seconds())
    for c in db.q("""SELECT created_at, model, stage, status, wall_seconds, input_tokens, output_tokens, thinking_tokens
                     FROM model_calls WHERE created_at >= ?""", since):
        t = local(c["created_at"])
        if t is None:
            continue
        fam, tok = _family(c["model"]), (c["input_tokens"] or 0) + (c["output_tokens"] or 0) + (c["thinking_tokens"] or 0)
        for w, cut in cuts.items():
            if t < cut:
                continue
            m = win[w]["calls"]
            m["n"] += 1
            m["failed"] += c["status"] not in ("SUCCESS", None)
            m["wall"] += c["wall_seconds"] or 0
            m["tokens"] += tok
            f = m["by_family"].setdefault(fam, {"n": 0, "tokens": 0, "wall": 0.0})
            f["n"] += 1
            f["tokens"] += tok
            f["wall"] += c["wall_seconds"] or 0
            stages_of = m["by_stage"].setdefault(fam, {})
            k = stage_key(c["stage"] or "?")
            stages_of[k] = stages_of.get(k, 0) + tok
    for w, cut in cuts.items():
        win[w]["retries"] = _retries_since(cut.astimezone(timezone.utc).isoformat(timespec="seconds"))
    for v in win.values():
        # all of them: the page puts together the kinds it names alike (输出不合规) before it shows the first three
        v["reasons"] = sorted(({"kind": k, "n": n} for k, n in v["reasons"].items()), key=lambda x: -x["n"])
        v["latency_median"] = round(median(v["latency"])) if v["latency"] else None
        del v["latency"]
        v["per_hour"] = sorted(({"name": n, "label": STAGE_LABELS.get(n, n), "minutes": round(median(x), 1)}
                                for n, x in v["per_hour"].items()), key=lambda x: -x["minutes"])
        for fam, g in v["calls"]["by_stage"].items():
            total = sum(g.values()) or 1
            v["calls"]["by_stage"][fam] = sorted(({"key": k, "tokens": n, "share": n / total} for k, n in g.items()),
                                                 key=lambda x: -x["tokens"])
    return {"now": now_l.isoformat(timespec="seconds"), "windows": win, "days": [bars[k] for k in days],
            "agy_runways": _agy_runways(), "runways": _sub_runways()}


RUNWAY_MIN_DONE = 3  # issues finished over the readings before the quota's use per issue is told


def _runway_of(log: list, week: dict | None) -> dict | None:
    """How many more issues a weekly window holds, roughly: the share of it used over the last week's readings
    (log: [time, left], oldest first), divided by the issues finished over them. A rise of more than 10 points is the
    week's reset; smaller ones are the reading wavering, not quota coming back. Runs still under way, trials and
    compiling (and the subscription used elsewhere) also draw on it, so it is told only once a few issues were finished,
    and reads as an estimate."""
    from datetime import datetime, timedelta, timezone
    cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    log = [x for x in log if x[1] is not None and x[0] >= cut]
    if not week or week["remaining"] is None or len(log) < 2:
        return None
    done = db.one("""SELECT COUNT(*) n FROM runs WHERE finished_at >= ? AND production IN ('COMPLETE','COMPLETE_PARTIAL')
                     AND kind NOT IN ('IMPORT','EXPERIMENT')""", log[0][0])["n"]
    if done < RUNWAY_MIN_DONE:
        return None
    used, top, low = 0.0, log[0][1], log[0][1]
    for _, v in log[1:]:
        if v > low + 0.1:  # the week's reset: what was used before it is counted
            used += top - low
            top = low = v
        else:
            low = min(low, v)
    used += top - low
    if used < 0.02:
        return None
    per = used / done
    return {"per_issue": per, "issues": int(week["remaining"] / per), "remaining": week["remaining"], "reset_at": week["reset_at"]}


def _agy_runways() -> dict:
    """约够印 N 期 for each group of agy's quota a stage draws on, by its name (the Gemini tiles on the 机房 page)."""
    from ..providers.agy import groups_in_use, quota_logs
    logs, out = quota_logs(), {}
    for g in groups_in_use():
        week = next((b for b in g["buckets"] if b["window"] == "weekly" and not b["disabled"]), None)
        if r := _runway_of([[x[0], x[2]] for x in logs.get(g["name"]) or []], week):
            out[g["name"]] = r
    return out


def _runway() -> dict | None:
    """For the 补录 sheet's estimate: the group of agy's quota in use that an issue takes the largest share of a week of."""
    return max(_agy_runways().values(), key=lambda r: r["per_issue"], default=None)


CREDIT_SPAN_GAP = 3600   # seconds without credits going that still count as one stretch of spending them
CREDIT_FINISH = 3600     # seconds after the last credits went within which an issue finishing was printed on them


def _credit_runway(log: list, credits: float | None) -> dict | None:
    """How many more issues an account's credits hold, roughly, once its window is used up: the credits that went over
    the last week's readings (log: [time, credits]; a rise is a top-up, not spending), divided by the issues finished
    while they went. Credits go only while a window is used up, so the issues finished at other times are not counted."""
    from datetime import datetime, timedelta, timezone
    t = datetime.fromisoformat
    cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    log = [x for x in log if x[0] >= cut]
    if not credits or len(log) < 2:
        return None
    used, spans = 0.0, []
    for (t0, a), (t1, b) in zip(log, log[1:]):
        if b >= a:
            continue
        used += a - b
        if spans and (t(t0) - t(spans[-1][1])).total_seconds() <= CREDIT_SPAN_GAP:
            spans[-1][1] = t1
        else:
            spans.append([t0, t1])
    done = sum(db.one("""SELECT COUNT(*) n FROM runs WHERE finished_at >= ? AND finished_at <= ?
                         AND production IN ('COMPLETE','COMPLETE_PARTIAL') AND kind NOT IN ('IMPORT','EXPERIMENT')""",
                      a, min((t(b) + timedelta(seconds=CREDIT_FINISH)).isoformat(timespec="seconds"), nxt))["n"]
               for (a, b), nxt in zip(spans, [s[0] for s in spans[1:]] + ["9999"]))
    if done < RUNWAY_MIN_DONE or used <= 0:
        return None
    per = used / done
    return {"per_issue": per, "issues": int(credits / per), "credits": credits}


def _sub_runways() -> dict:
    """约够印 N 期 for each of magpie's subscriptions, by provider and account, from magpie's own readings of its week;
    a ChatGPT account whose window is used up goes on on its credits, and its runway is theirs."""
    from ..providers.magpie_state import sub_id
    logs, out = db.setting("magpie_credit_logs") or {}, {}
    for s in (db.setting("magpie_state") or {}).get("subs") or []:
        windows = s.get("windows") or []
        if s.get("credits") and any(w["remaining"] == 0 and not w.get("disabled") for w in windows):
            r = _credit_runway(logs.get(sub_id(s)) or [], s["credits"])
        else:
            r = _runway_of(s.get("trend") or [], next((w for w in windows if w["window"] == "weekly"), None))
        if r:
            out[sub_id(s)] = r
    return out


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
    reprint: bool = False  # an issue in Notion already is printed again only when asked for (a06)


def _kept(vid: str | None) -> dict | None:
    """A video in Notion already, with nothing under way for it: its page, not a reprint (a06). REGISTERED is a page
    of the user's own, which a reprint would never replace."""
    s = db.one("SELECT id, delivery, notion_url, current_run_id FROM sources WHERE id=?", vid) if vid else None
    if not s or s["delivery"] not in ("DELIVERED", "REGISTERED") or \
            db.one("SELECT 1 FROM runs WHERE source_id=? AND production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')", vid):
        return None
    return {"source_id": vid, "run_id": s["current_run_id"], "existing": False, "kept": s["delivery"], "notion_url": s["notion_url"]}


@app.post("/api/submit")
def submit(body: Submit):
    """One link — or several pasted at once (one per line, by spaces, or run together by a one-line field): each video
    is queued once, the answer for the first keeps the one-link shape, and `items` has them all."""
    from ..pipeline.runner import submit as do
    from ..youtube import VIDEO_ID, video_id
    text = body.url.strip()
    ids = list(dict.fromkeys(VIDEO_ID.findall(text) + [w for w in text.split() if re.fullmatch(r"[A-Za-z0-9_-]{11}", w)]))
    kept = (lambda v: None) if body.reprint else _kept
    try:
        if len(ids) <= 1:
            return kept(video_id(text)) or do(text, priority=body.priority)
        items = [kept(v) or do(f"https://www.youtube.com/watch?v={v}", priority=body.priority) for v in ids]
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**items[0], "items": items}


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
    if not r:
        raise HTTPException(404)
    if r["production"] in ("QUEUED", "RUNNING"):  # a second click, or retried elsewhere: under way already
        return {"ok": True}
    stopped = r["production"] == "CANCELLED" and r["error_class"] == "STOPPED"  # 清空 stopped it (a10)
    if r["production"] not in ("FAILED", "WAITING", "INTERRUPTED", "COMPLETE_PARTIAL") and not stopped:
        raise HTTPException(409, "这一期已经印完或取消了，不用重试")
    first = db.one("SELECT MIN(ord) o FROM stages WHERE run_id=? AND status IN ('FAILED','WARN','RUNNING')", rid)["o"]
    if first is not None:  # everything downstream of the first problem is rebuilt
        db.exec("UPDATE stages SET status='PENDING' WHERE run_id=? AND ord>=? AND name NOT IN ('TRANSCRIBE')", rid, first)
        db.exec("UPDATE stages SET status='PENDING' WHERE run_id=? AND name='TRANSCRIBE' AND status!='DONE'", rid)
    src = db.one("SELECT media_state, media_path FROM sources WHERE id=?", r["source_id"])
    if src and not (src["media_state"] == "CACHED" and src["media_path"] and Path(src["media_path"]).exists()):
        # the video went with retention: fetch and cut it again; the stages done with it stay done
        db.exec("UPDATE stages SET status='PENDING' WHERE run_id=? AND name IN ('ACQUIRE','CHUNK')", rid)
    db.exec("UPDATE runs SET production='QUEUED', wait_reason=NULL, error=NULL, error_class=NULL, attempts=0, stop_at=NULL, retry=NULL WHERE id=?", rid)
    return {"ok": True}


@app.post("/api/runs/{rid}/ignore")
def ignore(rid: str):
    """A failed issue is put away: one no login opens (removed, private, members-only: nothing to fix, nothing to wait
    for, a15), one the safety filter keeps stopping, or any other the user would rather not 重试 (2026-11-10)."""
    from ..youtube import UNAVAILABLE
    r = db.one("SELECT production, error_class FROM runs WHERE id=?", rid)
    if not r:
        raise HTTPException(404)
    if r["production"] == "CANCELLED":  # a second click
        return {"ok": True}
    if r["production"] != "FAILED":
        raise HTTPException(409, "只有失败的这一期能忽略")
    db.exec("UPDATE runs SET production='CANCELLED' WHERE id=? AND production='FAILED'", rid)
    kind = r["error_class"]
    db.event("run", "被内容安全过滤拦下，已忽略" if kind == "CONTENT_SAFETY" else f"视频不可用（{UNAVAILABLE[kind]}），已忽略"
             if kind in UNAVAILABLE else f"生产失败（{kind or '未分类'}），已忽略", rid)
    return {"ok": True}


class MaybeAnswer(BaseModel):
    write: bool  # 照写: a page of its own all the same; else 算已有: the page found is this issue's


@app.post("/api/sources/{sid}/maybe")
def maybe_have(sid: str, body: MaybeAnswer):
    """库里可能已有 (a05): the answer to a hand-written page this 补录 issue may be."""
    s = db.one("SELECT delivery, delivery_reason FROM sources WHERE id=?", sid)
    if not s:
        raise HTTPException(404)
    page = db.setting(f"maybe_have:{sid}")
    if s["delivery"] != "HELD" or s["delivery_reason"] != "MAYBE_HAVE" or not page:
        raise HTTPException(409, "这一期没有在等你确认")
    if body.write:
        db.set_setting(f"write_anyway:{sid}", True)
        db.exec("""UPDATE sources SET delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END,
                   delivery_reason=NULL, updated_at=? WHERE id=?""", now(), sid)
    else:
        db.exec("UPDATE sources SET delivery='REGISTERED', delivery_reason=?, notion_url=?, updated_at=? WHERE id=?",
                f"库里已有：{page.get('title') or ''}"[:500], page.get("url"), now(), sid)
    db.set_setting(f"maybe_have:{sid}", None)
    db.event("delivery", "照写，另写一页" if body.write else f"算已有：「{page.get('title') or ''}」", None)
    return {"ok": True}


@app.post("/api/playbooks/{pid}/recompile")
def recompile_playbook(pid: int):
    """Compile the learning goal behind a version again into a new draft: an imported playbook gets its
    sentence-by-sentence provenance, an old one its rule short names. The version itself stays as it is."""
    from ..compiler import compile_prompt
    pb = db.one("SELECT id, channel_id, prompt_id, version FROM playbooks WHERE id=?", pid)
    if not pb:
        raise HTTPException(404)
    cid = pb["channel_id"]
    if not cid:
        raise HTTPException(400, "通用规程没有学习目标原文")
    prompt = (db.one("SELECT id FROM channel_prompts WHERE id=?", pb["prompt_id"]) if pb["prompt_id"]
              else db.one("SELECT id FROM channel_prompts WHERE channel_id=? ORDER BY version DESC LIMIT 1", cid))
    if not prompt:  # imported, and nobody has written the goal down here yet
        raise HTTPException(400, "这一版没有学习目标原文，先写学习目标")
    pbs = db.q("SELECT id, prompt_id FROM playbooks WHERE channel_id=?", cid)
    latest = db.one("SELECT id, created_at FROM channel_prompts WHERE channel_id=? ORDER BY version DESC LIMIT 1", cid)
    if (_compile_state(cid, latest, pbs) or {}).get("state") == "running":
        raise HTTPException(409, "这个频道正在编译，等它完成")
    db.set_setting(f"recompile:{cid}", {"playbook_id": pid, "after": max(p["id"] for p in pbs), "since": now()})
    _bg(compile_prompt, cid, prompt["id"], True, label=f"Playbook 编译（{cid}）")
    db.event("compile", f"{cid} 按 v{pb['version']} 的学习目标重新编译")
    return {"prompt_id": prompt["id"], "compiling": True}


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


def _throttled(key: str, interval: float, what: str = "测试") -> JSONResponse | None:
    """A 429 when `key` last ran less than `interval` seconds ago; otherwise None, and this run is recorded."""
    import time
    with _throttle_lock:
        wait = _throttle_at.get(key, float("-inf")) + interval - time.monotonic()
        if wait > 0:
            secs = int(wait) + 1
            return JSONResponse({"detail": f"{what}太频繁：{secs} 秒后再试"}, status_code=429, headers={"Retry-After": str(secs)})
        _throttle_at[key] = time.monotonic()
    return None


@app.post("/api/cookiecloud/test")
def cookiecloud_test():
    """One CookieCloud read for the 设置 page's button; at most one every COOKIE_TEST_INTERVAL seconds (429 otherwise)."""
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
    """The 设置 page's video test: does YouTube accept the CookieCloud login for this video (ideally a members-only one)?
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
                "UPDATE runs SET production='QUEUED' WHERE production='WAITING' AND wait_reason IN ('YOUTUBE_AUTH','YOUTUBE_REFUSED')").rowcount
        db.set_setting("youtube_video_test", out)
        title = (out["video"] or {}).get("title") or out["url"]
        db.event("config", (f"视频测试通过：{title}" + (f"；{out['woken']} 个等待登录的视频已重新排队" if out.get("woken") else ""))
                 if out["ok"] else f"视频测试未通过（{out['verdict']}）：{title}", level="info" if out["ok"] else "warn")
        return out
    finally:
        _video_test_lock.release()


class Ingest(BaseModel):
    enabled: bool
    min_minutes: object = None  # checked by _minutes(): a refusal is said in a sentence, not pydantic's list


MIN_MINUTES = (0, 240)  # the shortest video intake takes, in minutes (0: any length, Shorts too)


def _minutes(v: object) -> int | None:
    """The intake minimum from a request: a whole number of minutes within MIN_MINUTES, or None (unchanged)."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not MIN_MINUTES[0] <= v <= MIN_MINUTES[1]:
        raise HTTPException(400, f"最短时长要是 {MIN_MINUTES[0]}–{MIN_MINUTES[1]} 之间的整数分钟")
    return v


@app.post("/api/channels/{cid}/ingest")
def channel_ingest(cid: str, body: Ingest):
    from ..ingest import set_enabled
    if not db.one("SELECT id FROM channels WHERE id=? AND mode='TRACKED'", cid):
        raise HTTPException(404)
    return set_enabled(cid, body.enabled, _minutes(body.min_minutes))


# ------------------------------------------------------------------ 频道看板: stats, scans, 补录
WEEKS = 20  # weekly bars on the 看板 (the 频道 overview shows the last 12 of them)


def _channel_stats(cid: str) -> dict:
    """What a channel has produced: issues (made with any version), this and last month, units, clips, video time,
    校注 per issue, where delivery stands, the model tokens it took, and issues per week (by the day each was ready).
    Only what was produced counts: a video intake found already in the library is 已登记 without a run, not an issue
    (`registered` is an issue whose page turned out to be there already)."""
    from datetime import datetime, timedelta
    rows = db.q(f"""SELECT s.id, s.delivery, s.duration, r.finished_at, r.summary FROM sources s JOIN runs r ON r.id=s.current_run_id
                    LEFT JOIN channels c ON c.id=s.channel_id WHERE s.channel_id=? AND {NOT_SAMPLE}""", cid)
    notes = db.one(f"""SELECT COUNT(*) n FROM flags f JOIN sources s ON s.id=f.source_id AND f.run_id=s.current_run_id
                       LEFT JOIN channels c ON c.id=s.channel_id WHERE s.channel_id=? AND f.severity!='INFO' AND {NOT_SAMPLE}""", cid)["n"]
    tok = db.one("""SELECT SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+COALESCE(thinking_tokens,0)) n FROM model_calls m
                    JOIN runs r ON r.id=m.run_id JOIN sources s ON s.id=r.source_id WHERE s.channel_id=?""", cid)["n"] or 0
    active = db.one("""SELECT SUM(r.production='RUNNING') running, SUM(r.production IN ('QUEUED','WAITING','INTERRUPTED')) queued
                       FROM runs r JOIN sources s ON s.id=r.source_id WHERE s.channel_id=?""", cid)
    today = datetime.now().astimezone()
    month = today.strftime("%Y-%m")
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    monday = (today - timedelta(days=today.weekday())).date()
    weeks = [0] * WEEKS
    out = {"issues": len(rows), "month": 0, "last_month": 0, "units": 0, "clips": 0, "seconds": 0.0, "notes": notes, "tokens": tok,
           "delivered": 0, "registered": 0, "failed": 0, "waiting": 0,
           "running": active["running"] or 0, "queued": active["queued"] or 0}
    for r in rows:
        summ = db.unj(r["summary"], {}) or {}
        out["units"] += summ.get("units") or 0
        out["clips"] += (summ.get("assets") or {}).get("clip:GENERATED", 0)
        out["seconds"] += r["duration"] or 0
        d = r["delivery"]
        out["delivered" if d == "DELIVERED" else "registered" if d == "REGISTERED" else "failed" if d in ("FAILED", "BLOCKED")
            else "waiting"] += 1
        try:
            t = datetime.fromisoformat(r["finished_at"]).astimezone() if r["finished_at"] else None
        except ValueError:
            t = None
        if t:
            out["month"] += t.strftime("%Y-%m") == month
            out["last_month"] += t.strftime("%Y-%m") == last_month
            ago = (monday - (t.date() - timedelta(days=t.weekday()))).days // 7
            if 0 <= ago < WEEKS:
                weeks[WEEKS - 1 - ago] += 1
    out["weeks"] = weeks
    out["week_start"] = (monday - timedelta(weeks=WEEKS - 1)).isoformat()
    last = db.one(f"""SELECT s.id, s.title, s.headline, s.published_at, s.delivery, r.finished_at FROM sources s
                      JOIN runs r ON r.id=s.current_run_id LEFT JOIN channels c ON c.id=s.channel_id
                      WHERE s.channel_id=? AND {NOT_SAMPLE} ORDER BY r.finished_at DESC LIMIT 1""", cid)
    out["last"] = last
    return out


_scanning: set[str] = set()
_board_lock = threading.Lock()


def _tracked(cid: str, locked: bool = False) -> dict:
    c = db.one("SELECT * FROM channels WHERE id=? AND mode='TRACKED' AND youtube_channel_id IS NOT NULL", cid)
    if not c:
        raise HTTPException(404, "不是在跟踪的频道")
    if locked and c["stage"] != "LOCKED":
        raise HTTPException(409, "定稿后才能补录或扫描")
    return c


@app.get("/api/channels/{cid}/board")
def channel_board(cid: str):
    """The 看板 of one channel: its stats, intake settings with the last scans, and every issue it has."""
    from ..ingest import EVERY, backfill_view, next_round
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    issues = _issues("s.channel_id=?", cid)
    for i, it in enumerate(issues):
        it["no"] = i + 1
    under_way = db.q("""SELECT r.id, r.production, r.wait_reason, s.title FROM runs r JOIN sources s ON s.id=r.source_id
                        WHERE s.channel_id=? AND r.origin='backfill' AND r.production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')
                        ORDER BY r.started_at IS NULL, r.started_at, r.created_at""", cid)
    from ..tuning import minutes_per_video_minute
    return JSONResponse({"stats": _channel_stats(cid), "issues": list(reversed(issues)),
            "ingest": {**(db.setting(f"ingest:{cid}") or {"enabled": False}), "every_minutes": EVERY // 60, "next_at": next_round(),
                       "scanning": cid in _scanning},
            "backfill": {**backfill_view(cid), "running": under_way},
            # for the 补录 sheet's estimate: minutes of production per minute of video here, and the weekly quota an issue takes
            "pace": minutes_per_video_minute(cid), "runway": _runway()})  # plain JSON: as it is (a19)


@app.get("/api/channels/{cid}/avatar")
def channel_avatar(cid: str):
    """The channel's avatar and how far its pages are, without asking Notion (the settings page polls it)."""
    from .. import channel_icons
    from ..notion import mapping as M
    return channel_icons.state(cid, M.get(cid))


@app.post("/api/channels/{cid}/scan")
def channel_scan(cid: str):
    """Look at the channel's feed now instead of at the next half-hour round (intake must be on)."""
    from .. import ingest
    c = _tracked(cid, locked=True)
    cfg = db.setting(f"ingest:{cid}") or {}
    if not cfg.get("enabled"):
        raise HTTPException(409, "先打开自动收录")
    with _board_lock:
        if cid in _scanning:
            return {"scanning": True}
        _scanning.add(cid)

    def run() -> None:
        try:
            ingest.scan(c, cfg)
        except Exception as e:  # noqa: BLE001
            db.event("ingest", f"{c['name']} 扫描出错：{e}", level="warn")
        finally:
            _scanning.discard(cid)
    threading.Thread(target=run, name=f"scan-{cid}", daemon=True).start()
    return {"scanning": True}


class BackfillItem(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    title: str = Field(default="", max_length=500)
    duration: float | None = Field(default=None, ge=0, le=86400)


class Backfill(BaseModel):
    videos: list[BackfillItem] = Field(min_length=1, max_length=1000)  # a whole listing (LISTING_MAX), picked by date
    now: bool = False  # 试这一期: produce right away with normal priority, not through the 补录 line


@app.post("/api/channels/{cid}/backfill")
def channel_backfill(cid: str, body: Backfill):
    """补录 for a locked channel: line the chosen videos up (produced one at a time while idle and the quota has room),
    or with `now` take the one video right away."""
    from .. import ingest
    c = _tracked(cid, locked=True)
    if body.now:
        v = body.videos[0]
        if db.one("SELECT 1 FROM sources WHERE id=?", v.id):
            raise HTTPException(409, "这一期已经在了")
        state, rid = ingest.take(c, {"id": v.id, "title": v.title.strip()}, "试收", origin="manual", register=False)
        return {"queued": int(bool(rid)), "registered": int(not rid), "have": 0, "run_id": rid}
    out = ingest.enqueue_backfill(cid, [v.model_dump() for v in body.videos])
    if out["queued"]:
        db.event("ingest", f"补录排队：{c['name']} · {out['queued']} 期")
    return out


class Link(BaseModel):
    url: str = Field(min_length=1, max_length=500)


@app.post("/api/channels/{cid}/backfill/link")
def channel_backfill_link(cid: str, body: Link):
    """补录 one video by its link: one the channel's listing does not show (unlisted). It must be this channel's."""
    from .. import ingest
    from ..youtube import metadata, video_id
    c = _tracked(cid, locked=True)
    vid = video_id(body.url.strip())
    if not vid:
        raise HTTPException(422, "不是 YouTube 视频链接")
    try:
        info = metadata(f"https://www.youtube.com/watch?v={vid}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"读不到这个视频：{e}"[:300])
    if info.get("channel_id") != c["youtube_channel_id"]:
        raise HTTPException(409, f"这是「{info.get('channel') or '别的频道'}」的视频")
    out = ingest.enqueue_backfill(cid, [{"id": vid, "title": info.get("title") or "", "duration": info.get("duration")}])
    if out["queued"]:
        db.event("ingest", f"补录排队：{c['name']} · {info.get('title') or vid}")
    return out


@app.post("/api/channels/{cid}/backfill/cancel")
def channel_backfill_cancel(cid: str):
    from .. import ingest
    _tracked(cid)
    n = ingest.cancel_backfill(cid)
    if n:
        db.event("ingest", f"取消补录：{n} 期")
    return {"cancelled": n}


def _bg(fn, *args, label: str = "") -> None:
    def run():
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            db.event("compile", f"{label}失败：{e}", level="error")
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ 频道调试台
def _tuning_error(e: ValueError):
    from ..tuning import StageMoved
    raise HTTPException(409 if isinstance(e, StageMoved) else 400, str(e))


@app.get("/api/channels/{cid}/workbench")
def channel_workbench(cid: str):
    from .. import tuning
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    return tuning.workbench(cid)


FEED_TTL = 300        # seconds the channel's latest videos are kept (the 看板's 试一期, the sample picker)
LISTING_TTL = 600     # and a 补录 listing (one sitting of picking)
LISTING_MAX = 1000    # the deepest listing: 补录 reaches this many of the channel's latest videos
_feeds: dict[str, tuple[float, list]] = {}
_listings: dict[tuple[str, int], dict] = {}


def _marked(cid: str, entries: list[dict], lib: dict, maybe: dict | None = None) -> list[dict]:
    """Each video marked 已是样片 / 已在本刊 / 库里已有 / 可能已有 / 补录排队中 / 太短."""
    samples = {r["source_id"] for r in db.q("SELECT source_id FROM samples WHERE channel_id=?", cid)}
    lined = {r["source_id"] for r in db.q("SELECT source_id FROM backfill_queue WHERE channel_id=? AND status='PENDING'", cid)}
    least = 60 * int((db.setting(f"ingest:{cid}") or {}).get("min_minutes") or 0)
    out = []
    for e in entries:
        s = db.one("SELECT current_run_id FROM sources WHERE id=?", e["id"])
        out.append({**e, "sample": e["id"] in samples, "produced": bool(s and s["current_run_id"]), "here": bool(s),
                    "library": lib.get(e["id"]), "maybe": (maybe or {}).get(e["id"]), "queued": e["id"] in lined,
                    "short": bool(least and e.get("duration") and e["duration"] < least)})
    return out


def _maybe(cid: str, entries: list[dict]) -> dict | None:
    """video id → the hand-written page (no link) it may be (a05); None when the database could not be asked."""
    from ..delivery import maybe_many
    try:
        return maybe_many({"channel_id": cid}, entries)
    except Exception:  # noqa: BLE001
        return None


def _library(cid: str, entries: list[dict]) -> dict | None:
    """video id → its page in the channel's Notion database; None when the database could not be asked."""
    from ..delivery import registered_many
    try:
        return registered_many({"channel_id": cid}, [e["id"] for e in entries])
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/channels/{cid}/candidates")
def channel_candidates(cid: str, limit: int = 15):
    """The channel's latest videos (up to 15, from the feed; kept FEED_TTL), marked: the 调试台's sample picker and the
    看板's 试一期."""
    import time
    from ..ingest import feed
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not ch or not ch.get("youtube_channel_id"):
        raise HTTPException(404)
    hit = _feeds.get(cid)
    if hit and time.monotonic() - hit[0] < FEED_TTL:
        entries = hit[1]
    else:
        try:
            entries = feed(ch["youtube_channel_id"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"读不到频道最近的视频：{e}")
        _feeds[cid] = (time.monotonic(), entries)
    entries = entries[:max(1, min(limit, 15))]
    least = int((db.setting(f"ingest:{cid}") or {}).get("min_minutes") or 0)
    return {"candidates": _marked(cid, entries, _library(cid, entries) or {}), "min_minutes": least}


def _listing(ch: dict, depth: int) -> dict:
    """The channel's latest `depth` videos with what its Notion database already has, read in the background (yt-dlp
    takes a minute for the deepest) and kept LISTING_TTL: {"entries", "library"} once read, {"busy": True} until then,
    {"error"} once when it failed (asking again starts over). A fresh deeper listing serves a shallower one: its first
    videos are the same."""
    import time
    from ..ingest import listing
    cid = ch["id"]
    with _board_lock:
        for k in [k for k, v in _listings.items() if "at" in v and time.monotonic() - v["at"] >= LISTING_TTL]:
            del _listings[k]  # past their time: not kept around
        fresh = sorted((d, v) for (c, d), v in _listings.items()
                       if c == cid and d >= depth and "entries" in v and time.monotonic() - v["at"] < LISTING_TTL)
        if fresh:
            return {**fresh[0][1], "entries": fresh[0][1]["entries"][:depth]}
        hit = _listings.get((cid, depth))
        if hit and hit.get("error"):
            del _listings[(cid, depth)]
        if hit and ("busy" in hit or "error" in hit):
            return hit
        _listings[(cid, depth)] = {"busy": True}

    def run() -> None:
        try:
            entries = listing(ch["youtube_channel_id"], depth)
            if not entries:  # the tabs were read but listed nothing
                raise RuntimeError("yt-dlp 没有列出任何视频")
            lib = _library(cid, entries)
            v = {"at": time.monotonic(), "entries": entries, "library": lib, "maybe": _maybe(cid, entries) if lib is not None else None}
        except Exception as e:  # noqa: BLE001
            v = {"error": f"读不到频道的视频：{e}"[:300]}
        with _board_lock:
            _listings[(cid, depth)] = v
    threading.Thread(target=run, name=f"listing-{cid}", daemon=True).start()
    return {"busy": True}


@app.get("/api/channels/{cid}/backfill/candidates")
def backfill_candidates(cid: str, since: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                        until: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """补录's choice: the channel's videos published from `since` to `until` (days, both included; no `since`: all of
    them, up to LISTING_MAX), marked. yt-dlp's listing is read just deep enough to reach `since` — guessed from how often
    the channel posts — and dates its videos roughly ("3 weeks ago"), so the range is approximate at its ends.
    {"listing": true} while it is read."""
    from datetime import date, datetime, timezone
    try:  # the pattern lets 2026-02-30 through
        for d in (since, until):
            if d:
                date.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, "没有这一天")
    ch = _tracked(cid)
    today = datetime.now(timezone.utc).date()
    depth = 60 if since else LISTING_MAX
    while True:
        r = _listing(ch, depth)
        if "entries" not in r:
            break
        dated = [e for e in r["entries"] if e.get("published")]
        oldest = dated[-1]["published"][:10] if dated else None
        if not since or len(r["entries"]) < depth or depth >= LISTING_MAX or not oldest or oldest < since:
            break
        per_day = len(dated) / max(1, (today - date.fromisoformat(oldest)).days)
        depth = min(LISTING_MAX, max(depth * 2, int(per_day * (today - date.fromisoformat(since)).days * 1.2) + 30))
    if r.get("busy"):
        return {"listing": True}
    if r.get("error"):
        raise HTTPException(502, r["error"])
    entries = r["entries"]
    lib, maybe = r["library"], r.get("maybe")
    if lib is None or maybe is None:  # Notion could not be asked when the listing was read: ask again, and keep the answer
        lib, maybe = lib if lib is not None else _library(cid, entries), maybe if maybe is not None else _maybe(cid, entries)
        if lib is not None:
            with _board_lock:
                for (c, _), v in _listings.items():
                    if c == cid and "entries" in v and (v.get("library") is None or v.get("maybe") is None):
                        v.update(library=lib, maybe=maybe)
    inside = [e for e in entries if (not since or (e.get("published") or "")[:10] >= since)
              and (not until or (e.get("published") or "")[:10] <= until)]
    reached = len(entries) < depth or bool(since and oldest and oldest < since)
    least = int((db.setting(f"ingest:{cid}") or {}).get("min_minutes") or 0)
    return {"candidates": _marked(cid, inside, lib or {}, maybe), "min_minutes": least, "listed": len(entries), "complete": reached,
            "library_checked": lib is not None}


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
    return {"cancelled": n, "stopped": tuning.stop_trials(cid), **tuning.workbench(cid)}


@app.get("/api/compare")
def compare_runs(a: str, b: str):
    from .. import tuning
    ra, rb = (db.one("SELECT source_id FROM runs WHERE id=?", x) for x in (a, b))
    if not ra or not rb or ra["source_id"] != rb["source_id"]:
        raise HTTPException(400, "只能对比同一期视频的两个版本")
    return tuning.compare(a, b)


class AdoptBody(BaseModel):
    run_id: str | None = None
    pinned: bool = True


@app.post("/api/sources/{sid}/adopt")
def adopt_run(sid: str, body: AdoptBody):
    from .. import tuning
    try:
        return {"ok": True, **tuning.adopt(sid, body.run_id, body.pinned)}
    except ValueError as e:
        _tuning_error(e)


@app.get("/api/channels/{cid}/lock-preview")
def channel_lock_preview(cid: str, playbook_id: int):
    """定稿单: per sample what would be written, whether the library already has it (checked live) and whether
    that page may be replaced (only pages Assetizer wrote itself)."""
    from .. import tuning
    from ..delivery import registered_many
    try:
        out = tuning.lock_preview(cid, playbook_id)
    except ValueError as e:
        _tuning_error(e)
    try:
        lib = registered_many({"channel_id": cid}, [s["source_id"] for s in out["samples"]])
        out["library_checked"] = True
    except Exception as e:  # noqa: BLE001
        lib, out["library_checked"], out["library_error"] = {}, False, str(e)[:200]
    norm = lambda x: (x or "").replace("-", "")  # noqa: E731
    for s in out["samples"]:
        pg = lib.get(s["source_id"])
        own = s.pop("notion_page_id")
        if not pg and own and not out["library_checked"]:  # Notion not answering: the page Assetizer wrote is still there
            pg = {"id": own, "url": (db.one("SELECT notion_url FROM sources WHERE id=?", s["source_id"]) or {}).get("notion_url")}
        s["library"] = pg and {"url": pg.get("url"), "title": pg.get("title"), "owned": bool(own) and norm(own) == norm(pg.get("id"))}
        s["can_replace"] = bool(own) and (not out["library_checked"] or bool(s["library"] and s["library"]["owned"]))
    out["missed"], out["missed_error"] = _missed(cid)
    return out


def _missed(cid: str) -> tuple[list[dict], str | None]:
    """What the channel posted while its intake stood still for tuning, for the 定稿单 to offer to 补录 (a11): not here,
    not a sample, not lined up, not in the library, not too short. From the channel's listing, whose dates are rough —
    so are the ends of the window."""
    from .. import ingest
    from ..delivery import registered_many
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    cfg = db.setting(f"ingest:{cid}") or {}
    since = cfg.get("paused_at")
    if not since or ch["mode"] != "TRACKED" or not ch["youtube_channel_id"]:
        return [], None
    try:
        entries = ingest.look(ch["youtube_channel_id"], 30)[0]
    except Exception as e:  # noqa: BLE001
        return [], f"读不到调试期间发的视频：{e}"[:200]
    least = 60 * int(cfg.get("min_minutes") or 0)
    samples = {r["source_id"] for r in db.q("SELECT source_id FROM samples WHERE channel_id=?", cid)}
    new = [e for e in entries if (e.get("published") or "")[:10] >= since[:10] and e["id"] not in samples
           and not db.one("SELECT 1 FROM sources WHERE id=?", e["id"])
           and not db.one("SELECT 1 FROM backfill_queue WHERE source_id=? AND status='PENDING'", e["id"])
           and not (least and e.get("duration") and e["duration"] < least)]
    try:
        lib = registered_many({"channel_id": cid}, [e["id"] for e in new]) if new else {}
    except Exception:  # noqa: BLE001
        lib = {}
    return [{k: e.get(k) for k in ("id", "title", "published", "duration")} for e in new if e["id"] not in lib], None


class LockBody(BaseModel):
    backup: dict | None = None
    playbook_id: int
    samples: list[dict] = []
    ingest: dict | None = None
    backfill: list[BackfillItem] = []  # posted while tuned: lined up for 补录 (a11)


@app.post("/api/channels/{cid}/lock")
def channel_lock(cid: str, body: LockBody):
    from .. import tuning
    ing = body.ingest and {**body.ingest, "enabled": bool(body.ingest.get("enabled", True)),
                           "min_minutes": _minutes(body.ingest.get("min_minutes"))}
    try:
        return tuning.lock(cid, body.playbook_id, body.samples, ing, [v.model_dump() for v in body.backfill], backup_rule=body.backup)
    except (tuning.LockRefused, FileExistsError) as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        _tuning_error(e)


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


def _database(n, database_id: str, cached: bool = False) -> dict:
    from .. import preview as PV
    from ..notion.client import NotionError
    try:
        return PV.database(n, database_id, fresh=not cached)
    except NotionError as e:
        from ..delivery import TOKEN_FIX, token_invalid
        if token_invalid(e):
            raise HTTPException(502, f"Notion token 已失效（401），读不到数据库。{TOKEN_FIX}")
        if e.status in (403, 404):
            raise HTTPException(502, f"Integration 没有这个数据库的权限（{e.status}）：在数据库右上角 ··· → Connections 里添加它。")
        raise HTTPException(502, f"读不到目标数据库：{e}")


def _notion_view(cid: str, cfg: dict) -> dict:
    from .. import channel_icons
    from ..notion import mapping as M
    from ..notion.client import NotionError
    n = _notion()
    dbo = _database(n, cfg["database_id"])
    schema = M.schema_of(dbo)
    title = "".join(t.get("plain_text", "") for t in dbo.get("title", []))
    cfg = M.sync(cfg, schema)
    M.refresh(cid, cfg["database_id"], schema, **({"database_title": title} if title else {}))
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
            "rev": (M.get(cid) or {}).get("rev") or 0,  # what a save is checked against (a12)
            "avatar": channel_icons.state(cid, cfg),
            "test_copy": M.test_copy(cfg, title),
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
    icon: str = "channel"
    rev: int | None = None  # the revision the page read: saved since by another device → 412, unless `force` (a12)
    force: bool = False


@app.post("/api/channels/{cid}/notion")
def channel_notion_save(cid: str, body: NotionMapping):
    from ..notion import mapping as M
    from .. import preview as PV
    cfg = M.get(cid)
    if not cfg or not cfg.get("database_id"):
        raise HTTPException(409, "这个频道还没有 Notion 目标")
    rev = cfg.get("rev") or 0
    if body.rev is not None and body.rev != rev and not body.force:
        raise HTTPException(412, "这个频道的 Notion 设置刚在别处改过")
    try:
        cfg = PV.draft_config(cfg, body.model_dump(exclude={"rev", "force"}))
    except ValueError as e:
        raise HTTPException(400, str(e))
    M.save(cid, {**cfg, "rev": rev + 1})
    # sources paused on the old mapping try again now
    db.exec("""UPDATE sources SET delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END, delivery_reason=NULL
               WHERE channel_id=? AND delivery='BLOCKED' AND delivery_reason LIKE 'NOTION_MAPPING%'""", cid)
    db.event("notion", f"已更新「{cid}」的 Notion 属性映射")
    return {"configured": True, **_notion_view(cid, cfg)}


class PreviewBody(BaseModel):
    run_id: str | None = None
    config: dict | None = None


@app.post("/api/channels/{cid}/notion/preview")
def channel_notion_preview(cid: str, body: PreviewBody):
    """The Notion page one run would become under the channel's config plus unsaved card edits. Reads Notion, never writes."""
    from .. import preview as PV
    from ..notion import mapping as M
    ch = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not ch:
        raise HTTPException(404)
    saved = M.get(cid)
    if not saved or not saved.get("database_id"):
        return {"configured": False}
    try:
        cfg = PV.draft_config(saved, body.config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.run_id:
        r = db.one("SELECT r.id, r.source_id FROM runs r JOIN sources s ON s.id=r.source_id WHERE r.id=? AND s.channel_id=?",
                   body.run_id, cid)
        if not r:
            raise HTTPException(404, "这一版不属于这个频道")
        source, run_id = db.one("SELECT * FROM sources WHERE id=?", r["source_id"]), r["id"]
    else:
        source = db.one("""SELECT * FROM sources WHERE channel_id=? AND current_run_id IS NOT NULL
                           ORDER BY published_at DESC, updated_at DESC LIMIT 1""", cid)
        if not source:
            return {"configured": True, "empty": True}
        run_id = source["current_run_id"]
    n = _notion()
    # without card edits (opening the page, after saving) read Notion now; edits are previewed against that copy
    dbo = _database(n, cfg["database_id"], cached=body.config is not None)
    return {"configured": True, **PV.build(n, dbo, cfg, ch, source, run_id)}


@app.get("/api/playbooks/{pid}/annotations")
def playbook_annotations(pid: int):
    """稿纸: the prompt a version came from, each clause's range and fate, rules with short names, what changed."""
    from ..compiler import annotations
    try:
        return annotations(pid)
    except LookupError:
        raise HTTPException(404)


@app.get("/api/channels/{cid}/recent")
def channel_recent(cid: str, limit: int = 4):
    """The latest issues of a locked channel, with their 校注 counts (the 定稿后 strip)."""
    out = []
    for s in db.q("""SELECT id, title, published_at, duration, delivery, delivery_reason, notion_url, current_run_id FROM sources
                     WHERE channel_id=? AND current_run_id IS NOT NULL ORDER BY published_at DESC, created_at DESC LIMIT ?""",
                  cid, max(1, min(limit, 20))):
        run = db.one("SELECT summary FROM runs WHERE id=?", s["current_run_id"])
        out.append({**s, "units": (db.unj(run["summary"], {}) or {}).get("units") if run else None,
                    "notes": db.one("SELECT COUNT(*) n FROM flags WHERE run_id=? AND severity!='INFO'", s["current_run_id"])["n"]})
    return {"issues": out}


@app.get("/api/notion/databases")
def notion_databases(q: str = ""):
    """Databases the integration can see, for picking a channel's target in the app."""
    from ..notion.client import NotionError
    try:
        res = _notion().search(q.strip())
    except NotionError as e:
        raise HTTPException(502, f"Notion 搜索失败：{e}")
    return {"databases": [{"id": d["id"], "url": d.get("url"),
                           "title": "".join(t.get("plain_text", "") for t in d.get("title", []))} for d in res]}


class TargetBody(BaseModel):
    database: str
    parent_page_id: str | None = None
    is_test_copy: bool = False


@app.post("/api/channels/{cid}/notion/target")
def channel_notion_target(cid: str, body: TargetBody):
    """Point a channel at another database (what `assetizer notion-target` does): mappings follow by property name
    (as the old database names them now), a fixed relation is left for the user to pick again, and mappings the new
    database has no property for are named."""
    from .. import preview as PV
    from ..notion import mapping as M
    if not db.one("SELECT id FROM channels WHERE id=?", cid):
        raise HTTPException(404)
    try:
        dbid = M.norm_id(body.database)
    except ValueError as e:
        raise HTTPException(400, str(e))
    n = _notion()
    dbo = _database(n, dbid)
    schema = M.schema_of(dbo)
    old = M.get(cid) or {}
    if old.get("properties"):
        if old.get("database_id"):
            try:
                old = M.sync(old, M.schema_of(_database(n, old["database_id"])))
            except HTTPException:
                pass  # the old database is gone or shut out: carry over by the names last seen
        cfg, repick, dropped = M.remap(old, schema)
    else:
        cfg, repick, dropped = M.preset(cid, dbid, schema, body.parent_page_id), [], []
    title = "".join(t.get("plain_text", "") for t in dbo.get("title", []))
    cfg.update(database_id=dbid, database_title=title, is_test_copy=body.is_test_copy, rev=((M.get(cid) or {}).get("rev") or 0) + 1)
    M.save(cid, cfg)
    PV.forget(f"db:{dbid}")
    db.exec("""UPDATE sources SET delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END, delivery_reason=NULL
               WHERE channel_id=? AND delivery='BLOCKED' AND delivery_reason LIKE 'NOTION_MAPPING%'""", cid)
    db.event("notion", f"「{cid}」的 Notion 目标换成「{title}」")
    return {"configured": True, "repick": repick, "dropped": dropped, **_notion_view(cid, cfg)}


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
    from datetime import datetime, timedelta, timezone
    from ..util import sha256_text
    was = db.one("SELECT id, created_at FROM channel_prompts WHERE channel_id=? AND sha256=?", cid, sha256_text(body.text.strip()))
    pid = add_prompt_version(cid, body.text.strip(), "manual", body.note)
    if was and datetime.fromisoformat(was["created_at"]) > datetime.now(timezone.utc) - timedelta(minutes=15) and \
            (_compile_state(cid, was, db.q("SELECT id, prompt_id FROM playbooks WHERE channel_id=?", cid)) or {}).get("state") == "running":
        return {"prompt_id": pid, "compiling": True}  # this very text is being compiled (a double click, another tab)
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


# ------------------------------------------------------------------ agy: sign-in and quota (机房)
class LoginCode(BaseModel):
    code: str = Field(max_length=4096)


@app.get("/api/agy/login")
def agy_login_state():
    from ..providers import agy_login
    return agy_login.current()


@app.post("/api/agy/login")
def agy_login_start():
    """Start (or rejoin) the one sign-in session; the page polls GET for the link, then posts the code."""
    from ..providers import agy_login
    return agy_login.start()


@app.post("/api/agy/login/code")
def agy_login_code(body: LoginCode):
    from ..providers import agy_login
    try:
        return agy_login.submit(body.code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/agy/login/cancel")
def agy_login_cancel():
    from ..providers import agy_login
    return agy_login.cancel()


def _read_agy() -> dict:
    """Read agy's quota (`agy -p /usage`: no turn, no quota spent). agy answering is what clears its sign-in alert —
    after a sign-in in the terminal, say."""
    from ..pipeline.runner import agy_answers
    from ..providers.agy import AgyProvider, record_usage
    q = record_usage(AgyProvider().usage())
    if q.get("ok"):
        agy_answers()
    return q


@app.post("/api/agy/quota")
def agy_quota_refresh():
    """Read the quota now; at most once every 20 seconds."""
    if busy := _throttled("agy-quota", 20.0, "刷新"):
        return busy
    return _read_agy()


@app.post("/api/magpie/refresh")
def magpie_refresh():
    """Have magpie ask its vendors again and read the result; at most once every 20 seconds (magpie itself reads a card
    at most once in 30). No turn, no quota spent."""
    from ..providers.magpie_state import read
    if busy := _throttled("magpie-refresh", 20.0, "刷新"):
        return busy
    return read(vendors=True)


@app.post("/api/queue/resume")
def resume():
    """手动恢复: the queue goes on and what waited for the model is queued again. agy's sign-in alert is not the click's
    to clear: agy is asked in the background, and the alert goes once it answers (a run that fails on it raises it again)."""
    from ..pipeline.runner import auth_alert_provider
    paused = db.setting("queue_paused")
    db.set_setting("queue_paused", None)
    reasons = ("AGY_AUTH_REQUIRED", "PROVIDER_QUOTA", "PROVIDER_MODEL")
    db.exec(f"UPDATE runs SET production='QUEUED', wait_reason=NULL WHERE production='WAITING' AND wait_reason IN {reasons}")
    db.resolve_alert("PROVIDER_QUOTA")
    db.resolve_alert("PROVIDER_MODEL")
    if auth_alert_provider() == "agy":
        threading.Thread(target=_read_agy, name="agy-usage", daemon=True).start()
    else:
        db.resolve_alert("AGY_AUTH_REQUIRED")
    return {"ok": True, "was": paused}


# ------------------------------------------------------------------ 设置页的盒子设置 (knobs.py, design §R6-7)
@app.post("/api/knobs")
async def knobs_save(request: Request):
    """Partial update {key: value | null (default)}; the answer is every setting in effect."""
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse({"detail": "请求体必须是 JSON 对象"}, status_code=400)
    try:
        return knobs.save(body)
    except knobs.KnobError as e:
        return JSONResponse({"detail": e.message, "field": e.key}, status_code=400)


async def _secret_body(request: Request, fields: set[str]) -> dict | JSONResponse:
    """A small JSON object of these fields plus `clear`; parsed by hand so a rejected value is never echoed."""
    raw = await request.body()
    if len(raw) > 4096:
        return JSONResponse({"detail": "请求体太大"}, status_code=413)
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) - fields - {"clear"} or not all(
            isinstance(body.get(f), (str, type(None))) for f in fields) or not isinstance(body.get("clear", []), list):
        return JSONResponse({"detail": f"可用的字段是 {'、'.join(sorted(fields))}、clear"}, status_code=400)
    return body


def _put_secret(name: str, value: str | None) -> JSONResponse | None:
    """Write (or with None remove) secrets/<name>; a 409 when an environment variable provides it instead."""
    from ..cookies import _remove_secret, _write_secret
    if os.environ.get(env := config.secret_env(name)):
        return JSONResponse({"detail": f"由服务器环境变量 {env} 提供，这里改不了"}, status_code=409)
    if value:
        _write_secret(name, value)
    else:
        _remove_secret(name)
    return None


# ------------------------------------------------------------------ 专题网盘：客户端与 token 只写不读
async def _backup_json(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(400, "请求格式不正确") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "请求格式不正确")
    return body


@app.get("/api/gdrive/ready")
def gdrive_ready():
    from ..topics import backup
    return {"ready": backup.available()}


@app.get("/api/gdrive/rules")
def gdrive_rules():
    from ..topics import backup_rules
    return backup_rules.view()


@app.put("/api/gdrive/rules")
async def gdrive_rules_put(request: Request):
    from ..topics import backup_rules
    try:
        return await run_in_threadpool(backup_rules.update, await _backup_json(request))
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/channels/{cid}/backup")
def channel_backup(cid: str):
    from ..topics import backup_rules
    try:
        return backup_rules.channel(cid)
    except KeyError:
        raise HTTPException(404, "频道不存在") from None


@app.put("/api/channels/{cid}/backup")
async def channel_backup_put(cid: str, request: Request):
    from ..topics import backup, backup_rules
    body = await _backup_json(request)
    try:
        return await run_in_threadpool(backup_rules.update_channel, cid, body)
    except KeyError:
        raise HTTPException(404, "频道不存在") from None
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/gdrive/deletable")
def gdrive_deletable():
    from ..topics import backup
    return backup.deletable()


@app.post("/api/gdrive/delete", status_code=202)
async def gdrive_delete(request: Request):
    from ..topics import backup
    body = await _backup_json(request)
    try:
        backup.require_ready()
        if set(body) != {"videos"}:
            raise ValueError("请选择要删除的视频")
        return await run_in_threadpool(backup.start_delete, body["videos"])
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/gdrive/client")
async def gdrive_client(request: Request):
    from ..topics import backup
    body = await _secret_body(request, {"client_id", "client_secret"})
    if isinstance(body, JSONResponse):
        return body
    clear = body.get("clear") or []
    if any(x != "client" for x in clear) or (clear and (body.get("client_id") or body.get("client_secret"))):
        raise HTTPException(400, "只能清除 client，不能同时填写客户端")
    try:
        return await run_in_threadpool(backup.save_client, (body.get("client_id") or "").strip(),
                                       (body.get("client_secret") or "").strip(), clear=bool(clear))
    except (ValueError, OSError):
        raise HTTPException(400, "客户端 ID 和密钥必须一起填写，且不能含换行或控制字符；请确认凭据目录可写") from None


@app.post("/api/gdrive/login")
def gdrive_login():
    from ..topics import backup
    try:
        return backup.start_login()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/gdrive/login/code")
async def gdrive_code(request: Request):
    from ..topics import backup
    # 不用 Pydantic：它的校验错误会把粘贴的授权码原样带回页面。
    raw = await request.body()
    try:
        body = json.loads(raw) if len(raw) <= 32768 else None
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) != {"url"} or not isinstance(body["url"], str) or len(body["url"]) > 4096:
        raise HTTPException(400, "请粘贴授权后的地址或授权码（最多 4096 字符）")
    try:
        return await run_in_threadpool(backup.submit_code, body["url"])
    except (ValueError, OSError) as e:
        reason = str(e) if isinstance(e, ValueError) else "无法保存登录凭据，请确认凭据目录可写"
        raise HTTPException(400, backup.redact(reason)) from None


@app.post("/api/gdrive/login/cancel")
def gdrive_cancel():
    from ..topics import backup
    return backup.cancel_login()


@app.post("/api/gdrive/folder")
async def gdrive_folder(request: Request):
    from ..topics import backup
    body = await _secret_body(request, {"path"})
    if isinstance(body, JSONResponse):
        return body
    try:
        return await run_in_threadpool(backup.create_folder, body.get("path") or backup.DEFAULT_FOLDER)
    except FileExistsError:
        raise HTTPException(409, "已经建过备份文件夹") from None
    except (ValueError, OSError) as e:
        raise HTTPException(400, backup.redact(e) if isinstance(e, ValueError) else "无法保存网盘设置") from None


@app.post("/api/gdrive/test")
def gdrive_test():
    from ..topics import backup
    if busy := _throttled("gdrive", 5.0):
        return busy
    try:
        return backup.test()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/gdrive/disconnect")
def gdrive_disconnect():
    from ..topics import backup
    return backup.disconnect()


def _asr_view() -> dict:
    """The GPU box: its address, whether a token is set (never the token), the last test, and which device the last
    transcription actually used (a GPU box that is off falls back to the CPU without stopping the run)."""
    s = config.get()
    last = db.one("""SELECT detail, finished_at FROM stages WHERE name='TRANSCRIBE' AND status IN ('DONE','WARN')
                     AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1""")
    d = db.unj(last["detail"], {}) if last else {}
    used = None
    if d.get("device"):
        used = {"device": "gpu" if str(d["device"]).startswith("remote") else "cpu",
                "fell_back": bool(d.get("remote_failed")), "at": last["finished_at"]}
    return {"url": knobs.get("asr_url"), "token": bool(s.secret("asr-token")), "test": db.setting("asr_state"), "last": used}


@app.post("/api/asr")
async def asr_save(request: Request):
    """Set or clear the GPU box's address and token: {url?, token?, clear?: ["url", "token"]}. Write-only."""
    body = await _secret_body(request, {"url", "token"})
    if isinstance(body, JSONResponse):
        return body
    clear = set(body.get("clear") or [])
    url, token = (body.get("url") or "").strip(), (body.get("token") or "").strip()
    if not (url or token or clear):
        return JSONResponse({"detail": "没有要修改的内容"}, status_code=400)
    try:
        if url or "url" in clear:
            knobs.save({"asr_url": url or None})
    except knobs.KnobError as e:
        return JSONResponse({"detail": e.message, "field": "url"}, status_code=400)
    if token or "token" in clear:
        if busy := _put_secret("asr-token", token or None):
            return busy
    db.set_setting("asr_state", None)  # the last test was of the old address or token
    if token or "token" in clear:
        db.event("config", "GPU 转写令牌：" + ("已更新" if token else "已清除"))
    return _asr_view()


@app.post("/api/asr/test")
def asr_test():
    from ..media.asr import probe
    url = knobs.get("asr_url")
    if not url:
        return JSONResponse({"detail": "还没有填 GPU 转写地址"}, status_code=400)
    if busy := _throttled("asr", 5.0):
        return busy
    out = probe(url, config.get().secret("asr-token"))
    db.set_setting("asr_state", out)
    db.event("config", "测试 GPU 转写：通过" if out["ok"] else f"测试 GPU 转写没通过（{out['code']}）",
             level="info" if out["ok"] else "warn")
    return _asr_view()


def _asks() -> dict:
    """How much the words to 课代表 in Notion are used (design §R8-13): kept only if they are."""
    from ..topics.comments import used
    u = used(30)
    return {"n": u["asks"], "todo": len(u["todo"])}


def _telegram_view() -> dict:
    s = config.get()
    return {"token": bool(s.secret("telegram-bot-token")), "chat": bool(s.secret("telegram-chat-id")),
            "linked": db.setting("telegram_chat"), "digest_hour": knobs.get("digest_hour"),
            **(db.setting("telegram_state") or {"ok": None}), "pending": notify.pending()}


@app.post("/api/telegram")
async def telegram_save(request: Request):
    """Set or clear the bot token: {token?, clear?: ["token"]}. Write-only. A new bot needs its chat found again."""
    body = await _secret_body(request, {"token"})
    if isinstance(body, JSONResponse):
        return body
    token = (body.get("token") or "").strip()
    if not token and "token" not in (body.get("clear") or []):
        return JSONResponse({"detail": "没有要修改的内容"}, status_code=400)
    if token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        return JSONResponse({"detail": "bot token 的样子是「数字:一串字母数字」，在 @BotFather 里复制", "field": "token"}, status_code=400)
    for name, v in (("telegram-bot-token", token or None), ("telegram-chat-id", None)):
        if busy := _put_secret(name, v):
            return busy
    db.set_setting("telegram_chat", None)
    db.event("config", "Telegram bot token：" + ("已更新" if token else "已清除"))
    return _telegram_view()


@app.post("/api/telegram/setup")
def telegram_setup():
    """Find the chat that last messaged the bot, keep it, send a test message (notify.setup)."""
    if busy := _throttled("telegram", 5.0):
        return busy
    try:
        r = notify.setup()
    except notify.SetupError as e:
        return JSONResponse({"detail": str(e).replace("，再运行一次", "，再点一次")}, status_code=400)
    db.set_setting("telegram_chat", {"bot": r["bot"], "chat": r["chat"], "test_sent": r["test_sent"], "at": now()})
    db.event("config", f"Telegram 通知接到「{r['chat']}」" + ("" if r["test_sent"] else "，但测试消息没发出去"),
             level="info" if r["test_sent"] else "warn")
    return _telegram_view()


def _notion_box() -> dict:
    """The integration the token is (its name as Notion last said it, never the token), and where unrouted videos go."""
    return {"token": bool(config.get().secret("notion-token")), "me": db.setting("notion_me"),
            "database": db.setting("notion_database_id"), "parent": db.setting("notion_parent_page_id"),
            "title": db.setting("notion_database_title", "课代表"), "asks": _asks()}


def _notion_me(token: str) -> dict:
    """Who this token is: one /users/me, 5 s. Raises NotionError or httpx.HTTPError."""
    from ..notion.client import Notion
    n = Notion(token, timeout=5)
    try:
        me = n.me(tries=1)
    finally:
        n.c.close()
    return {"ok": True, "name": me.get("name"), "workspace": (me.get("bot") or {}).get("workspace_name"), "at": now()}


def _notion_fail(e: Exception) -> str:
    """token: Notion does not know the token; net: Notion did not answer; http_<n>: it answered something else."""
    from ..notion.client import NotionError
    if isinstance(e, NotionError):
        return "token" if e.status == 401 else f"http_{e.status}"
    return "net"


@app.post("/api/notion/token")
async def notion_token(request: Request):
    """Place or clear the integration token: {token?, clear?: ["token"]}. Write-only. A new token is asked who it is
    first and kept only if Notion knows it; the integration's name is kept for the page."""
    import httpx
    from ..notion.client import NotionError
    body = await _secret_body(request, {"token"})
    if isinstance(body, JSONResponse):
        return body
    token = (body.get("token") or "").strip()
    if not token and "token" not in (body.get("clear") or []):
        return JSONResponse({"detail": "没有要修改的内容"}, status_code=400)
    if os.environ.get(env := config.secret_env("notion-token")):
        return JSONResponse({"detail": f"由服务器环境变量 {env} 提供，这里改不了"}, status_code=409)
    me = None
    if token:
        if not re.fullmatch(r"(?:ntn|secret)_[A-Za-z0-9]{20,}", token):
            return JSONResponse({"detail": "Notion token 以 ntn_ 开头，在 integration 的 Configuration 里复制", "field": "token"},
                                status_code=400)
        try:
            me = await run_in_threadpool(_notion_me, token)
        except (NotionError, httpx.HTTPError) as e:
            code = _notion_fail(e)
            if code == "token":
                return JSONResponse({"detail": "token 不对：Notion 不认", "field": "token"}, status_code=400)
            return JSONResponse({"detail": f"没连上 Notion（{code}），token 没有保存"}, status_code=502)
    if busy := _put_secret("notion-token", token or None):
        return busy
    db.set_setting("notion_me", me)
    if me:
        db.resolve_alert("notion-auth")  # Notion knows this one; a delivery that still fails says why on its own
    db.event("config", f"Notion token：换成「{me['name']}」" if me else "Notion token：已清除")
    return _notion_box()


@app.post("/api/notion/test")
def notion_test():
    """Ask Notion who the token is, once; a failure keeps the name last seen."""
    import httpx
    from ..notion.client import NotionError
    token = config.get().secret("notion-token")
    if not token:
        return JSONResponse({"detail": "还没有放 Notion token"}, status_code=400)
    if busy := _throttled("notion", 5.0):
        return busy
    try:
        out = _notion_me(token)
    except (NotionError, httpx.HTTPError) as e:
        out = {**(db.setting("notion_me") or {}), "ok": False, "code": _notion_fail(e), "at": now()}
    db.set_setting("notion_me", out)
    who = f"（{out['name']}）" if out.get("name") else ""
    db.event("config", f"测试 Notion{who}：" + ("通过" if out["ok"] else f"没通过（{out['code']}）"),
             level="info" if out["ok"] else "warn")
    return _notion_box()


class NotionDefault(BaseModel):
    parent: str


@app.post("/api/notion/default")
def notion_default(body: NotionDefault):
    """Where a video with no channel target goes: the 课代表 database under this page (delivery.ensure_database).
    A new page means a new database there at the next delivery; pages already written stay where they are."""
    from ..notion import mapping as M
    try:
        pid = M.norm_id(body.parent)
    except ValueError as e:
        raise HTTPException(400, str(e))
    n = _notion()
    try:
        page = n._req("GET", f"/pages/{pid}")
    except Exception as e:  # noqa: BLE001 — NotionError, or the integration cannot see the page
        raise HTTPException(400, f"读不到这个页面：先把 integration 加进它的 Connections（{type(e).__name__}）")
    if db.setting("notion_parent_page_id") != pid:
        db.set_setting("notion_parent_page_id", pid)
        db.set_setting("notion_database_id", None)
        db.set_setting("notion_schema", None)
    db.event("notion", f"没有目标库的视频改写到「{_page_title(page) or pid}」下的课代表数据库")
    return {"parent": pid, "title": _page_title(page)}


# ------------------------------------------------------------------ 专题
from .notion_hook import router as notion_hook_router  # noqa: E402

app.include_router(notion_hook_router)

from .topics import router as topics_router  # noqa: E402

app.include_router(topics_router)


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
