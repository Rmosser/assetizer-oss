"""频道调试台 (design §R2-1): samples, trial runs per playbook version, version comparison, 定稿.

A channel in TUNING picks 2–3 sample videos and runs them with a draft playbook; the
user compares versions in the 对照视图, edits the prompt, reruns, and locks the channel.
Trial runs exist only while the channel is in TUNING: their results never reach Notion
until 定稿 (delivery waits on the channel stage), so a draft cannot overwrite the library.
"""
from __future__ import annotations

from . import db
from .util import now

ACTIVE_RUN = ("QUEUED", "RUNNING", "WAITING", "INTERRUPTED")


def draft_playbook(channel_id: str) -> dict | None:
    """The version a trial uses by default: the newest draft, else the active one."""
    return (db.one("SELECT * FROM playbooks WHERE channel_id=? AND status='NEEDS_CONFIRMATION' ORDER BY version DESC LIMIT 1", channel_id)
            or db.one("SELECT * FROM playbooks WHERE channel_id=? AND status='ACTIVE' ORDER BY version DESC LIMIT 1", channel_id))


def add_sample(channel_id: str, url: str) -> str:
    from . import youtube
    vid = youtube.video_id(url)
    if not vid:
        raise ValueError("无法识别的 YouTube 链接")
    if not db.one("SELECT id FROM sources WHERE id=?", vid):
        db.exec("INSERT INTO sources(id, url, channel_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                vid, f"https://www.youtube.com/watch?v={vid}", channel_id, now(), now())
    else:
        db.exec("UPDATE sources SET channel_id=COALESCE(channel_id, ?) WHERE id=?", channel_id, vid)
    db.exec("INSERT OR IGNORE INTO samples(channel_id, source_id, added_at) VALUES (?,?,?)", channel_id, vid, now())
    return vid


def trial(channel_id: str, playbook_ids: int | list[int], source_ids: list[str] | None = None) -> list[dict]:
    """Queue trial runs: every listed sample × every listed version, video by video (all versions of one
    sample before the next), so the user can compare a sample as soon as its versions are done.
    The queue is worked through one run at a time by the worker (dispatch_next)."""
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    if not ch or ch["stage"] != "TUNING":
        raise ValueError("只有调试中的频道可以试跑；已定稿的频道请先「回到调试期」")
    pids = [playbook_ids] if isinstance(playbook_ids, int) else list(playbook_ids)
    pbs = {p["id"]: p for p in db.q(f"SELECT id, version FROM playbooks WHERE channel_id=? AND id IN ({','.join('?' * len(pids))})",
                                    channel_id, *pids)} if pids else {}
    if not pids or len(pbs) != len(set(pids)):
        raise ValueError("规程版本不属于这个频道")
    ids = source_ids or [r["source_id"] for r in db.q("SELECT source_id FROM samples WHERE channel_id=? ORDER BY added_at", channel_id)]
    out = []
    for sid in ids:
        if not db.one("SELECT 1 FROM samples WHERE channel_id=? AND source_id=?", channel_id, sid):
            continue
        for pid in pids:
            dup = db.one("""SELECT 1 FROM trial_queue q LEFT JOIN runs r ON r.id=q.run_id WHERE q.source_id=? AND q.playbook_id=?
                            AND (q.status='PENDING' OR (q.status='DISPATCHED' AND r.production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')))""",
                         sid, pid)
            if dup:
                out.append({"source_id": sid, "playbook_id": pid, "skipped": "已在排队"})
                continue
            db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, created_at) VALUES (?,?,?,?)", channel_id, sid, pid, now())
            out.append({"source_id": sid, "playbook_id": pid, "queued": True})
    n = sum(1 for x in out if x.get("queued"))
    if n:
        db.event("tuning", f"{ch['name']} 排入 {n} 次试跑（规程 {'、'.join(f'v{pbs[p]['version']}' for p in pids)}）")
    dispatch_next()
    return out


def dispatch_next() -> str | None:
    """Submit the next queued trial once nothing else is waiting to run (strict queue order)."""
    from .pipeline.runner import submit
    if db.one("SELECT 1 FROM runs WHERE production IN ('QUEUED','RUNNING')"):
        return None
    # a login or disk problem stops every video alike: wait for it instead of sending the whole queue into it
    if db.one("SELECT 1 FROM runs WHERE production='WAITING' AND wait_reason IN ('YOUTUBE_AUTH','DISK_LOW')"):
        return None
    for e in db.q("SELECT * FROM trial_queue WHERE status='PENDING' ORDER BY id"):
        ch = db.one("SELECT stage FROM channels WHERE id=?", e["channel_id"])
        if not ch or ch["stage"] != "TUNING":
            db.exec("UPDATE trial_queue SET status='CANCELLED' WHERE id=?", e["id"])
            continue
        if db.one(f"SELECT 1 FROM runs WHERE source_id=? AND production IN {ACTIVE_RUN}", e["source_id"]):
            continue  # this video is waiting on something of its own (e.g. a live not yet over): the next one goes first
        r = submit(f"https://www.youtube.com/watch?v={e['source_id']}", playbook_id=e["playbook_id"])
        db.exec("UPDATE trial_queue SET status='DISPATCHED', run_id=?, dispatched_at=? WHERE id=?", r["run_id"], now(), e["id"])
        return r["run_id"]
    return None


