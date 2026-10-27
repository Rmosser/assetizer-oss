"""Playbook compiler — Raw Channel Prompt → Runtime Playbook (design review §3.2).

Why not "extract requirements → compile → self-audit" with one model: in Run-013 the
compiler graded itself "all preserved" while a concrete checklist found the pattern
catalogue and Setup→Trigger compressed into one vague rule. So:

1. code splits the raw prompt into numbered clauses (deterministic);
2. the compiler must give *every* clause a disposition (RULE / EXAMPLE / ORCHESTRATION /
   ARTICLE_STYLE / REDUNDANT / IGNORED+reason); coverage is computed by code;
3. rule quotes are assembled by code from the clauses they claim → provenance is exact;
4. a separate, bounded audit call grades each rule against only its own clauses;
5. conflicts with the global Source-only policy are listed, policy wins;
6. the result is NEEDS_CONFIRMATION until the user confirms once.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from difflib import SequenceMatcher

from . import config, db
from .contracts import prompts as P
from .llm import LLM, ContractError
from .util import now, sha256_text

RESERVED_FACETS = {"title", "ticker", "tickers", "video_time", "time", "start", "end", "learning_priority", "priority",
                   "summary", "speaker_summary", "teacher_reasoning", "visual_evidence", "visual_breakdown",
                   "screenshot", "screenshot_recommendation", "screenshot_anchors", "required_context", "claims",
                   "uncertainties", "concept_name", "importance_rationale"}
PHASES = ["understanding", "merge", "knowledge", "article", "selection"]
DISPOSITIONS = ["RULE", "EXAMPLE", "ORCHESTRATION", "ARTICLE_STYLE", "REDUNDANT", "IGNORED"]


def split_clauses(raw: str) -> list[dict]:
    """Lines / list items / sentences → clauses. Headings and decoration are dropped.
    start/end are the clause's character range in the raw prompt (for the 稿纸 margin notes)."""
    out: list[dict] = []
    heading = ""
    offset = 0
    for line in raw.splitlines(keepends=True):
        base, offset = offset, offset + len(line)
        line = line.rstrip("\r\n")
        t = line.strip()
        if not t or re.fullmatch(r"[-*_=#>`\s]+", t):
            continue
        if re.match(r"^#{1,6}\s", t):
            heading = re.sub(r"^#+\s*", "", t).strip("* ")
            continue
        t = re.sub(r"^([-*+]|\d+[.)、])\s+", "", t).strip()
        t = t.lstrip("> ").strip()
        parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;])\s*", t) if p.strip()]
        cursor = 0
        for p in parts:
            at = line.find(p, cursor)
            cursor = at + len(p) if at >= 0 else cursor
            plain = re.sub(r"[*_`]+", "", p).strip()
            if len(plain) < 3 and not plain.endswith(("：", ":")):
                continue
            out.append({"id": f"C{len(out) + 1:03}", "text": plain, "section": heading,
                        "start": base + at if at >= 0 else None, "end": base + at + len(p) if at >= 0 else None})
    # fold short lead-ins ("例如：", "格式：") into the clause they introduce
    merged: list[dict] = []
    carry, carry_start = "", None
    for c in out:
        if len(c["text"]) <= 24 and c["text"].endswith(("：", ":")):
            carry += c["text"]
            carry_start = c["start"] if carry_start is None else carry_start
            continue
        merged.append({**c, "text": carry + c["text"], "start": carry_start if carry_start is not None else c["start"]})
        carry, carry_start = "", None
    for i, c in enumerate(merged):
        c["id"] = f"C{i + 1:03}"
    return merged


def _norm(s: str) -> str:
    return re.sub(r"[\s*_`“”\"'「」『』，。、；：！？,.;:!?（）()\[\]【】-]+", "", s).lower()


