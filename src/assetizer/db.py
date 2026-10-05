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
  mode TEXT NOT NULL DEFAULT 'DISCOVERY',            -- TRACKED | DISCOVERY
  delivery_policy TEXT NOT NULL DEFAULT 'auto',       -- auto | after_approval
  notion_parent_id TEXT,
  why TEXT,
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
  origin_edited_at TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(channel_id, version)
);
CREATE TABLE IF NOT EXISTS playbooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT REFERENCES channels(id),          -- NULL = universal
  prompt_id INTEGER REFERENCES channel_prompts(id),
  version INTEGER NOT NULL,
  status TEXT NOT NULL,                               -- NEEDS_CONFIRMATION | ACTIVE | SUPERSEDED | FAILED
  data TEXT NOT NULL,                                 -- JSON playbook (identity, rules, facets, article_style)
  audit TEXT,
  origin TEXT,
  created_at TEXT NOT NULL,
  activated_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,                                -- youtube video id
  url TEXT,
  channel_id TEXT REFERENCES channels(id),
  youtube_channel_id TEXT,
  channel_title TEXT,
  title TEXT,
  headline TEXT,                                       -- 本期题眼 (article outline)
  published_at TEXT,
  duration REAL,
  info TEXT,
  media_path TEXT,
  media_sha256 TEXT,
  media_bytes INTEGER,
  media_height INTEGER,
  media_state TEXT NOT NULL DEFAULT 'ABSENT',         -- ABSENT | CACHED | PURGED
  transcript_path TEXT,
  current_run_id TEXT,
  delivery TEXT NOT NULL DEFAULT 'NOT_STARTED',      -- NOT_STARTED | BLOCKED | DELIVERING | DELIVERED | STALE | FAILED
  delivery_reason TEXT,
  notion_page_id TEXT,
  notion_url TEXT,
  notion_synced_run TEXT,
  package_path TEXT,
  review TEXT NOT NULL DEFAULT 'UNREVIEWED',          -- UNREVIEWED | IN_REVIEW | APPROVED | CHANGES_REQUESTED | REJECTED
  review_notes TEXT,
  review_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  purged_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  kind TEXT NOT NULL DEFAULT 'PRODUCTION',             -- PRODUCTION | DISCOVERY | IMPORT
  playbook_id INTEGER REFERENCES playbooks(id),
  production TEXT NOT NULL,                            -- QUEUED RUNNING WAITING INTERRUPTED FAILED COMPLETE COMPLETE_PARTIAL CANCELLED
  wait_reason TEXT,
  stage TEXT,
  error_class TEXT,
  error TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  pgid INTEGER,
  pins TEXT,                                           -- JSON: playbook, policy, pipeline, model, agy version
  summary TEXT,                                        -- JSON stats
  revision_of TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(production, priority DESC, created_at);
