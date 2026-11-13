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
import re
import shutil
import subprocess
import threading
import traceback
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import config, db, knobs, notify, youtube
from ..llm import LLM, ContractError
from ..media import ffmpeg
from ..pipeline.context import Waiting
from ..providers import is_agy, is_model, resolve
from ..providers.agy import ProviderError
from ..util import now, read_json, write_json, write_text
from . import Stopped, draft_dir, media_dir, root, synth_dir, work_dir
from . import backup, comments
from . import backup_rules
from . import write as W
from .draft import Draft
from .look import Looker

STEPS = ("ACQUIRE", "DRAFT", "PLAN", "LOOK", "SECOND", "WRITE", "LOOK2", "SECOND2", "REVISE")
TERMINAL = ("DONE", "FAILED")
PROVIDER_WAITS = {"PROVIDER_QUOTA", "AGY_AUTH_REQUIRED", "PROVIDER_MODEL"}
RETRY_EVERY = 1800        # seconds: waits are tried again this often
KEEP_DRAFTS_DAYS = 30     # a video no topic has keeps its draft this long, for the next topic that wants it
BACKUP_GRACE_DAYS = 7    # 只等网盘最多七天；磁盘低水位照常清理（design §R8-8）
MIN_FREE = 15e9
FORCE_NOTE = "（之前判断这期和问题无关，用户仍要读这期：按 some 处理，写出回看清单和写作提示词。）"
REMIND_EVERY = 3600       # seconds: predictions past their day are looked for this often (told once each)
LONG_WAIT = 12 * 3600     # seconds: a video waiting this long no longer holds its topic's answer back (design §R8-5)
KEEP_REMOVED_DAYS = 7     # a video taken out of a topic can be put back this long, with its number and notes (design §R8-8)
WAIT_WORDS = {"YOUTUBE_AUTH": "YouTube 需登录", "YOUTUBE_REFUSED": "YouTube 拒绝下载", "YTDLP_OUTDATED": "下载工具需更新",
              "NETWORK": "网络中断", "DISK_LOW": "磁盘不足", "TOPIC_QUOTA": "等额度", "PROVIDER_QUOTA": "模型配额",
              "AGY_AUTH_REQUIRED": "模型登录失效", "PROVIDER_MODEL": "工序的模型已取消"}   # web/src/util.ts WAIT
WATCH_LOOK_EVERY = 600    # seconds: topics whose channels are due a look (topics/watch.EVERY) are looked for this often
_last = {"retry": 0.0, "retention": 0.0, "remind": 0.0, "watch": 0.0, "comments": 0.0, "backup": 0.0}
_watching: dict[str, threading.Thread] = {}
TZ = ZoneInfo("Asia/Shanghai")


def changed(sql: str, *args) -> int:
    """Rows an UPDATE changed (db.exec answers with the connection's last inserted row id when there is one)."""
    return db.connect().execute(sql, args).rowcount


