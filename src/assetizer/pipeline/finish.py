"""REVIEW_CHECK → COMPOSE → PACKAGE → CLEANUP."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .. import config, db
from ..contracts import prompts as P
from ..llm import ContractError
from ..providers.agy import AgyProvider, ProviderError
from ..util import fmt_tc, now, read_json, sha256_file, write_json, write_text
from .context import RunContext, StageWarning


def _units(ctx: RunContext) -> list[dict]:
    return ctx.load("machine/knowledge-units.json")["units"]


def _text(words: list[dict], a: float, b: float) -> str:
    return "".join(w["word"] for w in words if w["start"] >= a and w["end"] <= b).strip()


# ------------------------------------------------------------------ review check
def review_check(ctx: RunContext) -> dict:
    tp = ctx.dir / "machine" / "transcript.json"
    if not tp.exists():
        raise StageWarning("no transcript")
    words = read_json(tp)["words"]
    units = {u["id"]: u for u in _units(ctx)}
    items = []
    for c in db.q("SELECT * FROM assets WHERE run_id=? AND kind='clip' AND status='GENERATED' ORDER BY start", ctx.id):
        u = units[c["unit_id"]]
        a, b = c["start"], c["end"]
        items.append({"label": u["label"], "title": u["title"], "required_context": u["required_context"],
                      "claims": [{k: x.get(k) for k in ("kind", "text", "condition", "time_scope")} for x in u.get("claims", [])][:8],
                      "asr_head": _text(words, a, a + 20), "asr_tail": _text(words, b - 20, b),
                      "asr_full": _text(words, a, b)[:1800]})
    if not items:
        return {"items": 0}
    llm = ctx.llm()
    by_label = {u["label"]: u for u in units.values()}
    found = 0
    for bi in range(0, len(items), 8):
        try:
            want = {x["label"] for x in items[bi:bi + 8]}

            def cover(o, want=want):
                if len({c["label"] for c in o["checks"]} & want) < 0.8 * len(want):
                    raise ContractError("MISSING_UNITS", "review check did not cover the batch")

            o = llm.structured(ctx.calls, f"review-check-{bi // 8 + 1:02}", P.check_prompt(items[bi:bi + 8]),
                               P.CHECK_SCHEMA, effort="medium", enforce=True, check=cover)
        except ContractError as e:
            ctx.event(f"review check batch failed: {e}", "warn")
            continue
        for chk in o["checks"]:
            u = by_label.get(chk["label"])
            if not u:
                continue
            for iss in chk["issues"]:
                found += 1
                # fidelity problems need the reviewer; boundary polish notes do not
                sev = iss["severity"] if iss["kind"] in FIDELITY_KINDS else "INFO"
                ctx.flag("UNIT", u["id"], "AI_" + iss["kind"], sev, f"「{u['title']}」{_KIND_ZH.get(iss['kind'], iss['kind'])}",
                         iss.get("note", ""), evidence={"quote": iss.get("evidence")}, origin="AI_CHECK")
    return {"items": len(items), "issues": found}


FIDELITY_KINDS = {"CLAIM_MISMATCH", "ATTRIBUTION_RISK", "OVERSTATEMENT"}
_KIND_ZH = {"HALF_SENTENCE_START": "片段从半句开始", "DANGLING_REFERENCE": "开头指代不明",
            "ENDS_BEFORE_CONCLUSION": "片段在结论前结束", "NEXT_TOPIC_BLEED": "结尾混入下一主题",
            "CLAIM_MISMATCH": "陈述与原话可能不一致", "ATTRIBUTION_RISK": "归属可能有误", "OVERSTATEMENT": "程度可能被加强"}


# ------------------------------------------------------------------ compose
def compose(ctx: RunContext) -> dict:
    units = {u["id"]: u for u in _units(ctx)}
    assets: dict[str, dict[str, list]] = {}
    for a in db.q("SELECT * FROM assets WHERE run_id=? AND status IN ('GENERATED','WARNING') ORDER BY start", ctx.id):
        assets.setdefault(a["unit_id"] or "", {}).setdefault(a["kind"], []).append(a)
    blocks = []
    for s in db.q("SELECT * FROM sections WHERE run_id=? ORDER BY ord", ctx.id):
        uids = db.unj(s["unit_ids"], [])
        entry = {"section_id": s["id"], "kind": s["kind"], "markdown": s["markdown"], "unit_id": uids[0] if uids else None,
                 "video": None, "screenshots": []}
        if uids:
            ua = assets.get(uids[0], {})
            vid = (ua.get("clip_subtitled") or ua.get("clip") or [None])[0]
            clip = (ua.get("clip") or [None])[0]
            if vid and clip and clip["embed"]:
                entry["video"] = {"path": vid["path"], "asset_id": vid["id"], "start": vid["start"], "end": vid["end"],
                                  "subtitled": vid["kind"] == "clip_subtitled", "standalone": bool(clip["standalone"])}
            entry["screenshots"] = [{"path": x["path"], "asset_id": x["id"], "t": x["start"],
                                     "reason": db.unj(x["meta"], {}).get("reason", "")} for x in ua.get("screenshot", [])]
        blocks.append(entry)
    ctx.save("composition.json", {"source": ctx.source["id"], "run": ctx.id, "order": "SOURCE_TIME",
                                  "within_unit": ["video", "screenshots", "text"], "blocks": blocks})
    return {"blocks": len(blocks), "videos": sum(1 for b in blocks if b["video"]),
            "screenshots": sum(len(b["screenshots"]) for b in blocks)}


# ------------------------------------------------------------------ package
def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)  # hard link: zero extra disk
    except OSError:
        shutil.copyfile(src, dst)


def package(ctx: RunContext) -> dict:
    s = config.get()
    root = s.packages_dir / ctx.source["id"]
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    units = {u["id"]: u for u in _units(ctx)}
    # the package article is rebuilt from sections so reviewer edits and renumbering are reflected
    outline = ctx.load("article/outline.json", {})
    parts = [f"# {outline.get('title') or ctx.source.get('title')}"]
    for sec in db.q("SELECT kind, unit_ids, markdown FROM sections WHERE run_id=? ORDER BY ord", ctx.id):
        parts.append(sec["markdown"])
    write_text(root / "article" / "article.md", "\n\n".join(parts))
    counts: dict[str, int] = {}
    for clip in db.q("SELECT * FROM assets WHERE run_id=? AND kind='clip' AND status='GENERATED'", ctx.id):
        u = units[clip["unit_id"]]
        tier = "context-dependent" if not clip["standalone"] else (clip["publication_priority"] or "P3")
        tier = "archive" if tier == "ARCHIVE" else tier
        counts[tier] = counts.get(tier, 0) + 1
        d = root / tier / u["label"]
        _link(Path(clip["path"]), d / "clip-clean.mp4")
        for a in db.q("SELECT * FROM assets WHERE run_id=? AND unit_id=? AND status IN ('GENERATED','WARNING')", ctx.id, u["id"]):
            p = Path(a["path"]) if a["path"] else None
            if not p or not p.exists():
                continue
            name = {"clip_subtitled": "clip-subtitled.mp4", "srt": "subtitle.srt", "ass": "subtitle.ass",
                    "poster": "poster.jpg"}.get(a["kind"])
            if a["kind"] == "screenshot":
                _link(p, d / "screenshots" / p.name)
            elif name:
                _link(p, d / name)
        write_json(d / "knowledge-unit.json", u)
        write_text(d / "publish.md", f"# {u['title']}\n\n{u['summary']}\n\n来源：{ctx.source.get('channel_title') or ''} · "
                                     f"{ctx.source.get('url') or ''} · {fmt_tc(clip['start'])}–{fmt_tc(clip['end'])}\n\n"
                                     f"（观点归原作者；AI 整理，可能存在误差。）\n")
    write_text(root / "README.md",
               f"# {ctx.source.get('title')}\n\n频道：{ctx.source.get('channel_title')}\n来源：{ctx.source.get('url')}\n\n"
               "- `article/article.md`：完整学习文章\n- `P1/ P2/ P3/`：可独立发布的片段（按发布优先级）\n"
               "- `context-dependent/`：需要前文才能理解的片段，不建议单独发布\n"
               "- 每个片段：`clip-clean.mp4`（无字幕）、`clip-subtitled.mp4`（烧录字幕，实验）、`subtitle.srt/.ass`、"
               "`screenshots/`、`publish.md`（标题与描述草稿）、`knowledge-unit.json`\n")
    manifest = {"source": ctx.source["id"], "run": ctx.id, "generated_at": now(),
                "files": {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}}
    write_json(root / "manifest.json", manifest)
    db.exec("UPDATE sources SET package_path=?, updated_at=? WHERE id=?", str(root), now(), ctx.source["id"])
    return {"files": len(manifest["files"]), **counts}


# ------------------------------------------------------------------ cleanup
def cleanup(ctx: RunContext) -> dict:
    freed = 0
    work = ctx.dir / "work"
    if work.exists():
        freed += sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
        shutil.rmtree(work, ignore_errors=True)
    freed += sweep_agy_state(max_age_s=3600)
    return {"freed_mb": round(freed / 1e6)}


def sweep_agy_state(max_age_s: int = 3600) -> int:
    """Delete leftover agy state for conversations *this app* created (recorded in model_calls).
    Never touches the user's own interactive agy sessions."""
    from ..providers.agy import AgyProvider
    ids = [r["conversation_id"] for r in db.q("SELECT DISTINCT conversation_id FROM model_calls WHERE conversation_id IS NOT NULL")]
    p = AgyProvider()
    return sum(p.purge_conversation(c) for c in ids)
