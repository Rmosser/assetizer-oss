"""UNDERSTAND → MERGE → KNOWLEDGE (skeleton + detail) → ARTICLE (outline + sections) → SELECT."""
from __future__ import annotations

import re

from .. import db
from ..contracts import prompts as P
from ..llm import ContractError
from ..providers.agy import ProviderError
from ..util import fmt_tc, parse_tc, ulid
from . import fidelity
from .context import RunContext, StageWarning
from .media_stages import _silences, _transcript, main_span, screenshots_off

BATCH = 6
SOURCE_BATCH = 4000  # 原话模式：一批最多这么多字的原话，写正文的输出才不至于太长
FORBIDDEN = ["振聋发聩", "醍醐灌顶", "集大成", "深刻揭示", "极高价值", "极强价值", "极佳示范", "精准洞察", "重磅",
             "史诗级", "顶级", "毫无疑问", "严密闭环", "完美诠释", "教科书级别", "speaker_"]


def _clamp(t: float, dur: float) -> float:
    return max(0.0, min(dur, t))


def clean_time_scope(o):
    """A claim's time_scope outside the enum (2026-11-05: 'PLAN' failed a whole 71-minute run) only loses that hint."""
    allowed = P.CLAIM["properties"]["time_scope"]["enum"]
    if isinstance(o, dict):
        for items in (o.get("topics"), o.get("units")):
            for t in items if isinstance(items, list) else []:
                for c in (t.get("claims") or []) if isinstance(t, dict) else []:
                    if isinstance(c, dict) and c.get("time_scope") not in allowed:
                        c["time_scope"] = None
    return o


# ------------------------------------------------------------------ understand
MIN_SPLIT = 600.0  # 理解输出超长时对半切的最短一段；再短还超长就是别的毛病


def _halves(ctx: RunContext, ch: dict) -> list[dict]:
    """A chunk too dense to describe in one answer, as two that overlap like the plan's chunks do."""
    from .. import config
    from ..media import ffmpeg
    from .acquire import _proxy
    over = min(config.get().chunk_overlap_minutes * 60, ch["length"] / 4)
    half = ch["length"] / 2
    out = []
    for suffix, offset, length in (("a", ch["offset"], half + over / 2),
                                   ("b", ch["offset"] + half - over / 2, ch["length"] - half + over / 2)):
        path, enc = _proxy(ctx, ch["part_id"] + suffix, offset, length)
        length = ffmpeg.duration(path)
        out.append({"part_id": ch["part_id"] + suffix, "path": str(path), "offset": offset, "length": length,
                    "encoding": enc, "source_start": fmt_tc(offset), "source_end": fmt_tc(offset + length)})
    return out


