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
  notion_synced_template TEXT,
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
  run_id TEXT,
  template TEXT,
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
CREATE TABLE IF NOT EXISTS topics (                  -- 专题：一个问题和一批视频（design §R8），不进刊；可另存一份到 Notion
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backup INTEGER CHECK (backup IN (0,1)),              -- NULL 待定；没有默认选择
  question TEXT NOT NULL,
  notes TEXT,                                          -- 要什么、不要什么
  rev INTEGER NOT NULL DEFAULT 1,                      -- 问题改过几次：改了就按新问题从看计划起重读
  last_no INTEGER NOT NULL DEFAULT 0,                  -- 给过的最大期号：回答按 V 号引用，移出的号不再给别的视频
  paused INTEGER NOT NULL DEFAULT 0,
  batch TEXT,                                          -- 扩展选片篮送来的批次：同一批再送一次就打开这个专题，不再新建
  synth TEXT,                                          -- JSON: the latest 综合
  synth_state TEXT,                                    -- NULL | QUEUED | RUNNING | DONE | FAILED
  synth_error TEXT,
  synth_of TEXT,                                       -- JSON: what the 综合 was made from (topics.run.signature)
  synth_at TEXT,
  notion_target TEXT,                                  -- JSON {kind: page|database, id, title}: 存到 Notion 的哪里（NULL 不存）
  notion_page_id TEXT,                                 -- 写过的那页：之后只认它（在 Notion 里挪了照样更新）
  notion_state TEXT,                                   -- NULL | QUEUED | RUNNING | DONE | FAILED
  notion_error TEXT,                                   -- JSON {kind: TOKEN|NO_ACCESS|GONE|OTHER|YOURS, message, found}
  notion_at TEXT,                                      -- 写进去的那次综合的 synth_at
  notion_db TEXT,                                      -- 那页里的来源表（Notion 数据库）id
  notion_rows TEXT,                                    -- JSON {video id: {page 那一行的页 id, want/props 写过的列（认出你改过的）, sig 写过的笔记}}
  marks TEXT,                                          -- JSON {论断 id: {status, why, at}}：用户淘汰的说法、结算的预测（design §R8-10）
  reminded TEXT,                                       -- JSON [论断 id]：到期提醒过的预测
  watch TEXT,                                          -- JSON {channels: [频道], words: [关键词]}：盯着的频道和关键词（design §R8-11）
  watched_at TEXT,                                     -- 上次看有没有新视频
  archived_at TEXT,                                    -- 归档：结论定格，不再读、不再综合、不再盯、不再推
  archived_how TEXT,                                   -- 归档时写的：应验 | 落空 | 部分应验 | 不判
  archived_why TEXT,                                   -- 归档时写的一句话（可不写）
  comments_at TEXT,                                    -- 上次读 Notion 页上的批注（design §R8-13）
  comments_err TEXT,                                   -- NO_READ（integration 没开读评论）| NO_INSERT（没开插入评论，回复不了）
  replies TEXT,                                        -- JSON [{discussion, text, n}]：还没在 Notion 讨论串里发出去的回复
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notion_events (           -- webhook metadata only: never a body, signature or token
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  ts TEXT,
  type TEXT NOT NULL,
  page TEXT,
  parent TEXT,
  parent_type TEXT,
  entity TEXT,
  bots TEXT NOT NULL,                                 -- JSON: original authors [{id, type}]
  done TEXT,
  outcome TEXT,                                      -- topic | row | channel | ours | unknown | error
  topic_id INTEGER,
  tries INTEGER NOT NULL DEFAULT 0,
  retry_at TEXT
);
CREATE INDEX IF NOT EXISTS notion_events_pending ON notion_events(at) WHERE done IS NULL;
CREATE TABLE IF NOT EXISTS topic_comments (          -- Notion 页上的批注：用户的意见交给下一次综合，在原讨论串里回复（design §R8-13）
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  id TEXT NOT NULL,                                    -- Notion 的评论 id
  discussion TEXT NOT NULL,                            -- 讨论串
  page TEXT NOT NULL,                                  -- 在哪一页：专题页，或来源表的一行
  block TEXT,                                          -- 在哪一块（NULL：整页的讨论）
  video_id TEXT,                                       -- 来源表那一行的视频（NULL：专题页）
  quote TEXT,                                          -- 那一块当时的字
  under TEXT,                                          -- 那一块所在的一节（上面最近的标题）
  kind TEXT,                                           -- 那一块的类型
  text TEXT NOT NULL,
  hash TEXT NOT NULL,                                  -- 改过的批注算新的
  ours INTEGER NOT NULL DEFAULT 0,                     -- 课代表的回复
  asked INTEGER NOT NULL DEFAULT 0,                    -- 以「课代表」开头：点名课代表（它在的讨论串才处理）
  created TEXT,
  open INTEGER NOT NULL DEFAULT 1,                     -- 上次读时还在（解决了、删了的 Notion 不再给）
  seen INTEGER,                                        -- 收进了第几版（NULL：还没有）
  PRIMARY KEY (topic_id, id)
);
CREATE TABLE IF NOT EXISTS asks (                    -- 点名课代表的批注：用了几次、怎么处理的；记进待修的是待修清单（design §R8-13）
  n INTEGER PRIMARY KEY AUTOINCREMENT,                 -- 回复和待修清单里的编号
  id TEXT NOT NULL UNIQUE,                             -- Notion 的评论 id
  at TEXT NOT NULL,                                    -- 课代表读到它的时候
  topic_id INTEGER,                                    -- 哪个专题（专题删了也留着：这是用量）
  source_id TEXT,                                      -- 哪一期频道页（与专题共用编号）
  discussion TEXT NOT NULL,
  text TEXT NOT NULL,
  result TEXT,                                         -- 已改 | 没改 | 已答 | 记进待修（NULL：还没处理）
  reply TEXT,                                          -- 回复的话
  answered_at TEXT,                                    -- 回复发进 Notion 的时候
  fixed TEXT,                                          -- 记进待修的：在哪一版修好（开发用 asks --fixed 记）
  fixed_told TEXT                                      -- 「已修」回复发进 Notion 的时候
);
CREATE TABLE IF NOT EXISTS issue_comments (          -- 频道页的定点读；不做整页扫描（design §R3-12）
  source_id TEXT NOT NULL,
  id TEXT NOT NULL,
  discussion TEXT NOT NULL,
  page TEXT NOT NULL,
  block TEXT,
  plan_key TEXT,
  quote TEXT,
  kind TEXT,
  owned INTEGER NOT NULL DEFAULT 0,                   -- 被批注块的作者是认过的课代表身份
  depth INTEGER NOT NULL DEFAULT 0,                   -- 到计划块的父层数（0、1、2）
  text TEXT NOT NULL,
  hash TEXT NOT NULL,
  ours INTEGER NOT NULL DEFAULT 0,
  asked INTEGER NOT NULL DEFAULT 0,
  created TEXT,
  touched TEXT NOT NULL,                              -- 新话或编辑的时间，QUIET 用它
  noticed TEXT NOT NULL,                              -- 读到新话的时间：失败后新到的事件可重新处理
  open INTEGER NOT NULL DEFAULT 1,
  seen INTEGER,                                      -- issue_batches.id
  PRIMARY KEY (source_id, id)
);
CREATE INDEX IF NOT EXISTS issue_comments_page ON issue_comments(source_id, page, open);
CREATE INDEX IF NOT EXISTS issue_comments_asked ON issue_comments(source_id, page, discussion, asked, open);
CREATE TABLE IF NOT EXISTS issue_positions (         -- one durable identity per discussion; never replace its original with a corrected quote
  source_id TEXT NOT NULL,
  discussion TEXT NOT NULL,
  run_id TEXT,
  template TEXT NOT NULL,
  plan_key TEXT NOT NULL,
  part TEXT,                                         -- NULL until the descendant is resolved
  section_id TEXT NOT NULL,
  original TEXT,
  PRIMARY KEY (source_id, discussion)
);
CREATE TABLE IF NOT EXISTS issue_state (
  source_id TEXT PRIMARY KEY,
  failed_at TEXT,
  error TEXT                                         -- NO_READ | NO_INSERT | FAILED（不存模型原文或异常）
);
CREATE TABLE IF NOT EXISTS issue_batches (           -- 冻结输入和模型结果；恢复时不拿新话改旧请求
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  page TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT,
  state TEXT NOT NULL DEFAULT 'MODEL',                -- MODEL | APPLY | DONE | FAILED
  apply_error INTEGER NOT NULL DEFAULT 0,             -- accepted output: warn once, retry the transaction
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_fixes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  plan_key TEXT NOT NULL,
  part TEXT NOT NULL,                                -- JSON child path; position is only a hint
  original TEXT NOT NULL,                            -- plain text in the uncorrected template
  original_hash TEXT NOT NULL,
  template TEXT NOT NULL,
  text TEXT NOT NULL,                                -- inline markdown, never a whole block
  section_id TEXT NOT NULL,
  readback INTEGER NOT NULL DEFAULT 0,                -- actually included in the latest completed delivery
  ask_id TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_replies (
  batch_id INTEGER NOT NULL,
  discussion TEXT NOT NULL,
  source_id TEXT NOT NULL,
  page TEXT NOT NULL,
  head TEXT NOT NULL,
  body TEXT NOT NULL,
  cids TEXT NOT NULL,                                -- 本次接纳的评论 id/hash
  fix_id INTEGER,                                   -- 等这条改正的交付读回；NULL 可立即回复
  version INTEGER NOT NULL DEFAULT 0,                -- CAS guards both claim and completion against concurrent invalidation
  void_of INTEGER,                                  -- correction invalidation notice, unique per thread
  ready INTEGER NOT NULL DEFAULT 0,
  sending INTEGER NOT NULL DEFAULT 0,                -- POST 前记账；中断后先定点读回去重
  tried_at TEXT,
  done TEXT,
  PRIMARY KEY (batch_id, discussion)
);
CREATE UNIQUE INDEX IF NOT EXISTS issue_void_once ON issue_replies(void_of, discussion) WHERE void_of IS NOT NULL;
CREATE TABLE IF NOT EXISTS topic_versions (          -- 每次综合留一版：回答底部的版本记录，下一次综合拿上一版对照
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  n INTEGER NOT NULL,
  at TEXT NOT NULL,
  read INTEGER NOT NULL,                               -- 用了几期
  total INTEGER NOT NULL,                              -- 当时专题里算数的有几期
  synth TEXT NOT NULL,                                 -- JSON: 这一版的综合
  PRIMARY KEY (topic_id, n)
);
CREATE TABLE IF NOT EXISTS topic_candidates (        -- 盯着的频道、关键词里冒出来的新视频：问「要加吗」，不自动加
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL,
  title TEXT,
  channel TEXT,
  duration REAL,
  published TEXT,
  why TEXT,                                            -- 从哪里来的：频道名或关键词
  state TEXT NOT NULL DEFAULT 'NEW',                   -- NEW（问要不要加）| ADDED | DROPPED | SEEN（盯上时就有的、不相关的：不问）
  found_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, video_id)
);
CREATE TABLE IF NOT EXISTS videos (                  -- 专题的视频：媒体和中立底稿按视频存，几个专题共用
  id TEXT PRIMARY KEY,                                 -- youtube video id
  youtube_channel_id TEXT,
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
  gone TEXT,                                           -- YouTube 那边没了（VIDEO_REMOVED、VIDEO_PRIVATE、VIDEO_MEMBERS…）：重读只用底稿和留下的帧
  gone_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_authors (
  id TEXT PRIMARY KEY,                                -- YouTube channel ID，名字不参与身份判断
  name TEXT,
  member INTEGER CHECK (member IN (0,1)),
  public INTEGER CHECK (public IN (0,1))
);
CREATE TABLE IF NOT EXISTS video_backups (            -- 两条线共用；不引用本地行，清理本地后仍保留规则依据与用量
  id TEXT PRIMARY KEY,
  youtube_channel_id TEXT,
  author_name TEXT,
  member INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  gone TEXT,
  backup_due INTEGER NOT NULL DEFAULT 0,
  backup_gen INTEGER NOT NULL DEFAULT 0,               -- 排队代次：旧上传不能确认后来追加的内容
  backup_at TEXT,                                       -- 原片在网盘核对过的时间、大小和文件夹
  backup_bytes INTEGER,
  backup_md5 TEXT,
  backup_draft_bytes INTEGER NOT NULL DEFAULT 0,
  delete_intent TEXT,                                  -- purge 前持久化；未决时不能信任确认，身份只存摘要
  backup_folder TEXT,
  backup_draft_at TEXT,                                 -- 底稿最后一次传完
  backup_draft_folder TEXT,                             -- 底稿只认当前网盘文件夹
  backup_bad_md5 TEXT,                                  -- 已知不一致的远端原片，用户处理前不能确认备份
  backup_src_md5 TEXT,                                  -- 上传前记下的原片 md5，待跨轮核对
  backup_error TEXT,                                    -- 上次失败（脱敏），稍后再试
  backup_failed_at TEXT,
  backup_missed_at TEXT,                                -- 原片没备份就被清理了
  restored_at TEXT,                                     -- 最后从网盘取回原片
  draft_restored_at TEXT                                -- 最后取回底稿，不代表原片可回看
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
  excluded_why TEXT,                                   -- 不算这期的原因：离题 | 质量差 | 转述别人
  wait_since TEXT,                                     -- 第一次在等的时刻：等太久（12 小时）就先综合别的期
  card TEXT,                                           -- JSON {interests, relays}：笔记记下的利益相关、转述的来源
  added_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  PRIMARY KEY (topic_id, video_id)
);
CREATE INDEX IF NOT EXISTS topic_videos_state ON topic_videos(state);
CREATE TABLE IF NOT EXISTS topic_removed (            -- 移出专题的视频：7 天内能撤销，加回来还是原来的期号和笔记
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL,
  item TEXT NOT NULL,                                  -- JSON: its topic_videos row as it was
  removed_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, video_id)
);
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
        _migrate(conn)
        _local.conn, _local.path = conn, path
    return conn



def _migrate(conn: sqlite3.Connection) -> None:
    """写锁内检查、建表、加列、搬迁、删列；任何一步失败都回到原结构。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        statement = ""
        for line in SCHEMA.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ""
        _migrate_locked(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate_locked(conn: sqlite3.Connection) -> None:
    block_cols = {r[1] for r in conn.execute("PRAGMA table_info(notion_blocks)")}
    for name in ("run_id", "template"):
        if name not in block_cols:
            conn.execute(f"ALTER TABLE notion_blocks ADD COLUMN {name} TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if "notion_synced_template" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN notion_synced_template TEXT")
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
    # 2026-11-10: channel pages sync in place; old whole-page replacement decisions are obsolete.
    conn.execute("UPDATE sources SET delivery='STALE', delivery_reason=NULL WHERE delivery='HELD' AND delivery_reason='YOURS_ON_PAGE'")
    conn.execute("DELETE FROM settings WHERE key GLOB 'replace_anyway:*' OR key GLOB 'yours_on_page:*'")
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
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(topics)")}
    if "batch" not in tcols:
        conn.execute("ALTER TABLE topics ADD COLUMN batch TEXT")
    for c in ("notion_target", "notion_page_id", "notion_state", "notion_error", "notion_at"):
        if c not in tcols:  # topics made before 0.4.40 are 不存
            conn.execute(f"ALTER TABLE topics ADD COLUMN {c} TEXT")
    for c in ("notion_db", "notion_rows", "marks", "reminded", "watch", "watched_at", "archived_at", "archived_how", "archived_why",
              "comments_at", "comments_err", "replies"):
        if c not in tcols:  # 0.4.41: claims, versions, watching, archive, the source table, comments
            conn.execute(f"ALTER TABLE topics ADD COLUMN {c} TEXT")
    if "asked" not in {r[1] for r in conn.execute("PRAGMA table_info(topic_comments)")}:
        conn.execute("ALTER TABLE topic_comments ADD COLUMN asked INTEGER NOT NULL DEFAULT 0")
    if "source_id" not in {r[1] for r in conn.execute("PRAGMA table_info(asks)")}:
        conn.execute("ALTER TABLE asks ADD COLUMN source_id TEXT")
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(notion_events)")}
    for column, kind in {"tries": "INTEGER NOT NULL DEFAULT 0", "retry_at": "TEXT"}.items():
        if column not in ecols:
            conn.execute(f"ALTER TABLE notion_events ADD COLUMN {column} {kind}")
    # 0.4.41: a page someone changed is written around what they wrote, not held (留着 / 另写一页 are gone); a page held
    # is as it was, its 更新 on the topic's page
    conn.execute("UPDATE topics SET notion_state='DONE' WHERE notion_state='HELD'")
    conn.execute("UPDATE topics SET notion_state='QUEUED' WHERE notion_state='NEW'")
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(topic_videos)")}
    for c in ("excluded_why", "wait_since", "card"):
        if c not in vcols:
            conn.execute(f"ALTER TABLE topic_videos ADD COLUMN {c} TEXT")
    vidcols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    for c in ("gone", "gone_at"):
        if c not in vidcols:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {c} TEXT")
    if "backup" not in tcols:
        conn.execute("ALTER TABLE topics ADD COLUMN backup INTEGER CHECK (backup IN (0,1))")
    if "youtube_channel_id" not in vidcols:
        conn.execute("ALTER TABLE videos ADD COLUMN youtube_channel_id TEXT")
    bcols = {r[1] for r in conn.execute("PRAGMA table_info(video_backups)")}
    for c, kind in {"backup_gen": "INTEGER NOT NULL DEFAULT 0", "backup_bad_md5": "TEXT", "backup_draft_folder": "TEXT",
                    "draft_restored_at": "TEXT", "backup_src_md5": "TEXT", "backup_md5": "TEXT",
                    "backup_draft_bytes": "INTEGER NOT NULL DEFAULT 0", "delete_intent": "TEXT"}.items():
        if c not in bcols:
            conn.execute(f"ALTER TABLE video_backups ADD COLUMN {c} {kind}")
    _backup_migrate(conn)
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


def _backup_migrate(conn: sqlite3.Connection) -> None:
    """一次性原子迁移：预填只针对当时存在的专题、频道；旧状态与 inventory 合流后删除旧列。"""
    from .notion.mapping import access_kind
    if not conn.execute("SELECT 1 FROM settings WHERE key='migrated:backup_rules_v1'").fetchone():
        conn.execute("UPDATE topics SET backup=1")
        conn.execute("""INSERT INTO backup_authors(id,name,member,public)
                        SELECT youtube_channel_id,name,1,1 FROM channels WHERE youtube_channel_id IS NOT NULL
                        ON CONFLICT(id) DO NOTHING""")
        oldcols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
        statecols = {r[1] for r in conn.execute("PRAGMA table_info(video_backups)")}
        legacy = sorted((oldcols & statecols) - {"id", "title", "youtube_channel_id", "gone"})
        for table in ("videos", "sources"):
            for r in list(conn.execute(f"SELECT * FROM {table}")):
                v = dict(r)
                info = json.loads(v.get("info") or "{}")
                if table == "sources":
                    topic_info = conn.execute("SELECT info FROM videos WHERE id=?", (v["id"],)).fetchone()
                    if topic_info and topic_info[0]:
                        info = json.loads(topic_info[0])
                ch = conn.execute("SELECT youtube_channel_id FROM channels WHERE id=?", (v.get("channel_id"),)).fetchone() if table == "sources" else None
                cid = v.get("youtube_channel_id") or info.get("channel_id") or (ch[0] if ch else None)
                name = v.get("channel") or v.get("channel_title") or info.get("channel")
                conn.execute("""INSERT INTO video_backups(id,youtube_channel_id,author_name,member,title,gone)
                                VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                                youtube_channel_id=COALESCE(video_backups.youtube_channel_id,excluded.youtube_channel_id),
                                author_name=COALESCE(video_backups.author_name,excluded.author_name),
                                member=excluded.member,title=COALESCE(video_backups.title,excluded.title)""",
                             (v["id"], cid, name, int(access_kind(info).startswith("member_")), v.get("title"), v.get("gone")))
                if table == "videos":
                    conn.execute("UPDATE videos SET youtube_channel_id=? WHERE id=?", (cid, v["id"]))
                    if legacy:
                        conn.execute("UPDATE video_backups SET " + ",".join(f"{c}=?" for c in legacy) + " WHERE id=?",
                                     (*[v[c] for c in legacy], v["id"]))
                if cid:
                    conn.execute("INSERT INTO backup_authors(id,name) VALUES (?,?) ON CONFLICT(id) DO NOTHING", (cid, name))
        inv = conn.execute("SELECT value FROM settings WHERE key='gdrive_inventory'").fetchone()
        if inv:
            inventory = json.loads(inv[0]) or {}
            for vid, v in inventory.get("videos", {}).items():
                # 旧本地状态（包括 NULL 确认）优先；inventory 仅能补没有旧行的视频。
                if conn.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone():
                    continue
                conn.execute("""INSERT INTO video_backups(id,backup_at,backup_bytes,backup_folder,backup_md5) VALUES (?,?,?,?,?)
                                ON CONFLICT(id) DO UPDATE SET backup_at=COALESCE(video_backups.backup_at,excluded.backup_at),
                                backup_bytes=COALESCE(video_backups.backup_bytes,excluded.backup_bytes),
                                backup_folder=COALESCE(video_backups.backup_folder,excluded.backup_folder),
                                backup_md5=COALESCE(video_backups.backup_md5,excluded.backup_md5)""",
                             (vid, now(), v.get("bytes", 0), inventory.get("folder"), v.get("md5")))
            conn.execute("DELETE FROM settings WHERE key='gdrive_inventory'")
        conn.execute("UPDATE video_backups SET backup_at=NULL WHERE backup_bad_md5 IS NOT NULL OR backup_src_md5 IS NOT NULL")
        for c in sorted(oldcols):
            if c.startswith("backup_") or c in ("restored_at", "draft_restored_at"):
                conn.execute(f"ALTER TABLE videos DROP COLUMN {c}")
        conn.execute("INSERT INTO settings(key,value) VALUES ('migrated:backup_rules_v1','true')")


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """多表规则读取共享同一个快照；已有事务时沿用调用者的快照。"""
    c = connect()
    if c.in_transaction:
        yield c
        return
    c.execute("BEGIN")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """嵌套调用沿用外层事务；提交和回滚由最外层负责。"""
    c = connect()
    if c.in_transaction:
        yield c
        return
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
