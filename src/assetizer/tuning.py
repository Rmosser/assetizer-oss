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
LOCK_ACTIONS = ("write", "keep", "replace", "wait")
# seconds of work per minute of video, by stage (ETA of a running issue; a first guess before a channel has history)
STAGE_COST = {"RESOLVE": 0.1, "PLAYBOOK": 0, "ACQUIRE": 3, "CHUNK": 0.3, "UNDERSTAND": 12, "MERGE": 2, "KNOWLEDGE": 8,
              "ARTICLE": 8, "SELECT": 1, "SCREENSHOTS": 0.5, "BOUNDARY": 14, "CLIPS": 30, "SUBTITLES": 10, "MEDIA_QA": 2,
              "REVIEW_CHECK": 2, "COMPOSE": 0, "PACKAGE": 0.2, "CLEANUP": 0, "TRANSCRIBE": 0}


class LockRefused(ValueError):
    """定稿 cannot be done as asked (HTTP 409); nothing was changed."""


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
    sample before the next), so the user can compare a sample as soon as its versions are done. Without `source_ids`
    (a version's 试跑 N 期) only the pairs that have no run yet are queued; a listed sample is run again on purpose.
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
            if source_ids is None and db.one("SELECT 1 FROM runs WHERE source_id=? AND playbook_id=?", sid, pid):
                out.append({"source_id": sid, "playbook_id": pid, "skipped": "已跑过"})
                continue
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
    """Submit the next queued trial once nothing else is waiting to run (strict queue order). The worker calls this only
    with a free slot, so with `worker_slots` > 1 trials run side by side."""
    from .pipeline.runner import submit
    if db.one("SELECT 1 FROM runs WHERE production='QUEUED'"):
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
        r = submit(f"https://www.youtube.com/watch?v={e['source_id']}", playbook_id=e["playbook_id"], origin="trial")
        db.exec("UPDATE trial_queue SET status='DISPATCHED', run_id=?, dispatched_at=? WHERE id=?", r["run_id"], now(), e["id"])
        db.set_setting("trial_batch", (db.setting("trial_batch") or 0) + 1)  # the worker says when the batch is done
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
    stages = db.q("SELECT name, status FROM stages WHERE run_id=?", run["id"])
    done = sum(1 for s in stages if s["status"] in ("DONE", "SKIPPED", "WARN"))
    dur = (db.one("SELECT duration FROM sources WHERE id=?", run["source_id"]) or {}).get("duration") or 3600
    eta = round(sum(STAGE_COST.get(s["name"], 0) * dur / 60 for s in stages if s["status"] in ("PENDING", "RUNNING"))) \
        if run["production"] in ACTIVE_RUN else None
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
        "track": [[round(u["start"], 1), round(u["end"], 1), u["learning_priority"]]
                  for u in db.q("SELECT start, end, learning_priority FROM units WHERE run_id=? ORDER BY start", run["id"])],
        "flag_at": [round((f["start"] + f["end"]) / 2, 1) for f in db.q(
            "SELECT u.start, u.end FROM flags f JOIN units u ON u.id=f.target_id WHERE f.run_id=? AND f.severity!='INFO'", run["id"])],
        "eta_seconds": eta,
        "error_line": ((run.get("error") or "").splitlines() or [None])[0],
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
            "adopt": {"run_id": r.get("current_run_id"), "pinned": bool(r.get("adopt_pinned"))},
            "delivery": r["delivery"], "notion_url": r.get("notion_url"), "runs": runs,
            # 定稿 delivers each sample's current version: say so when it was made with another playbook
            "stale": bool(cur and draft and cur["playbook_id"] != draft["id"]),
            "busy": any(x["production"] in ACTIVE_RUN for x in runs),
        })
    queue = db.q("""SELECT q.id, q.source_id, q.playbook_id, p.version, s.title FROM trial_queue q JOIN playbooks p ON p.id=q.playbook_id
                    LEFT JOIN sources s ON s.id=q.source_id WHERE q.channel_id=? AND q.status='PENDING' ORDER BY q.id""", channel_id)
    adopted = [next((x["playbook_id"] for x in sm["runs"] if x["id"] == sm["current_run_id"]), None) for sm in samples]
    rate = minutes_per_video_minute(channel_id)
    for p in pbs:
        p["adopt_count"] = sum(1 for a in adopted if a == p["id"])
        missing = [sm for sm in samples if not any(r["playbook_id"] == p["id"] for r in sm["runs"])
                   and not any(q["source_id"] == sm["id"] and q["playbook_id"] == p["id"] for q in queue)]
        p["missing"] = len(missing)
        p["est_minutes"] = round(sum(rate * (sm["duration"] or 3600) / 60 for sm in missing))
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


