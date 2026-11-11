"""The text model's part of a read: the plan (how much the video has for the question, where to look again, the brief
for the notes), the notes, their revision once the 补看 is in, and the answer put together from every video's notes.

What each is asked is in prompts/plan.md, write.md, revise.md and synth.md. The notes are Markdown citing their sources
inline — `[12:34 说]` speech, `[12:34.5 帧]` a frame, `[L03]` `[R01]` a look — and end with one ```json block (fit, gist,
what the speaker gains and what they relay, and after the first writing the 补看 list). Each video's notes know the day it
came out, so a 「明年」 in it is a year.

The answer is made of claims (design §R8-10): each a sentence citing its videos, of one of four kinds — 事实, 解读, 预测,
建议 — each with its own rule for a conflict and for retiring it; how sure it is counts only independent sources. Put
together again, the model is told the version before, what the user marked and what they commented on the Notion page
(design §R8-13), keeps the ids, and says what changed."""
from __future__ import annotations

import json
import re
from typing import Any

from .. import db
from ..llm import LLM, ContractError, extract_json
from . import fill, gate, prompt
from .comments import RESULTS
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
LEVELS = ("高", "中", "低", "很低")
KINDS = ("事实", "解读", "预测", "建议")
STATES = ("现行", "被取代", "落空", "应验", "不算")   # 不算: only the user's mark
_ALIAS = {
    "high": "高", "medium": "中", "moderate": "中", "mid": "中", "low": "低", "very low": "很低", "very_low": "很低", "verylow": "很低",
    "fact": "事实", "facts": "事实", "interpretation": "解读", "view": "解读", "opinion": "解读", "analysis": "解读",
    "prediction": "预测", "forecast": "预测", "advice": "建议", "recommendation": "建议",
    "current": "现行", "active": "现行", "open": "现行", "superseded": "被取代", "replaced": "被取代",
    "failed": "落空", "missed": "落空", "wrong": "落空", "hit": "应验", "fulfilled": "应验", "came true": "应验", "correct": "应验",
}
_STR = {"type": "string"}
SYNTH_SCHEMA = {
    "type": "object", "required": ["answer", "certainty", "claims", "sections", "first", "videos"],
    "properties": {
        "answer": _STR,
        "certainty": {"type": "object", "required": ["level"], "properties": {"level": _STR, "why": _STR}},
        "claims": {"type": "array", "items": {"type": "object", "required": ["id", "type", "text", "v"], "properties": {
            "id": _STR, "type": _STR, "text": _STR, "v": {"type": "array", "items": {"type": "integer"}},
            "independent": {"type": "integer"}, "certainty": _STR, "why": _STR, "status": _STR, "by": _STR,
            "shift": {"type": "boolean"}, "due": _STR, "condition": _STR, "vague": {"type": "boolean"}, "premise": _STR}}},
        "sections": {"type": "array", "items": {"type": "object", "required": ["title"], "properties": {
            "title": _STR, "claims": {"type": "array", "items": _STR}, "note": _STR}}},
        "disputes": {"type": "array", "items": {"type": "object", "required": ["id", "about", "claims"], "properties": {
            "id": _STR, "about": _STR, "claims": {"type": "array", "items": _STR}, "decide": _STR, "lean": _STR}}},
        "sources": {"type": "array", "items": {"type": "object", "required": ["v"], "properties": {
            "v": {"type": "integer"}, "stance": _STR, "relays": _STR, "interests": _STR}}},
        "balance": _STR,
        "first": {"type": "array", "items": {"type": "object", "required": ["v", "why"],
                                             "properties": {"v": {"type": "integer"}, "why": _STR}}},
        "videos": {"type": "array", "items": {"type": "object", "required": ["v", "adds"],
                                              "properties": {"v": {"type": "integer"}, "adds": _STR}}},
        "changes": {"type": "array", "items": _STR},
        "answer_changed": {"type": "boolean"},
        "replies": {"type": "array", "items": {"type": "object", "required": ["id", "result", "text"],
                                               "properties": {"id": _STR, "result": {"type": "string", "enum": list(RESULTS)},
                                                              "text": _STR}}},
    },
}