COMPILE_SCHEMA = P.obj({
    "rules": P.arr(P.obj({"id": {"type": "string", "pattern": r"^[a-z0-9_]{3,60}$"}, "label": P.STR, "rule": P.STR,
                          "phases": P.arr({"type": "string", "enum": PHASES}, minItems=1),
                          "clause_ids": P.arr(P.STR, minItems=1)})),
    "facets": P.arr(P.obj({"key": {"type": "string", "pattern": r"^[a-z0-9_]{2,40}$"}, "label": P.STR,
                           "type": {"type": "string", "enum": ["text", "list"]}, "description": P.STR,
                           "clause_ids": P.arr(P.STR)})),
    "article_style": P.STR,
    "clause_map": P.arr(P.obj({"clause_id": P.STR, "disposition": {"type": "string", "enum": DISPOSITIONS},
                               "target": P.STR, "reason": P.STR})),
    "policy_conflicts": P.arr(P.obj({"clause_id": P.STR, "issue": P.STR})),
})

AUDIT_SCHEMA = P.obj({"grades": P.arr(P.obj({"rule_id": P.STR, "status": {"type": "string", "enum": ["PRESERVED", "PARTIAL", "DROPPED"]},
                                              "missing": P.STR}))})


def _compile_prompt(identity: dict, clauses: list[dict]) -> str:
    return f"""{P.TOOL_RULE}
你是频道 Playbook 编译器。禁止工具与媒体。把用户为某个频道写下的长期学习偏好（已由系统切成编号条款）编译成可执行规则。

全局政策（不可违反，冲突时以此为准）：
{P.POLICY}

频道：{identity.get('channel', '')}；主讲人：{identity.get('speaker', '')}

要求：
- clause_map：**每一条**条款都必须出现且只出现一次，disposition 取：
  RULE（转成规则，target=规则 id）、EXAMPLE（只是示例/格式说明，target=它说明的规则 id）、
  ORCHESTRATION（执行环境/工具/保存路径类要求，由系统承担，不给 Worker）、ARTICLE_STYLE（文章写法）、
  REDUNDANT（与另一条款重复，target=那条规则 id）、IGNORED（必须在 reason 写明原因）。
- rules：每条规则是对 Worker 的明确指令，保留用户原意中的**具体目录、条件、步骤**（例如列举的结构类型、多周期 Setup→Trigger），不要压缩成笼统表述；label 是给用户看的 2–8 个字的中文短名（如「怎么看出来」「不造精度」）；phases 标出规则作用的阶段；clause_ids 列出来源条款。
- facets：用户希望每个知识单元**额外**固定记录的专门栏目，没有就给空数组。key 用英文小写。系统已内置以下核心字段，facets 不得重复它们：
  标题、Ticker、起止时间、学习优先级、摘要、主讲人推理（老师怎么看出来）、视觉证据、截图锚点（时间/原因）、所需上下文、关键陈述、不确定性。
- article_style：一段话概括文章写法（只来自 ARTICLE_STYLE 条款）。
- policy_conflicts：与全局政策冲突的条款（例如要求补充 Source 之外的资料），说明冲突点。
- 示例里的 ticker/日期/价格只说明格式，不得写成规则内容。

CLAUSES
{json.dumps(clauses, ensure_ascii=False)}"""


def _audit_prompt(pairs: list[dict]) -> str:
    return f"""{P.TOOL_RULE}
你是独立审计员。禁止工具与媒体。逐条检查每个规则是否完整保留了它所声称来源条款的具体意图。
- PRESERVED：条款里的具体要求（目录项、条件、步骤、限制）都在规则里；PARTIAL：有具体项被压缩或遗漏（missing 写出遗漏了什么）；DROPPED：基本没有保留。
- 只看给出的条款，不评价规则写得好不好。

PAIRS
{json.dumps(pairs, ensure_ascii=False)}"""


