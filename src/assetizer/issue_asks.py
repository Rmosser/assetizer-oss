"""Channel-page calls to 课代表 (design §R3-12).

Only webhook targets are read. A source lock serializes reads and frozen page batches, not delivery. Accepted model
answers survive in LLM's atomic cache; applying a batch, its corrections, asks and reply outbox is one transaction.
Corrections belong to a run and template, never to their artifacts. Delivery alone releases correction replies after Sync.check.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
import shutil
import sqlite3

from . import config, db, templates
from .contracts import prompts as P
from .llm import ContractError, LLM, call_recorder, validate
from .notion import blocks as B
from .notion.client import NotionError
from .notion.sync import _norm
from .topics import comments as C
from .util import now

# Limits are characters, not tokens. Never drop a thread: an oversized page waits for a new call, with a recorded failure.
MAX_THREADS = 32
MAX_SAID = 128
MAX_COMMENT = 2000
MAX_SECTION = 12000
MAX_NOTES = 4000
MAX_TRANSCRIPT = 40000
MAX_INPUT = 120000
TEXT_KINDS = {"paragraph", "heading_1", "heading_2", "heading_3", "quote", "bulleted_list_item",
              "numbered_list_item", "to_do", "toggle", "callout", "code"}
_ASKED = """SELECT source_id,page,discussion FROM issue_comments WHERE asked=1 AND open=1
             GROUP BY source_id,page,discussion"""
_OPEN_ASKED = "(c.source_id,c.page,c.discussion) IN (" + _ASKED + ")"
_ELIGIBLE = """s.notion_page_id IS NOT NULL AND s.delivery!='REGISTERED' AND ch.stage='LOCKED'"""


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def read_lock(sid: str):
    return C.read_lock(("issue", sid))


def eligible(sid: str) -> dict | None:
    return db.one(f"""SELECT s.* FROM sources s JOIN channels ch ON ch.id=s.channel_id
                      WHERE s.id=? AND {_ELIGIBLE}""", sid)


def _state(sid: str, error: str | None) -> None:
    db.exec("""INSERT INTO issue_state(source_id,error) VALUES (?,?)
               ON CONFLICT(source_id) DO UPDATE SET error=excluded.error""", sid, error)


def _location(n, sid: str, bid: str, bot: str | None) -> tuple[dict | None, str, str, bool, int]:
    """Read at most two parents. The ancestor supplies both the plan key and its checkpointed edition. A block with
    more than one mapping row (left from before checkpoint kept one per block) has no plan key, and we stop there."""
    keys: dict[str, list[dict]] = {}
    for r in db.q("SELECT * FROM notion_blocks WHERE source_id=?", sid):
        keys.setdefault(_norm(r["block_id"]), []).append(r)
    quote, kind, owned = "", "", False
    authors = set(db.setting(f"notion_authors:{sid}") or []) | ({bot} if bot else set())
    for hop in range(3):
        b = n._req("GET", f"/blocks/{bid}", idempotent=True)
        if hop == 0:
            quote, kind = C._words(b), b["type"]
            owned = (b.get("created_by") or {}).get("id") in authors
        if rows := keys.get(_norm(bid)):
            if len(rows) > 1:
                return None, quote, kind, owned, 0
            return rows[0], quote, kind, owned, hop
        parent = b.get("parent") or {}
        if parent.get("type") != "block_id" or not parent.get("block_id"):
            break
        bid = parent["block_id"]
    return None, quote, kind, owned, 0


def scan_one(sid: str, page: str, block: str | None, *, n, bot: str | None) -> int:
    with read_lock(sid):
        source = eligible(sid)
        if not source or _norm(source["notion_page_id"]) != _norm(page):
            return 0
        key, quote, kind = None, None, None
        edition = None
        owned = False
        depth = 0
        try:
            found = C._list(n, block or page)
            if block:
                try:
                    row, quote, kind, owned, depth = _location(n, sid, block, bot)
                    if row:
                        key = row["block_key"]
                        if row["run_id"] is None and row["template"] is None:
                            edition = _edition(source, source["notion_synced_run"], source["notion_synced_template"])
                        else:
                            edition = {"run_id": row["run_id"], "template": row["template"]}
                except NotionError as e:
                    if e.status == 403:
                        raise C.NoRead from e
                    if e.status not in (400, 404):
                        raise
                    found = []
        except C.NoRead:
            _state(sid, "NO_READ")
            return 0
        new = set()
        at = _stamp()
        with db.tx() as cx:
            cx.execute("UPDATE issue_comments SET open=0 WHERE source_id=? AND page=? AND block IS ?", (sid, page, block))
            for c in found:
                text = C._plain(c.get("rich_text"))
                h = hashlib.sha1(text.encode()).hexdigest()
                ours = int(text.startswith(C.MINE) or bool(bot and (c.get("created_by") or {}).get("id") == bot))
                asked = int(not ours and bool(C.ASK.match(text)))
                old = cx.execute("SELECT * FROM issue_comments WHERE source_id=? AND id=?", (sid, c["id"])).fetchone()
                changed = old is None or old["hash"] != h
                touched = c.get("last_edited_time") or (at if old and changed else c.get("created_time")) or at
                discussion = c.get("discussion_id") or c["id"]
                place = cx.execute("""SELECT plan_key,quote,kind,owned,depth FROM issue_comments
                                      WHERE source_id=? AND discussion=? AND plan_key IS NOT NULL AND ours=0 ORDER BY rowid LIMIT 1""",
                                   (sid, discussion)).fetchone()
                loc = dict(place) if place and old is None else dict(plan_key=key, quote=quote, kind=kind, owned=int(owned), depth=depth)
                if not ours and key:
                    cx.execute("""INSERT OR IGNORE INTO issue_positions(source_id,discussion,run_id,template,plan_key,section_id)
                                  VALUES (?,?,?,?,?,?)""", (sid, discussion, edition["run_id"], edition["template"],
                                  key, key.split(":")[0]))
                cx.execute("""INSERT INTO issue_comments(source_id,id,discussion,page,block,plan_key,quote,kind,owned,depth,text,hash,
                              ours,asked,created,touched,noticed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                              ON CONFLICT(source_id,id) DO UPDATE SET open=1,
                              plan_key=COALESCE(issue_comments.plan_key,excluded.plan_key),
                              quote=CASE WHEN issue_comments.plan_key IS NOT NULL THEN issue_comments.quote ELSE excluded.quote END,
                              kind=CASE WHEN issue_comments.plan_key IS NOT NULL THEN issue_comments.kind ELSE excluded.kind END,
                              owned=CASE WHEN issue_comments.plan_key IS NOT NULL THEN issue_comments.owned ELSE excluded.owned END,
                              depth=CASE WHEN issue_comments.plan_key IS NOT NULL THEN issue_comments.depth ELSE excluded.depth END,text=excluded.text,hash=excluded.hash,ours=excluded.ours,asked=excluded.asked,
                              touched=CASE WHEN issue_comments.hash!=excluded.hash THEN excluded.touched ELSE issue_comments.touched END,
                              noticed=CASE WHEN issue_comments.hash!=excluded.hash THEN excluded.noticed ELSE issue_comments.noticed END,
                              seen=CASE WHEN issue_comments.hash=excluded.hash THEN issue_comments.seen END""",
                           (sid, c["id"], discussion, page, block, loc["plan_key"], loc["quote"], loc["kind"], loc["owned"], loc["depth"], text, h, ours,
                            asked, c.get("created_time"), touched, at))
                if changed and not ours:
                    new.add(c["id"])
            words = list(cx.execute(f"""SELECT id,discussion,text FROM issue_comments c WHERE source_id=? AND page=?
                                       AND open=1 AND ours=0 AND {_OPEN_ASKED}""", (sid, page)))
            for w in words:
                text = C._bare(w["text"], False)
                if cx.execute("SELECT 1 FROM asks WHERE id=?", (w["id"],)).fetchone():
                    cx.execute("UPDATE asks SET text=? WHERE id=?", (text, w["id"]))
                else:
                    cx.execute("INSERT INTO asks(id,at,source_id,discussion,text) VALUES (?,?,?,?,?)",
                               (w["id"], at, sid, w["discussion"], text))
            cx.execute("UPDATE issue_state SET error=NULL WHERE source_id=? AND error='NO_READ'", (sid,))
        return len(new & {w["id"] for w in words})


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _texts(plan: list[dict]):
    """Every editable text in the uncorrected plan, with its optional position hint."""
    def walk(item, block, path):
        kind = block.get("type")
        body = block.get(kind) or {}
        if kind in TEXT_KINDS and "rich_text" in body:
            yield item, block, path
        for i, child in enumerate(body.get("children") or []):
            yield from walk(item, child, [*path, i])
    for item in plan:
        if "block" in item:
            yield from walk(item, item["block"], [])


def _edition(source: dict, run_id: str, template: str | None = None) -> dict:
    from . import delivery
    return {"run_id": run_id, "template": template or (delivery.target_for(source) or {}).get("template") or "default"}


def _find(plan: list[dict], want: dict, recorded: dict, displayed: list[dict] | None = None,
          *, previous: list[dict] | None = None):
    """Exact edition and hinted position. Only discussions may supply an old plan to relocate one-to-one.

    A correction never supplies previous: changing run or template makes it inapplicable. For a discussion,
    first prove its old position on the raw old plan, then require its text unique in both editions' section.
    """
    same = all(want[k] == recorded.get(k) for k in ("run_id", "template"))
    if not same:
        if want["run_id"] != recorded.get("run_id") or previous is None:
            return None
        origin = _find(previous, recorded, recorded)
        if not origin or not recorded.get("section_id"):
            return None
        text = B.plain(origin[1])
        def candidates(items):
            return [(it, b, path) for it, b, path in _texts(items)
                    if it["key"].startswith(recorded["section_id"] + ":") and B.plain(b) == text]
        old, current = candidates(previous), candidates(plan)
        return current[0] if len(old) == 1 and len(current) == 1 else None
    original = recorded.get("original")
    shown = {(it["key"], tuple(path)): B.plain(b) for it, b, path in _texts(displayed or [])}
    part = db.unj(recorded.get("part"))
    matches = []
    for item, block, path in _texts(plan):
        if item["key"] != recorded["plan_key"]:
            continue
        if part is not None and path != part:
            continue
        if part is None and len(path) != recorded.get("depth", 0):
            continue
        text = B.plain(block)
        if original is not None:
            ok = text == original and (not recorded.get("original_hash") or _hash(text) == recorded["original_hash"])
        else:
            ok = recorded.get("quote") in (text, shown.get((item["key"], tuple(path))))
        if ok:
            matches.append((item, block, path))
    return matches[0] if len(matches) == 1 else None


def apply_fixes(source: dict, run_id: str, plan: list[dict], *, template: str | None = None) -> list[dict]:
    """Locate all originals before changing any text. Preview/build never writes diagnostic events."""
    fixes = db.q("SELECT * FROM issue_fixes WHERE source_id=? ORDER BY id", source["id"])
    want = _edition(source, run_id, template)
    located = [(r, _find(plan, want, r)) for r in fixes if r["run_id"] == run_id]
    if plan and fixes:
        plan[0]["_fixes_checked"] = [r["id"] for r in fixes]
    for r, where in located:
        if where:
            item, block, _ = where
            block[block["type"]]["rich_text"] = B.rich(r["text"])
            item.setdefault("_fix_ids", []).append(r["id"])
    return plan

def _sections(run_id: str) -> list[dict]:
    out = db.q("SELECT * FROM sections WHERE run_id=? ORDER BY ord", run_id)
    for sec in out:
        units = db.unj(sec["unit_ids"], []) or []
        spans = db.q(f"SELECT start,end FROM units WHERE run_id=? AND id IN ({','.join('?' * len(units))})", run_id, *units) if units else []
        sec["start"] = min((u["start"] for u in spans), default=0)
        sec["end"] = max((u["end"] for u in spans), default=0)
        lines = (sec["overlay"] or sec["markdown"]).splitlines()
        sec["title"] = (lines[0] if lines else sec["id"]).lstrip("# ")[:200]
    return out


def _payload(source: dict) -> dict:
    from . import delivery
    rid, sid = source["current_run_id"], source["id"]
    # The unhashed builder keeps this text-only stage independent of private media files.
    target = delivery.target_for(source) or {}
    want = _edition(source, rid, target.get("template") or "default")
    raw = delivery.page_plan(source, rid, target, corrected=False)
    displayed = apply_fixes(source, rid, copy.deepcopy(raw), template=want["template"])
    shown = {(it["key"], tuple(path)): b for it, b, path in _texts(displayed)}
    secs = _sections(rid)
    bysec = {s["id"]: s for s in secs}
    segs = templates._segments(rid)
    by = {}
    for c in db.q("""SELECT * FROM issue_comments WHERE source_id=? AND page=? AND open=1 ORDER BY created,rowid""",
                  sid, source["notion_page_id"]):
        by.setdefault(c["discussion"], []).append(c)
    chosen = [cs for cs in by.values() if any(c["asked"] for c in cs) and any(not c["ours"] and c["seen"] is None for c in cs)]
    if len(chosen) > MAX_THREADS:
        raise ValueError("INPUT_LIMIT")
    channel = db.one("SELECT name FROM channels WHERE id=?", source.get("channel_id")) or {}
    payload = {"title": (source.get("title") or sid)[:500], "channel": (channel.get("name") or source.get("channel_title") or "")[:200], "threads": [], "sections": {}}
    # Resolve the whole batch before persisting any first original: migrated threads may collide.
    old_plans, locations, migrated, targets = {}, {}, set(), {}
    for cs in chosen:
        first = next(c for c in cs if not c["ours"])
        did = first["discussion"]
        recorded = db.one("SELECT * FROM issue_positions WHERE source_id=? AND discussion=?", sid, did)
        previous = None
        if recorded and recorded["run_id"] == rid and recorded["template"] != want["template"]:
            tpl = recorded["template"]
            if tpl not in old_plans:
                try:
                    old_plans[tpl] = delivery.page_plan(source, rid, {**target, "template": tpl}, corrected=False)
                except Exception:  # Cannot prove the old edition's identity: keep the discussion read-only.
                    old_plans[tpl] = None
            previous = old_plans[tpl]
            migrated.add(did)
        where = _find(raw, want, {**first, **recorded}, displayed, previous=previous) if recorded else None
        locations[did] = (recorded, where)
        if where:
            targets.setdefault((where[0]["key"], tuple(where[2])), []).append(did)
    for ids in targets.values():
        if len(ids) > 1 and migrated.intersection(ids):
            for did in ids:
                locations[did] = (locations[did][0], None)
    section_cache, remaining = {}, MAX_TRANSCRIPT
    for cs in chosen:
        if len(cs) > MAX_SAID:
            raise ValueError("INPUT_LIMIT")
        first = next(c for c in cs if not c["ours"])
        recorded, where = locations[first["discussion"]]
        key = where[0]["key"] if where else first["plan_key"]
        sec = bysec.get(recorded["section_id"] if recorded else (key or "").split(":")[0])
        context = None
        if sec:
            if sec["id"] not in section_cache:
                lines = []
                clipped = False
                for i, s in enumerate(segs, 1):
                    if s["end"] <= sec["start"] or s["start"] >= sec["end"] or not (s.get("text") or "").strip():
                        continue
                    text = s["text"].strip()
                    size = len(text) + 80
                    if size > remaining:
                        clipped = True
                        break
                    lines.append({"line": i, "start": s["start"], "time": _tc(s["start"]), "text": text})
                    remaining -= size
                units = db.unj(sec["unit_ids"], []) or []
                notes = [dict(r) for r in db.q("SELECT title,body,evidence,target_id FROM flags WHERE run_id=? AND severity!='INFO'", rid)
                         if r["target_id"] in [sec["id"], *units]]
                md = sec["overlay"] or sec["markdown"]
                ns = db.j(notes)
                section_cache[sec["id"]] = {"id": sec["id"], "start": sec["start"], "markdown": md[:MAX_SECTION],
                    "lines": lines, "notes": ns[:MAX_NOTES], "truncated": clipped or len(md) > MAX_SECTION or len(ns) > MAX_NOTES}
            context = section_cache[sec["id"]]
        path = where[2] if where else None
        target = shown[(key, tuple(path))] if where else {}
        can = bool(first["owned"] and first["kind"] in TEXT_KINDS and sec and target.get("type") in TEXT_KINDS
                   and "rich_text" in target.get(target.get("type"), {}) and context["lines"])
        payload["threads"].append({"id": first["discussion"], "key": key, "block": first["block"], "kind": first["kind"],
            "quote": (first["quote"] or "")[:1800], "plan_kind": target.get("type"), "can_fix": can, "part": path,
            "plan_text": B.plain(target)[:1800] if target else "", "_current_text": B.plain(target) if target else "",
            "truncated": len(first["quote"] or "") > 1800 or (bool(target) and len(B.plain(target)) > 1800),
            "section": sec["id"] if sec else None, "titles": [s["title"] for s in secs] if not sec else [],
            "said": [{"who": "课代表" if c["ours"] else "用户", "text": C._bare(c["text"], c["ours"])[:MAX_COMMENT],
                      "new": not c["ours"] and c["seen"] is None, "cid": c["id"], "hash": c["hash"],
                      "truncated": len(c["text"]) > MAX_COMMENT} for c in cs],
            "_original": None})
        if where:
            words = B.plain(where[1])
            identity = dict(plan_key=key, part=db.j(path), original=words, original_hash=_hash(words),
                            section_id=sec["id"] if sec else "", **want)
            payload["threads"][-1]["_original"] = identity
            # Save the first original even if the model later fails; never replace it with a corrected quote.
            db.exec("""UPDATE issue_positions SET original=COALESCE(original,?)
                       WHERE source_id=? AND discussion=?""", words, sid, first["discussion"])
    payload["sections"] = section_cache
    if len(db.j(payload)) > MAX_INPUT:
        raise ValueError("INPUT_LIMIT")
    return payload


def _check(payload: dict, output: dict) -> None:
    validate(output, P.COMMENTS_SCHEMA)
    by = {t["id"]: t for t in payload["threads"]}
    rs = output["replies"]
    if len(rs) != len(by) or {r["id"] for r in rs} != set(by):
        raise ContractError("COMMENTS", "每个讨论串必须恰好一条处理结果")
    fixes = {}
    for r in rs:
        t = by[r["id"]]
        if not r["text"].strip():
            raise ContractError("COMMENTS", "回复不能为空")
        lines = (payload["sections"].get(t["section"]) or {}).get("lines", [])
        if r["result"] in ("已答", "没改") and lines and "line" not in r:
            raise ContractError("COMMENTS", "有原话的已答、没改必须指定支持回答的 line")
        if "line" in r and r["line"] not in {x["line"] for x in lines}:
            raise ContractError("COMMENTS", "引用的原话行号不在该串给出的范围里")
        if r["result"] == "已改":
            if not t["can_fix"] or not (r.get("fix") or "").strip() or len(r["fix"]) > 1800:
                raise ContractError("COMMENTS", "这串不能已改，或 fix 为空、超过 1800 字")
            if B.plain(B.block("paragraph", r["fix"].strip())) == t["_current_text"]:
                raise ContractError("COMMENTS", "fix 与当前文字规范化后相同")
            if len(B.runs(r["fix"])) > B.MAX_PIECES:
                raise ContractError("COMMENTS", "fix 的行内格式超过单块上限")
            where = (t["key"], tuple(t["part"]))
            if where in fixes and fixes[where] != r["fix"]:
                raise ContractError("COMMENTS", "同一块的几个改正须给出相同的最终文字")
            fixes[where] = r["fix"]


def _tc(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _body(t: dict, r: dict, sid: str, sections: dict) -> str:
    sec = sections.get(t["section"]) or {}
    line = next((ln for ln in sec.get("lines", []) if ln["line"] == r.get("line")), None)
    body = r["text"].strip()
    if r["result"] in ("已答", "没改") and line:
        seconds = max(0, int(line["start"]))
        words = line["text"][:500] + ("…" if len(line["text"]) > 500 else "")
        body += f"\n原话：「{words}」\n[▶ {_tc(seconds)}](https://www.youtube.com/watch?v={sid}&t={seconds}s)"
    return body

def _rich_body(body: str) -> list[dict]:
    """Only the code-generated last line is a link. Preserve transcript quotes literally, like topic reply bodies."""
    text, _, last = body.rpartition("\n")
    match = re.fullmatch(r"\[(▶ \d+:\d{2})\]\((https://www\.youtube\.com/watch\?v=[^\s&)]+&t=\d+s)\)", last)
    if match:
        return [{"type": "text", "text": {"content": text + "\n"}},
                {"type": "text", "text": {"content": match[1], "link": {"url": match[2]}}}]
    return [{"type": "text", "text": {"content": body}}]


VOID_TEXT = "这页随后重印了，改正没能带到新版里。要改的话，在这串里再点一次名。"


def _void(cx, sid: str, *, checked: set[int] | None = None, applied: set[int] | None = None) -> None:
    """One transaction removes corrections and persists notices; repeated readbacks cannot enqueue them again."""
    missing = (checked or set()) - (applied or set())
    rows = cx.execute("""SELECT r.*,b.run_id,s.current_run_id,f.id present FROM issue_replies r
                         JOIN issue_batches b ON b.id=r.batch_id JOIN sources s ON s.id=r.source_id
                         LEFT JOIN issue_fixes f ON f.id=r.fix_id WHERE r.source_id=? AND r.head=?""",
                      (sid, C.MINE + "已改 · 改正在下一块")).fetchall()
    for row in rows:
        stale = row["run_id"] != row["current_run_id"] or row["present"] is None
        # Already-sent corrections are invalidated only by completed readback, using its frozen checked set.
        if row["fix_id"] not in missing and not (row["done"] is None and stale):
            continue
        cx.execute("DELETE FROM issue_fixes WHERE id=?", (row["fix_id"],))
        existing = cx.execute("SELECT * FROM issue_replies WHERE void_of=? AND discussion=?",
                              (row["fix_id"], row["discussion"])).fetchone()
        if row["done"] is None and existing:
            # Several accepted follow-ups can share one correction. One notice covers all their accounting rows.
            if existing["done"] is None:
                cids = db.unj(existing["cids"]) + [{**c, "batch": row["batch_id"]} for c in db.unj(row["cids"])]
                cx.execute("UPDATE issue_replies SET cids=?,version=version+1,sending=0,tried_at=NULL WHERE batch_id=? AND discussion=?",
                           (db.j(cids), existing["batch_id"], row["discussion"]))
            cx.execute("""UPDATE issue_replies SET head=?,body=?,fix_id=NULL,ready=0,done=?,version=version+1,sending=0,tried_at=NULL
                          WHERE batch_id=? AND discussion=?""",
                       (C.MINE + "没改", VOID_TEXT, _stamp(), row["batch_id"], row["discussion"]))
        elif row["done"] is None:
            cx.execute("""UPDATE issue_replies SET head=?,body=?,fix_id=NULL,void_of=?,ready=1,sending=0,tried_at=NULL,version=version+1
                          WHERE batch_id=? AND discussion=?""",
                       (C.MINE + "没改", VOID_TEXT, row["fix_id"], row["batch_id"], row["discussion"]))
        elif not existing:
            batch = cx.execute("""INSERT INTO issue_batches(source_id,run_id,page,input,state,created)
                                  VALUES (?,?,?,'{}','DONE',?)""",
                               (sid, row["current_run_id"], row["page"], _stamp())).lastrowid
            cids = [{**c, "batch": row["batch_id"]} for c in db.unj(row["cids"])]
            cx.execute("""INSERT INTO issue_replies(batch_id,discussion,source_id,page,head,body,cids,void_of,ready)
                          VALUES (?,?,?,?,?,?,?,?,1)""",
                       (batch, row["discussion"], sid, row["page"], C.MINE + "没改", VOID_TEXT, db.j(cids), row["fix_id"]))
        for c in db.unj(row["cids"]):
            cx.execute("UPDATE asks SET result='没改',reply=? WHERE id=?", (VOID_TEXT, c["id"]))
    for fid in missing:
        cx.execute("DELETE FROM issue_fixes WHERE source_id=? AND id=?", (sid, fid))

def _apply(batch: dict, output: dict) -> None:
    payload = db.unj(batch["input"])
    _check(payload, output)
    by = {t["id"]: t for t in payload["threads"]}
    at = _stamp()
    with db.tx() as cx:
        if cx.execute("SELECT state FROM issue_batches WHERE id=?", (batch["id"],)).fetchone()["state"] != "APPLY":
            return
        from . import delivery
        source = dict(cx.execute("SELECT * FROM sources WHERE id=?", (batch["source_id"],)).fetchone())
        target_config = delivery.target_for(source) or {}
        want = _edition(source, source["current_run_id"], target_config.get("template") or "default")
        raw = delivery.page_plan(source, source["current_run_id"], target_config, corrected=False)
        for r in output["replies"]:
            t = by[r["id"]]
            identity = t["_original"]
            same_edition = identity and all(want[k] == identity.get(k) for k in ("run_id", "template"))
            target = _find(raw, want, identity) if same_edition else None
            if target:
                identity = {**identity, **want, "plan_key": target[0]["key"], "part": db.j(target[2])}
                cx.execute("""UPDATE issue_positions SET plan_key=?,part=?,run_id=?,template=? WHERE source_id=? AND discussion=?""",
                           (identity["plan_key"], identity["part"], want["run_id"], want["template"], batch["source_id"], t["id"]))
            said = [s for s in t["said"] if s["new"]]
            cids = [{"id": s["cid"], "hash": s["hash"]} for s in said]
            ns = [cx.execute("SELECT n FROM asks WHERE id=?", (s["cid"],)).fetchone()["n"] for s in said]
            fix_id = None
            current = cx.execute("SELECT current_run_id FROM sources WHERE id=?", (batch["source_id"],)).fetchone()[0]
            obsolete = r["result"] == "已改" and (current != batch["run_id"] or target is None)
            if r["result"] == "已改" and not obsolete:
                old_ids = []
                for old in cx.execute("SELECT * FROM issue_fixes WHERE source_id=? AND run_id=?", (batch["source_id"], current)):
                    found = _find(raw, want, dict(old))
                    if found and (found[0]["key"], found[2]) == (target[0]["key"], target[2]):
                        old_ids.append(old["id"])
                for old_id in old_ids:
                    cx.execute("DELETE FROM issue_fixes WHERE id=?", (old_id,))
                cx.execute("""INSERT INTO issue_fixes(source_id,run_id,template,plan_key,part,original,original_hash,text,section_id,ask_id,created)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (batch["source_id"], current, want["template"], identity["plan_key"],
                              identity["part"], identity["original"], identity["original_hash"], r["fix"].strip(),
                              identity["section_id"], said[-1]["cid"], at))
                fix_id = cx.execute("SELECT last_insert_rowid()").fetchone()[0]
                for old_id in old_ids:
                    cx.execute("""UPDATE issue_replies SET fix_id=?,ready=CASE WHEN done IS NULL THEN 0 ELSE ready END,
                                  version=version+1,sending=0,tried_at=NULL WHERE fix_id=?""", (fix_id, old_id))
                cx.execute("UPDATE sources SET delivery='STALE',updated_at=? WHERE id=? AND current_run_id=? AND delivery!='REGISTERED'",
                           (at, batch["source_id"], batch["run_id"]))
            result = "没改" if obsolete else r["result"]
            body = VOID_TEXT if obsolete else _body(t, r, batch["source_id"], payload["sections"])
            head = C.MINE + result + (" · 改正在下一块" if result == "已改" else f" #{min(ns)}" if result == "记进待修" else "")
            cx.execute("""INSERT INTO issue_replies(batch_id,discussion,source_id,page,head,body,cids,fix_id,ready)
                          VALUES (?,?,?,?,?,?,?,?,?)""", (batch["id"], t["id"], batch["source_id"], batch["page"], head,
                          body, db.j(cids), fix_id, int(fix_id is None)))
            for word in said:
                cx.execute("UPDATE asks SET result=?,reply=?,answered_at=NULL WHERE id=?", (result, VOID_TEXT if obsolete else r["text"], word["cid"]))
                cx.execute("UPDATE issue_comments SET seen=? WHERE source_id=? AND id=? AND hash=?",
                           (batch["id"], batch["source_id"], word["cid"], word["hash"]))
        _void(cx, batch["source_id"])
        cx.execute("UPDATE issue_batches SET state='DONE' WHERE id=?", (batch["id"],))

