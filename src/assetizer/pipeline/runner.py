"""Serial worker: one Source in production at a time (ADR-1).

Lifecycle per run: QUEUED → RUNNING → COMPLETE | COMPLETE_PARTIAL | FAILED,
or WAITING(reason) when an external condition blocks it. Stages checkpoint in
the ``stages`` table and are idempotent, so a crash resumes where it stopped.
"""
from __future__ import annotations

import os
import shutil
import signal
import threading
import time
import traceback
from pathlib import Path

from .. import config, db, youtube
from ..llm import ContractError
from ..providers.agy import AgyProvider, ProviderError
from ..util import now
from . import acquire, finish, media_stages, semantic
from .context import RunContext, StageWarning, Waiting

# name, fn, blocking
STAGES: list[tuple[str, object, bool]] = [
    ("RESOLVE", acquire.resolve, True),
    ("PLAYBOOK", acquire.playbook, True),
    ("ACQUIRE", acquire.acquire, True),
    ("CHUNK", acquire.chunk, True),
    ("UNDERSTAND", semantic.understand, True),
    ("TRANSCRIBE", media_stages.transcribe, False),   # runs in background from MERGE until BOUNDARY
    ("MERGE", semantic.merge, True),
    ("KNOWLEDGE", semantic.knowledge, True),
    ("ARTICLE", semantic.article, True),
    ("SELECT", semantic.select, False),
    ("SCREENSHOTS", media_stages.screenshots, False),
    ("BOUNDARY", media_stages.boundary, False),
    ("CLIPS", media_stages.clips, False),
    ("SUBTITLES", media_stages.subtitle_stage, False),
    ("MEDIA_QA", media_stages.media_qa, False),
    ("REVIEW_CHECK", finish.review_check, False),
    ("COMPOSE", finish.compose, True),
    ("PACKAGE", finish.package, True),
    ("CLEANUP", finish.cleanup, False),
]
STAGE_NAMES = [s[0] for s in STAGES]
ALWAYS_RERUN = {"PLAYBOOK"}
PAUSE_KINDS = {"AGY_AUTH_REQUIRED", "PROVIDER_QUOTA"}
MEDIA_CHECK_EVERY = 600      # seconds: while idle, look this often whether agy updated itself
MEDIA_RECHECK_PAUSED = 3600  # seconds: while paused for AGY_MEDIA_READ, repeat the media self-test this often

ALERT_TEXT = {
    "AGY_AUTH_REQUIRED": ("模型登录已失效，生产已暂停",
                          "在本机终端执行 `ssh -t ovh-investment /home/debian/.local/bin/agy`，按提示完成 Google 登录。恢复后队列自动继续。"),
    "PROVIDER_QUOTA": ("模型配额暂时用尽，生产已暂停", "系统每 10 分钟自动探测一次，恢复后自动继续。"),
    "YOUTUBE_AUTH": ("YouTube 需要登录或会员权限",
                     "用了 CookieCloud 时：确认 Chrome 登录的是有频道会员权限的账号，并让 CookieCloud 插件同步一次。"
                     "没用 CookieCloud 时：建立隧道 `ssh -L 6080:127.0.0.1:6080 ovh-investment`，浏览器打开 http://localhost:6080/vnc.html ，"
                     "在远程 Chromium 中登录 YouTube。系统每 30 分钟自动重试。"),
    "DISK_LOW": ("磁盘空间不足，暂停下载", "系统会先清理过期原始媒体；仍不足时请在「机房」查看占用。"),
}


# ---------------------------------------------------------------- submission
def submit(url: str, priority: int = 0, playbook_id: int | None = None) -> dict:
    vid = youtube.video_id(url)
    if not vid:
        raise ValueError("无法识别的 YouTube 链接")
    if not db.one("SELECT id FROM sources WHERE id=?", vid):
        db.exec("INSERT INTO sources(id, url, created_at, updated_at) VALUES (?,?,?,?)",
                vid, f"https://www.youtube.com/watch?v={vid}", now(), now())
    active = db.one("SELECT id FROM runs WHERE source_id=? AND production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')", vid)
    if active:
        return {"source_id": vid, "run_id": active["id"], "existing": True}
    n = (db.one("SELECT COUNT(*) n FROM runs WHERE source_id=?", vid) or {"n": 0})["n"]
    rid = f"{vid}-r{n + 1}"
    prev = db.one("SELECT current_run_id FROM sources WHERE id=?", vid)
    db.exec("INSERT INTO runs(id, source_id, kind, playbook_id, production, priority, revision_of, created_at) VALUES (?,?,?,?,?,?,?,?)",
            rid, vid, "PRODUCTION", playbook_id, "QUEUED", priority, prev and prev["current_run_id"], now())
    for i, name in enumerate(STAGE_NAMES):
        db.exec("INSERT OR IGNORE INTO stages(run_id, name, ord, status) VALUES (?,?,?,'PENDING')", rid, name, i)
    db.event("submit", f"已加入队列：{url}", rid)
    return {"source_id": vid, "run_id": rid, "existing": False}


