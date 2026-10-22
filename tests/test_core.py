import json

import pytest

from assetizer.contracts import prompts as P
from assetizer.llm import ContractError, extract_json, validate
from assetizer.media import subtitles
from assetizer.notion import blocks as B
from assetizer.pipeline.media_stages import snap
from assetizer.providers.agy import classify_error, gemini_schema
from assetizer.util import fmt_tc, parse_tc
from assetizer.youtube import classify, video_id


def test_timecodes():
    assert parse_tc("01:02:03") == 3723
    assert parse_tc("12:30") == 750
    assert parse_tc("0:05.5") == 5.5
    assert parse_tc(42) == 42.0
    assert parse_tc("90") == 90.0
    assert fmt_tc(3723.9) == "01:02:03"
    with pytest.raises(ValueError):
        parse_tc("soon")


def test_extract_json_tolerates_prose_and_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ContractError):
        extract_json("no json here")


def test_schemas_down_convert_for_gemini():
    for name in ["UNDERSTAND_SCHEMA", "MERGE_SCHEMA", "SKELETON_SCHEMA", "OUTLINE_SCHEMA", "SECTIONS_SCHEMA",
                 "SELECT_SCHEMA", "BOUNDARY_SCHEMA", "CHECK_SCHEMA"]:
        s = json.dumps(gemini_schema(getattr(P, name)))
        assert "null" not in s and '"pattern"' not in s


def test_validate_rejects_missing_fields():
    with pytest.raises(ContractError):
        validate({"start": "00:10"}, P.BOUNDARY_SCHEMA)


def test_error_classification():
    assert classify_error("INVALID_ARGUMENT ... function_declarations[0] schema") == "PROVIDER_SCHEMA_REJECTED"
    assert classify_error("You exceeded your current quota") == "PROVIDER_QUOTA"
    assert classify_error("HTTP 503 unavailable") == "PROVIDER_TRANSIENT"
    assert classify_error("model output token limit reached") == "PROVIDER_OUTPUT_LIMIT"
    assert classify("ERROR: Sign in to confirm you’re not a bot") == "BOT_CHALLENGE"
    assert classify("HTTP Error 429: Too Many Requests") == "RATE_LIMITED"


def test_video_id():
    assert video_id("https://www.youtube.com/watch?v=1MuKAUgL4r4&t=3") == "1MuKAUgL4r4"
    assert video_id("https://youtu.be/1MuKAUgL4r4") == "1MuKAUgL4r4"
    assert video_id("https://example.com") is None


def _words(spec):
    """spec: list of (word, start, end)."""
    return [{"word": w, "start": a, "end": b} for w, a, b in spec]


def test_snap_prefers_earlier_start_and_later_end():
    # sentence break (gap 0.9 s) at ~9.1 s and ~31 s
    words = _words([("然后", 7.0, 7.6), ("看。", 7.6, 8.2), ("这个", 9.1, 9.5), ("缺口", 9.5, 10.0)] +
                   [(f"字{i}", 10.0 + i * 0.5, 10.4 + i * 0.5) for i in range(40)] +
                   [("结束。", 30.0, 30.5), ("下面", 31.4, 31.8), ("另外", 31.8, 32.2)])
    r = snap(10.0, 29.8, words, set(), None, 100.0)
    assert r["start_snapped"] and r["start"] < 10.0
    assert r["end_snapped"] and r["end"] > 30.5


def test_snap_respects_next_unit():
    words = _words([("a。", 0, 1), ("b", 2.0, 2.5), ("c。", 2.5, 3.0), ("d", 9.0, 9.5), ("e。", 9.5, 10.0), ("f", 11.0, 11.5)])
    r = snap(2.0, 9.6, words, set(), next_start=9.7, dur=20.0)
    assert r["end"] <= 9.7 + 0.31


def test_subtitle_segmentation_preserves_lexical_content():
    words = _words([("我们", 0.0, 0.4), ("看", 0.4, 0.6), ("TSLA", 0.6, 1.2), ("跳空", 1.3, 1.8), ("突破", 1.8, 2.3),
                    ("了", 2.3, 2.5), ("缺口", 3.4, 3.9), ("然后", 3.9, 4.3), ("回踩", 4.3, 4.8), ("10EMA", 4.8, 5.5)])
    frozen = [{"text": "".join(w["word"] for w in words), "start": 0, "end": 5.6}]
    cues, _, _ = subtitles.segment(frozen, words, ["TSLA"], 5.6, "t")
    assert subtitles.canon("".join(c["text"] for c in cues)) == subtitles.canon(frozen[0]["text"])
    assert "Dialogue:" in subtitles.to_ass(cues)
    assert "-->" in subtitles.to_srt(cues)


def test_markdown_blocks():
    bl = B.markdown("# 标题\n\n段落 **加粗** 与 `code`\n\n- 一\n- 二\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n---\n> 引用")
    kinds = [b["type"] for b in bl]
    assert kinds == ["heading_1", "paragraph", "bulleted_list_item", "bulleted_list_item", "table", "divider", "quote"]
    para = bl[1]["paragraph"]["rich_text"]
    assert any(x.get("annotations", {}).get("bold") for x in para)
    assert all(len(x["text"]["content"]) <= 1900 for x in B.rich("长" * 5000))


def test_compiler_clause_split_and_provenance():
    from assetizer.compiler import provenance_ok, split_clauses
    raw = "# 一、目标\n\n我想学习**读图**。不要硬凑数量；\n\n* “这个缺口”\n* 这里在盘整\n\n---\n\n1. 第一条要求。第二句。"
    cl = split_clauses(raw)
    texts = [c["text"] for c in cl]
    assert texts[0] == "我想学习读图。" and "不要硬凑数量；" in texts
    assert "“这个缺口”" in texts and "第二句。" in texts
    assert all(c["section"] == "一、目标" for c in cl)
    assert provenance_ok("我想学习读图", raw) and not provenance_ok("编造的要求", raw)


def test_effort_maps_to_model_variant():
    from assetizer.providers.agy import model_for
    assert model_for("gemini-3.8-flash-high", "medium") == "gemini-3.8-flash-medium"
    assert model_for("gemini-3.8-flash-high", None) == "gemini-3.8-flash-high"
    assert model_for("claude-opus-5-5-high", "low") == "claude-opus-5-5-low"


def test_benign_tools_only_readonly_listings():
    from assetizer.providers.agy import _benign
    assert _benign("run_command", {"CommandLine": "ls -la .."})
    assert _benign("list_dir", {"DirectoryPath": "."})
    # agy 1.4: several listings in one command, one per line (what 4_IxK7QS1tc-r4 did after reading the video)
    assert _benign("run_command", {"CommandLine": "ls -la /data/runs/r4/calls/understand-CH01/\nls -la /data/runs/r4/calls/understand-CH01/attempt-1/ws/\n"})
    assert _benign("run_command", {"CommandLine": "ls -la a b\npwd"})
    assert not _benign("run_command", {"CommandLine": "ls -la a\ncat prompt.md"})
    assert not _benign("run_command", {"CommandLine": "ls; rm -rf /"})
    assert not _benign("run_command", {"CommandLine": "ls -la a && curl http://x"})
    assert not _benign("run_command", {"CommandLine": "ls $(cat x)"})
    assert not _benign("run_command", {"CommandLine": "ls > out.txt"})
    assert not _benign("run_command", {"CommandLine": "ffmpeg -i x.mp4 out.wav"})
    assert not _benign("run_command", {"CommandLine": ""})
    assert not _benign("view_file", {"AbsolutePath": "/etc/passwd"})


def test_dedupe_clips_keeps_higher_priority():
    from assetizer.pipeline.media_stages import dedupe_clips
    units = {"a": {"learning_priority": 2}, "b": {"learning_priority": 3}, "c": {"learning_priority": 2}}
    bnd = {"a": {"start": 448, "end": 702}, "b": {"start": 449, "end": 701}, "c": {"start": 600, "end": 900}}
    assert dedupe_clips(bnd, units) == {"a": "b"}


def test_safety_block_detection():
    from assetizer.llm import is_safety_block
    assert is_safety_block("This request was blocked by Gemini's filters. They can occasionally trigger by mistake")
    assert not is_safety_block('{"topics": []}')


def test_network_errors_are_transient():
    assert classify_error('request failed: read tcp 1.2.3.4:1->5.6.7.8:443: read: connection reset by peer') == "PROVIDER_TRANSIENT"


def test_public_host_requires_access_jwt(tmp_path, monkeypatch):
    import dataclasses
    from fastapi.testclient import TestClient
    from assetizer import config
    from assetizer.web import app as web
    config.override(dataclasses.replace(config.load(), home=tmp_path))
    monkeypatch.setattr(web, "PUBLIC_HOST", "kan.example.com")
    monkeypatch.setattr(web, "ACCESS_TEAM", "example-team")
    monkeypatch.setattr(web, "ACCESS_AUD", "x" * 64)
    c = TestClient(web.app)
    assert c.get("/api/overview", headers={"host": "kan.example.com"}).status_code == 403
    assert c.get("/api/overview", headers={"host": "kan.example.com", "cf-access-jwt-assertion": "forged"}).status_code == 403
    assert c.get("/api/overview", headers={"host": "evil.example"}).status_code == 403
    assert c.get("/api/overview", headers={"host": "127.0.0.1:8770"}).status_code == 200


