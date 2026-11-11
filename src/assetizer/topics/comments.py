"""Notion 里的批注 (design §R8-13): the comments on a topic's Notion page and in its 来源 table's rows, read back (scan).
A thread is 课代表's when a word of the user's in it begins 「课代表」 (an integration cannot be @-mentioned): its words go
to the next answer as the user's — like the marks, never as what a video says (contracts/policy.md) — and it is
answered, 「〔课代表〕已改 · 第 3 版」, once the version that took it in is on the page (reply). The rest of the page's
comments (the user's own notes, Notion AI's) are not 课代表's business.

Notion gives a page's or a block's comments, not a page's blocks' comments together, and never the resolved: so every
block is asked, a little apart (PAUSE: the integration's 3 requests a second are delivery's too), and a resolved thread
is one the user is done with. Reading costs no model call; a word to 课代表 puts the answer together again once the user
has stopped writing a while (QUIET). Each word to 课代表 is kept in `asks`: how much this is used, and the 待修 list of
what an answer cannot mend (a developer's, logs.export; 「〔课代表〕已修」 once marked fixed)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
import time
from functools import wraps
from weakref import WeakValueDictionary

from .. import db
from ..notion.client import Notion, NotionError
from ..util import now

EVERY = 6 * 3600     # seconds: a topic's page is read for comments this often (and before each answer)
PAUSE = 0.4          # seconds between two reads of comments
SKIP = ("child_database", "child_page")   # the 来源 table is read row by row; a page in the page is not the topic's
QUOTE = 300          # characters of a block a comment is on the answer is told
HEADS = ("heading_1", "heading_2", "heading_3")
QUIET = 600          # seconds: the newest word to 课代表 this old, the answer is put together again (no more coming)
_BOT_UNSET = object()  # An explicitly unknown identity must not trigger a second /users/me in a webhook batch.
ASK = re.compile(r"^\s*[@＠]?\s*课代表[\s:：,，、。]*")   # how the user calls 课代表
MINE = "〔课代表〕"   # how 课代表's replies begin, to be told apart at a glance
RESULTS = ("已改", "没改", "已答", "记进待修")
_locks = WeakValueDictionary()
_locks_guard = threading.Lock()


def read_lock(tid: int):
    """Only reads of the same topic reconcile together; unused topic locks can be collected."""
    with _locks_guard:
        lock = _locks.get(tid)
        if lock is None:
            lock = threading.RLock()
            _locks[tid] = lock
        return lock


def _serial(fn):
    @wraps(fn)
    def locked(tid, *args, **kwargs):
        with read_lock(tid):
            return fn(tid, *args, **kwargs)
    return locked


class NoRead(Exception):
    """The integration may not read comments."""


def _plain(rich: list | None) -> str:
    return "".join(x.get("plain_text") or (x.get("text") or {}).get("content", "") for x in rich or [])


def _words(b: dict) -> str:
    body = b.get(b["type"]) or {}
    if b["type"] == "table_row":
        return " | ".join(_plain(c) for c in body.get("cells") or [])
    return _plain(body.get("rich_text"))


def _list(n: Notion, bid: str) -> list[dict]:
    """The comments on a page or a block that are not resolved, oldest first ([] for one not there)."""
    out: list[dict] = []
    cursor = None
    while True:
        if PAUSE:
            time.sleep(PAUSE)
        try:
            z = n._req("GET", f"/comments?block_id={bid}&page_size=100" + (f"&start_cursor={cursor}" if cursor else ""),
                       idempotent=True)
        except NotionError as e:
            if e.status == 403:
                raise NoRead from e
            if e.status in (400, 404):
                return out
            raise
        out += z.get("results") or []
        if not z.get("has_more") or not z.get("next_cursor"):
            return out
        cursor = z["next_cursor"]


def _walk(n: Notion, parent: str, place: dict, found: list, under: str | None = None) -> None:
    """The comments on every block under `parent`, with where each is: the block's words, the section it is in."""
    for b in n.children(parent):
        if b["type"] in SKIP:
            continue
        words = _words(b)
        if b["type"] in HEADS:
            under = words or under
        for c in _list(n, b["id"]):
            found.append((c, {**place, "block": b["id"], "quote": words[:QUOTE], "under": under, "kind": b["type"]}))
        if b.get("has_children"):
            _walk(n, b["id"], place, found, under)