def recorder(tid: int):
    """The topic's calls keep their own statistics, outside production runs."""
    from ..llm import call_recorder
    return call_recorder(f"topic-{tid}")


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
    on from there once the channels have room). A step keeps it while its call is retried: with two retries about 16
    minutes of a hung agy call's silence (91 for a 底稿's picture pass off Flash, agy.quiet_for), up to two hours for one
    that keeps writing and never ends (agy's wall limit)."""
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
        t = db.one("SELECT paused, archived_at FROM topics WHERE id=?", self.tid)
        i = db.one("SELECT rev, state, excluded FROM topic_videos WHERE topic_id=? AND video_id=?", *self._where())
        return bool(t and not t["paused"] and not t["archived_at"] and i and i["rev"] == self.rev and i["state"] == "RUNNING"
                    and not i["excluded"])

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
    def acquire(self) -> Path | None:
        """The original, downloaded if it is not here; None when YouTube no longer has it and its draft is kept."""
        v = self.video
        if v["media_state"] == "CACHED" and v["media_path"] and Path(v["media_path"]).exists():
            return Path(v["media_path"])
        dest = media_dir(self.vid) / "source.mkv"
        from_backup = bool((db.one("SELECT restored_at FROM video_backups WHERE id=?", self.vid) or {}).get("restored_at") and dest.exists())
        s = db.one("SELECT media_path FROM sources WHERE id=? AND media_state='CACHED'", self.vid)
        if not dest.exists() and s and s["media_path"] and Path(s["media_path"]).exists():
            from_backup = False
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
                if e.kind in youtube.UNAVAILABLE:
                    with db.tx():
                        db.exec("UPDATE videos SET gone=?,gone_at=COALESCE(gone_at,?) WHERE id=?", e.kind, now(), self.vid)
                        backup_rules.track(self.vid, gone=e.kind)
                rescue = e.kind in youtube.UNAVAILABLE or e.needs_human or e.kind in ("TOOL_OUTDATED", "YOUTUBE_REFUSED")
                restored = backup.restore_source(self.vid) if rescue and backup.available() else None
                if restored:
                    from_backup = True
                elif e.kind in youtube.UNAVAILABLE:
                    if not (draft_dir(self.vid) / "full.md").exists():
                        backup.restore_draft(self.vid)
                    if (draft_dir(self.vid) / "full.md").exists():
                        return None  # 两处都没有原片时才只照底稿读（design §R8-8）
                    raise
                elif e.needs_human:
                    raise Waiting("YOUTUBE_AUTH", str(e))
                elif e.kind in ("NETWORK", "RATE_LIMITED"):
                    raise Waiting("NETWORK", str(e))
                elif e.kind in ("TOOL_OUTDATED", "YOUTUBE_REFUSED"):
                    raise Waiting("YTDLP_OUTDATED" if e.kind == "TOOL_OUTDATED" else "YOUTUBE_REFUSED", str(e))
                else:
                    raise
            except subprocess.TimeoutExpired as e:
                raise Waiting("NETWORK", f"下载超时：{e}")
        pr = ffmpeg.probe(dest)
        vs = [x for x in pr["streams"] if x.get("codec_type") == "video"]
        with db.tx():
            db.exec("""UPDATE videos SET media_path=?, media_height=?, media_state='CACHED', duration=COALESCE(duration, ?),
                       gone=CASE WHEN ? THEN gone ELSE NULL END, gone_at=CASE WHEN ? THEN gone_at ELSE NULL END WHERE id=?""",
                    str(dest), vs[0].get("height") if vs else None, float(pr["format"]["duration"]), from_backup, from_backup, self.vid)
            self.video = db.one("SELECT * FROM videos WHERE id=?", self.vid)
            backup_rules.track(self.vid)
        return dest

    def describe(self) -> None:
        """Title, channel and the day it came out, when the picker or the extension did not have them: the notes read a
        「明年」 against that day (design §R8-11)."""
        v = self.video
        if v["title"] and v["channel"] and v["published_at"] and v["youtube_channel_id"] and v["info"]:
            return
        try:
            info = youtube.metadata(v["url"])
        except (youtube.AcquireError, subprocess.TimeoutExpired, OSError):
            return
        with db.tx():
            db.exec("""UPDATE videos SET title=COALESCE(title, ?), channel=COALESCE(channel, ?), published_at=COALESCE(published_at, ?),
                       info=COALESCE(info, ?), youtube_channel_id=COALESCE(youtube_channel_id, ?) WHERE id=?""", info.get("title"), info.get("channel") or info.get("uploader"),
                    info.get("upload_date"), db.j(info), info.get("channel_id"), self.vid)
            self.video = db.one("SELECT * FROM videos WHERE id=?", self.vid)
            backup_rules.track(self.vid)

    def read(self) -> None:
        self.step("ACQUIRE")
        src = self.acquire()
        self.describe()
        meta = {"id": self.vid, "title": self.video["title"], "channel": self.video["channel"]}
        title = self.video["title"] or self.vid
        self.step("DRAFT")
        if not (draft_dir(self.vid) / "full.md").exists():
            backup.restore_draft(self.vid)
        d = Draft(self.vid, meta, src, self.llm, self.check, self.progress).make()  # src None: the draft as kept
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
        first: list[dict] = []
        if src:  # with no original (gone from YouTube) nothing can be looked at again: the notes go by the draft
            look = Looker(src, d["duration"], title, self.root, self.llm, self.check, self.progress)
            self.step("LOOK")
            first = look.run(plan["looks"], "looks")
            self.step("SECOND")
            first = look.second(first, "looks", "a")
        self.step("WRITE")
        published = self.video["published_at"]
        notes_md, tail = W.write(self.llm, self.root, question, notes, plan["write"], first, draft, published, gone=not src)
        write_text(self.root / "notes-1.md", notes_md)
        write_json(self.root / "tail-1.json", tail)
        fit, gist, card = tail["fit"], tail["gist"], tail["card"]
        if tail["looks"] and src:
            self.step("LOOK2")
            again = look.run(tail["looks"], "looks2")
            self.step("SECOND2")
            again = look.second(again, "looks2", "b")
            self.step("REVISE")
            notes_md, t2 = W.revise(self.llm, self.root, question, notes, plan["write"], notes_md, again, draft, published)
            fit, gist, card = t2["fit"], t2["gist"] or gist, t2.get("card") or card
        write_text(self.root / "notes.md", notes_md)
        self.done(fit, gist or plan["reason"], card)

    # how it ends ---------------------------------------------------------------
    def done(self, fit: str, gist: str, card: dict | None = None) -> None:
        n = changed("""UPDATE topic_videos SET state='DONE', step=NULL, progress=NULL, fit=?, gist=?, card=?, done_rev=rev,
                       finished_at=?, wait_reason=NULL, wait_since=NULL, error_class=NULL, error=NULL
                       WHERE topic_id=? AND video_id=? AND rev=? AND state='RUNNING'""",
                    fit, gist[:200], db.j(card) if card and any(card.values()) else None, now(), self.tid, self.vid, self.rev)
        db.exec("UPDATE videos SET used_at=? WHERE id=?", now(), self.vid)
        if n and (self.root / "notes.md").exists():
            keep_shots(self.vid, (self.root / "notes.md").read_text())
        if n:  # reads of this video for the topic's earlier questions are not shown any more
            backup_rules.queue(self.vid, force=True)
            for old in (root() / str(self.tid)).glob("r*"):
                if old.name != f"r{self.rev}" and (old / self.vid).exists():
                    shutil.rmtree(old / self.vid, ignore_errors=True)
            settle(self.tid)

    def release(self) -> None:
        """Stopped: back in the queue where it was (a pause), unless the topic, the video or its question moved on."""
        db.exec("""UPDATE topic_videos SET state='QUEUED', progress=NULL, attempts=0
                   WHERE topic_id=? AND video_id=? AND state='RUNNING' AND rev=?""", self.tid, self.vid, self.rev)

    def wait(self, reason: str, message: str) -> None:
        """Waiting: tried again every RETRY_EVERY; from its first wait on, LONG_WAIT later the answer no longer waits for
        it (settle)."""
        db.exec("""UPDATE topic_videos SET state='WAITING', wait_reason=?, error=?, attempts=0, progress=NULL,
                   wait_since=COALESCE(wait_since, ?) WHERE topic_id=? AND video_id=? AND state='RUNNING' AND rev=?""",
                reason, message[:1000], now(), self.tid, self.vid, self.rev)
        settle(self.tid)

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


FRAME_CITE = re.compile(r"((?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?)(?:\s*[-–—~至到]\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?)?\s*(?:帧|画面)")


def keep_shots(vid: str, notes: str) -> int:
    """The frames the notes cite ([12:34.5 帧], [02:54 画面]) taken from the original while it is here, where the page
    shows them from (web/topics frame): they outlive the original's retention and the video leaving YouTube (design §R8-8)."""
    v = db.one("SELECT media_path, media_state, duration FROM videos WHERE id=?", vid)
    if not v or v["media_state"] != "CACHED" or not v["media_path"] or not Path(v["media_path"]).exists():
        return 0
    from .draft import grab, secs, shot
    n = 0
    for m in re.finditer(r"\[([^\[\]\n]{1,120})\]", notes):
        for at in FRAME_CITE.findall(m[1]):
            t, out = shot(vid, secs(at), v["duration"])
            if not out.exists():
                try:
                    grab(Path(v["media_path"]), t, out)
                    n += 1
                except Exception:  # noqa: BLE001 — ffmpeg could not take it: the page says so when it is asked for
                    pass
    return n


# ------------------------------------------------------------------ the answer put together
def signature(tid: int) -> str:
    """What an answer was put together from: the question's revision and each read in it."""
    t = db.one("SELECT rev FROM topics WHERE id=?", tid)
    reads = db.q("""SELECT video_id, done_rev, finished_at FROM topic_videos WHERE topic_id=? AND state='DONE' AND excluded=0
                    ORDER BY no""", tid)
    return db.j([t["rev"] if t else 0, [[r["video_id"], r["done_rev"], r["finished_at"]] for r in reads]])


def _long_ago() -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=LONG_WAIT)).isoformat(timespec="seconds")


