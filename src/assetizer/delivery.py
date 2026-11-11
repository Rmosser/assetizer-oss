"""Notion delivery — independent of production (review §1.5).

* One database row per Source. There is no per-issue review (design §R2): the
  system's doubts (校注) are a count property and a folded block at the page end.
* The page body is a deterministic *plan* of keyed blocks. Appends are
  checkpointed per batch in ``notion_blocks``; before resuming, the page is read
  back and aligned with the plan so a crash between PATCH and checkpoint never
  duplicates blocks.
* Files are uploaded just-in-time (unattached uploads expire after ~1 h).
* A new current run replaces the page (old one archived) instead of producing a
  second article.
* A video that is already in the channel's database (matched on the property mapped to
  the video link) is 已登记 (REGISTERED) and skipped; the user's page is never overwritten.
* A channel with a Notion target (notion/mapping.py) is written to *its* database
  with the user's property mapping and body template; other sources go to the
  Assetizer database created under ``notion_parent_page_id``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

from . import channel_icons, config, db
from .notion import blocks as B
from .notion import mapping as M
from .notion.client import Notion, NotionError
from .util import fmt_tc, now

DB_PROPS = {
    "名称": {"title": {}},
    "频道": {"select": {}},
    "发布日期": {"date": {}},
    "来源": {"url": {}},
    "时长（分钟）": {"number": {"format": "number"}},
    "知识单元": {"number": {"format": "number"}},
    "视频片段": {"number": {"format": "number"}},
    "校注": {"number": {"format": "number"}},
    "Run": {"rich_text": {}},
}


TOKEN_FIX = ("在本机终端执行 `lzc-cli docker -- exec -i cloudlazycatappassetizer-web-1 sh -c 'umask 077; "
             "cat > /lzcapp/var/assetizer/data/secrets/notion-token'`，粘贴新 token 后按 Ctrl-D；"
             "并把 Integration 加到目标数据库（数据库右上角 ··· → Connections）。")


def token_invalid(e: Exception) -> bool:
    return isinstance(e, NotionError) and e.status == 401


def client() -> Notion | None:
    tok = config.get().secret("notion-token")
    return Notion(tok) if tok else None


def ensure_database(n: Notion) -> str:
    did = db.setting("notion_database_id")
    if did:
        return did
    parent = db.setting("notion_parent_page_id")
    if not parent:
        raise RuntimeError("NOTION_NOT_CONFIGURED")
    r = n._req("POST", "/databases", json={
        "parent": {"type": "page_id", "page_id": parent}, "is_inline": True,
        "icon": {"type": "emoji", "emoji": "🎬"},
        "title": [{"type": "text", "text": {"content": db.setting("notion_database_title", "课代表")}}],
        "properties": DB_PROPS})
    db.set_setting("notion_database_id", r["id"])
    db.set_setting("notion_schema", 2)
    return r["id"]


def ensure_props(n: Notion, database: str) -> None:
    """Databases created before design §R2 lack the 校注 column; add it once (old columns are left alone)."""
    if db.setting("notion_schema") == 2:
        return
    n._req("PATCH", f"/databases/{database}", json={"properties": {"校注": DB_PROPS["校注"]}})
    db.set_setting("notion_schema", 2)


# ---------------------------------------------------------------- plan
def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def build_plan(source: dict, run_id: str, target: dict | None = None) -> list[dict]:
    """Ordered plan items: {key, block | media:{path, kind, caption}}."""
    from . import templates
    tpl = (target or {}).get("template") or "default"
    plan = templates.PLANS[tpl](source, run_id, target or {}) if tpl != "default" else _default_plan(source, run_id)
    plan += _tail(source, run_id)
    for it in plan:
        it["hash"] = _h(it.get("block") or {"media": {**it["media"], "sha": Path(it["media"]["path"]).stat().st_size}})
    return plan


def _default_plan(source: dict, run_id: str) -> list[dict]:
    from .templates import composition
    plan: list[dict] = []
    head = (f"来源：{source.get('channel_title') or ''} · {source.get('url') or ''} · 时长 {fmt_tc(source.get('duration') or 0)}。"
            "观点归原作者；本文由课代表自动整理，截图与解释性内容未经逐项事实审计。")
    plan.append({"key": "head", "block": B.callout(head, "🧭")})
    for bi, blk in enumerate(composition(run_id)):
        k = blk["section_id"]
        if blk.get("video"):
            v = blk["video"]
            cap = f"{fmt_tc(v['start'])}–{fmt_tc(v['end'])}" + (" · 烧录字幕（实验）" if v["subtitled"] else "") + \
                  ("" if v["standalone"] else " · 依赖前文")
            # video goes right after the section heading (video → screenshots → text)
            md_lines = blk["markdown"].strip().splitlines()
            heading = md_lines[0] if md_lines and md_lines[0].startswith("#") else None
            body = "\n".join(md_lines[1:] if heading else md_lines)
            if heading:
                for j, b in enumerate(B.markdown(heading)):
                    plan.append({"key": f"{k}:h{j}", "block": b})
            plan.append({"key": f"{k}:video", "media": {"path": v["path"], "kind": "video", "caption": cap}})
            for si, sc in enumerate(blk["screenshots"]):
                plan.append({"key": f"{k}:shot{si}", "media": {"path": sc["path"], "kind": "image",
                                                                "caption": f"{fmt_tc(sc['t'])} · {sc['reason']}"}})
            for j, b in enumerate(B.markdown(body)):
                plan.append({"key": f"{k}:p{j}", "block": b})
        else:
            for si, sc in enumerate(blk.get("screenshots", [])):
                plan.append({"key": f"{k}:shot{si}", "media": {"path": sc["path"], "kind": "image",
                                                                "caption": f"{fmt_tc(sc['t'])} · {sc['reason']}"}})
            for j, b in enumerate(B.markdown(blk["markdown"])):
                plan.append({"key": f"{k}:p{j}", "block": b})
    return plan


def _tail(source: dict, run_id: str) -> list[dict]:
    """Run info, then 校注 last on the page so adding it to an already delivered page only appends."""
    run = db.one("SELECT * FROM runs WHERE id=?", run_id)
    summ = db.unj(run["summary"], {})
    plan: list[dict] = []
    info = [B.block("paragraph", f"Run {run_id} · 模型调用 {(summ.get('model') or {}).get('calls', '?')} 次 · "
                                 f"知识单元 {summ.get('units', '?')} · 生成于 {run.get('finished_at') or ''}"),
            B.block("paragraph", f"发布素材包：{source.get('package_path') or '（未生成）'}")]
    plan.append({"key": "runinfo", "block": B.toggle("运行信息与素材索引", info)})
    notes = _notes(run_id)
    if notes:
        plan.append({"key": "notes", "block": B.toggle(
            f"校注 {len(notes)} 处：系统对片段边界与陈述忠实度没有把握的地方，仅供参考", notes)})
    return plan


def _notes(run_id: str) -> list[dict]:
    """One bullet per 校注, in source order: time · unit — what the system doubts — the speaker's words."""
    rows = db.q("""SELECT f.title, f.body, f.evidence, u.title unit, u.start FROM flags f
                   LEFT JOIN units u ON u.id=f.target_id WHERE f.run_id=? AND f.severity!='INFO'
                   ORDER BY COALESCE(u.start, -1), f.id""", run_id)
    out = []
    for r in rows:
        title = re.sub(r"^「[^」]*」\s*", "", r["title"] or "").strip() or (r["title"] or "")
        where = f"{fmt_tc(r['start'])} {r['unit']}" if r["unit"] else "全期"
        quote = (db.unj(r["evidence"], {}) or {}).get("quote")
        body = (r["body"] or "").strip()
        said = body if body.startswith(title) else title + (f"。{body}" if body else "")  # a title may be its body, cut
        text = f"〔{where}〕{said}" + (f" 原话：「{quote}」" if quote else "")
        out.append(B.block("bulleted_list_item", text))
    return out


