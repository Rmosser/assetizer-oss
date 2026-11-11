"""The 底稿 of one video: a record made with no question in mind, kept by video and shared by every topic that has it.

Three passes, each kept as soon as it is done, so a stopped worker goes on where it was:

* the picture — the video model watches a 1 fps proxy, ten minutes at a time with 20 s of overlap, and writes timed
  entries (prompts/visual.md). It writes times within its segment; the code adds the segment's start and cuts each
  overlap at its middle. An answer holding the model's own thinking is asked again; a segment that will not come out
  right stays a gap the draft says it has.
* the speech — whisper with the language detected, traditional Chinese turned simplified. A transcript that is mostly
  the same few sentences over and over (whisper making speech of silence) is dropped, and said so.
* the text on screen — for the entries that show a screen, a chart, a slide or digits, a full-resolution frame at
  their middle (one every 15 s on long ones), twelve to a call, copied word for word by the video model at low effort
  (prompts/text.md).

They are put together by time: full.md, and index.md, where each frame's text is cut to its first 160 characters for
the planner of a long video (the full text stays in full.md). Validated on 31 videos of 14 kinds, 2026-11 (底稿实验)."""
from __future__ import annotations

import re
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..llm import LLM
from ..media import asr, ffmpeg
from ..providers.agy import ProviderError
from ..util import now, read_json, write_json, write_text
from . import draft_dir, fill, gate, prompt

VERSION = 1            # of the passes and their prompts: a draft of another version is made again
SEG, OVER = 600.0, 20.0
BATCH = 12             # frames to one call of the text pass
BRIEF = 160            # characters of a frame's text kept in index.md
GIVE_UP = ("PROVIDER_QUOTA", "AGY_AUTH_REQUIRED", "PROVIDER_MODEL", "AGY_MEDIA_READ")  # no other segment would fare better

_SPAN = r"\[(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—~]\s*(\d{1,2}:\d{2}(?::\d{2})?)\]"
TC = re.compile(r"^\s*" + _SPAN, re.M)
TC_LINE = re.compile(r"^\s*" + _SPAN)
HEAD = re.compile(r"^\s*" + _SPAN + r"\s*(.*)$")
GLUED = re.compile(r"(?<=[^\n])(?=\[\d{1,2}:\d{2}(?::\d{2})?\s*[-–—~]\s*\d{1,2}:\d{2}(?::\d{2})?\]\s*类型)")
CJK = re.compile(r"[一-鿿]")
THINK = re.compile(r"^(Wait\b|Let's\b|Let me\b|Let us\b|Segment \d|Look at\b|Looking at\b|Hmm|Possibility\b|Actually\b|Okay\b|"
                   r"Ok,|So,|Now,|Therefore\b|The prompt\b|The user\b|I need\b|I will\b|I'll\b|I should\b|We need\b|Is there\b|"
                   r"Did \b|Does \b|Should I\b|Double-check|Re-check|Checking\b|Self-check)")
ABOUT_TASK = re.compile(r"\b(segment|prompt|evaluator|entry|entries|timecode|subtitles?|the record|fields?|rules?|instructions?|"
                        r"output|transcri\w+|narrat\w+|speaker says)\b", re.I)
SCREEN = ("软件界面", "代码", "图表", "幻灯片", "板书手写")
FIELD = re.compile(r"^(画面|文字|图形|动作|声音|不确定)：", re.M)
SECTION = re.compile(r"^## 图(\d+)\s*$", re.M)


def secs(tc: str) -> float:
    out = 0.0
    for part in tc.split(":"):
        out = out * 60 + float(part)
    return out


def fmt(s: float) -> str:
    s = int(round(s))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m:02d}:{x:02d}"


def segments(duration: float) -> list[tuple[str, float, float]]:
    """(id, start, length): ten minutes each, overlapping by 20 s; a video up to eleven minutes is one."""
    if duration <= SEG + 60:
        return [("CH01", 0.0, duration)]
    out, st, i = [], 0.0, 1
    while st < duration - OVER:
        out.append((f"CH{i:02}", st, min(SEG, duration - st)))
        st += SEG - OVER
        i += 1
    return out


def final_record(text: str) -> str:
    """The model sometimes hands back its thinking, a self-check and the record copied out again: keep the last run of
    entries that starts from the beginning, without what follows it."""
    lines = GLUED.sub("\n", text).splitlines()  # a record sometimes starts right after the thinking's last sentence
    heads = [(i, secs(m[1])) for i, line in enumerate(lines) if (m := TC_LINE.match(line))]
    if not heads:
        return text
    start = heads[0][0]
    for (i, t), (_, prev) in zip(heads[1:], heads):
        if t < prev - 30:  # time went back: it started over
            start = i
    end = len(lines)
    for j in range(len(lines) - 1, start, -1):
        if lines[j].lstrip().startswith(("- ", "->", "Let", "Check", "**")) and not TC_LINE.match(lines[j]):
            end = j
        elif lines[j].strip():
            break
    return "\n".join(lines[start:end])


