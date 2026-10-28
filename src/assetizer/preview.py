"""What a channel's Notion page will look like before anything is written (频道页 · Notion 复刻).

The channel page shows a live replica of the Notion page beside its settings. It is built from the
code delivery itself uses (mapping values, the page template, the closing toggles) for one run and a
channel config that may not be saved yet. Notion is only read: the database schema, related page
titles and the parent page's icon, each cached for a minute so editing a card does not hammer the
API. Media come back as asset ids for /media/{id}; file paths never leave the server.
"""
from __future__ import annotations

import re
import threading
import time

from . import db
from .notion import mapping as M

TTL = 60.0
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def cached(key: str, fn, ttl: float = TTL):
    t = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and t - hit[0] < ttl:
        return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (t, value)
    return value


def forget(prefix: str = "") -> None:
    with _lock:
        for k in [k for k in _cache if k.startswith(prefix)]:
            del _cache[k]


def database(n, database_id: str, fresh: bool = False) -> dict:
    """The database schema; ``fresh`` reads it now and refreshes the cached copy card edits are previewed against."""
    return cached(f"db:{database_id}", lambda: n._req("GET", f"/databases/{database_id}"), ttl=0 if fresh else TTL)


def page(n, page_id: str) -> dict | None:
    from .notion.client import NotionError

    def get():
        try:
            return n.page(page_id)
        except NotionError:
            return None
    return cached(f"page:{page_id}", get)


def page_title(pg: dict | None) -> str:
    for p in (pg or {}).get("properties", {}).values():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p["title"])
    return ""


def draft_config(saved: dict, draft: dict | None) -> dict:
    """The channel config with the card edits on top, checked the way saving checks it."""
    if not draft:
        return saved
    out = dict(saved)
    if "properties" in draft:
        props = {}
        for pid, m in (draft.get("properties") or {}).items():
            if not m or not m.get("source"):
                continue  # 不填
            if m["source"] not in M.SOURCES:
                raise ValueError(f"unknown source {m['source']}")
            props[pid] = {k: m[k] for k in ("name", "type", "source", "value", "option_ids") if k in m}
        out["properties"] = props
    if "template" in draft:
        if draft["template"] not in M.TEMPLATES:
            raise ValueError("unknown template")
        out["template"] = draft["template"]
    if "heading" in draft:
        out["heading"] = (draft.get("heading") or "").strip()
    if "icon" in draft:
        out["icon"] = draft.get("icon") or "none"
    return out


# ------------------------------------------------------------------ text
def parts(template: str, ctx: dict) -> list[dict]:
    """"{channel}-{date8}-{headline}" → the pieces of the filled-in text, each variable marked."""
    out, pos = [], 0
    for m in re.finditer(r"\{(\w+)\}", template or ""):
        if m.start() > pos:
            out.append({"text": template[pos:m.start()]})
        out.append({"text": str(ctx.get(m[1], "") or ""), "var": m[1]})
        pos = m.end()
    if pos < len(template or ""):
        out.append({"text": template[pos:]})
    return out


def title_parts(cfg: dict, schema: dict, ctx: dict) -> list[dict]:
    pid = next((p for p, sp in schema.items() if sp["type"] == "title"), None)
    m = (cfg.get("properties") or {}).get(pid) if pid else None
    if not m:
        return []
    if m.get("source") == "template":
        return parts(m.get("value") or "", ctx)
    if m.get("source") == "fixed":
        return [{"text": str(m.get("value") or "")}]
    return [{"text": str(M.value(m, ctx) or ""), "var": m.get("source")}]


def segments(rt: list[dict] | None) -> list[dict]:
    """Notion rich text → [{t, b?, i?, c?, href?}]."""
    out = []
    for x in rt or []:
        s: dict = {"t": x.get("plain_text") if "plain_text" in x else (x.get("text") or {}).get("content", "")}
        a = x.get("annotations") or {}
        for k, short in (("bold", "b"), ("italic", "i"), ("code", "c")):
            if a.get(k):
                s[short] = True
        link = ((x.get("text") or {}).get("link") or {}).get("url")
        if link:
            s["href"] = link
        out.append(s)
    return out


def block(b: dict) -> dict:
    t = b["type"]
    body = b.get(t) or {}
    out: dict = {"type": t}
    if "rich_text" in body:
        out["text"] = segments(body["rich_text"])
    if t == "toggle":
        out["children"] = [block(c) for c in body.get("children") or []]
    elif t == "table":
        out["header"] = bool(body.get("has_column_header"))
        out["rows"] = [[segments(cell) for cell in (r.get("table_row") or {}).get("cells", [])] for r in body.get("children") or []]
    elif t == "embed":
        out["url"] = body.get("url")
    elif t == "callout":
        out["emoji"] = (body.get("icon") or {}).get("emoji")
    elif t == "to_do":
        out["checked"] = bool(body.get("checked"))
    return out