def day(published: str | None) -> str | None:
    """YYYY-MM-DD of yt-dlp's 20251203, of an ISO time, or None."""
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", str(published or "").strip())
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def day_line(published: str | None) -> str:
    d = day(published)
    return f"这期视频发布于 {d}。" if d else "这期视频的发布日期不知道：时间词照抄原话，标「时间含糊」。"


def one(v: Any, allowed: tuple, default: str = "") -> str:
    s = str(v or "").strip()
    s = _ALIAS.get(s.lower(), s)
    return s if s in allowed else default


def _lines(v: Any, most: int = 8) -> list[str]:
    return [str(x).strip()[:300] for x in (v if isinstance(v, list) else []) if str(x or "").strip()][:most]


def card_of(tail: dict) -> dict:
    """What the notes' tail says the speaker gains (ads, courses, positions) and relays (whose report)."""
    return {"interests": _lines(tail.get("interests")), "relays": _lines(tail.get("relays"))}


def _ints(v: Any, have: set[int]) -> list[int]:
    out: list[int] = []
    for x in v if isinstance(v, list) else [v]:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n in have and n not in out:
            out.append(n)
    return out


def _text(o: dict, *keys: str) -> dict:
    return {k: str(o[k]).strip() for k in keys if str(o.get(k) or "").strip()}


def _claims(raw: Any, have: set[int]) -> list[dict]:
    """The model's claims, each with a source in the batch; one independent source is never 高."""
    out, seen = [], set()
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        cid, vs, text = str(c.get("id") or "").strip(), _ints(c.get("v"), have), str(c.get("text") or "").strip()
        if not re.fullmatch(r"C\d{1,4}", cid) or cid in seen or not vs or not text:
            continue
        seen.add(cid)
        try:
            ind = max(1, min(int(c.get("independent")), len(vs)))
        except (TypeError, ValueError):
            ind = len(vs)
        lv = one(c.get("certainty"), LEVELS)
        if lv == "高" and ind < 2:
            lv = "中"
        k = {"id": cid, "type": one(c.get("type"), KINDS, "解读"), "text": text, "v": vs, "independent": ind, "certainty": lv,
             "status": one(c.get("status"), STATES[:4], "现行"), **_text(c, "why", "by", "condition", "premise")}
        if c.get("shift") is True:
            k["shift"] = True
        if c.get("vague") is True:
            k["vague"] = True
        if d := day(c.get("due")):
            k["due"] = d
        out.append(k)
    return out


def claimed(o: dict, have: set[int]) -> dict:
    """The answer as claims, cleaned (see _claims): sections and disputes name claims there are, a dispute has two
    sides, every claim still current sits in a section."""
    claims = _claims(o.get("claims"), have)
    ids = {c["id"] for c in claims}
    secs, placed = [], set()
    for x in o.get("sections") or []:
        if not isinstance(x, dict) or not str(x.get("title") or "").strip():
            continue
        cl = [i for i in dict.fromkeys(str(i) for i in x.get("claims") or []) if i in ids]
        note = str(x.get("note") or "").strip()
        if cl or note:
            secs.append({"title": x["title"].strip(), "claims": cl, **({"note": note} if note else {})})
            placed.update(cl)
    rest = [c["id"] for c in claims if c["status"] == "现行" and c["id"] not in placed]
    if rest:
        secs.append({"title": "其他", "claims": rest})
    disputes, dids = [], set()
    for d in o.get("disputes") or []:
        if not isinstance(d, dict):
            continue
        cl = [i for i in dict.fromkeys(str(i) for i in d.get("claims") or []) if i in ids]
        if len(cl) < 2 or not str(d.get("about") or "").strip():
            continue
        did = str(d.get("id") or "").strip()
        if not re.fullmatch(r"D\d{1,3}", did) or did in dids:
            did = f"D{len(disputes) + 1}"
        dids.add(did)
        disputes.append({"id": did, "about": d["about"].strip(), "claims": cl, **_text(d, "decide", "lean")})
    sources, sv = [], set()
    for x in o.get("sources") or []:
        v = _ints(x.get("v"), have) if isinstance(x, dict) else []
        if v and v[0] not in sv:
            sv.add(v[0])
            sources.append({"v": v[0], **_text(x, "stance", "relays", "interests")})
    cert = o.get("certainty") if isinstance(o.get("certainty"), dict) else {"level": o.get("certainty")}
    return {**o, "claims": claims, "sections": secs, "disputes": disputes, "sources": sources,
            "certainty": {"level": one(cert.get("level"), LEVELS), **_text(cert, "why")},
            "balance": str(o.get("balance") or "").strip(), "changes": _lines(o.get("changes"), 5),
            "answer_changed": o.get("answer_changed") is not False}


