"""TRANSCRIBE → SCREENSHOTS → BOUNDARY → CLIPS → SUBTITLES → MEDIA_QA.

Everything here is non-blocking at the asset level: one failed clip never
stops the article, Notion or the package.
"""
from __future__ import annotations

import re
from bisect import bisect_left
from pathlib import Path

from .. import config, db
from ..contracts import prompts as P
from ..llm import ContractError
from ..media import ffmpeg, subtitles
from ..providers.agy import ProviderError
from ..util import fmt_tc, now, parse_tc, read_json, sha256_file, write_json, write_text
from . import fidelity
from .context import RunContext, StageWarning


def _asset(ctx: RunContext, unit_id: str | None, kind: str, status: str, path: Path | None = None,
           start: float | None = None, end: float | None = None, meta: dict | None = None, **cols) -> int:
    if kind != "screenshot":  # one row per (unit, kind); screenshots are cleared per stage run
        db.exec("DELETE FROM assets WHERE run_id=? AND unit_id IS ? AND kind=?", ctx.id, unit_id, kind)
    sha = sha256_file(path) if path and path.exists() else None
    size = path.stat().st_size if path and path.exists() else None
    keys = ["run_id", "source_id", "unit_id", "kind", "status", "path", "sha256", "bytes", "start", "end", "meta", "created_at", *cols]
    vals = [ctx.id, ctx.source["id"], unit_id, kind, status, str(path) if path else None, sha, size, start, end,
            db.j(meta or {}), now(), *cols.values()]
    return db.exec(f"INSERT INTO assets({','.join(keys)}) VALUES ({','.join('?' * len(vals))})", *vals)


def _units(ctx: RunContext) -> list[dict]:
    return ctx.load("machine/knowledge-units.json")["units"]


# ------------------------------------------------------------------ transcribe
def transcribe(ctx: RunContext) -> dict:
    out = ctx.dir / "machine" / "transcript.json"
    if out.exists():
        t = read_json(out)
        return {"words": len(t["words"]), "cached": True}
    # the same video transcribed by an earlier run (another playbook version): speech does not depend on the
    # playbook, and sharing one transcript keeps the compared versions on identical subtitles and quotes
    prev = ctx.source.get("transcript_path")
    if prev and prev != str(out) and Path(prev).exists():
        t = read_json(Path(prev))
        if t.get("media_sha256") and t["media_sha256"] == ctx.source.get("media_sha256"):
            write_json(out, t)
            return {"words": len(t["words"]), "reused": prev}
    from ..media import asr

    tickers = []
    for c in ctx.load("machine/chunks.json", []):
        for t in c["topics"]:
            tickers += t.get("tickers", [])
    top = sorted(set(tickers), key=tickers.count, reverse=True)[:40]
    prompt = "以下是中文投资交易视频，涉及 " + "、".join(top) + "。" if top else None
    wav = ffmpeg.extract_wav(ctx.media, ctx.path("work", "source.wav"))
    try:
        r = asr.transcribe(wav, initial_prompt=prompt)
    finally:
        wav.unlink(missing_ok=True)
    try:
        import opencc
        cc = opencc.OpenCC("t2s")
        for s in r["segments"]:
            s["text"] = cc.convert(s["text"])
        for w in r["words"]:
            w["word"] = cc.convert(w["word"])
    except ImportError:
        pass
    r["media_sha256"] = ctx.source.get("media_sha256")
    write_json(out, r)
    db.exec("UPDATE sources SET transcript_path=? WHERE id=?", str(out), ctx.source["id"])
    return {"words": len(r["words"]), "segments": len(r["segments"]), "device": r.get("device"),
            **({"remote_failed": r["remote_failed"]} if r.get("remote_failed") else {})}


def _transcript(ctx: RunContext) -> dict | None:
    p = ctx.dir / "machine" / "transcript.json"
    return read_json(p) if p.exists() else None


# ------------------------------------------------------------------ screenshots
def screenshots_off(ctx: RunContext) -> bool:
    """频道设置 screenshots:<频道>=off：这个频道的画面没有信息量（只有口播），文章不放截图。"""
    return db.setting(f"screenshots:{ctx.source.get('channel_id')}") == "off"