# ------------------------------------------------------------------ page
FRAME_IN_UNIT = re.compile(r":(qh|q\d+|div)$")  # 原话参考 and the rule the template puts around a unit


def body(source: dict, run_id: str, cfg: dict) -> list[dict]:
    """The page body delivery would write, each block tagged with where it comes from:
    origin unit / intro / outro (the article), frame (the page template) or tail (run info, 校注)."""
    from . import delivery, templates
    tpl = cfg.get("template") or "default"
    plan = templates.PLANS[tpl](source, run_id, cfg) if tpl != "default" else delivery._default_plan(source, run_id)
    plan += delivery._tail(source, run_id)
    secs = {r["id"]: (r["kind"], db.unj(r["unit_ids"], []) or [])
            for r in db.q("SELECT id, kind, unit_ids FROM sections WHERE run_id=?", run_id)}
    out = []
    for it in plan:
        head = it["key"].split(":")[0]
        kind, units = secs.get(head, (None, []))
        origin = kind.lower() if kind else ("tail" if head in ("runinfo", "notes") else head if head in ("intro", "outro") else "frame")
        if it.get("media"):
            m = it["media"]
            a = db.one("SELECT id FROM assets WHERE path=? ORDER BY id DESC LIMIT 1", m["path"])
            b = {"type": m["kind"], "caption": m["caption"], "asset_id": a and a["id"]}
        else:
            b = block(it["block"])
        b.update(key=it["key"], origin=origin, unit_ids=units,
                 tpl=origin in ("frame", "tail") or bool(FRAME_IN_UNIT.search(it["key"])))
        out.append(b)
    return out


def icon(n, cfg: dict) -> dict | None:
    ic = cfg.get("icon") or "none"
    if ic == "none":
        return None
    if ic != "parent":
        return {"emoji": ic}
    rel = next((m["value"][0] for m in (cfg.get("properties") or {}).values()
                if m.get("type") == "relation" and m.get("source") == "fixed" and m.get("value")), None)
    i = ((page(n, rel) if rel else None) or {}).get("icon") or {}
    if i.get("type") == "emoji":
        return {"emoji": i["emoji"]}
    if i.get("type") == "external":
        return {"url": i["external"]["url"]}
    return None  # uploaded icons come back as expiring URLs (delivery leaves them out too)


def properties(n, cfg: dict, schema: dict, ctx: dict, probs: dict) -> list[dict]:
    rows = []
    for pid, sp in schema.items():
        m = (cfg.get("properties") or {}).get(pid)
        row = {"id": pid, "name": sp["name"], "type": sp["type"], "writable": sp["type"] not in M.NOT_WRITABLE,
               "mapping": m, "problem": probs.get(pid), "colors": sp.get("colors") or {}}
        if m and row["writable"]:
            try:
                v = M.value(m, ctx)
            except Exception:  # noqa: BLE001
                v = None
            if sp["type"] == "relation":
                v = [{"id": x, "title": page_title(page(n, x))} for x in (v or [])]
            row["value"] = v
        rows.append(row)
    for pid, m in (cfg.get("properties") or {}).items():
        if pid not in schema:
            rows.append({"id": pid, "name": m.get("name"), "type": m.get("type"), "writable": False, "mapping": m,
                         "problem": probs.get(pid), "deleted": True})
    return rows


def build(n, dbo: dict, cfg: dict, channel: dict, source: dict, run_id: str) -> dict:
    schema = M.schema_of(dbo)
    cfg = M.sync(cfg, schema)
    ctx = M.context(source, run_id, channel)

    def page_ok(pid: str) -> bool:
        pg = page(n, pid)
        return bool(pg) and not pg.get("archived")

    probs = M.problems(cfg, schema, page_ok)
    try:
        blocks, body_error = body(source, run_id, cfg), None
    except (FileNotFoundError, KeyError):
        blocks, body_error = [], "这一版还没有成文"
    return {
        "database": {"id": dbo.get("id"), "url": dbo.get("url"),
                     "title": "".join(t.get("plain_text", "") for t in dbo.get("title", []))},
        "source": {"id": source["id"], "title": source.get("title"), "url": ctx["url"]},
        "run_id": run_id,
        "title": title_parts(cfg, schema, ctx),
        "heading": parts(cfg.get("heading") or "", ctx),
        "icon": icon(n, cfg),
        "properties": properties(n, cfg, schema, ctx, probs),
        "problems": probs,
        "warnings": M.warnings(cfg, schema),
        "blocks": blocks,
        "body_error": body_error,
        "context": {k: ctx.get(k) for k in ("channel", "date", "date8", "datetime", "headline", "video_title", "url", "access")},
    }