def minutes_per_video_minute(channel_id: str) -> float:
    """How long a run takes per minute of video on this channel (median of recent runs), for 试跑 estimates."""
    from datetime import datetime
    rates = []
    for r in db.q("""SELECT r.started_at, r.finished_at, s.duration FROM runs r JOIN sources s ON s.id=r.source_id
                     WHERE s.channel_id=? AND r.production LIKE 'COMPLETE%' AND r.started_at IS NOT NULL
                     AND r.finished_at IS NOT NULL AND s.duration>0 ORDER BY r.finished_at DESC LIMIT 20""", channel_id):
        try:
            wall = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds() / 60
        except ValueError:
            continue
        if wall > 0:
            rates.append(wall / (r["duration"] / 60))
    if not rates:
        return sum(STAGE_COST.values()) / 60
    return sorted(rates)[len(rates) // 2]


def changes(A: list[dict], B: list[dict]) -> tuple[list[str], list[str]]:
    """What became of each unit between two runs, by time overlap (a unit belongs where ≥60% of its own span lies).
    Old units: same / split / merged / gone. New units: same / split (carved out of one old unit) /
    merge (covers several old units) / new."""
    def ov(a, b):
        return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))

    def span(u):
        return max(1e-6, u["end"] - u["start"])

    nb: list[tuple[str, object]] = []
    for b in B:
        if not A:
            nb.append(("new", None))
            continue
        i = max(range(len(A)), key=lambda k: ov(A[k], b))
        fb, fa = ov(A[i], b) / span(b), ov(A[i], b) / span(A[i])
        if fb >= 0.6 and fa >= 0.6:
            nb.append(("same", i))
        elif fb >= 0.6:
            nb.append(("split", i))
        else:
            cov = [k for k, a in enumerate(A) if ov(a, b) / span(a) >= 0.6]
            nb.append(("merge", cov) if len(cov) >= 2 else ("new", None))
    na = []
    for i, a in enumerate(A):
        kids = [j for j, b in enumerate(B) if ov(a, b) / span(b) >= 0.6]
        if len(kids) >= 2:
            na.append("split")
        elif any(t == "same" and x == i for t, x in nb):
            na.append("same")
        elif any(t == "merge" and i in x for t, x in nb):
            na.append("merged")
        else:
            na.append("gone")
    return na, [t for t, _ in nb]


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
        return (db.unj(r["data"], {}).get("rules") or []) if r else []

    from .compiler import _rule_diff, rule_label
    d = _rule_diff(rules(run_a), rules(run_b))
    na, nb = changes(A, B)
    ga, gb = (db.one("SELECT * FROM runs WHERE id=?", x) for x in (run_a, run_b))
    return {"a": run_stats(ga), "b": run_stats(gb), "pairs": pairs,
            "matched": sum(1 for p in pairs if p["a"] and p["b"]),
            "a_units": [{**u, "change": c} for u, c in zip(A, na)], "b_units": [{**u, "change": c} for u, c in zip(B, nb)],
            "counts": {"new": nb.count("new"), "split": nb.count("split"), "merge": nb.count("merge"), "gone": na.count("gone")},
            "rules_added": sorted(r["rule"].strip() for r in d["added"]), "rules_removed": sorted(r["rule"].strip() for r in d["removed"]),
            "rules_changed": [{"from": x["from"]["rule"], "to": x["to"]["rule"], "label": rule_label(x["to"])} for x in d["changed"]]}