def screenshots(ctx: RunContext) -> dict:
    n = 0
    db.exec("DELETE FROM assets WHERE run_id=? AND kind='screenshot'", ctx.id)
    if screenshots_off(ctx):
        return {"screenshots": 0, "off": True}
    for u in _units(ctx):
        for i, a in enumerate(u.get("screenshot_anchors", [])[:2]):
            p = ctx.path("assets", "screenshots", u["label"], f"{i + 1}.jpg")
            try:
                side = p.with_suffix(".json")
                if p.exists() and side.exists():
                    t = read_json(side)["t_actual"]
                else:
                    tmp = p.with_name(f".{p.stem}.tmp.jpg")
                    _, t = ffmpeg.sharpest_frame(ctx.media, a["t"], tmp)
                    write_json(side, {"anchor": a, "t_actual": t})
                    tmp.replace(p)
                _asset(ctx, u["id"], "screenshot", "GENERATED", p, start=t, end=t,
                       meta={"reason": a.get("reason"), "timeframe": a.get("timeframe"),
                             "visible_structure": a.get("visible_structure"), "anchor_t": a["t"]})
                n += 1
            except Exception as e:  # noqa: BLE001
                ctx.flag("UNIT", u["id"], "SCREENSHOT_FAILED", "INFO", f"截图失败：{u['title']}", str(e)[:300])
    return {"screenshots": n}


# ------------------------------------------------------------------ boundary snapping
PROTECTED_END = re.compile(r"(的|把|被|不|没|别|很|我|你|他|它|在|和|与|这个|那个|就是)$")


def _boundaries(words: list[dict], seg_ends: set[int]) -> list[tuple[float, float, int, int]]:
    """Candidate cut points between words: (cut_start_time, prev_end_time, strength, index)."""
    out = []
    for k in range(1, len(words)):
        prev, cur = words[k - 1], words[k]
        gap = cur["start"] - prev["end"]
        s = 0
        pw = prev["word"].strip()
        if re.search(r"[。？！!?]$", pw):
            s = 8
        elif re.search(r"[，,：:；;]$", pw):
            s = 5
        elif gap >= 0.65:
            s = 7
        elif gap >= 0.28:
            s = 4
        if k - 1 in seg_ends:
            s = max(s, 5) + 1
        if PROTECTED_END.search(pw):
            s = -8
        out.append((cur["start"], prev["end"], s, k))
    return out


def snap(start: float, end: float, words: list[dict], seg_ends: set[int], next_start: float | None,
         dur: float) -> dict:
    cands = _boundaries(words, seg_ends)
    times = [c[0] for c in cands]
    res = {"start": start, "end": end, "start_snapped": False, "end_snapped": False}
    # start: prefer earlier, window [-6, +2]
    lo, hi = bisect_left(times, start - 6), bisect_left(times, start + 2)
    best = None
    for c in cands[lo:hi]:
        if c[2] < 4:
            continue
        delta = c[0] - start
        score = c[2] - abs(delta) * 0.4 - (1.5 if delta > 0 else 0)
        if best is None or score > best[0]:
            best = (score, c)
    if best:
        res["start"] = max(0.0, best[1][0] - 0.15)
        res["start_snapped"] = True
        res["start_strength"] = best[1][2]
    # end: prefer later, window [-1, +6], never beyond next unit start (unless already beyond)
    cap = dur
    if next_start is not None and next_start > end:
        cap = min(cap, next_start + 0.3)
    lo, hi = bisect_left(times, end - 1), bisect_left(times, end + 6)
    best = None
    for c in cands[lo:hi]:
        if c[2] < 4 or c[1] + 0.3 > cap:
            continue
        delta = c[1] - end
        score = c[2] - abs(delta) * 0.4 - (1.5 if delta < 0 else 0)
        if best is None or score > best[0]:
            best = (score, c)
    if best:
        res["end"] = min(dur, best[1][1] + 0.3)
        res["end_snapped"] = True
        res["end_strength"] = best[1][2]
    if res["end"] - res["start"] < 5:
        res["start"], res["end"] = start, end
        res["start_snapped"] = res["end_snapped"] = False
    return res


def _seg_end_indices(t: dict) -> set[int]:
    ends, wi = set(), 0
    words = t["words"]
    for s in t["segments"]:
        while wi < len(words) and words[wi]["end"] <= s["end"] + 0.01:
            wi += 1
        if wi:
            ends.add(wi - 1)
    return ends


MAX_CLIP = 15 * 60
BOUNDARY_PARALLEL = 3