# ---------------------------------------------------------------- design §R2: tuning → locked, 校注, no per-issue review
@pytest.fixture
def home(tmp_path):
    import dataclasses
    from assetizer import config, db
    config.override(dataclasses.replace(config.load(), home=tmp_path))
    db._local.conn = None
    yield tmp_path
    db._local.conn = None


def test_migration_locks_tuned_channels_and_dates_deliveries(home):
    from assetizer import db
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, created_at, updated_at) VALUES ('tom','Tom','TRACKED',?,?), ('wang','Wang','TRACKED',?,?)", t, t, t, t)
    db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('tom',1,'ACTIVE','{}',?), ('wang',1,'NEEDS_CONFIRMATION','{}',?)", t, t)
    db.exec("INSERT INTO sources(id, delivery, notion_page_id, created_at, updated_at) VALUES ('a','DELIVERED','pg',?,'2026-10-01T00:00:00+00:00'), ('b','NOT_STARTED',NULL,?,?)", t, t, t)
    db.set_setting("notion_database_id", "00000006-0000-8000-a000-000000000006")
    # a database from before design §R2
    db.exec("ALTER TABLE channels DROP COLUMN stage")
    db.exec("ALTER TABLE sources DROP COLUMN delivered_at")
    db.exec("ALTER TABLE sources DROP COLUMN notion_database_id")
    db._local.conn = None
    assert {r["id"]: r["stage"] for r in db.q("SELECT id, stage FROM channels")} == {"tom": "LOCKED", "wang": "TUNING"}
    assert {r["id"]: r["delivered_at"] for r in db.q("SELECT id, delivered_at FROM sources")} == {"a": "2026-10-01T00:00:00+00:00", "b": None}
    assert {r["id"]: r["notion_database_id"] for r in db.q("SELECT id, notion_database_id FROM sources")} == \
        {"a": "00000006-0000-8000-a000-000000000006", "b": None}


def test_tuning_samples_wait_for_lock(home, monkeypatch):
    from assetizer import db, delivery
    from assetizer.util import now
    t = now()
    db.set_setting("notion_database_id", "x")
    db.exec("INSERT INTO channels(id, name, mode, created_at, updated_at) VALUES ('wang','Wang','TRACKED',?,?)", t, t)
    db.exec("INSERT INTO sources(id, channel_id, current_run_id, created_at, updated_at) VALUES ('s1','wang','r1',?,?)", t, t)
    picked = []
    monkeypatch.setattr(delivery, "client", lambda: object())
    monkeypatch.setattr(delivery, "deliver", lambda sid: picked.append(sid) or {"status": "DELIVERED"})
    assert delivery.deliver_pending() is False and picked == []
    db.exec("UPDATE channels SET stage='LOCKED'")
    assert delivery.deliver_pending() is True and picked == ["s1"]


def test_notes_block_lists_doubts_in_source_order(home):
    from assetizer import db, delivery
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('s',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('r','s','COMPLETE',?)", t)
    for uid, title, start in [("u1", "缺口回踩", 95.0), ("u2", "减仓计划", 30.0)]:
        db.exec("INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, data) VALUES (?,'r','s',?,0,?,?,?,'{}')",
                uid, uid, title, start, start + 60)

    def flag(uid, sev, title, body="", ev=None):
        db.exec("""INSERT INTO flags(source_id, run_id, target_kind, target_id, kind, severity, title, body, evidence, origin, created_at)
                   VALUES ('s','r','UNIT',?,'AI_X',?,?,?,?,'AI_CHECK',?)""", uid, sev, title, body, json.dumps(ev or {}), t)
    flag("u1", "REVIEW", "「缺口回踩」程度可能被加强", "文中写成建议买入", {"quote": "这个位置可以关注"})
    flag("u2", "REVIEW", "「减仓计划」归属可能有误")
    flag("u1", "INFO", "「缺口回踩」片段从半句开始")
    assert [B.plain(b) for b in delivery._notes("r")] == [
        "〔00:00:30 减仓计划〕归属可能有误",
        "〔00:01:35 缺口回踩〕程度可能被加强。文中写成建议买入 原话：「这个位置可以关注」",
    ]


def test_lock_on_confirm_enables_intake(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, created_at, updated_at) VALUES ('wang','Wang','UCx','TRACKED',?,?)", t, t)
    pid = db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('wang',1,'NEEDS_CONFIRMATION','{}',?)", t)
    c = TestClient(web.app)
    assert c.post(f"/api/playbooks/{pid}/confirm", json={}, headers={"host": "127.0.0.1"}).status_code == 200
    assert db.one("SELECT stage FROM channels WHERE id='wang'")["stage"] == "LOCKED"
    assert db.setting("ingest:wang")["enabled"] is True


# ---------------------------------------------------------------- Notion target per channel (notion/mapping.py)
LIB = {"properties": {
    "Name": {"id": "title", "type": "title", "title": {}},
    "内容所属": {"id": "own", "type": "select", "select": {"options": [{"name": "@交易员汤姆"}, {"name": "王海滨"}]}},
    "会员/公开": {"id": "acc", "type": "select", "select": {"options": [{"name": n} for n in ("公开直播", "会员直播", "公开视频", "会员视频")]}},
    "内容发布日期": {"id": "date", "type": "date", "date": {}},
    "Re-上级 项目": {"id": "up", "type": "relation", "relation": {"database_id": "lib"}},
    "AI 摘要": {"id": "sum", "type": "rich_text", "rich_text": {}},
    "收录日期": {"id": "ct", "type": "created_time", "created_time": {}},
    "视频链接": {"id": "vl", "type": "url", "url": {}},
}}


def test_preset_maps_by_name_and_follows_renames():
    from assetizer.notion import mapping as M
    schema = M.schema_of(LIB)
    cfg = M.preset("trader-tom", "lib", schema, "https://app.notion.com/p/0000000500008000a000000000000005")
    assert cfg["properties"]["own"] == {"name": "内容所属", "type": "select", "source": "fixed", "value": "@交易员汤姆"}
    assert cfg["properties"]["up"]["value"] == ["00000005-0000-8000-a000-000000000005"]
    assert cfg["template"] == "reading-guide" and M.problems(cfg, schema) == {}
    # renamed in Notion: the id is unchanged, so the mapping still holds
    renamed = {**schema, "own": {**schema["own"], "name": "作者"}}
    assert M.problems(cfg, renamed) == {}


def test_mapping_problems_pause_instead_of_writing_wrong():
    from assetizer.notion import mapping as M
    schema = M.schema_of(LIB)
    cfg = M.preset("trader-tom", "lib", schema, "0000000500008000a000000000000005")
    gone = {k: v for k, v in schema.items() if k != "own"}
    assert "已没有「内容所属」" in M.problems(cfg, gone)["own"]
    retyped = {**schema, "date": {"name": "内容发布日期", "type": "rich_text"}}
    assert "类型" in M.problems(cfg, retyped)["date"]
    no_option = {**schema, "own": {**schema["own"], "options": ["王海滨"]}}
    assert "没有选项「@交易员汤姆」" in M.problems(cfg, no_option)["own"]
    assert "已不存在" in M.problems(cfg, schema, page_ok=lambda pid: False)["up"]
    bad_tpl = {**cfg, "properties": {**cfg["properties"], "title": {**cfg["properties"]["title"], "value": "{nope}"}}}
    assert "nope" in M.problems(bad_tpl, schema)["title"]
    assert "标题属性必须配置" in M.problems({"properties": {}}, schema)["title"]


def test_properties_payload_for_tom():
    from assetizer.notion import mapping as M
    schema = M.schema_of(LIB)
    cfg = M.preset("trader-tom", "lib", schema, "0000000500008000a000000000000005")
    cfg["properties"]["sum"] = {"name": "AI 摘要", "type": "rich_text", "source": "fixed", "value": "x"}
    ctx = {"channel": "交易员汤姆", "date": "2026-09-08", "date8": "20260908", "datetime": "2026-09-08T12:25+00:00",
           "headline": "MRNA双缺口地量回踩", "video_title": "t", "url": "u", "access": "public_video", "run_id": "r"}
    p = M.properties(cfg, schema, ctx)
    assert p["title"]["title"][0]["text"]["content"] == "交易员汤姆-20260908-MRNA双缺口地量回踩"
    assert p["acc"] == {"select": {"name": "公开视频"}}
    assert p["date"] == {"date": {"start": "2026-09-08"}}
    assert p["up"] == {"relation": [{"id": "00000005-0000-8000-a000-000000000005"}]}
    assert "ct" not in p


def test_access_kind():
    from assetizer.notion.mapping import access_kind
    assert access_kind({"live_status": "not_live"}) == "public_video"
    assert access_kind({"live_status": "was_live"}) == "public_live"
    assert access_kind({"live_status": "was_live", "availability": "subscriber_only"}) == "member_live"
    assert access_kind({}) == "public_video"


def _run_fixture(home):
    from assetizer import config, db
    from assetizer.util import now, write_json
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('trader-tom','交易员汤姆','TRACKED','LOCKED',?,?)", t, t)
    db.exec("""INSERT INTO sources(id, url, channel_id, title, headline, published_at, info, created_at, updated_at)
               VALUES ('v','https://www.youtube.com/watch?v=v','trader-tom','09/08 技术分析','题眼','20260908','{}',?,?)""", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, summary, created_at) VALUES ('r','v','COMPLETE','{}',?)", t)
    db.exec("""INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, learning_priority, data)
               VALUES ('u1','r','v','a',0,'MRNA双缺口回踩',60,120,3,?)""", json.dumps({"tickers": ["MRNA"], "why": "复合支撑"}))
    rd = config.get().runs_dir / "r"
    shot = rd / "s.jpg"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"x")
    write_json(rd / "composition.json", {"blocks": [
        {"section_id": "i", "kind": "INTRO", "markdown": "本期导语。\n\n### 学习导航\n\n| 单元 | 看点 |\n|---|---|\n| a | b |", "unit_id": None, "video": None, "screenshots": []},
        {"section_id": "s1", "kind": "UNIT", "unit_id": "u1", "video": None,
         "screenshots": [{"path": str(shot), "t": 75, "reason": "缺口"}],
         "markdown": "## 01. MRNA双缺口回踩\n\n### Ticker\nMRNA\n\n### 推荐截图位置\n01:15\n\n### 老师在讲什么？\n回踩。"}]})
    write_json(rd / "machine" / "transcript.json", {"segments": [{"start": 50, "end": 70, "text": "这个缺口"}, {"start": 200, "end": 210, "text": "无关"}], "words": []})


