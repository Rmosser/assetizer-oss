"""The logs kept for iterating on what changes under the pipeline (agy and yt-dlp update often): a month of them, the
week read in Monday's digest, and a read-only export for the developer's agent that scans them
(docs/operations.md「日志与迭代」). Nothing here calls a model or writes anywhere but the logs it prunes."""
from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

from . import config, db

WEEK = 7  # days the Monday digest reads
MANY = 5  # this many of the week's failures, waits and model calls gone wrong — or one kind not seen before — ask for a scan
# a model call's files that are its log; its receipt (invocation.json) and answer (response.txt) stay: a retry reuses them
CALL_LOGS = ("stderr.log", "raw-output.jsonl", "prompt.md", "stdin-message.jsonl", "schema.json")
# not what iterating is about: a passing hiccup, a live not over, a video gone, what the user stopped or was too short
QUIET = {"NETWORK", "LIVE_IN_PROGRESS", "PLAYBOOK_CONFIRMATION", "STOPPED", "SKIPPED_SHORT",
         "VIDEO_REMOVED", "VIDEO_PRIVATE", "VIDEO_MEMBERS", "SOURCE_UNAVAILABLE"}
TAIL = 3000  # characters of a log file in the export


def _since(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")


def prune(days: int | None = None) -> dict:
    """Logs older than ``days`` (the retention, 30) go: yt-dlp's output, each model call's log files, the events — but an
    event of a run still under way stays (intake tells its videos by one)."""
    s = config.get()
    days = days or s.log_retention_days
    cut = time.time() - days * 86400
    files = 0
    paths = [*s.logs_dir.glob("yt-dlp/*.log"), *(p for n in CALL_LOGS for p in s.runs_dir.glob(f"*/calls/**/{n}")),
             *(p for n in CALL_LOGS for p in (s.home / "compile").glob(f"**/{n}"))]
    for p in paths:
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
                files += 1
        except OSError:
            pass
    events = db.connect().execute("""DELETE FROM events WHERE ts < ? AND (run_id IS NULL OR run_id NOT IN
                                     (SELECT id FROM runs WHERE production IN ('QUEUED','RUNNING','WAITING','INTERRUPTED')))""",
                                  (_since(days),)).rowcount
    db.exec("DELETE FROM notion_events WHERE at < ?", _since(30))
    return {"files": files, "events": events}


def _kinds(since: str, until: str | None = None) -> dict[str, dict[str, int]]:
    """What went wrong from ``since`` (to ``until``), by kind: runs failed, waits begun, model calls failed."""
    until = until or "9999"
    out: dict[str, dict[str, int]] = {"failed": {}, "waited": {}, "calls": {}}
    for r in db.q("SELECT error_class k, COUNT(*) n FROM runs WHERE production='FAILED' AND finished_at>=? AND finished_at<? "
                  "GROUP BY error_class", since, until):
        out["failed"][r["k"] or "UNKNOWN"] = r["n"]
    for r in db.q("SELECT substr(message, 4) k, COUNT(*) n FROM events WHERE kind='run' AND message LIKE '等待：%' AND ts>=? AND ts<? "
                  "GROUP BY k", since, until):
        out["waited"][r["k"]] = r["n"]
    for r in db.q("SELECT COALESCE(error_class, status) k, COUNT(*) n FROM model_calls WHERE status!='SUCCESS' AND created_at>=? "
                  "AND created_at<? GROUP BY k", since, until):
        out["calls"][r["k"] or "UNKNOWN"] = r["n"]
    return {g: {k: n for k, n in v.items() if k not in QUIET} for g, v in out.items()}


def summary(days: int = WEEK) -> dict:
    """The last ``days``: what went wrong by kind, the partial issues, the agy versions it ran on, the kinds not seen in
    the rest of the month before, and whether that asks for a scan (``many``)."""
    since = _since(days)
    now = _kinds(since)
    before = _kinds(_since(config.get().log_retention_days), since)
    seen = {k for g in before.values() for k in g}
    kinds = {k for g in now.values() for k in g}
    partial = db.one("SELECT COUNT(*) n FROM runs WHERE production='COMPLETE_PARTIAL' AND finished_at>=?", since)["n"]
    agy = [r["v"] for r in db.q("SELECT DISTINCT agy_version v FROM model_calls WHERE created_at>=? AND agy_version IS NOT NULL "
                                "ORDER BY v", since)]
    total = sum(n for g in now.values() for n in g.values())
    return {"days": days, "since": since, **now, "partial": partial, "agy": agy, "new": sorted(kinds - seen),
            "many": total >= MANY or bool(kinds - seen)}


def _counts(d: dict[str, int]) -> str:
    return "、".join(f"{k} {n}" for k, n in sorted(d.items(), key=lambda x: -x[1]))


def digest_lines() -> list[str]:
    """Monday's digest: the week in a line or three, and a word when the logs are worth a scan."""
    w = summary()
    parts = [f"失败 {sum(w['failed'].values())}（{_counts(w['failed'])}）" if w["failed"] else "",
             f"等待 {sum(w['waited'].values())}（{_counts(w['waited'])}）" if w["waited"] else "",
             f"模型调用出错 {sum(w['calls'].values())}（{_counts(w['calls'])}）" if w["calls"] else "",
             f"部分完成 {w['partial']}" if w["partial"] else ""]
    lines = [f"🔧 迭代 · 过去 {w['days']} 天：" + (" · ".join(p for p in parts if p) or "没有要处理的报错")]
    if w["new"]:
        lines.append("新出现：" + "、".join(w["new"]))
    if w["many"]:
        lines.append(f"日志多了，可以让开发用的 agent 扫一遍（日志留 {config.get().log_retention_days} 天）")
    from .topics.comments import used  # how much the words to 课代表 in Notion are used: kept only if they are
    week, month = used(WEEK), used(30)
    lines.append(f"📝 Notion 里点名课代表：这周 {week['asks']} 次，近 30 天 {month['asks']} 次"
                 + (f"；待修 {len(month['todo'])} 条" if month["todo"] else ""))
    return lines


# --------------------------------------------------------------------------- the export a developer's agent reads
_SECRETS = [
    (re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),  # bot<id>:<secret> in a URL too
    (re.compile(r"\b(?:secret|ntn)_[A-Za-z0-9]{20,}"), "<notion-token>"),
    (re.compile(r"(?i)\b(bearer|token|authorization|api[_-]?key)([\"':= ]+)(?!<)[A-Za-z0-9._~+/=-]{12,}"), r"\1\2<redacted>"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "<google-token>"),
    (re.compile(r"(?i)\b(SAPISID|APISID|HSID|SSID|SID|LOGIN_INFO|__Secure-[0-9A-Za-z-]+)=[^;\s]+"), r"\1=<redacted>"),
    (re.compile(r"(?m)^[^\t\n]*\t(?:TRUE|FALSE)\t[^\t\n]*\t(?:TRUE|FALSE)\t\d+\t[^\t\n]+\t[^\t\n]*$"), "<cookie>"),
    (re.compile(r"([?&](?:key|token|auth|sig|signature|access_token)=)[^&\s\"']+"), r"\1<redacted>"),
]


def redact(text: str | None) -> str:
    """Logs show no credentials (AGENTS.md): a token, a cookie or a signed URL's key that got into one is blanked."""
    out = text or ""
    for pat, repl in _SECRETS:
        out = pat.sub(repl, out)
    return out


def _tail(p: Path) -> str | None:
    try:
        return redact(p.read_text(errors="replace")[-TAIL:])
    except OSError:
        return None


def export(days: int | None = None) -> dict:
    """Everything a scan reads, for the last ``days`` (all that is kept), redacted: the week's summary, what went wrong
    (runs failed or partial, waits, model calls with their stderr), yt-dlp's words and the warnings and errors logged."""
    s = config.get()
    days = days or s.log_retention_days
    since = _since(days)
    runs = [{**r, "error": redact((r.get("error") or "")[:1500])} for r in db.q(
        """SELECT id, source_id, origin, production, wait_reason, error_class, error, pins, created_at, finished_at FROM runs
           WHERE (production IN ('FAILED','COMPLETE_PARTIAL') AND finished_at>=?) OR (production='WAITING' AND created_at>=?)
           ORDER BY created_at DESC LIMIT 300""", since, since)]
    calls = []
    for c in db.q("""SELECT run_id, stage, attempt, model, agy_version, status, error_class, wall_seconds, created_at FROM model_calls
                     WHERE status!='SUCCESS' AND created_at>=? ORDER BY created_at DESC LIMIT 200""", since):
        d = s.runs_dir / (c["run_id"] or "") / "calls" / (c["stage"] or "") / (c["attempt"] or "")
        calls.append({**c, "stderr": _tail(d / "stderr.log") if c["run_id"] else None})
    cut = time.time() - days * 86400
    ytdlp = [{"file": p.name, "tail": _tail(p)} for p in sorted(s.logs_dir.glob("yt-dlp/*.log"), reverse=True)
             if p.stat().st_mtime >= cut][:100]
    events = [{**e, "message": redact(e["message"]), "data": redact(e["data"])} for e in db.q(
        "SELECT ts, run_id, level, kind, message, data FROM events WHERE level IN ('warn','error') AND ts>=? ORDER BY id DESC LIMIT 500",
        since)]
    from .topics.comments import used
    return {"days": days, "week": summary(), "runs": runs, "calls": calls, "yt_dlp": ytdlp, "events": events, "asks": used(days),
            "versions": {"agy": [r["v"] for r in db.q("SELECT DISTINCT agy_version v FROM model_calls WHERE created_at>=? "
                                                      "AND agy_version IS NOT NULL ORDER BY v", since)]}}