def adopt(source_id: str, run_id: str | None = None, pinned: bool = True) -> dict:
    """Pin a finished run as the issue's version, or (pinned=False) follow the newest finished run again."""
    s = db.one("SELECT current_run_id FROM sources WHERE id=?", source_id)
    if not s:
        raise ValueError("找不到这一期")
    if pinned:
        r = db.one("SELECT * FROM runs WHERE id=? AND source_id=?", run_id, source_id)
        if not r or not r["production"].startswith("COMPLETE"):
            raise ValueError("只能采用已完成的版本")
        target = run_id
    else:
        r = db.one("""SELECT id FROM runs WHERE source_id=? AND kind='PRODUCTION' AND production LIKE 'COMPLETE%'
                      ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1""", source_id)
        target = r["id"] if r else s["current_run_id"]
    if target != s["current_run_id"]:
        from .notion.mapping import run_headline
        db.exec("""UPDATE sources SET current_run_id=?, headline=?, delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END,
                   delivery_reason=NULL, updated_at=? WHERE id=?""", target, run_headline(target), now(), source_id)
    db.exec("UPDATE sources SET adopt_pinned=? WHERE id=?", int(pinned), source_id)
    return {"run_id": target, "pinned": pinned}


def tested(channel_id: str, playbook_id: int) -> bool:
    """Has this version finished at least once on one of the channel's samples?"""
    return bool(db.one("""SELECT 1 FROM runs r JOIN samples sm ON sm.source_id=r.source_id AND sm.channel_id=?
                          WHERE r.playbook_id=? AND r.production LIKE 'COMPLETE%' LIMIT 1""", channel_id, playbook_id))


def confirm(playbook_id: int, clear_pins: bool = True, intake: bool = True) -> dict:
    """定稿 (design §R2-1): the playbook becomes the channel's active one, the channel leaves tuning, its held samples go
    to Notion and new uploads come in by RSS."""
    from .ingest import set_enabled
    p = db.one("SELECT * FROM playbooks WHERE id=?", playbook_id)
    if not p:
        raise LookupError(playbook_id)
    db.exec("UPDATE playbooks SET status='SUPERSEDED' WHERE channel_id=? AND status='ACTIVE'", p["channel_id"])
    db.exec("UPDATE playbooks SET status='ACTIVE', activated_at=? WHERE id=?", now(), playbook_id)
    db.exec("UPDATE decisions SET resolved_at=?, outcome='CONFIRMED' WHERE kind='CONFIRM_PLAYBOOK' AND ref=?", now(), str(playbook_id))
    db.exec("""UPDATE runs SET production='QUEUED', wait_reason=NULL WHERE production='WAITING' AND wait_reason='PLAYBOOK_CONFIRMATION'
               AND source_id IN (SELECT id FROM sources WHERE channel_id=?)""", p["channel_id"])
    ch = db.one("SELECT * FROM channels WHERE id=?", p["channel_id"]) if p["channel_id"] else None
    if ch:
        db.exec("UPDATE channels SET stage='LOCKED', updated_at=? WHERE id=?", now(), ch["id"])
        if clear_pins:  # pins only matter while tuning: after 定稿 a rerun replaces the issue as usual
            db.exec("UPDATE sources SET adopt_pinned=0 WHERE id IN (SELECT source_id FROM samples WHERE channel_id=?)", ch["id"])
        if intake and ch["mode"] == "TRACKED" and ch["youtube_channel_id"]:
            set_enabled(ch["id"], True)
    return {"ok": True}