def test_reading_guide_puts_screenshots_under_their_field(home):
    from assetizer import templates
    _run_fixture(home)
    from assetizer import db
    plan = templates.reading_guide(db.one("SELECT * FROM sources"), "r", {"heading": "{channel} - {date} 美股市场复盘与读图学习导航"})
    keys = [it["key"] for it in plan]
    text = [B.plain(it["block"]) if "block" in it else "[img]" for it in plan]
    assert text[1] == "交易员汤姆 - 2026-09-08 美股市场复盘与读图学习导航"
    assert text[2].startswith("复盘背景概述：\n本期导语") and "单元" not in text[2]
    i = text.index("推荐截图位置")
    assert text[i + 1] == "01:15" and text[i + 2] == "[img]"
    assert "二、优先学习清单（Top 推荐精选）" in text and len(keys) == len(set(keys))
    assert sum(1 for it in plan if it.get("block", {}).get("type") == "table") == 1  # built from units: no closing list


def test_reading_guide_uses_the_closing_list_when_present(home):
    from assetizer import config, db, templates
    from assetizer.util import read_json, write_json
    _run_fixture(home)
    p = config.get().runs_dir / "r" / "composition.json"
    comp = read_json(p)
    comp["blocks"].append({"section_id": "o", "kind": "OUTRO", "markdown": "### 优先学习清单\n\n| 优先级 | 单元 |\n|---|---|\n| ★★★ | a |",
                           "unit_id": None, "video": None, "screenshots": []})
    write_json(p, comp)
    plan = templates.reading_guide(db.one("SELECT * FROM sources"), "r", {})
    text = [B.plain(it["block"]) if "block" in it else "" for it in plan]
    tables = [it for it in plan if it.get("block", {}).get("type") == "table"]
    assert text.count("二、优先学习清单（Top 推荐精选）") == 1 and len(tables) == 1
    assert "三、本期可带走的要点" not in text


def test_chapters_quotes_transcript_within_span(home):
    from assetizer import db, templates
    _run_fixture(home)
    plan = templates.chapters(db.one("SELECT * FROM sources"), "r", {})
    text = [B.plain(it["block"]) if "block" in it else "[img]" for it in plan]
    assert plan[0]["block"]["type"] == "embed" and "视频发布于2026年09月08日" in text
    assert "第一章：MRNA双缺口回踩" in text
    assert "[00:01:00] 这个缺口" in text and not any("无关" in t for t in text)


def test_broken_mapping_blocks_delivery_with_alert(home, monkeypatch):
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    _run_fixture(home)
    db.exec("UPDATE sources SET current_run_id='r'")
    cfg = M.preset("trader-tom", "lib", M.schema_of(LIB), "0000000500008000a000000000000005")
    M.save("trader-tom", cfg)
    renamed_option = json.loads(json.dumps(LIB))
    renamed_option["properties"]["内容所属"]["select"]["options"] = [{"name": "Tom"}]
    calls = []

    class Fake:
        def _req(self, method, path, **kw):
            calls.append((method, path))
            return renamed_option

        def page(self, pid):
            return {"archived": False}
    monkeypatch.setattr(delivery, "client", lambda: Fake())
    assert delivery.deliver("v")["status"] == "BLOCKED"
    s = db.one("SELECT delivery, delivery_reason FROM sources")
    assert s["delivery"] == "BLOCKED" and s["delivery_reason"].startswith("NOTION_MAPPING")
    assert db.one("SELECT title FROM alerts WHERE key='notion-mapping:trader-tom' AND resolved_at IS NULL")
    assert all(m == "GET" for m, _ in calls)  # nothing was written
    # paused sources are not retried every loop
    assert delivery.deliver_pending() is False and len(calls) == 1


def test_video_already_in_library_is_registered_not_written(home, monkeypatch):
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    _run_fixture(home)
    db.exec("UPDATE sources SET current_run_id='r'")
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), "0000000500008000a000000000000005"))
    assert M.get("trader-tom")["properties"]["vl"]["source"] == "video_url"
    calls = []

    class Fake:
        def _req(self, method, path, **kw):
            calls.append((method, path, kw.get("json")))
            if path.endswith("/query"):
                return {"results": [{"id": "hand", "url": "https://notion/hand", "archived": False,
                                     "properties": {"Name": {"type": "title", "title": [{"plain_text": "手工整理的那期"}]}}}]}
            return LIB

        def page(self, pid):
            return {"archived": False, "icon": None}
    monkeypatch.setattr(delivery, "client", lambda: Fake())
    assert delivery.deliver("v")["status"] == "REGISTERED"
    q = next(c for c in calls if c[1].endswith("/query"))
    assert q[2]["filter"] == {"property": "视频链接", "url": {"contains": "v"}}
    assert not any(m == "POST" and p == "/pages" for m, p, _ in calls)
    s = db.one("SELECT delivery, delivery_reason, notion_url, notion_page_id FROM sources")
    assert s == {"delivery": "REGISTERED", "delivery_reason": "库里已有：手工整理的那期", "notion_url": "https://notion/hand", "notion_page_id": None}
    assert delivery.deliver_pending() is False  # not retried


# ---------------------------------------------------------------- 频道调试台 (tuning.py)
def _tuning_fixture():
    from assetizer import db
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('w','王','UCw','TRACKED','TUNING',?,?)", t, t)
    v1 = db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('w',1,'NEEDS_CONFIRMATION',?,?)",
                 json.dumps({"rules": [{"rule": "按章节写"}]}), t)
    v2 = db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('w',2,'NEEDS_CONFIRMATION',?,?)",
                 json.dumps({"rules": [{"rule": "按章节写"}, {"rule": "附原话"}]}), t)
    return v1, v2


def test_trials_queue_video_by_video_and_run_one_at_a_time(home):
    from assetizer import db, tuning
    v1, v2 = _tuning_fixture()
    assert tuning.draft_playbook("w")["id"] == v2
    tuning.add_sample("w", "https://youtu.be/AAAAAAAAAAA")
    tuning.add_sample("w", "BBBBBBBBBBB")
    out = tuning.trial("w", [v1, v2])
    assert [(x["source_id"], x["playbook_id"]) for x in out] == [
        ("AAAAAAAAAAA", v1), ("AAAAAAAAAAA", v2), ("BBBBBBBBBBB", v1), ("BBBBBBBBBBB", v2)]
    # the first one went out at once; the rest wait, and nothing more goes out while a run is queued
    assert [(r["source_id"], r["playbook_id"]) for r in db.q("SELECT source_id, playbook_id FROM runs")] == [("AAAAAAAAAAA", v1)]
    assert tuning.dispatch_next() is None
    # asking again for the same pairs queues nothing new
    assert all(x.get("skipped") for x in tuning.trial("w", [v1, v2]))
    db.exec("UPDATE runs SET production='COMPLETE'")
    assert tuning.dispatch_next() == "AAAAAAAAAAA-r2"
    assert db.one("SELECT playbook_id FROM runs WHERE id='AAAAAAAAAAA-r2'")["playbook_id"] == v2
    # a login problem holds the whole queue (it would hit every video the same way)
    db.exec("UPDATE runs SET production='WAITING', wait_reason='YOUTUBE_AUTH' WHERE id='AAAAAAAAAAA-r2'")
    tuning.trial("w", [v1], ["AAAAAAAAAAA"])
    assert not db.one("SELECT 1 FROM runs WHERE source_id='BBBBBBBBBBB'")
    # a video waiting on something of its own lets the next video go first
    db.exec("UPDATE runs SET wait_reason='LIVE_IN_PROGRESS' WHERE id='AAAAAAAAAAA-r2'")
    assert tuning.dispatch_next() == "BBBBBBBBBBB-r1"
    wb = tuning.workbench("w")
    assert [(q["source_id"], q["version"]) for q in wb["queue"]] == [("BBBBBBBBBBB", 2), ("AAAAAAAAAAA", 1)]
    assert tuning.clear_queue("w") == 2 and tuning.workbench("w")["queue"] == []
    db.exec("UPDATE channels SET stage='LOCKED'")
    with pytest.raises(ValueError):
        tuning.trial("w", [v2])


