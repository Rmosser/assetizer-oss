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

import hashlib
import json
import re
import traceback
from pathlib import Path

from . import config, db
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
        "title": [{"type": "text", "text": {"content": db.setting("notion_database_title", "投资内容资产 · Assetizer")}}],
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
            "观点归原作者；本文由 Assetizer 自动整理，截图与解释性内容未经逐项事实审计。")
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
        text = f"〔{where}〕{title}" + (f"。{r['body'].strip()}" if (r["body"] or "").strip() else "") + \
               (f" 原话：「{quote}」" if quote else "")
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


def check_target(n: Notion, source: dict, cfg: dict) -> dict:
    """Live schema of the target database; raises MappingProblem (and alerts) if the mapping no longer fits."""
    ch = db.one("SELECT id, name FROM channels WHERE id=?", source["channel_id"])
    schema = M.schema_of(n._req("GET", f"/databases/{cfg['database_id']}"))

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
    return schema


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
        r = n._req("POST", f"/databases/{cfg['database_id']}/query", idempotent=True, json={"page_size": 100, "filter": {
            "or": [{"property": prop["name"], prop["type"]: {"contains": v}} for v in part]}})
        for pg in r.get("results", []):
            val = pg["properties"].get(prop["name"], {})
            text = val.get("url") or "".join(t.get("plain_text", "") for t in val.get("rich_text", []) or [])
            for v in part:
                if v in (text or "") and v not in out:
                    out[v] = {"id": pg.get("id"), "url": pg.get("url"), "title": _title(pg)}
    return out


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


def _icon(n: Notion, cfg: dict) -> dict | None:
    ic = cfg.get("icon") or "none"
    if ic == "none":
        return None
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
    n = client()
    source = db.one("SELECT * FROM sources WHERE id=?", source_id)
    run_id = source["current_run_id"]
    if not n:
        db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_NOT_CONFIGURED' WHERE id=?", source_id)
        return {"status": "BLOCKED"}
    db.exec("UPDATE sources SET delivery='DELIVERING', delivery_reason=NULL WHERE id=?", source_id)
    try:
        target = target_for(source)
        if target:
            schema = check_target(n, source, target)
            database = target["database_id"]
            ch = db.one("SELECT * FROM channels WHERE id=?", source["channel_id"])
            make_props = lambda: M.properties(target, schema, M.context(  # noqa: E731
                db.one("SELECT * FROM sources WHERE id=?", source_id), run_id, ch))
            icon = _icon(n, target)
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
            try:
                n.archive_page(page_id)
            except NotionError:
                pass
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            page_id = None
        if not page_id and target:
            have = registered(n, target, schema, source_id, exclude=(source.get("notion_page_id"),))
            if have:
                db.exec("""UPDATE sources SET delivery='REGISTERED', delivery_reason=?, notion_url=?, updated_at=? WHERE id=?""",
                        f"库里已有：{_title(have)}"[:500], have.get("url"), now(), source_id)
                db.event("delivery", f"已登记，跳过：库里已有「{_title(have)}」", run_id)
                return {"status": "REGISTERED", "url": have.get("url")}
        if not page_id:
            pg = _create(n, database, make_props(), icon)
            page_id = pg["id"]
            db.exec("UPDATE sources SET notion_page_id=?, notion_url=?, notion_synced_run=?, notion_database_id=? WHERE id=?",
                    page_id, pg["url"], run_id, database, source_id)
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
        plan = build_plan(source, run_id, target)
        done = {r["block_key"]: r for r in db.q("SELECT * FROM notion_blocks WHERE source_id=?", source_id)}
        changed = [it["key"] for it in plan if it["key"] in done and done[it["key"]]["hash"] != it["hash"]]
        orphaned = set(done) - {it["key"] for it in plan}
        if changed or orphaned:  # content edited in place (nudge, overlay, template) → rebuild the page cleanly
            try:
                n.archive_page(page_id)
            except NotionError:
                pass
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            pg = _create(n, database, make_props(), icon)
            page_id = pg["id"]
            db.exec("UPDATE sources SET notion_page_id=?, notion_url=?, notion_database_id=? WHERE id=?",
                    page_id, pg["url"], database, source_id)
            done = {}
        # reconcile: read back, adopt untracked tail blocks that match the plan, delete strays
        actual = n.children(page_id)
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
        actual = n.children(page_id)
        if len(actual) != len(plan):
            raise RuntimeError(f"READBACK_MISMATCH {len(actual)} != {len(plan)}")
        bad_media = [b["id"] for b in actual if b["type"] in ("image", "video") and b[b["type"]].get("type") != "file"]
        if bad_media:
            raise RuntimeError(f"READBACK_MEDIA {len(bad_media)}")
        n._req("PATCH", f"/pages/{page_id}", json={"properties": make_props()})
        db.exec("UPDATE sources SET delivery='DELIVERED', notion_synced_run=?, delivery_reason=NULL, delivered_at=?, updated_at=? WHERE id=?",
                run_id, now(), now(), source_id)
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