def compile_prompt(channel_id: str, prompt_id: int, again: bool = False) -> int:
    """Compile a channel prompt version into a NEEDS_CONFIRMATION playbook. Returns playbook id.
    ``again`` asks the model afresh instead of reusing the answer accepted for the same text last time."""
    ch = db.one("SELECT * FROM channels WHERE id=?", channel_id)
    pr = db.one("SELECT * FROM channel_prompts WHERE id=?", prompt_id)
    identity = {"channel": ch["name"], "speaker": ch["name"], "speaker_key": "host"}
    prev = db.one("SELECT data FROM playbooks WHERE channel_id=? ORDER BY version DESC LIMIT 1", channel_id)
    if prev:
        identity = (db.unj(prev["data"], {}).get("identity") or identity)
    clauses = split_clauses(pr["text"])
    if not clauses:
        raise ContractError("CONTENT_FAILURE", "prompt has no clauses")
    by_id = {c["id"]: c for c in clauses}
    root = config.get().home / "compile" / f"{channel_id}-p{pr['version']}-{sha256_text(pr['text'])[:8]}"
    if again:
        root = root.with_name(f"{root.name}-r{int(time.time())}")
    llm = LLM()

    def check(o):
        ids = [m["clause_id"] for m in o["clause_map"]]
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise ContractError("SCHEMA_VIOLATION", f"unknown clauses {unknown[:5]}")
        rule_ids = {r["id"] for r in o["rules"]}
        if len(rule_ids) != len(o["rules"]):
            raise ContractError("SCHEMA_VIOLATION", "duplicate rule ids")

    o = llm.structured(root, "compile", _compile_prompt(identity, clauses), COMPILE_SCHEMA, effort="high", check=check)
    mapped = {m["clause_id"]: m for m in o["clause_map"]}
    for r in o["rules"]:
        mapped_ids = [i for i in r["clause_ids"] if i in by_id]
        r["quote"] = "\n".join(by_id[i]["text"] for i in mapped_ids)
        r["clause_ids"] = mapped_ids
        for i in mapped_ids:
            mapped.setdefault(i, {"clause_id": i, "disposition": "RULE", "target": r["id"], "reason": "rule claims clause"})
    uncovered = [c for c in clauses if c["id"] not in mapped]
    bad_ignored = [m for m in mapped.values() if m["disposition"] == "IGNORED" and len(m.get("reason", "").strip()) < 4]

    pairs = [{"rule_id": r["id"], "rule": r["rule"], "clauses": [by_id[i]["text"] for i in r["clause_ids"]]} for r in o["rules"]]
    grades = []
    for bi in range(0, len(pairs), 12):
        try:
            a = llm.structured(root, f"audit-{bi // 12 + 1:02}", _audit_prompt(pairs[bi:bi + 12]), AUDIT_SCHEMA, effort="medium")
            grades += a["grades"]
        except ContractError as e:
            grades += [{"rule_id": p["rule_id"], "status": "PARTIAL", "missing": f"审计失败：{e.kind}"} for p in pairs[bi:bi + 12]]
    data = {
        "identity": identity,
        "rules": [{"id": r["id"], "label": (r.get("label") or "").strip()[:12], "rule": r["rule"], "quote": r["quote"],
                   "phases": r["phases"], "clause_ids": r["clause_ids"]} for r in o["rules"]],
        "facets": [{**{k: f[k] for k in ("key", "label", "type", "description")},
                    "clause_ids": [i for i in f.get("clause_ids") or [] if i in by_id]}
                   for f in o["facets"] if f["key"] not in RESERVED_FACETS],
        "article_style": o["article_style"],
    }
    audit = {
        "source": f"compiler v1 · {now()}",
        "clauses": clauses,
        "clause_map": list(mapped.values()),
        "uncovered": [c["id"] for c in uncovered],
        "ignored_without_reason": [m["clause_id"] for m in bad_ignored],
        "grades": grades,
        "core_dropped": sum(g["status"] == "DROPPED" for g in grades),
        "core_partial": sum(g["status"] == "PARTIAL" for g in grades),
        "policy_conflicts": [{"clause_id": c["clause_id"], "rule_id": mapped.get(c["clause_id"], {}).get("target", ""),
                              "issue": c["issue"]} for c in o["policy_conflicts"]],
        "coverage": round(1 - len(uncovered) / len(clauses), 3),
    }
    v = (db.one("SELECT MAX(version) v FROM playbooks WHERE channel_id=?", channel_id) or {}).get("v") or 0
    pid = db.exec("""INSERT INTO playbooks(channel_id, prompt_id, version, status, data, audit, origin, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""", channel_id, prompt_id, v + 1, "NEEDS_CONFIRMATION", db.j(data), db.j(audit),
                  f"compiler:p{pr['version']}", now())
    # one pending 定稿 per channel: a newer draft replaces the older request (the drafts themselves stay)
    db.exec("""UPDATE decisions SET resolved_at=?, outcome='SUPERSEDED' WHERE kind='CONFIRM_PLAYBOOK' AND resolved_at IS NULL
               AND ref IN (SELECT CAST(id AS TEXT) FROM playbooks WHERE channel_id=?)""", now(), channel_id)
    db.decision("CONFIRM_PLAYBOOK", str(pid), f"为「{ch['name']}」定稿：规程 v{v + 1}",
                f"{audit['coverage'] * 100:.0f}% 的条款有去处；{audit['core_partial']} 条规则部分保留、{audit['core_dropped']} 条丢失；"
                f"{len(audit['policy_conflicts'])} 处与「只依据视频」原则冲突，以原则为准。"
                "定稿后这个频道的新视频会自动收录、生产并写入 Notion。")
    db.event("compile", f"{ch['name']} Playbook v{v + 1} 编译完成，待确认")
    return pid