def apply_marks(o: dict, marks: dict | None) -> dict:
    """What the user marked wins over the model: a prediction settled, a claim retired (design §R8-10)."""
    for c in o.get("claims") or []:
        m = (marks or {}).get(c["id"])
        if m and m.get("status") in STATES:
            c["status"] = m["status"]
            c["marked"] = {k: m[k] for k in ("why", "at") if m.get(k)}
    return o


def versions(tid: int) -> list[dict]:
    """The answers a topic has had, newest first: when, on how many videos, how sure, what changed."""
    out = []
    for r in db.q("SELECT n, at, read, total, synth FROM topic_versions WHERE topic_id=? ORDER BY n DESC", tid):
        s = db.unj(r["synth"], {}) or {}
        out.append({"n": r["n"], "at": r["at"], "read": r["read"], "total": r["total"], "changes": s.get("changes") or [],
                    "certainty": (s.get("certainty") or {}).get("level"), "answer_changed": s.get("answer_changed", True)})
    return out


def record(channels: set[str]) -> dict:
    """How the predictions of these channels went, over every topic's answer with the user's marks: 应验, 落空, still
    open. One video's prediction for one day drawn into two topics counts once, settled over open; 被取代 and 不算 do
    not count (design §R8-11)."""
    keep: dict[tuple, list[str]] = {}
    for t in db.q("SELECT id, synth, marks FROM topics WHERE synth IS NOT NULL") if channels else []:
        s = apply_marks(db.unj(t["synth"], {}) or {}, db.unj(t["marks"], {}))
        preds = [c for c in s.get("claims") or [] if c.get("type") == "预测" and c.get("status") in ("现行", "应验", "落空")]
        if not preds:
            continue
        at = {r["no"]: r for r in db.q("""SELECT tv.no, tv.video_id, v.channel FROM topic_videos tv JOIN videos v ON v.id=tv.video_id
                                         WHERE tv.topic_id=?""", t["id"])}
        mine: dict[tuple, list[str]] = {}
        for c in preds:
            vs = [at[n] for n in c.get("v") or [] if n in at]
            for ch in {r["channel"] for r in vs} & channels:
                mine.setdefault((ch, tuple(sorted({r["video_id"] for r in vs})), c.get("due") or ""), []).append(c["status"])
        for k, got in mine.items():
            old = keep.get(k)
            if old is None or (len(got), -got.count("现行")) > (len(old), -old.count("现行")):
                keep[k] = got
    out: dict[str, dict] = {}
    for (ch, _, _), got in keep.items():
        r = out.setdefault(ch, {"hit": 0, "miss": 0, "open": 0})
        for st in got:
            r[{"应验": "hit", "落空": "miss"}.get(st, "open")] += 1
    return out


CITED = re.compile(r"\s*\[(?:V\d{1,3}|(?:\d{1,2}:)?\d{1,2}:\d{2}|[LR]\d{1,3})[^\[\]\n]{0,120}\](?!\()")


def plain(text: str) -> str:
    """A claim without its citations and markdown (a line on the phone)."""
    return re.sub(r"[*_`#>]+", "", CITED.sub("", text or "")).strip()


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