def clear_queue(channel_id: str) -> int:
    # rowcount, not db.exec(): that returns the connection's last insert id for an UPDATE too
    return db.connect().execute("UPDATE trial_queue SET status='CANCELLED' WHERE channel_id=? AND status='PENDING'", (channel_id,)).rowcount


def vote(run_id: str, liked: bool, note: str | None) -> dict:
    r = db.one("SELECT r.id, r.source_id, s.channel_id FROM runs r JOIN sources s ON s.id=r.source_id WHERE r.id=?", run_id)
    if not r or not r["channel_id"]:
        raise ValueError("找不到这个版本")
    db.exec("""INSERT INTO run_votes(run_id, channel_id, source_id, liked, note, updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET liked=excluded.liked, note=excluded.note, updated_at=excluded.updated_at""",
            run_id, r["channel_id"], r["source_id"], int(bool(liked)), (note or "").strip() or None, now())
    return {"run_id": run_id, "liked": bool(liked), "note": (note or "").strip() or None}


def run_stats(run: dict) -> dict:
    summ = db.unj(run.get("summary"), {}) or {}
    pb = db.one("SELECT version, status FROM playbooks WHERE id=?", run["playbook_id"]) if run.get("playbook_id") else None
    stages = db.q("SELECT status FROM stages WHERE run_id=?", run["id"])
    done = sum(1 for s in stages if s["status"] in ("DONE", "SKIPPED", "WARN"))
    chars = db.one("SELECT COALESCE(SUM(LENGTH(markdown)), 0) n FROM sections WHERE run_id=?", run["id"])["n"]
    return {
        "id": run["id"], "production": run["production"], "stage": run["stage"], "wait_reason": run.get("wait_reason"),
        "error_class": run.get("error_class"), "created_at": run["created_at"], "finished_at": run.get("finished_at"),
        "playbook_id": run.get("playbook_id"), "version": pb and pb["version"], "playbook_status": pb and pb["status"],
        "progress": [done, len(stages)],
        "units": summ.get("units") if summ else db.one("SELECT COUNT(*) n FROM units WHERE run_id=?", run["id"])["n"],
        "must": db.one("SELECT COUNT(*) n FROM units WHERE run_id=? AND learning_priority=3", run["id"])["n"],
        "clips": sum(v for k, v in (summ.get("assets") or {}).items() if k == "clip:GENERATED"),
        "notes": db.one("SELECT COUNT(*) n FROM flags WHERE run_id=? AND severity!='INFO'", run["id"])["n"],
        "chars": chars,
        **({"liked": bool(v["liked"]), "note": v["note"]} if (v := db.one("SELECT liked, note FROM run_votes WHERE run_id=?", run["id"]))
           else {"liked": False, "note": None}),
    }


