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
  delivery_policy TEXT NOT NULL DEFAULT 'auto',       -- superseded by stage (design §R2-5); kept for old databases
  stage TEXT NOT NULL DEFAULT 'TUNING',               -- TUNING | LOCKED (design §R2-1)
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
  adopt_pinned INTEGER NOT NULL DEFAULT 0,          -- 调试期：用户钉住的版本，新跑完的运行不替换它
  lock_pinned INTEGER NOT NULL DEFAULT 0,           -- 这个钉是定稿单钉的，不是用户：回到调试期时清掉
  deliver_after_run TEXT,                            -- 定稿时选了「等跑完」：这次运行结束前不交付（delivery=HELD）
  delivery TEXT NOT NULL DEFAULT 'NOT_STARTED',      -- NOT_STARTED | BLOCKED | DELIVERING | DELIVERED | STALE | FAILED | REGISTERED | HELD
  delivery_reason TEXT,
  delivered_at TEXT,
  notion_page_id TEXT,
  notion_url TEXT,
  notion_synced_run TEXT,
  notion_database_id TEXT,                             -- where notion_page_id lives (channel targets can move)
  notion_icon TEXT,                                    -- the channel avatar that page carries (channel_icons)
  package_path TEXT,
  review TEXT NOT NULL DEFAULT 'UNREVIEWED',          -- legacy per-issue review; no longer drives UI or delivery (design §R2-4)
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
  origin TEXT,                                         -- manual | ingest | backfill | trial | lock（定稿单排入或等着的）(NULL: before 2026-10-28)
  attempts INTEGER NOT NULL DEFAULT 0,
  pgid INTEGER,
  stop_at TEXT,                                        -- 清空 asked this trial to stop (a10): the worker stops it
  retry TEXT,                                          -- JSON: the call being tried again, or what was tried before it failed
  pins TEXT,                                           -- JSON: playbook, policy, pipeline, model, agy version
  summary TEXT,                                        -- JSON stats
  revision_of TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(production, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS runs_source ON runs(source_id);
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
CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls(run_id);
CREATE TABLE IF NOT EXISTS retries (                   -- a model call that was tried again, once it came to its end (llm.LLM)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  call TEXT NOT NULL,
  kind TEXT NOT NULL,                                  -- what failed it: a key of knobs.RETRY_DEFAULTS
  tries INTEGER NOT NULL,
  rescued INTEGER NOT NULL,                            -- 1: it got through in the end
  fallback TEXT,                                       -- the other model the safety filter sent it to
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS retries_when ON retries(created_at);
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
CREATE INDEX IF NOT EXISTS sections_run ON sections(run_id, ord);  -- the 调试台 matrix asks per run: no scan of them all
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
CREATE INDEX IF NOT EXISTS flags_run ON flags(run_id);
CREATE TABLE IF NOT EXISTS alerts (
  key TEXT PRIMARY KEY,
  severity TEXT NOT NULL,                              -- critical | warning | info
  title TEXT NOT NULL,
  body TEXT,
  action TEXT,
  created_at TEXT NOT NULL,                            -- when this occurrence began
  resolved_at TEXT,
  notified_at TEXT                                     -- pushed to Telegram (non-urgent ones only once 10 minutes old)
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
CREATE TABLE IF NOT EXISTS samples (                 -- 频道调试台: the videos a channel is tuned on
  channel_id TEXT NOT NULL REFERENCES channels(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  added_at TEXT NOT NULL,
  PRIMARY KEY (channel_id, source_id)
);
CREATE TABLE IF NOT EXISTS trial_queue (             -- 调试台: trial runs waiting their turn (a video has one active run at a time)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  playbook_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',              -- PENDING | DISPATCHED | CANCELLED
  run_id TEXT,
  created_at TEXT NOT NULL,
  dispatched_at TEXT
);
CREATE TABLE IF NOT EXISTS backfill_queue (          -- 补录: older videos of a locked channel, produced one at a time when idle
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT,
  duration REAL,
  status TEXT NOT NULL DEFAULT 'PENDING',              -- PENDING | DISPATCHED | SKIPPED | CANCELLED
  run_id TEXT,
  created_at TEXT NOT NULL,
  dispatched_at TEXT
);
CREATE TABLE IF NOT EXISTS run_votes (               -- 调试台: the user's preference for one version of a sample
  run_id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  liked INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  updated_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notify_outbox (           -- Telegram messages it could not take yet: sent as one later
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (                  -- 专题：一个问题和一批视频（design §R8），不进刊、不写 Notion
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  notes TEXT,                                          -- 要什么、不要什么
  rev INTEGER NOT NULL DEFAULT 1,                      -- 问题改过几次：改了就按新问题从看计划起重读
  last_no INTEGER NOT NULL DEFAULT 0,                  -- 给过的最大期号：回答按 V 号引用，移出的号不再给别的视频
  paused INTEGER NOT NULL DEFAULT 0,
  synth TEXT,                                          -- JSON: the latest 综合
  synth_state TEXT,                                    -- NULL | QUEUED | RUNNING | DONE | FAILED
  synth_error TEXT,
  synth_of TEXT,                                       -- JSON: what the 综合 was made from (topics.run.signature)
  synth_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (                  -- 专题的视频：媒体和中立底稿按视频存，几个专题共用
  id TEXT PRIMARY KEY,                                 -- youtube video id
  url TEXT NOT NULL,
  title TEXT,
  channel TEXT,
  duration REAL,
  published_at TEXT,
  info TEXT,
  media_path TEXT,
  media_height INTEGER,
  media_state TEXT NOT NULL DEFAULT 'ABSENT',          -- ABSENT | CACHED | PURGED
  draft TEXT,                                          -- JSON: what the 底稿 holds (language, segments, frames), once made
  used_at TEXT,                                        -- last read for a topic: media kept `retention_days` after it
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_videos (
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL REFERENCES videos(id),
  no INTEGER NOT NULL,                                 -- 第几期 in the topic, what the 综合 cites
  state TEXT NOT NULL,                                 -- QUEUED RUNNING WAITING DONE FAILED
  step TEXT,                                           -- topics.run.STEPS
  progress TEXT,                                       -- JSON: {"done", "of"} of the step's calls
  wait_reason TEXT,
  error_class TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 1,                      -- the topic's rev this read is for
  done_rev INTEGER,                                    -- rev of the notes shown (an older one while a new read runs)
  fit TEXT,                                            -- high | some | none: how much it answers the question
  gist TEXT,                                           -- one line: what it has for the question
  excluded INTEGER NOT NULL DEFAULT 0,                 -- the user: leave it out of the 综合
  force INTEGER NOT NULL DEFAULT 0,                    -- read it through even though the plan found it off the question
  added_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  PRIMARY KEY (topic_id, video_id)
);
CREATE INDEX IF NOT EXISTS topic_videos_state ON topic_videos(state);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    path = str(config.get().db_path)
    if conn is None or getattr(_local, "path", None) != path:
        config.get().ensure_dirs()
        # the database lives on prodbox's spinning disk: with 16 runs encoding at once a commit waited out a
        # 30 s lock and failed two runs mid-ACQUIRE (2026-10-26); a write now waits up to two minutes
        conn = sqlite3.connect(path, timeout=120, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=120000")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _local.conn, _local.path = conn, path
    return conn



def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created before the column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if "headline" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN headline TEXT")
    if "delivered_at" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN delivered_at TEXT")
        conn.execute("UPDATE sources SET delivered_at=updated_at WHERE delivery='DELIVERED'")
    if "adopt_pinned" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN adopt_pinned INTEGER NOT NULL DEFAULT 0")
    if "lock_pinned" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN lock_pinned INTEGER NOT NULL DEFAULT 0")
        # until now 定稿's pins looked like the user's: on a locked channel a pin is taken for 定稿's (a hand-pinned one
        # too, which only comes up if the channel goes back to 调试期)
        conn.execute("UPDATE sources SET lock_pinned=1 WHERE adopt_pinned=1 AND channel_id IN (SELECT id FROM channels WHERE stage='LOCKED')")
    if "deliver_after_run" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN deliver_after_run TEXT")
    if "notion_icon" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN notion_icon TEXT")
    if "notion_database_id" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN notion_database_id TEXT")
        r = conn.execute("SELECT value FROM settings WHERE key='notion_database_id'").fetchone()
        if r and json.loads(r[0]):  # pages delivered so far live in the Assetizer database
            conn.execute("UPDATE sources SET notion_database_id=? WHERE notion_page_id IS NOT NULL", (json.loads(r[0]),))
    runs = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "origin" not in runs:
        conn.execute("ALTER TABLE runs ADD COLUMN origin TEXT")
    if "stop_at" not in runs:
        conn.execute("ALTER TABLE runs ADD COLUMN stop_at TEXT")
    if "retry" not in runs:
        conn.execute("ALTER TABLE runs ADD COLUMN retry TEXT")
        # the filter's failures read as the model's, or as an answer that did not pass, until it had a class of its own
        conn.execute("""UPDATE runs SET error_class='CONTENT_SAFETY' WHERE (error_class='PROVIDER_FAILURE'
                        AND lower(error) LIKE '%safety filter%') OR error_class='PROVIDER_SAFETY_BLOCK'""")
    if "notified_at" not in {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}:
        conn.execute("ALTER TABLE alerts ADD COLUMN notified_at TEXT")
        conn.execute("UPDATE alerts SET notified_at=created_at")  # raised before: pushed already
    if "stage" not in {r[1] for r in conn.execute("PRAGMA table_info(channels)")}:
        conn.execute("ALTER TABLE channels ADD COLUMN stage TEXT NOT NULL DEFAULT 'TUNING'")
        # a channel whose playbook is already confirmed has been through tuning
        conn.execute("""UPDATE channels SET stage='LOCKED'
                        WHERE id IN (SELECT channel_id FROM playbooks WHERE status='ACTIVE' AND channel_id IS NOT NULL)""")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='trial_queue_pending'").fetchone():
        # a version of a sample is queued once (two overlapping requests put it in twice): the doubles go first
        conn.execute("""UPDATE trial_queue SET status='CANCELLED' WHERE status='PENDING' AND id NOT IN
                        (SELECT MIN(id) FROM trial_queue WHERE status='PENDING' GROUP BY source_id, playbook_id)""")
        conn.execute("CREATE UNIQUE INDEX trial_queue_pending ON trial_queue(source_id, playbook_id) WHERE status='PENDING'")
    if not conn.execute("SELECT 1 FROM settings WHERE key='migrated:not_available'").fetchone():
        # "This video is not available." (a members tier above the account's) read as 未分类 until 2026-11-10: 会员权限不够
        conn.execute("""UPDATE runs SET error_class='VIDEO_MEMBERS' WHERE production='FAILED' AND error_class='UNKNOWN'
                        AND lower(error) LIKE '%this video is not available%'""")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('migrated:not_available', 'true')")


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
    from . import notify
    was_open = one("SELECT 1 FROM alerts WHERE key=? AND resolved_at IS NULL", key)
    # one that came back after it was resolved is a new occurrence: its own start, not yet pushed
    exec("""INSERT INTO alerts(key, severity, title, body, action, created_at) VALUES (?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET severity=excluded.severity, title=excluded.title, body=excluded.body,
            action=excluded.action,
            created_at=CASE WHEN alerts.resolved_at IS NULL THEN alerts.created_at ELSE excluded.created_at END,
            notified_at=CASE WHEN alerts.resolved_at IS NULL THEN alerts.notified_at END, resolved_at=NULL""",
         key, severity, title, body, j(action) if action else None, now())
    if not was_open and key in notify.URGENT:  # re-raised on every retry: told once per occurrence; the rest after a wait
        notify.alert_raised(severity, title, body)
        exec("UPDATE alerts SET notified_at=? WHERE key=?", now(), key)


def resolve_alert(key: str) -> None:
    from . import notify
    a = one("SELECT title, notified_at FROM alerts WHERE key=? AND resolved_at IS NULL", key)
    exec("UPDATE alerts SET resolved_at=? WHERE key=? AND resolved_at IS NULL", now(), key)
    if a and a["notified_at"]:  # one never pushed (it passed within its wait) is not followed by a recovery either
        notify.alert_resolved(a["title"])


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
