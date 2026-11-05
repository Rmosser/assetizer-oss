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
EXPANDED_ABOVE = 1.3      # 高于这个比例，算写长了（多出来的是套话和他没说的话）
EXPAND_MIN_SOURCE = 100   # 原话太短时比例没有意义
SILENT_SHARE = 0.8        # 一句转写有这么大比例落在长静音里，就是语音识别凭空生成的
QUESTION_GAP = 3.0        # 问答单元里停顿这么久就标一次时间，让每一问的时间写得准
SILENCE_MIN = 5.0         # 这么长的静音才剪掉：短停顿是说话的节奏
SILENCE_MARGIN = 0.4      # 剪口两侧各留一点，不切字
SILENCE_MERGE = 1.0       # 静音之间夹的这么短的声响算噪声，一起剪掉
PIECE_MIN = 0.6           # 比这更短的片段不单独留
PROCESS_TERMS = re.compile(r"主题记录|所附记录|所附资料|所附材料|原记录|视频记录|转写|source_text|如实记录|文稿|记录(?:中|里|未|并未|没有|显示|称)(?![西])")
# 最容易被换成中性词的说法：粗口、贬义词、极端说法、对某国某类人的称呼。原话有、正文没有，就退回重写
STRONG_WORDS = re.compile(
    r"他妈|妈的|我操|屁|个毛|毛事|滚蛋|骗子|猥琐|无耻|垃圾|傻|蠢|混蛋|流氓|恶心|王八|狗|废物|杀光|杀人|穷人|吓死|要命"
    r"|政治动物|草台|累赘|(?:泰国|美国|中国|日本|韩国|台湾|大陆|香港|德国|英国|法国|俄罗斯|乌克兰|越南|印度|伊朗|以色列|阿拉伯|犹太)人")


def enabled(playbook: dict | None) -> bool:
    return bool(playbook) and playbook.get("fidelity") == MODE


def cjk(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def spoken(segments: list[dict], silences: list[tuple[float, float]]) -> list[dict]:
    """去掉落在长静音里的转写：没人说话时语音识别会凭空生成“请点赞订阅”“谢谢观看”这类句子。"""
    quiet = [s for s in _merged(silences) if s[1] - s[0] >= SILENCE_MIN]
    out = []
    for sg in segments:
        length = sg["end"] - sg["start"]
        hit = sum(max(0.0, min(b, sg["end"]) - max(a, sg["start"])) for a, b in quiet)
        silent = hit >= SILENT_SHARE * length if length > 0 else any(a <= sg["start"] <= b for a, b in quiet)
        if not silent:
            out.append(sg)
    return out


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


def text_of(segments: list[dict], spans: list[dict], mark_pauses: bool = False) -> str:
    """一个单元的全部原话；不相邻的段落之间标出时间，让写作的人知道中间隔了别的内容。mark_pauses 时每次停顿也标时间。"""
    parts = []
    for sp in spans:
        text, prev = f"（{fmt_tc(sp['start'])}）", None
        for s in segments[sp["from"]:sp["to"] + 1]:
            if mark_pauses and prev is not None and s["start"] - prev >= QUESTION_GAP:
                text += f"\n（{fmt_tc(s['start'])}）"
            text += s["text"].strip()
            prev = s["end"]
        parts.append(text)
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


def softened_words(source: str, md: str) -> list[str]:
    """原话里的粗口、贬义词、称呼，正文没有用的。"""
    return sorted({w for w in STRONG_WORDS.findall(source) if w not in md})


def section_problems(role: str, source: str, md: str) -> list[str]:
    """正文要退回重写的原因；空列表就是过关。"""
    out = []
    terms = sorted(set(PROCESS_TERMS.findall(md)))
    if terms:
        out.append("出现了读者看不懂的整理过程用语：" + "、".join(terms))
    softened = softened_words(source, md)
    if softened:
        out.append("原话的这些说法被换掉或丢了，要照原词写进去：" + "、".join(softened))
    if role == "TOPIC" and cjk(source) >= 40:
        a, b = cjk(source), cjk(md)
        if b < COMPRESSED_BELOW * a:
            out.append(f"被压缩了：原话 {a} 字，正文只有 {b} 字")
        elif a >= EXPAND_MIN_SOURCE and b > EXPANDED_ABOVE * a:
            out.append(f"写长了：原话 {a} 字，正文 {b} 字，去掉套话和他没说的话，内容一点不能少")
        numbers = missing_numbers(source, md)
        if len(numbers) >= 3:
            out.append("漏了原话里的数字：" + "、".join(numbers[:12]))
    return out


def _merged(silences: list[tuple[float, float]]) -> list[list[float]]:
    merged: list[list[float]] = []
    for a, b in sorted(silences):
        if merged and a - merged[-1][1] < SILENCE_MERGE:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def cut_silences(spans: list[list[float]], silences: list[tuple[float, float]]) -> list[list[float]]:
    """去掉 spans 里的长静音，返回要保留的时间段（按时间排序）。什么都不剩时原样返回。"""
    merged = _merged(silences)
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