def understand(ctx: RunContext) -> dict:
    plan = ctx.load("machine/chunk-plan.json")
    llm = ctx.llm()
    out = []
    i = 0
    while i < len(plan["chunks"]):
        ch = plan["chunks"][i]

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

        try:
            rec = llm.structured(ctx.calls, f"understand-{ch['part_id']}",
                                 P.understand_prompt(ctx.playbook, ch["path"], ch, ch["length"]),
                                 P.UNDERSTAND_SCHEMA, media=[ch["path"]], check=check, normalize=clean_time_scope)
        except ProviderError as e:
            # ghf6mWCp_HY-r1 (2026-11-05)：50 分钟的经济话题，话题和论断写不下一个回答。切开后的计划存回去，
            # 合并按它找接缝，重跑也直接用切开的两段
            if e.kind != "PROVIDER_OUTPUT_LIMIT" or ch["length"] / 2 < MIN_SPLIT:
                raise
            plan["chunks"][i:i + 1] = _halves(ctx, ch)
            ctx.save("machine/chunk-plan.json", plan)
            ctx.event(f"{ch['part_id']} 输出超长，切成两段重新理解")
            continue
        i += 1
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
def resolve_seams(chunks: list[dict], plan: list[dict]) -> tuple[list[dict], list[dict]]:
    """Seam policy "trim": a coarse topic at the end of chunk i that is covered, inside the overlap, by two or more
    finer topics of chunk i+1 is cut back to the overlap start (or dropped if it lies almost entirely inside it)."""
    log = []
    out = [dict(c, topics=list(c["topics"])) for c in chunks]
    for i in range(len(out) - 1):
        o0 = plan[i + 1]["offset"]
        o1 = plan[i]["offset"] + plan[i]["length"]
        nxt = out[i + 1]["topics"]
        kept = []
        for t in out[i]["topics"]:
            a, b = t["start_s"], t["end_s"]
            inside = max(0.0, min(b, o1) - max(a, o0))
            covered = [x for x in nxt if min(b, x["end_s"]) - max(a, x["start_s"]) >= 0.5 * (x["end_s"] - x["start_s"])]
            if inside >= 0.5 * (b - a) and len(covered) >= 2:
                if o0 - a >= 30:
                    t = dict(t, end_s=o0, end=fmt_tc(o0), claims=[c for c in t.get("claims", []) if parse_tc(c["t"]) < o0])
                    log.append({"topic": t["id"], "action": "trim", "to": fmt_tc(o0), "covered_by": [x["id"] for x in covered]})
                else:
                    log.append({"topic": t["id"], "action": "drop", "covered_by": [x["id"] for x in covered]})
                    continue
            kept.append(t)
        out[i]["topics"] = kept
    return out, log


def merge(ctx: RunContext) -> dict:
    import os
    policy = os.environ.get("ASSETIZER_SEAM_POLICY") or db.setting("seam_policy", "trim")  # experiment S, 2026-10-05
    chunks = ctx.load("machine/chunks.json")
    seam_log = []
    if policy == "trim" and len(chunks) > 1:
        chunks, seam_log = resolve_seams(chunks, ctx.load("machine/chunk-plan.json")["chunks"])
        ctx.save("machine/seam-log.json", seam_log)
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

        o = ctx.llm().structured(ctx.calls, "merge", P.merge_prompt(ctx.playbook, slim, pairs, seam_hint=policy == "prompt"),
                                 P.MERGE_SCHEMA, effort="medium", check=check)
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
    return {"topics": len(merged), "speakers": len(speakers), "conflicts": len(conflicts), "seam_policy": policy,
            "seam_actions": len(seam_log)}


def _topic_records(ctx: RunContext):
    chunks = ctx.load("machine/chunks.json")
    merged = ctx.load("machine/merged.json")
    by_id = {t["id"]: t for c in chunks for t in c["topics"]}
    return merged, by_id


def _strip(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("start_s", "end_s")}


# ------------------------------------------------------------------ knowledge
def fit_labels(o: dict) -> dict:
    """Shorten unit labels past LABEL_MAX, keeping them unique and renaming depends_on to match. The model now
    and then runs a descriptive label a character over (2026-10-23: a 65-character label failed both attempts
    of a skeleton, and with it the whole run)."""
    if not isinstance(o, dict) or not isinstance(o.get("units"), list):
        return o  # malformed: the schema reports it
    units = [u for u in o["units"] if isinstance(u, dict) and isinstance(u.get("label"), str)]
    taken = {u["label"] for u in units if len(u["label"]) <= P.LABEL_MAX}
    renamed = {}
    for u in units:
        old = u["label"]
        if len(old) <= P.LABEL_MAX:
            continue
        new, n = old[:P.LABEL_MAX].rstrip("_"), 1
        while new in taken:
            n += 1
            new = old[:P.LABEL_MAX - len(f"_{n}")].rstrip("_") + f"_{n}"
        taken.add(new)
        renamed[old] = u["label"] = new
    for u in units:
        if renamed and isinstance(u.get("depends_on"), list):
            u["depends_on"] = [renamed.get(x, x) for x in u["depends_on"]]
    return o