def delivered(cx, sid: str, rid: str, page: str, plan: list[dict]) -> bool:
    """Only this completed readback releases corrections. Newer corrections were not checked by the old plan."""
    applied = {fid for item in plan for fid in item.get("_fix_ids", [])}
    checked = {fid for item in plan for fid in item.get("_fixes_checked", [])}
    missing = checked - applied
    count = sum(1 for row in cx.execute("SELECT id FROM issue_fixes WHERE source_id=?", (sid,)) if row[0] in missing)
    _void(cx, sid, checked=checked, applied=applied)
    if count:
        title = cx.execute("SELECT title FROM sources WHERE id=?", (sid,)).fetchone()[0] or sid
        cx.execute("INSERT INTO events(ts,kind,level,message) VALUES (?,?,?,?)",
                   (now(), "issue-asks", "warn", f"频道页 {title} 有 {count} 条改正没能带到这次交付，读回后已作废"))
    cx.execute("UPDATE issue_fixes SET readback=0 WHERE source_id=?", (sid,))
    for fid in applied:
        cx.execute("UPDATE issue_fixes SET readback=1 WHERE id=?", (fid,))
        cx.execute("UPDATE issue_replies SET ready=1 WHERE source_id=? AND page=? AND fix_id=? AND done IS NULL", (sid, page, fid))
    current = {r[0] for r in cx.execute("SELECT id FROM issue_fixes WHERE source_id=? AND run_id=?", (sid, rid))}
    return bool(current - checked)

