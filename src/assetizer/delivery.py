"""Notion delivery — independent of production (review §1.5).

* One database row per Source; review state is a select property.
* The page body is a deterministic *plan* of keyed blocks. Appends are
  checkpointed per batch in ``notion_blocks``; before resuming, the page is read
  back and aligned with the plan so a crash between PATCH and checkpoint never
  duplicates blocks.
* Files are uploaded just-in-time (unattached uploads expire after ~1 h).
* A new current run replaces the page (old one archived) instead of producing a
  second article.
"""
from __future__ import annotations

import hashlib
import json
import re
import traceback
from pathlib import Path

from . import config, db
from .notion import blocks as B
from .notion.client import Notion, NotionError
from .util import fmt_tc, now, read_json

STATUS = {"UNREVIEWED": "待审阅", "IN_REVIEW": "审阅中", "APPROVED": "已通过", "CHANGES_REQUESTED": "需修改", "REJECTED": "已驳回"}
DB_PROPS = {
    "名称": {"title": {}},
    "频道": {"select": {}},
    "状态": {"select": {"options": [{"name": v, "color": c} for v, c in
                                    zip(STATUS.values(), ["yellow", "blue", "green", "orange", "red"])]}},
    "发布日期": {"date": {}},
    "来源": {"url": {}},
    "时长（分钟）": {"number": {"format": "number"}},
    "知识单元": {"number": {"format": "number"}},
    "视频片段": {"number": {"format": "number"}},
    "待判断": {"number": {"format": "number"}},
    "Run": {"rich_text": {}},
}


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
    return r["id"]


# ---------------------------------------------------------------- plan
def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def build_plan(source: dict, run_id: str) -> list[dict]:
    """Ordered plan items: {key, block | media:{path, kind, caption}}."""
    s = config.get()
    comp = read_json(s.runs_dir / run_id / "composition.json")
    run = db.one("SELECT * FROM runs WHERE id=?", run_id)
    summ = db.unj(run["summary"], {})
    plan: list[dict] = []
    head = (f"来源：{source.get('channel_title') or ''} · {source.get('url') or ''} · 时长 {fmt_tc(source.get('duration') or 0)}。"
            "观点归原作者；本文由 Assetizer 自动整理，截图与解释性内容未经逐项事实审计。")
    plan.append({"key": "head", "block": B.callout(head, "🧭")})
    for bi, blk in enumerate(comp["blocks"]):
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
    info = [B.block("paragraph", f"Run {run_id} · 模型调用 {(summ.get('model') or {}).get('calls', '?')} 次 · "
                                 f"知识单元 {summ.get('units', '?')} · 生成于 {run.get('finished_at') or ''}"),
            B.block("paragraph", f"发布素材包：{source.get('package_path') or '（未生成）'}")]
    plan.append({"key": "runinfo", "block": B.toggle("运行信息与素材索引", info)})
    for it in plan:
        it["hash"] = _h(it.get("block") or {"media": {**it["media"], "sha": Path(it["media"]["path"]).stat().st_size}})
    return plan


# ---------------------------------------------------------------- deliver
def _props(source: dict, run_id: str) -> dict:
    run = db.one("SELECT summary FROM runs WHERE id=?", run_id)
    summ = db.unj(run["summary"], {}) if run else {}
    clips = sum(v for k, v in (summ.get("assets") or {}).items() if k.startswith("clip:"))
    pub = source.get("published_at")
    date = f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if pub and len(pub) == 8 else None
    flags = db.one("SELECT COUNT(*) n FROM flags WHERE source_id=? AND run_id=? AND resolution='OPEN' AND severity!='INFO'",
                   source["id"], run_id)["n"]
    props = {
        "名称": {"title": [{"type": "text", "text": {"content": (source.get("title") or source["id"])[:1900]}}]},
        "频道": {"select": {"name": (source.get("channel_title") or "未知频道")[:90].replace(",", " ")}},
        "状态": {"select": {"name": STATUS.get(source.get("review") or "UNREVIEWED", "待审阅")}},
        "来源": {"url": source.get("url")},
        "时长（分钟）": {"number": round((source.get("duration") or 0) / 60, 1)},
        "知识单元": {"number": summ.get("units")},
        "视频片段": {"number": clips},
        "待判断": {"number": flags},
        "Run": {"rich_text": [{"type": "text", "text": {"content": run_id}}]},
    }
    if date:
        props["发布日期"] = {"date": {"start": date}}
    return props


