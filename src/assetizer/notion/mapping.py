"""Per-channel Notion target: which database, how each property is filled, which page template.

A channel's config lives in the setting ``notion:{channel_id}``::

    {"database_id": "...", "template": "reading-guide", "heading": "{channel} - {date} …",
     "icon": "parent" | "none" | "<emoji>",
     "properties": {"<property id>": {"name": "内容所属", "type": "select", "source": "fixed", "value": "@交易员汤姆"}}}

Mappings are keyed by the Notion property *id*, so renaming a property in Notion is
followed automatically. A deleted property, a changed type, a select option that no
longer exists or a related page that is gone is a *problem*: delivery for the channel
pauses with an alert instead of writing the wrong thing. Properties without a mapping
are simply left alone.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import db

TEMPLATES = {
    "default": "通用（视频 → 截图 → 正文）",
    "reading-guide": "读图学习导航（案例 + 优先学习清单）",
    "chapters": "章节 + 原话参考",
}

# value sources: key → (label, property types it can fill)
TEXT = ("title", "rich_text")
SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "fixed": ("固定值", ("title", "rich_text", "select", "multi_select", "status", "relation", "number",
                         "checkbox", "url", "date")),
    "template": ("模板", ("title", "rich_text", "url")),
    "published_date": ("发布日期", ("date", "rich_text")),
    "published_datetime": ("发布日期与时间", ("date", "rich_text")),
    "access": ("会员/公开 · 直播/视频（自动判断）", ("select", "multi_select", "status", "rich_text")),
    "key_levels": ("关键位摘要（每个重点单元一行）", TEXT),
    "headline": ("本期题眼", TEXT),
    "video_title": ("视频原标题", TEXT),
    "video_url": ("视频链接", ("url", "rich_text")),
    "channel": ("频道名", TEXT + ("select",)),
    "duration_min": ("时长（分钟）", ("number",)),
    "units": ("知识单元数", ("number",)),
    "clips": ("视频片段数", ("number",)),
    "notes": ("校注数", ("number",)),
    "run": ("Run 编号", TEXT),
}
TEMPLATE_VARS = {"channel": "频道名", "date": "发布日期 2026-09-08", "date8": "发布日期 20260908",
                 "headline": "本期题眼", "video_title": "视频原标题", "url": "视频链接"}
ACCESS_DEFAULT = {"public_live": "公开直播", "member_live": "会员直播", "public_video": "公开视频", "member_video": "会员视频"}
ACCESS_LABELS = {"public_live": "公开直播", "member_live": "会员直播", "public_video": "公开视频", "member_video": "会员视频"}
NOT_WRITABLE = {"created_time", "last_edited_time", "created_by", "last_edited_by", "formula", "rollup",
                "unique_id", "verification", "button", "people", "files", "email", "phone_number"}

# Initial mappings for the channels the user already keeps in 知识领袖长期追踪库, by property name.
PRESETS: dict[str, dict] = {
    "trader-tom": {
        "template": "reading-guide", "heading": "{channel} - {date} 美股市场复盘与读图学习导航", "icon": "parent",
        "by_name": {"Name": ("template", "{channel}-{date8}-{headline}"), "内容所属": ("fixed", "@交易员汤姆"),
                    "会员/公开": ("access", None), "内容发布日期": ("published_date", None),
                    "Re-上级 项目": ("fixed", "$parent"), "AI 摘要": ("key_levels", None), "视频链接": ("video_url", None)},
    },
    "wang-haibin": {
        "template": "chapters", "heading": "", "icon": "none",
        "by_name": {"Name": ("template", "{video_title}"), "内容所属": ("fixed", "王海滨"),
                    "会员/公开": ("access", None), "内容发布日期": ("published_datetime", None),
                    "Re-上级 项目": ("fixed", "$parent"), "视频链接": ("video_url", None)},
    },
}


def key(channel_id: str) -> str:
    return f"notion:{channel_id}"


def get(channel_id: str | None) -> dict | None:
    return db.setting(key(channel_id)) if channel_id else None


def save(channel_id: str, cfg: dict) -> None:
    db.set_setting(key(channel_id), cfg)


def norm_id(s: str) -> str:
    """Accept a raw id or any notion.so / notion.com link; return the dashed UUID."""
    m = re.findall(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", (s or "").lower())
    if not m:
        raise ValueError(f"不是 Notion 页面或数据库链接：{s}")
    h = m[-1].replace("-", "")
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def preset(channel_id: str, database_id: str, schema: dict, parent: str | None) -> dict:
    """Build a config from PRESETS against the live schema (names → ids)."""
    p = PRESETS.get(channel_id, {"template": "default", "heading": "", "icon": "none", "by_name": {}})
    by_name = {v["name"]: (pid, v) for pid, v in schema.items()}
    props = {}
    for name, (source, value) in p["by_name"].items():
        if name not in by_name:
            continue
        pid, sp = by_name[name]
        if value == "$parent":
            if not parent:
                continue
            value = [norm_id(parent)]
        m = {"name": name, "type": sp["type"], "source": source}
        if value is not None:
            m["value"] = value
        if source == "access":
            m["value"] = dict(ACCESS_DEFAULT)
        props[pid] = m
    return {"database_id": database_id, "template": p["template"], "heading": p["heading"], "icon": p["icon"],
            "properties": props}


# --------------------------------------------------------------------------- schema
def schema_of(database: dict) -> dict:
    """GET /databases/{id} → {property id: {name, type, options?, relation_db?}}."""
    out = {}
    for name, p in database["properties"].items():
        t = p["type"]
        e = {"name": name, "type": t}
        if t in ("select", "multi_select", "status"):
            e["options"] = [o["name"] for o in p[t].get("options", [])]
        if t == "relation":
            e["relation_db"] = p["relation"].get("database_id")
        out[p["id"]] = e
    return out


def problems(cfg: dict, schema: dict, page_ok=lambda pid: True) -> dict[str, str]:
    """property id → what is wrong with its mapping (empty dict = good to deliver)."""
    out: dict[str, str] = {}
    for pid, m in (cfg.get("properties") or {}).items():
        sp = schema.get(pid)
        if not sp:
            out[pid] = f"Notion 里已没有「{m.get('name')}」这个属性"
            continue
        if sp["type"] != m.get("type"):
            out[pid] = f"「{sp['name']}」的类型从 {m.get('type')} 改成了 {sp['type']}"
            continue
        src = m.get("source")
        if src not in SOURCES or sp["type"] not in SOURCES[src][1]:
            out[pid] = f"「{sp['name']}」（{sp['type']}）不能用「{SOURCES.get(src, (src,))[0]}」填写"
            continue
        v = m.get("value")
        opts = sp.get("options")
        if src == "fixed" and sp["type"] in ("select", "status") and v and v not in opts:
            out[pid] = f"「{sp['name']}」没有选项「{v}」"
        elif src == "fixed" and sp["type"] == "multi_select" and any(x not in opts for x in (v or [])):
            out[pid] = f"「{sp['name']}」缺少选项：{'、'.join(x for x in v if x not in opts)}"
        elif src == "access" and sp["type"] in ("select", "multi_select", "status"):
            missing = sorted({x for x in (v or {}).values() if x and x not in opts})
            if missing:
                out[pid] = f"「{sp['name']}」没有选项：{'、'.join(missing)}"
        elif src == "template":
            bad = [x for x in re.findall(r"\{(\w+)\}", v or "") if x not in TEMPLATE_VARS]
            if bad:
                out[pid] = f"「{sp['name']}」的模板里有不认识的变量：{'、'.join(bad)}"
        elif src == "fixed" and sp["type"] == "relation":
            gone = [x for x in (v or []) if not page_ok(x)]
            if gone:
                out[pid] = f"「{sp['name']}」关联的页面已不存在或无权访问"
    title = next((pid for pid, sp in schema.items() if sp["type"] == "title"), None)
    if title and title not in (cfg.get("properties") or {}) and title not in out:
        out[title] = "标题属性必须配置"
    return out


# --------------------------------------------------------------------------- values
def access_kind(info: dict) -> str:
    live = (info.get("live_status") in ("was_live", "post_live", "is_live")) or bool(info.get("was_live"))
    member = info.get("availability") in ("subscriber_only", "premium_only")
    return f"{'member' if member else 'public'}_{'live' if live else 'video'}"


def _date(source: dict) -> str | None:
    pub = source.get("published_at") or ""
    return f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if len(pub) == 8 and pub.isdigit() else None


def context(source: dict, run_id: str, channel: dict | None) -> dict:
    info = db.unj(source.get("info"), {}) or {}
    run = db.one("SELECT summary FROM runs WHERE id=?", run_id)
    summ = db.unj(run["summary"], {}) if run else {}
    ts = info.get("timestamp")
    date = _date(source)
    return {
        "channel": (channel or {}).get("name") or source.get("channel_title") or "",
        "date": date or "", "date8": (date or "").replace("-", ""),
        "datetime": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes") if ts else date,
        "headline": source.get("headline") or source.get("title") or "",
        "video_title": source.get("title") or source.get("id"),
        "url": source.get("url") or f"https://www.youtube.com/watch?v={source['id']}",
        "access": access_kind(info),
        "duration_min": round((source.get("duration") or 0) / 60, 1),
        "units": summ.get("units"),
        "clips": sum(v for k, v in (summ.get("assets") or {}).items() if k.startswith("clip:")),
        "notes": db.one("SELECT COUNT(*) n FROM flags WHERE run_id=? AND severity!='INFO'", run_id)["n"],
        "run": run_id,
        "run_id": run_id,
    }


def key_levels(run_id: str, limit: int = 12) -> str:
    """One line per must-learn unit, in the shape the library already uses: 「标的 要点｜为什么」."""
    rows = db.q("SELECT title, data FROM units WHERE run_id=? ORDER BY learning_priority DESC, start", run_id)
    lines = []
    for r in rows[:limit]:
        d = db.unj(r["data"], {})
        tick = "/".join(d.get("tickers") or [])
        head = r["title"] if not tick or tick in r["title"] else f"{tick} {r['title']}"
        why = (d.get("why") or "").strip().rstrip("。")
        lines.append(f"{head}｜{why}" if why else head)
    return "\n".join(lines)


def value(m: dict, ctx: dict) -> object:
    src, v = m.get("source"), m.get("value")
    if src == "fixed":
        return v
    if src == "template":
        return re.sub(r"\{(\w+)\}", lambda x: str(ctx.get(x[1], "")), v or "").strip()
    if src == "published_date":
        return ctx["date"] or None
    if src == "published_datetime":
        return ctx["datetime"] or None
    if src == "access":
        return (v or ACCESS_DEFAULT).get(ctx["access"]) or None
    if src == "key_levels":
        return key_levels(ctx["run_id"])
    if src == "video_url":
        return ctx["url"]
    return ctx.get(src)


def _text(s: str) -> list[dict]:
    s = str(s or "")
    return [{"type": "text", "text": {"content": s[i:i + 1900]}} for i in range(0, len(s), 1900)][:100]


def payload(t: str, v: object) -> dict:
    if t == "title":
        return {"title": _text(v)[:1] if v else []}
    if t == "rich_text":
        return {"rich_text": _text(v) if v not in (None, "") else []}
    if t in ("select", "status"):
        return {t: {"name": str(v)[:100]} if v else None}
    if t == "multi_select":
        vals = v if isinstance(v, list) else [v] if v else []
        return {"multi_select": [{"name": str(x)[:100]} for x in vals]}
    if t == "date":
        return {"date": {"start": v} if v else None}
    if t == "number":
        return {"number": v if isinstance(v, (int, float)) else None}
    if t == "checkbox":
        return {"checkbox": bool(v)}
    if t == "url":
        return {"url": v or None}
    if t == "relation":
        return {"relation": [{"id": x} for x in (v or [])]}
    raise ValueError(t)


def properties(cfg: dict, schema: dict, ctx: dict) -> dict:
    """Notion page `properties` body; keyed by property id (Notion accepts ids or names)."""
    out = {}
    for pid, m in (cfg.get("properties") or {}).items():
        sp = schema.get(pid)
        if sp and sp["type"] not in NOT_WRITABLE:
            out[pid] = payload(sp["type"], value(m, ctx))
    return out


def preview(cfg: dict, schema: dict, ctx: dict | None) -> dict:
    """What the 频道 page shows: every live property, its mapping, its problem and a sample value."""
    probs = problems(cfg, schema)
    rows = []
    for pid, sp in schema.items():
        m = (cfg.get("properties") or {}).get(pid)
        sample = None
        if m and ctx and pid not in probs:
            try:
                sample = value(m, ctx)
            except Exception:  # noqa: BLE001
                sample = None
        rows.append({"id": pid, **sp, "writable": sp["type"] not in NOT_WRITABLE,
                     "sources": [k for k, (_, ts) in SOURCES.items() if sp["type"] in ts],
                     "mapping": m, "problem": probs.get(pid), "sample": sample})
    for pid, m in (cfg.get("properties") or {}).items():
        if pid not in schema:
            rows.append({"id": pid, "name": m.get("name"), "type": m.get("type"), "writable": False, "sources": [],
                         "mapping": m, "problem": probs.get(pid), "deleted": True})
    order = {"title": 0}
    rows.sort(key=lambda r: (r.get("deleted", False), order.get(r["type"], 1), not r.get("mapping"), r["name"] or ""))
    return {"rows": rows, "problems": len(probs)}
