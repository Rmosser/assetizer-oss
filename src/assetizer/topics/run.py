"""Topics in the worker: each video of a topic is read for the topic's question when the worker has a slot, and once
they are read the answer is put together.

A read goes ACQUIRE → DRAFT → PLAN → LOOK → SECOND → WRITE → (LOOK2 → SECOND2 → REVISE when the notes listed what to
look at again). Every step keeps what it made (the draft by video, the rest under topics/<tid>/r<rev>/<vid>/), so a
read stopped by a pause, a restart or a wait goes on where it was. A video the plan finds unrelated stops after PLAN
(fit "none") unless the user asks for it anyway (force).

Production comes first: a topic's video starts only in a slot no queued run wants, at most `topic_slots` at once, and
not while a window of a subscription its steps draw on is below what the channels keep (`topic_min_weekly`,
`topic_min_5h`; held()). A topic's waits are its own: they never pause the queue or raise an alert. Its calls are
recorded in model_calls under run_id "topic-<tid>"."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import threading
import traceback
from pathlib import Path
from typing import Any

from .. import config, db, knobs, notify, youtube
from ..llm import LLM, ContractError
from ..media import ffmpeg
from ..pipeline.context import Waiting
from ..providers import is_agy, is_model, resolve
from ..providers.agy import ProviderError
from ..util import now, read_json, write_json, write_text
from . import Stopped, draft_dir, media_dir, root, synth_dir, work_dir
from . import write as W
from .draft import Draft
from .look import Looker

STEPS = ("ACQUIRE", "DRAFT", "PLAN", "LOOK", "SECOND", "WRITE", "LOOK2", "SECOND2", "REVISE")
TERMINAL = ("DONE", "FAILED")
PROVIDER_WAITS = {"PROVIDER_QUOTA", "AGY_AUTH_REQUIRED", "PROVIDER_MODEL"}
RETRY_EVERY = 1800        # seconds: waits are tried again this often
KEEP_DRAFTS_DAYS = 30     # a video no topic has keeps its draft this long, for the next topic that wants it
MIN_FREE = 15e9
FORCE_NOTE = "（之前判断这期和问题无关，用户仍要读这期：按 some 处理，写出回看清单和写作提示词。）"
_last = {"retry": 0.0, "retention": 0.0}


def changed(sql: str, *args) -> int:
    """Rows an UPDATE changed (db.exec answers with the connection's last inserted row id when there is one)."""
    return db.connect().execute(sql, args).rowcount


def recorder(tid: int):
    """The topic's model calls, as model_calls rows under run_id "topic-<tid>" (the 机房's totals count them)."""
    def record(r: dict) -> None:
        u = r.get("usage") or {}
        db.exec("""INSERT INTO model_calls(run_id, stage, attempt, conversation_id, model, agy_version, status, error_class,
                   wall_seconds, input_tokens, output_tokens, thinking_tokens, peak_rss_kib, view_files, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                f"topic-{tid}", r.get("stage"), r.get("attempt"), r.get("conversation_id"), r.get("actual_model"),
                r.get("agy_version"), r.get("status"), r.get("error_class"), r.get("wall_seconds"), u.get("input_tokens"),
                u.get("output_tokens"), u.get("thinking_tokens"), r.get("peak_rss_kib"), r.get("view_file_count"), now())
    return record


# ------------------------------------------------------------------ the quota topics leave to the channels
def _fresh(iso: str | None, hours: float = 2) -> bool:
    try:
        return dt.datetime.fromisoformat(iso) > dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


def held() -> dict | None:
    """{window, reset_at, remaining, vendor, group} of a subscription the topic's steps draw on that is down to the share
    topics leave (`topic_min_weekly`, `topic_min_5h`: for the channels, and for whatever else the account is used for),
    or None (an unknown or stale reading holds nothing: a quota that runs out stops the read by itself). `vendor` is agy
    or a magpie subscription's provider; `group`, the group of agy's quota."""
    from ..providers.agy import quota_group
    from ..providers.magpie_state import vendor_of
    routes = {resolve(r) for r in knobs.topic_routes().values()}
    buckets: list[dict] = []
    q = db.setting("agy_quota") or {}
    if q.get("ok") and _fresh(q.get("checked_at")):
        for r in routes:
            if is_agy(r) and (g := quota_group(q.get("groups") or [], r[len("agy/"):])):
                buckets += [{**b, "vendor": "agy", "group": g["name"]} for b in g["buckets"]]
    st = db.setting("magpie_state") or {}
    if st.get("ok") and _fresh(st.get("read_at")):
        used = {vendor_of(r, st.get("models")) for r in routes if is_model(r)}
        buckets += [{**w, "vendor": s.get("provider")} for s in st.get("subs") or [] if s.get("provider") in used
                    for w in s.get("windows") or []]
    floor = {"weekly": knobs.get("topic_min_weekly"), "5h": knobs.get("topic_min_5h")}
    low = [b for b in buckets if not b.get("disabled") and b.get("remaining") is not None
           and b.get("window") in floor and b["remaining"] < floor[b["window"]]]
    if not low:
        return None
    b = min(low, key=lambda b: (b["window"] != "weekly", b["remaining"]))
    return {"window": b["window"], "reset_at": b.get("reset_at"), "remaining": b["remaining"], "vendor": b.get("vendor"),
            "group": b.get("group")}


def crowded() -> bool:
    """A channel's run is queued and every slot is taken: a topic's read gives its slot back at its next step (and goes
    on from there once the channels have room), so a new video never waits an hour behind a topic."""
    if db.setting("queue_paused") or not db.one("SELECT 1 FROM runs WHERE production='QUEUED'"):
        return False
    busy = db.one("""SELECT (SELECT COUNT(*) FROM runs WHERE production='RUNNING')
                            + (SELECT COUNT(*) FROM topic_videos WHERE state='RUNNING')
                            + (SELECT COUNT(*) FROM topics WHERE synth_state='RUNNING') n""")["n"]
    return busy >= knobs.get("worker_slots")


# ------------------------------------------------------------------ one read
class Job:
    """One video of a topic read for the topic's question. Watches its LLM's retries (llm.LLM `watch`)."""

    def __init__(self, tid: int, vid: str):
        self.tid, self.vid = tid, vid
        self.topic = db.one("SELECT * FROM topics WHERE id=?", tid)
        self.item = db.one("SELECT * FROM topic_videos WHERE topic_id=? AND video_id=?", tid, vid)
        self.video = db.one("SELECT * FROM videos WHERE id=?", vid)
        if not (self.topic and self.item and self.video):
            raise Stopped()
        self.rev = self.item["rev"]
        self.root = work_dir(tid, self.rev, vid)
        self.llm = LLM(recorder=recorder(tid), session=f"topic-{tid}", watch=self)

    def _where(self) -> tuple:
        return self.tid, self.vid

    # what llm.LLM tells its watch -------------------------------------------
    def retrying(self, state: dict) -> bool:
        db.exec("UPDATE topic_videos SET progress=json_set(COALESCE(progress,'{}'),'$.retry',json(?)) WHERE topic_id=? AND video_id=?",
                db.j(state), *self._where())
        return self.alive()

    def settled(self, call: str, tried: dict[str, int], rescued: bool, fallback: str | None = None) -> None:
        for kind, n in {k: n for k, n in tried.items() if n}.items():
            db.exec("INSERT INTO retries(run_id, call, kind, tries, rescued, fallback, created_at) VALUES (?,?,?,?,?,?,?)",
                    f"topic-{self.tid}", call, kind, n, int(rescued), fallback if kind == "CONTENT_SAFETY" else None, now())
        db.exec("UPDATE topic_videos SET progress=json_remove(progress,'$.retry') WHERE topic_id=? AND video_id=? AND progress IS NOT NULL",
                *self._where())

    def event(self, message: str, level: str = "info") -> None:
        db.event("topic", f"专题 {self.tid} · {self.vid}：{message}", None, level)

    # where the read stands ---------------------------------------------------
    def alive(self) -> bool:
        t = db.one("SELECT paused FROM topics WHERE id=?", self.tid)
        i = db.one("SELECT rev, state, excluded FROM topic_videos WHERE topic_id=? AND video_id=?", *self._where())
        return bool(t and not t["paused"] and i and i["rev"] == self.rev and i["state"] == "RUNNING" and not i["excluded"])

    def check(self) -> None:
        if not self.alive() or crowded():
            raise Stopped()

    def step(self, name: str) -> None:
        self.check()
        if h := held():
            raise Waiting("TOPIC_QUOTA", h["window"])
        db.exec("UPDATE topic_videos SET step=?, progress=NULL WHERE topic_id=? AND video_id=?", name, *self._where())

    def progress(self, done: int, of: int) -> None:
        db.exec("UPDATE topic_videos SET progress=json_set(COALESCE(progress,'{}'),'$.done',?,'$.of',?) WHERE topic_id=? AND video_id=?",
                done, of, *self._where())

    # the steps ----------------------------------------------------------------
    def acquire(self) -> Path:
        v = self.video
        if v["media_state"] == "CACHED" and v["media_path"] and Path(v["media_path"]).exists():
            return Path(v["media_path"])
        dest = media_dir(self.vid) / "source.mkv"
        s = db.one("SELECT media_path FROM sources WHERE id=? AND media_state='CACHED'", self.vid)
        if not dest.exists() and s and s["media_path"] and Path(s["media_path"]).exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:  # a channel has it already: one more name for the same file, outliving the channel's retention
                os.link(s["media_path"], dest)
            except OSError:
                shutil.copy2(s["media_path"], dest)
        if not dest.exists():
            if shutil.disk_usage(config.get().home).free < MIN_FREE:
                raise Waiting("DISK_LOW", f"剩余空间不足 {MIN_FREE / 1e9:.0f} GB")
            from ..pipeline.acquire import DOWNLOADS
            try:
                with DOWNLOADS:
                    youtube.download(v["url"], dest.parent)
            except youtube.AcquireError as e:
                if e.needs_human:
                    raise Waiting("YOUTUBE_AUTH", str(e))
                if e.kind in ("NETWORK", "RATE_LIMITED"):
                    raise Waiting("NETWORK", str(e))
                if e.kind in ("TOOL_OUTDATED", "YOUTUBE_REFUSED"):
                    raise Waiting("YTDLP_OUTDATED" if e.kind == "TOOL_OUTDATED" else "YOUTUBE_REFUSED", str(e))
                raise
            except subprocess.TimeoutExpired as e:
                raise Waiting("NETWORK", f"下载超时：{e}")
        pr = ffmpeg.probe(dest)
        vs = [x for x in pr["streams"] if x.get("codec_type") == "video"]
        db.exec("""UPDATE videos SET media_path=?, media_height=?, media_state='CACHED', duration=COALESCE(duration, ?)
                   WHERE id=?""", str(dest), vs[0].get("height") if vs else None, float(pr["format"]["duration"]), self.vid)
        if not v["title"]:
            try:
                info = youtube.metadata(v["url"])
                db.exec("UPDATE videos SET title=?, channel=?, info=? WHERE id=?", info.get("title"),
                        info.get("channel") or info.get("uploader"), db.j(info), self.vid)
            except youtube.AcquireError:
                pass
        self.video = db.one("SELECT * FROM videos WHERE id=?", self.vid)
        return dest

    def read(self) -> None:
        self.step("ACQUIRE")
        src = self.acquire()
        meta = {"id": self.vid, "title": self.video["title"], "channel": self.video["channel"]}
        title = self.video["title"] or self.vid
        self.step("DRAFT")
        d = Draft(self.vid, meta, src, self.llm, self.check, self.progress).make()
        db.exec("UPDATE videos SET draft=? WHERE id=?", db.j(d), self.vid)
        full, index = (draft_dir(self.vid) / "full.md").read_text(), (draft_dir(self.vid) / "index.md").read_text()
        draft, brief = W.draft_for(full, index)
        question = self.topic["question"]
        notes = self.topic["notes"]
        force = bool(self.item["force"])
        self.step("PLAN")
        plan = W.plan(self.llm, self.root, question, notes, draft, brief)
        if plan["fit"] == "none" and force:
            plan = W.plan(self.llm, self.root, question, ((notes or "") + "\n" + FORCE_NOTE).strip(), draft, brief, forced=True)
        write_json(self.root / "plan.json", plan)
        if plan["fit"] == "none" and not force:
            return self.done("none", plan["reason"])
        look = Looker(src, d["duration"], title, self.root, self.llm, self.check, self.progress)
        self.step("LOOK")
        first = look.run(plan["looks"], "looks")
        self.step("SECOND")
        first = look.second(first, "looks", "a")
        self.step("WRITE")
        notes_md, tail = W.write(self.llm, self.root, question, notes, plan["write"], first, draft)
        write_text(self.root / "notes-1.md", notes_md)
        write_json(self.root / "tail-1.json", tail)
        fit, gist = tail["fit"], tail["gist"]
        if tail["looks"]:
            self.step("LOOK2")
            again = look.run(tail["looks"], "looks2")
            self.step("SECOND2")
            again = look.second(again, "looks2", "b")
            self.step("REVISE")
            notes_md, t2 = W.revise(self.llm, self.root, question, notes, plan["write"], notes_md, again, draft)
            fit, gist = t2["fit"], t2["gist"] or gist
        write_text(self.root / "notes.md", notes_md)
        self.done(fit, gist or plan["reason"])

    # how it ends ---------------------------------------------------------------
    def done(self, fit: str, gist: str) -> None:
        n = changed("""UPDATE topic_videos SET state='DONE', step=NULL, progress=NULL, fit=?, gist=?, done_rev=rev, finished_at=?,
                       wait_reason=NULL, error_class=NULL, error=NULL WHERE topic_id=? AND video_id=? AND rev=? AND state='RUNNING'""",
                    fit, gist[:200], now(), self.tid, self.vid, self.rev)
        db.exec("UPDATE videos SET used_at=? WHERE id=?", now(), self.vid)
        if n:  # reads of this video for the topic's earlier questions are not shown any more
            for old in (root() / str(self.tid)).glob("r*"):
                if old.name != f"r{self.rev}" and (old / self.vid).exists():
                    shutil.rmtree(old / self.vid, ignore_errors=True)
            settle(self.tid)

    def release(self) -> None:
        """Stopped: back in the queue where it was (a pause), unless the topic, the video or its question moved on."""
        db.exec("""UPDATE topic_videos SET state='QUEUED', progress=NULL, attempts=0
                   WHERE topic_id=? AND video_id=? AND state='RUNNING' AND rev=?""", self.tid, self.vid, self.rev)

    def wait(self, reason: str, message: str) -> None:
        db.exec("""UPDATE topic_videos SET state='WAITING', wait_reason=?, error=?, attempts=0, progress=NULL
                   WHERE topic_id=? AND video_id=? AND state='RUNNING' AND rev=?""", reason, message[:1000], self.tid, self.vid, self.rev)

    def fail(self, e: BaseException) -> None:
        kind = getattr(e, "kind", None) or type(e).__name__
        db.exec("""UPDATE topic_videos SET state='FAILED', error_class=?, error=?, finished_at=?, progress=NULL
                   WHERE topic_id=? AND video_id=? AND state='RUNNING' AND rev=?""",
                kind, f"{e}"[:1000], now(), self.tid, self.vid, self.rev)
        db.event("topic", f"专题 {self.tid} 的 {self.vid} 没有读成：{kind}", None, "warn", {"tb": traceback.format_exc()[-1500:]})
        settle(self.tid)


def execute(tid: int, vid: str) -> None:
    try:
        job = Job(tid, vid)
    except Stopped:
        return
    try:
        job.read()
    except Stopped:
        job.release()
    except Exception as e:  # noqa: BLE001 — anything else ends this read, the topic goes on
        if not job.alive():  # paused, left out or asked anew meanwhile (a retry it cut short raises the call's error)
            job.release()
        elif isinstance(e, Waiting):
            job.wait(e.reason, str(e))
        elif isinstance(e, ProviderError) and e.kind in PROVIDER_WAITS:
            job.wait(e.kind, str(e))
        else:
            job.fail(e)


# ------------------------------------------------------------------ the answer put together
def signature(tid: int) -> str:
    """What an answer was put together from: the question's revision and each read in it."""
    t = db.one("SELECT rev FROM topics WHERE id=?", tid)
    reads = db.q("""SELECT video_id, done_rev, finished_at FROM topic_videos WHERE topic_id=? AND state='DONE' AND excluded=0
                    ORDER BY no""", tid)
    return db.j([t["rev"] if t else 0, [[r["video_id"], r["done_rev"], r["finished_at"]] for r in reads]])


def settle(tid: int) -> None:
    """A read ended: once every video of the topic is read (or failed), put the answer together if it is not yet."""
    open_ = db.one("""SELECT 1 FROM topic_videos WHERE topic_id=? AND excluded=0 AND state NOT IN ('DONE','FAILED')""", tid)
    any_done = db.one("SELECT 1 FROM topic_videos WHERE topic_id=? AND excluded=0 AND state='DONE'", tid)
    if not open_ and any_done:
        db.exec("""UPDATE topics SET synth_state='QUEUED', synth_error=NULL WHERE id=? AND (synth_of IS NOT ? OR synth IS NULL)
                   AND COALESCE(synth_state,'') NOT IN ('QUEUED','RUNNING')""", tid, signature(tid))


def put_together(tid: int) -> None:
    t = db.one("SELECT * FROM topics WHERE id=?", tid)
    if not t or changed("UPDATE topics SET synth_state='RUNNING' WHERE id=? AND synth_state='QUEUED'", tid) == 0:
        return
    sig = signature(tid)
    if t["synth"] and t["synth_of"] == sig:  # what it was put together from is what there is (a video left out and back in)
        db.exec("UPDATE topics SET synth_state='DONE' WHERE id=?", tid)
        return
    reads = db.q("""SELECT tv.*, v.title, v.channel, v.duration FROM topic_videos tv JOIN videos v ON v.id=tv.video_id
                    WHERE tv.topic_id=? AND tv.state='DONE' AND tv.excluded=0 ORDER BY tv.no""", tid)
    videos = []
    for r in reads:
        p = work_dir(tid, r["done_rev"], r["video_id"]) / "notes.md"
        videos.append({"no": r["no"], "title": r["title"] or r["video_id"], "channel": r["channel"], "fit": r["fit"] or "some",
                       "gist": r["gist"], "asset": p.read_text() if p.exists() else ""})
    if not videos:
        db.exec("UPDATE topics SET synth_state=NULL WHERE id=?", tid)
        return
    try:
        o = W.synth(LLM(recorder=recorder(tid), session=f"topic-{tid}"), synth_dir(tid), "topic-synth", t["question"], t["notes"], videos)
    except ProviderError as e:
        state = "WAITING" if e.kind in PROVIDER_WAITS else "FAILED"
        db.exec("UPDATE topics SET synth_state=?, synth_error=? WHERE id=?", state, f"{e.kind}: {e}"[:500], tid)
        return
    except Exception as e:  # noqa: BLE001 — a contract, a file, a locked database: the answer is not stuck RUNNING
        db.exec("UPDATE topics SET synth_state='FAILED', synth_error=? WHERE id=?", f"{e}"[:500], tid)
        _tell(tid, t["question"], len(videos), ok=False)
        return
    db.exec("""UPDATE topics SET synth=?, synth_of=?, synth_at=?, synth_state='DONE', synth_error=NULL, updated_at=?
               WHERE id=?""", db.j(o), sig, now(), now(), tid)
    _tell(tid, t["question"], len(videos))


def _tell(tid: int, question: str, n: int, ok: bool = True) -> None:
    """Hours of reading ended: a word on the phone, once the whole batch is in the answer (an answer asked for early,
    while videos are still being read, is on the page the user is looking at)."""
    if not db.one("SELECT 1 FROM topic_videos WHERE topic_id=? AND excluded=0 AND state NOT IN ('DONE','FAILED')", tid):
        notify.topic_answered(tid, question, n, ok)


# ------------------------------------------------------------------ the worker's part
def _start(worker: Any, key: str, fn, *args) -> None:
    def work():
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            db.event("topic", f"专题异常：{e}", None, "error", {"tb": traceback.format_exc()[-1500:]})
    th = threading.Thread(target=work, name=key, daemon=True)
    worker.active[key] = th
    th.start()


def drafting(worker: Any, vid: str) -> bool:
    """Another topic's read of this video is still downloading it or making its 底稿 (kept by video, in one folder): the
    next waits, and then takes the draft as it is instead of making it again."""
    for k in [k for k in worker.active if k.startswith("topic:") and k.endswith(f":{vid}")]:
        r = db.one("SELECT step FROM topic_videos WHERE topic_id=? AND video_id=?", int(k.split(":")[1]), vid)
        if not r or r["step"] in (None, "ACQUIRE", "DRAFT"):
            return True
    return False


def dispatch(worker: Any) -> bool:
    """Start what the worker has room for: an answer to put together, then the next videos to read. True if one started."""
    slots = knobs.get("worker_slots")
    started = False
    for t in db.q("SELECT id FROM topics WHERE synth_state='QUEUED' AND paused=0 ORDER BY id"):
        key = f"topic-synth:{t['id']}"
        if key not in worker.active and len(worker.active) < slots:
            _start(worker, key, put_together, t["id"])
            started = True
    if held():
        return started
    reading = {k for k in worker.active if k.startswith("topic:")}
    if len(worker.active) >= slots or len(reading) >= knobs.get("topic_slots"):
        return started
    for r in db.q("""SELECT tv.topic_id, tv.video_id FROM topic_videos tv JOIN topics t ON t.id=tv.topic_id
                     WHERE tv.state='QUEUED' AND tv.excluded=0 AND t.paused=0 ORDER BY t.created_at, tv.no LIMIT 50"""):
        if len(worker.active) >= slots or len(reading) >= knobs.get("topic_slots"):
            break
        key = f"topic:{r['topic_id']}:{r['video_id']}"
        if key in worker.active or drafting(worker, r["video_id"]):  # winding down (a pause, an edit); or see drafting
            continue
        if not changed("""UPDATE topic_videos SET state='RUNNING', attempts=attempts+1, started_at=COALESCE(started_at, ?),
                          wait_reason=NULL, error=NULL, error_class=NULL WHERE topic_id=? AND video_id=? AND state='QUEUED'""",
                       now(), r["topic_id"], r["video_id"]):
            continue  # the page changed it meanwhile
        _start(worker, key, execute, r["topic_id"], r["video_id"])
        reading.add(key)
        started = True
    return started


def recover() -> None:
    """The worker started: reads it was doing go back in the queue (given up after five starts in a row)."""
    given_up = {r["topic_id"] for r in db.q("SELECT topic_id FROM topic_videos WHERE state='RUNNING' AND attempts>=5")}
    db.exec("""UPDATE topic_videos SET state=CASE WHEN attempts>=5 THEN 'FAILED' ELSE 'QUEUED' END,
               error_class=CASE WHEN attempts>=5 THEN 'TOO_MANY_RESTARTS' ELSE error_class END,
               error=CASE WHEN attempts>=5 THEN '多次中断后停止自动续读' ELSE error END, progress=NULL WHERE state='RUNNING'""")
    db.exec("UPDATE topics SET synth_state='QUEUED' WHERE synth_state='RUNNING'")
    for tid in given_up:  # it may have been the last one to read
        settle(tid)


def housekeeping(t: float) -> None:
    if t - _last["retry"] > RETRY_EVERY:
        _last["retry"] = t
        db.exec("UPDATE topic_videos SET state='QUEUED' WHERE state='WAITING'")
        db.exec("UPDATE topics SET synth_state='QUEUED' WHERE synth_state='WAITING'")
    if t - _last["retention"] > 3600:
        _last["retention"] = t
        try:
            retention()
        except Exception as e:  # noqa: BLE001
            db.event("topic", f"专题清理异常：{e}", None, "warn")


def _age_days(iso: str | None) -> float:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 0.0


def retention() -> dict:
    """The originals no read needs any more after `retention_days` (a read again downloads it again; the draft stays), the
    videos no topic has after KEEP_DRAFTS_DAYS, and the folders of deleted topics."""
    days, purged, dropped = knobs.get("retention_days"), 0, 0
    for v in db.q("SELECT * FROM videos WHERE media_state='CACHED'"):
        if db.one("""SELECT 1 FROM topic_videos WHERE video_id=? AND state IN ('QUEUED','RUNNING','WAITING') AND excluded=0""", v["id"]):
            continue
        if _age_days(v["used_at"] or v["created_at"]) >= days:
            shutil.rmtree(media_dir(v["id"]), ignore_errors=True)
            db.exec("UPDATE videos SET media_state='PURGED', media_path=NULL WHERE id=?", v["id"])
            purged += 1
    for v in db.q("SELECT * FROM videos WHERE id NOT IN (SELECT video_id FROM topic_videos)"):
        if _age_days(v["used_at"] or v["created_at"]) >= KEEP_DRAFTS_DAYS:
            shutil.rmtree(media_dir(v["id"]), ignore_errors=True)
            shutil.rmtree(draft_dir(v["id"]), ignore_errors=True)
            db.exec("DELETE FROM videos WHERE id=?", v["id"])
            dropped += 1
    ids = {str(r["id"]) for r in db.q("SELECT id FROM topics")}
    if root().exists():
        for d in root().iterdir():
            if d.is_dir() and d.name.isdigit() and d.name not in ids:
                shutil.rmtree(d, ignore_errors=True)
    return {"purged": purged, "dropped": dropped}