DRAFT_SCHEMA = P.obj({"raw_prompt": P.STR})


def draft_prompt(channel_name: str, why: str, sample_titles: list[str]) -> str:
    """Draft a raw channel prompt from the user's stated reason (user edits before compiling)."""
    root = config.get().home / "compile" / f"draft-{sha256_text(channel_name + why)[:10]}"
    o = LLM().structured(root, "draft", f"""{P.TOOL_RULE}
禁止工具与媒体。用户决定长期追踪频道「{channel_name}」。
用户说明的原因：{why}
该频道近期标题（仅用于了解题材，不是事实来源）：{json.dumps(sample_titles[:10], ensure_ascii=False)}

请以用户第一人称写一份「频道学习 Prompt」初稿（Markdown），供多模态模型每期理解视频时使用：
- 说明我为什么看这个频道、最想学什么、哪些视觉证据（图表/屏幕文字/PPT）重要；
- 列出希望每个知识单元记录的要点（具体、可执行）；希望文章具备什么学习功能；
- 列出需要特别注意的忠实度风险（归属、条件、不要补外部资料）。
不要编造该频道的具体观点或数据。""", DRAFT_SCHEMA, effort="medium")
    return o["raw_prompt"]


def add_prompt_version(channel_id: str, text: str, origin: str = "manual", note: str | None = None) -> int:
    h = sha256_text(text)
    existing = db.one("SELECT id FROM channel_prompts WHERE channel_id=? AND sha256=?", channel_id, h)
    if existing:
        return existing["id"]
    v = (db.one("SELECT MAX(version) v FROM channel_prompts WHERE channel_id=?", channel_id) or {}).get("v") or 0
    return db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, origin, note, created_at) VALUES (?,?,?,?,?,?,?)",
                   channel_id, v + 1, text, h, origin, note, now())


def rule_label(r: dict) -> str:
    """The rule's short name; rules compiled before labels existed fall back to their opening words."""
    if (r.get("label") or "").strip():
        return r["label"].strip()
    text = (r.get("rule") or "").strip()
    head = re.split(r"[，。；：、,.;:（(]", text, maxsplit=1)[0]
    return head if len(head) <= 10 else head[:8] + "…"


def _locate(raw: str):
    """Find clause texts (markdown stripped) back in the raw prompt, in order: normalized text + index map."""
    keep = [(i, ch) for i, ch in enumerate(raw) if ch not in "*_`"]
    norm = "".join(ch for _, ch in keep)
    idx = [i for i, _ in keep]
    pos = 0

    def find(text: str) -> tuple[int, int] | None:
        nonlocal pos
        for needle in (text, re.split(r"[：:]", text)[-1].strip()):
            j = norm.find(needle, pos) if needle else -1
            if j >= 0:
                pos = j + len(needle)
                return idx[j], idx[j + len(needle) - 1] + 1
        return None
    return find