# ---------------------------------------------------------------- deliver
def _props(source: dict, run_id: str) -> dict:
    run = db.one("SELECT summary FROM runs WHERE id=?", run_id)
    summ = db.unj(run["summary"], {}) if run else {}
    clips = sum(v for k, v in (summ.get("assets") or {}).items() if k.startswith("clip:"))
    pub = source.get("published_at")
    date = f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if pub and len(pub) == 8 else None
    notes = db.one("SELECT COUNT(*) n FROM flags WHERE source_id=? AND run_id=? AND severity!='INFO'", source["id"], run_id)["n"]
    props = {
        "名称": {"title": [{"type": "text", "text": {"content": (source.get("title") or source["id"])[:1900]}}]},
        "频道": {"select": {"name": (source.get("channel_title") or "未知频道")[:90].replace(",", " ")}},
        "来源": {"url": source.get("url")},
        "时长（分钟）": {"number": round((source.get("duration") or 0) / 60, 1)},
        "知识单元": {"number": summ.get("units")},
        "视频片段": {"number": clips},
        "校注": {"number": notes},
        "Run": {"rich_text": [{"type": "text", "text": {"content": run_id}}]},
    }
    if date:
        props["发布日期"] = {"date": {"start": date}}
    return props


def target_for(source: dict) -> dict | None:
    """The channel's Notion target (notion/mapping.py), or None for the Assetizer database."""
    cfg = M.get(source.get("channel_id"))
    return cfg if cfg and cfg.get("database_id") else None