def _pending(tid: int) -> bool:
    """A video of the topic is still to be read: queued, being read, or waiting less than LONG_WAIT since it first had to."""
    return bool(db.one("""SELECT 1 FROM topic_videos WHERE topic_id=? AND excluded=0 AND state NOT IN ('DONE','FAILED')
                          AND (wait_since IS NULL OR wait_since > ?)""", tid, _long_ago()))


def settle(tid: int) -> None:
    """A read ended, or the batch changed: once every video of the topic is read (or failed, or has waited LONG_WAIT),
    put the answer together if there is none yet or a read it does not have. A video left out (or taken back) does not
    by itself: the page says the answer still cites it, and it is put together again when asked (design §R8-5). An
    archived topic is left as it is (design §R8-11)."""
    if _pending(tid):
        return
    t = db.one("SELECT synth, synth_of, archived_at FROM topics WHERE id=?", tid)
    reads = {tuple(x) for x in db.unj(signature(tid))[1]}
    if not t or t["archived_at"] or not reads:
        return
    if t["synth"] is None or reads - {tuple(x) for x in (db.unj(t["synth_of"], None) or [0, []])[1]}:
        db.exec("""UPDATE topics SET synth_state='QUEUED', synth_error=NULL WHERE id=?
                   AND COALESCE(synth_state,'') NOT IN ('QUEUED','RUNNING')""", tid)