def _finish_reply(r: dict, got: dict | None) -> None:
    at = _stamp()
    with db.tx() as cx:
        finished = cx.execute("""UPDATE issue_replies SET done=? WHERE batch_id=? AND discussion=? AND version=? AND done IS NULL""",
                              (at, r["batch_id"], r["discussion"], r["version"])).rowcount
        for c in db.unj(r["cids"]) if got and finished else []:
            # A new edit of the same user comment may already be waiting: don't mark that answer as sent yet.
            cx.execute("""UPDATE asks SET answered_at=? WHERE id=? AND EXISTS
                          (SELECT 1 FROM issue_comments WHERE source_id=? AND id=? AND hash=? AND seen=?)""",
                       (at, c["id"], r["source_id"], c["id"], c["hash"], c.get("batch", r["batch_id"])))
        if got and got.get("id"):
            place = cx.execute("SELECT * FROM issue_comments WHERE source_id=? AND discussion=? LIMIT 1",
                               (r["source_id"], r["discussion"])).fetchone()
            text = r["head"] + "\n" + C._plain(_rich_body(r["body"]))
            cx.execute("""INSERT OR IGNORE INTO issue_comments(source_id,id,discussion,page,block,plan_key,quote,kind,text,hash,
                          ours,created,touched,noticed,seen) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                       (r["source_id"], got["id"], r["discussion"], r["page"], place["block"] if place else None,
                        place["plan_key"] if place else None, place["quote"] if place else None, place["kind"] if place else None,
                        text, hashlib.sha1(text.encode()).hexdigest(), got.get("created_time") or at, at, at, r["batch_id"]))


def reply(sid: str, *, n=None) -> int:
    from . import delivery
    with read_lock(sid):
        source = eligible(sid)
        with db.tx() as cx:
            _void(cx, sid)
        waiting = db.q("SELECT * FROM issue_replies WHERE source_id=? AND done IS NULL AND ready=1 ORDER BY batch_id", sid)
        if not source or not waiting:
            return 0
        n = n or delivery.client()
        if not n:
            return 0
        sent = 0
        for r in waiting:
            # A replaced page has another discussion: never say its new copy corrected the old page.
            if _norm(r["page"]) != _norm(source["notion_page_id"]):
                continue
            if r["fix_id"]:
                fix = db.one("SELECT readback FROM issue_fixes WHERE id=?", r["fix_id"])
                if not fix or not fix["readback"]:
                    continue
            if r["sending"] and r["tried_at"] and C._age(r["tried_at"]) < C.QUIET:
                continue
            with db.tx() as cx:
                claimed = cx.execute("""UPDATE issue_replies SET sending=1,tried_at=?
                                        WHERE batch_id=? AND discussion=? AND version=? AND done IS NULL""",
                                     (_stamp(), r["batch_id"], r["discussion"], r["version"])).rowcount
            if not claimed:
                continue
            try:
                if r["void_of"] and not r["sending"]:
                    place = db.one("SELECT block FROM issue_comments WHERE source_id=? AND discussion=? LIMIT 1", sid, r["discussion"])
                    found = C._list(n, (place or {}).get("block") or r["page"])
                    if not any(c.get("discussion_id") == r["discussion"] for c in found):
                        _finish_reply(r, None)
                        continue
                if r["sending"]:
                    place = db.one("SELECT block FROM issue_comments WHERE source_id=? AND discussion=? LIMIT 1", sid, r["discussion"])
                    found = C._list(n, (place or {}).get("block") or r["page"])
                    expected = r["head"] + "\n" + C._plain(_rich_body(r["body"]))
                    previous = {c["id"] for c in db.q("SELECT id FROM issue_comments WHERE source_id=? AND ours=1 AND seen!=?",
                                                      sid, r["batch_id"])}
                    got = next((c for c in found if c.get("discussion_id") == r["discussion"] and c["id"] not in previous
                                and C._plain(c.get("rich_text")) == expected), None)
                    if got:
                        _finish_reply(r, got)
                        sent += 1
                        continue
                    if not any(c.get("discussion_id") == r["discussion"] for c in found):
                        _finish_reply(r, None)  # Resolved/deleted discussions are no longer requests to answer.
                        continue
                got = C._post(n, r["discussion"], r["head"], rich_body=_rich_body(r["body"]))
            except C.NoRead:
                _state(sid, "NO_READ")
                break
            except NotionError as e:
                err = C.reply_error(e)
                db.exec("UPDATE issue_replies SET sending=0 WHERE batch_id=? AND discussion=? AND version=?", r["batch_id"], r["discussion"], r["version"])
                if err == "GONE":
                    _finish_reply(r, None)
                    continue
                _state(sid, "NO_INSERT" if err == "NO_INSERT" else "FAILED")
                break
            _finish_reply(r, got)
            _state(sid, None)
            sent += 1
        return sent


def _ripe() -> list[str]:
    out = []
    for r in db.q(f"""WITH asked AS ({_ASKED})
                       SELECT s.id,MAX(c.touched) newest,MAX(c.noticed) noticed,st.failed_at FROM asked a
                       JOIN issue_comments c ON c.source_id=a.source_id AND c.page=a.page AND c.discussion=a.discussion
                       JOIN sources s ON s.id=c.source_id AND s.notion_page_id=c.page JOIN channels ch ON ch.id=s.channel_id
                       LEFT JOIN issue_state st ON st.source_id=s.id WHERE {_ELIGIBLE} AND s.current_run_id IS NOT NULL
                       AND c.open=1 AND c.ours=0 AND c.seen IS NULL GROUP BY s.id"""):
        age = C._age(r["newest"])
        if age is not None and age >= C.QUIET and (not r["failed_at"] or r["noticed"] > r["failed_at"]):
            out.append(r["id"])
    return out

def _work() -> list[str]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=C.QUIET)).isoformat(timespec="microseconds")
    return list(dict.fromkeys([r["source_id"] for r in db.q(f"""SELECT b.source_id FROM issue_batches b JOIN sources s ON s.id=b.source_id
           JOIN channels ch ON ch.id=s.channel_id WHERE b.state IN ('MODEL','APPLY') AND {_ELIGIBLE}
           UNION SELECT r.source_id FROM issue_replies r JOIN sources s ON s.id=r.source_id JOIN channels ch ON ch.id=s.channel_id
           LEFT JOIN issue_batches batch ON batch.id=r.batch_id LEFT JOIN issue_fixes f ON f.id=r.fix_id
           WHERE r.done IS NULL AND r.page=s.notion_page_id AND {_ELIGIBLE}
           AND ((r.ready=1 AND (r.tried_at IS NULL OR r.tried_at<?)) OR
                (r.head='〔课代表〕已改 · 改正在下一块' AND (batch.run_id!=s.current_run_id OR f.id IS NULL)))""", cutoff)] + _ripe()))


def pending() -> bool:
    return bool(_work())


def process_one(sid: str) -> None:
    # Freeze and apply under the source lock. The expensive model call must not hold up targeted webhook reads.
    with read_lock(sid):
        source = eligible(sid)
        if not source or not source["current_run_id"]:
            return
        batch = db.one("SELECT * FROM issue_batches WHERE source_id=? AND state IN ('MODEL','APPLY') ORDER BY id LIMIT 1", sid)
        if batch is None and sid in _ripe():
            try:
                payload = _payload(source)
            except Exception:
                _fail(sid, None)
                return
            if payload["threads"]:
                bid = db.exec("INSERT INTO issue_batches(source_id,run_id,page,input,created) VALUES (?,?,?,?,?)",
                              sid, source["current_run_id"], source["notion_page_id"], db.j(payload), _stamp())
                batch = db.one("SELECT * FROM issue_batches WHERE id=?", bid)
    if batch:
        root = config.get().home / "issue-asks" / str(batch["id"])
        try:
            payload = db.unj(batch["input"])
            if batch["state"] == "MODEL":
                public = {**payload, "threads": [{k: v for k, v in t.items() if not k.startswith("_")} for t in payload["threads"]]}
                output = LLM(recorder=call_recorder(f"issue-{sid}"), session=f"comments-{sid}", retry=False).structured(
                    root, f"comments-{batch['id']}", P.comments_prompt(public), P.COMMENTS_SCHEMA,
                    effort="medium", check=lambda o: _check(payload, o))
                _check(payload, output)
                db.exec("UPDATE issue_batches SET output=?,state='APPLY' WHERE id=?", db.j(output), batch["id"])
            else:
                output = db.unj(batch["output"])
            with read_lock(sid):
                try:
                    _apply(batch, output)
                except sqlite3.OperationalError:
                    # Accepted output is durable; only retry its transaction, never the model.
                    try:
                        with db.tx() as cx:
                            first = cx.execute("UPDATE issue_batches SET apply_error=1 WHERE id=? AND apply_error=0", (batch["id"],)).rowcount
                            if first:
                                cx.execute("INSERT INTO events(ts,kind,level,message) VALUES (?,?,?,?)",
                                           (now(), "issue-asks", "warn", f"频道页 {sid} 批注结果暂未入库，下次继续应用"))
                    except sqlite3.OperationalError:
                        pass  # The competing writer may also prevent diagnostics; accepted output still stays APPLY.
                    return
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            _fail(sid, batch["id"])
            return
    reply(sid)

def _fail(sid: str, bid: int | None) -> None:
    with db.tx() as cx:
        cx.execute("""INSERT INTO issue_state(source_id,failed_at,error) VALUES (?,?,'FAILED')
                      ON CONFLICT(source_id) DO UPDATE SET failed_at=excluded.failed_at,error=excluded.error""", (sid, _stamp()))
        if bid:
            cx.execute("UPDATE issue_batches SET state='FAILED' WHERE id=?", (bid,))
    db.event("issue-asks", f"频道页 {sid} 批注处理失败，等新的点名再处理", level="warn")


def due() -> None:
    # One worker thread drains page batches; webhook reads can continue during each model call.
    for sid in _work():
        process_one(sid)