def _source_segments(ctx: RunContext) -> list[dict] | None:
    """The transcript lines to write from, in 原话模式; None in the summary path or when transcription failed."""
    if not fidelity.enabled(ctx.playbook):
        return None
    tr = _transcript(ctx)
    segments = fidelity.spoken(tr["segments"], _silences(ctx)) if tr and tr.get("segments") else None
    if segments:
        return segments
    ctx.flag("RUN", None, "TRANSCRIPT_MISSING", "REVIEW", "这一期没有转写，正文只能按理解摘要写，会有压缩")
    return None


def _batches(units: list[dict], size) -> list[list[dict]]:
    """At most BATCH units and about SOURCE_BATCH characters of source each; a long unit goes alone."""
    out: list[list[dict]] = []
    used = 0
    for u in units:
        n = size(u)
        if out and len(out[-1]) < BATCH and used + n <= SOURCE_BATCH:
            out[-1].append(u)
            used += n
        else:
            out.append([u])
            used = n
    return out


def knowledge(ctx: RunContext) -> dict:
    merged, by_id = _topic_records(ctx)
    mtopics = {t["id"]: t for t in merged["topics"]}
    llm = ctx.llm()
    source = _source_segments(ctx)
    skel_input = [{**_strip(t), "records": [_strip(by_id[r]) for r in t["chunk_topic_refs"]]} for t in merged["topics"]]

    def check_skel(o):
        labels = [u["label"] for u in o["units"]]
        if len(labels) != len(set(labels)):
            raise ContractError("SCHEMA_VIOLATION", "duplicate labels")
        bad = [r for u in o["units"] for r in u["topic_refs"] if r not in mtopics]
        if bad:
            raise ContractError("SCHEMA_VIOLATION", f"unknown topic refs {bad[:5]}")
        if source:
            fidelity.check_partition(o["units"], source)

    if source:
        skel = llm.structured(ctx.calls, "knowledge-skeleton",
                              P.transcript_skeleton_prompt(ctx.playbook, skel_input, fidelity.numbered(source)),
                              P.TRANSCRIPT_SKELETON_SCHEMA, check=check_skel, normalize=fit_labels)
        owned, info = fidelity.partition(skel["units"], source)
        for label in info["dropped_units"]:
            ctx.flag("RUN", None, "UNIT_WITHOUT_SOURCE", "INFO", f"知识单元 {label} 没有分到原话，已去掉")
        if info["missed_lines"]:
            ctx.flag("RUN", None, "TRANSCRIPT_UNASSIGNED", "INFO", f"{info['missed_lines']} 行原话模型没有划分，已并入相邻单元")
    else:
        skel = llm.structured(ctx.calls, "knowledge-skeleton", P.skeleton_prompt(ctx.playbook, skel_input),
                              P.SKELETON_SCHEMA, check=check_skel, normalize=fit_labels)
        owned = None
    units = []
    for u in skel["units"]:
        if owned is not None and u["label"] not in owned:
            continue
        refs = [mtopics[r] for r in u["topic_refs"]]
        segs = owned[u["label"]] if owned is not None else \
            sorted(({"start": r["start_s"], "end": r["end_s"]} for r in refs), key=lambda x: x["start"])
        a, b = main_span({"segments": segs, "start": segs[0]["start"], "end": segs[-1]["end"]})
        units.append({**u, "start": a, "end": b, "segments": segs,
                      "chunk_topic_refs": [c for r in refs for c in r["chunk_topic_refs"]]})
    units.sort(key=lambda u: u["start"])

    schema = P.detail_schema(ctx.playbook)
    details: dict[str, dict] = {}
    if source:
        batches = _batches(units, lambda u: fidelity.cjk(fidelity.text_of(source, u["segments"])))
    else:
        batches = [units[bi:bi + BATCH] for bi in range(0, len(units), BATCH)]
    for bn, batch in enumerate(batches, 1):
        labels = {u["label"] for u in batch}
        recs = {c: _strip(by_id[c]) for u in batch for c in u["chunk_topic_refs"]}
        brief = [{k: u[k] for k in ("label", "title", "knowledge_type", "learning_priority", "tickers", "why")}
                 | {"start": fmt_tc(u["start"]), "end": fmt_tc(u["end"]), "topic_ids": u["chunk_topic_refs"]}
                 | ({"source_text": fidelity.text_of(source, u["segments"])} if source else {}) for u in batch]

        def check_detail(o, labels=labels):
            got = {u["label"] for u in o["units"]}
            if not labels <= got:
                raise ContractError("MISSING_UNITS", ",".join(sorted(labels - got)))

        name = f"knowledge-detail-{bn:02}"
        prompt = P.detail_prompt(ctx.playbook, brief, list(recs.values()), transcript=bool(source))
        try:
            o = llm.structured(ctx.calls, name, prompt, schema, check=check_detail, normalize=clean_time_scope)
        except ContractError as e:
            if e.kind != "MISSING_UNITS":
                raise
            o = llm.structured(ctx.calls, name + "-retry", prompt, schema, normalize=clean_time_scope)
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
        for a in [] if screenshots_off(ctx) else d.get("screenshot_anchors", [])[:2]:
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
        if source:
            ku["role"] = u.get("role") or "TOPIC"
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
def _source_brief(u: dict, source: list[dict]) -> dict:
    return {k: u.get(k) for k in ("label", "title", "role", "knowledge_type", "tickers", "visual_evidence", "facets")} \
        | {"source_text": fidelity.text_of(source, u["segments"], mark_pauses=u.get("role") == "QA_DIGEST")}