def put_together(tid: int) -> None:
    t = db.one("SELECT * FROM topics WHERE id=?", tid)
    if not t or changed("UPDATE topics SET synth_state='RUNNING' WHERE id=? AND synth_state='QUEUED'", tid) == 0:
        return
    if t["notion_page_id"]:  # what the user commented in Notion since: the newest goes in (design §R8-13)
        try:
            comments.scan(tid)
        except Exception as e:  # noqa: BLE001 — Notion not there: the answer goes on with what was read before
            db.event("topic", f"专题 {tid} 读 Notion 批注异常：{e}"[:300], None, "warn")
    sig = signature(tid)
    unseen = any(not m.get("seen") for m in (db.unj(t["marks"], {}) or {}).values()) or comments.fresh(tid) > 0
    if t["synth"] and t["synth_of"] == sig and not unseen:  # made of what there is (a video left out and back in)
        db.exec("UPDATE topics SET synth_state='DONE' WHERE id=?", tid)
        return
    reads = db.q("""SELECT tv.*, v.title, v.channel, v.duration, v.published_at FROM topic_videos tv JOIN videos v ON v.id=tv.video_id
                    WHERE tv.topic_id=? AND tv.state='DONE' AND tv.excluded=0 ORDER BY tv.no""", tid)
    videos = []
    for r in reads:
        p = work_dir(tid, r["done_rev"], r["video_id"]) / "notes.md"
        videos.append({"no": r["no"], "title": r["title"] or r["video_id"], "channel": r["channel"], "fit": r["fit"] or "some",
                       "published": r["published_at"], "gist": r["gist"], "card": db.unj(r["card"], {}) or {},
                       "asset": p.read_text() if p.exists() else ""})
    if not videos:
        db.exec("UPDATE topics SET synth_state=NULL WHERE id=?", tid)
        return
    prev, marks = db.unj(t["synth"], None), db.unj(t["marks"], {}) or {}
    told = comments.threads(tid)
    try:
        o = W.synth(LLM(recorder=recorder(tid), session=f"topic-{tid}"), synth_dir(tid), "topic-synth", t["question"], t["notes"],
                    videos, prev=prev, marks=marks, today=dt.datetime.now(TZ).date().isoformat(), prev_at=t["synth_at"],
                    comments=told)
    except ProviderError as e:
        state = "WAITING" if e.kind in PROVIDER_WAITS else "FAILED"
        db.exec("UPDATE topics SET synth_state=?, synth_error=? WHERE id=?", state, f"{e.kind}: {e}"[:500], tid)
        return
    except Exception as e:  # noqa: BLE001 — a contract, a file, a locked database: the answer is not stuck RUNNING
        db.exec("UPDATE topics SET synth_state='FAILED', synth_error=? WHERE id=?", f"{e}"[:500], tid)
        _tell(tid, t["question"], len(videos), ok=False)
        return
    replies = o.pop("replies", None) or []  # into their threads in Notion once this answer is on the page
    at = now()
    db.exec("""UPDATE topics SET synth=?, synth_of=?, synth_at=?, synth_state='DONE', synth_error=NULL, updated_at=?
               WHERE id=?""", db.j(o), sig, at, at, tid)
    comments.taken(tid, told, keep_version(tid, o, at, len(videos), marks), replies)
    wrote = None
    if _whole(tid) and db.one("SELECT 1 FROM topics WHERE id=? AND notion_target IS NOT NULL", tid):
        from .notion import write  # the whole batch's answer goes to its Notion place (one put together early does not)
        wrote = write(tid, auto=True)
    level = lambda x: ((x or {}).get("certainty") or {}).get("level")  # noqa: E731
    if not prev or o.get("answer_changed", True) or level(prev) != level(o):  # the same conclusion, as sure: no word (design §R8-11)
        _tell(tid, t["question"], len(videos), notion=wrote, changes=o.get("changes") if prev else None)


