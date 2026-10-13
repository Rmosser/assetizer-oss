"""Channel page templates for Notion: the layouts the user already keeps per channel.

Both reuse the article the playbook produced (sections in Source order) and only
change the page frame and where the media sit:

* ``reading-guide`` (交易员汤姆): source link → heading → 复盘背景概述 → 一、读图案例深度解析
  (each case: video, then its fields; real screenshots under 推荐截图位置) → 二、优先学习清单.
* ``chapters`` (王海滨): YouTube embed → 视频发布于… → 第N章 (video, screenshots, text,
  📎 原话参考 from the transcript) → closing list.

Plans are deterministic: the same run always yields the same keys and blocks.
"""
from __future__ import annotations

import re

from . import config, db
from .notion import blocks as B
from .notion import mapping as M
from .util import fmt_tc, read_json

CN = "零一二三四五六七八九十"


def _cn(n: int) -> str:
    if n <= 10:
        return CN[n]
    if n < 20:
        return "十" + (CN[n - 10] if n > 10 else "")
    return CN[n // 10] + "十" + (CN[n % 10] if n % 10 else "")


def _comp(run_id: str) -> list[dict]:
    return read_json(config.get().runs_dir / run_id / "composition.json")["blocks"]


def _split(md: str) -> tuple[str | None, str]:
    lines = md.strip().splitlines()
    if lines and lines[0].startswith("#"):
        return re.sub(r"^#+\s*", "", lines[0]).strip(), "\n".join(lines[1:])
    return None, "\n".join(lines)


def _prose(md: str) -> str:
    """Intro prose without headings, tables or navigation rows."""
    keep = [ln.strip() for ln in md.splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", "|", "<!--")) and not ln.strip().startswith("视频发布于")]
    return "\n".join(keep)


def _media(k: str, blk: dict) -> tuple[list[dict], list[dict]]:
    video, shots = [], []
    if blk.get("video"):
        v = blk["video"]
        cap = f"{fmt_tc(v['start'])}–{fmt_tc(v['end'])}" + (" · 烧录字幕（实验）" if v["subtitled"] else "") + \
              ("" if v["standalone"] else " · 依赖前文")
        video.append({"key": f"{k}:video", "media": {"path": v["path"], "kind": "video", "caption": cap}})
    for si, sc in enumerate(blk.get("screenshots", [])):
        shots.append({"key": f"{k}:shot{si}", "media": {"path": sc["path"], "kind": "image",
                                                        "caption": f"{fmt_tc(sc['t'])} · {sc['reason']}"}})
    return video, shots


def _md(k: str, md: str, tag: str = "p") -> list[dict]:
    return [{"key": f"{k}:{tag}{j}", "block": b} for j, b in enumerate(B.markdown(md))]


def _ctx(source: dict, run_id: str) -> dict:
    ch = db.one("SELECT * FROM channels WHERE id=?", source.get("channel_id")) if source.get("channel_id") else None
    return M.context(source, run_id, ch)


# ------------------------------------------------------------------ reading guide
def reading_guide(source: dict, run_id: str, target: dict) -> list[dict]:
    ctx = _ctx(source, run_id)
    blocks = _comp(run_id)
    plan: list[dict] = [{"key": "src", "block": B.block("paragraph", f"**来源链接**：[{ctx['url']}]({ctx['url']})")}]
    heading = re.sub(r"\{(\w+)\}", lambda x: str(ctx.get(x[1], "")), target.get("heading") or "").strip()
    if heading:
        plan.append({"key": "h1", "block": B.block("heading_1", heading)})
    intro = next((b for b in blocks if b["kind"] == "INTRO"), None)
    if intro and (text := _prose(intro["markdown"])):
        plan.append({"key": "intro", "block": B.block("quote", f"**复盘背景概述**：\n{text}")})
    plan.append({"key": "div0", "block": B.divider()})
    plan.append({"key": "part1", "block": B.block("heading_2", "一、读图案例深度解析（学习地图）")})
    for blk in blocks:
        if blk["kind"] != "UNIT":
            continue
        k = blk["section_id"]
        title, body = _split(blk["markdown"])
        video, shots = _media(k, blk)
        plan.append({"key": f"{k}:h", "block": B.block("heading_3", title or "")})
        plan += video
        # field subheadings become bold labels; real screenshots go under 推荐截图位置
        parts = re.split(r"^#{2,6}\s+(.+)$", body, flags=re.M)
        if parts[0].strip():
            plan += _md(k, parts[0], "p0_")
        placed = False
        for i in range(1, len(parts), 2):
            label, text = parts[i].strip(), parts[i + 1]
            plan.append({"key": f"{k}:f{i}", "block": B.block("paragraph", f"**{label}**")})
            plan += _md(k, text, f"f{i}_")
            if "截图" in label and shots:
                plan += shots
                placed = True
        if not placed:
            plan += shots
        plan.append({"key": f"{k}:div", "block": B.divider()})
    # the playbook's closing list already is the 优先学习清单; build one from the units only when it is missing
    outro = next((b for b in blocks if b["kind"] == "OUTRO" and b["markdown"].strip()), None)
    _, body = _split(outro["markdown"]) if outro else (None, "")
    has_table = any(ln.lstrip().startswith("|") for ln in body.splitlines())
    rows = [] if has_table else _priority_rows(run_id)
    if has_table or rows:
        plan.append({"key": "part2", "block": B.block("heading_2", "二、优先学习清单（Top 推荐精选）")})
    if has_table:
        plan += _md("part2", body, "p")
    else:
        if rows:
            md = "| 优先级 | 视频时间 | Ticker | 核心读图知识点 | 为什么优先回看 |\n|---|---|---|---|---|\n" + \
                 "\n".join("| " + " | ".join(c.replace("|", "／") for c in r) + " |" for r in rows)
            plan += _md("part2", md, "t")
        if body.strip():
            plan.append({"key": "part3", "block": B.block("heading_2", "三、本期可带走的要点")})
            plan += _md("part3", body, "p")
    return plan


def _priority_rows(run_id: str, limit: int = 8) -> list[list[str]]:
    rows = db.q("SELECT title, start, end, learning_priority, data FROM units WHERE run_id=? AND learning_priority=3 "
                "ORDER BY start", run_id)[:limit]
    out = []
    for n, r in enumerate(rows, 1):
        d = db.unj(r["data"], {})
        out.append([f"**{n}**", f"`{fmt_tc(r['start'])}–{fmt_tc(r['end'])}`", f"**{' / '.join(d.get('tickers') or []) or '—'}**",
                    r["title"], (d.get("why") or "").strip()])
    return out


# ------------------------------------------------------------------ chapters
def chapters(source: dict, run_id: str, target: dict) -> list[dict]:
    ctx = _ctx(source, run_id)
    blocks = _comp(run_id)
    segs = _segments(run_id)
    plan: list[dict] = [{"key": "embed", "block": {"object": "block", "type": "embed", "embed": {"url": ctx["url"]}}},
                        {"key": "div0", "block": B.divider()}]
    if ctx["date"]:
        y, m, d = ctx["date"].split("-")
        plan.append({"key": "pub", "block": B.block("paragraph", f"视频发布于{y}年{m}月{d}日")})
    heading = re.sub(r"\{(\w+)\}", lambda x: str(ctx.get(x[1], "")), target.get("heading") or "").strip()
    if heading:
        plan.append({"key": "h1", "block": B.block("heading_1", heading)})
    intro = next((b for b in blocks if b["kind"] == "INTRO"), None)
    if intro and (text := _prose(intro["markdown"])):
        plan += _md("intro", text, "p")
    n = 0
    for blk in blocks:
        if blk["kind"] != "UNIT":
            continue
        n += 1
        k = blk["section_id"]
        title, body = _split(blk["markdown"])
        title = re.sub(r"^\d+\s*[.、]\s*", "", title or "")
        video, shots = _media(k, blk)
        plan.append({"key": f"{k}:h", "block": B.block("heading_2", f"第{_cn(n)}章：{title}")})
        plan += video + shots + _md(k, body)
        span = (blk["video"]["start"], blk["video"]["end"]) if blk.get("video") else _unit_span(blk.get("unit_id"))
        quotes = _quotes(segs, *span) if span else []
        if quotes:
            plan.append({"key": f"{k}:qh", "block": B.block("paragraph", "**📎 原话参考**（自动转写）")})
            plan += [{"key": f"{k}:q{j}", "block": B.block("quote", q)} for j, q in enumerate(quotes)]
        plan.append({"key": f"{k}:div", "block": B.divider()})
    outro = next((b for b in blocks if b["kind"] == "OUTRO"), None)
    if outro and outro["markdown"].strip():
        plan += _md("outro", outro["markdown"], "p")
    return plan


def _segments(run_id: str) -> list[dict]:
    p = config.get().runs_dir / run_id / "machine" / "transcript.json"
    return read_json(p).get("segments", []) if p.exists() else []


def _unit_span(unit_id: str | None) -> tuple[float, float] | None:
    r = db.one("SELECT start, end FROM units WHERE id=?", unit_id) if unit_id else None
    return (r["start"], r["end"]) if r else None


def _quotes(segs: list[dict], a: float, b: float, limit: int = 4, width: int = 160) -> list[str]:
    out = []
    for s in segs:
        if s["end"] <= a or s["start"] >= b or not (s.get("text") or "").strip():
            continue
        t = s["text"].strip()
        out.append(f"[{fmt_tc(max(s['start'], a))}] " + (t if len(t) <= width else t[:width] + "…"))
        if len(out) >= limit:
            break
    return out


PLANS = {"reading-guide": reading_guide, "chapters": chapters}