# ---------------------------------------------------------------- orphans
def kill_orphans() -> int:
    """Kill agy and whisper processes left behind by a previous worker (they run in their own session)."""
    from ..media.asr import CHILD

    runs_dir = str(config.get().runs_dir)
    killed = 0
    proc = Path("/proc")
    if not proc.exists():
        return 0
    for p in proc.iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().split(b"\0")
            whisper = len(cmd) > 3 and cmd[1:3] == [b"-m", CHILD.encode()] and cmd[3].startswith(runs_dir.encode())
            if not (cmd[0].endswith(b"agy") or whisper):
                continue
            cwd = os.readlink(p / "cwd")
            if whisper or cwd.startswith(runs_dir):
                os.killpg(os.getpgid(int(p.name)), signal.SIGKILL)
                killed += 1
        except (OSError, ProcessLookupError):
            continue
    return killed


# ---------------------------------------------------------------- execution
def _set_stage(run_id: str, name: str, **kw) -> None:
    sets = ", ".join(f"{k}=?" for k in kw)
    db.exec(f"UPDATE stages SET {sets} WHERE run_id=? AND name=?", *kw.values(), run_id, name)


def _exec_stage(ctx: RunContext, name: str, fn, foreground: bool = True) -> tuple[str, dict | str]:
    t0 = time.monotonic()
    _set_stage(ctx.id, name, status="RUNNING", started_at=now())
    if foreground:
        db.exec("UPDATE runs SET stage=? WHERE id=?", name, ctx.id)
    try:
        detail = fn(ctx) or {}
        _set_stage(ctx.id, name, status="DONE", finished_at=now(), wall_seconds=round(time.monotonic() - t0, 1), detail=db.j(detail))
        ctx.event(f"{name} 完成", data=detail)
        return "DONE", detail
    except StageWarning as w:
        _set_stage(ctx.id, name, status="WARN", finished_at=now(), wall_seconds=round(time.monotonic() - t0, 1), detail=db.j({"warning": str(w)}))
        ctx.event(f"{name} 警告：{w}", "warn")
        return "WARN", str(w)
    except Exception:
        _set_stage(ctx.id, name, status="FAILED", finished_at=now(), wall_seconds=round(time.monotonic() - t0, 1),
                   detail=db.j({"error": traceback.format_exc()[-2000:]}))
        raise


def _classify(e: BaseException) -> str:
    for attr in ("kind", "reason"):
        if getattr(e, attr, None):
            return getattr(e, attr)
    msg = str(e)
    if ":" in msg and msg.split(":", 1)[0].isupper():
        return msg.split(":", 1)[0]
    return type(e).__name__


