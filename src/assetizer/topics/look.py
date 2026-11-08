"""Looking again: each item of a plan's 看 list answered from the original, and the ones one model alone gets wrong
answered once more by a second model from the same images.

Modes (what the planner is told in prompts/plan.md):
* frame  — 1–3 full-resolution frames across the span (one when it is under 4 s);
* frames — a frame every `step` s (at least 0.2), at most 8 spread evenly; or one at each of `times` (at most 8);
* zoom   — the middle frame; the model first finds `target` on it (0–1000 box), the code crops it with 8% to spare and
           scales its long edge to 1800 px, and the model is shown the crop and the whole frame;
* clip   — the span with a second to spare each side, at most 40 s, 4 frames a second up to 20 s (else 2), low
           resolution: for movement, counts and order only.

The answer is one line of JSON {answer, evidence, sure}; `second: true` items go to the second model too (answer2,
evidence2, sure2). What was shown stays under media/<id>/ of the read, for the page's evidence panel."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .. import knobs
from ..llm import LLM
from ..media import ffmpeg
from ..providers import is_agy, resolve
from ..providers.agy import ProviderError
from ..util import read_json, write_json
from . import fill, gate, prompt
from .draft import GIVE_UP, grab

MODES = ("frame", "frames", "zoom", "clip")
MOST = 25            # items in one list
SECOND_IMAGES = 12   # images in one call of the second model: through Claude Code, about 20 in a message get no answer
JSON = re.compile(r"\{.*\}", re.S)
ARRAY = re.compile(r"\[.*\]", re.S)


def at(v: Any) -> float:
    """A time of the plan: the draft's mm:ss(.s) / h:mm:ss, or seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    out = 0.0
    for part in str(v).strip().split(":"):
        out = out * 60 + float(part)
    return out


def tc(s: float) -> str:
    return f"{int(s) // 60:02d}:{s % 60:04.1f}"


def parse(text: str) -> dict:
    for cand in reversed(JSON.findall(text) or []):
        try:
            o = json.loads(cand)
            if isinstance(o, dict):
                return o
        except json.JSONDecodeError:
            continue
    return {"answer": text.strip()[:2000], "evidence": "", "sure": False, "unparsed": True}


SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9]{0,7}")