def keep_version(tid: int, o: dict, at: str, read: int, marks: dict) -> int:
    """This answer as the topic's next version (the 版本记录, and what the next one is told); the marks it was told are
    in it from now on (`seen`), so the page stops saying the answer still has to take them in."""
    n = (db.one("SELECT MAX(n) n FROM topic_versions WHERE topic_id=?", tid)["n"] or 0) + 1
    total = db.one("SELECT COUNT(*) n FROM topic_videos WHERE topic_id=? AND excluded=0", tid)["n"]
    db.exec("INSERT INTO topic_versions(topic_id, n, at, read, total, synth) VALUES (?,?,?,?,?,?)", tid, n, at, read, total, db.j(o))
    if marks:
        with db.tx() as c:  # a mark made while this was being put together is not in it
            now_ = db.unj(c.execute("SELECT marks FROM topics WHERE id=?", (tid,)).fetchone()["marks"], {}) or {}
            for cid, m in now_.items():
                if marks.get(cid, {}).get("at") == m.get("at"):
                    m["seen"] = n
            c.execute("UPDATE topics SET marks=? WHERE id=?", (db.j(now_), tid))
    return n


def _whole(tid: int) -> bool:
    """The batch is read, but for videos that have waited LONG_WAIT (design §R8-5)."""
    return not _pending(tid)


def _waiting(tid: int) -> list[str]:
    """The videos the answer went on without, having waited LONG_WAIT, and why: V7 还在等：YouTube 需登录."""
    return [f"V{r['no']} 还在等" + (f"：{WAIT_WORDS.get(r['wait_reason'], r['wait_reason'])}" if r["wait_reason"] else "")
            for r in db.q("""SELECT no, wait_reason FROM topic_videos WHERE topic_id=? AND excluded=0
                             AND state NOT IN ('DONE','FAILED') ORDER BY no""", tid)]