def execute(run_id: str) -> str:
    import fcntl
    lockf = (config.get().runs_dir / f".{run_id}.lock").open("w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "LOCKED"
    try:
        return _execute(run_id)
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def _execute(run_id: str) -> str:
    run = db.one("SELECT * FROM runs WHERE id=?", run_id)
    source = db.one("SELECT * FROM sources WHERE id=?", run["source_id"])
    ctx = RunContext(run=run, source=source)
    db.exec("UPDATE runs SET production='RUNNING', wait_reason=NULL, error=NULL, error_class=NULL, attempts=attempts+1, "
            "started_at=COALESCE(started_at, ?) WHERE id=?", now(), run_id)
    done = {r["name"]: r["status"] for r in db.q("SELECT name, status FROM stages WHERE run_id=?", run_id)}
    bg: threading.Thread | None = None
    bg_err: list = []
    warned = False

    def run_bg():
        try:
            st, _ = _exec_stage(ctx, "TRANSCRIBE", media_stages.transcribe, foreground=False)
        except Exception as e:  # noqa: BLE001 — transcript is non-blocking
            bg_err.append(e)
            _set_stage(ctx.id, "TRANSCRIBE", status="WARN", detail=db.j({"warning": str(e)[:500]}))

    try:
        for name, fn, blocking in STAGES:
            if name == "TRANSCRIBE":
                continue
            if name == "MERGE" and done.get("TRANSCRIBE") not in ("DONE", "WARN"):
                bg = threading.Thread(target=run_bg, daemon=True)
                bg.start()
            if name == "BOUNDARY" and bg is not None:
                db.exec("UPDATE runs SET stage='TRANSCRIBE' WHERE id=?", run_id)
                bg.join()
                bg = None
            if done.get(name) in ("DONE", "WARN", "SKIPPED") and name not in ALWAYS_RERUN:
                warned |= done.get(name) == "WARN"
                continue
            try:
                st, _ = _exec_stage(ctx, name, fn)
                warned |= st == "WARN"
                if name in ("RESOLVE", "ACQUIRE") and st == "DONE":
                    db.resolve_alert("YOUTUBE_AUTH")
                    if name == "ACQUIRE":
                        db.resolve_alert("DISK_LOW")
            except (Waiting, ProviderError) as e:
                if isinstance(e, ProviderError) and e.kind not in PAUSE_KINDS:
                    if blocking:
                        raise
                    _set_stage(run_id, name, status="WARN", detail=db.j({"warning": str(e)[:500]}))
                    warned = True
                    continue
                raise
            except Exception as e:  # noqa: BLE001
                if blocking:
                    raise
                _set_stage(run_id, name, status="WARN", detail=db.j({"warning": f"{_classify(e)}: {str(e)[:400]}"}))
                ctx.event(f"{name} 非阻塞失败：{e}", "warn")
                warned = True
        if bg is not None:
            bg.join()
            bg = None
        if bg_err:
            warned = True
    except Waiting as w:
        _wait(run_id, w.reason, str(w))
        return "WAITING"
    except ProviderError as e:
        if e.kind in PAUSE_KINDS:
            _wait(run_id, e.kind, str(e))
            pause_queue(e.kind)
            return "WAITING"
        if _hold_if_agy_cannot_read_media(run_id, e):
            return "WAITING"
        _fail(run_id, e)
        return "FAILED"
    except Exception as e:  # noqa: BLE001
        _fail(run_id, e)
        return "FAILED"
    finally:
        if bg is not None:  # never let whisper overlap the next run's agy call
            bg.join()
    state = "COMPLETE_PARTIAL" if warned else "COMPLETE"
    summary = _summary(run_id)
    db.exec("UPDATE runs SET production=?, stage=NULL, finished_at=?, summary=? WHERE id=?", state, now(), db.j(summary), run_id)
    from ..delivery import has_target
    token = config.get().secret("notion-token")
    delivery = "NOT_STARTED" if token and has_target(source) else "BLOCKED"
    db.exec("""UPDATE sources SET current_run_id=?, delivery=CASE WHEN ? = 'BLOCKED' THEN 'BLOCKED'
               WHEN notion_page_id IS NOT NULL THEN 'STALE' ELSE 'NOT_STARTED' END,
               delivery_reason=?, review=CASE WHEN review='APPROVED' THEN 'UNREVIEWED' ELSE review END, updated_at=? WHERE id=?""",
            run_id, delivery, None if delivery != "BLOCKED" else "NOTION_NOT_CONFIGURED", now(), source["id"])
    db.event("run", f"生产完成（{state}）", run_id, data=summary)
    return state


def _summary(run_id: str) -> dict:
    a = db.q("SELECT kind, status, COUNT(*) n FROM assets WHERE run_id=? GROUP BY kind, status", run_id)
    m = db.one("""SELECT COUNT(*) calls, SUM(wall_seconds) wall, SUM(input_tokens) inp, SUM(output_tokens) outp,
                  SUM(thinking_tokens) think, MAX(peak_rss_kib) rss FROM model_calls WHERE run_id=?""", run_id)
    return {"units": db.one("SELECT COUNT(*) n FROM units WHERE run_id=?", run_id)["n"],
            "assets": {f"{r['kind']}:{r['status']}": r["n"] for r in a},
            "flags_open": db.one("SELECT COUNT(*) n FROM flags WHERE run_id=? AND resolution='OPEN' AND severity!='INFO'", run_id)["n"],
            "model": m}


def _wait(run_id: str, reason: str, message: str) -> None:
    db.exec("UPDATE runs SET production='WAITING', wait_reason=?, error=? WHERE id=?", reason, message[:1000], run_id)
    title, body = ALERT_TEXT.get(reason, (f"生产等待：{reason}", message))
    if reason in ALERT_TEXT:
        db.raise_alert(reason, "critical" if reason in ("AGY_AUTH_REQUIRED", "YOUTUBE_AUTH") else "warning", title, body)
    db.event("run", f"等待：{reason}", run_id, "warn")


def _fail(run_id: str, e: BaseException) -> None:
    kind = _classify(e)
    state = "CANCELLED" if kind == "SKIPPED_SHORT" else "FAILED"
    db.exec("UPDATE runs SET production=?, error_class=?, error=?, finished_at=? WHERE id=?",
            state, kind, f"{e}\n{traceback.format_exc()[-1500:]}", now(), run_id)
    db.event("run", f"生产失败：{kind}", run_id, "error")


def pause_queue(reason: str) -> None:
    db.set_setting("queue_paused", {"reason": reason, "since": now()})


# ---------------------------------------------------------------- agy media reads (agy updates itself)
def media_self_test() -> dict:
    """Run agy's media self-test and keep the result for the 机房; a pass remembers the agy version it passed on."""
    h = AgyProvider().media_health()
    db.set_setting("agy_media_health", h)
    if h.get("ok"):
        db.set_setting("agy_media_version", h.get("version"))
    return h


def media_alert(h: dict) -> None:
    """Pause production and say what agy did instead, so the code can be taught agy's new way to read media."""
    mb = f"{(h.get('bytes') or 0) / 1048576:.0f} MB"
    if h.get("error") == "MEDIA_READ_FAILED":
        how = (f"拒绝读取 {mb} 的自检视频：它可能又调低了单个视频文件的大小上限。"
               "要调小 media/ffmpeg.py 里的 VIEW_FILE_LIMIT，片段会照新上限重新压缩")
    elif h.get("tool"):
        how = (f"读视频时用了没见过的工具 {h['tool']}（参数 {'、'.join(h.get('parameters') or []) or '无'}）。"
               "要把它登记进 providers/agy.py 的 MEDIA_READ")
    elif h.get("error") == "WRONG_ANSWER":
        how = "读了自检视频，却没答对画面里的数字"
    else:
        how = f"读视频自检出错（{h.get('error')}）"
    db.raise_alert("AGY_MEDIA_READ", "critical", "agy 读不了视频，生产已暂停",
                   f"agy {h.get('version')} {how}。系统每小时自检一次，通过后自动继续。需要改代码时，把这条告警交给维护者。")
    pause_queue("AGY_MEDIA_READ")


def _hold_if_agy_cannot_read_media(run_id: str, e: ProviderError) -> bool:
    """A media call failed on reading the media itself: every attempt deviated, or agy refused the file. If the
    self-test fails too, agy itself changed (2026-10-22: an update renamed view_file and cut the file size limit to
    ~25 MB): the run waits and production pauses, instead of failing every run after it. If the self-test passes,
    the problem belongs to this run, which fails as before."""
    if e.kind not in ("MODALITY_ROUTING_DEVIATION", "MEDIA_READ_COUNT", "MEDIA_READ_FAILED"):
        return False
    h = media_self_test()
    if h.get("ok"):
        return False
    _wait(run_id, "AGY_MEDIA_READ", str(e))
    media_alert(h)
    return True


# ---------------------------------------------------------------- worker loop
class Worker:
    def __init__(self, poll: float = 5.0):
        self.poll = poll
        self.stop = False
        self.last = {"health": 0.0, "retry": 0.0, "retention": 0.0, "ingest": 0.0, "probe": 0.0, "media": 0.0}

    def recover(self) -> None:
        n = kill_orphans()
        for r in db.q("SELECT id, attempts FROM runs WHERE production='RUNNING'"):
            if r["attempts"] < 5:
                db.exec("UPDATE runs SET production='QUEUED' WHERE id=?", r["id"])
            else:
                db.exec("UPDATE runs SET production='FAILED', error_class='TOO_MANY_RESTARTS', error=? WHERE id=?",
                        "Worker 多次在该期生产中重启，已停止自动续跑；请在机房重试。", r["id"])
            db.exec("UPDATE stages SET status='PENDING' WHERE run_id=? AND status='RUNNING'", r["id"])
            db.event("run", "Worker 重启，从断点继续", r["id"], "warn")
        db.exec("UPDATE sources SET delivery='NOT_STARTED' WHERE delivery='DELIVERING'")
        finish.sweep_agy_state()
        if n:
            db.event("worker", f"清理了 {n} 个遗留 agy 进程", level="warn")

    def housekeeping(self) -> None:
        t = time.monotonic()
        paused = db.setting("queue_paused")
        if paused and paused.get("reason") == "AGY_MEDIA_READ":
            # a text-only health probe would pass and resume into the same failure: only the media self-test decides
            if t - self.last["media"] > MEDIA_RECHECK_PAUSED:
                self.last["media"] = t
                h = media_self_test()
                if h.get("ok"):
                    db.set_setting("queue_paused", None)
                    db.resolve_alert("AGY_MEDIA_READ")
                    db.exec("UPDATE runs SET production='QUEUED' WHERE production='WAITING' AND wait_reason='AGY_MEDIA_READ'")
                    db.event("worker", f"agy {h.get('version')} 能读视频了，队列继续")
                else:
                    media_alert(h)
        elif not paused and t - self.last["media"] > MEDIA_CHECK_EVERY and not db.one("SELECT 1 FROM runs WHERE production='RUNNING'"):
            self.last["media"] = t
            if AgyProvider().version() != db.setting("agy_media_version"):  # agy updated itself (or was never checked)
                h = media_self_test()
                db.event("worker", f"agy {h.get('version')} 读视频自检{'通过' if h.get('ok') else '没通过'}",
                         level="info" if h.get("ok") else "warn")
                if not h.get("ok"):
                    media_alert(h)
                    paused = db.setting("queue_paused")
        if paused and paused.get("reason") != "AGY_MEDIA_READ" and t - self.last["health"] > 600:
            self.last["health"] = t
            h = AgyProvider().health()
            db.set_setting("agy_health", h)
            if h.get("ok"):
                db.set_setting("queue_paused", None)
                db.resolve_alert(paused["reason"])
                db.exec("UPDATE runs SET production='QUEUED' WHERE production='WAITING' AND wait_reason=?", paused["reason"])
                db.event("worker", "模型恢复，队列继续")
        if not paused and t - self.last["probe"] > 3 * 3600 and not db.one("SELECT 1 FROM runs WHERE production='RUNNING'"):
            self.last["probe"] = t  # idle-time login probe so the Engine lamp is current
            h = AgyProvider().health()
            db.set_setting("agy_health", h)
            if not h.get("ok") and h.get("error") == "AGY_AUTH_REQUIRED":
                db.raise_alert("AGY_AUTH_REQUIRED", "critical", *ALERT_TEXT["AGY_AUTH_REQUIRED"])
        if t - self.last["retry"] > 1800:
            self.last["retry"] = t
            db.exec("UPDATE runs SET production='QUEUED' WHERE production='WAITING' AND wait_reason IN ('YOUTUBE_AUTH','DISK_LOW','LIVE_IN_PROGRESS')")
        if t - self.last["retention"] > 3600:
            self.last["retention"] = t
            retention()
        if t - self.last["ingest"] > 1800:
            self.last["ingest"] = t
            from ..ingest import poll
            try:
                poll()
            except Exception as e:  # noqa: BLE001
                db.event("ingest", f"自动收录异常：{e}", level="warn")

    def tick(self) -> bool:
        self.housekeeping()
        if apply_nudges():
            return True
        if db.setting("queue_paused"):
            return False
        from ..delivery import deliver_pending
        r = db.one("SELECT id FROM runs WHERE production='QUEUED' ORDER BY priority DESC, created_at LIMIT 1")
        if r:
            state = execute(r["id"])
            if state.startswith("COMPLETE"):
                deliver_pending()
            return True
        from ..tuning import dispatch_next
        if dispatch_next():  # 调试台: the next queued trial run
            return True
        return deliver_pending()

    def loop(self) -> None:
        self.recover()
        def on_term(*_):
            # stop now: kill in-flight agy groups; the next start resumes from checkpoints
            from ..providers.agy import LIVE_PGIDS
            for pg in list(LIVE_PGIDS):
                try:
                    os.killpg(pg, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os._exit(0)

        signal.signal(signal.SIGTERM, on_term)
        db.event("worker", "Worker 已启动")
        while not self.stop:
            try:
                busy = self.tick()
            except Exception as e:  # noqa: BLE001
                db.event("worker", f"Worker 异常：{e}", level="error", data={"tb": traceback.format_exc()[-1500:]})
                busy = False
            if not busy:
                time.sleep(self.poll)


def retention() -> dict:
    """Purge original media past retention; never touch a source with an active run."""
    s = config.get()
    purged = 0
    rows = db.q("""SELECT s.* FROM sources s WHERE s.media_state='CACHED' AND NOT EXISTS
                   (SELECT 1 FROM runs r WHERE r.source_id=s.id AND r.production IN ('QUEUED','RUNNING','WAITING'))""")
    import datetime as dt
    for r in rows:
        # delivered issues are done with their media; tuning samples (never delivered) keep it for re-runs
        delivered = r["delivery"] == "DELIVERED" and r["delivered_at"]
        ref = r["delivered_at"] if delivered else r["updated_at"]
        days = 2 if delivered else s.source_retention_days
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ref)).days
        except (TypeError, ValueError):
            continue
        if age >= days and r["media_path"]:
            d = Path(r["media_path"]).parent
            if d.is_relative_to(s.media_dir):
                shutil.rmtree(d, ignore_errors=True)
            db.exec("UPDATE sources SET media_state='PURGED', purged_at=? WHERE id=?", now(), r["id"])
            purged += 1
    finish.sweep_agy_state()
    return {"purged": purged}