def deliver(source_id: str) -> dict:
    n = client()
    source = db.one("SELECT * FROM sources WHERE id=?", source_id)
    run_id = source["current_run_id"]
    if not n:
        db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_NOT_CONFIGURED' WHERE id=?", source_id)
        return {"status": "BLOCKED"}
    db.exec("UPDATE sources SET delivery='DELIVERING', delivery_reason=NULL WHERE id=?", source_id)
    try:
        database = ensure_database(n)
        page_id = source.get("notion_page_id")
        if page_id and source.get("notion_synced_run") and source["notion_synced_run"] != run_id:
            try:
                n.archive_page(page_id)
            except NotionError:
                pass
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            page_id = None
        if not page_id:
            pg = n._req("POST", "/pages", json={"parent": {"database_id": database}, "icon": {"type": "emoji", "emoji": "📘"},
                                                 "properties": _props(source, run_id)})
            page_id = pg["id"]
            db.exec("UPDATE sources SET notion_page_id=?, notion_url=?, notion_synced_run=? WHERE id=?",
                    page_id, pg["url"], run_id, source_id)
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
        plan = build_plan(source, run_id)
        done = {r["block_key"]: r for r in db.q("SELECT * FROM notion_blocks WHERE source_id=?", source_id)}
        changed = [it["key"] for it in plan if it["key"] in done and done[it["key"]]["hash"] != it["hash"]]
        orphaned = set(done) - {it["key"] for it in plan}
        if changed or orphaned:  # content edited in place (nudge, overlay) → rebuild the page cleanly
            try:
                n.archive_page(page_id)
            except NotionError:
                pass
            db.exec("DELETE FROM notion_blocks WHERE source_id=?", source_id)
            pg = n._req("POST", "/pages", json={"parent": {"database_id": database}, "icon": {"type": "emoji", "emoji": "📘"},
                                                 "properties": _props(source, run_id)})
            page_id = pg["id"]
            db.exec("UPDATE sources SET notion_page_id=?, notion_url=? WHERE id=?", page_id, pg["url"], source_id)
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
        n._req("PATCH", f"/pages/{page_id}", json={"properties": _props(db.one("SELECT * FROM sources WHERE id=?", source_id), run_id)})
        db.exec("UPDATE sources SET delivery='DELIVERED', notion_synced_run=?, delivery_reason=NULL, updated_at=? WHERE id=?",
                run_id, now(), source_id)
        db.event("delivery", f"Notion 交付完成：{len(plan)} 块，{uploads} 个文件", run_id)
        return {"status": "DELIVERED", "blocks": len(plan), "uploads": uploads}
    except Exception as e:  # noqa: BLE001
        reason = str(e)
        if "NOTION_NOT_CONFIGURED" in reason:
            db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_NOT_CONFIGURED' WHERE id=?", source_id)
            return {"status": "BLOCKED"}
        db.exec("UPDATE sources SET delivery='FAILED', delivery_reason=?, updated_at=? WHERE id=?", reason[:500], now(), source_id)
        db.event("delivery", f"Notion 交付失败：{reason[:200]}", run_id, "error", {"tb": traceback.format_exc()[-1500:]})
        return {"status": "FAILED", "error": reason}


def sync_review(source_id: str) -> None:
    """Push review state to the Notion row (cheap property update)."""
    n = client()
    s = db.one("SELECT * FROM sources WHERE id=?", source_id)
    if not n or not s or not s.get("notion_page_id"):
        return
    try:
        n._req("PATCH", f"/pages/{s['notion_page_id']}", json={"properties": _props(s, s["current_run_id"])})
    except NotionError as e:
        db.event("delivery", f"审阅状态同步失败：{e}", s["current_run_id"], "warn")


def deliver_pending() -> bool:
    if not client():
        return False
    r = db.one("""SELECT s.id FROM sources s LEFT JOIN channels c ON c.id=s.channel_id
                  WHERE s.current_run_id IS NOT NULL
                  AND (s.delivery IN ('NOT_STARTED','STALE','BLOCKED')
                       OR (s.delivery='FAILED' AND s.updated_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 minutes')))
                  AND (c.delivery_policy IS NULL OR c.delivery_policy='auto' OR s.review='APPROVED')
                  ORDER BY s.updated_at LIMIT 1""")
    if not r:
        return False
    if not (db.setting("notion_database_id") or db.setting("notion_parent_page_id")):
        return False
    return deliver(r["id"])["status"] != "BLOCKED"