def _tell(tid: int, question: str, n: int, ok: bool = True, notion: dict | None = None, changes: list[str] | None = None) -> None:
    """Hours of reading ended: a word on the phone, once the whole batch is in the answer (an answer asked for early,
    while videos are still being read, is on the page the user is looking at), after it is written to Notion.
    `changes`: an answer put together again, and what changed in it."""
    if _whole(tid):
        notify.topic_answered(tid, question, n, ok, notion, changes, waiting=_waiting(tid))


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
    """Start what the worker has room for: an answer to put together, then the next videos to read. True if one started.
    A write to Notion asked for on the topic's page takes no slot (a few requests)."""
    from .notion import due, write
    slots = knobs.get("worker_slots")
    started = False
    for tid in due():
        key = f"topic-notion:{tid}"
        if key not in worker.active and f"topic-synth:{tid}" not in worker.active:
            _start(worker, key, write, tid)
            started = True
    for t in db.q("SELECT id FROM topics WHERE synth_state='QUEUED' AND paused=0 AND archived_at IS NULL ORDER BY id"):
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
                     WHERE tv.state='QUEUED' AND tv.excluded=0 AND t.paused=0 AND t.archived_at IS NULL
                     ORDER BY t.created_at, tv.no LIMIT 50"""):
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
    if db.setting("gdrive_upload"):
        db.set_setting("gdrive_upload", None)  # worker 重启，之前的备份线程也已退出
    backup.reap()  # 上次被杀时没删掉的网盘配置副本（断开之后不会再有网盘调用来清它）
    given_up = {r["topic_id"] for r in db.q("SELECT topic_id FROM topic_videos WHERE state='RUNNING' AND attempts>=5")}
    db.exec("""UPDATE topic_videos SET state=CASE WHEN attempts>=5 THEN 'FAILED' ELSE 'QUEUED' END,
               error_class=CASE WHEN attempts>=5 THEN 'TOO_MANY_RESTARTS' ELSE error_class END,
               error=CASE WHEN attempts>=5 THEN '多次中断后停止自动续读' ELSE error END, progress=NULL WHERE state='RUNNING'""")
    db.exec("UPDATE topics SET synth_state='QUEUED' WHERE synth_state='RUNNING'")
    db.exec("UPDATE topics SET notion_state='QUEUED' WHERE notion_state='RUNNING'")
    for tid in given_up:  # it may have been the last one to read
        settle(tid)


def remind(today: str | None = None) -> int:
    """Predictions past their day and not settled: a word on the phone for a topic's, once for each, to settle them on
    its page (design §R8-11). An archived topic's are not told. Returns how many topics were told."""
    today = today or dt.datetime.now(TZ).date().isoformat()
    n = 0
    for t in db.q("SELECT id, question, synth, marks, reminded FROM topics WHERE synth IS NOT NULL AND archived_at IS NULL"):
        s = W.apply_marks(db.unj(t["synth"], {}) or {}, db.unj(t["marks"], {}))
        told = set(db.unj(t["reminded"], []) or [])
        due = sorted((c for c in s.get("claims") or [] if c.get("type") == "预测" and c.get("status") == "现行"
                      and c.get("due") and c["due"] < today and c.get("id") not in told), key=lambda c: c["due"])
        if due:
            notify.predictions_due(t["id"], t["question"], due)
            db.exec("UPDATE topics SET reminded=? WHERE id=?", db.j(sorted(told | {c["id"] for c in due})), t["id"])
            n += 1
    return n


