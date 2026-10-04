"""Seed channels and playbooks from validated Run evidence.

* 交易员汤姆 — Run-014 compiled + audited playbook (core DROPPED = 0) → ACTIVE.
* 王海滨的财经闲谈 — Run-013 compiled playbook, never fully audited, and one
  rule conflicts with the Source-only policy → NEEDS_CONFIRMATION.
* Universal — empty channel prompt (Run-011 discovery instruction).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .util import now, sha256_text

SEEDS = Path(__file__).resolve().parents[2] / "seeds"

TOM_FACETS = [
    {"key": "higher_timeframe_setup", "label": "大周期 Setup", "type": "text", "description": "大周期定义的结构背景（无证据则 null）"},
    {"key": "lower_timeframe_trigger", "label": "小周期 Trigger", "type": "text", "description": "小周期的执行触发条件（无证据则 null）"},
    {"key": "observation_tasks", "label": "观察任务", "type": "list", "description": "回到原视频截图后具体去看什么"},
]
WANG_FACETS = [
    {"key": "argument_chain", "label": "论证链", "type": "list", "description": "主讲人的论证步骤，按顺序"},
    {"key": "cases", "label": "案例", "type": "list", "description": "本单元引用的案例与数据（原样）"},
    {"key": "terminology", "label": "术语", "type": "list", "description": "术语：简要释义（仅 Source 中的解释）"},
    {"key": "cited_resources", "label": "提及资料", "type": "list", "description": "Source 明确提到的书籍/报告/视频，不得联想补充"},
]
W_PHASES = {
    "speaker_context": ["understanding", "merge", "knowledge", "article", "selection"],
    "article_structure": ["article"],
    "visual_attention": ["understanding"],
    "extraction_schema": ["understanding", "knowledge"],
    "semantic_priorities": ["understanding", "knowledge", "article"],
    "fidelity_risks": ["understanding", "merge", "knowledge", "article", "selection"],
}

UNIVERSAL = {
    "universal": True,
    "identity": {},
    "rules": [],
    "facets": [],
    "article_style": "结构自然来自当前内容，不强迫章节模板；分别归属多人的观点；完整保留关键解释、案例、条件、风险、限制与结论。",
}


def _channel(cid: str, name: str, yt: str, mode: str, why: str) -> None:
    if db.one("SELECT id FROM channels WHERE id=?", cid):
        return
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, why, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            cid, name, yt, mode, why, now(), now())


def _prompt(cid: str, text: str, origin: str | None, edited: str | None) -> int:
    h = sha256_text(text)
    r = db.one("SELECT id FROM channel_prompts WHERE channel_id=? AND sha256=?", cid, h)
    if r:
        return r["id"]
    v = (db.one("SELECT MAX(version) v FROM channel_prompts WHERE channel_id=?", cid) or {}).get("v") or 0
    return db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, origin, origin_edited_at, created_at) VALUES (?,?,?,?,?,?,?)",
                   cid, v + 1, text, h, origin, edited, now())


def _playbook(cid: str | None, prompt_id: int | None, data: dict, audit: dict, status: str, origin: str) -> int:
    r = db.one("SELECT id FROM playbooks WHERE channel_id IS ? AND origin=?", cid, origin)
    if r:
        return r["id"]
    v = (db.one("SELECT MAX(version) v FROM playbooks WHERE channel_id IS ?", cid) or {}).get("v") or 0
    return db.exec("INSERT INTO playbooks(channel_id, prompt_id, version, status, data, audit, origin, created_at, activated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   cid, prompt_id, v + 1, status, db.j(data), db.j(audit), origin, now(), now() if status == "ACTIVE" else None)


def run() -> dict:
    # Universal
    _playbook(None, None, UNIVERSAL, {"note": "Run-011 universal discovery instruction"}, "ACTIVE", "seed:universal")

    # Tom
    tom_dir = SEEDS / "tom"
    raw = (tom_dir / "raw-prompt.md").read_text()
    meta = json.loads((tom_dir / "raw-prompt.metadata.json").read_text())
    pb = json.loads((tom_dir / "playbook.json").read_text())
    audit = json.loads((tom_dir / "audit.json").read_text())
    _channel("trader-tom", "交易员汤姆", "UCUobvwww8yvh8pAEy_oPQXA", "TRACKED",
             "学习主讲人如何读图：截图锚点、多周期 Setup → Trigger、老师怎么看出来与观察任务。")
    pid = _prompt("trader-tom", raw, meta.get("notion_url"), meta.get("last_edited_at"))
    data = {
        "identity": {"channel": "交易员汤姆", "speaker": "交易员汤姆", "speaker_key": "tom"},
        "rules": [{"id": r["id"], "rule": r["rule"], "quote": r["quote"], "phases": r["phase"]} for r in pb["rules"]],
        "facets": TOM_FACETS,
        "article_style": "读图学习导航：前部学习表；每个知识单元先说明看什么、老师怎么看出来，再给观察任务；克制、具体。",
    }
    _playbook("trader-tom", pid, data, {"source": "Run-014 Codex fidelity audit", **audit}, "ACTIVE", "seed:run-014")

    # 王海滨
    w_dir = SEEDS / "wang-haibin"
    wraw = (w_dir / "raw-prompt.md").read_text()
    wmeta = json.loads((w_dir / "raw-prompt.metadata.json").read_text())
    wpb = json.loads((w_dir / "playbook.json").read_text())
    _channel("wang-haibin", "王海滨的财经闲谈", wpb["channel"].get("id"), "TRACKED",
             "完整知识存档：保留论证过程、案例、引用、视觉信息和长文结构。")
    wpid = _prompt("wang-haibin", wraw, wmeta.get("url"), wmeta.get("page_last_edited_at"))
    rules = []
    for cat, items in wpb.items():
        if not isinstance(items, list):
            continue
        for r in items:
            rules.append({"id": r["rule_id"], "rule": r["rule"], "quote": r.get("quote", ""), "phases": W_PHASES.get(cat, ["understanding"])})
    wdata = {
        "identity": {"channel": "王海滨的财经闲谈", "speaker": "王海滨", "speaker_key": "wang"},
        "rules": rules, "facets": WANG_FACETS,
        "article_style": "顶部标注「视频发布于xxxx年xx月xx日」；层级标题；多要点分条；文末单独列出知识点与经验认知点清单。",
    }
    waudit = {
        "source": "Run-013 compile; not independently audited for production",
        "policy_conflicts": [{"rule_id": "rule_extended_reading_recommendations",
                              "issue": "「可联想到」的延伸资源与 Source-only 政策冲突；执行时只保留 Source 明确提到的资料。"}],
    }
    wid = _playbook("wang-haibin", wpid, wdata, waudit, "NEEDS_CONFIRMATION", "seed:run-013")
    db.decision("CONFIRM_PLAYBOOK", str(wid), "确认「王海滨的财经闲谈」频道 Playbook",
                "Run-013 编译结果尚未做生产级逐条审计；1 条规则与 Source-only 政策冲突（Policy 优先）。确认后该频道视频会自动生产。")
    return {"channels": db.q("SELECT id, name, mode FROM channels"),
            "playbooks": db.q("SELECT id, channel_id, version, status FROM playbooks")}