def has_target(source: dict) -> bool:
    return bool(target_for(source) or db.setting("notion_database_id") or db.setting("notion_parent_page_id"))


class MappingProblem(RuntimeError):
    pass


def check_target(n: Notion, source: dict, cfg: dict) -> tuple[dict, dict]:
    """Live schema of the target database and the mapping synced with it (renamed properties and options followed,
    and stored); raises MappingProblem (and alerts) if the mapping no longer fits."""
    ch = db.one("SELECT id, name FROM channels WHERE id=?", source["channel_id"])
    schema = M.schema_of(n._req("GET", f"/databases/{cfg['database_id']}"))
    cfg = M.sync(cfg, schema)
    M.refresh(ch["id"], cfg["database_id"], schema)

    def page_ok(pid: str) -> bool:
        try:
            return not n.page(pid).get("archived")
        except NotionError:
            return False

    probs = M.problems(cfg, schema, page_ok)
    akey = f"notion-mapping:{ch['id']}"
    if probs:
        db.raise_alert(akey, "warning", f"「{ch['name']}」的 Notion 属性映射需要调整",
                       "；".join(probs.values()) + "。这个频道的交付已暂停，改好映射后自动继续。",
                       {"label": "去频道页调整", "href": "/channels"})
        raise MappingProblem("；".join(probs.values()))
    db.resolve_alert(akey)
    return schema, cfg


def registered(n: Notion, cfg: dict, schema: dict, video_id: str, exclude: tuple = ()) -> dict | None:
    """A live page in the target database that already holds this video (by the property mapped to the video link).

    The library predates Assetizer: when the video is already there, the issue is 已登记 and skipped
    rather than written a second time."""
    prop = next((schema[pid] for pid, m in (cfg.get("properties") or {}).items()
                 if m.get("source") == "video_url" and pid in schema and schema[pid]["type"] in ("url", "rich_text")), None)
    if not prop or not video_id:
        return None
    r = n._req("POST", f"/databases/{cfg['database_id']}/query", idempotent=True, json={
        "page_size": 10, "filter": {"property": prop["name"], prop["type"]: {"contains": video_id}}})
    return next((p for p in r.get("results", []) if p["id"] not in exclude and not p.get("archived")), None)


def registered_many(source_like: dict, video_ids: list[str]) -> dict[str, dict]:
    """video id → library page, for the videos of one channel already in its database (one query)."""
    cfg = target_for(source_like)
    n = client() if cfg else None
    if not n or not video_ids:
        return {}
    schema = M.schema_of(n._req("GET", f"/databases/{cfg['database_id']}"))
    prop = next((schema[pid] for pid, m in (cfg.get("properties") or {}).items()
                 if m.get("source") == "video_url" and pid in schema and schema[pid]["type"] in ("url", "rich_text")), None)
    if not prop:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        part = video_ids[i:i + 50]
        body: dict = {"page_size": 100, "filter": {"or": [{"property": prop["name"], prop["type"]: {"contains": v}} for v in part]}}
        while True:  # a library with duplicates can answer more than a page
            r = n._req("POST", f"/databases/{cfg['database_id']}/query", idempotent=True, json=body)
            for pg in r.get("results", []):
                val = pg["properties"].get(prop["name"], {})
                text = val.get("url") or "".join(t.get("plain_text", "") for t in val.get("rich_text", []) or [])
                for v in part:
                    if v in (text or "") and v not in out:
                        out[v] = {"id": pg.get("id"), "url": pg.get("url"), "title": _title(pg)}
            if not r.get("has_more") or not r.get("next_cursor"):
                break
            body = {**body, "start_cursor": r["next_cursor"]}
    return out


# ---------------------------------------------------------------- a page written by hand (a05)
_MMDD = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{4}))?(?!\d)")
_NONWORD = re.compile(r"[\W_]+", re.U)


