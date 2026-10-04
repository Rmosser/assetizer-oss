"""SQLite persistence. Files on disk stay the source of truth for artifacts;
the DB indexes state so the UI and worker can coordinate (WAL mode, short
transactions, one writer at a time)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from . import config
from .util import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  youtube_channel_id TEXT UNIQUE,
  mode TEXT NOT NULL DEFAULT 'DISCOVERY',          -- TRACKED | DISCOVERY
  delivery_policy TEXT NOT NULL DEFAULT 'auto',     -- auto | after_approval
  playbook_approval TEXT NOT NULL DEFAULT 'auto_if_clean', -- auto_if_clean | manual
  notion_parent_id TEXT,
  why TEXT,                                         -- user's stated reason for tracking
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL REFERENCES channels(id),
  version INTEGER NOT NULL,
  text TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  origin TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(channel_id, version)
);
CREATE TABLE IF NOT EXISTS playbooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL REFERENCES channels(id),
  prompt_id INTEGER NOT NULL REFERENCES channel_prompts(id),
  version INTEGER NOT NULL,
  status TEXT NOT NULL,              -- COMPILING | NEEDS_CONFIRMATION | ACTIVE | SUPERSEDED | FAILED
  data TEXT,                          -- JSON: requirements, rules, article_style, orchestration_notes
  audit TEXT,                         -- JSON: fidelity matrix + provenance checks
  error TEXT,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  UNIQUE(channel_id, version)
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,                -- youtube video id or local:<sha12>
  url TEXT,
  channel_id TEXT REFERENCES channels(id),
  youtube_channel_id TEXT,
  channel_title TEXT,
  title TEXT,
  published_at TEXT,
  duration REAL,
  media_path TEXT,
  media_sha256 TEXT,
  media_bytes INTEGER,
  media_state TEXT NOT NULL DEFAULT 'ABSENT',   -- ABSENT | CACHED | PURGED
  info TEXT,
  created_at TEXT NOT NULL,
  purged_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  channel_id TEXT REFERENCES channels(id),
  kind TEXT NOT NULL,                 -- PRODUCTION | DISCOVERY
  playbook_id INTEGER REFERENCES playbooks(id),
  status TEXT NOT NULL,               -- QUEUED RUNNING BLOCKED FAILED READY DISCOVERED APPROVED REJECTED CANCELLED
  stage TEXT,
  block_reason TEXT,
  error_class TEXT,
  error TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  notion_url TEXT,
  notion_page_id TEXT,
  package_path TEXT,
  review_status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING APPROVED CHANGES_REQUESTED REJECTED
  review_notes TEXT,
  review_at TEXT,
  summary TEXT,                       -- JSON quick stats for list views
  supersedes TEXT                     -- run id this one regenerates
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, priority DESC, created_at);
CREATE TABLE IF NOT EXISTS stages (
  run_id TEXT NOT NULL REFERENCES runs(id),
  name TEXT NOT NULL,
  ord INTEGER NOT NULL,
  status TEXT NOT NULL,               -- PENDING RUNNING DONE SKIPPED WARN FAILED
  started_at TEXT,
  finished_at TEXT,
  wall_seconds REAL,
  detail TEXT,
  PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  stage TEXT,
  attempt TEXT,
  conversation_id TEXT,
  model TEXT,
  status TEXT,
  error_class TEXT,
  wall_seconds REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  thinking_tokens INTEGER,
  peak_rss_kib INTEGER,
  view_files INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS units (
  run_id TEXT NOT NULL REFERENCES runs(id),
  unit_id TEXT NOT NULL,
  ord INTEGER NOT NULL,
  data TEXT NOT NULL,
  decision TEXT,                      -- null | APPROVED | REJECTED
  publication_override TEXT,
  note TEXT,
  PRIMARY KEY (run_id, unit_id)
);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  unit_id TEXT,
  kind TEXT NOT NULL,                 -- screenshot clip clip_subtitled srt ass article package notion
  status TEXT NOT NULL,               -- GENERATED WARNING FAILED NOT_SELECTED
  path TEXT,
  sha256 TEXT,
  bytes INTEGER,
  meta TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, unit_id, kind, path)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  run_id TEXT,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  data TEXT
);
CREATE TABLE IF NOT EXISTS attention (
  key TEXT PRIMARY KEY,
  severity TEXT NOT NULL,             -- critical | warning | info
  title TEXT NOT NULL,
  body TEXT,
  action TEXT,                        -- JSON: {label, href | api}
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    path = str(config.get().db_path)
    if conn is None or getattr(_local, "path", None) != path:
        config.get().ensure_dirs()
        conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        _local.conn, _local.path = conn, path
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def q(sql: str, *args) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def one(sql: str, *args) -> dict | None:
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


def exec(sql: str, *args) -> int:
    cur = connect().execute(sql, args)
    return cur.lastrowid or cur.rowcount


def j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def unj(v: str | None, default: Any = None) -> Any:
    return json.loads(v) if v else default


# --------------------------------------------------------------- conveniences
def event(kind: str, message: str, run_id: str | None = None, level: str = "info", data: Any = None) -> None:
    exec("INSERT INTO events(ts, run_id, level, kind, message, data) VALUES (?,?,?,?,?,?)",
         now(), run_id, level, kind, message, j(data) if data is not None else None)


def raise_attention(key: str, severity: str, title: str, body: str = "", action: dict | None = None) -> None:
    exec("""INSERT INTO attention(key, severity, title, body, action, created_at) VALUES (?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET severity=excluded.severity, title=excluded.title, body=excluded.body,
            action=excluded.action, resolved_at=NULL""",
         key, severity, title, body, j(action) if action else None, now())


def resolve_attention(key: str) -> None:
    exec("UPDATE attention SET resolved_at=? WHERE key=? AND resolved_at IS NULL", now(), key)


def setting(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM settings WHERE key=?", key)
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    exec("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, j(value))
