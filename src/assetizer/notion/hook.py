"""Notion integration webhook: bounded ingress, metadata queue, and targeted comment reads.

The verification secret lives only in secrets/, never in SQLite or diagnostics. Events contain no comment text;
worker processing reads the current discussion, so duplicates and out-of-order deliveries cannot restore old text.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections import deque
from contextlib import closing
from pathlib import Path

import httpx

from .. import config, db, knobs
from ..util import now

PATH = "/api/notion/webhook"
LIMIT = 64 * 1024
INGRESS_WAIT = 5
TUNNEL_TOKEN_FILE = Path("/lzcapp/var/assetizer/hook-tunnel/tunnel-token")
KINDS = {"comment.created", "comment.updated", "comment.deleted"}
_receive_lock = threading.Lock()
_rate_lock = threading.Lock()
_requests: deque[float] = deque()


def file_present(name: str | Path) -> bool:
    """Presence only, including for doctor and the tunnel token: never open the file."""
    try:
        return (config.get().secrets_dir / name).stat().st_size > 0
    except FileNotFoundError:
        return False


def token() -> str | None:
    try:
        return (config.get().secrets_dir / "notion-hook-verification").read_text().strip() or None
    except FileNotFoundError:
        return None


def _put_token(value: str) -> None:
    folder = config.get().secrets_dir
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp is exclusive and 0600 as well; set umask only during creation, not while doing I/O in a web thread.
    mask = os.umask(0o077)
    try:
        fd, path = tempfile.mkstemp(prefix=".notion-hook-", dir=folder)
    finally:
        os.umask(mask)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path, folder / "notion-hook-verification")
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _setting(key: str, default=None, cx=None):
    if cx is None:
        return db.setting(key, default)
    row = cx.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return db.unj(row["value"]) if row else default


def _save(cx, key: str, value) -> None:
    cx.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, db.j(value)))


def _event(cx, message: str, level: str = "info") -> None:
    cx.execute("INSERT INTO events(ts,kind,message,level) VALUES (?,?,?,?)", (now(), "notion-hook", message, level))


def rearm_until(cx=None) -> str | None:
    until = _setting("notion_hook_rearm_until", cx=cx)
    try:
        return until if until and dt.datetime.fromisoformat(until) > dt.datetime.now(dt.timezone.utc) else None
    except (TypeError, ValueError):
        return None


def rearm() -> dict:
    with _receive_lock:
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)).isoformat(timespec="seconds")
        db.set_setting("notion_hook_rearm_until", until)
    return {"rearm_until": until}


def events(cx=None) -> dict:
    return {"total": 0, "rejected": 0, "rejected_at": None, "last_at": None, "last_type": None, "signed_at": None,
            **(_setting("notion_hook_events", cx=cx) or {})}


def rate_limit() -> int | None:
    """One sliding window for the process, across clients and Hosts; Retry-After in seconds."""
    t = time.monotonic()
    with _rate_lock:
        while _requests and _requests[0] <= t - 60:
            _requests.popleft()
        if len(_requests) >= 120:
            return max(1, math.ceil(60 - (t - _requests[0])))
        _requests.append(t)
    return None


def signature_ok(raw: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:].lower())


def receive(raw: bytes, signature: str | None) -> int:
    """No Notion calls; no exceptions or submitted values are put in diagnostics."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return 400
    if not isinstance(body, dict):
        return 400
    if not _receive_lock.acquire(timeout=INGRESS_WAIT):
        return 503
    try:
        config.get().home.mkdir(parents=True, exist_ok=True)
        # This connection belongs only to this request; production db connections keep their 120s budget.
        with closing(sqlite3.connect(str(config.get().db_path), timeout=INGRESS_WAIT, isolation_level=None)) as cx:
            cx.row_factory = sqlite3.Row
            try:
                # One bounded writer acquisition, including a first request on an empty database.
                # The canonical additive schema needs no data migration for the three ingress tables.
                cx.executescript("BEGIN IMMEDIATE;\n" + db.SCHEMA)
                code = _receive(body, raw, signature, cx)
                cx.execute("COMMIT")
                return code
            except BaseException:
                if cx.in_transaction:
                    cx.execute("ROLLBACK")
                raise
    except sqlite3.Error:
        return 503  # Notion retries; never acknowledge an event that was not committed.
    finally:
        _receive_lock.release()