def _title_day(title: str | None, ref: dt.date) -> str | None:
    """The day a title names (MM/DD or MM/DD/YYYY), in the year of ``ref`` (the year before, for 12/31 named early in
    January)."""
    m = _MMDD.search(title or "")
    if not m:
        return None
    try:
        d = dt.date(int(m[3] or ref.year), int(m[1]), int(m[2]))
    except ValueError:
        return None
    return (d.replace(year=d.year - 1) if not m[3] and d > ref + dt.timedelta(days=1) else d).isoformat()


def days_of(source: dict) -> set[str]:
    """The days a video may be filed under by hand: YouTube's upload day (UTC), the day it went up in New York (an
    evening review is of that day) and the day its title names."""
    out: set[str] = set()
    up = source.get("published_at") or ""
    ref = dt.date(int(up[:4]), int(up[4:6]), int(up[6:8])) if re.fullmatch(r"\d{8}", up) else dt.date.today()
    if re.fullmatch(r"\d{8}", up):
        out.add(ref.isoformat())
    ts = (db.unj(source.get("info"), {}) or {}).get("timestamp")
    if ts:
        out.add(dt.datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).date().isoformat())
    if t := _title_day(source.get("title"), ref):
        out.add(t)
    return out


def maybe_written(pages: list[dict], days: set[str], title: str | None, by_title: bool) -> dict | None:
    """The page written by hand (hand_written) a video may be: one of its days for a channel filed by date, its own
    title for a channel titled with the video's."""
    want = _NONWORD.sub("", title or "").lower()
    for pg in pages:
        if (by_title and want and _NONWORD.sub("", pg.get("title") or "").lower() == want) or \
                (not by_title and pg.get("date") in days):
            return pg
    return None


def _by_title(cfg: dict, schema: dict) -> bool:
    """The channel's pages carry the video's own title (王海滨), not a date in theirs (汤姆): compared by title."""
    t = next((m for pid, m in (cfg.get("properties") or {}).items() if (schema.get(pid) or {}).get("type") == "title"), {})
    return "{video_title}" in (t.get("value") or "")


def hand_written(n: Notion, cfg: dict, schema: dict) -> list[dict] | None:
    """The channel's pages in its database with no video link — written by hand, before Assetizer or beside it:
    [{id, url, title, date}]. None: the mapping has no fixed select (内容所属) to tell the channel's pages by, or no
    video link."""
    props = cfg.get("properties") or {}
    own = next(((pid, m) for pid, m in props.items() if m.get("source") == "fixed" and m.get("value")
                and (schema.get(pid) or {}).get("type") == "select"), None)
    link = next((pid for pid, m in props.items() if m.get("source") == "video_url"
                 and (schema.get(pid) or {}).get("type") in ("url", "rich_text")), None)
    if not own or not link:
        return None
    day = next((schema[pid]["name"] for pid, m in props.items() if m.get("source") in ("published_date", "published_datetime")
                and (schema.get(pid) or {}).get("type") == "date"), None)
    ours = {r["notion_page_id"] for r in db.q("SELECT notion_page_id FROM sources WHERE notion_page_id IS NOT NULL")}
    body: dict = {"page_size": 100, "filter": {"and": [
        {"property": schema[own[0]]["name"], "select": {"equals": own[1]["value"]}},
        {"property": schema[link]["name"], schema[link]["type"]: {"is_empty": True}}]}}
    out = []
    while True:
        r = n._req("POST", f"/databases/{cfg['database_id']}/query", idempotent=True, json=body)
        for pg in r.get("results", []):
            if pg.get("archived") or pg.get("id") in ours:
                continue
            d = ((pg.get("properties", {}).get(day) or {}).get("date") or {}).get("start") if day else None
            out.append({"id": pg.get("id"), "url": pg.get("url"), "title": _title(pg), "date": (d or "")[:10] or None})
        if not r.get("has_more") or not r.get("next_cursor"):
            return out
        body = {**body, "start_cursor": r["next_cursor"]}


