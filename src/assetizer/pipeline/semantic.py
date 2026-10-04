"""UNDERSTAND → MERGE → KNOWLEDGE (skeleton + detail) → ARTICLE (outline + sections) → SELECT."""
from __future__ import annotations

import re

from .. import db
from ..contracts import prompts as P
from ..llm import ContractError
from ..util import fmt_tc, parse_tc, ulid
from .context import RunContext, StageWarning

BATCH = 6
FORBIDDEN = ["振聋发聩", "醍醐灌顶", "集大成", "深刻揭示", "极高价值", "极强价值", "极佳示范", "精准洞察", "重磅",
             "史诗级", "顶级", "毫无疑问", "严密闭环", "完美诠释", "教科书级别", "speaker_"]


def _clamp(t: float, dur: float) -> float:
    return max(0.0, min(dur, t))


# ------------------------------------------------------------------ understand
def understand(ctx: RunContext) -> dict:
    plan = ctx.load("machine/chunk-plan.json")
    llm = ctx.llm()
    out = []
    for ch in plan["chunks"]:
        def check(o, L=ch["length"]):
            spans = []
            for t in o["topics"]:
                try:
                    spans.append(parse_tc(t["end"]) - parse_tc(t["start"]))
                except ValueError:
                    spans.append(0)
            covered = sum(x for x in spans if x > 0)
            if covered < min(0.3 * L, 600):
                raise ContractError("CONTENT_FAILURE", f"topics cover only {covered:.0f}s of {L:.0f}s")

        rec = llm.structured(ctx.calls, f"understand-{ch['part_id']}",
                             P.understand_prompt(ctx.playbook, ch["path"], ch, ch["length"]),
                             P.UNDERSTAND_SCHEMA, media=[ch["path"]], check=check)
        topics = []
        for t in rec["topics"]:
            try:
                a = _clamp(ch["offset"] + parse_tc(t["start"]), ctx.duration)
                b = _clamp(ch["offset"] + parse_tc(t["end"]), ctx.duration)
            except ValueError:
                ctx.flag("RUN", None, "BAD_TIMECODE", "INFO", f"{ch['part_id']} 主题时间码无法解析：{t.get('id')}")
                continue
            if b <= a:
                b = min(ctx.duration, a + 30)
            claims = []
            for c in t.get("claims", []):
                c = dict(c)
                try:
                    c["t"] = fmt_tc(_clamp(ch["offset"] + parse_tc(c["t"]), ctx.duration))
                except ValueError:
                    c["t"] = fmt_tc(a)
                claims.append(c)
            topics.append({**t, "id": f"{ch['part_id']}:{t['id']}", "start": fmt_tc(a), "end": fmt_tc(b),
                           "start_s": a, "end_s": b, "claims": claims, "chunk": ch["part_id"]})
        if not topics:
            raise ContractError("CONTENT_FAILURE_EMPTY_TOPICS", ch["part_id"])
        out.append({"part_id": ch["part_id"], "speakers": rec["speakers"], "topics": topics,
                    "uncertainties": rec.get("uncertainties", [])})
    ctx.save("machine/chunks.json", out)
    return {"chunks": len(out), "topics": sum(len(c["topics"]) for c in out)}