def main_span(u: dict, gap: float = 60.0) -> tuple[float, float]:
    """Longest cluster of a unit's segments (segments closer than ``gap`` merge)."""
    segs = sorted(u.get("segments") or [{"start": u["start"], "end": u["end"]}], key=lambda x: x["start"])
    clusters = [[segs[0]["start"], segs[0]["end"]]]
    for sg in segs[1:]:
        if sg["start"] - clusters[-1][1] <= gap:
            clusters[-1][1] = max(clusters[-1][1], sg["end"])
        else:
            clusters.append([sg["start"], sg["end"]])
    # the first substantial cluster is where the unit is taught; later clusters are usually re-mentions
    for a, b in clusters:
        if b - a >= 30:
            return a, b
    a, b = max(clusters, key=lambda c: c[1] - c[0])
    return a, b


def _boundaries_from_spans(ctx: RunContext, units: list[dict], sel: list[dict]) -> dict:
    """原话模式：单元的时间段就是文字用到的原话，片段照它剪，文字和视频对得上，不用再问模型。
    每个 TOPIC 都有片段；归总的问答和杂谈不成片。"""
    verdicts = {s["unit_id"]: s for s in sel}
    results = {}
    for u in units:
        if u.get("role", "TOPIC") != "TOPIC":
            continue
        sl = verdicts.get(u["id"]) or {}
        verdict = sl.get("verdict") or "SELECT"
        if verdict == "REJECT":
            verdict = "CONTEXT_DEPENDENT"
        spans, left = [], float(MAX_CLIP)
        for sg in sorted(u["segments"], key=lambda x: x["start"]):
            a, b = max(0.0, sg["start"] - 0.15), min(ctx.duration, sg["end"] + 0.3)
            if spans and a <= spans[-1][1]:
                left -= max(0.0, b - spans[-1][1])
                spans[-1][1] = max(spans[-1][1], b)
            else:
                left -= b - a
                spans.append([a, b])
            if left <= 0:
                spans[-1][1] += left
                ctx.flag("UNIT", u["id"], "CLIP_TOO_LONG", "REVIEW", f"「{u['title']}」超过 {MAX_CLIP // 60} 分钟，片段截取前 {MAX_CLIP // 60} 分钟")
                break
        results[u["id"]] = {"label": u["label"], "model": {}, "source": "transcript", "model_start": spans[0][0], "model_end": spans[-1][1],
                            "start": spans[0][0], "end": spans[-1][1], "start_snapped": False, "end_snapped": False, "spans": spans,
                            "selection": verdict, "publication_priority": sl.get("publication_priority") or "P3", "context_missing": False}
    ctx.save("machine/boundaries.json", results)
    return {"model": 0, "fallback": 0, "snapped": 0, "from_spans": len(results)}


