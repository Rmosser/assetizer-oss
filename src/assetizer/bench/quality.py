"""Quality checks on the golden set. C2/C3: does one understanding call over the whole video now find as much, and
place it as well, as the 50-minute chunks with 5 minutes of overlap (§R-18) that the pipeline uses? Both run in the same
benchmark with the same model and prompt, so a model change shows up as a change in the gap, not as noise."""
from __future__ import annotations

import math
import statistics
from pathlib import Path

from .. import config, db, youtube
from ..contracts import prompts as P
from ..llm import LLM
from ..media import ffmpeg
from ..util import parse_tc

MATCH_WINDOW = 60.0  # seconds: a topic may start this far outside a unit and still be the same passage


def media(g: dict) -> Path:
    """The golden video's source file: the bench cache, the production media dir, or a fresh download."""
    for d in (config.get().home / "bench" / "media" / g["id"], config.get().media_dir / g["id"]):
        found = sorted(p for p in d.glob("source.*") if p.suffix in (".mkv", ".mp4", ".webm") and p.stat().st_size > 0)
        if found:
            return found[0]
    return youtube.download(g["url"], config.get().home / "bench" / "media" / g["id"])


def chunk_plan(duration: float) -> list[tuple[float, float]]:
    """(offset, length) pairs exactly as pipeline/acquire.chunk cuts them."""
    s = config.get()
    size, overlap = s.chunk_minutes * 60, s.chunk_overlap_minutes * 60
    if duration <= size + 120:
        return [(0.0, duration)]
    step = size - overlap
    starts = [i * step for i in range(math.ceil((duration - overlap) / step))]
    return [(st, min(size, duration - st)) for st in starts]


def _playbook(channel: str | None) -> dict:
    row = db.one("SELECT data FROM playbooks WHERE channel_id=? AND status='ACTIVE' ORDER BY version DESC", channel) \
        if channel else None
    return db.unj(row["data"], {}) if row else {"universal": True}


def understanding(p, g: dict, work: Path) -> dict:
    src = media(g)
    cache = config.get().home / "bench" / "media" / g["id"]
    cache.mkdir(parents=True, exist_ok=True)
    playbook = _playbook(g.get("channel"))
    llm = LLM(provider=p)
    variants = {"whole": [(0.0, float(g["duration"]))], "chunked": chunk_plan(float(g["duration"]))}
    out: dict = {"units": len(g["units"]), "variants": {}}
    for name, plan in variants.items():
        topics, used = [], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "wall": 0.0}
        try:
            for i, (offset, length) in enumerate(plan):
                proxy, enc = ffmpeg.fit_proxy(src, offset, length, cache / f"{name}-{i + 1}-{offset:.0f}-{length:.0f}.mp4")
                part = {"part_id": f"CH{i + 1:02}"}
                rec = llm.structured(work / name, f"understand-{part['part_id']}",
                                     P.understand_prompt(playbook, str(proxy), part, length),
                                     P.UNDERSTAND_SCHEMA, media=[str(proxy)])
                receipt = _last_receipt(work / name / f"understand-{part['part_id']}")
                used["calls"] += 1
                used["input_tokens"] += (receipt.get("usage") or {}).get("input_tokens") or 0
                used["output_tokens"] += (receipt.get("usage") or {}).get("output_tokens") or 0
                used["wall"] += receipt.get("wall_seconds") or 0
                for t in rec["topics"]:
                    try:
                        a, b = offset + parse_tc(t["start"]), offset + parse_tc(t["end"])
                    except ValueError:
                        continue
                    topics.append({"title": t.get("title"), "tickers": t.get("tickers") or [], "start": a, "end": max(b, a)})
            out["variants"][name] = {**score(g["units"], topics), **used, "proxy": enc,
                                     "topics": len(topics)}
        except Exception as e:  # noqa: BLE001 -- a failed variant is a result too, the other one still runs
            out["variants"][name] = {"error": f"{type(e).__name__}: {str(e)[:200]}", **used}
    return out


def _last_receipt(stage_dir: Path) -> dict:
    import json
    found = sorted(stage_dir.glob("attempt-*/invocation.json"), key=lambda f: f.stat().st_mtime)
    return json.loads(found[-1].read_text()) if found else {}


def score(units: list[dict], topics: list[dict], window: float = MATCH_WINDOW) -> dict:
    """A unit is found when a topic overlaps its span (widened by `window`) and, if the unit names tickers, shares one.
    Its start error is how far the nearest such topic starts from the unit's start."""
    errors, found = [], 0
    for u in units:
        want = {t.upper() for t in u.get("tickers") or []}
        cands = [t for t in topics if t["start"] <= u["end"] + window and t["end"] >= u["start"] - window
                 and (not want or want & {x.upper() for x in t["tickers"]})]
        if cands:
            found += 1
            errors.append(min(abs(t["start"] - u["start"]) for t in cands))
    n = len(units) or 1
    return {"recall": round(found / n, 3),
            "start_error_median": round(statistics.median(errors), 1) if errors else None,
            "within_60s": round(sum(e <= 60 for e in errors) / n, 3)}