def maybe_many(source_like: dict, entries: list[dict]) -> dict[str, dict]:
    """video id → the page written by hand each of a channel's listed videos may be (the 补录 list's 可能已有). The
    listing dates videos roughly ("3 weeks ago"): only an exact date counts, and the MM/DD a title names."""
    cfg = target_for(source_like)
    n = client() if cfg else None
    if not n or not entries:
        return {}
    schema = M.schema_of(n._req("GET", f"/databases/{cfg['database_id']}"))
    pages = hand_written(n, cfg, schema)
    if not pages:
        return {}
    by_title, out = _by_title(cfg, schema), {}
    for e in entries:
        pub = (e.get("published") or "")[:10]
        ref = dt.date.fromisoformat(pub) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pub) else dt.date.today()
        days = {d for d in (None if e.get("approx", True) else pub, _title_day(e.get("title"), ref)) if d}
        if pg := maybe_written(pages, days, e.get("title"), by_title):
            out[e["id"]] = pg
    return out


def _came_by_backfill(source_id: str) -> bool:
    return bool(db.one("SELECT 1 FROM runs WHERE source_id=? AND origin='backfill'", source_id))


# ---------------------------------------------------------------- what someone did on a page of ours (a07)
def _yours(n: Notion, source_id: str, page_id: str) -> list[str]:
    """What someone other than Assetizer did on its page, in words — [] when it is as Assetizer left it. Comments count
    only where the integration may read them."""
    bot = _bot(n)
    if not bot:
        raise RuntimeError("读不到课代表在 Notion 里的身份，换页前看不出页上有没有你的改动；稍后再试")
    blocks = _blocks_of(n, page_id, bot)
    out = []
    if any((b.get("created_by") or {}).get("id") != bot for b in blocks):
        out.append("加了段落")
    if any((b.get("created_by") or {}).get("id") == bot and (b.get("last_edited_by") or {}).get("id") not in (None, bot)
           for b in blocks):
        out.append("改了段落")
    last = db.setting(f"notion_props:{source_id}") or {}
    if last and any(pid in last and last[pid] != h for pid, h in _prop_hashes(n.page(page_id).get("properties")).items()):
        out.append("改了属性")
    if _commented(n, [page_id] + [b["id"] for b in blocks if (b.get("created_by") or {}).get("id") == bot], bot):
        out.append("加了评论")
    return out


def _commented(n: Notion, ids: list[str], bot: str) -> bool:
    """Someone else commented on the page or on one of these blocks. Notion lists a page's comments without those on
    its blocks, so each is asked for (only when a page is about to be replaced); no right to read comments: none."""
    for bid in ids:
        try:
            said = n._req("GET", f"/comments?block_id={bid}&page_size=100", idempotent=True).get("results", [])
        except NotionError as e:
            if e.status == 403:
                return False
            if e.status in (400, 404):
                continue
            raise
        if any((c.get("created_by") or {}).get("id") != bot for c in said):
            return True
    return False


def _blocks_of(n: Notion, block_id: str, bot: str) -> list[dict]:
    """The page's blocks, and inside each block Assetizer made that has blocks of its own (a fold, a callout, a table)
    those too: an edit inside a fold is an edit. A block someone else made counts as theirs whole; a sub-page is a page."""
    out = []
    for b in n.children(block_id):
        out.append(b)
        if (b.get("has_children") and (b.get("created_by") or {}).get("id") == bot
                and b.get("type") not in ("child_page", "child_database")):
            out += _blocks_of(n, b["id"], bot)
    return out


def _keep_old_page(n: Notion, source: dict, page_id: str, run_id: str) -> bool:
    """Replacing ``page_id`` (an issue printed again, a new version, content rebuilt): held instead when someone did
    something on it, unless they said to replace it (a07). True: held."""
    sid = source["id"]
    if db.setting(f"replace_anyway:{sid}"):
        return False
    found = _yours(n, sid, page_id)
    if not found:
        return False
    db.set_setting(f"yours_on_page:{sid}", found)
    db.exec("UPDATE sources SET delivery='HELD', delivery_reason='YOURS_ON_PAGE', updated_at=? WHERE id=?", now(), sid)
    db.event("delivery", f"旧页上有你的改动（{'、'.join(found)}）：等你决定换不换页", run_id)
    return True


def _bot(n: Notion) -> str | None:
    """The user id of Assetizer's integration: what it created on a page is its own (None: Notion would not say)."""
    try:
        return n.me().get("id")
    except NotionError:
        return None


def _prop_hashes(props: dict | None) -> dict:
    """property id → a hash of its value as Notion answered it after Assetizer wrote it."""
    return {v["id"]: hashlib.sha256(json.dumps({k: x for k, x in v.items() if k != "id"}, sort_keys=True,
                                               ensure_ascii=False).encode()).hexdigest()[:16]
            for v in (props or {}).values() if isinstance(v, dict) and v.get("id")}