def test_votes_feed_the_preference_summary(home):
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    for sid in ("AAAAAAAAAAA", "BBBBBBBBBBB"):
        tuning.add_sample("w", sid)
        for n, pb in ((1, v1), (2, v2)):
            db.exec("INSERT INTO runs(id, source_id, playbook_id, production, summary, created_at) VALUES (?,?,?,'COMPLETE','{}',?)",
                    f"{sid}-r{n}", sid, pb, now())
    tuning.vote("AAAAAAAAAAA-r2", True, "章节清楚")
    tuning.vote("BBBBBBBBBBB-r2", True, None)
    tuning.vote("BBBBBBBBBBB-r1", False, "太长")
    summ = {x["version"]: x for x in tuning.workbench("w")["summary"]}
    assert (summ[2]["liked"], summ[2]["done"], summ[2]["notes"]) == (2, 2, 1)
    assert (summ[1]["liked"], summ[1]["notes"]) == (0, 1)
    run = next(r for sm in tuning.workbench("w")["samples"] for r in sm["runs"] if r["id"] == "AAAAAAAAAAA-r2")
    assert run["liked"] is True and run["note"] == "章节清楚"


def test_tuning_channel_runs_on_the_draft_instead_of_waiting(home):
    from assetizer import db
    from assetizer.pipeline import acquire
    from assetizer.util import now
    _, v2 = _tuning_fixture()
    db.exec("INSERT INTO sources(id, channel_id, created_at, updated_at) VALUES ('CCCCCCCCCCC','w',?,?)", now(), now())
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('c-r1','CCCCCCCCCCC','RUNNING',?)", now())

    class Ctx:
        id = "c-r1"
        run = {"id": "c-r1"}
        source = {"id": "CCCCCCCCCCC", "channel_id": "w"}
        def save(self, *a): pass
    out = acquire.playbook(Ctx())
    assert out["playbook_id"] == v2
    assert db.one("SELECT 1 x FROM samples WHERE channel_id='w' AND source_id='CCCCCCCCCCC'")


def test_compare_aligns_units_by_time_and_diffs_rules(home):
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('s',?,?)", now(), now())
    for rid, pb in (("s-r1", v1), ("s-r2", v2)):
        db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at) VALUES (?,?,?,'COMPLETE',?)", rid, "s", pb, now())
    for uid, rid, title, a, b in [("a1", "s-r1", "通胀", 0, 100), ("a2", "s-r1", "汇率", 100, 200),
                                  ("b1", "s-r2", "通胀与利率", 5, 110), ("b2", "s-r2", "地产", 300, 400)]:
        db.exec("INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, data) VALUES (?,?,'s',?,0,?,?,?,'{}')",
                uid, rid, uid, title, a, b)
    c = tuning.compare("s-r1", "s-r2")
    rows = [((p["a"] or {}).get("title"), (p["b"] or {}).get("title")) for p in c["pairs"]]
    assert rows == [("通胀", "通胀与利率"), ("汇率", None), (None, "地产")]
    assert c["matched"] == 1 and c["rules_added"] == ["附原话"] and c["rules_removed"] == []
    tuning.adopt("s", "s-r1")
    assert db.one("SELECT current_run_id, delivery FROM sources WHERE id='s'") == {"current_run_id": "s-r1", "delivery": "NOT_STARTED"}
    db.exec("UPDATE runs SET production='RUNNING' WHERE id='s-r2'")
    with pytest.raises(ValueError):
        tuning.adopt("s", "s-r2")


def test_retune_pauses_intake(home):
    from assetizer import db, tuning
    from assetizer.ingest import set_enabled
    _tuning_fixture()
    with pytest.raises(ValueError):
        tuning.retune("w")  # still tuning
    db.exec("UPDATE channels SET stage='LOCKED'")
    set_enabled("w", True)
    assert tuning.retune("w") == {"stage": "TUNING", "ingest_paused": True}
    assert db.setting("ingest:w")["enabled"] is False



def test_intake_falls_back_to_channel_listing_and_compares_approx_dates_by_day(home, monkeypatch):
    from assetizer import db, ingest
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('t','Tom','UCt','TRACKED','LOCKED',?,?)", t, t)
    db.set_setting("ingest:t", {"enabled": True, "since": "2026-10-09T15:00:00+00:00", "min_minutes": 10})
    monkeypatch.setattr(ingest, "rss", lambda cid: (_ for _ in ()).throw(RuntimeError("404")))
    monkeypatch.setattr(ingest, "listing", lambda cid, limit=15: [
        {"id": "AAAAAAAAAAA", "title": "same day", "published": "2026-10-09T00:00:00+00:00", "duration": 3600, "approx": True},
        {"id": "BBBBBBBBBBB", "title": "short", "published": "2026-10-10T00:00:00+00:00", "duration": 120, "approx": True},
        {"id": "CCCCCCCCCCC", "title": "older", "published": "2026-10-08T00:00:00+00:00", "duration": 3600, "approx": True}])
    import assetizer.delivery as d
    monkeypatch.setattr(d, "registered_in_target", lambda src: None)
    assert ingest.poll()["queued"] == ["AAAAAAAAAAA"]


def test_channel_info_update_and_fixed_routes_win(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    _tuning_fixture()
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    r = c.post("/api/channels/w", json={"name": " 王海滨 ", "why": "宏观框架"}, headers=h)
    assert r.status_code == 200 and r.json() == {"id": "w", "name": "王海滨", "why": "宏观框架"}
    assert c.post("/api/channels/w", json={"name": "  "}, headers=h).status_code == 400
    # /api/channels/track is its own endpoint, not a channel called "track"
    r = c.post("/api/channels/track", json={"name": "x", "why": "y", "prompt": "z", "source_id": "nope"}, headers=h)
    assert r.status_code == 400 and "频道信息" in r.json()["detail"]


def test_channels_api_reports_compile_state(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    _tuning_fixture()
    p1 = db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, created_at) VALUES ('w',1,'a','x','2026-10-01T00:00:00+00:00')")
    db.exec("UPDATE playbooks SET prompt_id=? WHERE version=1", p1)
    c = TestClient(web.app)
    state = lambda: next(x for x in c.get("/api/channels", headers={"host": "127.0.0.1"}).json()["channels"] if x["id"] == "w")["compile"]
    assert state() == {"state": "done"}
    db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, created_at) VALUES ('w',2,'b','y',?)", now())
    assert state()["state"] == "running"
    db.event("compile", "Playbook 编译（w）失败：CONTENT_FAILURE", level="error")
    assert state()["state"] == "failed" and "CONTENT_FAILURE" in state()["error"]


# ---------------------------------------------------------------- YouTube login via CookieCloud (cookies.py)
class _Resp:
    def __init__(self, status, text="", ctype="text/plain", data=None):
        self.status_code, self.text, self.headers, self._data = status, text, {"content-type": ctype}, data

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


NETSCAPE = "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\ty\n"


def _cc(home, monkeypatch, resp):
    from assetizer import cookies, db
    db.set_setting("cookiecloud_origin", "https://cc.example.net/")
    monkeypatch.setenv("ASSETIZER_COOKIECLOUD_TOKEN", "ccat_test")
    monkeypatch.setenv("ASSETIZER_CF_ACCESS_CLIENT_ID", "id.access")
    monkeypatch.setenv("ASSETIZER_CF_ACCESS_CLIENT_SECRET", "sec")
    monkeypatch.setattr(cookies.time, "sleep", lambda s: None)  # the one retry after a dropped connection or a 530
    seen = {}

    def get(url, params=None, headers=None, **kw):
        seen.update(url=url, params=params, headers=headers)
        return resp
    monkeypatch.setattr(cookies.httpx, "get", get)
    return seen


def test_cookiecloud_fetch_sends_both_credentials_and_counts_httponly(home, monkeypatch):
    from assetizer import cookies
    seen = _cc(home, monkeypatch, _Resp(200, NETSCAPE))
    text = cookies.fetch()
    assert seen["url"] == "https://cc.example.net/api/v3/cookies"
    assert seen["params"] == {"format": "netscape", "domains": "youtube.com"}
    assert seen["headers"]["Authorization"] == "Bearer ccat_test" and seen["headers"]["CF-Access-Client-Id"] == "id.access"
    assert len(cookies._cookie_lines(text)) == 2


@pytest.mark.parametrize("resp,code", [
    (_Resp(403, "<html>Access</html>", "text/html"), "access_denied"),
    (_Resp(302, ""), "access_denied"),
    (_Resp(401, "", "application/json", {"code": "token_revoked"}), "token_revoked"),
    (_Resp(404, "", "application/json", {"code": "cookie_payload_missing"}), "cookie_payload_missing"),
    (_Resp(200, "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"), "no_youtube_cookies"),
])
def test_cookiecloud_errors_have_codes(home, monkeypatch, resp, code):
    from assetizer import cookies
    _cc(home, monkeypatch, resp)
    with pytest.raises(cookies.CookieError) as e:
        cookies.fetch()
    assert e.value.code == code and code in cookies.FIX


def test_ytdlp_args_uses_a_private_temp_file_and_removes_it(home, monkeypatch):
    import os
    import stat
    from assetizer import cookies, db
    _cc(home, monkeypatch, _Resp(200, NETSCAPE))
    with cookies.ytdlp_args() as args:
        assert args[0] == "--cookies"
        path = args[1]
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and "SID" in open(path).read()
    assert not os.path.exists(path)
    st = db.setting("youtube_cookies_state")
    assert st["ok"] is True and st["source"] == "cookiecloud" and st["count"] == 2