# ------------------------------------------------------------------ merge
def merge(ctx: RunContext) -> dict:
    chunks = ctx.load("machine/chunks.json")
    by_id = {t["id"]: t for c in chunks for t in c["topics"]}
    if len(chunks) == 1:
        merged = [{"id": f"M{i + 1:02}", "title": t["title"], "chunk_topic_refs": [t["id"]], "causal_links": [],
                   "state_events": [], "uncertainties": []} for i, t in enumerate(chunks[0]["topics"])]
        speakers, conflicts = chunks[0]["speakers"], []
    else:
        pairs = []
        for i in range(len(chunks) - 1):
            for a in chunks[i]["topics"]:
                for b in chunks[i + 1]["topics"]:
                    if a["start_s"] < b["end_s"] and b["start_s"] < a["end_s"]:
                        pairs.append([a["id"], b["id"]])
        slim = [{"part_id": c["part_id"], "speakers": c["speakers"],
                 "topics": [{k: t[k] for k in ("id", "title", "start", "end", "summary", "tickers", "claims", "uncertainties")}
                            for t in c["topics"]]} for c in chunks]

        def check(o):
            bad = [r for t in o["topics"] for r in t["chunk_topic_refs"] if r not in by_id]
            if bad:
                raise ContractError("SCHEMA_VIOLATION", f"unknown refs {bad[:5]}")

        o = ctx.llm().structured(ctx.calls, "merge", P.merge_prompt(ctx.playbook, slim, pairs), P.MERGE_SCHEMA,
                                 effort="medium", check=check)
        merged = [{**t, "id": f"M{i + 1:02}"} for i, t in enumerate(o["topics"])]
        speakers, conflicts = o["speakers"], o["conflicts"]
        referenced = {r for t in merged for r in t["chunk_topic_refs"]}
        for tid, t in by_id.items():
            if tid not in referenced:  # never silently lose content
                merged.append({"id": f"M{len(merged) + 1:02}", "title": t["title"], "chunk_topic_refs": [tid],
                               "causal_links": [], "state_events": [], "uncertainties": ["合并时未被引用，系统保留"]})
                ctx.flag("RUN", None, "MERGE_UNREFERENCED_TOPIC", "INFO", f"合并遗漏主题已自动保留：{t['title']}")
    for t in merged:
        refs = [by_id[r] for r in t["chunk_topic_refs"]]
        t["start_s"] = min(r["start_s"] for r in refs)
        t["end_s"] = max(r["end_s"] for r in refs)
        t["start"], t["end"] = fmt_tc(t["start_s"]), fmt_tc(t["end_s"])
        t["summary"] = " ".join(r["summary"] for r in refs)
        t["tickers"] = sorted({x for r in refs for x in r.get("tickers", [])})
    merged.sort(key=lambda t: t["start_s"])
    ctx.save("machine/merged.json", {"topics": merged, "speakers": speakers, "conflicts": conflicts})
    db.exec("DELETE FROM speakers WHERE run_id=?", ctx.id)
    for sp in speakers:
        db.exec("INSERT OR REPLACE INTO speakers(source_id, run_id, key, display_name, role, confidence, evidence) VALUES (?,?,?,?,?,?,?)",
                ctx.source["id"], ctx.id, sp.get("key"), sp.get("name"), sp.get("role"), sp.get("confidence"), sp.get("evidence"))
    for c in conflicts:
        ctx.flag("RUN", None, "MERGE_CONFLICT", "REVIEW", "分段间存在矛盾", c)
    return {"topics": len(merged), "speakers": len(speakers), "conflicts": len(conflicts)}


def _topic_records(ctx: RunContext):
    chunks = ctx.load("machine/chunks.json")
    merged = ctx.load("machine/merged.json")
    by_id = {t["id"]: t for c in chunks for t in c["topics"]}
    return merged, by_id


def _strip(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("start_s", "end_s")}