def lock_preview(channel_id: str, playbook_id: int) -> dict:
    """Everything the 定稿单 asks about, from the database (the library check is added by the caller)."""
    from .notion import mapping as M
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    pb = db.one("SELECT id, version, status, audit FROM playbooks WHERE id=? AND channel_id=?", playbook_id, channel_id)
    if not ch or not pb:
        raise ValueError("规程版本不属于这个频道")
    rows = []
    for s in db.q("SELECT s.*, sm.added_at FROM samples sm JOIN sources s ON s.id=sm.source_id WHERE sm.channel_id=? ORDER BY sm.added_at",
                  channel_id):
        cur = db.one("SELECT r.id, p.version FROM runs r LEFT JOIN playbooks p ON p.id=r.playbook_id WHERE r.id=?",
                     s["current_run_id"]) if s["current_run_id"] else None
        live = db.one(f"""SELECT r.id, r.production, p.version FROM runs r LEFT JOIN playbooks p ON p.id=r.playbook_id
                          WHERE r.source_id=? AND r.production IN {ACTIVE_RUN} ORDER BY r.created_at DESC LIMIT 1""", s["id"])
        queued = db.q("""SELECT p.version FROM trial_queue q JOIN playbooks p ON p.id=q.playbook_id
                         WHERE q.channel_id=? AND q.source_id=? AND q.status='PENDING' ORDER BY q.id""", channel_id, s["id"])
        rows.append({"source_id": s["id"], "title": s.get("title") or s["id"], "delivery": s["delivery"],
                     "notion_page_id": s.get("notion_page_id"),
                     "adopted": cur and {"run_id": cur["id"], "version": cur["version"], "pinned": bool(s["adopt_pinned"])},
                     "running": live and {"run_id": live["id"], "version": live["version"], "state": live["production"]},
                     "queued": [q["version"] for q in queued]})
    cfg = M.get(channel_id) or {}
    title = cfg.get("database_title") or ""
    ing = db.setting(f"ingest:{channel_id}") or {}
    cands = db.q("SELECT id, version, status FROM playbooks WHERE channel_id=? AND status IN ('NEEDS_CONFIRMATION','ACTIVE') "
                 "ORDER BY version DESC", channel_id)
    return {
        "channel": {"id": ch["id"], "name": ch["name"], "stage": ch["stage"]},
        "playbook": {"id": pb["id"], "version": pb["version"], "status": pb["status"]},
        "tested": tested(channel_id, playbook_id),
        "candidates": [{**c, "tested": tested(channel_id, c["id"])} for c in cands],
        "conflicts": (db.unj(pb["audit"], {}) or {}).get("policy_conflicts") or [],
        "samples": rows,
        "target": {"configured": bool(cfg.get("database_id")), "title": title, "test_copy": M.test_copy(cfg)},
        "ingest": {"available": ch["mode"] == "TRACKED" and bool(ch["youtube_channel_id"]),
                   "enabled": bool(ing.get("enabled")), "min_minutes": ing.get("min_minutes", 10)},
    }