def test_ytdlp_args_falls_back_to_the_server_browser_with_an_alert(home, monkeypatch):
    import dataclasses
    from pathlib import Path
    from assetizer import config, cookies, db
    config.override(dataclasses.replace(config.get(), browser_profile=Path("/srv/profile")))
    _cc(home, monkeypatch, _Resp(401, "", "application/json", {"code": "token_revoked"}))
    with cookies.ytdlp_args() as args:
        assert args == ["--cookies-from-browser", "chromium:/srv/profile"]
    assert db.setting("youtube_cookies_state")["code"] == "token_revoked"
    assert db.one("SELECT body FROM alerts WHERE key='cookiecloud' AND resolved_at IS NULL")["body"].endswith("（错误码 token_revoked）")
    # not configured at all: the browser profile, quietly
    db.set_setting("cookiecloud_origin", None)
    with cookies.ytdlp_args() as args:
        assert args == ["--cookies-from-browser", "chromium:/srv/profile"]


@pytest.mark.parametrize("failure", ["chmod", "record"])
def test_ytdlp_temp_file_is_removed_when_preparation_fails(home, monkeypatch, failure):
    from assetizer import config, cookies
    _cc(home, monkeypatch, _Resp(200, NETSCAPE))
    chmod = cookies.os.chmod

    def fail_cookie_file(path, mode, **kw):
        if str(path).endswith(".txt"):
            raise OSError("simulated filesystem failure")
        return chmod(path, mode, **kw)

    if failure == "chmod":
        monkeypatch.setattr(cookies.os, "chmod", fail_cookie_file)
    else:
        def fail_record(*args, **kw):
            raise OSError("simulated state failure")
        monkeypatch.setattr(cookies, "_record", fail_record)
    with cookies.ytdlp_args() as args:
        assert "--cookies" not in args
        assert not list((config.get().secrets_dir / "run").glob("yt-cookies-*"))


# ---------------------------------------------------------------- 机房: CookieCloud credentials (docs/cookiecloud-credentials-ui.md)
# synthetic values in the accepted formats; a leak is any 6-character piece of them in an answer or a log
CC_ID = "9f86d081884c7d659a2feaa0c55ad015.access"
CC_SECRET = "Kx7Qp2Lm9Vb4Nz8Rt6Yh1Wd5Jf0Gc_Ua-Se3Ti!"
CC_TOKEN = "ccat_Q7mK2pV9xL4bN8zR1tY6hW3dJ5fG0cUaE2sT4vB8nM"
CC_TOKEN_2 = "ccat_Z1yX2wV3uT4sR5qP6oN7mL8kJ9iH0gF1eD2cB3aZ4y"
CC_URL = "/api/cookiecloud/credentials"


def _no_secret_in(text: str) -> None:
    t = text.lower()
    for s in (CC_ID[:32], CC_SECRET.lower(), CC_TOKEN[5:].lower(), CC_TOKEN_2[5:].lower()):
        assert not [s[i:i + 6] for i in range(len(s) - 5) if s[i:i + 6] in t]