# ------------------------------------------------------------------ knowledge
def knowledge(ctx: RunContext) -> dict:
    merged, by_id = _topic_records(ctx)
    mtopics = {t["id"]: t for t in merged["topics"]}
    llm = ctx.llm()
    skel_input = [{**_strip(t), "records": [_strip(by_id[r]) for r in t["chunk_topic_refs"]]} for t in merged["topics"]]

    def check_skel(o):
        labels = [u["label"] for u in o["units"]]
        if len(labels) != len(set(labels)):
            raise ContractError("SCHEMA_VIOLATION", "duplicate labels")
        bad = [r for u in o["units"] for r in u["topic_refs"] if r not in mtopics]
        if bad:
            raise ContractError("SCHEMA_VIOLATION", f"unknown topic refs {bad[:5]}")

    skel = llm.structured(ctx.calls, "knowledge-skeleton", P.skeleton_prompt(ctx.playbook, skel_input),
                          P.SKELETON_SCHEMA, effort="medium", check=check_skel)
    units = []
    for u in skel["units"]:
        refs = [mtopics[r] for r in u["topic_refs"]]
        segs = sorted(({"start": r["start_s"], "end": r["end_s"]} for r in refs), key=lambda x: x["start"])
        units.append({**u, "start": segs[0]["start"], "end": max(s["end"] for s in segs), "segments": segs,
                      "chunk_topic_refs": [c for r in refs for c in r["chunk_topic_refs"]]})
    units.sort(key=lambda u: u["start"])

    schema = P.detail_schema(ctx.playbook)
    details: dict[str, dict] = {}
    for bi in range(0, len(units), BATCH):
        batch = units[bi:bi + BATCH]
        labels = {u["label"] for u in batch}
        recs = {c: _strip(by_id[c]) for u in batch for c in u["chunk_topic_refs"]}
        brief = [{k: u[k] for k in ("label", "title", "knowledge_type", "learning_priority", "tickers", "why")}
                 | {"start": fmt_tc(u["start"]), "end": fmt_tc(u["end"]), "topic_ids": u["chunk_topic_refs"]} for u in batch]

        def check_detail(o, labels=labels):
            got = {u["label"] for u in o["units"]}
            if not labels <= got:
                raise ContractError("MISSING_UNITS", ",".join(sorted(labels - got)))

        name = f"knowledge-detail-{bi // BATCH + 1:02}"
        try:
            o = llm.structured(ctx.calls, name, P.detail_prompt(ctx.playbook, brief, list(recs.values())), schema,
                               effort="medium", check=check_detail)
        except ContractError as e:
            if e.kind != "MISSING_UNITS":
                raise
            o = llm.structured(ctx.calls, name + "-retry", P.detail_prompt(ctx.playbook, brief, list(recs.values())),
                               schema, effort="medium")
        for d in o["units"]:
            if d["label"] in labels:
                details[d["label"]] = d

    keep = {r["label"]: r["id"] for r in db.q("SELECT id, label FROM units WHERE run_id=?", ctx.id)}  # stable ids on rerun
    db.exec("DELETE FROM units WHERE run_id=?", ctx.id)
    final = []
    speaker_key = (ctx.playbook.get("identity") or {}).get("speaker_key", "host")
    for i, u in enumerate(units):
        uid = keep.get(u["label"]) or ulid()
        d = details.get(u["label"])
        if d is None:
            ctx.flag("UNIT", uid, "UNIT_DETAIL_MISSING", "REVIEW", f"知识单元「{u['title']}」详细内容生成失败，仅保留骨架")
            d = {"summary": u.get("why", ""), "speaker_key": speaker_key, "teacher_reasoning": "", "visual_evidence": [],
                 "screenshot_anchors": [], "required_context": "", "claims": [], "facets": {}, "uncertainties": ["详细内容缺失"]}
        anchors = []
        for a in d.get("screenshot_anchors", [])[:2]:
            try:
                t = _clamp(parse_tc(a["t"]), ctx.duration)
            except ValueError:
                continue
            if not (u["start"] - 60 <= t <= u["end"] + 60):
                ctx.flag("UNIT", uid, "ANCHOR_OUT_OF_RANGE", "INFO", f"截图锚点 {a['t']} 不在单元区间内，已忽略")
                continue
            anchors.append({**a, "t": t})
        ku = {
            "id": uid, "label": u["label"], "title": u["title"], "start": u["start"], "end": u["end"],
            "segments": u["segments"], "provenance": {"chunk_topic_refs": u["chunk_topic_refs"], "merged_topic_refs": u["topic_refs"]},
            "speaker_key": d.get("speaker_key") or speaker_key, "tickers": u.get("tickers", []),
            "knowledge_type": u["knowledge_type"], "learning_priority": u["learning_priority"],
            "depends_on": [x for x in u.get("depends_on", []) if x != u["label"]], "clip_candidate": u["clip_candidate"],
            "why": u.get("why", ""), "summary": d["summary"], "teacher_reasoning": d.get("teacher_reasoning", ""),
            "visual_evidence": d.get("visual_evidence", []), "screenshot_anchors": anchors,
            "required_context": d.get("required_context", ""), "claims": d.get("claims", []),
            "facets": d.get("facets", {}), "uncertainties": d.get("uncertainties", []),
        }
        final.append(ku)
        db.exec("""INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, learning_priority, knowledge_type, data)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""", ku["id"], ctx.id, ctx.source["id"], ku["label"], i, ku["title"],
                ku["start"], ku["end"], ku["learning_priority"], ku["knowledge_type"], db.j(ku))
        if any(w in (ku["required_context"] or "") for w in ("彻底", "完全剔除", "坚决")):
            ctx.flag("UNIT", ku["id"], "OVERSTATEMENT_CANDIDATE", "INFO", "required_context 含强化措辞，需核对条件范围",
                     ku["required_context"])
    ctx.save("machine/knowledge-units.json", {"units": final, "uncertainties": skel.get("uncertainties", [])})
    return {"units": len(final), "p3": sum(u["learning_priority"] == 3 for u in final)}


def _units(ctx: RunContext) -> list[dict]:
    return ctx.load("machine/knowledge-units.json")["units"]


def _brief(u: dict) -> dict:
    return {k: u[k] for k in ("label", "title", "knowledge_type", "learning_priority", "tickers", "summary",
                              "teacher_reasoning", "visual_evidence", "required_context", "claims", "facets",
                              "uncertainties", "depends_on")} | {"start": fmt_tc(u["start"]), "end": fmt_tc(u["end"])}