def _receive(body: dict, raw: bytes, signature: str | None, cx) -> int:
    if set(body) == {"verification_token"}:
        value = body["verification_token"]
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or value != value.strip():
            return 400
        stored = token()
        if stored and hmac.compare_digest(stored.encode(), value.encode()):
            return 200
        if not file_present("notion-hook-verification") or rearm_until(cx):
            _put_token(value)
            _save(cx, "notion_hook_token_at", now())
            _save(cx, "notion_hook_rearm_until", None)
            stats = events(cx)
            stats.update(signed_at=None, rejected=0, rejected_at=None)
            _save(cx, "notion_hook_events", stats)
            _event(cx, "收到 Notion 验证令牌")
        else:
            _event(cx, "未更换 Notion 验证令牌：请在设置 → Notion 点「重新接收」，再到 Notion 点「Resend token」", "warn")
        return 200
    secret = token()
    if not secret:
        return 503
    if not signature_ok(raw, signature, secret):
        stats = events(cx)
        stats.update(rejected=stats["rejected"] + 1, rejected_at=now())
        _save(cx, "notion_hook_events", stats)
        minute = int(time.time() // 60)
        if _setting("notion_hook_rejected_minute", cx=cx) != minute:
            _save(cx, "notion_hook_rejected_minute", minute)
            _event(cx, "Notion 推送签名无效，已拒绝", "warn")
        return 401
    at = now()
    kind = body.get("type")
    if not isinstance(kind, str):
        return 400
    stats = events(cx)
    stats.update(total=stats["total"] + 1, last_at=at,
                 last_type=kind if re.fullmatch(r"[a-z_]+\.[a-z_]+", kind) else None,
                 signed_at=stats["signed_at"] or at)
    # Persist routing metadata only, including the original authors for later channel handling.
    if kind in KINDS:
        data, entity, authors = body.get("data"), body.get("entity"), body.get("authors")
        if not isinstance(data, dict) or not isinstance(entity, dict) or not isinstance(authors, list):
            return 400
        parent = data.get("parent")
        fields = [body.get("id"), body.get("timestamp"), data.get("page_id"),
                  parent.get("id") if isinstance(parent, dict) else None, entity.get("id")]
        if not all(isinstance(x, str) and 0 < len(x) <= 128 for x in fields):
            return 400
        if parent.get("type") not in ("page", "block") or entity.get("type") != "comment":
            return 400
        if not all(isinstance(a, dict) and isinstance(a.get("id"), str) and len(a["id"]) <= 128 for a in authors):
            return 400
        cx.execute("""INSERT OR IGNORE INTO notion_events(id, at, ts, type, page, parent, parent_type, entity, bots)
                      VALUES (?,?,?,?,?,?,?,?,?)""",
                   (fields[0], at, fields[1], kind, fields[2], fields[3], parent["type"], fields[4], db.j(authors)))
    _save(cx, "notion_hook_events", stats)
    return 200


def status() -> dict:
    tunnel = "off"
    if file_present(TUNNEL_TOKEN_FILE):
        try:
            tunnel = "up" if httpx.get("http://hook-tunnel:20241/ready", timeout=1, trust_env=False).status_code == 200 else "down"
        except httpx.HTTPError:
            tunnel = "down"
    return {"url": knobs.get("notion_hook_url"), "token": "set" if file_present("notion-hook-verification") else "none",
            "token_at": db.setting("notion_hook_token_at"), "rearm_until": rearm_until(), "tunnel": tunnel, "events": events()}


def pending() -> bool:
    return db.one("SELECT 1 FROM notion_events WHERE done IS NULL AND (retry_at IS NULL OR retry_at<=?) LIMIT 1", now()) is not None


def process() -> None:
    """The topic-hook thread drains arrivals too; busy topics do not hold up other topics."""
    while rows := db.q("SELECT * FROM notion_events WHERE done IS NULL AND (retry_at IS NULL OR retry_at<=?) ORDER BY at, rowid", now()):
        if not _batch(rows):
            time.sleep(0.1)  # All remaining topics are being scanned: recheck without spinning.


def _batch(rows: list[dict]) -> bool:
    from ..topics import comments, notion as TN
    from .. import issue_asks
    norm = TN._norm
    places = {}
    for t in db.q("SELECT id, notion_page_id, notion_rows, archived_at FROM topics WHERE notion_page_id IS NOT NULL"):
        places[norm(t["notion_page_id"])] = (t, t["notion_page_id"], "topic")
        for row in (db.unj(t["notion_rows"], {}) or {}).values():
            if row.get("page"):
                places[norm(row["page"])] = (t, row["page"], "row")
    channels = {norm(r["notion_page_id"]): r for r in db.q("""SELECT s.id,s.notion_page_id,s.delivery,c.stage channel_stage FROM sources s
                LEFT JOIN channels c ON c.id=s.channel_id WHERE s.notion_page_id IS NOT NULL""")}
    read = {}
    n, bot = None, None
    progressed = False
    terminal = {}
    for e in rows:
        outcome, tid, held = "unknown", None, None
        failure = None
        try:
            place = places.get(norm(e["page"]))
            if place and not place[0]["archived_at"]:
                t, page, outcome = place
                tid = t["id"]
                lock = comments.read_lock(tid)
                if not lock.acquire(blocking=False):
                    continue  # Leave this event pending and give the other topics a turn.
                held = lock
                if n is None:
                    n = TN.client()
                    if not n:
                        raise RuntimeError("NO_CLIENT")
                    try:
                        bot = n.me(tries=1, timeout=5).get("id")
                    except Exception:  # Unknown identity must never prevent a targeted read.
                        bot = None
                authors = db.unj(e["bots"], [])
                if bot and authors and all(a.get("type") == "bot" and a.get("id") == bot for a in authors):
                    outcome = "ours"
                else:
                    block = norm(e["parent"]) if e["parent_type"] == "block" else None
                    key = (tid, page, block)
                    if key not in read:
                        try:
                            comments.scan_one(tid, page, block, n=n, bot=bot)
                            read[key] = outcome
                        except Exception:  # No response bodies or raw exceptions in diagnostics.
                            read[key] = "error"
                            db.event("notion-hook", f"专题 {tid} 定点读 Notion 批注异常，等待整页重读补齐", level="warn")
                    outcome = read[key]
            elif norm(e["page"]) in channels and not place:
                outcome = "channel"
                s = channels[norm(e["page"])]
                if s["delivery"] != "REGISTERED" and s["channel_stage"] == "LOCKED":
                    lock = issue_asks.read_lock(s["id"])
                    if not lock.acquire(blocking=False):
                        continue
                    held = lock
                    if n is None:
                        n = TN.client()
                        if not n:
                            raise RuntimeError("NO_CLIENT")
                        try:
                            bot = n.me(tries=1, timeout=5).get("id")
                        except Exception:
                            bot = None
                    authors = db.unj(e["bots"], [])
                    if not (bot and authors and all(a.get("type") == "bot" and a.get("id") == bot for a in authors)):
                        block = norm(e["parent"]) if e["parent_type"] == "block" else None
                        key = ("channel", s["id"], s["notion_page_id"], block)
                        if key not in read:
                            try:
                                issue_asks.scan_one(s["id"], s["notion_page_id"], block, n=n, bot=bot)
                                read[key] = "channel"
                            except Exception as error:
                                read[key] = error
                        if isinstance(read[key], Exception):
                            failure = read[key]
                            outcome = "error"
                        else:
                            outcome = read[key]
        except Exception as error:
            channel = outcome == "channel"
            outcome = "error"
            if channel:
                failure = error
            else:
                db.event("notion-hook", "处理 Notion 批注推送异常，等待整页重读补齐", level="warn")
        finally:
            if held is not None:
                held.release()
        if failure is not None:
            if _channel_retry(e, failure):
                progressed = True
                continue
            sid = channels[norm(e["page"])]["id"]
            terminal[sid] = terminal.get(sid, 0) + 1
        db.exec("UPDATE notion_events SET done=?, outcome=?, topic_id=?,retry_at=NULL WHERE id=? AND done IS NULL", now(), outcome, tid, e["id"])
        progressed = True
    for sid, count in terminal.items():
        title = (db.one("SELECT title FROM sources WHERE id=?", sid) or {}).get("title") or sid
        db.event("notion-hook", f"频道页 {title} 定点读批注失败，{count} 个事件已结束，等原串新的点名", level="warn")
    return progressed


def _channel_retry(event: dict, error: Exception) -> bool:
    """Five delayed retries after the initial read; topics retain their existing terminal handling."""
    from .client import NotionError
    retryable = isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)) or (
        isinstance(error, NotionError) and (error.status == 429 or error.status >= 500))
    tries = event.get("tries", 0)
    delays = (60, 300, 1800, 7200, 21600)
    if retryable and tries < len(delays):
        at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delays[tries])).isoformat(timespec="seconds")
        db.exec("UPDATE notion_events SET tries=?,retry_at=? WHERE id=? AND done IS NULL", tries + 1, at, event["id"])
        return True
    return False
