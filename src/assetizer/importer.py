"""Import a frozen Run-014-style directory as a completed run.

Gives the UI and the Notion renderer real data, and doubles as the reference
fixture for the KU v2 model (HH:MM:SS → seconds, P1/P2/P3 → learning priority
+ clip publication priority, issues.json → Flags).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import config, db, seed
from .topics import backup_rules
from .pipeline import finish
from .pipeline.context import RunContext
from .pipeline.runner import STAGE_NAMES
from .util import now, parse_tc, read_json, sha256_file, ulid, write_json

LP = {"P1": 3, "P2": 2, "P3": 1, "ARCHIVE_ONLY": 1}


def _link(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    os.symlink(src.resolve(), dst)
    return dst


def run14(root: Path) -> str:
    root = Path(root)
    seed.run()
    ref = read_json(root / "source/source-reference.json")
    sm = ref["source_metadata"]
    vid = sm["id"]
    s = config.get()
    run_id = f"{vid}-r0"
    if db.one("SELECT id FROM runs WHERE id=?", run_id):
        return run_id
    media = root / "source/source.mkv"
    with db.tx():
        db.exec("""INSERT OR REPLACE INTO sources(id, url, channel_id, youtube_channel_id, channel_title, title, published_at, duration, info,
                   media_path, media_sha256, media_bytes, media_height, media_state, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                vid, ref["original_media_reference"]["url"], "trader-tom", sm["channel_id"], sm["channel"], sm["title"],
                sm["upload_date"], 3503.752, db.j(sm), str(media) if media.exists() else None,
                ref["original_media_reference"]["sha256"], ref["original_media_reference"]["bytes"], 1080,
                "CACHED" if media.exists() else "PURGED", now(), now())
        backup_rules.track(vid)
    pb = db.one("SELECT id FROM playbooks WHERE channel_id='trader-tom' AND status='ACTIVE'")
    db.exec("""INSERT INTO runs(id, source_id, kind, playbook_id, production, pins, created_at, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?)""", run_id, vid, "IMPORT", pb["id"], "COMPLETE_PARTIAL",
            db.j({"origin": "Run-014 Production Acceptance", "model": "gemini-3.8-flash-high", "agy": "1.2.0"}),
            "2026-09-10T07:08:08+00:00", "2026-09-10T07:08:08+00:00", "2026-09-10T08:26:48+00:00")
    for i, n in enumerate(STAGE_NAMES):
        db.exec("INSERT INTO stages(run_id, name, ord, status) VALUES (?,?,?,?)", run_id, n, i,
                "WARN" if n in ("MERGE", "KNOWLEDGE", "BOUNDARY") else "DONE")
    ctx = RunContext(run=db.one("SELECT * FROM runs WHERE id=?", run_id), source=db.one("SELECT * FROM sources WHERE id=?", vid))
    rd = ctx.dir

    # speakers
    for sp in read_json(root / "machine/source-speaker-registry.json")["speakers"]:
        db.exec("INSERT INTO speakers VALUES (?,?,?,?,?,?,?)", vid, run_id, "tom", sp.get("name"), sp.get("role"), "HIGH", "Run-014 registry")

    # units
    old = read_json(root / "machine/knowledge-units.json")["units"]
    sel = {x["unit_id"]: x for x in read_json(root / "machine/clip-selection.json")["selections"]}
    label2id, units = {}, []
    for i, u in enumerate(old):
        uid = ulid()
        label2id[u["unit_id"]] = uid
        st, en = min(parse_tc(u["source_start"]), 3503.752), min(parse_tc(u["source_end"]), 3503.752)
        ku = {
            "id": uid, "label": u["unit_id"], "title": u["title"], "start": st, "end": en,
            "segments": [{"start": st, "end": en}], "provenance": {"chunk_topic_refs": u.get("source_segments", [])},
            "speaker_key": "tom", "tickers": u.get("ticker", []), "knowledge_type": u["knowledge_type"],
            "learning_priority": LP.get(u["priority"], 1), "depends_on": [], "clip_candidate": u["clip_candidate"],
            "why": "", "summary": u["summary"], "teacher_reasoning": u["teacher_reasoning"],
            "visual_evidence": u.get("visual_evidence", []),
            "screenshot_anchors": [{**a, "t": parse_tc(a["timestamp"])} for a in u.get("screenshot_anchors", [])],
            "required_context": u.get("required_context", ""), "claims": [],
            "facets": {"higher_timeframe_setup": u.get("higher_timeframe_setup"),
                       "lower_timeframe_trigger": u.get("lower_timeframe_trigger"),
                       "observation_tasks": u.get("observation_tasks", [])},
            "uncertainties": u.get("uncertainties", []), "legacy_priority": u["priority"],
        }
        units.append(ku)
        db.exec("""INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, learning_priority, knowledge_type, data)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""", uid, run_id, vid, ku["label"], i, ku["title"], st, en,
                ku["learning_priority"], ku["knowledge_type"], db.j(ku))
    units.sort(key=lambda u: u["start"])
    write_json(rd / "machine/knowledge-units.json", {"units": units})
    write_json(rd / "machine/selection.json", {"selections": [
        {"label": k, "unit_id": label2id[k], "verdict": v["verdict"], "reason": v["reason"],
         "publication_priority": next(u["legacy_priority"] for u in units if u["label"] == k).replace("ARCHIVE_ONLY", "ARCHIVE")}
        for k, v in sel.items() if k in label2id]})

    # article sections
    article = (root / "article/full-article.md").read_text()
    parts = re.split(r"<!--\s*KU:([A-Za-z0-9_-]+)\s*-->", article)
    db.exec("INSERT INTO sections(id, run_id, ord, kind, unit_ids, markdown) VALUES (?,?,?,?,?,?)",
            ulid(), run_id, 0, "INTRO", "[]", parts[0].strip())
    for k in range(1, len(parts), 2):
        db.exec("INSERT INTO sections(id, run_id, ord, kind, unit_ids, markdown) VALUES (?,?,?,?,?,?)",
                ulid(), run_id, k // 2 + 1, "UNIT", db.j([label2id[parts[k]]]), parts[k + 1].strip())
    (rd / "article").mkdir(exist_ok=True)
    (rd / "article/article.md").write_text(article)

    # assets
    def asset(unit_id, kind, status, path, start=None, end=None, meta=None, **cols):
        keys = ["run_id", "source_id", "unit_id", "kind", "status", "path", "sha256", "bytes", "start", "end", "meta", "created_at", *cols]
        vals = [run_id, vid, unit_id, kind, status, str(path) if path else None, None,
                path.stat().st_size if path and path.exists() else None, start, end, db.j(meta or {}), now(), *cols.values()]
        db.exec(f"INSERT INTO assets({','.join(keys)}) VALUES ({','.join('?' * len(vals))})", *vals)

    for u in units:
        sd = root / "assets/screenshots" / u["label"]
        for jp in sorted(sd.glob("*.jpg")):
            meta = read_json(jp.with_suffix(".json"))["anchor"]
            p = _link(jp, rd / "assets/screenshots" / u["label"] / jp.name)
            asset(u["id"], "screenshot", "GENERATED", p, parse_tc(meta["timestamp"]), parse_tc(meta["timestamp"]),
                  {"reason": meta.get("reason"), "timeframe": meta.get("timeframe"), "visible_structure": meta.get("visible_structure")})
        td = root / "topic-assets" / u["label"]
        if (td / "clip-clean.mp4").exists():
            m = read_json(td / "metadata.json")
            a, b = m["final_start_seconds"], m["final_end_seconds"]
            d = rd / "assets/clips" / u["label"]
            clean = _link(td / "clip-clean.mp4", d / "clip-clean.mp4")
            sv = sel.get(u["label"], {}).get("verdict", "SELECT")
            asset(u["id"], "clip", "GENERATED", clean, a, b, {"boundary_source": "model+codex_sanity"}, selection=sv,
                  publication_priority=u["legacy_priority"].replace("ARCHIVE_ONLY", "ARCHIVE"),
                  standalone=0 if sv == "CONTEXT_DEPENDENT" else 1)
            poster = td / "clip-clean.mp4-first.jpg"
            if poster.exists():
                asset(u["id"], "poster", "GENERATED", _link(poster, d / "poster.jpg"), a, a)
            if (td / "clip-subtitled.mp4").exists():
                w = m.get("subtitle_warnings", [])
                asset(u["id"], "clip_subtitled", "WARNING" if w else "GENERATED", _link(td / "clip-subtitled.mp4", d / "clip-subtitled.mp4"),
                      a, b, {"subtitle_result": m.get("subtitle_result"), "warnings": w[:20]})
            for nm, kind in (("subtitle.srt", "srt"), ("subtitle.ass", "ass")):
                if (td / nm).exists():
                    asset(u["id"], kind, "GENERATED", _link(td / nm, d / nm), a, b)
            words = td / "words.json"
            if words.exists():
                _link(words, d / "words.json")
        elif u["label"] in sel and sel[u["label"]]["verdict"] == "SELECT":
            asset(u["id"], "clip", "FAILED", None, u["start"], u["end"], {"error": "Boundary contract failure (Run-014 I008)"},
                  selection=sel[u["label"]]["verdict"], publication_priority=u["legacy_priority"])

    # flags
    for iss in read_json(root / "evaluation/issues.json"):
        uid = label2id.get(iss.get("unit_id") or "")
        sev = "REVIEW" if "REVIEW" in iss.get("severity", "") or iss.get("classification", "").startswith(("ATTRIBUTION", "CONTEXT")) else "INFO"
        db.flag(vid, run_id, "UNIT" if uid else "RUN", uid, iss["classification"], sev,
                iss.get("description") or iss["classification"], iss.get("recovery", ""),
                {"source_range": iss.get("source_range"), "stage": iss.get("stage")}, "PROVIDER" if "PROVIDER" in iss["classification"] else "MACHINE_QA")
    unit14 = label2id.get("unit_14_mega_cap_gap_reactions_and_discipline")
    if unit14:
        db.flag(vid, run_id, "UNIT", unit14, "BOUNDARY_FALLBACK", "REVIEW", "「大盘股跳空反应与纪律」边界结构化输出失败，未生成视频",
                "Run-014 未做兜底；新流水线会改用确定性剪辑。", None, "PROVIDER")

    # model telemetry
    for inv in root.glob("calls/**/invocation.json"):
        r = read_json(inv)
        u = r.get("result", {}).get("usage", {})
        db.exec("""INSERT INTO model_calls(run_id, stage, attempt, conversation_id, model, agy_version, status, wall_seconds,
                   input_tokens, output_tokens, thinking_tokens, peak_rss_kib, view_files, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                run_id, inv.parent.parent.name, inv.parent.name, r.get("session_id"), r.get("actual_model"), "1.2.0",
                r.get("result", {}).get("status"), r.get("wall_seconds"), u.get("input_tokens"), u.get("output_tokens"),
                u.get("thinking_tokens"), r.get("peak_rss_kib"), r.get("view_file_count"), r.get("started_utc") or now())

    finish.compose(ctx)
    from .pipeline.runner import _summary
    db.exec("UPDATE runs SET summary=? WHERE id=?", db.j(_summary(run_id)), run_id)
    np = read_json(root / "notion/page.json") if (root / "notion/page.json").exists() else {}
    from .notion.mapping import run_headline
    db.exec("UPDATE sources SET current_run_id=?, headline=?, delivery=?, notion_url=?, review='UNREVIEWED' WHERE id=?",
            run_id, run_headline(run_id), "DELIVERED" if np else "NOT_STARTED", np.get("url"), vid)
    db.event("import", "导入 Run-014 生产验收结果", run_id)
    return run_id