def lock(channel_id: str, playbook_id: int, decisions: list[dict] | None = None, intake: dict | None = None) -> dict:
    """定稿单: lock the channel on one playbook and decide, sample by sample, what goes to Notion. All or nothing.

    write   → delivered after 定稿 (a leftover trial run no longer replaces it)
    keep    → the library already has it: 已登记, nothing written
    replace → rewrite the page Assetizer wrote before (pages the user made themselves are never replaced)
    wait    → hold delivery until the run still in progress ends; if it fails, deliver the adopted version"""
    from .ingest import set_enabled
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    if not ch or ch["stage"] != "TUNING":
        raise ValueError("频道不在调试中")
    if not db.one("SELECT 1 FROM playbooks WHERE id=? AND channel_id=?", playbook_id, channel_id):
        raise ValueError("规程版本不属于这个频道")
    if not tested(channel_id, playbook_id):
        raise LockRefused("这一版还没在样片上跑过")
    samples = {r["source_id"] for r in db.q("SELECT source_id FROM samples WHERE channel_id=?", channel_id)}
    plan: dict[str, dict] = {}
    for d in decisions or []:
        sid, act = d.get("source_id"), d.get("action") or "write"
        if sid not in samples:
            raise ValueError(f"{sid} 不是这个频道的样片")
        if act not in LOCK_ACTIONS:
            raise ValueError(f"不认识的决定：{act}")
        s = db.one("SELECT * FROM sources WHERE id=?", sid)
        name = s.get("title") or sid
        if act == "replace" and not s.get("notion_page_id"):
            raise LockRefused(f"「{name}」库里那一页不是课代表写的，只能保留")
        run_id = None
        if act == "wait":
            r = db.one(f"SELECT id FROM runs WHERE source_id=? AND production IN {ACTIVE_RUN} ORDER BY created_at DESC LIMIT 1", sid)
            if not r:
                raise LockRefused(f"「{name}」没有还在跑的版本可等")
            run_id = r["id"]
        plan[sid] = {"action": act, "run_id": run_id}
    counts = dict.fromkeys(LOCK_ACTIONS, 0)
    with db.tx():
        confirm(playbook_id, clear_pins=False, intake=False)
        # trials still waiting in line belong to tuning: cancel them now, so a later 回到调试期 does not revive them
        db.exec("UPDATE trial_queue SET status='CANCELLED' WHERE channel_id=? AND status='PENDING'", channel_id)
        for sid in samples:
            d = plan.get(sid, {"action": "write"})
            counts[d["action"]] += 1
            if d["action"] == "wait":
                db.exec("""UPDATE sources SET deliver_after_run=?, delivery='HELD', delivery_reason=?, adopt_pinned=0, updated_at=?
                           WHERE id=?""", d["run_id"], f"等 {d['run_id']} 跑完", now(), sid)
            elif d["action"] == "keep":
                db.exec("""UPDATE sources SET delivery='REGISTERED', delivery_reason='库里已有，定稿时选择保留', adopt_pinned=1, updated_at=?
                           WHERE id=?""", now(), sid)
            elif d["action"] == "replace":
                db.exec("UPDATE sources SET delivery='STALE', delivery_reason=NULL, adopt_pinned=1, updated_at=? WHERE id=?", now(), sid)
            else:
                db.exec("UPDATE sources SET adopt_pinned=1 WHERE id=?", sid)
        ing = None
        if ch["mode"] == "TRACKED" and ch["youtube_channel_id"]:
            on = True if intake is None else bool(intake.get("enabled", True))
            ing = set_enabled(channel_id, on, (intake or {}).get("min_minutes"))
    v = db.one("SELECT version FROM playbooks WHERE id=?", playbook_id)["version"]
    db.event("tuning", f"{ch['name']} 定稿 v{v}：写入 {counts['write'] + counts['replace']} 期，保留 {counts['keep']} 期，"
             f"等待 {counts['wait']} 期")
    return {"stage": "LOCKED", "version": v, "write": counts["write"] + counts["replace"], "keep": counts["keep"],
            "wait": counts["wait"], "ingest": ing}


def release_hold(run_id: str) -> None:
    """The run a 定稿 was waiting for ended without finishing: deliver the version adopted before."""
    db.exec("""UPDATE sources SET deliver_after_run=NULL, delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END,
               delivery_reason=NULL, updated_at=? WHERE deliver_after_run=? AND delivery='HELD'""", now(), run_id)


def retune(channel_id: str) -> dict:
    """Back to 调试期: new issues stop arriving (intake paused) until the channel is locked again."""
    from .ingest import set_enabled
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    if not ch or ch["stage"] != "LOCKED":
        raise ValueError("频道不在已定稿状态")
    db.exec("UPDATE channels SET stage='TUNING', updated_at=? WHERE id=?", now(), channel_id)
    db.exec("""UPDATE sources SET deliver_after_run=NULL, delivery=CASE WHEN notion_page_id IS NULL THEN 'NOT_STARTED' ELSE 'STALE' END,
               delivery_reason=NULL WHERE channel_id=? AND delivery='HELD'""", channel_id)
    cfg = db.setting(f"ingest:{channel_id}") or {}
    if cfg.get("enabled"):
        set_enabled(channel_id, False)
    db.exec("UPDATE backfill_queue SET status='CANCELLED' WHERE channel_id=? AND status='PENDING'", channel_id)  # 补录 waits for 定稿
    db.event("tuning", f"{ch['name']} 回到调试期，自动收录已暂停")
    return {"stage": "TUNING", "ingest_paused": bool(cfg.get("enabled"))}