def _keep_yours(n: Notion, page_id: str, source_id: str, props: dict) -> dict:
    """The properties to write, less those someone changed in Notion since Assetizer last wrote them."""
    last = db.setting(f"notion_props:{source_id}") or {}
    if not last:
        return props
    cur = n.page(page_id).get("properties") or {}
    edited = {pid for pid, h in _prop_hashes(cur).items() if pid in last and last[pid] != h}
    pid_of = {name: v.get("id") for name, v in cur.items() if isinstance(v, dict)}
    return {k: v for k, v in props.items() if pid_of.get(k, k) not in edited}


def _ours_unclaimed(n: Notion, page: dict, source_id: str) -> bool:
    """A library page Assetizer's integration made that no issue holds: the page of an earlier try whose answer
    (the page id) was lost — not a page someone wrote by hand."""
    who = (page.get("created_by") or {}).get("id")
    if not who or db.one("SELECT 1 FROM sources WHERE notion_page_id=? AND id!=?", page.get("id"), source_id):
        return False
    try:
        return who == n.me().get("id")
    except NotionError:
        return False


def _title(pg: dict) -> str:
    for v in pg.get("properties", {}).values():
        if v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v["title"])
    return ""


def registered_in_target(source: dict) -> dict | None:
    """For intake: is this video already in the channel's database? Never raises (intake must go on)."""
    cfg = target_for(source)
    n = client() if cfg else None
    if not n:
        return None
    try:
        schema = M.schema_of(n._req("GET", f"/databases/{cfg['database_id']}"))
        return registered(n, cfg, schema, source["id"])
    except Exception as e:  # noqa: BLE001
        db.event("ingest", f"查重失败，照常收录：{e}", level="warn")
        return None


def _icon(n: Notion, cfg: dict, channel_id: str | None) -> dict | None:
    ic = cfg.get("icon") or "none"
    if ic == "none":
        return None
    if ic == "channel":
        url = channel_icons.kept(channel_id) if channel_id else None
        return channel_icons.external(url) if url else None
    if ic != "parent":
        return {"type": "emoji", "emoji": ic}
    rel = next((m["value"][0] for m in (cfg.get("properties") or {}).values()
                if m.get("type") == "relation" and m.get("source") == "fixed" and m.get("value")), None)
    if not rel:
        return None
    try:
        icon = n.page(rel).get("icon")
    except NotionError:
        return None
    if not icon or icon.get("type") == "file":  # uploaded icons come back as expiring URLs
        return None
    return {k: v for k, v in icon.items() if k in ("type", icon.get("type"))}


def _create(n: Notion, database: str, props: dict, icon: dict | None) -> dict:
    body = {"parent": {"database_id": database}, "properties": props}
    if icon:
        try:
            return n._req("POST", "/pages", json={**body, "icon": icon})
        except NotionError as e:
            if e.status != 400 or "icon" not in e.body:
                raise
            db.event("delivery", f"Notion 不接受这个图标，已不设图标：{e.body[:160]}", level="warn")
    return n._req("POST", "/pages", json=body)