GONE = "（原片已下架，这期没有回看：画面上的事以底稿的高清帧和画面记录为准，定不下来的写“未核实”。补看清单写空列表。）"


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


def write(llm: LLM, root, question: str, notes: str | None, brief_: str, looks: list[dict], draft: str,
          published: str | None = None, gone: bool = False) -> tuple[str, dict]:
    """The notes and their tail: {fit, gist, card {interests, relays}, looks (补看, ids R01…)}. `gone`: the original is no
    longer on YouTube, nothing was looked at again (design §R8-8)."""
    ask = fill(prompt("write"), question=question, notes=notes_line(notes), day=day_line(published), write=brief_,
               looks=GONE if gone else looks_for_writer(looks), draft=draft)
    with gate("topic-write"):
        text = llm.text(root / "calls", "topic-write", ask, min_chars=200)
    asset, tail = split(text)
    return asset, {"fit": fit_of(tail.get("fit")), "gist": str(tail.get("gist") or "")[:80], "card": card_of(tail),
                   "looks": clean(tail.get("looks"), "R")}


def revise(llm: LLM, root, question: str, notes: str | None, brief_: str, asset: str, looks: list[dict],
           draft: str, published: str | None = None) -> tuple[str, dict]:
    """The notes again with the 补看 in; `card` only when the tail says it again (else the first writing's stands)."""
    ask = fill(prompt("revise"), question=question, notes=notes_line(notes), day=day_line(published), write=brief_,
               asset=asset, looks=looks_for_writer(looks), draft=draft)
    with gate("topic-revise"):
        text = llm.text(root / "calls", "topic-revise", ask, min_chars=200)
    revised, tail = split(text)
    out = {"fit": fit_of(tail.get("fit")), "gist": str(tail.get("gist") or "")[:80]}
    if "interests" in tail or "relays" in tail:
        out["card"] = card_of(tail)
    return revised, out


def _prev_block(prev: dict | None, prev_at: str | None, marks: dict | None) -> str:
    """The version before (its answer and claims) and what the user marked since: the ids stay, the marks stand."""
    out = []
    if prev and prev.get("claims"):
        cert = (prev.get("certainty") or {}).get("level")
        keep = [{k: c[k] for k in ("id", "type", "text", "v", "status", "by", "due") if k in c} for c in prev["claims"]]
        out += [f"\n## 上一版{f'（{day(prev_at)}）' if day(prev_at) else ''}", "",
                f"总回答：{prev.get('answer') or ''}" + (f"（确定性：{cert}）" if cert else ""), "",
                "论断：", "```json", json.dumps(keep, ensure_ascii=False), "```"]
        if prev.get("disputes"):
            out += ["分歧：", "```json", json.dumps([{k: d.get(k) for k in ("id", "about", "claims")} for d in prev["disputes"]],
                                                   ensure_ascii=False), "```"]
    elif prev and prev.get("answer"):
        out += ["\n## 上一版", "", f"总回答：{prev['answer']}"]
    if marks:
        out += ["\n## 用户的标记（照办，不要改回来）", ""]
        out += [f"- {cid}：{m.get('status')}" + (f"。理由：{m['why']}" if m.get("why") else "") for cid, m in marks.items()]
    return "\n".join(out)