def _bare(text: str, ours: bool) -> str:
    """A word without its call (「课代表：」) or 课代表's mark (〔课代表〕): what was said."""
    return text[len(MINE):] if ours and text.startswith(MINE) else ASK.sub("", text, count=1)


def _age(at: str | None) -> float | None:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds() if at else None
    except ValueError:
        return None


_OPEN_ASKED = """EXISTS (SELECT 1 FROM topic_comments a WHERE a.topic_id=c.topic_id AND a.discussion=c.discussion
                                   AND a.asked=1 AND a.open=1)"""


def _set(tid: int, **cols) -> None:
    db.exec(f"UPDATE topics SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", *cols.values(), tid)


@_serial
def scan(tid: int) -> int:
    """Read the comments on the topic's Notion page and its rows; returns how many of the user's words to 课代表 are new
    (or edited) since. What is no longer there (resolved, deleted) is no longer open. An integration that may not read
    comments is said on the page (comments_err NO_READ)."""
    from . import notion as TN  # it writes the page, and sends the replies (reply) once it has
    t = db.one("SELECT id, notion_page_id, notion_rows FROM topics WHERE id=?", tid)
    n = TN.client() if t and t["notion_page_id"] else None
    if not n:
        return 0
    bot = TN._bot(n)
    pid = t["notion_page_id"]
    found: list[tuple[dict, dict]] = []
    try:
        top = {"page": pid, "video": None}
        found += [(c, {**top, "block": None}) for c in _list(n, pid)]
        _walk(n, pid, top, found)
        for vid, rec in (db.unj(t["notion_rows"], {}) or {}).items():
            if not rec.get("page"):
                continue
            row = {"page": rec["page"], "video": vid}
            found += [(c, {**row, "block": None}) for c in _list(n, rec["page"])]
            try:
                _walk(n, rec["page"], row, found)
            except NotionError as e:
                if e.status not in (400, 404):  # the row taken out of the table: its comments with it
                    raise
    except NoRead:
        _set(tid, comments_at=now(), comments_err="NO_READ")
        return 0
    except NotionError as e:
        if e.status not in (400, 404):
            raise
        _set(tid, comments_at=now())  # the page deleted in Notion: the next write says so (notion.write)
        return 0
    new: set[str] = set()
    at = now()
    with db.tx() as cx:
        have = {r["id"]: r for r in cx.execute("SELECT id, hash, ours FROM topic_comments WHERE topic_id=?", (tid,))}
        cx.execute("UPDATE topic_comments SET open=0 WHERE topic_id=?", (tid,))
        for c, p in found:
            text = _plain(c.get("rich_text"))
            h = hashlib.sha1(text.encode()).hexdigest()
            ours = int(text.startswith(MINE) or (bool(bot) and (c.get("created_by") or {}).get("id") == bot))
            asked = int(not ours and bool(ASK.match(text)))
            old = have.get(c["id"])
            if old is None:
                cx.execute("""INSERT INTO topic_comments(topic_id, id, discussion, page, block, video_id, quote, under, kind, text, hash,
                              ours, asked, created, open) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                           (tid, c["id"], c.get("discussion_id") or c["id"], p["page"], p.get("block"), p.get("video"),
                            p.get("quote"), p.get("under"), p.get("kind"), text, h, ours, asked, c.get("created_time")))
                if not ours:
                    new.add(c["id"])
            else:
                cx.execute("""UPDATE topic_comments SET open=1, text=?, hash=?, asked=?, quote=COALESCE(?, quote), under=?,
                              seen=CASE WHEN hash=? THEN seen END WHERE topic_id=? AND id=?""",
                           (text, h, asked, p.get("quote"), p.get("under"), h, tid, c["id"]))
                if old["hash"] != h and not old["ours"]:
                    new.add(c["id"])
        words = list(cx.execute(f"""SELECT id, discussion, text FROM topic_comments c WHERE topic_id=? AND open=1 AND ours=0
                                    AND {_OPEN_ASKED}""", (tid,)))
        for w in words:  # each word to 课代表 once, as said last (asks: how much this is used)
            cx.execute("""INSERT INTO asks(id, at, topic_id, discussion, text) VALUES (?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET text=excluded.text""",
                       (w["id"], at, tid, w["discussion"], _bare(w["text"], False)))
        cx.execute("""UPDATE topics SET comments_at=?, comments_err=CASE WHEN comments_err='NO_READ' THEN NULL ELSE comments_err END
                      WHERE id=?""", (at, tid))
    return len(new & {w["id"] for w in words})


@_serial
def scan_one(tid: int, page: str, block: str | None, *, n: Notion | None = None, bot: str | None | object = _BOT_UNSET) -> int:
    """Reconcile just this block (or the page's own discussion). Other blocks, comments_at and existing under stay.
    The writes mirror scan; a later full read supplies the section a new comment is under."""
    from . import notion as TN
    t = db.one("SELECT notion_page_id, notion_rows FROM topics WHERE id=? AND archived_at IS NULL", tid)
    if not t or not t["notion_page_id"]:
        return 0
    n = n or TN.client()
    if not n:
        raise RuntimeError("NO_CLIENT")
    if bot is _BOT_UNSET:
        bot = TN._bot(n)
    video = next((vid for vid, row in (db.unj(t["notion_rows"], {}) or {}).items()
                  if TN._norm(row.get("page")) == TN._norm(page)), None)
    quote, kind = None, None
    try:
        found = _list(n, block or page)
        if block:
            try:
                b = n._req("GET", f"/blocks/{block}", idempotent=True)
                quote, kind = _words(b)[:QUOTE], b["type"]
            except NotionError as e:
                if e.status == 403:
                    raise NoRead from e
                if e.status not in (400, 404):
                    raise
                found = []
    except NoRead:
        _set(tid, comments_err="NO_READ")
        return 0
    new: set[str] = set()
    at = now()
    with db.tx() as cx:
        have = {r["id"]: r for r in cx.execute("SELECT id, hash, ours FROM topic_comments WHERE topic_id=?", (tid,))}
        cx.execute("UPDATE topic_comments SET open=0 WHERE topic_id=? AND page=? AND block IS ?", (tid, page, block))
        for c in found:
            text = _plain(c.get("rich_text"))
            h = hashlib.sha1(text.encode()).hexdigest()
            ours = int(text.startswith(MINE) or (bool(bot) and (c.get("created_by") or {}).get("id") == bot))
            asked = int(not ours and bool(ASK.match(text)))
            old = have.get(c["id"])
            if old is None:
                cx.execute("""INSERT INTO topic_comments(topic_id, id, discussion, page, block, video_id, quote, kind, text, hash,
                              ours, asked, created, open) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                           (tid, c["id"], c.get("discussion_id") or c["id"], page, block, video, quote, kind, text, h,
                            ours, asked, c.get("created_time")))
                if not ours:
                    new.add(c["id"])
            else:
                cx.execute("""UPDATE topic_comments SET open=1, text=?, hash=?, asked=?, quote=COALESCE(?, quote),
                              seen=CASE WHEN hash=? THEN seen END WHERE topic_id=? AND id=?""",
                           (text, h, asked, quote, h, tid, c["id"]))
                if old["hash"] != h and not old["ours"]:
                    new.add(c["id"])
        words = list(cx.execute(f"""SELECT id, discussion, text FROM topic_comments c WHERE topic_id=? AND open=1 AND ours=0
                                    AND {_OPEN_ASKED}""", (tid,)))
        for w in words:
            cx.execute("""INSERT INTO asks(id, at, topic_id, discussion, text) VALUES (?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET text=excluded.text""",
                       (w["id"], at, tid, w["discussion"], _bare(w["text"], False)))
        cx.execute("UPDATE topics SET comments_err=CASE WHEN comments_err='NO_READ' THEN NULL ELSE comments_err END WHERE id=?", (tid,))
    return len(new & {w["id"] for w in words})


def fresh(tid: int) -> int:
    """The user's words to 课代表 still open in Notion that no answer has taken in yet."""
    return db.one(f"SELECT COUNT(*) n FROM topic_comments c WHERE topic_id=? AND open=1 AND ours=0 AND seen IS NULL AND {_OPEN_ASKED}",
                  tid)["n"]


def _where(c: dict, rows: dict) -> str:
    """Where a thread is, in the answer's words: 整页, a line of a section, a video's row or a line of its notes."""
    if c["video_id"]:
        r = rows.get(c["video_id"]) or {}
        v = f"V{r['no']}《{r['title'] or c['video_id']}》" if r else "一期"
        return f"{v}的笔记这一行：{c['quote'] or ''}" if c["block"] else f"来源表 {v}这一行"
    if not c["block"]:
        return "整页"
    if c["kind"] in HEADS:
        return f"小标题「{c['quote']}」"
    part = f"「{c['under']}」一节" if c["under"] else "页上"
    return f"{part}这一行：{c['quote'] or ''}"


def threads(tid: int) -> list[dict]:
    """The threads still open that call 课代表, by when each began: N1, N2…, where it is, all that was said in it
    (课代表's replies too, the calls and marks left out), what is new (not taken into an answer yet)."""
    rows = {r["video_id"]: r for r in db.q("""SELECT tv.video_id, tv.no, v.title FROM topic_videos tv JOIN videos v ON v.id=tv.video_id
                                              WHERE tv.topic_id=?""", tid)}
    by: dict[str, list[dict]] = {}
    for c in db.q("SELECT * FROM topic_comments WHERE topic_id=? AND open=1 ORDER BY created, rowid", tid):
        by.setdefault(c["discussion"], []).append(c)
    out = []
    for d, cs in sorted(by.items(), key=lambda x: x[1][0]["created"] or ""):
        if not any(c["asked"] for c in cs):
            continue
        out.append({"id": f"N{len(out) + 1}", "discussion": d, "where": _where(cs[0], rows),
                    "said": [{"who": "课代表" if c["ours"] else "用户", "text": _bare(c["text"], c["ours"]),
                              "new": not c["ours"] and c["seen"] is None, "cid": c["id"]} for c in cs]})
    return out


def taken(tid: int, told: list[dict], n: int, replies: list[dict]) -> None:
    """The answer's version `n` took in the threads it was told: what was new in them is seen, how each was handled is
    kept with the words (asks), its replies wait to go into their threads with the next write to Notion (a newer reply in
    a thread over one not sent)."""
    by = {x["id"]: x for x in told}
    new = []
    with db.tx() as cx:
        for r in replies:
            if r.get("id") not in by:
                continue
            cids = [s["cid"] for s in by[r["id"]]["said"] if s["new"]]
            result = r.get("result") if r.get("result") in RESULTS else "已改"
            if cids:
                cx.execute(f"UPDATE asks SET result=?, reply=? WHERE id IN ({','.join('?' * len(cids))})", (result, r["text"], *cids))
            first = cx.execute(f"SELECT MIN(n) n FROM asks WHERE id IN ({','.join('?' * len(cids))})", cids).fetchone()["n"] if cids else None
            new.append({"discussion": by[r["id"]]["discussion"], "text": r["text"], "result": result, "n": n, "ask": first})
        for x in told:
            for s in x["said"]:
                if s["new"]:
                    cx.execute("UPDATE topic_comments SET seen=? WHERE topic_id=? AND id=? AND seen IS NULL", (n, tid, s["cid"]))
        old = db.unj(cx.execute("SELECT replies FROM topics WHERE id=?", (tid,)).fetchone()["replies"], []) or []
        mine = {r["discussion"] for r in new}
        keep = [r for r in old if r["discussion"] not in mine] + new
        cx.execute("UPDATE topics SET replies=? WHERE id=?", (db.j(keep) if keep else None, tid))


def _head(r: dict) -> str:
    """The first line of a reply, the same few words each time: 〔课代表〕已改 · 第 3 版, 〔课代表〕记进待修 #12."""
    result = r.get("result") or "已改"
    return f"{MINE}{result} #{r['ask']}" if result == "记进待修" and r.get("ask") else f"{MINE}{result} · 第 {r['n']} 版"


def _post(n: Notion, discussion: str, head: str, body: str = "", *, rich_body: list | None = None) -> dict:
    rich = [{"type": "text", "text": {"content": head}, "annotations": {"bold": True}}]
    if rich_body is not None:
        rich += [{"type": "text", "text": {"content": "\n"}}, *rich_body]
    elif body:
        rich.append({"type": "text", "text": {"content": "\n" + body[:1800]}})
    return n._req("POST", "/comments", json={"discussion_id": discussion, "rich_text": rich})


def reply_error(e: NotionError) -> str:
    """Shared reply policy: retain forbidden/temporary replies; discard a gone discussion."""
    return "NO_INSERT" if e.status == 403 else "GONE" if e.status in (400, 404) else "RETRY"


def reply(n: Notion, tid: int) -> int:
    """Send the replies waiting, each into its thread: 「〔课代表〕已改 · 第 3 版」 and a line or two. Called once the
    topic's page has the answer they were written with. An integration that may not insert comments keeps them waiting
    (comments_err NO_INSERT); a thread gone from Notion drops its reply. Returns how many went."""
    t = db.one("SELECT replies, comments_err FROM topics WHERE id=?", tid)
    waiting = (db.unj(t["replies"], []) or []) if t else []
    if not waiting:
        return 0
    left, sent, err = [], 0, t["comments_err"] if t["comments_err"] != "NO_INSERT" else None
    for i, r in enumerate(waiting):
        text = f"{_head(r)}\n{r['text'][:1800]}"
        try:
            got = _post(n, r["discussion"], _head(r), r["text"])
        except NotionError as e:
            if reply_error(e) == "NO_INSERT":
                err = "NO_INSERT"
                left += waiting[i:]
                break
            if reply_error(e) != "GONE":
                left.append(r)
            continue
        sent += 1
        db.exec("UPDATE asks SET answered_at=? WHERE discussion=? AND result IS NOT NULL AND answered_at IS NULL", now(), r["discussion"])
        if got.get("id"):
            at = db.one("SELECT page, block, video_id, quote, under, kind FROM topic_comments WHERE topic_id=? AND discussion=? LIMIT 1",
                        tid, r["discussion"]) or {}
            db.exec("""INSERT OR IGNORE INTO topic_comments(topic_id, id, discussion, page, block, video_id, quote, under, kind, text,
                       hash, ours, created, open, seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,1,?)""",
                    tid, got["id"], r["discussion"], at.get("page") or "", at.get("block"), at.get("video_id"), at.get("quote"),
                    at.get("under"), at.get("kind"), text, hashlib.sha1(text.encode()).hexdigest(),
                    got.get("created_time") or now(), r["n"])
    _set(tid, replies=db.j(left) if left else None, comments_err=err)
    return sent


def forget(tid: int) -> None:
    """The topic's page is another (or gone): the old one's comments and replies are not its own any more."""
    db.exec("DELETE FROM topic_comments WHERE topic_id=?", tid)
    _set(tid, replies=None, comments_at=None, comments_err=None)


def _ripe(tid: int) -> None:
    """Words to 课代表 no answer took in, the newest QUIET old: the answer is put together again with them (one that
    failed is not tried again by itself: 重新综合 is the user's)."""
    at = db.one(f"""SELECT MAX(created) at FROM topic_comments c WHERE topic_id=? AND open=1 AND ours=0 AND seen IS NULL
                    AND {_OPEN_ASKED}""", tid)["at"]
    age = _age(at)
    if age is not None and age >= QUIET:
        db.exec("""UPDATE topics SET synth_state='QUEUED', synth_error=NULL WHERE id=? AND synth IS NOT NULL AND archived_at IS NULL
                   AND COALESCE(synth_state,'DONE')='DONE'""", tid)


def fixed(k: int, version: str) -> bool:
    """A 待修 (asks.n) mended in `version`: said in its thread with the next look (due). False: no such 待修."""
    return db.exec("UPDATE asks SET fixed=?, fixed_told=NULL WHERE n=? AND result='记进待修'", version.strip()[:60], k) > 0


def _tell_fixed() -> None:
    """「〔课代表〕已修 #12 · 0.4.43」 into the thread of each 待修 mended since; one gone from Notion is not tried again."""
    waiting = db.q("SELECT n, discussion, fixed FROM asks WHERE fixed IS NOT NULL AND fixed_told IS NULL ORDER BY n")
    if not waiting:
        return
    from . import notion as TN
    n = TN.client()
    if not n:
        return
    for a in waiting:
        try:
            _post(n, a["discussion"], f"{MINE}已修 #{a['n']} · {a['fixed']}")
        except NotionError as e:
            if reply_error(e) != "GONE":
                return  # may not insert, Notion away: next time
        db.exec("UPDATE asks SET fixed_told=? WHERE n=?", now(), a["n"])


def used(days: int) -> dict:
    """The words to 课代表 in the last `days`, by how each was handled, and the 待修 not mended: is this worth keeping."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    by = {r["result"] or "没处理": r["n"] for r in db.q("SELECT result, COUNT(*) n FROM asks WHERE at>=? GROUP BY result", since)}
    todo = db.q("""SELECT a.n, a.at, a.topic_id, a.source_id, s.title source_title, a.text, a.reply FROM asks a
                   LEFT JOIN sources s ON s.id=a.source_id WHERE a.result='记进待修' AND a.fixed IS NULL ORDER BY a.n""")
    out = {"days": days, "asks": sum(by.values()), "by": by, "todo": todo}
    sources = db.q("""SELECT a.source_id,s.title,COUNT(*) asks FROM asks a LEFT JOIN sources s ON s.id=a.source_id
                      WHERE a.at>=? AND a.source_id IS NOT NULL GROUP BY a.source_id ORDER BY a.source_id""", since)
    if sources:
        out["sources"] = sources
    return out


def due() -> None:
    """Say the 待修 mended; read the comments of the topics with a Notion page read longer than EVERY ago, one after
    another (run.housekeeping starts it in the background), and put an answer together again for words to 课代表 the
    user has stopped writing. An archived topic's answer stays as it is: not read."""
    try:
        _tell_fixed()
    except Exception as e:  # noqa: BLE001
        db.event("topic", f"回复已修异常：{e}"[:300], None, "warn")
    for t in db.q("""SELECT id, comments_at FROM topics WHERE notion_page_id IS NOT NULL AND archived_at IS NULL
                     AND COALESCE(notion_state,'') != 'RUNNING' ORDER BY id"""):
        age = _age(t["comments_at"])
        if age is None or age >= EVERY:
            try:
                scan(t["id"])
            except Exception as e:  # noqa: BLE001
                db.event("topic", f"专题 {t['id']} 读 Notion 批注异常：{e}"[:300], None, "warn")
        _ripe(t["id"])
