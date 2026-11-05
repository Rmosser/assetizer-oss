"""原话模式：正文从转写逐段改写，而不是从理解摘要总结（design §R7）。

本模块只放确定性的计算：把转写划给知识单元、检查正文有没有被压缩、算出去掉长静音后要保留的时间段。
"""
from __future__ import annotations

import re

from ..llm import ContractError
from ..util import fmt_tc

MODE = "transcript"
UNASSIGNED_MAX = 0.05     # 模型漏划超过这个比例就重做一次；更少的漏划并给前一段
COMPRESSED_BELOW = 0.6    # 正文字数低于原话这个比例，算被压缩（口语有卡顿和重复，正常改写约 0.7–1.0）
SILENCE_MIN = 5.0         # 这么长的静音才剪掉：短停顿是说话的节奏
SILENCE_MARGIN = 0.4      # 剪口两侧各留一点，不切字
SILENCE_MERGE = 1.0       # 静音之间夹的这么短的声响算噪声，一起剪掉
PIECE_MIN = 0.6           # 比这更短的片段不单独留
PROCESS_TERMS = re.compile(r"主题记录|所附记录|所附资料|所附材料|原记录|视频记录|转写文本|语音转写|source_text|记录(?:中|里|未|并未|没有|显示|称)(?![西])")


def enabled(playbook: dict | None) -> bool:
    return bool(playbook) and playbook.get("fidelity") == MODE


def cjk(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def numbered(segments: list[dict]) -> str:
    return "\n".join(f"[{i} {fmt_tc(s['start'])}] {s['text'].strip()}" for i, s in enumerate(segments))


def _owners(units: list[dict], n: int) -> list[int | None]:
    owner: list[int | None] = [None] * n
    for ui, u in enumerate(units):
        for sp in u.get("spans") or []:
            lo, hi = sorted((max(0, sp["from"]), min(n - 1, sp["to"])))
            for i in range(lo, hi + 1):
                if owner[i] is None:  # 划重了的归先出现的单元，同一句话不写两遍
                    owner[i] = ui
    return owner


def check_partition(units: list[dict], segments: list[dict]) -> None:
    """模型的划分漏掉太多原话就退回重做（llm.structured 会把原因反馈给下一次调用）。"""
    owner = _owners(units, len(segments))
    total = sum(cjk(s["text"]) for s in segments) or 1
    missed = [i for i, o in enumerate(owner) if o is None]
    lost = sum(cjk(segments[i]["text"]) for i in missed)
    if lost / total > UNASSIGNED_MAX:
        raise ContractError("SCHEMA_VIOLATION", f"有 {lost} 字原话没有划给任何单元（如第 {missed[0]} 行起），每一行都要划进且只划进一个单元")


def partition(units: list[dict], segments: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    """label → 时间段。每行转写恰好属于一个单元；漏划的并给前一行的单元（开头的并给后一行的）。"""
    owner = _owners(units, len(segments))
    missed = sum(o is None for o in owner)
    last = next((o for o in owner if o is not None), None)
    for i, o in enumerate(owner):
        if o is None:
            owner[i] = last
        else:
            last = o
    out: dict[str, list[dict]] = {}
    i = 0
    while i < len(owner):
        j = i
        while j + 1 < len(owner) and owner[j + 1] == owner[i]:
            j += 1
        if owner[i] is not None:
            out.setdefault(units[owner[i]]["label"], []).append(
                {"start": segments[i]["start"], "end": segments[j]["end"], "from": i, "to": j})
        i = j + 1
    return out, {"missed_lines": missed, "dropped_units": [u["label"] for u in units if u["label"] not in out]}


def text_of(segments: list[dict], spans: list[dict]) -> str:
    """一个单元的全部原话；不相邻的段落之间标出时间，让写作的人知道中间隔了别的内容。"""
    parts = []
    for sp in spans:
        parts.append(f"（{fmt_tc(sp['start'])}）" + "".join(s["text"].strip() for s in segments[sp["from"]:sp["to"] + 1]))
    return "\n".join(parts)


def missing_numbers(source: str, md: str) -> list[str]:
    """原话里的阿拉伯数字没进正文的。转写把大数都写成阿拉伯数字，它们丢了最容易核对。"""
    plain = md.replace(",", "")
    seen, out = set(), []
    for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r"（\d\d:\d\d:\d\d）", "", source)):
        if len(n) >= 2 and n not in seen:
            seen.add(n)
            if n not in plain:
                out.append(n)
    return out


def section_problems(role: str, source: str, md: str) -> list[str]:
    """正文要退回重写的原因；空列表就是过关。"""
    out = []
    terms = sorted(set(PROCESS_TERMS.findall(md)))
    if terms:
        out.append("出现了读者看不懂的整理过程用语：" + "、".join(terms))
    if role == "TOPIC" and cjk(source) >= 40:
        a, b = cjk(source), cjk(md)
        if b < COMPRESSED_BELOW * a:
            out.append(f"被压缩了：原话 {a} 字，正文只有 {b} 字")
        numbers = missing_numbers(source, md)
        if len(numbers) >= 3:
            out.append("漏了原话里的数字：" + "、".join(numbers[:12]))
    return out


def cut_silences(spans: list[list[float]], silences: list[tuple[float, float]]) -> list[list[float]]:
    """去掉 spans 里的长静音，返回要保留的时间段（按时间排序）。什么都不剩时原样返回。"""
    merged: list[list[float]] = []
    for a, b in sorted(silences):
        if merged and a - merged[-1][1] < SILENCE_MERGE:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    keep: list[list[float]] = []
    for a, b in sorted(spans):
        cur = a
        for sa, sb in merged:
            ca, cb = max(sa, a), min(sb, b)
            if cb - ca < SILENCE_MIN:
                continue
            if ca + SILENCE_MARGIN - cur >= PIECE_MIN:
                keep.append([cur, ca + SILENCE_MARGIN])
            cur = max(cur, cb - SILENCE_MARGIN)
        if b - cur >= PIECE_MIN:
            keep.append([cur, b])
    out: list[list[float]] = []
    for a, b in keep:
        if out and a - out[-1][1] <= 0.05:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out or sorted([list(s) for s in spans])


def to_local(t: float, keep: list[list[float]]) -> float:
    """原片时间 → 剪辑后片段里的时间。落在剪掉部分的时间贴到下一段开头。"""
    done = 0.0
    for a, b in keep:
        if t <= a:
            return done
        if t <= b:
            return done + t - a
        done += b - a
    return done


def inside(a: float, b: float, keep: list[list[float]], slack: float = 0.05) -> bool:
    return any(a >= x - slack and b <= y + slack for x, y in keep)
