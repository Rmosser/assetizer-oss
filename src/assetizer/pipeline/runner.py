"""Serial worker: one Source in production at a time (ADR-1).

Lifecycle per run: QUEUED → RUNNING → COMPLETE | COMPLETE_PARTIAL | FAILED,
or WAITING(reason) when an external condition blocks it. Stages checkpoint in
the ``stages`` table and are idempotent, so a crash resumes where it stopped.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import traceback
from pathlib import Path

from .. import config, db, knobs, notify, youtube
from ..llm import ContractError
from .. import providers
from ..providers.agy import ProviderError
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
QUOTA_EVERY = 900            # seconds between two reads of agy's quota while it is in use (no turn, no quota spent)

ALERT_TEXT = {
    "AGY_AUTH_REQUIRED": ("模型登录已失效，生产已暂停",
                          "在「机房」点「登录 agy」，用 Google 授权后贴回授权码；登录后队列自动继续。"),
    "PROVIDER_QUOTA": ("模型配额暂时用尽，生产已暂停", "系统每 10 分钟自动探测一次，恢复后自动继续。"),
    "YOUTUBE_AUTH": ("YouTube 需要登录或会员权限",
                     "确认 Chrome 登录的是有频道会员权限的账号，并让 CookieCloud 插件同步一次；还没配置 CookieCloud 时，"
                     "在「机房」→「YouTube 会话」里配置。系统每 30 分钟自动重试。"),
    "DISK_LOW": ("磁盘空间不足，暂停下载", "系统会先清理过期原始媒体；仍不足时请在「机房」查看占用。"),
}


# ---------------------------------------------------------------- submission
def submit(url: str, priority: int = 0, playbook_id: int | None = None, origin: str = "manual") -> dict:
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
    db.exec("""INSERT INTO runs(id, source_id, kind, playbook_id, production, priority, origin, revision_of, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""", rid, vid, "PRODUCTION", playbook_id, "QUEUED", priority, origin,
            prev and prev["current_run_id"], now())
    # a run asked for after 定稿 is meant to replace the issue, whatever was pinned while tuning
    db.exec("UPDATE sources SET adopt_pinned=0 WHERE id=? AND channel_id IN (SELECT id FROM channels WHERE stage='LOCKED')", vid)
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
            _wait(run_id, e.kind, str(e), provider=provider_of(e))
            pause_queue(e.kind)
            return "WAITING"
        if _hold_if_agy_cannot_read_media(run_id, e):
            return "WAITING"
        if e.transient:  # still failing after the call's own retries: an outage, not this run — resume it later
            _wait(run_id, "NETWORK", str(e))
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
    adopt_completed(source, run_id)
    db.event("run", f"生产完成（{state}）", run_id, data=summary)
    return state


def adopt_completed(source: dict, run_id: str) -> None:
    """A finished run becomes the issue's version, unless the user pinned another one while tuning."""
    from ..delivery import has_target
    from ..notion.mapping import run_headline
    token = config.get().secret("notion-token")
    delivery = "NOT_STARTED" if token and has_target(source) else "BLOCKED"
    db.exec("""UPDATE sources SET current_run_id=?, headline=?, delivery=CASE WHEN ? = 'BLOCKED' THEN 'BLOCKED'
               WHEN notion_page_id IS NOT NULL THEN 'STALE' ELSE 'NOT_STARTED' END,
               delivery_reason=?, review=CASE WHEN review='APPROVED' THEN 'UNREVIEWED' ELSE review END, updated_at=?
               WHERE id=? AND (adopt_pinned=0 OR current_run_id IS NULL)""",
            run_id, run_headline(run_id), delivery, None if delivery != "BLOCKED" else "NOTION_NOT_CONFIGURED", now(), source["id"])
    db.exec("UPDATE sources SET deliver_after_run=NULL WHERE id=? AND deliver_after_run=?", source["id"], run_id)


def _summary(run_id: str) -> dict:
    a = db.q("SELECT kind, status, COUNT(*) n FROM assets WHERE run_id=? GROUP BY kind, status", run_id)
    m = db.one("""SELECT COUNT(*) calls, SUM(wall_seconds) wall, SUM(input_tokens) inp, SUM(output_tokens) outp,
                  SUM(thinking_tokens) think, MAX(peak_rss_kib) rss FROM model_calls WHERE run_id=?""", run_id)
    return {"units": db.one("SELECT COUNT(*) n FROM units WHERE run_id=?", run_id)["n"],
            "assets": {f"{r['kind']}:{r['status']}": r["n"] for r in a},
            "flags_open": db.one("SELECT COUNT(*) n FROM flags WHERE run_id=? AND resolution='OPEN' AND severity!='INFO'", run_id)["n"],
            "model": m}


KEYED = {"magpie": "ChatGPT（magpie）", "tuzi": "兔子 API"}  # providers signed in with a key file, not by logging in


def provider_of(e: ProviderError) -> str:
    """Which provider a pause comes from: its receipt says, else a missing key file names it (raised before a call)."""
    p = (e.receipt or {}).get("provider")
    return p or next((k for k in KEYED if f"{k}-api-key" in str(e)), "agy")


def auth_alert(provider: str = "agy") -> None:
    """The sign-in alert, worded for what fixes it: agy signs in from the 机房 page; magpie and 兔子 need their key file.
    Its action says which provider it is about, so only agy's offers 登录 agy."""
    if provider in KEYED:
        title, body = "模型密钥失效，生产已暂停", (f"{KEYED[provider]} 的密钥缺失或失效：把有效的密钥放进 `secrets/{provider}-api-key`"
                                               "（放法见 docs/operations.md），恢复后队列自动继续。")
    else:
        title, body = ALERT_TEXT["AGY_AUTH_REQUIRED"]
    db.raise_alert("AGY_AUTH_REQUIRED", "critical", title, body, {"provider": provider})


def auth_alert_provider() -> str | None:
    """The provider the open sign-in alert is about (None: no such alert; one raised before 2026-10-28 names none: agy)."""
    a = db.one("SELECT action FROM alerts WHERE key='AGY_AUTH_REQUIRED' AND resolved_at IS NULL")
    return ((db.unj(a["action"], {}) or {}).get("provider") or "agy") if a else None


def agy_answers() -> bool:
    """agy answered (a quota read, the 机房 sign-in): what waited only for agy's sign-in goes on — the alert, a queue
    paused for it, the runs it held. A pause that another provider's key caused is left alone. True if lifted."""
    if auth_alert_provider() != "agy":
        return False
    db.resolve_alert("AGY_AUTH_REQUIRED")
    paused = db.setting("queue_paused")
    if paused and paused.get("reason") == "AGY_AUTH_REQUIRED":
        db.set_setting("queue_paused", None)
        db.exec("UPDATE runs SET production='QUEUED', wait_reason=NULL WHERE production='WAITING' AND wait_reason='AGY_AUTH_REQUIRED'")
        db.event("worker", "agy 已登录，队列继续")
    return True


def _wait(run_id: str, reason: str, message: str, provider: str | None = None) -> None:
    db.exec("UPDATE runs SET production='WAITING', wait_reason=?, error=? WHERE id=?", reason, message[:1000], run_id)
    title, body = ALERT_TEXT.get(reason, (f"生产等待：{reason}", message))
    if reason == "AGY_AUTH_REQUIRED":
        auth_alert(provider or "agy")
    elif reason in ALERT_TEXT:
        db.raise_alert(reason, "critical" if reason == "YOUTUBE_AUTH" else "warning", title, body)
    db.event("run", f"等待：{reason}", run_id, "warn")


def _fail(run_id: str, e: BaseException) -> None:
    kind = _classify(e)
    state = "CANCELLED" if kind == "SKIPPED_SHORT" else "FAILED"
    db.exec("UPDATE runs SET production=?, error_class=?, error=?, finished_at=? WHERE id=?",
            state, kind, f"{e}\n{traceback.format_exc()[-1500:]}", now(), run_id)
    db.event("run", f"生产失败：{kind}", run_id, "error")
    from ..tuning import release_hold
    release_hold(run_id)
    if state == "FAILED":
        notify.run_failed(run_id, kind, str(e))


def pause_queue(reason: str) -> None:
    db.set_setting("queue_paused", {"reason": reason, "since": now()})


# ---------------------------------------------------------------- agy media reads (agy updates itself)
def media_self_test() -> dict:
    """Run agy's media self-test and keep the result for the 机房; a pass remembers the agy version it passed on."""
    h = providers.get().media_health()
    db.set_setting("agy_media_health", h)
    if h.get("ok"):
        db.set_setting("agy_media_version", h.get("version"))
    return h


def media_alert(h: dict) -> None:
    """Pause production and say what agy did instead, so the code can be taught agy's new way to read media."""
    if h.get("error") in PAUSE_KINDS:
        # the self-test never reached the media (2026-10-27: Antigravity's weekly quota ran out right after an
        # update check): pause for that reason, which the health probe lifts, not as if agy could not read video
        if h["error"] == "AGY_AUTH_REQUIRED":
            auth_alert(providers.get().name)
        else:
            db.raise_alert(h["error"], "warning", *ALERT_TEXT[h["error"]])
        pause_queue(h["error"])
        return
    if h.get("error") in ("PROVIDER_TRANSIENT", "PROVIDER_TIMEOUT"):
        return  # the network, not agy: the next self-test decides
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
    if h.get("error") in ("PROVIDER_TRANSIENT", "PROVIDER_TIMEOUT"):
        # the self-test itself hit the network (2026-10-25 prodbox: a flaky uplink, not an agy change):
        # hold this run like any outage instead of pausing all production for an hour
        _wait(run_id, "NETWORK", str(e))
        return True
    _wait(run_id, h["error"] if h.get("error") in PAUSE_KINDS else "AGY_MEDIA_READ", str(e))
    media_alert(h)
    return True


# ---------------------------------------------------------------- worker health
MEMORY_CHECK_EVERY = 60  # seconds
MEMORY_ALERT_AT, MEMORY_CLEAR_AT = 0.8, 0.6  # share of the unit's MemoryMax


def systemd_restarts() -> int | None:
    """How often systemd restarted the worker after it died since the unit was last started by hand.
    A crash or OOM raises it; a deploy or manual restart sets it back to 0."""
    try:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "NRestarts", "--value", "assetizer-worker"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def exit_reason() -> str:
    try:
        out = subprocess.run(["journalctl", "--user", "-u", "assetizer-worker", "--since", "-15min", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "原因不明"
    return "内存不足，被系统杀掉" if "oom-kill" in out else "进程意外退出"


CGROUP_MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")
HEARTBEAT_EVERY = 30  # seconds; a thread, so a run that blocks the main loop for an hour still shows the worker alive


def worker_run_dir() -> Path:
    d = config.get().home / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def heartbeat_loop() -> None:
    """Liveness for a container healthcheck (the LazyCat package): the worker process is up."""
    beat = worker_run_dir() / "worker.heartbeat"
    while True:
        beat.touch()
        time.sleep(HEARTBEAT_EVERY)


def container_exit_reason() -> str:
    """Without systemd: the container cgroup's OOM-kill count tells an OOM from any other crash."""
    try:
        events = dict(line.split() for line in CGROUP_MEMORY_EVENTS.read_text().splitlines())
        kills = int(events.get("oom_kill", 0))
    except (OSError, ValueError):
        return "原因不明"
    seen = db.setting("worker_oom_kills") or 0
    db.set_setting("worker_oom_kills", kills)
    return "内存不足，被系统杀掉" if kills > seen else "进程意外退出"


def cgroup_memory() -> tuple[int, int] | None:
    """Anonymous memory of the worker's cgroup (worker, agy and whisper together) and its MemoryMax, in bytes."""
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        cg = Path("/sys/fs/cgroup") / rel.lstrip("/")
        stat = dict(line.split() for line in (cg / "memory.stat").read_text().splitlines())
        limit = (cg / "memory.max").read_text().strip()
        return (int(stat["anon"]), int(limit)) if limit != "max" else None
    except (OSError, IndexError, KeyError, ValueError):
        return None


def memory_alert() -> None:
    m = cgroup_memory()
    if not m:
        return
    anon, limit = m
    if anon > MEMORY_ALERT_AT * limit:
        db.raise_alert("MEMORY_HIGH", "warning", "Worker 内存接近上限",
                       f"worker（含 agy、whisper）占用 {anon / 2**30:.1f} GB，上限 {limit / 2**30:.0f} GB。"
                       "超过上限时 worker 会被系统杀掉，随后自动重启并从断点续跑。")
    elif anon < MEMORY_CLEAR_AT * limit:
        db.resolve_alert("MEMORY_HIGH")


# ---------------------------------------------------------------- worker loop
def worker_slots() -> int:
    """How many runs the worker produces at once: setting `worker_slots` (1–16, default 1). A run is mostly waiting
    on agy (8 at once on prodbox peaked at 9.1 GB; 16 agy calls at once ran clean once the container proxy
    allowed enough connections, 2026-10-26) and whisper takes turns, so it is set per box to fit that box."""
    return knobs.get("worker_slots")


class Worker:
    def __init__(self, poll: float = 5.0):
        self.poll = poll
        self.stop = False
        self.last = {"health": 0.0, "retry": 0.0, "retention": 0.0, "ingest": 0.0, "probe": 0.0, "media": 0.0,
                     "memory": 0.0, "digest": 0.0, "playback": 0.0, "quota": 0.0}
        self.active: dict[str, threading.Thread] = {}  # run id → the thread producing it
        self.finished: dict[str, str] = {}             # run id → final state, until reap() picks it up
        self.signed_out = 0                            # quota reads in a row that found agy signed out

    def recover(self) -> None:
        n = kill_orphans()
        interrupted = [r["id"] for r in db.q("SELECT id FROM runs WHERE production='RUNNING'")]
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
        restarts = systemd_restarts()
        if restarts is not None:
            if restarts > (db.setting("worker_nrestarts") or 0):
                notify.worker_recovered(exit_reason(), interrupted)
            db.set_setting("worker_nrestarts", restarts)
        else:  # a container: a SIGTERM stop leaves a clean-exit mark; its absence after an earlier start is a crash
            run_dir = worker_run_dir()
            clean = run_dir / "worker.clean-exit"
            if (run_dir / "worker.heartbeat").exists() and not clean.exists():
                notify.worker_recovered(container_exit_reason(), interrupted)
            clean.unlink(missing_ok=True)

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
            if providers.get().version() != db.setting("agy_media_version"):  # agy updated itself (or was never checked)
                h = media_self_test()
                db.event("worker", f"agy {h.get('version')} 读视频自检{'通过' if h.get('ok') else '没通过'}",
                         level="info" if h.get("ok") else "warn")
                if not h.get("ok"):
                    media_alert(h)
                    paused = db.setting("queue_paused")
        if paused and paused.get("reason") != "AGY_MEDIA_READ" and t - self.last["health"] > 600:
            self.last["health"] = t
            hs = [p.health() for p in providers.in_use()]
            h = next((x for x in hs if not x.get("ok")), hs[0])
            db.set_setting("agy_health", h)
            if h.get("ok"):
                db.set_setting("queue_paused", None)
                db.resolve_alert(paused["reason"])
                db.exec("UPDATE runs SET production='QUEUED' WHERE production='WAITING' AND wait_reason=?", paused["reason"])
                db.event("worker", "模型恢复，队列继续")
        if not paused and t - self.last["probe"] > 3 * 3600 and not db.one("SELECT 1 FROM runs WHERE production='RUNNING'"):
            self.last["probe"] = t  # idle-time login probe so the Engine lamp is current
            h = providers.get().health()
            db.set_setting("agy_health", h)
            if not h.get("ok") and h.get("error") == "AGY_AUTH_REQUIRED":
                auth_alert(providers.get().name)
        if t - self.last["quota"] > QUOTA_EVERY and any(p.name == "agy" for p in providers.in_use()):
            self.last["quota"] = t  # the 机房 page's quota bars and their week-long trend
            from ..providers.agy import AgyProvider, record_usage
            q = record_usage(AgyProvider().usage())
            self.signed_out = self.signed_out + 1 if q.get("error") == "AGY_AUTH_REQUIRED" else 0
            if self.signed_out >= 2:  # twice in a row: say so before a run fails on it
                auth_alert("agy")
            elif q.get("ok"):
                agy_answers()
        if t - self.last["retry"] > 1800:
            self.last["retry"] = t
            db.exec("UPDATE runs SET production='QUEUED' WHERE production='WAITING' "
                    "AND wait_reason IN ('YOUTUBE_AUTH','DISK_LOW','LIVE_IN_PROGRESS','NETWORK')")
        if t - self.last["retention"] > 3600:
            self.last["retention"] = t
            retention()
        if t - self.last["playback"] > 60:
            self.last["playback"] = t
            from .. import playback
            try:
                playback.tick()  # 原片栏的低清版: release what tuning no longer needs, make the next one
            except Exception as e:  # noqa: BLE001
                db.event("worker", f"原片低清版异常：{e}", level="warn")
        from ..ingest import EVERY as INGEST_EVERY
        if t - self.last["ingest"] > INGEST_EVERY:
            self.last["ingest"] = t
            from ..ingest import poll
            try:
                poll()
            except Exception as e:  # noqa: BLE001
                db.event("ingest", f"自动收录异常：{e}", level="warn")
        if t - self.last["memory"] > MEMORY_CHECK_EVERY:
            self.last["memory"] = t
            memory_alert()
        if t - self.last["digest"] > 300:
            self.last["digest"] = t
            if notify.configured() and notify.digest_due():
                notify.send_digest()

    def start(self, run_id: str) -> None:
        def work():
            try:
                self.finished[run_id] = execute(run_id)
            except Exception as e:  # noqa: BLE001 — execute records its own failures; this is a crash around it
                self.finished[run_id] = "FAILED"
                db.event("worker", f"Worker 异常：{e}", run_id, "error", {"tb": traceback.format_exc()[-1500:]})
        t = threading.Thread(target=work, name=f"run-{run_id}", daemon=True)
        self.active[run_id] = t
        t.start()

    def reap(self) -> bool:
        """Forget finished run threads; True when one of them completed (its issue is ready to deliver)."""
        completed = False
        for run_id in [k for k, t in self.active.items() if not t.is_alive()]:
            del self.active[run_id]
            completed |= self.finished.pop(run_id, "").startswith("COMPLETE")
        return completed

    def tick(self) -> bool:
        self.housekeeping()
        if apply_nudges():
            return True
        from ..delivery import deliver_pending
        if self.reap():  # delivery stays on this thread, one issue at a time, so two runs never race for a page
            deliver_pending()
            return True
        if db.setting("queue_paused"):
            return False
        started = False
        while len(self.active) < worker_slots():
            skip = ",".join("?" * len(self.active)) or "''"
            r = db.one(f"SELECT id FROM runs WHERE production='QUEUED' AND id NOT IN ({skip}) "
                       "ORDER BY priority DESC, created_at LIMIT 1", *self.active)
            if not r:
                break
            self.start(r["id"])
            started = True
        if started:
            return True
        if len(self.active) < worker_slots():
            from ..ingest import dispatch_backfill
            from ..tuning import dispatch_next
            if dispatch_next() or dispatch_backfill():  # 调试台's next trial, else the next 补录 video
                return True
        batch = db.setting("trial_batch")
        # the batch is done when no trial waits or runs — whatever else (补录, a new video) is still in production
        if batch and not db.one("SELECT 1 FROM trial_queue WHERE status='PENDING'") and not db.one(
                """SELECT 1 FROM trial_queue t JOIN runs r ON r.id=t.run_id
                   WHERE t.status='DISPATCHED' AND r.production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')"""):
            db.set_setting("trial_batch", None)
            notify.trials_done(batch)
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
            try:
                (worker_run_dir() / "worker.clean-exit").touch()
            except OSError:
                pass
            os._exit(0)

        signal.signal(signal.SIGTERM, on_term)
        threading.Thread(target=heartbeat_loop, name="worker-heartbeat", daemon=True).start()
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
        days = knobs.get("delivered_retention_days" if delivered else "retention_days")
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