def apply_nudges() -> bool:
    """Re-cut clips whose boundaries the reviewer nudged (deterministic, no model)."""
    rows = [r for r in db.q("SELECT * FROM assets WHERE kind='clip' AND meta LIKE '%pending_nudge%'")
            if (db.unj(r["meta"], {}) or {}).get("pending_nudge")]
    if not rows:
        return False
    from ..media import ffmpeg
    for r in rows:
        meta = db.unj(r["meta"], {})
        req = meta.pop("pending_nudge")
        run = db.one("SELECT * FROM runs WHERE id=?", r["run_id"])
        src = db.one("SELECT * FROM sources WHERE id=?", r["source_id"])
        ctx = RunContext(run=run, source=src)
        unit = db.one("SELECT label FROM units WHERE id=?", r["unit_id"])
        rev = (r["rev"] or 1) + 1
        d = ctx.dir / "assets" / "clips" / unit["label"] / f"rev{rev}"
        try:
            clean = ffmpeg.render_clip(Path(src["media_path"]), req["start"], req["end"], d / "clip-clean.mp4")
            ffmpeg.frame(clean, min(3.0, (req["end"] - req["start"]) / 2), d / "poster.jpg", width=1280)
            meta.update(nudged_from={"start": r["start"], "end": r["end"]}, nudged_at=now(), boundary_source="USER_NUDGE")
            db.exec("UPDATE assets SET path=?, start=?, end=?, rev=?, meta=?, bytes=? WHERE id=?",
                    str(clean), req["start"], req["end"], rev, db.j(meta), clean.stat().st_size, r["id"])
            db.exec("UPDATE assets SET path=?, start=?, end=? WHERE run_id=? AND unit_id=? AND kind='poster'",
                    str(d / "poster.jpg"), req["start"], req["start"], r["run_id"], r["unit_id"])
            # subtitles regenerate from the shared transcript if available
            db.exec("DELETE FROM assets WHERE run_id=? AND unit_id=? AND kind IN ('clip_subtitled','srt','ass')", r["run_id"], r["unit_id"])
            try:
                media_stages.subtitle_stage(ctx, only_unit=r["unit_id"])
            except StageWarning:
                pass
            finish.compose(ctx)
            finish.package(ctx)
            db.exec("UPDATE sources SET delivery=CASE WHEN delivery='DELIVERED' THEN 'STALE' ELSE delivery END WHERE id=?", src["id"])
            db.event("review", f"片段已按微调重剪（rev {rev}）", r["run_id"])
        except Exception as e:  # noqa: BLE001
            db.exec("UPDATE assets SET meta=? WHERE id=?", db.j(meta | {"nudge_error": str(e)[:300]}), r["id"])
            db.event("review", f"微调重剪失败：{e}", r["run_id"], "error")
    return True