@pytest.fixture
def cc_api(home, monkeypatch):
    """A loopback client for the 机房 API, with no CookieCloud secret coming from the environment."""
    from fastapi.testclient import TestClient
    from assetizer.web import app as web
    for n in ("COOKIECLOUD_TOKEN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.delenv("ASSETIZER_" + n, raising=False)
    monkeypatch.setattr(web, "_throttle_at", {})
    return TestClient(web.app, base_url="http://127.0.0.1:8770")


def test_credentials_are_written_0600_atomically_and_partially(cc_api, home):
    import stat
    from assetizer import config, db
    d = config.get().secrets_dir
    d.mkdir(parents=True)
    d.chmod(0o755)
    r = cc_api.post(CC_URL, json={"origin": "https://CookieCloud-Read.Example.net/", "access_client_id": CC_ID,
                                  "access_client_secret": CC_SECRET, "app_token": f"  {CC_TOKEN}\n"})
    assert r.status_code == 200, r.text
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = {"cf-access-client-id": CC_ID, "cf-access-client-secret": CC_SECRET, "cookiecloud-token": CC_TOKEN}
    for name, value in files.items():
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600 and (d / name).read_text() == value
    assert sorted(p.name for p in d.iterdir()) == sorted(files)  # no temp file left behind
    assert db.setting("cookiecloud_origin") == "https://cookiecloud-read.example.net"
    cc = r.json()["cookiecloud"]
    assert cc["configured"] and cc["fields"]["app_token"]["source"] == "file" and cc["fields"]["app_token"]["updated_at"]
    _no_secret_in(r.text)

    # a partial update leaves the other fields alone; an empty string means "unchanged"
    r = cc_api.post(CC_URL, json={"app_token": CC_TOKEN_2, "access_client_id": ""})
    assert r.status_code == 200
    assert (d / "cookiecloud-token").read_text() == CC_TOKEN_2 and (d / "cf-access-client-id").read_text() == CC_ID
    assert db.setting("cookiecloud_origin") == "https://cookiecloud-read.example.net"

    # clear deletes the file (or the origin setting)
    r = cc_api.post(CC_URL, json={"clear": ["access_client_secret", "origin"]})
    assert r.status_code == 200
    assert not (d / "cf-access-client-secret").exists() and db.setting("cookiecloud_origin") is None
    assert r.json()["cookiecloud"]["fields"]["access_client_secret"] == {"configured": False, "source": None, "updated_at": None}
    assert (d / "cf-access-client-id").read_text() == CC_ID
    log = " ".join(e["message"] for e in db.q("SELECT message FROM events WHERE kind='config'"))
    assert "App Token" in log and "已清除" in log
    _no_secret_in(log)


@pytest.mark.parametrize("body", [
    {"access_client_id": CC_ID.upper()},
    {"access_client_id": CC_SECRET + ".access"},
    {"access_client_secret": CC_SECRET[:20] + " " + CC_SECRET[20:]},
    {"access_client_secret": CC_SECRET[:20]},
    {"app_token": "ccat_" + CC_SECRET},
    {"app_token": CC_TOKEN[5:]},
    {"origin": f"https://u:{CC_SECRET}@cc.example.net"},
    {"origin": f"https://cc.example.net/{CC_TOKEN}"},
    {"origin": f"http://cc.example.net?{CC_TOKEN}"},
    {"origin": "https://cc.example.net:8443"},
    {"origin": f"https://@cc.example.net/#{CC_TOKEN}"},
    {"app_token": CC_TOKEN, "clear": ["app_token"]},
    {"clear": [CC_SECRET]},
    {CC_SECRET: CC_TOKEN},
    {"app_token": {"v": CC_TOKEN}},
    [CC_TOKEN],
])
def test_invalid_credentials_get_400_without_echo(cc_api, home, caplog, body):
    import logging
    from assetizer import config
    caplog.set_level(logging.DEBUG)
    r = cc_api.post(CC_URL, json=body)
    assert r.status_code == 400 and r.json()["detail"]
    _no_secret_in(r.text)
    _no_secret_in(caplog.text)
    assert not (config.get().secrets_dir / "cookiecloud-token").exists()


def test_malformed_or_oversized_bodies_are_refused_without_echo(cc_api):
    r = cc_api.post(CC_URL, content=b'{"app_token": "' + CC_TOKEN.encode(), headers={"content-type": "application/json"})
    assert r.status_code == 400
    _no_secret_in(r.text)
    r = cc_api.post(CC_URL, content=b'{"app_token": "' + CC_TOKEN.encode() * 400 + b'"}', headers={"content-type": "application/json"})
    assert r.status_code == 413
    _no_secret_in(r.text)


def test_validation_errors_never_echo_the_input(cc_api):
    r = cc_api.post("/api/submit", json={"url": {"token": CC_TOKEN}})
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["loc"] == ["body", "url"] and "input" not in err and "ctx" not in err
    _no_secret_in(r.text)


def test_secrets_from_the_environment_are_refused_with_409(cc_api, home, monkeypatch):
    from assetizer import config
    monkeypatch.setenv("ASSETIZER_COOKIECLOUD_TOKEN", CC_TOKEN)
    d = config.get().secrets_dir
    for body in ({"app_token": CC_TOKEN_2}, {"clear": ["app_token"]}, {"app_token": CC_TOKEN_2, "access_client_secret": CC_SECRET}):
        r = cc_api.post(CC_URL, json=body)
        assert r.status_code == 409 and "ASSETIZER_COOKIECLOUD_TOKEN" in r.json()["detail"]
        _no_secret_in(r.text)
    # nothing was written, not even the other field of the mixed request
    assert not (d / "cookiecloud-token").exists() and not (d / "cf-access-client-secret").exists()
    assert cc_api.post(CC_URL, json={"access_client_id": CC_ID}).status_code == 200
    eng = cc_api.get("/api/engine")
    fields = eng.json()["youtube"]["cookiecloud"]["fields"]
    assert fields["app_token"] == {"configured": True, "source": "env", "updated_at": None}
    assert fields["access_client_id"]["source"] == "file"
    _no_secret_in(eng.text)


def test_status_tells_where_each_secret_comes_from_but_not_what_it_is(home, monkeypatch):
    from assetizer import cookies
    for n in ("COOKIECLOUD_TOKEN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.delenv("ASSETIZER_" + n, raising=False)
    cookies.save_credentials({"origin": "https://cc.example.net", "access_client_id": CC_ID, "access_client_secret": CC_SECRET})
    monkeypatch.setenv("ASSETIZER_COOKIECLOUD_TOKEN", CC_TOKEN)
    st = cookies.status()
    f = st["cookiecloud"]["fields"]
    assert f["access_client_id"]["source"] == "file" and f["access_client_id"]["updated_at"].endswith("+00:00")
    assert f["app_token"] == {"configured": True, "source": "env", "updated_at": None}
    assert st["cookiecloud"]["configured"] and st["cookiecloud"]["access_token"]
    _no_secret_in(json.dumps(st))


@pytest.mark.parametrize("resp,code", [
    (_Resp(401, "<html>Unauthorized</html>", "text/html"), "access_denied"),  # Cloudflare: no service token
    (_Resp(403, "<html>Forbidden</html>", "text/html"), "access_denied"),     # Cloudflare: wrong IP or bad token
    (_Resp(429, "error code: 1015", "text/plain"), "rate_limited"),            # Cloudflare edge rate limit
    (_Resp(429, "", "application/json", {"error": "Too Many Requests", "code": "ingress_rate_limited"}), "ingress_rate_limited"),
    (_Resp(404, "", "application/json", {"error": "Not Found", "code": "not_found"}), "not_found"),
    (_Resp(502, "", "application/json", {"error": "Bad Gateway", "code": "upstream_unavailable"}), "upstream_unavailable"),
    (_Resp(504, "", "application/json", {"error": "Gateway Timeout", "code": "upstream_timeout"}), "upstream_timeout"),
    (_Resp(401, "", "application/json", {"error": "Unauthorized", "code": "token_revoked"}), "token_revoked"),
    (_Resp(403, "", "application/json", {"error": "Forbidden", "code": "domain_not_allowed"}), "domain_not_allowed"),
    (_Resp(530, "<html>Argo Tunnel error</html>", "text/html"), "tunnel_down"),  # Cloudflare 1033: no connector
    (_Resp(502, "<html>Bad gateway</html>", "text/html"), "proxy_down"),         # cloudflared up, read-proxy not
    # Cloudflare answers in JSON when JSON was asked for; it is still Cloudflare's own error, not the box's
    (_Resp(502, "", "application/json", {"cloudflare_error": True, "error_name": "origin_bad_gateway", "error_code": 502}), "proxy_down"),
    (_Resp(530, "", "application/json", {"cloudflare_error": True, "error_code": 1033}), "tunnel_down"),
    (_Resp(503, "<html>Service unavailable</html>", "text/html"), "unreachable"),
    (_Resp(200, "<html>another site</html>", "text/html"), "not_found"),         # the origin points elsewhere
])
def test_cookiecloud_test_maps_every_ingress_error(cc_api, home, monkeypatch, resp, code):
    from assetizer import cookies, db
    _cc(home, monkeypatch, resp)
    r = cc_api.post("/api/cookiecloud/test", json={})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False and out["code"] == code and out["fix"] == cookies.FIX[code]
    assert db.setting("youtube_cookies_state")["code"] == code


def test_cookiecloud_test_reports_cookies_and_token_then_throttles(cc_api, home, monkeypatch):
    from assetizer import cookies, db
    from assetizer.web import app as web
    _cc(home, monkeypatch, None)
    calls = []

    def get(url, params=None, headers=None, **kw):
        calls.append(url)
        if url.endswith("/api/v3/token/introspect"):
            return _Resp(200, "", "application/json", {"status": "ok", "auth_type": "token", "token": {
                "token_id": "tk_internal", "name": "assetizer", "scopes": ["cookies:read"], "allowed_domains": ["youtube.com"]}})
        return _Resp(200, NETSCAPE)
    monkeypatch.setattr(cookies.httpx, "get", get)
    db.raise_alert("cookiecloud", "warning", "读不到 CookieCloud 的 YouTube 登录态")
    r = cc_api.post("/api/cookiecloud/test", json={})
    assert r.json() == {"ok": True, "count": 2, "hosts": [".youtube.com"],
                        "token": {"name": "assetizer", "scopes": ["cookies:read"], "allowed_domains": ["youtube.com"]}}
    assert "tk_internal" not in r.text and "SID" not in r.text
    assert db.setting("youtube_cookies_state")["ok"] is True
    assert db.one("SELECT resolved_at FROM alerts WHERE key='cookiecloud'")["resolved_at"]
    # a second test within 10 seconds is refused before it reaches CookieCloud
    r = cc_api.post("/api/cookiecloud/test", json={})
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 10
    assert len(calls) == 2
    monkeypatch.setattr(web, "_throttle_at", {})  # ten seconds later
    assert cc_api.post("/api/cookiecloud/test", json={}).status_code == 200


def test_cookiecloud_endpoints_stay_behind_the_guard(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import config
    from assetizer.web import app as web
    monkeypatch.setattr(web, "PUBLIC_HOST", "kan.example.com")
    monkeypatch.setattr(web, "ACCESS_TEAM", "example-team")
    monkeypatch.setattr(web, "ACCESS_AUD", "x" * 64)
    c = TestClient(web.app)
    for path in (CC_URL, "/api/cookiecloud/test", "/api/cookiecloud/test-video"):
        assert c.post(path, json={"app_token": CC_TOKEN}, headers={"host": "kan.example.com"}).status_code == 403
        assert c.post(path, json={"app_token": CC_TOKEN},
                      headers={"host": "kan.example.com", "cf-access-jwt-assertion": "forged"}).status_code == 403
        assert c.post(path, json={"app_token": CC_TOKEN}, headers={"host": "evil.example"}).status_code == 403
        assert c.post(path, content=f"app_token={CC_TOKEN}",
                      headers={"host": "127.0.0.1:8770", "content-type": "application/x-www-form-urlencoded"}).status_code == 415
    assert not (config.get().secrets_dir / "cookiecloud-token").exists()


# ---------------------------------------------------------------- 机房: video test (does YouTube accept the CookieCloud login?)
SIGNED_IN = ("# Netscape HTTP Cookie File\n"
             ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsapisid-value-7Qp2Lm\n"
             "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tlogin-value-K2pV9x\n")
VT_URL = "/api/cookiecloud/test-video"
MEMBER = "https://youtu.be/-PM846taOS4?si=shared"


def _yt(monkeypatch, returncode=0, info=None, stderr=""):
    """Stub yt-dlp; remember its command and what the cookie file looked like while it ran."""
    import os
    import stat
    import subprocess
    from assetizer import youtube
    seen = {}

    def run(cmd, **kw):
        path = cmd[cmd.index("--cookies") + 1]
        seen.update(cmd=cmd, path=path, mode=stat.S_IMODE(os.stat(path).st_mode), signed_in="SAPISID" in open(path).read())
        return subprocess.CompletedProcess(cmd, returncode, json.dumps(info or {}), stderr)
    monkeypatch.setattr(youtube.subprocess, "run", run)
    return seen


def _waiting_run():
    from assetizer import db
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, wait_reason, created_at) VALUES ('v-r1','v','WAITING','YOUTUBE_AUTH',?)", t)


def test_video_test_passes_on_a_members_only_video_and_wakes_waiting_runs(cc_api, home, monkeypatch):
    import os
    from assetizer import db
    _cc(home, monkeypatch, _Resp(200, SIGNED_IN))
    seen = _yt(monkeypatch, info={"id": "-PM846taOS4", "title": "会员专享", "channel": "王海滨的财经闲谈",
                                  "availability": "subscriber_only", "duration": 600})
    _waiting_run()
    r = cc_api.post(VT_URL, json={"url": MEMBER})
    out = r.json()
    assert r.status_code == 200 and out["ok"] and out["verdict"] == "member_ok" and out["woken"] == 1
    assert out["url"] == "https://www.youtube.com/watch?v=-PM846taOS4" and out["video"]["title"] == "会员专享"
    assert out["cookies"] == {"count": 2, "signed_in": True}
    # yt-dlp saw only the CookieCloud cookies, in a private file that is gone afterwards
    assert seen["cmd"][-1] == out["url"] and "--cookies-from-browser" not in seen["cmd"] and "--skip-download" in seen["cmd"]
    assert seen["mode"] == 0o600 and seen["signed_in"] and not os.path.exists(seen["path"])
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "QUEUED"
    assert db.setting("youtube_video_test")["verdict"] == "member_ok"
    shown = r.text + json.dumps(db.setting("youtube_video_test")) + cc_api.get("/api/engine").text
    assert "sapisid-value" not in shown and "login-value" not in shown


@pytest.mark.parametrize("returncode,stderr,verdict", [
    (1, "ERROR: [youtube] -PM846taOS4: Join this channel to get access to members-only content like this video, and other exclusive perks.", "not_member"),
    (1, "ERROR: [youtube] -PM846taOS4: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication.", "bot_challenge"),
    (0, "WARNING: [youtube] The provided YouTube account cookies are no longer valid. They have likely been rotated in the browser as a security measure.", "cookies_rejected"),
    (1, "ERROR: [youtube] -PM846taOS4: Sign in to confirm your age. This video may be inappropriate for some users.", "age_restricted"),
    (1, "ERROR: [youtube] -PM846taOS4: Private video. Sign in if you've been granted access to this video", "private"),
    (1, "ERROR: [youtube] -PM846taOS4: Video unavailable. This video has been removed by the uploader", "unavailable"),
    (1, "ERROR: unable to download API page: HTTP Error 429: Too Many Requests", "rate_limited"),
])
def test_video_test_explains_why_youtube_refused(cc_api, home, monkeypatch, returncode, stderr, verdict):
    from assetizer import db, youtube
    _cc(home, monkeypatch, _Resp(200, SIGNED_IN))
    _yt(monkeypatch, returncode, {"id": "-PM846taOS4", "title": "t", "availability": "public"}, stderr)
    _waiting_run()
    out = cc_api.post(VT_URL, json={"url": MEMBER}).json()
    assert out["ok"] is False and out["verdict"] == verdict and out["message"] == youtube.VERDICT[verdict]
    assert out["detail"] == stderr[:300] and "woken" not in out
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "WAITING"  # a failed test wakes nothing


def test_video_test_does_not_ask_youtube_without_a_sign_in(cc_api, home, monkeypatch):
    _cc(home, monkeypatch, _Resp(200, NETSCAPE))  # SID and PREF only: no SAPISID, so YouTube would see a visitor
    seen = _yt(monkeypatch)
    out = cc_api.post(VT_URL, json={"url": MEMBER}).json()
    assert out["verdict"] == "not_signed_in" and out["ok"] is False and out["cookies"] == {"count": 2, "signed_in": False}
    assert "cmd" not in seen


def test_video_test_reports_cookiecloud_failures_without_asking_youtube(cc_api, home, monkeypatch):
    from assetizer import cookies, db
    _cc(home, monkeypatch, _Resp(403, "<html>Forbidden</html>", "text/html"))
    seen = _yt(monkeypatch)
    out = cc_api.post(VT_URL, json={"url": MEMBER}).json()
    assert out["verdict"] == "cookiecloud" and out["code"] == "access_denied" and out["message"] == cookies.FIX["access_denied"]
    assert out["detail"] == "HTTP 403"
    assert "cmd" not in seen and db.setting("youtube_cookies_state")["code"] == "access_denied"


def test_video_test_needs_a_youtube_link_and_is_throttled(cc_api, home, monkeypatch):
    _cc(home, monkeypatch, _Resp(200, SIGNED_IN))
    _yt(monkeypatch, info={"id": "-PM846taOS4", "title": "t", "availability": "public"})
    assert cc_api.post(VT_URL, json={"url": "随便写的几个字"}).status_code == 400
    assert cc_api.post(VT_URL, json={"url": MEMBER}).json()["verdict"] == "login_ok"  # the bad link used no quota
    r = cc_api.post(VT_URL, json={"url": MEMBER})
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 20


def test_changing_credentials_clears_the_last_video_test_but_keeps_its_video(cc_api, home):
    from assetizer import db
    db.set_setting("youtube_video_test", {"ok": True, "verdict": "member_ok", "url": "https://www.youtube.com/watch?v=-PM846taOS4"})
    assert cc_api.post(CC_URL, json={"app_token": CC_TOKEN}).status_code == 200
    assert db.setting("youtube_video_test") == {"url": "https://www.youtube.com/watch?v=-PM846taOS4"}


def test_a_dropped_connection_is_tried_once_more_and_the_failure_is_named(home, monkeypatch):
    import httpx
    from assetizer import cookies, db
    _cc(home, monkeypatch, None)
    monkeypatch.setattr(cookies.time, "sleep", lambda s: None)
    calls = []

    def blip(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset by peer")
        return _Resp(200, NETSCAPE)
    monkeypatch.setattr(cookies.httpx, "get", blip)
    assert cookies.fetch() and len(calls) == 2

    def down(url, **kw):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(cookies.httpx, "get", down)
    with cookies.ytdlp_args():
        pass
    st = db.setting("youtube_cookies_state")
    assert st["code"] == "unreachable" and st["detail"] == "ConnectError"
    assert db.one("SELECT body FROM alerts WHERE key='cookiecloud'")["body"].endswith("（错误码 unreachable，ConnectError）")

    def slow(url, **kw):
        calls.append(url)
        raise httpx.ReadTimeout("read timed out")
    calls.clear()
    monkeypatch.setattr(cookies.httpx, "get", slow)
    with pytest.raises(cookies.CookieError):
        cookies.fetch()
    assert len(calls) == 1  # a timeout is not tried again: it already waited 30 seconds


def test_a_flapping_tunnel_gets_one_more_try(home, monkeypatch):
    from assetizer import cookies
    _cc(home, monkeypatch, None)
    answers = [_Resp(530, "<html>error code: 1033</html>", "text/html"), _Resp(200, NETSCAPE)]
    monkeypatch.setattr(cookies.httpx, "get", lambda url, **kw: answers.pop(0))
    assert len(cookies._cookie_lines(cookies.fetch())) == 2 and not answers
    answers[:] = [_Resp(502, "", "application/json", {"cloudflare_error": True, "error_name": "origin_bad_gateway"}),
                  _Resp(200, NETSCAPE)]
    assert cookies.fetch() and not answers  # Cloudflare's JSON error page is retried as well
    # the ingress proxy's own JSON errors are answers from the box, not a flapping tunnel: no retry
    answers[:] = [_Resp(502, "", "application/json", {"code": "upstream_unavailable"}), _Resp(200, NETSCAPE)]
    with pytest.raises(cookies.CookieError) as e:
        cookies.fetch()
    assert e.value.code == "upstream_unavailable" and len(answers) == 1


# ---------------------------------------------------------------- agy media reads survive agy updating itself
FAKE_AGY = """#!{python}
import json, os, sys, time
if "--version" in sys.argv:
    print(os.environ.get("FAKE_AGY_VERSION", "1.4.5"))
    sys.exit(0)
tool, param, path, answer = (os.environ.get(k, "") for k in ("FAKE_AGY_TOOL", "FAKE_AGY_PARAM", "FAKE_AGY_PATH", "FAKE_AGY_ANSWER"))
if path == "@clip":  # the media self-test: the clip in its one --add-dir
    path = os.path.join(sys.argv[sys.argv.index("--add-dir") + 1], "canary.mp4")
cid = "fake-conversation-0001"
print(json.dumps({"event": "init", "init": {"model": sys.argv[sys.argv.index("--model") + 1], "tools": ["view_file"]}}), flush=True)
steps = [(2, tool, param, path)] if tool else []
if os.environ.get("FAKE_AGY_TOOL2"):  # a second tool step after the first, e.g. re-reading its own prompt
    path2 = os.environ["FAKE_AGY_PATH2"]
    path2 = os.path.join(os.getcwd(), "..", "prompt.md") if path2 == "@prompt" else path2
    steps.append((4, os.environ["FAKE_AGY_TOOL2"], os.environ["FAKE_AGY_PARAM2"], path2))
for index, name, key, value in steps:
    for state in ("ACTIVE", "DONE"):
        print(json.dumps({"step_update": {"conversation_id": cid, "step_index": index, "step_type": "tool", "tool_name": name,
                                          "state": state, "tool_info": {"name": name, "parameters": {key: value}}}}), flush=True)
        time.sleep(0.3)  # still running when the watchdog kills a deviation
    # agy refusing the media: an error step right after the read (2026-10-22, 26 MB), then the model flails
    step = "error_message" if os.environ.get("FAKE_AGY_REFUSE") and index == 2 else "agent_response"
    print(json.dumps({"step_update": {"conversation_id": cid, "step_index": index + 1, "step_type": step, "state": "DONE"}}), flush=True)
    time.sleep(0.3)
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": answer, "conversation_id": cid, "usage": {}}}), flush=True)
"""


@pytest.fixture
def fake_agy(home, monkeypatch):
    """agy replaced by a script that reports one tool step (name, parameter, path) and then answers."""
    import dataclasses
    import sys
    from assetizer import config
    from assetizer.providers.agy import AgyProvider
    exe = home / "agy"
    exe.write_text(FAKE_AGY.replace("{python}", sys.executable))
    exe.chmod(0o755)
    config.override(dataclasses.replace(config.get(), agy_bin=str(exe), agy_state_dir=home / "agy-state"))
    monkeypatch.setattr(AgyProvider, "_version", None)

    def use(tool="", param="", path="", answer="", version="1.4.5"):
        for k, v in (("TOOL", tool), ("PARAM", param), ("PATH", path), ("ANSWER", answer), ("VERSION", version)):
            monkeypatch.setenv(f"FAKE_AGY_{k}", v)
    return use


@pytest.fixture
def canary(monkeypatch):
    from assetizer.providers import agy
    monkeypatch.setattr(agy, "_canary_clip", lambda path, code, target: path.write_bytes(b"c" * 1000))
    monkeypatch.setattr(agy.random, "randint", lambda a, b: 4721)


@pytest.mark.parametrize("tool,param", [("view_file", "AbsolutePath"), ("read_file", "Path")])
def test_both_spellings_of_the_agy_media_read_are_accepted(fake_agy, home, tool, param):
    from assetizer.providers.agy import AgyProvider
    clip = home / "clip.mp4"
    clip.write_bytes(b"x")
    fake_agy(tool, param, str(clip), '{"ok": true}')
    r = AgyProvider().call(home / "call", "读视频", kind="understand", media=[str(clip)])
    assert r.receipt["status"] == "SUCCESS" and r.receipt["view_file_count"] == 1 and r.receipt["media_tool"] == tool


def test_reading_any_other_file_is_still_a_deviation(fake_agy, home):
    from assetizer.providers.agy import AgyProvider, ProviderError
    clip = home / "clip.mp4"
    clip.write_bytes(b"x")
    fake_agy("read_file", "Path", "/etc/hosts", "{}")
    with pytest.raises(ProviderError) as e:
        AgyProvider().call(home / "call", "读视频", kind="understand", media=[str(clip)])
    assert e.value.kind == "MODALITY_ROUTING_DEVIATION" and e.value.receipt["deviation"]["tool"] == "read_file"


def test_rereading_its_own_prompt_is_benign_but_any_other_file_is_not(fake_agy, home, monkeypatch):
    from assetizer.providers.agy import AgyProvider, ProviderError
    clip = home / "clip.mp4"
    clip.write_bytes(b"x")
    fake_agy("read_file", "Path", str(clip), '{"ok": true}')
    monkeypatch.setenv("FAKE_AGY_TOOL2", "read_file")
    monkeypatch.setenv("FAKE_AGY_PARAM2", "Path")
    monkeypatch.setenv("FAKE_AGY_PATH2", "@prompt")  # what MUBB14vJc9U-r5 did on a 25-minute chunk
    r = AgyProvider().call(home / "call", "读视频", kind="understand", media=[str(clip)])
    assert r.receipt["status"] == "SUCCESS" and r.receipt["view_file_count"] == 1
    assert {b["tool"] for b in r.receipt["benign_tools"]} == {"read_file"}  # one entry per step update (ACTIVE, DONE)
    (home / "notes.txt").write_text("x")
    monkeypatch.setenv("FAKE_AGY_PATH2", str(home / "notes.txt"))
    with pytest.raises(ProviderError) as e:
        AgyProvider().call(home / "call-2", "读视频", kind="understand", media=[str(clip)])
    assert e.value.kind == "MODALITY_ROUTING_DEVIATION"


def test_the_media_self_test_needs_the_number_drawn_in_the_clip(fake_agy, canary):
    from assetizer.providers.agy import AgyProvider
    fake_agy("read_file", "Path", "@clip", "4721")
    h = AgyProvider().media_health()
    assert h["ok"] and h["tool"] == "read_file" and h["version"] == "1.4.5"
    fake_agy("read_file", "Path", "@clip", "1234")
    h = AgyProvider().media_health()
    assert not h["ok"] and h["error"] == "WRONG_ANSWER"


def test_the_media_self_test_names_the_tool_agy_used_instead(fake_agy, canary):
    from assetizer.providers.agy import AgyProvider
    fake_agy("open_media", "FilePath", "@clip", "4721")
    h = AgyProvider().media_health()
    assert not h["ok"] and h["error"] == "MODALITY_ROUTING_DEVIATION"
    assert h["tool"] == "open_media" and h["parameters"] == ["FilePath"]


def test_an_agy_update_that_breaks_media_reads_pauses_production_until_the_self_test_passes(home, monkeypatch):
    import time
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.providers.agy import AgyProvider
    result = {"ok": False, "version": "1.5.0", "tool": "open_media", "parameters": ["FilePath"],
              "error": "MODALITY_ROUTING_DEVIATION", "checked_at": "t"}
    monkeypatch.setattr(AgyProvider, "media_health", lambda self: dict(result))
    monkeypatch.setattr(AgyProvider, "version", lambda self: "1.5.0")
    monkeypatch.setattr(runner, "MEDIA_CHECK_EVERY", 0)
    monkeypatch.setattr(runner, "MEDIA_RECHECK_PAUSED", 0)
    db.set_setting("agy_media_version", "1.4.5")
    w = runner.Worker()
    t = time.monotonic()
    w.last.update(retry=t, retention=t, ingest=t, probe=t, health=t)  # only the media check is due
    w.housekeeping()
    assert db.setting("queue_paused")["reason"] == "AGY_MEDIA_READ"
    body = db.one("SELECT body FROM alerts WHERE key='AGY_MEDIA_READ' AND resolved_at IS NULL")["body"]
    assert "1.5.0" in body and "open_media" in body and "FilePath" in body
    # still broken: stays paused (a text-only health probe must not resume it)
    w.housekeeping()
    assert db.setting("queue_paused")["reason"] == "AGY_MEDIA_READ"
    # fixed: production resumes and the run held for it is retried
    _waiting_run()
    db.exec("UPDATE runs SET wait_reason='AGY_MEDIA_READ' WHERE id='v-r1'")
    result.update(ok=True, tool="open_media", error=None)
    w.housekeeping()
    assert db.setting("queue_paused") is None and db.setting("agy_media_version") == "1.5.0"
    assert db.one("SELECT resolved_at FROM alerts WHERE key='AGY_MEDIA_READ'")["resolved_at"]
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "QUEUED"


def test_a_run_whose_media_reads_all_deviate_waits_only_when_agy_itself_cannot_read(home, monkeypatch):
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.providers.agy import AgyProvider, ProviderError
    _waiting_run()
    db.exec("UPDATE runs SET production='RUNNING', wait_reason=NULL WHERE id='v-r1'")
    e = ProviderError("MODALITY_ROUTING_DEVIATION", "read_file", {"deviation": {"tool": "read_file"}})
    monkeypatch.setattr(AgyProvider, "media_health", lambda self: {"ok": True, "version": "1.4.5", "tool": "read_file"})
    assert runner._hold_if_agy_cannot_read_media("v-r1", e) is False  # agy reads the self-test clip: this run's own problem
    assert db.setting("queue_paused") is None
    monkeypatch.setattr(AgyProvider, "media_health", lambda self: {"ok": False, "version": "1.5.0", "tool": "open_media",
                                                                    "parameters": ["FilePath"], "error": "MODALITY_ROUTING_DEVIATION"})
    assert runner._hold_if_agy_cannot_read_media("v-r1", e) is True
    run = db.one("SELECT production, wait_reason FROM runs WHERE id='v-r1'")
    assert run["production"] == "WAITING" and run["wait_reason"] == "AGY_MEDIA_READ"
    assert db.setting("queue_paused")["reason"] == "AGY_MEDIA_READ"
    assert runner._hold_if_agy_cannot_read_media("v-r1", ProviderError("PROVIDER_FAILURE")) is False


def test_agy_refusing_the_media_file_stops_the_call_at_once(fake_agy, home, monkeypatch):
    from assetizer.providers.agy import AgyProvider, ProviderError
    clip = home / "clip.mp4"
    clip.write_bytes(b"x" * 2048)
    fake_agy("read_file", "Path", str(clip), "读不到")
    monkeypatch.setenv("FAKE_AGY_REFUSE", "1")
    with pytest.raises(ProviderError) as e:
        AgyProvider().call(home / "call", "读视频", kind="understand", media=[str(clip)])
    assert e.value.kind == "MEDIA_READ_FAILED" and "2048 bytes" in str(e.value)
    assert e.value.receipt["media_error"]["bytes"] == 2048 and e.value.receipt["status"] == "MEDIA_REFUSED"


def test_the_media_self_test_reports_a_refused_file_size(fake_agy, canary, home, monkeypatch):
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.providers.agy import AgyProvider
    fake_agy("read_file", "Path", "@clip", "4721")
    monkeypatch.setenv("FAKE_AGY_REFUSE", "1")
    h = AgyProvider().media_health()
    assert not h["ok"] and h["error"] == "MEDIA_READ_FAILED" and h["bytes"] == 1000
    runner.media_alert(h)
    body = db.one("SELECT body FROM alerts WHERE key='AGY_MEDIA_READ'")["body"]
    assert "大小上限" in body and "VIEW_FILE_LIMIT" in body
    assert db.setting("queue_paused")["reason"] == "AGY_MEDIA_READ"


def test_proxies_step_down_the_ladder_until_they_fit(home, monkeypatch):
    from assetizer.media import ffmpeg
    sizes = {(32, 1080, 48): 30, (36, 1080, 48): 26, (34, 720, 48): 20}
    tried = []

    def fake_proxy(src, start, length, out, fps=1.0, crf=30, max_height=1080, audio_kbps=48):
        tried.append((crf, max_height, audio_kbps))
        out.write_bytes(b"x" * sizes.get((crf, max_height, audio_kbps), 10))
        return out
    monkeypatch.setattr(ffmpeg, "model_proxy", fake_proxy)
    monkeypatch.setattr(ffmpeg, "VIEW_FILE_LIMIT", 24)
    out, enc = ffmpeg.fit_proxy(home / "src.mkv", 0, 1500, home / "p.mp4")
    assert tried == [(32, 1080, 48), (36, 1080, 48), (34, 720, 48)]
    assert enc == {"fps": 1.0, "crf": 34, "height": 720, "audio_kbps": 48, "bytes": 20}


def test_a_cached_proxy_over_the_current_limit_is_encoded_again(home, monkeypatch):
    import types
    from assetizer import config
    from assetizer.media import ffmpeg
    from assetizer.pipeline import acquire
    from assetizer.util import write_json
    s = config.get()
    calls = []

    def fit(src, start, length, out):
        calls.append(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 10)
        return out, {"crf": 36}
    monkeypatch.setattr(ffmpeg, "fit_proxy", fit)
    monkeypatch.setattr(ffmpeg, "VIEW_FILE_LIMIT", 100)
    ctx = types.SimpleNamespace(source={"id": "v", "media_sha256": "abc"}, media=home / "src.mkv",
                                path=lambda *parts: home.joinpath("work", *parts[1:]))
    name = f"CH01_0_1500_{s.chunk_minutes}_{s.chunk_overlap_minutes}"
    cache = s.media_dir / "v" / "proxies"
    cache.mkdir(parents=True)
    (cache / f"{name}.mp4").write_bytes(b"x" * 500)  # made under the old, larger limit
    write_json(cache / f"{name}.json", {"media_sha256": "abc", "encoding": {"crf": 32}})
    path, enc = acquire._proxy(ctx, "CH01", 0, 1500)
    assert len(calls) == 1 and enc == {"crf": 36} and path.stat().st_size == 10
    path, enc = acquire._proxy(ctx, "CH01", 0, 1500)  # now under the limit: reused
    assert len(calls) == 1 and enc == {"crf": 36}