def _write_from_source(ctx: RunContext, units: list[dict], by_id: dict, source: list[dict]) -> dict[str, str]:
    """原话模式写正文：逐段改写；被压缩、漏数字或混进整理用语的单元退回重写一次，还不过关就留给人看。"""
    llm = ctx.llm()
    sections: dict[str, str] = {}

    def recs(batch: list[dict]) -> list[dict]:
        return list({c: _strip(by_id[c]) for u in batch for c in u["provenance"]["chunk_topic_refs"] if c in by_id}.values())

    for bn, batch in enumerate(_batches(units, lambda u: fidelity.cjk(fidelity.text_of(source, u["segments"]))), 1):
        o = llm.structured(ctx.calls, f"article-sections-{bn:02}",
                           P.transcript_sections_prompt(ctx.playbook, [_source_brief(u, source) for u in batch], recs(batch)),
                           P.SECTIONS_SCHEMA)
        for s in o["sections"]:
            sections.setdefault(s["label"], s["markdown"])
    for u in units:
        md = sections.get(u["label"])
        if not md:
            continue
        text = fidelity.text_of(source, u["segments"])
        problems = fidelity.section_problems(u.get("role") or "TOPIC", text, md)
        if not problems:
            continue
        note = "\n【这一节上一次没有通过检查，请重写】" + "；".join(problems) + "。\n"
        try:
            o = llm.structured(ctx.calls, f"article-rewrite-{u['label']}",
                               P.transcript_sections_prompt(ctx.playbook, [_source_brief(u, source)], recs([u]), feedback=note),
                               P.SECTIONS_SCHEMA)
        except ProviderError as e:
            if e.kind != "CONTENT_SAFETY":
                raise
            o = {"sections": []}  # the filter stops the rewrite, not the section: the draft stands, for a person to look at
            db.exec("UPDATE runs SET retry=NULL WHERE id=?", ctx.id)
        again = next((s["markdown"] for s in o["sections"] if s["label"] == u["label"]), "")
        left = fidelity.section_problems(u.get("role") or "TOPIC", text, again) if again else problems
        # 写长了要改短，所以只拦住比原稿和原话都短太多的重写
        if again and len(left) <= len(problems) and fidelity.cjk(again) >= min(fidelity.cjk(md), fidelity.cjk(text)) * 0.9:
            sections[u["label"]] = again
        else:
            left = problems
        for p in left:
            ctx.flag("UNIT", u["id"], "SECTION_NOT_FAITHFUL", "REVIEW", f"「{u['title']}」{p[:40]}", p)
    return sections


