"""The text model's part of a read: the plan (how much the video has for the question, where to look again, the brief
for the notes), the notes, their revision once the 补看 is in, and the answer put together from every video's notes.

What each is asked is in prompts/plan.md, write.md, revise.md and synth.md. The notes are Markdown citing their sources
inline — `[12:34 说]` speech, `[12:34.5 帧]` a frame, `[L03]` `[R01]` a look — and end with one ```json block (fit, gist
and, after the first writing, the 补看 list)."""
from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLM, ContractError, extract_json
from . import fill, gate, prompt
from .look import clean

FITS = ("high", "some", "none")
FULL_DRAFT = 360_000   # characters of the full draft a call takes; past it the index (frames' text cut short) goes instead
BRIEF = ("这期很长，下面给的是索引版底稿：每张高清帧只留开头 160 个字，后面写了共多少字。完整的字在原片上，"
         "要用到的长段文字安排 frames 或 zoom 回看。")
TAIL = re.compile(r"```json\s*(\{.*?\})\s*```\s*$", re.S)
LOOK_FIELDS = ("id", "start", "end", "mode", "question", "answer", "evidence", "sure", "answer2", "evidence2", "sure2", "error")

PLAN_SCHEMA = {
    "type": "object", "required": ["fit", "reason", "looks", "write"],
    "properties": {
        "fit": {"type": "string", "enum": list(FITS)},
        "reason": {"type": "string"},
        "looks": {"type": "array", "items": {"type": "object", "required": ["id", "question"], "properties": {
            "id": {"type": "string"}, "start": {"type": ["string", "number"]}, "end": {"type": ["string", "number"]},
            "mode": {"type": "string"}, "step": {"type": "number"}, "times": {"type": "array", "items": {"type": ["string", "number"]}},
            "second": {"type": "boolean"}, "target": {"type": "string"}, "question": {"type": "string"}, "why": {"type": "string"}}}},
        "write": {"type": "string"},
    },
}
SYNTH_SCHEMA = {
    "type": "object", "required": ["answer", "first", "sections", "videos"],
    "properties": {
        "answer": {"type": "string"},
        "first": {"type": "array", "items": {"type": "object", "required": ["v", "why"],
                                             "properties": {"v": {"type": "integer"}, "why": {"type": "string"}}}},
        "sections": {"type": "array", "items": {"type": "object", "required": ["title", "body"],
                                                "properties": {"title": {"type": "string"}, "body": {"type": "string"}}}},
        "videos": {"type": "array", "items": {"type": "object", "required": ["v", "adds"],
                                              "properties": {"v": {"type": "integer"}, "adds": {"type": "string"}}}},
    },
}


def notes_line(notes: str | None) -> str:
    return f"用户补充：{notes.strip()}" if notes and notes.strip() else ""


def draft_for(full: str, index: str) -> tuple[str, str]:
    """The draft a call takes, and what the planner is told about it."""
    return (full, "") if len(full) <= FULL_DRAFT else (index, BRIEF)


def split(text: str) -> tuple[str, dict]:
    """Notes and the ```json block they end with ({} when it is missing or broken: the notes are still the notes)."""
    m = TAIL.search(text.strip())
    if not m:
        return text.strip(), {}
    try:
        tail = json.loads(m[1])
    except json.JSONDecodeError:
        try:
            tail = extract_json(m[1])
        except ContractError:
            tail = {}
    return text.strip()[:m.start()].rstrip(), tail if isinstance(tail, dict) else {}


def fit_of(v: Any, default: str = "some") -> str:
    v = str(v or "").strip().lower()
    return v if v in FITS else default


def looks_for_writer(results: list[dict]) -> str:
    return json.dumps([{k: x[k] for k in LOOK_FIELDS if k in x} for x in results], ensure_ascii=False, indent=1)


def plan(llm: LLM, root, question: str, notes: str | None, draft: str, brief: str, forced: bool = False) -> dict:
    """{fit, reason, looks, write}. `forced`: the user wants the video read though an earlier plan found it unrelated."""
    def normalize(o: Any) -> Any:
        if isinstance(o, dict):
            o["fit"] = fit_of(o.get("fit"))
            if forced and o["fit"] == "none":
                o["fit"] = "some"
            o.setdefault("looks", [])
            o.setdefault("write", "")
            o.setdefault("reason", "")
        return o

    def check(o: dict) -> None:
        if o["fit"] != "none" and len(o["write"].strip()) < 200:
            raise ContractError("CONTENT_FAILURE", "写作提示词太短")
    stage = "topic-plan-forced" if forced else "topic-plan"
    ask = fill(prompt("plan"), question=question, notes=notes_line(notes), brief=brief, draft=draft)
    with gate(stage):
        o = llm.structured(root / "calls", stage, ask, PLAN_SCHEMA, normalize=normalize, check=check)
    return {**o, "looks": clean(o["looks"], "L")}


def write(llm: LLM, root, question: str, notes: str | None, brief_: str, looks: list[dict], draft: str) -> tuple[str, dict]:
    """The notes and their tail: {fit, gist, looks (补看, ids R01…)}."""
    ask = fill(prompt("write"), question=question, notes=notes_line(notes), write=brief_, looks=looks_for_writer(looks), draft=draft)
    with gate("topic-write"):
        text = llm.text(root / "calls", "topic-write", ask, min_chars=200)
    asset, tail = split(text)
    return asset, {"fit": fit_of(tail.get("fit")), "gist": str(tail.get("gist") or "")[:80], "looks": clean(tail.get("looks"), "R")}


def revise(llm: LLM, root, question: str, notes: str | None, brief_: str, asset: str, looks: list[dict],
           draft: str) -> tuple[str, dict]:
    ask = fill(prompt("revise"), question=question, notes=notes_line(notes), write=brief_, asset=asset,
               looks=looks_for_writer(looks), draft=draft)
    with gate("topic-revise"):
        text = llm.text(root / "calls", "topic-revise", ask, min_chars=200)
    revised, tail = split(text)
    return revised, {"fit": fit_of(tail.get("fit")), "gist": str(tail.get("gist") or "")[:80]}


def synth(llm: LLM, root, stage: str, question: str, notes: str | None, videos: list[dict]) -> dict:
    """`videos`: [{no, title, channel, duration, fit, gist, asset}] of the reads done. The answer, `first` (at most 3 to
    watch), the sections by sub-question, and what each video adds."""
    have = {v["no"] for v in videos}
    parts = []
    for v in videos:
        head = f"## V{v['no']}《{v['title']}》\n\n频道：{v.get('channel') or '—'}；分量：{v['fit']}；要点：{v.get('gist') or '—'}"
        parts.append(head + ("\n\n" + v["asset"] if v.get("asset") else "\n\n（和问题无关，没有写笔记）"))

    def normalize(o: Any) -> Any:
        if isinstance(o, dict):
            o["first"] = [x for x in o.get("first") or [] if isinstance(x, dict) and x.get("v") in have][:3]
            o["videos"] = [x for x in o.get("videos") or [] if isinstance(x, dict) and x.get("v") in have]
        return o
    ask = fill(prompt("synth"), question=question, notes=notes_line(notes), videos="\n\n".join(parts))
    with gate("topic-synth"):
        return llm.structured(root, stage, ask, SYNTH_SCHEMA, normalize=normalize)