def workbench(channel_id: str) -> dict:
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    pbs = db.q("""SELECT p.id, p.version, p.status, p.origin, p.created_at, p.activated_at, cp.version prompt_version, cp.note
                  FROM playbooks p LEFT JOIN channel_prompts cp ON cp.id=p.prompt_id WHERE p.channel_id=? ORDER BY p.version DESC""", channel_id)
    draft = draft_playbook(channel_id)
    active = next((p for p in pbs if p["status"] == "ACTIVE"), None)
    samples = []
    for r in db.q("""SELECT s.*, sm.added_at FROM samples sm JOIN sources s ON s.id=sm.source_id
                     WHERE sm.channel_id=? ORDER BY sm.added_at""", channel_id):
        runs = [run_stats(x) for x in db.q("SELECT * FROM runs WHERE source_id=? AND kind='PRODUCTION' ORDER BY created_at DESC", r["id"])]
        cur = next((x for x in runs if x["id"] == r["current_run_id"]), None)
        samples.append({
            "id": r["id"], "title": r.get("title") or r["id"], "url": r.get("url"), "duration": r.get("duration"),
            "published_at": r.get("published_at"), "added_at": r["added_at"], "current_run_id": r.get("current_run_id"),
            "delivery": r["delivery"], "notion_url": r.get("notion_url"), "runs": runs,
            # 定稿 delivers each sample's current version: say so when it was made with another playbook
            "stale": bool(cur and draft and cur["playbook_id"] != draft["id"]),
            "busy": any(x["production"] in ACTIVE_RUN for x in runs),
        })
    queue = db.q("""SELECT q.id, q.source_id, q.playbook_id, p.version, s.title FROM trial_queue q JOIN playbooks p ON p.id=q.playbook_id
                    LEFT JOIN sources s ON s.id=q.source_id WHERE q.channel_id=? AND q.status='PENDING' ORDER BY q.id""", channel_id)
    summary = []
    for p in pbs:
        runs = [r for sm in samples for r in sm["runs"] if r["playbook_id"] == p["id"] and r["production"].startswith("COMPLETE")]
        if runs:
            summary.append({"playbook_id": p["id"], "version": p["version"], "note": p.get("note"),
                            "done": len({r["id"] for r in runs}), "samples": len({sm["id"] for sm in samples for r in sm["runs"] if r in runs}),
                            "liked": sum(1 for r in runs if r["liked"]), "notes": sum(1 for r in runs if r["note"])})
    return {"channel": {k: ch[k] for k in ("id", "name", "stage", "mode", "youtube_channel_id")},
            "playbooks": pbs, "draft_id": draft and draft["id"], "active_id": active and active["id"], "samples": samples,
            "queue": queue, "summary": summary}


def compare(run_a: str, run_b: str) -> dict:
    """Align two runs of the same video by time overlap of their knowledge units."""
    def units(rid):
        out = []
        for u in db.q("SELECT id, title, start, end, learning_priority, data FROM units WHERE run_id=? ORDER BY start", rid):
            d = db.unj(u["data"], {})
            n = db.one("SELECT COUNT(*) n FROM flags WHERE run_id=? AND target_id=? AND severity!='INFO'", rid, u["id"])["n"]
            out.append({"id": u["id"], "title": u["title"], "start": u["start"], "end": u["end"],
                        "priority": u["learning_priority"], "tickers": d.get("tickers") or [], "notes": n})
        return out

    A, B = units(run_a), units(run_b)

    def overlap(x, y):
        inter = max(0.0, min(x["end"], y["end"]) - max(x["start"], y["start"]))
        return inter / max(1e-6, min(x["end"] - x["start"], y["end"] - y["start"]))

    pairs, used = [], set()
    for a in A:
        best = max(((overlap(a, b), i) for i, b in enumerate(B) if i not in used), default=(0, None))
        if best[1] is not None and best[0] >= 0.5:
            used.add(best[1])
            pairs.append({"a": a, "b": B[best[1]]})
        else:
            pairs.append({"a": a, "b": None})
    pairs += [{"a": None, "b": b} for i, b in enumerate(B) if i not in used]
    pairs.sort(key=lambda p: (p["a"] or p["b"])["start"])

    def rules(rid):
        r = db.one("SELECT p.data FROM runs x JOIN playbooks p ON p.id=x.playbook_id WHERE x.id=?", rid)
        return {x["rule"].strip() for x in (db.unj(r["data"], {}).get("rules") or [])} if r else set()

    ra, rb = rules(run_a), rules(run_b)
    ga, gb = (db.one("SELECT * FROM runs WHERE id=?", x) for x in (run_a, run_b))
    return {"a": run_stats(ga), "b": run_stats(gb), "pairs": pairs,
            "matched": sum(1 for p in pairs if p["a"] and p["b"]),
            "rules_added": sorted(rb - ra), "rules_removed": sorted(ra - rb)}


def adopt(source_id: str, run_id: str) -> None:
    r = db.one("SELECT * FROM runs WHERE id=? AND source_id=?", run_id, source_id)
    if not r or not r["production"].startswith("COMPLETE"):
        raise ValueError("只能采用已完成的版本")
    db.exec("""UPDATE sources SET current_run_id=?, delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END,
               delivery_reason=NULL, updated_at=? WHERE id=?""", run_id, now(), source_id)


def retune(channel_id: str) -> dict:
    """Back to 调试期: new issues stop arriving (intake paused) until the channel is locked again."""
    from .ingest import set_enabled
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    if not ch or ch["stage"] != "LOCKED":
        raise ValueError("频道不在已定稿状态")
    db.exec("UPDATE channels SET stage='TUNING', updated_at=? WHERE id=?", now(), channel_id)
    cfg = db.setting(f"ingest:{channel_id}") or {}
    if cfg.get("enabled"):
        set_enabled(channel_id, False)
    db.event("tuning", f"{ch['name']} 回到调试期，自动收录已暂停")
    return {"stage": "TUNING", "ingest_paused": bool(cfg.get("enabled"))}