def _rule_diff(old: list[dict], new: list[dict]) -> dict:
    """Rules of two versions matched by id, then by text (exact, then similar): what was added, changed, removed."""
    used: set[int] = set()
    added, changed = [], []
    for r in new:
        k = next((i for i, o in enumerate(old) if i not in used and r.get("id") and o.get("id") == r.get("id")), None)
        if k is None:
            k = next((i for i, o in enumerate(old) if i not in used and o["rule"].strip() == r["rule"].strip()), None)
        if k is None:
            best = max(((SequenceMatcher(None, o["rule"], r["rule"]).ratio(), i) for i, o in enumerate(old) if i not in used),
                       default=(0.0, None))
            k = best[1] if best[0] >= 0.6 else None
        if k is None:
            added.append(r)
            continue
        used.add(k)
        if old[k]["rule"].strip() != r["rule"].strip():
            changed.append({"from": old[k], "to": r})
    return {"added": added, "changed": changed, "removed": [o for i, o in enumerate(old) if i not in used]}


def annotations(playbook_id: int) -> dict:
    """The 稿纸 for one playbook version: the prompt it was compiled from, every clause's range and fate,
    the rules with short names, the facets, conflicts, coverage, and what changed since the version before."""
    pb = db.one("""SELECT p.*, cp.text prompt_text, cp.version prompt_version, cp.note prompt_note FROM playbooks p
                   LEFT JOIN channel_prompts cp ON cp.id=p.prompt_id WHERE p.id=?""", playbook_id)
    if not pb:
        raise LookupError(playbook_id)
    data = db.unj(pb["data"], {}) or {}
    audit = db.unj(pb["audit"], {}) or {}
    clauses = audit.get("clauses") or []
    cmap = {m["clause_id"]: m for m in audit.get("clause_map") or []}
    text = pb["prompt_text"] or ""
    find = _locate(text)
    out = []
    for c in clauses:
        span = (c["start"], c["end"]) if c.get("start") is not None else find(c["text"]) if text else None
        m = cmap.get(c["id"], {})
        out.append({"id": c["id"], "text": c["text"], "section": c.get("section", ""),
                    "start": span and span[0], "end": span and span[1],
                    "disposition": m.get("disposition"), "rule_id": m.get("target") or None, "reason": m.get("reason")})
    rules = [{**r, "label": rule_label(r)} for r in data.get("rules") or []]
    prev = db.one("SELECT id, version, data FROM playbooks WHERE channel_id IS ? AND version<? ORDER BY version DESC LIMIT 1",
                  pb["channel_id"], pb["version"])
    diff = None
    if prev:
        d = _rule_diff(db.unj(prev["data"], {}).get("rules") or [], rules)
        lab = lambda r: {"id": r.get("id"), "label": rule_label(r), "rule": r["rule"]}  # noqa: E731
        diff = {"version": prev["version"], "added": [lab(r) for r in d["added"]], "removed": [lab(r) for r in d["removed"]],
                "changed": [{"from": lab(x["from"]), "to": lab(x["to"])} for x in d["changed"]]}
    counts = Counter(m.get("disposition") for m in cmap.values())
    return {
        "playbook": {"id": pb["id"], "version": pb["version"], "status": pb["status"], "origin": pb["origin"]},
        "prompt": {"id": pb["prompt_id"], "version": pb["prompt_version"], "note": pb["prompt_note"], "text": text} if pb["prompt_id"] else None,
        "has_provenance": bool(clauses and cmap),
        "has_labels": all((r.get("label") or "").strip() for r in data.get("rules") or []),
        "clauses": out,
        "rules": rules,
        "facets": data.get("facets") or [],
        "article_style": data.get("article_style"),
        "conflicts": audit.get("policy_conflicts") or [],
        "coverage": {"clauses": len(clauses), "used": counts["RULE"] + counts["EXAMPLE"] + counts["ARTICLE_STYLE"],
                     "system": counts["ORCHESTRATION"], "redundant": counts["REDUNDANT"], "ignored": counts["IGNORED"]},
        "vs_previous": diff,
    }


def provenance_ok(rule_quote: str, raw: str) -> bool:
    return _norm(rule_quote) in _norm(raw) if rule_quote else False