def boundary(ctx: RunContext) -> dict:
    if fidelity.enabled(ctx.playbook) and _transcript(ctx):
        return _boundaries_from_spans(ctx, _units(ctx), ctx.load("machine/selection.json", {"selections": []})["selections"])
    s = config.get()
    units = {u["id"]: u for u in _units(ctx)}
    ordered = sorted(units.values(), key=lambda u: u["start"])
    nxt = {u["id"]: (ordered[i + 1]["start"] if i + 1 < len(ordered) else None) for i, u in enumerate(ordered)}
    sel = ctx.load("machine/selection.json", {"selections": []})["selections"]
    tr = _transcript(ctx)
    words = tr["words"] if tr else []
    seg_ends = _seg_end_indices(tr) if tr else set()
    llm = ctx.llm()

    def one(sl: dict) -> tuple[str, dict] | None:
        u = units.get(sl["unit_id"])
        if not u:
            return None
        span_a, span_b = main_span(u)
        if (span_a, span_b) != (u["start"], u["end"]):
            ctx.flag("UNIT", u["id"], "UNIT_SPLIT_SPAN", "INFO", f"「{u['title']}」引用了不相邻的段落，片段只取主要部分",
                     evidence={"segments": u.get("segments")})
        if span_b - span_a > MAX_CLIP:
            ctx.flag("UNIT", u["id"], "CLIP_TOO_LONG", "REVIEW", f"「{u['title']}」超过 {MAX_CLIP // 60} 分钟，片段截取前 {MAX_CLIP // 60} 分钟")
            span_b = span_a + MAX_CLIP
        w0 = max(0.0, span_a - s.boundary_pad_seconds)
        w1 = min(ctx.duration, span_b + s.boundary_pad_seconds)
        src = "model"
        try:
            win = ctx.dir / "work" / "windows" / f"{u['label']}.mp4"
            if not (ctx.calls / f"boundary-{u['label']}" / "accepted.json").exists() and not win.exists():
                ffmpeg.render_window(ctx.media, w0, w1 - w0, win)

            def check(o, L=w1 - w0):
                a, b = parse_tc(o["start"]), parse_tc(o["end"])
                if not (0 <= a < b <= L + 2):
                    raise ContractError("SCHEMA_VIOLATION", f"boundary outside window {a}-{b}/{L}")

            o = llm.structured(ctx.calls, f"boundary-{u['label']}",
                               P.boundary_prompt(ctx.playbook, str(win), {k: u[k] for k in ("label", "title", "summary", "required_context")}
                                                 | {"unit_start_in_window": fmt_tc(span_a - w0), "unit_end_in_window": fmt_tc(span_b - w0)},
                                                 w1 - w0,
                                                 [x["title"] for x in units.values() if x["id"] != u["id"]
                                                  and x["start"] < w1 and x["end"] > w0]),
                               P.BOUNDARY_SCHEMA, media=[str(win)], check=check, enforce=True)
            a, b = w0 + parse_tc(o["start"]), min(w1, w0 + parse_tc(o["end"]))
        except ProviderError as e:
            if e.kind in ("AGY_AUTH_REQUIRED", "PROVIDER_QUOTA"):
                raise
            o, src = {"error": e.kind}, "fallback"
        except (ContractError, ffmpeg.MediaError) as e:
            o, src = {"error": str(e)[:200]}, "fallback"
        if src == "fallback":
            a, b = span_a, span_b
            ctx.flag("UNIT", u["id"], "BOUNDARY_FALLBACK", "REVIEW", f"「{u['title']}」边界模型失败，已按单元区间确定性剪辑",
                     evidence={"error": o.get("error")})
        # Experiment C (2026-10-05): raw model boundaries match Codex-verified cuts better than any ASR snapping
        # variant (≤3 s at both ends: raw 75–79 %, snapped 54–71 %). Snap only the deterministic fallback span.
        if words and src == "fallback":
            sn = snap(a, b, words, seg_ends, nxt.get(u["id"]), ctx.duration)
        else:
            sn = {"start": a, "end": b, "start_snapped": False, "end_snapped": False}
        if isinstance(o, dict) and o.get("context_missing"):
            ctx.flag("UNIT", u["id"], "CONTEXT_MISSING", "REVIEW", f"「{u['title']}」片段可能缺少上下文",
                     o.get("start_reason", ""))
        return u["id"], {"label": u["label"], "model": o, "source": src, "model_start": a, "model_end": b, **sn,
                         "selection": sl["verdict"], "publication_priority": sl["publication_priority"],
                         "context_missing": bool(o.get("context_missing")) if isinstance(o, dict) else False}

    # boundary calls read short 2 fps windows (~0.3–0.5 GB agy RSS) → safe to run a few at once
    from concurrent.futures import ThreadPoolExecutor
    todo = [sl for sl in sel if sl["verdict"] != "REJECT"]
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=BOUNDARY_PARALLEL) as pool:
        for r in pool.map(one, todo):
            if r:
                results[r[0]] = r[1]
    stats = {"model": sum(v["source"] == "model" for v in results.values()),
             "fallback": sum(v["source"] == "fallback" for v in results.values()),
             "snapped": sum(bool(v.get("start_snapped") and v.get("end_snapped")) for v in results.values())}
    ctx.save("machine/boundaries.json", results)
    return stats


# ------------------------------------------------------------------ clips
def dedupe_clips(bnd: dict, units: dict, iou_min: float = 0.85) -> dict[str, str]:
    """Map secondary unit → primary unit for near-identical clip spans (keep the higher learning priority)."""
    ids = sorted(bnd, key=lambda k: (-units[k]["learning_priority"], bnd[k]["start"]))
    shared: dict[str, str] = {}
    kept: list[str] = []
    for k in ids:
        a, b = bnd[k]["start"], bnd[k]["end"]
        for p in kept:
            pa, pb = bnd[p]["start"], bnd[p]["end"]
            inter = max(0.0, min(b, pb) - max(a, pa))
            union = max(b, pb) - min(a, pa)
            if union > 0 and inter / union >= iou_min:
                shared[k] = p
                break
        else:
            kept.append(k)
    return shared

def _silences(ctx: RunContext) -> list[tuple[float, float]]:
    """Long silences in the source audio, measured once per run."""
    p = ctx.dir / "machine" / "silences.json"
    if p.exists():
        return [tuple(x) for x in read_json(p)]
    found = ffmpeg.silences(ctx.media, min_dur=fidelity.SILENCE_MIN)
    write_json(p, found)
    return found