def deliver(source_id: str) -> dict:
    source = db.one("SELECT * FROM sources WHERE id=?", source_id)
    run_id = source["current_run_id"]
    if db.one("SELECT 1 FROM runs WHERE id=? AND production='COMPLETE_PARTIAL'", run_id):
        # a stage that does not stop a run left this version short (clips, stills, checks): not written, by whatever way
        # it came here, until a retry of it completes (a08); adopt_completed lifts the hold
        db.exec("UPDATE sources SET delivery='HELD', delivery_reason='PARTIAL', updated_at=? WHERE id=?", now(), source_id)
        return {"status": "HELD"}
    n = client()
    if not n:
        db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_NOT_CONFIGURED' WHERE id=?", source_id)
        return {"status": "BLOCKED"}
    db.exec("UPDATE sources SET delivery='DELIVERING', delivery_reason=NULL WHERE id=?", source_id)
    try:
        target = target_for(source)
        if target:
            schema, target = check_target(n, source, target)
            database = target["database_id"]
            ch = db.one("SELECT * FROM channels WHERE id=?", source["channel_id"])
            make_props = lambda: M.properties(target, schema, M.context(  # noqa: E731
                db.one("SELECT * FROM sources WHERE id=?", source_id), run_id, ch))
            icon = _icon(n, target, source["channel_id"])
        else:
            database = ensure_database(n)
            ensure_props(n, database)
            make_props = lambda: _props(db.one("SELECT * FROM sources WHERE id=?", source_id), run_id)  # noqa: E731
            icon = {"type": "emoji", "emoji": "📘"}
        page_id = source.get("notion_page_id")
        if page_id and (source.get("notion_database_id") or database) != database:
            # the channel now writes elsewhere: start a page there, leave the old one where it is
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            page_id = None
        if page_id and source.get("notion_synced_run") and source["notion_synced_run"] != run_id:
            if _keep_old_page(n, source, page_id, run_id):
                return {"status": "HELD"}
            try:
                n.archive_page(page_id)
            except NotionError as e:  # gone already is fine; anything else: try again later rather than make a second page
                if e.status != 404 and "archived" not in e.body.lower():
                    raise
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            page_id = None
        if not page_id and target:
            have = registered(n, target, schema, source_id, exclude=(source.get("notion_page_id"),))
            if have and _ours_unclaimed(n, have, source_id):  # made by an earlier try whose answer was lost: write it
                page_id = have["id"]
                db.exec("UPDATE sources SET notion_page_id=?, notion_url=?, notion_synced_run=?, notion_database_id=? WHERE id=?",
                        page_id, have.get("url"), run_id, database, source_id)
                db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
                have = None
            if have:
                db.exec("""UPDATE sources SET delivery='REGISTERED', delivery_reason=?, notion_url=?, updated_at=? WHERE id=?""",
                        f"库里已有：{_title(have)}"[:500], have.get("url"), now(), source_id)
                db.event("delivery", f"已登记，跳过：库里已有「{_title(have)}」", run_id)
                return {"status": "REGISTERED", "url": have.get("url")}
            if not page_id and _came_by_backfill(source_id) and not db.setting(f"write_anyway:{source_id}"):
                # an older video taken in late: the user may have written its page by hand, with no link (a05)
                maybe = maybe_written(hand_written(n, target, schema) or [], days_of(source), source.get("title"),
                                      _by_title(target, schema))
                if maybe:
                    db.set_setting(f"maybe_have:{source_id}", maybe)
                    db.exec("UPDATE sources SET delivery='HELD', delivery_reason='MAYBE_HAVE', updated_at=? WHERE id=?",
                            now(), source_id)
                    db.event("delivery", f"库里可能已有「{maybe['title']}」：等你确认", run_id)
                    return {"status": "HELD"}
        if not page_id:
            pg = _create(n, database, make_props(), icon)
            channel_icons.note(source_id, pg)
            page_id = pg["id"]
            db.exec("UPDATE sources SET notion_page_id=?, notion_url=?, notion_synced_run=?, notion_database_id=? WHERE id=?",
                    page_id, pg["url"], run_id, database, source_id)
            db.set_setting(f"notion_props:{source_id}", _prop_hashes(pg.get("properties")))
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
        plan = build_plan(source, run_id, target)
        done = {r["block_key"]: r for r in db.q("SELECT * FROM notion_blocks WHERE source_id=?", source_id)}
        changed = [it["key"] for it in plan if it["key"] in done and done[it["key"]]["hash"] != it["hash"]]
        orphaned = set(done) - {it["key"] for it in plan}
        if changed or orphaned:  # content edited in place (nudge, overlay, template) → rebuild the page cleanly
            if _keep_old_page(n, source, page_id, run_id):
                return {"status": "HELD"}
            try:
                n.archive_page(page_id)
            except NotionError:
                pass
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            pg = _create(n, database, make_props(), icon)
            channel_icons.note(source_id, pg)
            page_id = pg["id"]
            db.exec("UPDATE sources SET notion_page_id=?, notion_url=?, notion_database_id=? WHERE id=?",
                    page_id, pg["url"], database, source_id)
            db.set_setting(f"notion_props:{source_id}", _prop_hashes(pg.get("properties")))
            done = {}
        # reconcile: read back, adopt untracked tail blocks that match the plan, delete strays — of Assetizer's own
        # blocks only: what someone added on the page (a note, a heading) stays where they put it
        bot = _bot(n)
        ours = lambda b: not bot or (b.get("created_by") or {}).get("id") in (None, bot)  # noqa: E731
        actual = [b for b in n.children(page_id) if ours(b)]
        known_ids = {r["block_id"] for r in done.values()}
        nxt = [it for it in plan if it["key"] not in done]
        extras = [b for b in actual if b["id"] not in known_ids]
        for b in extras:
            it = nxt[0] if nxt else None
            if it and b["type"] == (it.get("block") or {}).get("type", it.get("media", {}).get("kind")) and \
                    (it.get("media") or B.plain(b) == B.plain(it["block"])):
                db.exec("INSERT OR REPLACE INTO notion_blocks VALUES (?,?,?,?)", source_id, it["key"], b["id"], it["hash"])
                nxt.pop(0)
            else:
                n._req("DELETE", f"/blocks/{b['id']}")
        uploads = 0
        i = 0
        while i < len(nxt):
            batch, keys = [], []
            while i < len(nxt) and len(batch) < 40:
                it = nxt[i]
                if it.get("media"):
                    m = it["media"]
                    p = Path(m["path"])
                    up = n.upload(p, "video/mp4" if m["kind"] == "video" else "image/jpeg")
                    uploads += 1
                    batch.append(B.media(m["kind"], up["id"], m["caption"]))
                else:
                    batch.append(it["block"])
                keys.append(it)
                i += 1
                if it.get("media") and m["kind"] == "video":
                    break  # attach videos promptly
            res = n.append(page_id, batch)["results"]
            for it, b in zip(keys, res):
                db.exec("INSERT OR REPLACE INTO notion_blocks VALUES (?,?,?,?)", source_id, it["key"], b["id"], it["hash"])
        # verify
        actual = [b for b in n.children(page_id) if ours(b)]
        if len(actual) != len(plan):
            raise RuntimeError(f"READBACK_MISMATCH {len(actual)} != {len(plan)}")
        bad_media = [b["id"] for b in actual if b["type"] in ("image", "video") and b[b["type"]].get("type") != "file"]
        if bad_media:
            raise RuntimeError(f"READBACK_MEDIA {len(bad_media)}")
        res = n._req("PATCH", f"/pages/{page_id}", json={"properties": _keep_yours(n, page_id, source_id, make_props())})
        db.set_setting(f"notion_props:{source_id}", _prop_hashes((res or {}).get("properties")))
        db.exec("UPDATE sources SET delivery='DELIVERED', notion_synced_run=?, delivery_reason=NULL, delivered_at=?, updated_at=? WHERE id=?",
                run_id, now(), now(), source_id)
        for k in ("write_anyway", "replace_anyway"):  # said for this page: the next one is asked about again
            if db.setting(f"{k}:{source_id}"):
                db.set_setting(f"{k}:{source_id}", None)
        db.event("delivery", f"Notion 交付完成：{len(plan)} 块，{uploads} 个文件", run_id)
        db.resolve_alert("notion-auth")
        return {"status": "DELIVERED", "blocks": len(plan), "uploads": uploads}
    except Exception as e:  # noqa: BLE001
        reason = str(e)
        if token_invalid(e):  # every delivery fails until the token is replaced: say so once, loudly
            db.raise_alert("notion-auth", "critical", "Notion token 已失效，交付暂停", TOKEN_FIX)
        if isinstance(e, MappingProblem):
            db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason=?, updated_at=? WHERE id=?",
                    f"NOTION_MAPPING：{reason}"[:500], now(), source_id)
            return {"status": "BLOCKED", "error": reason}
        if "NOTION_NOT_CONFIGURED" in reason:
            db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_NOT_CONFIGURED' WHERE id=?", source_id)
            return {"status": "BLOCKED"}
        db.exec("UPDATE sources SET delivery='FAILED', delivery_reason=?, updated_at=? WHERE id=?", reason[:500], now(), source_id)
        db.event("delivery", f"Notion 交付失败：{reason[:200]}", run_id, "error", {"tb": traceback.format_exc()[-1500:]})
        return {"status": "FAILED", "error": reason}


def deliver_pending() -> bool:
    if not client():
        return False
    for r in db.q("""SELECT s.* FROM sources s LEFT JOIN channels c ON c.id=s.channel_id
                  WHERE s.current_run_id IS NOT NULL
                  AND (s.delivery IN ('NOT_STARTED','STALE')
                       OR (s.delivery='BLOCKED' AND COALESCE(s.delivery_reason, '') NOT LIKE 'NOTION_MAPPING%')
                       OR (s.delivery IN ('FAILED','BLOCKED') AND s.updated_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 minutes')))
                  AND (c.id IS NULL OR c.stage='LOCKED')  -- tuning samples wait for 定稿 (design §R2-5)
                  ORDER BY s.updated_at"""):
        if has_target(r):
            return deliver(r["id"])["status"] != "BLOCKED"
    return False