def problem(record: str, length: float) -> str | None:
    """Why a segment's record cannot be used, or None: no entries, past the end, stopped early, too little covered."""
    rows = [(secs(a), secs(b)) for a, b in TC.findall(record)]
    if not rows:
        return "没有带时间的条目"
    last = max(b for _, b in rows)
    if last > length + 15:
        return f"时间超出片段：{fmt(last)} > {fmt(length)}"
    if last < length - max(25, length * 0.08):
        return f"没记完：停在 {fmt(last)}，片段长 {fmt(length)}"
    cover = sum(max(0, b - a) for a, b in rows) / max(1, length)
    return None if cover >= 0.75 else f"只覆盖了 {cover:.0%}"


def thinking(record: str) -> int:
    """Lines of the model talking to itself: they start in English like a thought (Wait, Let's…) and are about the task
    (the segment, the prompt, a field, the subtitles). English on a screen seldom does both."""
    return sum(1 for line in record.splitlines()
               if (s := line.strip()) and not CJK.search(s[:40]) and (THINK.match(s) or s.startswith("---")) and ABOUT_TASK.search(s))


def entries(record: str, offset: float) -> list[dict]:
    out: list[dict] = []
    for line in record.splitlines():
        if m := HEAD.match(line):
            out.append({"start": secs(m[1]) + offset, "end": secs(m[2]) + offset, "head": m[3].strip(), "body": []})
        elif out and line.strip():
            out[-1]["body"].append(line.rstrip())
    return out


def rows(segs: list[dict]) -> list[dict]:
    """The entries of all segments in the video's time; where two overlap, each keeps its half."""
    segs = sorted(segs, key=lambda s: s["start"])
    out: list[dict] = []
    for i, s in enumerate(segs):
        if s.get("error"):
            out.append({"start": s["start"], "end": s["start"] + s["length"], "head": "[这段没有画面记录]",
                        "body": [f"原因：{s['error']}"]})
            continue
        lo = (s["start"] + segs[i - 1]["start"] + segs[i - 1]["length"]) / 2 if i else float("-inf")
        hi = (segs[i + 1]["start"] + s["start"] + s["length"]) / 2 if i + 1 < len(segs) else float("inf")
        out += [e for e in entries(s["record"], s["start"]) if lo <= e["start"] < hi]
    return out


def wants(e: dict) -> bool:
    """Whether an entry is worth a full-resolution frame: a screen of any kind, or digits or a run of letters in what it
    says is written there (a nameplate, a reading, a label, a card)."""
    body = "\n".join([e["head"], *e["body"]])
    if any(k in body for k in SCREEN):
        return True
    parts = FIELD.split(body)
    text = " ".join(parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i] == "文字")
    text = text.replace("画面底部有对白字幕", "").replace("同上", "")
    return bool(re.search(r"\d|[A-Za-z]{4,}|[぀-ヿ]", text))