def clips(ctx: RunContext) -> dict:
    bnd = ctx.load("machine/boundaries.json", {})
    units = {u["id"]: u for u in _units(ctx)}
    tr = _transcript(ctx)
    words = tr["words"] if tr else []
    ok = failed = 0
    from_spans = any(b.get("spans") for b in bnd.values())
    shared = {} if from_spans else dedupe_clips(bnd, units)
    quiet = _silences(ctx) if from_spans else []
    for uid, primary in shared.items():
        u, pu = units[uid], units[primary]
        b = bnd[uid]
        _asset(ctx, uid, "clip", "NOT_SELECTED", None, b["start"], b["end"], {"shared_with": primary, "shared_title": pu["title"]},
               selection=b["selection"], publication_priority=b["publication_priority"])
        ctx.flag("UNIT", uid, "CLIP_SHARED", "INFO", f"「{u['title']}」与「{pu['title']}」落在同一段视频，只保留一个片段")
    for uid, b in bnd.items():
        if uid in shared:
            continue
        u = units[uid]
        a, e = b["start"], b["end"]
        keep = fidelity.cut_silences(b["spans"], quiet) if b.get("spans") else None
        if keep:
            a, e = keep[0][0], keep[-1][1]
        elif words:  # trim only real leading/trailing dead air (>1.5 s before first / after last word)
            inside = [w for w in words if w["start"] >= a and w["end"] <= e]
            if inside:
                if inside[0]["start"] - a > 1.5:
                    a = inside[0]["start"] - 0.3
                if e - inside[-1]["end"] > 1.5:
                    e = inside[-1]["end"] + 0.5
        d = ctx.path("assets", "clips", u["label"], "x").parent
        clean = d / "clip-clean.mp4"
        meta = {"model_start": b["model_start"], "model_end": b["model_end"], "boundary_source": b["source"],
                "start_snapped": b.get("start_snapped"), "end_snapped": b.get("end_snapped")}
        if keep:
            meta["keep"] = keep
        length = sum(y - x for x, y in keep) if keep else e - a
        standalone = 0 if b["selection"] == "CONTEXT_DEPENDENT" or b.get("context_missing") else 1
        try:
            params = d / "clip-params.json"
            want = {"start": round(a, 3), "end": round(e, 3)}
            if keep:
                want["keep"] = [[round(x, 3), round(y, 3)] for x, y in keep]
            if not clean.exists() or not params.exists() or read_json(params) != want:
                for stale in ("clip-subtitled.mp4", "subtitle.srt", "subtitle.ass"):
                    (d / stale).unlink(missing_ok=True)
                ffmpeg.render_pieces(ctx.media, keep or [[a, e]], clean)
                write_json(params, want)
            _asset(ctx, uid, "clip", "GENERATED", clean, a, e, meta, selection=b["selection"],
                   publication_priority=b["publication_priority"], standalone=standalone)
            try:
                ffmpeg.frame(clean, min(3.0, length / 2), d / "poster.jpg", width=1280)
                _asset(ctx, uid, "poster", "GENERATED", d / "poster.jpg", a, a)
            except ffmpeg.MediaError:
                pass  # a poster failure never fails a good clip
            ok += 1
            if length < 8:
                ctx.flag("ASSET", uid, "CLIP_TOO_SHORT", "REVIEW", f"「{u['title']}」片段只有 {length:.0f} 秒")
        except ffmpeg.MediaError as err:
            failed += 1
            _asset(ctx, uid, "clip", "FAILED", None, a, e, {**meta, "error": str(err)[:500]}, selection=b["selection"],
                   publication_priority=b["publication_priority"], standalone=standalone)
            ctx.flag("ASSET", uid, "CLIP_RENDER_FAILED", "REVIEW", f"「{u['title']}」剪辑失败", str(err)[:300])
    if failed and not ok:
        raise StageWarning(f"all {failed} clips failed")
    return {"clips": ok, "failed": failed}