CREATE TABLE IF NOT EXISTS stages (
  run_id TEXT NOT NULL REFERENCES runs(id),
  name TEXT NOT NULL,
  ord INTEGER NOT NULL,
  status TEXT NOT NULL,                                -- PENDING RUNNING DONE SKIPPED WARN FAILED
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
  agy_version TEXT,
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
CREATE TABLE IF NOT EXISTS speakers (
  source_id TEXT NOT NULL REFERENCES sources(id),
  run_id TEXT NOT NULL,
  key TEXT NOT NULL,
  display_name TEXT,
  role TEXT,
  confidence TEXT,
  evidence TEXT,
  PRIMARY KEY (run_id, key)
);
CREATE TABLE IF NOT EXISTS units (
  id TEXT PRIMARY KEY,                                 -- ULID
  run_id TEXT NOT NULL REFERENCES runs(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  label TEXT NOT NULL,
  ord INTEGER NOT NULL,
  title TEXT NOT NULL,
  start REAL NOT NULL,
  end REAL NOT NULL,
  learning_priority INTEGER NOT NULL DEFAULT 2,
  knowledge_type TEXT,
  data TEXT NOT NULL,                                  -- full KU JSON (frozen)
  overlay TEXT,                                        -- user minor edits
  decision TEXT,                                       -- NULL | APPROVED | REJECTED
  note TEXT,
  UNIQUE(run_id, label)
);
CREATE TABLE IF NOT EXISTS sections (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  ord INTEGER NOT NULL,
  kind TEXT NOT NULL,                                  -- INTRO | UNIT | OUTRO
  unit_ids TEXT,                                       -- JSON array of unit ULIDs
  markdown TEXT NOT NULL,
  overlay TEXT,
  rev INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  source_id TEXT NOT NULL,
  unit_id TEXT,
  kind TEXT NOT NULL,                                  -- screenshot clip clip_subtitled srt ass poster
  rev INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,                                -- GENERATED WARNING FAILED NOT_SELECTED
  path TEXT,
  sha256 TEXT,
  bytes INTEGER,
  start REAL,
  end REAL,
  selection TEXT,                                      -- SELECT REJECT CONTEXT_DEPENDENT
  publication_priority TEXT,                           -- P1 P2 P3 ARCHIVE
  embed INTEGER NOT NULL DEFAULT 1,
  standalone INTEGER NOT NULL DEFAULT 1,
  decision TEXT,
  meta TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_run ON assets(run_id, unit_id, kind);
CREATE TABLE IF NOT EXISTS flags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  target_kind TEXT NOT NULL,                           -- RUN UNIT ASSET SECTION
  target_id TEXT,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL,                              -- BLOCKER REVIEW INFO
  title TEXT NOT NULL,
  body TEXT,
  evidence TEXT,                                       -- JSON {t, quote, asr}
  origin TEXT NOT NULL,                                -- MACHINE_QA PROVIDER AI_CHECK USER
  resolution TEXT NOT NULL DEFAULT 'OPEN',             -- OPEN ACCEPTED FIXED DISMISSED
  resolution_note TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  key TEXT PRIMARY KEY,
  severity TEXT NOT NULL,                              -- critical | warning | info
  title TEXT NOT NULL,
  body TEXT,
  action TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                                  -- CONFIRM_PLAYBOOK | TRACK_CHANNEL
  ref TEXT,
  title TEXT NOT NULL,
  body TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  outcome TEXT
);
CREATE TABLE IF NOT EXISTS notion_blocks (
  source_id TEXT NOT NULL,
  block_key TEXT NOT NULL,
  block_id TEXT NOT NULL,
  hash TEXT NOT NULL,
  PRIMARY KEY (source_id, block_key)
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
        _migrate(conn)
        _local.conn, _local.path = conn, path
    return conn



def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created before the column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if "headline" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN headline TEXT")


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


def raise_alert(key: str, severity: str, title: str, body: str = "", action: dict | None = None) -> None:
    exec("""INSERT INTO alerts(key, severity, title, body, action, created_at) VALUES (?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET severity=excluded.severity, title=excluded.title, body=excluded.body,
            action=excluded.action, resolved_at=NULL""",
         key, severity, title, body, j(action) if action else None, now())


def resolve_alert(key: str) -> None:
    exec("UPDATE alerts SET resolved_at=? WHERE key=? AND resolved_at IS NULL", now(), key)


def setting(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM settings WHERE key=?", key)
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    exec("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, j(value))


def flag(source_id: str, run_id: str, target_kind: str, target_id: str | None, kind: str, severity: str,
         title: str, body: str = "", evidence: Any = None, origin: str = "MACHINE_QA") -> int:
    return exec("""INSERT INTO flags(source_id, run_id, target_kind, target_id, kind, severity, title, body, evidence,
                   origin, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                source_id, run_id, target_kind, target_id, kind, severity, title, body,
                j(evidence) if evidence is not None else None, origin, now())


def decision(kind: str, ref: str, title: str, body: str = "") -> None:
    if one("SELECT id FROM decisions WHERE kind=? AND ref=? AND resolved_at IS NULL", kind, ref):
        return
    exec("INSERT INTO decisions(kind, ref, title, body, created_at) VALUES (?,?,?,?,?)", kind, ref, title, body, now())
