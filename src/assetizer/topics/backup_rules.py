"""网盘规则及本地可用内容。唯一判定入口 decision(video_id)，True / False / None。"""
from __future__ import annotations

from pathlib import Path
import contextlib
import fcntl
import functools
import inspect
import os
import secrets
import threading
import time

from .. import config, db, youtube
from ..notion.mapping import access_kind
from . import draft_dir, media_dir

_guard = threading.RLock()
_held = threading.local()
WRITE_WAIT = 3.0


@contextlib.contextmanager
def mutation(*, wait: float | None = None, writer: bool = True):
    """规则写与单期最终删除共用跨进程锁；先于数据库事务，等待有上限。"""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    from . import backup
    wait = WRITE_WAIT if wait is None else wait
    deadline = time.monotonic() + wait
    # 没有网盘冲突状态时只用进程内锁，不产生网盘目录或锁文件。
    shared = bool(backup._folder().get("folder_id") or backup._busy("batch") or
                  db.one("SELECT 1 FROM video_backups WHERE delete_intent IS NOT NULL LIMIT 1"))
    fd = waiter = waiter_fd = None
    acquired = False
    try:
        if shared:
            p = config.get().home / "cache/gdrive-locks/rules.lock"
            p.parent.mkdir(parents=True, exist_ok=True)
            if writer:
                waiter = p.with_name("writer-" + secrets.token_hex(16))
                waiter_fd = os.open(waiter, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                fcntl.flock(waiter_fd, fcntl.LOCK_EX)
            fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = _guard.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            raise FileExistsError("网盘规则正在使用，请稍后重试")
        if fd is not None:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise FileExistsError("网盘规则正在使用，请稍后重试") from None
                    time.sleep(.01)
        _held.depth = 1
        yield
    finally:
        _held.depth = 0
        if fd is not None:
            os.close(fd)
        if acquired:
            _guard.release()
        if waiter is not None:
            waiter.unlink(missing_ok=True)
        if waiter_fd is not None:
            os.close(waiter_fd)


def yield_writers() -> None:
    """单期删除释放规则锁后，让已等候的跨进程写方先完成，最多一秒。"""
    pending = list((config.get().home / "cache/gdrive-locks").glob("writer-*"))
    deadline = time.monotonic() + 1
    def alive(p):
        try:
            with p.open("rb") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                p.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        return False
    while any([alive(p) for p in pending]) and time.monotonic() < deadline:
        time.sleep(.01)


@contextlib.contextmanager
def cleanup(vid: str):
    """调用方已持 media_lock 与 claim；等不到规则锁则只跳过这一期。"""
    if db.one("SELECT 1 FROM video_backups WHERE id=? AND delete_intent IS NOT NULL", vid):
        yield False
        return
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(mutation(wait=0, writer=False))
        except FileExistsError:
            from . import backup
            backup._warn_once(vid, "规则正在使用，跳过本轮本地清理")
            yield False
            return
        yield True


def serialized(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with mutation():
            return fn(*args, **kwargs)
    wrapped.__signature__ = inspect.signature(fn, eval_str=True)
    return wrapped




def metadata(vid: str) -> dict:
    saved = db.one("SELECT * FROM video_backups WHERE id=?", vid) or {"id": vid}
    topic = db.one("SELECT * FROM videos WHERE id=?", vid) or {}
    source = db.one("SELECT * FROM sources WHERE id=?", vid) or {}
    # 专题可能只存选片标题而没有 info；以真正取得的元数据为准。
    info = db.unj(topic.get("info") or source.get("info"), {}) or {}
    channel = db.one("SELECT youtube_channel_id FROM channels WHERE id=?", source.get("channel_id")) or {}
    cid = (topic.get("youtube_channel_id") or source.get("youtube_channel_id") or info.get("channel_id")
           or channel.get("youtube_channel_id") or saved.get("youtube_channel_id"))
    failed = db.one("SELECT error_class FROM runs WHERE source_id=? AND error_class IN (" + ",".join("?" for _ in youtube.UNAVAILABLE) + ") ORDER BY created_at DESC LIMIT 1", vid, *youtube.UNAVAILABLE)
    return {**saved, "id": vid, "youtube_channel_id": cid,
            "author_name": topic.get("channel") or source.get("channel_title") or info.get("channel") or saved.get("author_name"),
            "title": topic.get("title") or source.get("title") or saved.get("title"),
            "member": int(access_kind(info).startswith("member_")) if topic.get("info") or source.get("info") else saved.get("member", 0),
            "gone": topic.get("gone") or saved.get("gone") or (failed["error_class"] if failed else None)}


def track(vid: str, *, gone: str | None = None) -> dict:
    with db.tx():
        return _track(vid, gone=gone)


def _track(vid: str, *, gone: str | None = None) -> dict:
    m = metadata(vid)
    saved = db.one("SELECT * FROM video_backups WHERE id=?", vid)
    facts = ("youtube_channel_id", "author_name", "member", "title", "gone")
    m["gone"] = gone or m["gone"]
    if saved and all(saved[k] == m[k] for k in facts):
        return saved
    db.exec("""INSERT INTO video_backups(id,youtube_channel_id,author_name,member,title,gone) VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET youtube_channel_id=excluded.youtube_channel_id,
               author_name=excluded.author_name, member=excluded.member,title=excluded.title,gone=excluded.gone""",
            vid, m["youtube_channel_id"], m["author_name"], m["member"], m["title"], gone or m["gone"])
    if m["youtube_channel_id"]:
        db.exec("""INSERT INTO backup_authors(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET
                   name=COALESCE(excluded.name,backup_authors.name)""", m["youtube_channel_id"], m["author_name"])
        db.exec("UPDATE videos SET youtube_channel_id=? WHERE id=? AND youtube_channel_id IS NULL", m["youtube_channel_id"], vid)
    return db.one("SELECT * FROM video_backups WHERE id=?", vid)


def sync() -> None:
    with db.tx():
        _sync()


def _sync() -> None:
    for v in db.q("SELECT id FROM videos UNION SELECT id FROM sources"):
        track(v["id"])
    for c in db.q("SELECT youtube_channel_id id,name FROM channels WHERE youtube_channel_id IS NOT NULL"):
        db.exec("""INSERT INTO backup_authors(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name WHERE backup_authors.name IS NOT excluded.name""", c["id"], c["name"])


def applicable(vid: str) -> list[dict]:
    with db.read_tx():
        m = metadata(vid)
        author = db.one("SELECT * FROM backup_authors WHERE id=?", m.get("youtube_channel_id")) or {}
        rules = [{"kind": "author", "id": m.get("youtube_channel_id"), "name": m.get("author_name"),
                  "value": author.get("member" if m.get("member") else "public")}]
        # topic_removed 不在这张表；归档只影响读片，不影响备份规则。
        rules += [{"kind": "topic", "id": t["id"], "question": t["question"], "value": t["backup"]}
                  for t in db.q("""SELECT t.id,t.question,t.backup FROM topics t JOIN topic_videos tv ON tv.topic_id=t.id
                                   WHERE tv.video_id=? ORDER BY t.id""", vid)]
        return rules


def decision(vid: str) -> bool | None:
    """任一传 → 传；否则任一待定 → 待定；否则不传。未知作者始终贡献一条待定。"""
    values = [r["value"] for r in applicable(vid)]
    if any(v == 1 for v in values):
        return True
    return None if any(v is None for v in values) else False


def source_path(vid: str) -> Path | None:
    rows = [db.one("SELECT media_path FROM videos WHERE id=?", vid), db.one("SELECT media_path FROM sources WHERE id=?", vid)]
    paths = [Path(r["media_path"]) for r in rows if r and r["media_path"]]
    paths += [media_dir(vid) / "source.mkv", config.get().media_dir / vid / "source.mkv"]
    return next((p for p in paths if p.is_file()), None)


def queue(vid: str, *, force: bool = False) -> bool:
    from . import backup
    if not backup.available():
        return False
    backup._valid_video(vid)
    if decision(vid) is not True:
        db.exec("UPDATE video_backups SET backup_due=0 WHERE id=? AND backup_due!=0", vid)
        return False
    v = track(vid)
    source = source_path(vid) is not None
    draft = (draft_dir(vid) / "full.md").is_file()
    missing = (source and not backup.verified(vid)) or (draft and not backup.verified(vid, draft=True))
    if (source or draft) and (force or (missing and not v["backup_due"])):
        db.exec("UPDATE video_backups SET backup_due=1,backup_gen=backup_gen+1,backup_failed_at=NULL WHERE id=?", vid)
        return True
    return False


def reconcile(ids: set[str] | None = None) -> None:
    if ids is None:
        sync()
        ids = {v["id"] for v in db.q("SELECT id FROM video_backups")}
    for vid in ids:
        v = {"id": vid}
        try:
            queue(v["id"])
        except ValueError:  # 历史非 YouTube ID 不能成为远端路径
            continue


def _bool(value):
    return None if value is None else bool(value)


def drive(ids: set[str]) -> dict:
    from . import backup
    fid = backup._folder().get("folder_id")
    rows = [r for r in backup._copies() if r["id"] in ids]
    return {"videos": len(rows), "bytes": sum(((r["backup_bytes"] or 0) if r["backup_folder"] == fid else 0) +
                                              ((r["backup_draft_bytes"] or 0) if r["backup_draft_folder"] == fid else 0) for r in rows)}



def view() -> dict:
    from . import backup
    if not backup.available():
        return {"ready": False}
    authors = []
    catalog = {a["id"]: a for a in db.q("SELECT * FROM backup_authors")}
    for c in db.q("SELECT youtube_channel_id id,name FROM channels WHERE youtube_channel_id IS NOT NULL"):
        catalog.setdefault(c["id"], {**c, "member": None, "public": None})
    for a in catalog.values():
        c = db.one("SELECT id,name FROM channels WHERE youtube_channel_id=?", a["id"])
        ids = {r["id"] for r in db.q("SELECT id FROM video_backups WHERE youtube_channel_id=?", a["id"])}
        authors.append({"id": a["id"], "name": (c["name"] if c else a["name"]) or a["id"], "channel": c, "member": _bool(a["member"]),
                        "public": _bool(a["public"]), "videos": len(ids), "drive": drive(ids)})
    authors.sort(key=lambda a: (a["channel"] is None, -a["videos"], a["id"]))
    topics = []
    for t in db.q("SELECT id,question,backup FROM topics ORDER BY id DESC"):
        ids = {r["video_id"] for r in db.q("SELECT video_id FROM topic_videos WHERE topic_id=?", t["id"])}
        topics.append({**t, "backup": _bool(t["backup"]), "videos": len(ids), "drive": drive(ids)})
    return {"ready": True, "authors": authors, "topics": topics}


@serialized
def update(body: dict) -> dict:
    """先验证整批（包括未知 ID），再一次提交；不能把已选项改回待定。"""
    from . import backup
    backup.require_ready()
    if not isinstance(body, dict) or set(body) - {"authors", "topics"}:
        raise ValueError("备份规则格式不正确")
    for group, fields, table in (("authors", {"member", "public"}, "backup_authors"), ("topics", {"backup"}, "topics")):
        items = body.get(group, [])
        if not isinstance(items, list):
            raise ValueError("备份规则格式不正确")
        for item in items:
            if not isinstance(item, dict) or "id" not in item or set(item) - fields - {"id"} or any(type(v) is not bool for k,v in item.items() if k != "id"):
                raise ValueError("规则只能选择传或不传")
            if (group == "authors" and not isinstance(item["id"], str)) or (group == "topics" and type(item["id"]) is not int):
                raise ValueError("规则 ID 不正确")
            if not db.one(f"SELECT 1 FROM {table} WHERE id=?", item["id"]):
                if group != "authors" or not db.one("SELECT 1 FROM channels WHERE youtube_channel_id=?", item["id"]):
                    raise ValueError("未知的规则 ID")
    ids = set()
    with db.tx():
        for group, table in (("authors", "backup_authors"), ("topics", "topics")):
            for item in body.get(group, []):
                if group == "authors" and set(item) - {"id"}:
                    db.exec("INSERT INTO backup_authors(id,name) SELECT youtube_channel_id,name FROM channels WHERE youtube_channel_id=? ON CONFLICT(id) DO NOTHING", item["id"])
                for key, value in item.items():
                    if key != "id":
                        old = db.one(f"SELECT {key} FROM {table} WHERE id=?", item["id"])[key]
                        if old != int(value):
                            db.exec(f"UPDATE {table} SET {key}=? WHERE id=?", int(value), item["id"])
                            ids |= affected(author=item["id"]) if group == "authors" else affected(topic=item["id"])
        reconcile(ids)
    backup.pending_alert()
    return view()


def channel(cid: str) -> dict:
    from . import backup
    c = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not c:
        raise KeyError(cid)
    if not backup.available() or not c["youtube_channel_id"]:
        return {"ready": False}
    a = db.one("SELECT * FROM backup_authors WHERE id=?", c["youtube_channel_id"])
    a = a or {"id": c["youtube_channel_id"], "name": c["name"], "member": None, "public": None}
    ids = affected(author=a["id"])
    return {"ready": True, "author": {"id": a["id"], "name": c["name"] or a["name"]},
            "member": _bool(a["member"]), "public": _bool(a["public"]), "drive": drive(ids),
            "deletable": backup.deletable_author(a["id"])}


def save_channel(cid: str, body: dict) -> set[str]:
    """可放进定稿事务；调用者在提交后 reconcile。"""
    from . import backup
    backup.require_ready()
    c = db.one("SELECT * FROM channels WHERE id=?", cid)
    if not c or not c["youtube_channel_id"]:
        raise ValueError("频道还没有 YouTube 频道 ID")
    if not isinstance(body, dict) or set(body) - {"member", "public"} or any(type(v) is not bool for v in body.values()):
        raise ValueError("规则只能选择传或不传")
    db.exec("INSERT OR IGNORE INTO backup_authors(id,name) VALUES (?,?)", c["youtube_channel_id"], c["name"])
    changed = False
    for k,v in body.items():
        if db.one(f"SELECT {k} FROM backup_authors WHERE id=?", c["youtube_channel_id"])[k] != int(v):
            db.exec(f"UPDATE backup_authors SET {k}=? WHERE id=?", int(v), c["youtube_channel_id"])
            changed = True
    return affected(author=c["youtube_channel_id"]) if changed else set()


def affected(*, author: str | None = None, topic: int | None = None) -> set[str]:
    if author is not None:
        # 包括尚未被 housekeeping 归集的新行；读接口不替它们写库。
        return {r["id"] for r in db.q("SELECT id FROM video_backups WHERE youtube_channel_id=? UNION SELECT id FROM sources WHERE youtube_channel_id=? UNION SELECT id FROM videos WHERE youtube_channel_id=?", author, author, author)}
    return {r["video_id"] for r in db.q("SELECT video_id FROM topic_videos WHERE topic_id=?", topic)}


@serialized
def update_channel(cid: str, body: dict) -> dict:
    from . import backup
    if not db.one("SELECT 1 FROM channels WHERE id=?", cid):
        raise KeyError(cid)
    backup.require_ready()
    if not db.one("SELECT youtube_channel_id FROM channels WHERE id=?", cid)["youtube_channel_id"]:
        raise FileExistsError("频道还没有 YouTube 频道 ID")
    with db.tx():
        reconcile(save_channel(cid, body))
    backup.pending_alert()
    return channel(cid)