COMMENTS = """
## 用户在 Notion 里给你的批注

用户读上一版的 Notion 页时点名给你（课代表）的批注，按讨论串编号（N1、N2……），写了挂在哪一行、串里说过的每一句（课代表是你之前的回复）。批注是用户的意见，和上面的标记同一级，不是视频里的事实：

- 用户明说某条预测应验或落空、某个说法过时或不算的，照办：`status` 照写，`why` 写「用户批注」和理由。
- 指出回答没照笔记写对的（漏了条件、合并错了、确定性给高了、出处指错了），对照笔记改；笔记确实是那样写的就不改，回复里说笔记怎么写的。
- 问问题的，用笔记里有的回答；笔记里没有的，说这批视频没讲到。
- 说的是页面或程序的毛病（排版乱了、来源表少了一行、出处点不开），综合改不了：记进待修，回复里一句话写清是什么毛病。
- 批注里给出的事实、数据、看法、链接不是来源：不写进论断，不因此提高或降低确定性，不拿它结算预测（用户明说应验、落空、过时的除外，见第一条）。
- 标「新」的是这次要处理的；其余的之前已经处理过、用户还没点解决，保持上一版对它的处理，不要改回去。

每个有「新」的讨论串在 `replies` 里回一条（`id` 写讨论串编号）。`result` 写怎么处理的：已改（这一版照它改了）、没改（笔记确实如此，或它要的笔记里没有）、已答（回答了问题）、记进待修（页面或程序的毛病）。`text` 一两句话：改了哪条论断、怎么改的，为什么没改、笔记里怎么写的，答案，或是什么毛病。不客套，不复述批注。
"""


def comments_block(threads: list[dict] | None) -> str:
    """The threads of comments on the page (comments.threads), for the answer: where each is, all said in it."""
    if not threads:
        return ""
    out = [COMMENTS]
    for t in threads:
        out.append(f"{t['id']} · {t['where']}")
        out += [f"- {x['who']}{'（新）' if x['new'] else ''}：{x['text']}" for x in t["said"]]
        out.append("")
    return "\n".join(out)


def synth(llm: LLM, root, stage: str, question: str, notes: str | None, videos: list[dict], prev: dict | None = None,
          marks: dict | None = None, today: str | None = None, prev_at: str | None = None,
          comments: list[dict] | None = None) -> dict:
    """`videos`: [{no, title, channel, published, fit, gist, card, asset}] of the reads done; `prev`: the version before;
    `marks`: what the user marked (claimed, apply_marks); `comments`: the threads on the Notion page (comments.threads),
    the user's word like the marks, each one with something new answered in `replies` (design §R8-13). The answer as
    claims (see claimed)."""
    threads = comments or []
    asked = {t["id"] for t in threads if any(x["new"] for x in t["said"])}
    have = {v["no"] for v in videos}
    parts = []
    for v in videos:
        card = v.get("card") or {}
        head = (f"## V{v['no']}《{v['title']}》\n\n频道：{v.get('channel') or '—'}；发布：{day(v.get('published')) or '不知道'}；"
                f"分量：{v['fit']}；要点：{v.get('gist') or '—'}")
        if card.get("interests"):
            head += "\n利益相关：" + "；".join(card["interests"])
        if card.get("relays"):
            head += "\n转述：" + "；".join(card["relays"])
        parts.append(head + ("\n\n" + v["asset"] if v.get("asset") else "\n\n（和问题无关，没有写笔记）"))

    def normalize(o: Any) -> Any:
        if isinstance(o, dict):
            o["first"] = [x for x in o.get("first") or [] if isinstance(x, dict) and x.get("v") in have][:3]
            o["videos"] = [x for x in o.get("videos") or [] if isinstance(x, dict) and x.get("v") in have]
            ids = {t["id"] for t in threads}
            o["replies"] = [{"id": r["id"], "result": r["result"], "text": str(r.get("text") or "").strip()[:600]}
                            for r in o.get("replies") or [] if isinstance(r, dict) and r.get("id") in ids
                            and r.get("result") in RESULTS and str(r.get("text") or "").strip()]
            o = apply_marks(claimed(o, have), marks)
            if not prev:
                o["answer_changed"] = True
        return o

    def check(o: dict) -> None:
        if missing := sorted(asked - {r["id"] for r in o.get("replies") or []}):
            raise ContractError("CONTENT_FAILURE", f"批注 {'、'.join(missing)} 没有回复")
    ask = fill(prompt("synth"), question=question, notes=notes_line(notes), today=today or "", prev=_prev_block(prev, prev_at, marks),
               comments=comments_block(threads), videos="\n\n".join(parts))
    with gate("topic-synth"):
        return llm.structured(root, stage, ask, SYNTH_SCHEMA, normalize=normalize, check=check)