def article(ctx: RunContext) -> dict:
    units = _units(ctx)
    _, by_id = _topic_records(ctx)
    llm = ctx.llm()
    meta = {"title": ctx.source.get("title"), "channel": ctx.source.get("channel_title"),
            "published": ctx.source.get("published_at"), "duration": fmt_tc(ctx.duration)}
    brief = [{k: u[k] for k in ("label", "title", "learning_priority", "why", "tickers")} | {"start": fmt_tc(u["start"])} for u in units]
    source = _source_segments(ctx)
    if not all("from" in sg for u in units for sg in u["segments"]):
        source = None  # the units were planned without the transcript
    sections: dict[str, str] = {}
    if source:
        # the opening and the closing lists are written last, from the text itself: from titles alone they drift
        sections = _write_from_source(ctx, units, by_id, source)
        outline = llm.structured(ctx.calls, "article-outline",
                                 P.outline_prompt(ctx.playbook, meta, [b | {"markdown": sections.get(b["label"], "")} for b in brief], written=True),
                                 P.OUTLINE_SCHEMA)
    else:
        outline = llm.structured(ctx.calls, "article-outline", P.outline_prompt(ctx.playbook, meta, brief), P.OUTLINE_SCHEMA)
        for bi in range(0, len(units), BATCH):
            batch = units[bi:bi + BATCH]
            recs = {c: _strip(by_id[c]) for u in batch for c in u["provenance"]["chunk_topic_refs"] if c in by_id}
            o = llm.structured(ctx.calls, f"article-sections-{bi // BATCH + 1:02}",
                               P.sections_prompt(ctx.playbook, [_brief(u) for u in batch], list(recs.values())),
                               P.SECTIONS_SCHEMA)
            for s in o["sections"]:
                sections.setdefault(s["label"], s["markdown"])
    db.exec("DELETE FROM sections WHERE run_id=?", ctx.id)
    rows = [("INTRO", [], outline["intro_markdown"])]
    for n, u in enumerate(units, 1):
        md = sections.get(u["label"])
        if not md:
            ctx.flag("UNIT", u["id"], "SECTION_MISSING", "REVIEW", f"「{u['title']}」正文未生成，已用摘要代替")
            md = f"## {u['title']}\n\n{u['summary']}"
        rows.append(("UNIT", [u["id"]], renumber(md.strip(), n)))
    if outline.get("outro_markdown", "").strip():
        rows.append(("OUTRO", [], outline["outro_markdown"]))
    full = [f"# {outline['title']}"]
    for i, (kind, uids, md) in enumerate(rows):
        db.exec("INSERT INTO sections(id, run_id, ord, kind, unit_ids, markdown) VALUES (?,?,?,?,?,?)",
                ulid(), ctx.id, i, kind, db.j(uids), md)
        full.append((f"<!-- KU:{uids[0]} -->\n" if uids else "") + md)
    text = "\n\n".join(full)
    ctx.path("article", "article.md").write_text(text)
    ctx.save("article/outline.json", outline)  # 题眼 stays with its run; sources.headline follows the adopted run
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


def renumber(md: str, n: int) -> str:
    """Batch-written sections number themselves from 01; strip that and number in Source order."""
    lines = md.splitlines()
    if lines and lines[0].startswith("#"):
        title = re.sub(r"^#+\s*", "", lines[0])
        title = re.sub(r"^(第?\s*\d+\s*[.、:：）)]|\d+\s+)\s*", "", title).strip()
        lines[0] = f"## {n:02d}. {title}"
    else:
        lines.insert(0, f"## {n:02d}.")
    return "\n".join(lines)


def unit_by_label(units: list[dict]) -> dict:
    return {u["label"]: u for u in units}


SPEAKER_RE = re.compile(r"speaker_\d+")