def frame_times(entries_: list[dict], duration: float) -> list[float]:
    out: list[float] = []
    for e in entries_:
        if not wants(e):
            continue
        a, b = max(0.0, e["start"]), min(duration - 0.5, e["end"])
        if b <= a:
            continue
        ts = [(a + b) / 2] if b - a <= 20 else [a + 3 + 15 * k for k in range(int((b - a - 3) // 15) + 1)]
        for t in ts:
            if not out or t - out[-1] >= 3:
                out.append(round(t, 1))
    return out


def grab(src: Path, t: float, out: Path) -> Path:
    """A full-resolution frame (simple numbered names: the model copies names with leading zeros and decimals wrongly)."""
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.run(["-ss", f"{max(0.0, t):.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)], nice=10)
    return out


def shot(vid: str, t: float, duration: float | None) -> tuple[float, Path]:
    """The original's frame at t (to a tenth of a second, inside the video) and where it is kept once taken: the frames
    the notes cite are taken when the read ends (run.keep_shots), the page's others when asked (web/topics frame)."""
    t = round(min(t, max(0.0, duration - 0.2)) if duration else t, 1)
    return t, draft_dir(vid) / "shots" / f"{t:.1f}.jpg"


def parse_text(text: str, n: int) -> list[str]:
    parts = SECTION.split(GLUED.sub("\n", text))
    got = {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return [got.get(i + 1, "") for i in range(n)]


def heard(t: dict) -> list[dict]:
    """The transcript's sentences, or none when most of it is the same few sentences again."""
    said = [x for x in t.get("segments") or [] if x.get("text", "").strip()]
    if said:
        c = Counter(x["text"].strip() for x in said)
        rep = sum(n for n in c.values() if n >= 3)
        if rep / len(said) >= 0.5:
            t["note"] = f"疑似幻听：{rep}/{len(said)} 句重复 3 次以上，整段转写已丢弃"
            return []
    return said


def _silent(e: dict) -> bool:
    return any(line.startswith("声音：") and "无人说话" in line for line in e["body"])


def render(meta: dict, duration: float, entries_: list[dict], t: dict, shots: list[dict], brief: int = 0) -> str:
    said = heard(t)
    lang = t.get("language") or "无"
    out = [f"# {meta.get('title') or meta['id']}", "",
           f"- 链接：https://www.youtube.com/watch?v={meta['id']}",
           f"- 频道：{meta.get('channel') or ''}",
           f"- 时长：{fmt(duration)}",
           f"- 转写：{lang}（{t.get('device') or '没有转写'}）" + (f"；{t['note']}" if t.get("note") else ""), ""]
    body, j, dropped, gone, seen = [], 0, 0, [], set()
    for e in entries_:
        while j < len(said) and said[j]["start"] < e["start"] - 0.5:  # speech between two entries
            body.append(f"说 {fmt(said[j]['start'])}：{said[j]['text'].strip()}")
            j += 1
        body += ["", f"[{fmt(e['start'])}-{fmt(e['end'])}] {e['head']}".rstrip(), *e["body"]]
        for x in shots:
            if e["start"] <= x["t"] < e["end"] and (x.get("text") or "").strip() not in ("", "[看不清]"):
                at = f"{fmt(int(x['t']))}.{int(round(x['t'] * 10)) % 10}"  # a tenth: a chart's reading follows the mouse
                lines = [line.strip() for line in x["text"].splitlines() if line.strip()]
                fresh = [line for line in lines if line not in seen]  # tabs and toolbars repeat on every frame
                seen = set(lines)
                line = " ⏎ ".join(fresh) if fresh else "同上"
                if brief and len(line) > brief:
                    line = line[:brief] + f"…（共 {len(line)} 字）"
                body.append(f"高清帧 {at}：" + line)
        lines = []
        while j < len(said) and said[j]["start"] < e["end"]:
            lines.append(said[j]["text"].strip())
            j += 1
        if lines and _silent(e):
            dropped += len(lines)
            gone.append(f"{fmt(e['start'])}：" + " ".join(lines))
        elif lines:
            body.append("说：" + " ".join(lines))
    body += [f"说 {fmt(x['start'])}：{x['text'].strip()}" for x in said[j:]]
    if dropped:
        out[-2] += f"；画面记录写了“无人说话”的时段丢弃 {dropped} 句（见文末）"
        body += ["", "## 丢弃的转写", "", *gone]
    return "\n".join(out + body).strip() + "\n"


class Draft:
    """Making one video's 底稿. `check` raises Stopped when the read it is for should stop; `progress(done, of)` tells the
    page how far it is (segments, then frame batches)."""

    def __init__(self, vid: str, meta: dict, src: Path | None, llm: LLM, check: Callable[[], None],
                 progress: Callable[[int, int], None]):
        self.vid, self.meta, self.src, self.llm, self.check, self.progress = vid, meta, src, llm, check, progress
        self.d = draft_dir(vid)
        self.done, self.of = 0, 0

    def _tick(self) -> None:
        self.done += 1
        self.progress(self.done, self.of)

    def _visual(self, seg: tuple[str, float, float], limit: int | None) -> list[dict]:
        pid, st, length = seg
        kept = self.d / "visual" / f"{pid}.json"
        if kept.exists():
            return [read_json(kept)]
        proxy = self.d / "proxies" / f"{pid}_{st:.0f}_{length:.0f}.mp4"
        if not proxy.exists() or (limit and proxy.stat().st_size > limit):
            proxy.parent.mkdir(parents=True, exist_ok=True)
            ffmpeg.fit_proxy(self.src, st, length, proxy, fps=1.0, limit=limit)
        length = ffmpeg.duration(proxy)
        ask = fill(prompt("visual"), path=proxy.resolve(), length=fmt(length))
        why = ""
        for attempt in range(1, 4):
            self.check()
            stage = f"draft-visual-{pid}" + ("" if attempt == 1 else f"-r{attempt}")
            try:
                with gate(stage):
                    text = self.llm.text(self.d / "calls", stage, ask, min_chars=40, media=[str(proxy)])
            except ProviderError as e:
                if e.kind in GIVE_UP:
                    raise
                if e.kind == "PROVIDER_OUTPUT_LIMIT" and length > 120:  # too dense: each half on its own
                    half = length / 2
                    self.of += 1
                    return (self._visual((pid + "a", st, half + OVER / 2), limit)
                            + self._visual((pid + "b", st + half - OVER / 2, length - half + OVER / 2), limit))
                why = f"模型没有写成（{e.kind}）"
                break
            record = final_record(text)
            n = thinking(record)
            why = problem(record, length) or (f"混进了 {n} 行模型的思考" if n and attempt < 3 else "")
            if not why:
                seg_ = {"pid": pid, "start": st, "length": length, "record": record}
                write_json(kept, seg_)
                self._tick()
                return [seg_]
        self._tick()
        return [{"pid": pid, "start": st, "length": length, "error": why}]

    def _speech(self) -> dict:
        kept = self.d / "transcript.json"
        if kept.exists():
            return read_json(kept)
        if not any(s.get("codec_type") == "audio" for s in ffmpeg.probe(self.src)["streams"]):
            t = {"language": None, "device": None, "segments": [], "note": "没有音轨"}
        else:
            wav = ffmpeg.extract_wav(self.src, self.d / "speech.wav")
            try:
                r = asr.transcribe(wav, language=None)
            finally:
                wav.unlink(missing_ok=True)
            segs = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in r["segments"]]
            if (r.get("language") or "").startswith("zh"):
                try:
                    import opencc
                    cc = opencc.OpenCC("t2s")
                    for s in segs:
                        s["text"] = cc.convert(s["text"])
                except ImportError:
                    pass
            t = {"language": r.get("language"), "device": r.get("device"), "segments": segs}
        write_json(kept, t)
        return t

    def _text(self, entries_: list[dict], duration: float) -> list[dict]:
        kept = self.d / "text.json"
        if kept.exists():
            return read_json(kept)
        ts = frame_times(entries_, duration)
        batches = [ts[b:b + BATCH] for b in range(0, len(ts), BATCH)]
        self.of += len(batches)
        self.progress(self.done, self.of)
        frames = self.d / "frames"

        def one(k: int, chunk: list[float]) -> list[dict]:
            self.check()
            files = [grab(self.src, t, frames / f"{k * BATCH + i + 1:03d}.jpg") for i, t in enumerate(chunk)]
            images = "\n".join(f"{i}. {f.resolve()}" for i, f in enumerate(files, 1))
            stage = f"draft-text-B{k + 1:03d}"
            try:
                with gate(stage):
                    text = self.llm.text(self.d / "calls", stage, fill(prompt("text"), images=images), min_chars=1,
                                         media=[str(f) for f in files], effort="low")
            except ProviderError as e:
                if e.kind in GIVE_UP:
                    raise
                text = ""  # these frames go without their text; the visual record still has the entries
            self._tick()
            return [{"t": t, "text": x or None} for t, x in zip(chunk, parse_text(text, len(chunk)))]

        with ThreadPoolExecutor(3) as ex:
            parts = list(ex.map(lambda a: one(*a), enumerate(batches)))
        out = [x for p in parts for x in p]
        write_json(kept, out)
        shutil.rmtree(frames, ignore_errors=True)
        return out

    def make(self) -> dict:
        """The draft, made or as kept: {version, duration, gaps, language, chars, made_at}. With no original (`src`
        None: gone from YouTube) the one kept, of whatever version."""
        done = self.d / "draft.json"
        if done.exists() and (self.d / "full.md").exists() and ((d := read_json(done)).get("version") == VERSION or not self.src):
            return d
        if not self.src:
            raise RuntimeError("原片下载不了，也没有底稿")
        duration = ffmpeg.duration(self.src)
        plan = segments(duration)
        self.done, self.of = 0, len(plan)
        self.progress(0, self.of)
        limit = getattr(self.llm.provider("draft"), "media_size_limit", ffmpeg.VIEW_FILE_LIMIT)
        self.d.mkdir(parents=True, exist_ok=True)  # the speech thread writes its wav here before any visual makes it
        with ThreadPoolExecutor(1) as speech:
            heard_ = speech.submit(self._speech)
            with ThreadPoolExecutor(3) as ex:
                segs = [s for part in ex.map(lambda s: self._visual(s, limit), plan) for s in part]
            entries_ = rows(segs)
            shots = self._text(entries_, duration)
            t = heard_.result()
        write_text(self.d / "full.md", render(self.meta, duration, entries_, dict(t), shots))
        write_text(self.d / "index.md", render(self.meta, duration, entries_, dict(t), shots, brief=BRIEF))
        shutil.rmtree(self.d / "proxies", ignore_errors=True)
        d = {"version": VERSION, "duration": duration, "gaps": [s["pid"] for s in segs if s.get("error")],
             "language": t.get("language"), "chars": len((self.d / "full.md").read_text()), "made_at": now()}
        write_json(done, d)
        return d
