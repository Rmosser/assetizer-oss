"""Subtitle segmentation — Run-008 frozen sentence-flow algorithm.

Ported from Run-014 ``subtitle_algorithm.py`` with the logic unchanged:
lexical characters are preserved exactly (asserted at the end); spaces and
punctuation are presentation only; a DP picks cue boundaries preferring
sentence/clause punctuation and pauses, with width/duration as soft pressure.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

import jieba

MARKERS = ["但是", "所以", "因为", "然后", "如果", "虽然", "其实", "那么", "而且", "不过", "就是", "比如", "另外", "否则",
           "but", "so", "because", "if", "when", "then", "and"]
BASE_PHRASES = ["Gap Up", "Gap Down", "Stop Loss", "Relative Strength", "Buy Point", "Profit Taking", "Price Action"]

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2.4,0.6,2,44,44,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def canon(s: str) -> str:
    return re.sub(r"[\s，。！？：；、,.?!:;…]", "", s)


def width(s: str) -> float:
    return sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.5 for c in s if not c.isspace())


def stamp(t: float, ass: bool = False) -> str:
    u = 100 if ass else 1000
    n = round(t * u)
    return (f"{n // (3600 * u):02}:{n // (60 * u) % 60:02}:{n // u % 60:02}"
            + ("." if ass else ",") + f"{n % u:0{2 if ass else 3}}")


def segment(frozen: list[dict], words: list[dict], entities: list[str], dur: float, sid: str):
    """Return (cues, diff, ops). ``frozen`` = raw ASR segments; ``words`` = word timestamps."""
    text = " ".join(c["text"].replace("\n", " ") for c in frozen)
    text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)
    raw, rawmap = "", []
    for k, w in enumerate(words):
        cw = canon(w["word"])
        raw += cw
        rawmap.extend([(max(0, w["start"]), min(dur, w["end"]), k)] * len(cw))
    content = canon(text)
    mapped: list = [None] * len(content)
    ops = []
    for tag, a, b, c, d in difflib.SequenceMatcher(None, raw, content, autojunk=False).get_opcodes():
        if tag == "equal":
            mapped[c:d] = [(s, e, [k]) for s, e, k in rawmap[a:b]]
        elif c < d:
            spans = rawmap[a:b] or rawmap[max(0, a - 1):a + 1]
            v = (min(x[0] for x in spans), max(x[1] for x in spans), sorted({x[2] for x in spans}))
            mapped[c:d] = [v] * (d - c)
            ops.append({"op": tag, "chars": [c, d], "source_words": v[2], "span": v[:2]})
    pos, ci = [], 0
    for ch in text:
        pos.append(ci)
        ci += len(canon(ch))
    phrases = BASE_PHRASES + [e for e in entities if e]
    pattern = re.compile("|".join(re.escape(x) for x in sorted(phrases, key=len, reverse=True) if x)
                         + r"|\$?\d+(?:\.\d+)?\s*(?:%|EMA|MA|[BMK]|美元|美金|倍)?|[A-Za-z]+(?:'[A-Za-z]+)?", re.I)
    chunks, cursor = [], 0
    for m in pattern.finditer(text):
        chunks.extend((z, cursor + a, cursor + b, False) for z, a, b in jieba.tokenize(text[cursor:m.start()]))
        chunks.append((m.group(), m.start(), m.end(), True))
        cursor = m.end()
    chunks.extend((z, cursor + a, cursor + b, False) for z, a, b in jieba.tokenize(text[cursor:]))
    ts = []
    for tx, a, b, protected in chunks:
        ca = pos[a]
        cb = pos[b] if b < len(pos) else len(content)
        if ca == cb:
            if ts and tx.strip():
                ts[-1]["text"] += tx
            continue
        mm = mapped[ca:cb]
        ts.append(dict(text=tx, start=min(x[0] for x in mm), end=max(x[1] for x in mm),
                       words=sorted({k for x in mm for k in x[2]}), a=ca, b=cb, protected=protected))

    def join(i: int, j: int) -> str:
        out = ""
        for z in ts[i:j]:
            if out and re.search(r"[A-Za-z0-9]$", out) and re.match("[A-Za-z]", z["text"]):
                out += " "
            out += z["text"]
        return out

    n = len(ts)
    if n == 0:
        return [], [], ops
    strength = {0: 8, n: 8}
    reasons = {n: "end_of_clip"}
    for k in range(1, n):
        left = join(max(0, k - 3), k)
        right = ts[k]["text"]
        gap = ts[k]["start"] - ts[k - 1]["end"]
        s, reason = 0, "phrase_fallback"
        if re.search("[。？！!?]$", left):
            s, reason = 8, "existing_sentence_punctuation"
        elif re.search("[，,：:；;]$", left):
            s, reason = 5, "existing_clause_punctuation"
        elif gap >= 0.65:
            s, reason = 7, "sentence_pause"
        elif gap >= 0.28:
            s, reason = 4, "clause_pause"
        elif right.lower() in MARKERS:
            s, reason = 3, "discourse_candidate"
        if re.search(r"(的|把|被|不|没|别|很|我|你|他|它|在|和|与|不会|不能|不要)$", left):
            s, reason = -8, "protected_syntax"
        if ts[k]["start"] < ts[k - 1]["end"] - 0.001:
            s, reason = -100, "shared_timestamp_span"
        strength[k] = s
        reasons[k] = reason
    dp = [float("inf")] * (n + 1)
    dp[0] = 0
    back: dict[int, int] = {}
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            units = width(join(i, j))
            dur_ij = max(x["end"] for x in ts[i:j]) - ts[i]["start"]
            if units > 90 and j - i > 1:
                break
            if strength.get(i, 0) <= -100 or strength.get(j, 0) <= -100:
                continue
            missed = sum(max(0, strength[k] - 4) * 5 for k in range(i + 1, j))
            cost = 12 - max(0, strength[j]) * 1.2 + missed + max(0, units - 32) ** 2 * 0.1 + max(0, dur_ij - 8) ** 2 * 0.6
            if strength[j] < 0:
                cost += 25
            if dur_ij < 1:
                cost += 12
            if units < 5:
                cost += 10
            if dp[i] + cost < dp[j]:
                dp[j] = dp[i] + cost
                back[j] = i
    if n not in back:
        raise ValueError("SEGMENTATION_NO_PATH")
    groups, j = [], n
    while j:
        i = back[j]
        groups.append((i, j))
        j = i
    groups.reverse()
    cues, diff = [], []
    for k, (i, j) in enumerate(groups):
        tx = join(i, j)
        if k < len(groups) - 1 and not re.search("[，。？！；：,.?!:;]$", tx):
            if reasons[j] == "sentence_pause":
                tx += "。"
            elif strength[j] >= 3:
                tx += "，"
        if width(tx) > 16 and j - i > 1:
            cands = []
            for z in range(i + 1, j):
                a, b = join(i, z), join(z, j)
                score = max(0, width(a) - 16) ** 2 + max(0, width(b) - 16) ** 2 + abs(width(a) - width(b)) * 0.2 - strength[z] * 2
                if min(width(a), width(b)) < 3:
                    score += 30
                if strength[z] <= -100:
                    score += 100
                cands.append((score, z))
            z = min(cands)[1]
            a, b = join(i, z), join(z, j)
            tx = a + "\n" + b + tx[len(join(i, j)):]
        st = max(0, ts[i]["start"] - 0.05)
        end = min(dur, max(t["end"] for t in ts[i:j]) + 0.2)
        nxt = max(0, ts[j]["start"] - 0.05) if j < n else dur
        end = min(nxt, max(end, min(st + 0.8, nxt)))
        if end <= st:
            end = st + 0.3
        idx = sorted({w for t in ts[i:j] for w in t["words"]})
        cid = f"{sid}-SF{k + 1:03}"
        cues.append(dict(cue_id=cid, start=st, end=end, text=tx, source_words=idx, boundary_reason=reasons[j],
                         protected_tokens=[t["text"] for t in ts[i:j] if t["protected"]]))
        diff.append(dict(cue_id=cid, speech_start=ts[i]["start"], speech_end=max(t["end"] for t in ts[i:j])))
    if canon("".join(c["text"] for c in cues)) != content:
        raise ValueError("SEGMENTATION_LEXICAL_DRIFT")
    return cues, diff, ops


def to_srt(cues: list[dict]) -> str:
    return "\n".join(f"{k + 1}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}\n" for k, c in enumerate(cues))


def to_ass(cues: list[dict]) -> str:
    return ASS_HEADER + "".join(
        f"Dialogue: 0,{stamp(c['start'], True)},{stamp(c['end'], True)},Default,,0,0,0,,"
        + c["text"].replace("\n", r"\N") + "\n" for c in cues)


def warnings(cues: list[dict]) -> list[dict]:
    return [{"cue_id": c["cue_id"], "reason": "LONG_OR_DENSE_CUE"} for c in cues
            if c["end"] - c["start"] > 8 or len(c["text"]) / max(0.001, c["end"] - c["start"]) > 9]