# ------------------------------------------------------------------ subtitles
def subtitle_stage(ctx: RunContext, only_unit: str | None = None) -> dict:
    tr = _transcript(ctx)
    if not tr:
        raise StageWarning("no transcript; subtitles skipped")
    s = config.get()
    fonts = s.font_dir or Path(__file__).resolve().parents[3] / "assets" / "subtitle"
    units = {u["id"]: u for u in _units(ctx)}
    res = {"SUCCESS": 0, "SUCCESS_WITH_WARNINGS": 0, "FAILED_NON_BLOCKING": 0}
    for c in db.q("SELECT * FROM assets WHERE run_id=? AND kind='clip' AND status='GENERATED'", ctx.id):
        if only_unit and c["unit_id"] != only_unit:
            continue
        u = units[c["unit_id"]]
        clip = Path(c["path"])
        d = clip.parent
        a, e = c["start"], c["end"]
        keep = db.unj(c["meta"], {}).get("keep") or [[a, e]]  # the ranges the clip is made of; it runs on their joined time
        length = sum(y - x for x, y in keep)
        try:
            words = [{**w, "start": fidelity.to_local(w["start"], keep), "end": fidelity.to_local(w["end"], keep)}
                     for w in tr["words"] if fidelity.inside(w["start"], w["end"], keep)]
            segs = [{"text": sg["text"], "start": fidelity.to_local(sg["start"], keep), "end": fidelity.to_local(sg["end"], keep)}
                    for sg in tr["segments"] if any(sg["end"] > x and sg["start"] < y for x, y in keep)]
            if not words:
                raise ValueError("no speech in clip")
            # rebuild segment text from in-range words so lexical content matches exactly
            text_words = "".join(w["word"] for w in words)
            frozen = [{"text": text_words, "start": 0.0, "end": length}] if not segs else \
                [{"text": "".join(w["word"] for w in words if w["start"] >= sg["start"] - 0.05 and w["end"] <= sg["end"] + 0.05),
                  "start": sg["start"], "end": sg["end"]} for sg in segs]
            frozen = [f for f in frozen if f["text"].strip()]
            cues, _, _ = subtitles.segment(frozen, words, u.get("tickers", []), length, u["label"])
            write_text(d / "subtitle.srt", subtitles.to_srt(cues))
            write_text(d / "subtitle.ass", subtitles.to_ass(cues))
            out = d / "clip-subtitled.mp4"
            if not out.exists():
                ffmpeg.burn_subtitles(clip, d / "subtitle.ass", out, fonts)
            warns = subtitles.warnings(cues)
            status = "SUCCESS_WITH_WARNINGS" if warns else "SUCCESS"
            res[status] += 1
            _asset(ctx, c["unit_id"], "clip_subtitled", "WARNING" if warns else "GENERATED", out, a, e,
                   {"subtitle_result": status, "warnings": warns[:20], "cues": len(cues)})
            _asset(ctx, c["unit_id"], "srt", "GENERATED", d / "subtitle.srt", a, e)
            _asset(ctx, c["unit_id"], "ass", "GENERATED", d / "subtitle.ass", a, e)
        except Exception as err:  # noqa: BLE001 — subtitles are best-effort
            res["FAILED_NON_BLOCKING"] += 1
            _asset(ctx, c["unit_id"], "clip_subtitled", "FAILED", None, a, e, {"error": str(err)[:300]})
    return res


# ------------------------------------------------------------------ media qa
def media_qa(ctx: RunContext) -> dict:
    bad = 0
    rows = db.q("SELECT * FROM assets WHERE run_id=? AND kind IN ('clip','clip_subtitled') AND status IN ('GENERATED','WARNING')", ctx.id)
    for r in rows:
        p = Path(r["path"])
        try:
            q = ffmpeg.qa(p)
            pr = ffmpeg.probe(p)
            v = next(s for s in pr["streams"] if s["codec_type"] == "video")
            q.update(profile=v.get("profile"), pix_fmt=v.get("pix_fmt"), moov_first=ffmpeg.moov_first(p))
            problems = [k for k, okv in (("decode", q["ok"]), ("h264", q["video_codec"] == "h264"),
                                         ("yuv420p", q["pix_fmt"] == "yuv420p"), ("aac", q["audio_codec"] == "aac"),
                                         ("faststart", q["moov_first"])) if not okv]
        except Exception as e:  # noqa: BLE001
            q, problems = {"error": str(e)[:300]}, ["probe"]
        meta = db.unj(r["meta"], {}) | {"qa": q, "qa_problems": problems}
        status = r["status"] if not problems else "FAILED"
        db.exec("UPDATE assets SET meta=?, status=? WHERE id=?", db.j(meta), status, r["id"])
        if problems:
            bad += 1
            ctx.flag("ASSET", r["unit_id"], "MEDIA_QA_FAILED", "REVIEW", "视频文件可能无法播放：" + ",".join(problems))
    return {"checked": len(rows), "failed": bad}