def clean(items: Any, prefix: str) -> list[dict]:
    """The usable items of a 看 list, at most MOST: a known mode, a question, a time to look at, and an id of its own
    (it names the folder of what was shown: one the model gave twice, an odd one, or one without the round's `prefix` —
    an L03 in the second round would be shown the first round's frames — is numbered anew)."""
    out, seen = [], set()
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict) or not str(x.get("question") or "").strip():
            continue
        x = {**x, "mode": x.get("mode") if x.get("mode") in MODES else "frame"}
        lid = str(x.get("id") or "")
        if not SAFE_ID.fullmatch(lid) or not lid.startswith(prefix) or lid in seen:
            lid = next(f"{prefix}{n:02d}" for n in range(1, 99) if f"{prefix}{n:02d}" not in seen)
        x["id"] = lid
        try:
            if x.get("times"):
                x["times"] = [t for t in x["times"] if at(t) >= 0][:8]
                x.setdefault("start", x["times"][0])
                x.setdefault("end", x["times"][-1])
            at(x["start"])
            at(x.get("end", x["start"]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        seen.add(lid)
        out.append(x)
    return out[:MOST]


class Looker:
    """The 看 list of one read. `root` is the read's folder: media/<id>/ for what was shown, calls/ for the calls."""

    def __init__(self, src: Path, duration: float, title: str, root: Path, llm: LLM, check: Callable[[], None],
                 progress: Callable[[int, int], None]):
        self.src, self.duration, self.title, self.root, self.llm = src, duration, title, root, llm
        self.check, self.progress = check, progress

    def _ask(self, stage: str, text: str, media: list[Path]) -> dict:
        with gate(stage):
            return parse(self.llm.text(self.root / "calls", stage, text, min_chars=5, media=[str(m) for m in media]))

    def one(self, x: dict) -> dict:
        self.check()
        lid, mode = x["id"], x["mode"]
        a = max(0.0, at(x["start"]))
        b = max(a, min(self.duration - 0.3, at(x.get("end", x["start"]))))
        md = self.root / "media" / lid
        md.mkdir(parents=True, exist_ok=True)
        out = {**x, "media": [], "times_shown": []}
        if mode == "clip":
            a, b = max(0.0, a - 1), min(self.duration, b + 1)
            length = min(40.0, max(1.0, b - a))
            fps = 4.0 if length <= 20 else 2.0
            clip = md / f"clip_{a:.1f}_{length:.1f}.mp4"
            if not clip.exists():
                limit = getattr(self.llm.provider("look"), "media_size_limit", ffmpeg.VIEW_FILE_LIMIT)
                ffmpeg.fit_proxy(self.src, a, length, clip, fps=fps, limit=limit)
            media = [clip]
            span = f"{tc(a)} 起的 {length:.0f} 秒片段（每秒 {fps:.0f} 帧，低清；片段内第 0 秒 = 原片 {tc(a)}）"
            listing = f"1. {clip.resolve()}"
            out["times_shown"] = [round(a, 1), round(a + length, 1)]
        elif mode == "frames":
            if x.get("times"):  # a few moments apart, e.g. burned-in subtitles
                ts = sorted(round(min(self.duration - 0.3, max(0.0, at(t))), 1) for t in x["times"])[:8]
            else:
                step = max(0.2, float(x.get("step") or 1.0))
                n = min(8, int((b - a) / step) + 1)  # with 12 Gemini sometimes did not read them all
                ts = [round(a + (b - a) * k / (n - 1), 1) for k in range(n)] if n > 1 else [round(a, 1)]
            media = [grab(self.src, t, md / f"f{i:02d}.jpg") for i, t in enumerate(ts, 1)]
            span = f"{tc(ts[0])} 到 {tc(ts[-1])} 按时间顺序的 {len(ts)} 张全分辨率帧"
            listing = "\n".join(f"{i}. {f.resolve()}（原片 {tc(t)}）" for i, (f, t) in enumerate(zip(media, ts), 1))
            out["times_shown"] = ts
        else:
            n = 1 if mode == "zoom" or b - a <= 4 else 3
            ts = [round((a + b) / 2, 1)] if n == 1 else [round(a + (b - a) * k / 2, 1) for k in range(3)]
            media = [grab(self.src, t, md / f"f{i}.jpg") for i, t in enumerate(ts, 1)]
            span = "、".join(tc(t) for t in ts) + (" 的单帧" if n == 1 else " 的三帧")
            listing = "\n".join(f"{i}. {f.resolve()}（原片 {tc(t)}）" for i, (f, t) in enumerate(zip(media, ts), 1))
            out["times_shown"] = ts
            if mode == "zoom":
                target = str(x.get("target") or x["question"])
                loc = self._ask(f"look-{lid}-locate", fill(prompt("locate"), image=media[0].resolve(), target=target), media[:1])
                box = loc.get("box")
                out["box"] = box if isinstance(box, list) and len(box) == 4 else None
                if out["box"]:
                    y0, x0, y1, x1 = (min(1000.0, max(0.0, float(v))) / 1000 for v in out["box"])
                    left, right = max(0.0, min(x0, x1) - 0.08), min(1.0, max(x0, x1) + 0.08)  # 8% to spare each side
                    top, bottom = max(0.0, min(y0, y1) - 0.08), min(1.0, max(y0, y1) + 0.08)
                    crop = md / "crop.png"
                    if not crop.exists():
                        vf = (f"crop=iw*{right - left:.4f}:ih*{bottom - top:.4f}:iw*{left:.4f}:ih*{top:.4f},"
                              "scale='if(gt(iw,ih),1800,-2)':'if(gt(iw,ih),-2,1800)':flags=lanczos")
                        ffmpeg.run(["-i", str(media[0]), "-vf", vf, str(crop)], nice=10)
                    media = [crop, media[0]]
                    listing = (f"1. {crop.resolve()}（原片 {tc(ts[0])} 这一帧里“{target}”的放大图）\n"
                               f"2. {media[1].resolve()}（整帧）")
        out["media"] = [str(m.relative_to(self.root)) for m in media]
        ask = fill(prompt("look"), media=listing, title=self.title, span=span, question=x["question"])
        try:
            got = self._ask(f"look-{lid}", ask, media)
            out.update(answer=str(got.get("answer") or ""), evidence=str(got.get("evidence") or ""),
                       sure=bool(got.get("sure")), **({"unparsed": True} if got.get("unparsed") else {}))
        except ProviderError as e:
            if e.kind in GIVE_UP:
                raise
            out.update(answer=None, error=e.kind)
        return out

    def run(self, items: list[dict], name: str) -> list[dict]:
        """Every item, four at a time; kept as `name`.json."""
        kept = self.root / f"{name}.json"
        if kept.exists():
            return read_json(kept)
        done = [0]
        self.progress(0, len(items))

        def one(x: dict) -> dict:
            r = self.one(x)
            done[0] += 1
            self.progress(done[0], len(items))
            return r
        with ThreadPoolExecutor(4) as ex:
            out = list(ex.map(one, items))
        write_json(kept, out)
        return out

    def second(self, results: list[dict], name: str, round_: str) -> list[dict]:
        """The `second: true` items answered again by the second model from the same images, at most SECOND_IMAGES to a
        call; merged into `name`.json as answer2, evidence2, sure2."""
        kept = self.root / f"{name}.json"
        mark = self.root / f"{name}.second"
        if mark.exists():
            return read_json(kept)
        todo = [x for x in results if x.get("second") and not x.get("error")
                and any(not m.endswith(".mp4") for m in x.get("media") or [])]
        calls: list[list[dict]] = []
        for x in todo:
            n = sum(1 for m in x["media"] if not m.endswith(".mp4"))
            if not calls or sum(sum(1 for m in y["media"] if not m.endswith(".mp4")) for y in calls[-1]) + n > SECOND_IMAGES:
                calls.append([])
            calls[-1].append(x)
        self.progress(0, len(calls))
        agy = is_agy(resolve(knobs.topic_routes()["second"]))
        got: dict[str, dict] = {}
        for k, group in enumerate(calls, 1):
            self.check()
            items, media, no = [], [], 0
            for x in group:
                imgs = [m for m in x["media"] if not m.endswith(".mp4")]
                shown = list(x.get("times_shown") or [])
                lines = []
                for i, m in enumerate(imgs):
                    no += 1
                    media.append(self.root / m)
                    when = "放大图" if m.endswith("crop.png") else f"原片 {tc(shown[min(i, len(shown) - 1)])}" if shown else ""
                    lines.append(f"- 图 {no}（{when}）" + (f"：{(self.root / m).resolve()}" if agy else ""))
                items.append(f"### {x['id']}\n问题：{x['question']}\n" + "\n".join(lines))
            read = ("依次用 view_file 读取下面列出的每张图片，每张只读一次，全部读完再回答。禁止其他工具。" if agy
                    else "图片按图号附在后面。")
            ask = fill(prompt("second"), title=self.title, read=read, items="\n\n".join(items))
            stage = f"second-{round_}{k:02d}"
            try:
                with gate(stage):
                    text = self.llm.text(self.root / "calls", stage, ask, min_chars=5, media=[str(m) for m in media])
            except ProviderError as e:
                if e.kind in GIVE_UP:
                    raise
                text = ""  # these stay with one answer: the notes say 未核实
            for cand in reversed(ARRAY.findall(text) or []):
                try:
                    arr = json.loads(cand)
                except json.JSONDecodeError:
                    continue
                if isinstance(arr, list):
                    got.update({str(o.get("id")): o for o in arr if isinstance(o, dict)})
                    break
            self.progress(k, len(calls))
        out = []
        for x in results:
            s = got.get(x["id"])
            out.append({**x, "answer2": str(s.get("answer") or ""), "evidence2": str(s.get("evidence") or ""),
                        "sure2": bool(s.get("sure"))} if s else x)
        write_json(kept, out)
        mark.touch()
        return out