# ------------------------------------------------------------------ article
def article(ctx: RunContext) -> dict:
    units = _units(ctx)
    _, by_id = _topic_records(ctx)
    llm = ctx.llm()
    meta = {"title": ctx.source.get("title"), "channel": ctx.source.get("channel_title"),
            "published": ctx.source.get("published_at"), "duration": fmt_tc(ctx.duration)}
    outline = llm.structured(ctx.calls, "article-outline",
                             P.outline_prompt(ctx.playbook, meta, [{k: u[k] for k in ("label", "title", "learning_priority", "why", "tickers")}
                                                                   | {"start": fmt_tc(u["start"])} for u in units]),
                             P.OUTLINE_SCHEMA, effort="medium")
    sections: dict[str, str] = {}
    for bi in range(0, len(units), BATCH):
        batch = units[bi:bi + BATCH]
        recs = {c: _strip(by_id[c]) for u in batch for c in u["provenance"]["chunk_topic_refs"] if c in by_id}
        o = llm.structured(ctx.calls, f"article-sections-{bi // BATCH + 1:02}",
                           P.sections_prompt(ctx.playbook, [_brief(u) for u in batch], list(recs.values())),
                           P.SECTIONS_SCHEMA, effort="medium")
        for s in o["sections"]:
            sections.setdefault(s["label"], s["markdown"])
    db.exec("DELETE FROM sections WHERE run_id=?", ctx.id)
    rows = [("INTRO", [], outline["intro_markdown"])]
    for u in units:
        md = sections.get(u["label"])
        if not md:
            ctx.flag("UNIT", u["id"], "SECTION_MISSING", "REVIEW", f"「{u['title']}」正文未生成，已用摘要代替")
            md = f"## {u['title']}\n\n{u['summary']}"
        rows.append(("UNIT", [u["id"]], md.strip()))
    if outline.get("outro_markdown", "").strip():
        rows.append(("OUTRO", [], outline["outro_markdown"]))
    full = [f"# {outline['title']}"]
    for i, (kind, uids, md) in enumerate(rows):
        db.exec("INSERT INTO sections(id, run_id, ord, kind, unit_ids, markdown) VALUES (?,?,?,?,?,?)",
                ulid(), ctx.id, i, kind, db.j(uids), md)
        full.append((f"<!-- KU:{uids[0]} -->\n" if uids else "") + md)
    text = "\n\n".join(full)
    ctx.path("article", "article.md").write_text(text)
    ctx.save("article/outline.json", outline)
    hits = [w for w in FORBIDDEN if w in text]
    for w in hits:
        ctx.flag("RUN", None, "EDITORIAL_TERM", "INFO", f"文章出现禁用措辞「{w.rstrip('_')}」", evidence={"quote": w})
    return {"title": outline["title"], "chars": len(text), "sections": len(rows), "editorial_hits": hits}


# ------------------------------------------------------------------ select
def select(ctx: RunContext) -> dict:
    units = _units(ctx)
    labels = {u["label"] for u in units}

    def check(o):
        got = {s["label"] for s in o["selections"]}
        if got - labels:
            raise ContractError("SCHEMA_VIOLATION", "unknown labels")
        if len(got & labels) < 0.9 * len(labels):
            raise ContractError("MISSING_UNITS", f"{len(got & labels)}/{len(labels)} selections")

    try:
        o = ctx.llm().structured(ctx.calls, "selection",
                                 P.select_prompt(ctx.playbook, [{k: u[k] for k in ("label", "title", "learning_priority",
                                                                                     "knowledge_type", "summary", "required_context",
                                                                                     "clip_candidate", "depends_on")}
                                                                | {"duration_s": round(u["end"] - u["start"])} for u in units]),
                                 P.SELECT_SCHEMA, effort="medium", check=check, enforce=True)
    except ContractError as e:
        raise StageWarning(f"selection contract failed: {e}")
    sel = {s["label"]: s for s in o["selections"]}
    missing = [u["label"] for u in units if u["label"] not in sel]
    if missing:
        ctx.flag("RUN", None, "SELECTION_FALLBACK", "INFO", f"{len(missing)} 个单元未返回选片结果，按骨架兜底", ", ".join(missing))
    result = []
    for u in units:
        s = sel.get(u["label"]) or {"label": u["label"], "verdict": "SELECT" if u["clip_candidate"] else "REJECT",
                                    "publication_priority": "P3", "reason": "（未返回，按骨架 clip_candidate 兜底）"}
        result.append({**s, "unit_id": u["id"]})
    ctx.save("machine/selection.json", {"selections": result})
    from collections import Counter
    return dict(Counter(s["verdict"] for s in result))


def unit_by_label(units: list[dict]) -> dict:
    return {u["label"]: u for u in units}


SPEAKER_RE = re.compile(r"speaker_\d+")