def housekeeping(t: float) -> None:
    if t - _last.get("backup", 0.0) > 60 and backup.available() and not (_watching.get("b") and _watching["b"].is_alive()):
        _last["backup"] = t
        _watching["b"] = threading.Thread(target=backup.due, name="topic-backup", daemon=True)
        _watching["b"].start()
    if t - _last["remind"] > REMIND_EVERY:
        _last["remind"] = t
        try:
            remind()
        except Exception as e:  # noqa: BLE001
            db.event("topic", f"预测到期提醒异常：{e}", None, "warn")
    if t - _last["watch"] > WATCH_LOOK_EVERY and not (_watching.get("t") and _watching["t"].is_alive()):
        _last["watch"] = t  # a look waits on YouTube: not in the worker's loop
        from . import watch
        _watching["t"] = threading.Thread(target=watch.due, name="topic-watch", daemon=True)
        _watching["t"].start()
    from ..notion import hook
    if hook.pending() and not (_watching.get("h") and _watching["h"].is_alive()):
        _watching["h"] = threading.Thread(target=hook.process, name="topic-hook", daemon=True)
        _watching["h"].start()
    from .. import issue_asks
    if not (_watching.get("i") and _watching["i"].is_alive()) and issue_asks.pending():
        _watching["i"] = threading.Thread(target=issue_asks.due, name="issue-asks", daemon=True)
        _watching["i"].start()
    if t - _last["comments"] > WATCH_LOOK_EVERY and not (_watching.get("c") and _watching["c"].is_alive()):
        _last["comments"] = t  # the comments on topics' Notion pages (comments.EVERY): many requests, not in the worker's loop
        _watching["c"] = threading.Thread(target=comments.due, name="topic-comments", daemon=True)
        _watching["c"].start()
    if t - _last["retry"] > RETRY_EVERY:
        _last["retry"] = t
        db.exec("UPDATE topic_videos SET state='QUEUED' WHERE state='WAITING'")
        db.exec("UPDATE topics SET synth_state='QUEUED' WHERE synth_state='WAITING'")
        for r in db.q("SELECT DISTINCT topic_id FROM topic_videos WHERE wait_since <= ? AND state NOT IN ('DONE','FAILED')",
                      _long_ago()):
            settle(r["topic_id"])  # waited half a day: the answer goes on without it (design §R8-5)
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
    """The originals no read needs any more after `retention_days` (a read again downloads it again; the draft and the
    frames the notes cite stay), videos taken out of a topic after KEEP_REMOVED_DAYS (their reads), the videos no topic
    has after KEEP_DRAFTS_DAYS, and the folders of deleted topics."""
    days, purged, dropped = knobs.get("retention_days"), 0, 0
    from .backup_retention import plan, due, mark_missed
    enabled = backup.available()
    low = enabled and shutil.disk_usage(config.get().home).free < MIN_FREE
    for s in db.q("SELECT topic_id, video_id, removed_at FROM topic_removed"):
        if _age_days(s["removed_at"]) >= KEEP_REMOVED_DAYS:
            for d in (root() / str(s["topic_id"])).glob(f"r*/{s['video_id']}"):
                shutil.rmtree(d, ignore_errors=True)
            db.exec("DELETE FROM topic_removed WHERE topic_id=? AND video_id=?", s["topic_id"], s["video_id"])
    for candidate in db.q("SELECT * FROM videos WHERE media_state='CACHED'"):
        if not due(plan(candidate["id"], low=low), "topic_media"):
            continue
        with backup.media_lock, backup.retention_claim(candidate["id"]) as free:
            if not free:
                continue
            with backup_rules.cleanup(candidate["id"]) as locked:
                if not locked:
                    continue
                with db.tx():
                    v = db.one("SELECT * FROM videos WHERE id=?", candidate["id"])
                    if not v or v["media_state"] != "CACHED":
                        continue
                    p = plan(v["id"], low=low)
                    if due(p, "topic_media"):
                        mark_missed(v["id"], p)
                        if enabled:
                            backup_rules.track(v["id"])
                        shutil.rmtree(media_dir(v["id"]), ignore_errors=True)
                        db.exec("UPDATE videos SET media_state='PURGED', media_path=NULL WHERE id=?", v["id"])
                        purged += 1
    for candidate in db.q("""SELECT * FROM videos WHERE id NOT IN (SELECT video_id FROM topic_videos)
                             AND id NOT IN (SELECT video_id FROM topic_removed)"""):
        if not due(plan(candidate["id"], low=low), "topic_drop"):
            continue
        with backup.media_lock, backup.retention_claim(candidate["id"]) as free:
            if not free:
                continue
            with backup_rules.cleanup(candidate["id"]) as locked:
                if not locked:
                    continue
                with db.tx():
                    v = db.one("""SELECT * FROM videos WHERE id=? AND id NOT IN (SELECT video_id FROM topic_videos)
                                  AND id NOT IN (SELECT video_id FROM topic_removed)""", candidate["id"])
                    if not v:
                        continue
                    p = plan(v["id"], low=low)
                    if due(p, "topic_drop"):
                        if v["media_state"] == "CACHED":
                            mark_missed(v["id"], p)
                        if enabled:
                            backup_rules.track(v["id"])
                        shutil.rmtree(media_dir(v["id"]), ignore_errors=True)
                        shutil.rmtree(draft_dir(v["id"]), ignore_errors=True)
                        db.exec("DELETE FROM videos WHERE id=?", v["id"])
                        dropped += 1
    ids = {str(r["id"]) for r in db.q("SELECT id FROM topics")}
    if root().exists():
        for d in root().iterdir():
            if d.is_dir() and d.name.isdigit() and d.name not in ids:
                shutil.rmtree(d, ignore_errors=True)
    backup.pending_alert()
    return {"purged": purged, "dropped": dropped}
