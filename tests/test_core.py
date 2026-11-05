import json
from pathlib import Path

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
    assert classify_error('Eligibility check failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:'
                          'loadCodeAssist": EOF') == "PROVIDER_TRANSIENT"
    assert classify_error("model output token limit reached") == "PROVIDER_OUTPUT_LIMIT"
    assert classify("ERROR: Sign in to confirm you’re not a bot") == "BOT_CHALLENGE"
    assert classify("HTTP Error 429: Too Many Requests") == "RATE_LIMITED"
    assert classify("ERROR: [youtube] eu6OH1R3mL8: This live event will begin in a few moments.") == "LIVE_UPCOMING"
    assert classify("[download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol "
                    "(_ssl.c:1032). Giving up after 5 retries") == "NETWORK"


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
    assert classify_error("The stream was interrupted. Please continue the task you were working on.") == "PROVIDER_TRANSIENT"
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
    from assetizer import db, tuning
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, created_at, updated_at) VALUES ('wang','Wang','UCx','TRACKED',?,?)", t, t)
    pid = db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('wang',1,'NEEDS_CONFIRMATION','{}',?)", t)
    tuning.confirm(pid)
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


def _option_ids(lib: dict, prefix: str = "id-") -> dict:
    """``lib`` as Notion returns it: every option carries an id."""
    out = json.loads(json.dumps(lib))
    for p in out["properties"].values():
        for o in p.get(p["type"], {}).get("options", []):
            o["id"] = prefix + o["name"]
    return out


def _rename_option(lib: dict, prop: str, old: str, new: str) -> dict:
    out = json.loads(json.dumps(lib))
    for o in out["properties"][prop]["select"]["options"]:
        if o["name"] == old:
            o["name"] = new
    return out


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


def test_renamed_options_are_followed_by_option_id():
    from assetizer.notion import mapping as M
    lib = _option_ids(LIB)
    cfg = M.preset("trader-tom", "lib", M.schema_of(lib), "0000000500008000a000000000000005")
    assert cfg["properties"]["own"]["option_ids"] == {"@交易员汤姆": "id-@交易员汤姆"}
    assert cfg["properties"]["acc"]["option_ids"]["会员视频"] == "id-会员视频"
    # renamed in Notion: the property, its fixed option and one cell of the 会员/公开 table
    renamed = _rename_option(_rename_option(lib, "内容所属", "@交易员汤姆", "Tom"), "会员/公开", "会员视频", "会员")
    renamed["properties"]["作者"] = renamed["properties"].pop("内容所属")
    schema = M.schema_of(renamed)
    assert set(M.problems(cfg, schema)) == {"own", "acc"}  # by name alone both would look gone
    s = M.sync(cfg, schema)
    assert s["properties"]["own"] == {"name": "作者", "type": "select", "source": "fixed", "value": "Tom",
                                      "option_ids": {"Tom": "id-@交易员汤姆"}}
    assert M.problems(s, schema) == {} and M.sync(s, schema) == s
    assert M.payload("select", M.value(s["properties"]["own"], {})) == {"select": {"name": "Tom"}}
    assert M.value(s["properties"]["acc"], {"access": "member_video"}) == "会员"
    # an option deleted in Notion is a problem again; its id is kept in case it comes back
    deleted = json.loads(json.dumps(renamed))
    deleted["properties"]["作者"]["select"]["options"] = [o for o in deleted["properties"]["作者"]["select"]["options"] if o["name"] != "Tom"]
    d = M.sync(s, M.schema_of(deleted))
    assert d["properties"]["own"]["option_ids"] == {"Tom": "id-@交易员汤姆"}
    assert "没有选项「Tom」" in M.problems(d, M.schema_of(deleted))["own"]
    # a config saved before option ids is pinned by name
    legacy = M.preset("trader-tom", "lib", M.schema_of(LIB), "0000000500008000a000000000000005")
    assert "option_ids" not in legacy["properties"]["own"]
    assert M.sync(legacy, M.schema_of(lib))["properties"]["own"]["option_ids"] == {"@交易员汤姆": "id-@交易员汤姆"}
    # multi-select: each option followed, a name picked in the card pinned, ids of options no longer picked let go
    multi = {"properties": {"tag": {"name": "标签", "type": "multi_select", "source": "fixed", "value": ["A", "C"],
                                    "option_ids": {"A": "1", "B": "2"}}}}
    tag = {"tag": {"name": "标签", "type": "multi_select", "options": ["A2", "B", "C"], "option_ids": {"1": "A2", "2": "B", "3": "C"}}}
    assert M.sync(multi, tag)["properties"]["tag"] == {"name": "标签", "type": "multi_select", "source": "fixed",
                                                       "value": ["A2", "C"], "option_ids": {"A2": "1", "C": "3"}}


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


def test_delivery_follows_a_renamed_option_and_stores_it(home):
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    _preview_fixture(home)
    lib = _option_ids(LIB)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(lib), "0000000500008000a000000000000005"))
    renamed = _rename_option(lib, "内容所属", "@交易员汤姆", "Tom")
    source = db.one("SELECT * FROM sources WHERE id='s1'")
    schema, cfg = delivery.check_target(FakeNotion(renamed), source, M.get("trader-tom"))
    assert M.payload("select", M.value(cfg["properties"]["own"], {})) == {"select": {"name": "Tom"}}
    assert M.get("trader-tom")["properties"]["own"]["value"] == "Tom"  # stored, so the channel page shows it too
    assert not db.one("SELECT 1 FROM alerts WHERE key='notion-mapping:trader-tom' AND resolved_at IS NULL")
    # a config moved to another database meanwhile is left as it is
    M.save("trader-tom", {**M.get("trader-tom"), "database_id": "other"})
    before = M.get("trader-tom")
    M.refresh("trader-tom", "lib", M.schema_of(_rename_option(renamed, "内容所属", "Tom", "汤姆")))
    assert M.get("trader-tom") == before


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


def test_a_version_trial_skips_samples_it_already_ran(home):
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    tuning.add_sample("w", "AAAAAAAAAAA")
    tuning.add_sample("w", "BBBBBBBBBBB")
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at) VALUES ('AAAAAAAAAAA-r1','AAAAAAAAAAA',?,'COMPLETE',?)",
            v2, now())
    out = tuning.trial("w", [v2])
    assert [(x["source_id"], "queued" in x) for x in out] == [("AAAAAAAAAAA", False), ("BBBBBBBBBBB", True)]
    db.exec("UPDATE runs SET production='COMPLETE'")
    # naming the sample runs it again on purpose
    assert tuning.trial("w", [v2], ["AAAAAAAAAAA"])[0].get("queued")


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



def test_intake_compares_the_listings_approx_dates_by_day(home, monkeypatch):
    from assetizer import db, ingest
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('t','Tom','UCt','TRACKED','LOCKED',?,?)", t, t)
    db.set_setting("ingest:t", {"enabled": True, "since": "2026-10-09T15:00:00+00:00", "min_minutes": 10})
    monkeypatch.setattr(ingest, "_list", lambda cid, limit, tabs: ([
        {"id": "AAAAAAAAAAA", "title": "same day", "published": "2026-10-09T00:00:00+00:00", "duration": 3600, "approx": True},
        {"id": "BBBBBBBBBBB", "title": "short", "published": "2026-10-10T00:00:00+00:00", "duration": 120, "approx": True},
        {"id": "CCCCCCCCCCC", "title": "older", "published": "2026-10-08T00:00:00+00:00", "duration": 3600, "approx": True}], None, {"videos"}))
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


WAF_BLOCK = "<html><title>Attention Required! | Cloudflare</title><h1>Sorry, you have been blocked</h1></html>"


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
    (_Resp(403, WAF_BLOCK, "text/html"), "ip_not_allowed"),
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


def test_cookiecloud_blocked_ip_reports_the_egress_address(home, monkeypatch):
    from assetizer import cookies, db
    _cc(home, monkeypatch, _Resp(403, WAF_BLOCK, "text/html"))
    real = cookies.httpx.get
    monkeypatch.setattr(cookies.httpx, "get", lambda url, **kw: _Resp(200, "fl=1\nip=203.0.113.7\nts=2\n") if "trace" in url else real(url, **kw))
    out = cookies.probe()
    assert out["code"] == "ip_not_allowed" and out["detail"] == "出口 IP 203.0.113.7"
    assert db.setting("youtube_cookies_state")["detail"] == "出口 IP 203.0.113.7"
    with cookies.ytdlp_login() as (_, how):
        assert how["code"] == "ip_not_allowed"
    assert "203.0.113.7" in db.connect().execute("SELECT body FROM alerts WHERE key='cookiecloud'").fetchone()[0]


def test_cookiecloud_block_page_without_service_token_is_not_blamed_on_the_ip(home, monkeypatch):
    from assetizer import cookies
    _cc(home, monkeypatch, _Resp(403, WAF_BLOCK, "text/html"))
    monkeypatch.delenv("ASSETIZER_CF_ACCESS_CLIENT_SECRET")
    assert cookies.probe()["code"] == "access_denied"


def test_egress_ip_is_none_when_the_lookup_fails(home, monkeypatch):
    from assetizer import cookies

    def down(url, **kw):
        raise cookies.httpx.ConnectError("x")
    monkeypatch.setattr(cookies.httpx, "get", down)
    assert cookies.egress_ip() is None


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
    with cookies.ytdlp_login() as (args, how):  # SID and PREF only: no sign-in YouTube would see
        assert args[0] == "--cookies" and how == {"source": "cookiecloud", "signed_in": False, "code": None}


def test_ytdlp_args_falls_back_to_the_server_browser_with_an_alert(home, monkeypatch):
    import dataclasses
    from pathlib import Path
    from assetizer import config, cookies, db
    config.override(dataclasses.replace(config.get(), browser_profile=Path("/srv/profile")))
    _cc(home, monkeypatch, _Resp(401, "", "application/json", {"code": "token_revoked"}))
    with cookies.ytdlp_login() as (args, how):
        assert args == ["--cookies-from-browser", "chromium:/srv/profile"]
        assert how == {"source": "browser", "signed_in": None, "code": "token_revoked"}
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
    (_Resp(403, "<html>Forbidden</html>", "text/html"), "access_denied"),     # Cloudflare Access: bad service token
    (_Resp(403, WAF_BLOCK, "text/html"), "ip_not_allowed"),                   # Cloudflare WAF: source IP off the allowlist
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
if os.environ.get("FAKE_AGY_QUOTA"):  # the turn fails at the read because the quota ran out (2026-10-26)
    msg = "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 101h13m11s."
    print(json.dumps({"event": "error", "error": {"message": msg}}), flush=True)
    print(json.dumps({"event": "result", "result": {"status": "ERROR", "error": msg, "conversation_id": cid}}), flush=True)
    sys.exit(1)
time.sleep(float(os.environ.get("FAKE_AGY_FLAIL", "0")))  # a model that lost its media and wanders off
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": answer, "conversation_id": cid, "usage": {}}}), flush=True)
time.sleep(float(os.environ.get("FAKE_AGY_LINGER", "0")))  # agy 1.4.7 often stays up after its result
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


def test_a_call_returns_as_soon_as_agy_reports_its_result(fake_agy, home, monkeypatch):
    import time
    from assetizer.providers.agy import AgyProvider
    fake_agy(answer="在线")
    monkeypatch.setenv("FAKE_AGY_LINGER", "120")
    t = time.monotonic()
    r = AgyProvider().call(home / "call", "只回复两个字：在线", kind="health")
    assert r.text == "在线" and r.receipt["status"] == "SUCCESS" and "watchdog" not in r.receipt
    assert time.monotonic() - t < 15


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
    w.last.update(retry=t, retention=t, ingest=t, probe=t, health=t, playback=t, quota=t)  # only the media check is due
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
    # the self-test could not reach the model at all: an outage holds this run, production goes on
    monkeypatch.setattr(AgyProvider, "media_health", lambda self: {"ok": False, "version": "1.4.7", "error": "PROVIDER_TRANSIENT"})
    assert runner._hold_if_agy_cannot_read_media("v-r1", e) is True
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "NETWORK"
    assert db.setting("queue_paused") is None
    db.exec("UPDATE runs SET production='RUNNING', wait_reason=NULL WHERE id='v-r1'")
    monkeypatch.setattr(AgyProvider, "media_health", lambda self: {"ok": False, "version": "1.5.0", "tool": "open_media",
                                                                    "parameters": ["FilePath"], "error": "MODALITY_ROUTING_DEVIATION"})
    assert runner._hold_if_agy_cannot_read_media("v-r1", e) is True
    run = db.one("SELECT production, wait_reason FROM runs WHERE id='v-r1'")
    assert run["production"] == "WAITING" and run["wait_reason"] == "AGY_MEDIA_READ"
    assert db.setting("queue_paused")["reason"] == "AGY_MEDIA_READ"
    assert runner._hold_if_agy_cannot_read_media("v-r1", ProviderError("PROVIDER_FAILURE")) is False
    # the self-test ran out of quota: the run and production wait for the quota, which the health probe lifts
    db.set_setting("queue_paused", None)
    db.exec("UPDATE runs SET production='RUNNING', wait_reason=NULL WHERE id='v-r1'")
    monkeypatch.setattr(AgyProvider, "media_health", lambda self: {"ok": False, "version": "1.4.7", "error": "PROVIDER_QUOTA"})
    assert runner._hold_if_agy_cannot_read_media("v-r1", e) is True
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "PROVIDER_QUOTA"
    assert db.setting("queue_paused")["reason"] == "PROVIDER_QUOTA"


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


def test_a_quota_error_at_the_media_read_is_quota_not_a_refused_file(fake_agy, home, monkeypatch):
    from assetizer.providers.agy import AgyProvider, ProviderError
    clip = home / "clip.mp4"
    clip.write_bytes(b"x" * 2048)
    fake_agy("read_file", "Path", str(clip), "读不到")
    monkeypatch.setenv("FAKE_AGY_REFUSE", "1")
    monkeypatch.setenv("FAKE_AGY_QUOTA", "1")
    with pytest.raises(ProviderError) as e:
        AgyProvider().call(home / "call", "读视频", kind="understand", media=[str(clip)])
    assert e.value.kind == "PROVIDER_QUOTA" and e.value.receipt["status"] == "FAILED"


def test_agy_wandering_off_after_a_refused_file_is_stopped_after_the_grace(fake_agy, home, monkeypatch):
    import time
    from assetizer.providers import agy
    clip = home / "clip.mp4"
    clip.write_bytes(b"x" * 2048)
    fake_agy("read_file", "Path", str(clip), "读不到")
    monkeypatch.setenv("FAKE_AGY_REFUSE", "1")
    monkeypatch.setenv("FAKE_AGY_FLAIL", "120")
    monkeypatch.setattr(agy, "MEDIA_ERROR_GRACE", 1.0)
    t = time.monotonic()
    with pytest.raises(agy.ProviderError) as e:
        agy.AgyProvider().call(home / "call", "读视频", kind="understand", media=[str(clip)])
    assert e.value.kind == "MEDIA_READ_FAILED" and time.monotonic() - t < 15


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

    def fit(src, start, length, out, **kw):
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


def _pcm(seconds, quiet=()):
    """Loud noise with silent stretches at the given (start, end) seconds."""
    import numpy as np
    from assetizer.media import asr
    x = np.random.default_rng(0).integers(-8000, 8000, int(seconds * asr.SR)).astype(np.int16)
    for a, b in quiet:
        x[int(a * asr.SR):int(b * asr.SR)] = 0
    return x


def test_asr_windows_cut_in_silence_and_cover_the_audio():
    from assetizer.media import asr
    pcm = _pcm(40 * 60, quiet=[(910, 911), (1795, 1796)])
    w = asr.windows(pcm)
    assert w[0][0] == 0 and w[-1][1] == len(pcm)
    assert all(a == b for (_, a), (b, _) in zip(w, w[1:]))  # contiguous, no gap or overlap
    cuts = [a / asr.SR for a, _ in w[1:]]
    assert len(cuts) == 2 and 910 <= cuts[0] <= 911 and 1795 <= cuts[1] <= 1796
    assert asr.windows(_pcm(15 * 60)) == [(0, 15 * 60 * asr.SR)]  # short audio: one window


class _FakeWhisper:
    """Says one word one second into every window it is given."""

    def __init__(self, *a, **kw):
        self.calls = []

    def transcribe(self, audio, **kw):
        import types
        self.calls.append(len(audio))
        w = types.SimpleNamespace(start=1.0, end=1.5, word="涨")
        return [types.SimpleNamespace(start=1.0, end=2.0, text="涨", words=[w])], types.SimpleNamespace(language="zh")


def test_asr_windows_are_stitched_on_one_timeline():
    from assetizer.media import asr
    pcm = _pcm(40 * 60, quiet=[(910, 911), (1795, 1796)])
    model = _FakeWhisper()
    r = asr.transcribe_pcm(model, pcm, offset=100.0)
    starts = [a / asr.SR for a, _ in asr.windows(pcm)]
    assert len(model.calls) == 3 and max(model.calls) <= (asr.WINDOW + asr.SEARCH) * asr.SR
    assert [s["start"] for s in r["segments"]] == pytest.approx([100.0 + s + 1.0 for s in starts])
    assert [w["end"] for w in r["words"]] == pytest.approx([100.0 + s + 1.5 for s in starts])
    assert r["language"] == "zh"


def test_remote_asr_runs_each_window_on_the_gpu_box_and_stitches_them_here(monkeypatch):
    import importlib.util
    import threading
    from http.server import ThreadingHTTPServer
    from pathlib import Path
    import numpy as np
    from assetizer.media import asr
    monkeypatch.setenv("HF_HOME", "unused")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    spec = importlib.util.spec_from_file_location("gpu_asr_server", Path(__file__).parents[1] / "scripts/gpu_asr/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    class Recording(_FakeWhisper):
        def transcribe(self, audio, **kw):
            self.audio = getattr(self, "audio", []) + [audio]
            return super().transcribe(audio, **kw)

    fake = Recording()
    monkeypatch.setattr(server, "_load", lambda: fake)
    monkeypatch.setattr(server, "_token", "s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    pcm = _pcm(40 * 60, quiet=[(910, 911), (1795, 1796)])
    try:
        r = asr.transcribe_pcm(asr.RemoteWhisper(url, "s3cret"), pcm, offset=100.0, initial_prompt="涉及 QQQ、NVDA。")
        with pytest.raises(OSError):  # 401
            asr.transcribe_pcm(asr.RemoteWhisper(url, "wrong"), pcm[:asr.SR])
    finally:
        httpd.shutdown()
    w = asr.windows(pcm)
    assert len(fake.audio) == len(w)
    for audio, (a, b) in zip(fake.audio, w):  # the samples arrive exactly as cut here
        assert np.array_equal(audio, pcm[a:b].astype(np.float32) / 32768.0)
    assert [x["end"] for x in r["words"]] == pytest.approx([100.0 + a / asr.SR + 1.5 for a, _ in w])


def test_the_worker_hands_the_whisper_child_the_gpu_box_from_the_setting(home):
    from pathlib import Path
    from assetizer import db
    from assetizer.media import asr
    assert "--remote" not in asr._command(Path("a.wav"), Path("a.json"), 0.0, "zh", None)
    db.set_setting("asr_url", "http://192.168.1.50:8790")
    cmd = asr._command(Path("a.wav"), Path("a.json"), 0.0, "zh", None)
    assert cmd[cmd.index("--remote") + 1] == "http://192.168.1.50:8790"


def test_an_unreachable_gpu_box_falls_back_to_the_cpu(home, monkeypatch):
    import json
    import sys
    import types
    import wave
    from assetizer.media import asr
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=_FakeWhisper))
    wav, out = home / "a.wav", home / "a.json"
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(asr.SR)
        f.writeframes(_pcm(60).tobytes())
    asr.main([str(wav), str(out), "--remote", "http://127.0.0.1:9"])  # nothing listens on the discard port
    r = json.loads(out.read_text())
    assert r["device"] == "cpu" and r["remote_failed"] and [x["word"] for x in r["words"]] == ["涨"]


def test_asr_child_reads_wav_and_writes_json(home, monkeypatch):
    import sys
    import types
    import wave
    from assetizer.media import asr
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=_FakeWhisper))
    wav, out = home / "a.wav", home / "a.wav.asr.json"
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(asr.SR)
        f.writeframes(_pcm(5).tobytes())
    asr.main([str(wav), str(out), "--offset", "2.5", "--language", "zh"])
    r = json.loads(out.read_text())
    assert r["segments"] == [{"start": 3.5, "end": 4.5, "text": "涨"}] and r["words"][0]["word"] == "涨"


def test_asr_runs_in_a_child_process_and_reports_its_failure(tmp_path, monkeypatch):
    import sys
    from assetizer.media import asr
    from assetizer.providers.agy import LIVE_PGIDS
    wav = tmp_path / "a.wav"
    ok = "import json, sys; json.dump({'segments': [], 'words': [], 'language': 'zh'}, open(sys.argv[1], 'w'))"
    monkeypatch.setattr(asr, "_command", lambda w, out, *a: [sys.executable, "-c", ok, str(out)])
    assert asr.transcribe(wav) == {"segments": [], "words": [], "language": "zh"}
    assert not (tmp_path / "a.wav.asr.json").exists() and not LIVE_PGIDS
    bad = "import sys; print('model file missing', file=sys.stderr); sys.exit(3)"
    monkeypatch.setattr(asr, "_command", lambda w, out, *a: [sys.executable, "-c", bad])
    with pytest.raises(RuntimeError, match="exit 3: model file missing"):
        asr.transcribe(wav)
    assert not LIVE_PGIDS


LONG_LABEL = "unit_18_low_interest_rate_defensive_assets_high_dividend_and_rent"  # 65 chars, 16vTrljdSaE-r4


def _skel_unit(label, depends_on=()):
    return {"label": label, "title": "t", "topic_refs": ["T1"], "focus": "f", "knowledge_type": "CONCEPT",
            "learning_priority": 2, "tickers": [], "depends_on": list(depends_on), "clip_candidate": False, "why": "w"}


def test_fit_labels_shortens_keeps_unique_and_renames_dependencies():
    import re
    from assetizer.pipeline.semantic import fit_labels
    clash = LONG_LABEL[:64]  # what plain truncation would produce
    o = fit_labels({"units": [_skel_unit(clash), _skel_unit(LONG_LABEL), _skel_unit("unit_01_ok", [LONG_LABEL, clash])],
                    "uncertainties": []})
    labels = [u["label"] for u in o["units"]]
    assert labels[0] == clash and labels[2] == "unit_01_ok"  # labels within the limit stay as they are
    assert len(labels[1]) <= P.LABEL_MAX and labels[1].endswith("_2") and len(set(labels)) == 3
    assert all(re.fullmatch(r"[a-z0-9_]{3,64}", x) for x in labels)
    assert o["units"][2]["depends_on"] == [labels[1], clash]
    assert fit_labels(["not", "an", "object"]) == ["not", "an", "object"]


def test_skeleton_with_an_overlong_label_is_mended_before_validation(tmp_path, monkeypatch):
    import types
    from assetizer.llm import LLM
    from assetizer.pipeline.semantic import fit_labels
    text = json.dumps({"units": [_skel_unit(LONG_LABEL)], "uncertainties": []})
    llm = LLM(provider=object())
    monkeypatch.setattr(llm, "_call", lambda *a, **kw: types.SimpleNamespace(text=text))
    with pytest.raises(ContractError, match="does not match"):
        llm.structured(tmp_path / "a", "knowledge-skeleton", "p", P.SKELETON_SCHEMA)
    o = llm.structured(tmp_path / "b", "knowledge-skeleton", "p", P.SKELETON_SCHEMA, normalize=fit_labels)
    assert o["units"][0]["label"] == LONG_LABEL[:64]


# ---------------------------------------------------------------- Telegram notifications
@pytest.fixture
def sent(home, monkeypatch):
    """Telegram configured; captures what would be sent instead of calling the API."""
    from assetizer import config, notify
    s = config.get().secrets_dir
    s.mkdir(parents=True, exist_ok=True)
    (s / "telegram-bot-token").write_text("123:SECRET")
    (s / "telegram-chat-id").write_text("42")
    out = []
    monkeypatch.setattr(notify, "_api", lambda token, method, **kw: out.append(kw["text"]) or {"ok": True})
    return out


def test_notify_is_silent_until_configured(home, monkeypatch):
    from assetizer import notify
    monkeypatch.setattr(notify, "_api", lambda *a, **kw: pytest.fail("must not call Telegram"))
    assert not notify.configured() and notify.send("x") is False


def test_alerts_notify_once_per_occurrence(sent):
    from assetizer import db
    db.raise_alert("YOUTUBE_AUTH", "critical", "YouTube 需要登录", "去 CookieCloud 同步一次")
    db.raise_alert("YOUTUBE_AUTH", "critical", "YouTube 需要登录", "去 CookieCloud 同步一次")  # retried: no repeat
    db.resolve_alert("YOUTUBE_AUTH")
    db.resolve_alert("YOUTUBE_AUTH")
    db.raise_alert("YOUTUBE_AUTH", "critical", "YouTube 需要登录", "again")
    assert len(sent) == 3 and sent[0].startswith("🔴 YouTube 需要登录") and sent[1] == "✅ 已恢复：YouTube 需要登录"


def test_a_failed_run_is_reported_but_a_skipped_one_is_not(sent):
    from assetizer import db
    from assetizer.llm import ContractError
    from assetizer.pipeline import runner
    from assetizer.util import now
    db.exec("INSERT INTO sources(id, title, created_at, updated_at) VALUES ('v','中国往何处去？',?,?)", now(), now())
    for rid in ("v-r1", "v-r2"):
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?, 'v', 'RUNNING', ?)", rid, now())
    try:
        raise ContractError("SCHEMA_VIOLATION", "units/17/label: too long")
    except ContractError as e:
        runner._fail("v-r1", e)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(runner, "_classify", lambda e: "SKIPPED_SHORT")
        runner._fail("v-r2", RuntimeError("short"))
    assert len(sent) == 1 and "中国往何处去？" in sent[0] and "v-r1" in sent[0] and "SCHEMA_VIOLATION" in sent[0]


def test_worker_reports_a_restart_by_systemd_but_not_a_deploy(sent, monkeypatch):
    from assetizer import db
    from assetizer.pipeline import finish, runner
    from assetizer.util import now
    monkeypatch.setattr(runner, "kill_orphans", lambda: 0)
    monkeypatch.setattr(finish, "sweep_agy_state", lambda: None)
    monkeypatch.setattr(runner, "exit_reason", lambda: "内存不足，被系统杀掉")
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('v',?,?)", now(), now())
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','RUNNING',?)", now())
    for restarts in (0, 1, 0, 0, 1):  # deploy, OOM, deploy, deploy, crash
        db.exec("UPDATE runs SET production='RUNNING' WHERE id='v-r1'")
        monkeypatch.setattr(runner, "systemd_restarts", lambda n=restarts: n)
        runner.Worker().recover()
    assert len(sent) == 2 and "内存不足" in sent[0] and "v-r1" in sent[0]


def test_memory_alert_raises_near_the_limit_and_clears_well_below(sent, monkeypatch):
    from assetizer import db, notify
    from assetizer.pipeline import runner
    seen = []
    for used in (5.0, 7.6, 7.8, 6.0, 5.0, 8.0):
        monkeypatch.setattr(runner, "cgroup_memory", lambda u=used: (int(u * 2**30), 9 * 2**30))
        runner.memory_alert()
        seen.append(db.one("SELECT 1 FROM alerts WHERE key='MEMORY_HIGH' AND resolved_at IS NULL") is not None)
    assert seen == [False, True, True, True, False, True]
    notify.send_due_alerts()
    assert sent == []  # a17: not urgent — pushed only if it is still there 10 minutes on


def test_telegram_setup_finds_the_chat_and_keeps_its_id(home, monkeypatch):
    from assetizer import config, notify
    s = config.get().secrets_dir
    s.mkdir(parents=True, exist_ok=True)
    (s / "telegram-bot-token").write_text("123:SECRET")
    updates = []
    calls = []

    def api(token, method, **kw):
        calls.append(method)
        return {"getMe": {"ok": True, "result": {"username": "assetizer_bot"}},
                "getUpdates": {"ok": True, "result": updates},
                "sendMessage": {"ok": True}}[method]
    monkeypatch.setattr(notify, "_api", api)
    with pytest.raises(notify.SetupError, match="@assetizer_bot"):
        notify.setup()
    updates.append({"message": {"chat": {"id": 7001, "first_name": "Demo"}, "text": "hi"}})
    r = notify.setup()
    assert r == {"bot": "assetizer_bot", "chat_id": 7001, "chat": "Demo", "test_sent": True}
    p = s / "telegram-chat-id"
    assert p.read_text() == "7001" and oct(p.stat().st_mode & 0o777) == "0o600" and calls[-1] == "sendMessage"


def test_a_failed_send_is_logged_without_the_token(home, monkeypatch):
    import httpx
    from assetizer import config, db, notify
    s = config.get().secrets_dir
    s.mkdir(parents=True, exist_ok=True)
    (s / "telegram-bot-token").write_text("123:SECRET")
    (s / "telegram-chat-id").write_text("42")

    def boom(url, **kw):
        raise httpx.ConnectError(f"cannot reach {url}")
    monkeypatch.setattr(httpx, "post", boom)
    assert notify.send("x") is False
    e = db.one("SELECT message FROM events WHERE kind='notify'")["message"]
    assert "ConnectError" in e and "SECRET" not in e


def test_daily_digest(sent, monkeypatch):
    import datetime as dt
    from assetizer import db, notify
    from assetizer.util import now
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('v',?,?)", now(), now())
    db.exec("INSERT INTO runs(id, source_id, production, created_at, finished_at) VALUES ('v-r1','v','COMPLETE',?,?), "
            "('v-r2','v','FAILED',?,?)", now(), now(), now(), now())
    db.exec("INSERT INTO runs(id, source_id, production, stage, created_at) VALUES ('v-r3','v','RUNNING','CLIPS',?)", now())
    text = notify.digest()
    assert "完成 1 · 部分完成 0 · 失败 1" in text and "正在跑：v-r3（CLIPS）" in text and "未解决告警：无" in text

    class Clock(dt.datetime):
        hour_now = 8

        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 24, cls.hour_now, 0, tzinfo=tz)
    monkeypatch.setattr(notify.dt, "datetime", Clock)
    assert not notify.digest_due()  # before 9:00
    Clock.hour_now = 9
    assert notify.digest_due()
    notify.send_digest()
    assert not notify.digest_due() and sent[-1].startswith("📋 课代表日报（10-24 09:00）")


def test_digest_goes_at_the_hour_set_in_the_engine_room_and_waits_between_tries(sent, monkeypatch):
    import datetime as dt
    from assetizer import db, notify

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 24, 7, 0, tzinfo=tz)
    monkeypatch.setattr(notify.dt, "datetime", Clock)
    assert not notify.digest_due()  # 7:00, and the default is 9:00
    db.set_setting("digest_hour", 7)
    assert notify.digest_due()
    db.set_setting("digest_tried", "2026-10-24T06:50:00+00:00")  # Telegram did not take it ten minutes ago
    assert not notify.digest_due()
    db.set_setting("digest_tried", "2026-10-24T06:40:00+00:00")
    assert notify.digest_due()


# ---------------------------------------------------------------- LazyCat package (docs/lazycat-lpk-plan.md)
def test_lazycat_gateway_needs_a_logged_in_user(tmp_path, monkeypatch):
    import dataclasses
    from fastapi.testclient import TestClient
    from assetizer import config
    from assetizer.web import app as web
    config.override(dataclasses.replace(config.load(), home=tmp_path))
    monkeypatch.setattr(web, "TRUST_LAZYCAT", True)
    c = TestClient(web.app)
    gateway = {"host": "web:8770", "x-forwarded-host": "assetizer.prodbox.heiyu.space"}  # as sniffed on dev
    user = {**gateway, "x-hc-user-id": "demo"}
    assert c.get("/api/overview", headers=gateway).status_code == 403
    assert c.get("/api/overview", headers=user).status_code == 200
    assert c.get("/api/overview", headers={**user, "x-forwarded-host": "evil.example"}).status_code == 403
    assert c.get("/api/overview", headers={**user, "host": "assetizer.prodbox.heiyu.space"}).status_code == 403
    assert c.get("/healthz", headers={"host": "127.0.0.1:8770"}).json() == {"ok": True}
    monkeypatch.setattr(web, "TRUST_LAZYCAT", False)  # off outside LazyCat: the header alone admits nobody
    assert c.get("/api/overview", headers=user).status_code == 403


def test_container_worker_reports_a_crash_but_not_a_clean_stop(sent, monkeypatch):
    from assetizer import config, db
    from assetizer.pipeline import finish, runner
    from assetizer.util import now
    monkeypatch.setattr(runner, "kill_orphans", lambda: 0)
    monkeypatch.setattr(finish, "sweep_agy_state", lambda: None)
    monkeypatch.setattr(runner, "systemd_restarts", lambda: None)  # no systemd in the container
    monkeypatch.setattr(runner, "container_exit_reason", lambda: "内存不足，被系统杀掉")
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('v',?,?)", now(), now())
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','RUNNING',?)", now())
    run_dir = config.get().home / "run"
    runner.Worker().recover()  # first start ever: nothing ran before
    (run_dir / "worker.heartbeat").touch()
    (run_dir / "worker.clean-exit").touch()  # a SIGTERM stop (upgrade, restart)
    runner.Worker().recover()
    assert sent == [] and not (run_dir / "worker.clean-exit").exists()
    db.exec("UPDATE runs SET production='RUNNING' WHERE id='v-r1'")
    runner.Worker().recover()  # heartbeat but no clean-exit mark: it died
    assert len(sent) == 1 and "内存不足" in sent[0] and "v-r1" in sent[0]


def test_container_exit_reason_counts_oom_kills(home, monkeypatch, tmp_path):
    from assetizer.pipeline import runner
    events = tmp_path / "memory.events"
    monkeypatch.setattr(runner, "CGROUP_MEMORY_EVENTS", events)
    events.write_text("low 0\nhigh 3\nmax 1\noom 1\noom_kill 1\n")
    assert runner.container_exit_reason() == "内存不足，被系统杀掉"
    assert runner.container_exit_reason() == "进程意外退出"  # the same kill is not counted twice
    events.unlink()
    assert runner.container_exit_reason() == "原因不明"


def test_worker_runs_as_many_issues_at_once_as_the_box_allows(home, monkeypatch):
    import threading
    import time
    from assetizer import db, delivery
    from assetizer.pipeline import runner
    from assetizer.util import now
    t = now()
    for v in ("a", "b", "c"):
        db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES (?,'NOT_STARTED',?,?)", v, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?,?,'QUEUED',?)", f"{v}-r1", v, t)
    release = {r: threading.Event() for r in ("a-r1", "b-r1", "c-r1")}
    started, delivered = [], []
    def execute(rid):
        started.append(rid)
        db.exec("UPDATE runs SET production='RUNNING' WHERE id=?", rid)
        release[rid].wait(5)
        db.exec("UPDATE runs SET production='COMPLETE' WHERE id=?", rid)
        return "COMPLETE"
    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(runner.Worker, "housekeeping", lambda self: None)
    monkeypatch.setattr(runner, "apply_nudges", lambda: False)
    monkeypatch.setattr(delivery, "deliver_pending", lambda: delivered.append(threading.current_thread().name) or False)
    w = runner.Worker()
    assert w.tick() and started == ["a-r1"]  # one slot unless the box says otherwise: the old serial worker
    db.set_setting("worker_slots", 2)
    w.tick()
    assert started == ["a-r1", "b-r1"] and not w.tick()  # both slots busy: the third waits
    release["a-r1"].set()
    while w.active["a-r1"].is_alive():
        time.sleep(0.01)
    delivered.clear()
    assert w.tick() and delivered == ["MainThread"]  # a finished issue is delivered by the worker loop, never a run thread
    w.tick()
    assert started == ["a-r1", "b-r1", "c-r1"]
    release["b-r1"].set(), release["c-r1"].set()
    db.set_setting("worker_slots", "many")
    assert runner.worker_slots() == 1
    db.set_setting("worker_slots", 40)
    assert runner.worker_slots() == 16


def test_an_outage_that_outlasts_the_call_retries_holds_the_run_instead_of_failing_it(home, monkeypatch):
    import time
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.providers.agy import ProviderError
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)

    def knowledge(ctx):
        raise ProviderError("PROVIDER_TRANSIENT", 'Eligibility check failed: Post "https://x/loadCodeAssist": EOF')
    monkeypatch.setattr(runner, "STAGES", [("KNOWLEDGE", knowledge, True)])
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT production, wait_reason FROM runs WHERE id='v-r1'") == {"production": "WAITING", "wait_reason": "NETWORK"}
    w = runner.Worker()
    m = time.monotonic()
    w.last.update({k: m for k in w.last}, retry=m - 1801)  # only the periodic retry is due, whatever the uptime
    w.housekeeping()
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "QUEUED"


# ---------------------------------------------------------------- channel page v2: pins, intake settings, preset names
def test_a_pinned_version_survives_newer_runs_until_lock(home):
    from fastapi.testclient import TestClient
    from assetizer import db, tuning
    from assetizer.pipeline import runner
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO sources(id, channel_id, created_at, updated_at) VALUES ('s1','w',?,?)", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?)", t)
    for rid in ("r1", "r2", "r3"):
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?,'s1','COMPLETE',?)", rid, t)
    cur = lambda: db.one("SELECT current_run_id, adopt_pinned FROM sources WHERE id='s1'")  # noqa: E731
    src = db.one("SELECT * FROM sources WHERE id='s1'")
    runner.adopt_completed(src, "r1")
    runner.adopt_completed(src, "r2")
    assert cur() == {"current_run_id": "r2", "adopt_pinned": 0}  # not pinned: the newest finished run
    tuning.adopt("s1", "r1")
    runner.adopt_completed(src, "r3")
    assert cur() == {"current_run_id": "r1", "adopt_pinned": 1}  # pinned: a newer run is only recorded
    tuning.confirm(v2)
    assert cur() == {"current_run_id": "r1", "adopt_pinned": 0}  # after 定稿 a rerun replaces the issue as usual


def test_intake_keeps_its_minimum_and_never_backfills_the_pause(home):
    from fastapi.testclient import TestClient
    from assetizer import db, ingest, tuning
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    assert c.post("/api/channels/w/ingest", json={"enabled": False, "min_minutes": 30}, headers=h).status_code == 200
    tuning.confirm(v2)
    cfg = db.setting("ingest:w")
    assert cfg["enabled"] is True and cfg["min_minutes"] == 30
    db.set_setting("ingest:w", {**cfg, "since": "2026-01-01T00:00:00+00:00"})  # locked long ago
    tuning.retune("w")
    assert db.setting("ingest:w")["min_minutes"] == 30
    ingest.set_enabled("w", True)  # locked again: only what comes out from now on
    cfg = db.setting("ingest:w")
    assert cfg["min_minutes"] == 30 and cfg["since"] > "2026-01-01T00:00:00+00:00"
    ingest.set_enabled("w", True, 45)  # already on: just the minimum changes
    assert db.setting("ingest:w") == {**cfg, "min_minutes": 45}


def test_preset_finds_the_video_link_under_either_name_and_warns_without_one():
    from assetizer.notion import mapping as M
    lib = json.loads(json.dumps(LIB))
    lib["properties"]["参考链接"] = lib["properties"].pop("视频链接")
    schema = M.schema_of(lib)
    cfg = M.preset("wang-haibin", "lib", schema, "https://app.notion.com/p/0000000300008000a000000000000003")
    assert cfg["properties"]["vl"] == {"name": "参考链接", "type": "url", "source": "video_url"}
    assert M.warnings(cfg, schema) == {}
    del cfg["properties"]["vl"]
    assert list(M.warnings(cfg, schema)) == ["video_url"]


# ---------------------------------------------------------------- channel page v2: Notion preview, pinned adoption, 定稿单
class FakeNotion:
    """Reads only: the library database and its pages. Any write is recorded so a test can say there was none."""

    def __init__(self, schema=None):
        self.calls = []
        self.schema = schema or LIB

    def _req(self, method, path, idempotent=None, **kw):
        self.calls.append((method, path))
        if method == "GET" and path == "/databases/lib":
            return {"id": "lib", "url": "https://www.notion.so/lib", "title": [{"plain_text": "知识领袖长期追踪库"}], **self.schema}
        raise AssertionError(f"unexpected Notion call {method} {path}")

    def page(self, pid):
        self.calls.append(("GET", f"/pages/{pid}"))
        return {"id": pid, "icon": {"type": "emoji", "emoji": "📈"},
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "交易员汤姆"}]}}}


def _preview_fixture(home):
    from assetizer import config, db
    from assetizer.notion import mapping as M
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, created_at, updated_at) VALUES ('trader-tom','交易员汤姆','UCt','TRACKED',?,?)", t, t)
    db.exec("""INSERT INTO sources(id, url, channel_id, title, headline, published_at, info, created_at, updated_at)
               VALUES ('s1','https://www.youtube.com/watch?v=s1','trader-tom','09/08/2026 技术分析视频','最后跑完那次的题眼','20260908','{}',?,?)""", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, summary, created_at) VALUES ('r1','s1','COMPLETE',?,?)",
            json.dumps({"units": 2, "model": {"calls": 3}}), t)
    for uid, title, start in [("u1", "MRNA 双重缺口回补", 99.0), ("u2", "TSLA 跳空突破", 915.0)]:
        db.exec("INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, data) VALUES (?,'r1','s1',?,0,?,?,?,'{}')",
                uid, uid, title, start, start + 60)
    for sid, kind, uids in [("secI", "INTRO", []), ("sec1", "UNIT", ["u1"]), ("sec2", "UNIT", ["u2"]), ("secO", "OUTRO", [])]:
        db.exec("INSERT INTO sections(id, run_id, ord, kind, unit_ids, markdown) VALUES (?,'r1',0,?,?,'')", sid, kind, json.dumps(uids))
    run = config.get().runs_dir / "r1"
    (run / "article").mkdir(parents=True)
    shot = run / "shot.jpg"
    shot.write_bytes(b"jpg")
    aid = db.exec("INSERT INTO assets(run_id, source_id, unit_id, kind, status, path, created_at) VALUES ('r1','s1','u1','screenshot','GENERATED',?,?)",
                  str(shot), t)
    (run / "composition.json").write_text(json.dumps({"blocks": [
        {"kind": "INTRO", "section_id": "secI", "markdown": "本期复盘围绕生物科技与半导体。"},
        {"kind": "UNIT", "section_id": "sec1", "unit_id": "u1", "markdown": "## 01. MRNA 双重缺口回补\n两个缺口在同一价格带重叠，**成交量**极度萎缩。",
         "screenshots": [{"path": str(shot), "t": 132, "reason": "日线双缺口"}]},
        {"kind": "UNIT", "section_id": "sec2", "unit_id": "u2", "markdown": "## 02. TSLA 跳空突破\n长周末前减持。"},
        {"kind": "OUTRO", "section_id": "secO", "markdown": "| 优先级 | 时间 |\n|---|---|\n| 1 | 01:39 |"}]}))
    (run / "article" / "outline.json").write_text(json.dumps({"headline": "美股图表读图与交易执行学习导航"}))
    cfg = M.preset("trader-tom", "lib", M.schema_of(LIB), "0000000500008000a000000000000005")
    M.save("trader-tom", {**cfg, "heading": "{channel} - {date8}美股市场复盘"})
    return aid


def test_channel_page_shows_a_renamed_option_by_its_new_name(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import preview as PV
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    _preview_fixture(home)
    lib = _option_ids(LIB)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(lib), "0000000500008000a000000000000005"))
    monkeypatch.setattr(web, "_notion", lambda: FakeNotion(_rename_option(lib, "内容所属", "@交易员汤姆", "Tom")))
    PV.forget()
    r = TestClient(web.app).get("/api/channels/trader-tom/notion", headers={"host": "127.0.0.1"}).json()
    row = next(x for x in r["rows"] if x["id"] == "own")
    assert row["mapping"]["value"] == "Tom" and row["problem"] is None and r["problems"] == 0
    assert M.get("trader-tom")["properties"]["own"]["value"] == "Tom"


def test_notion_preview_reads_notion_again_when_there_are_no_card_edits(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import preview as PV
    from assetizer.web import app as web
    _preview_fixture(home)
    n = FakeNotion()
    monkeypatch.setattr(web, "_notion", lambda: n)
    PV.forget()
    c, h, url = TestClient(web.app), {"host": "127.0.0.1"}, "/api/channels/trader-tom/notion/preview"
    edits = {"run_id": "r1", "config": {"template": "reading-guide"}}
    assert c.post(url, json=edits, headers=h).json()["problems"] == {}
    n.schema = _rename_option(LIB, "内容所属", "@交易员汤姆", "Tom")  # no option id to follow: the option is gone
    assert c.post(url, json=edits, headers=h).json()["problems"] == {}  # card edits go by the cached copy
    assert "own" in c.post(url, json={"run_id": "r1"}, headers=h).json()["problems"]  # opening the page reads Notion
    assert "own" in c.post(url, json=edits, headers=h).json()["problems"]  # ...and refreshes that copy
    n.schema = LIB
    c.get("/api/channels/trader-tom/notion", headers=h)  # so does reading the settings
    assert c.post(url, json=edits, headers=h).json()["problems"] == {}


def test_notion_preview_renders_unsaved_edits_from_one_run_and_never_writes(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import preview as PV
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    aid = _preview_fixture(home)
    n = FakeNotion()
    monkeypatch.setattr(web, "_notion", lambda: n)
    PV.forget()
    saved = M.get("trader-tom")
    props = {**saved["properties"], "title": {"name": "Name", "type": "title", "source": "template", "value": "{channel} · {headline}"},
             "own": {"name": "内容所属", "type": "select", "source": "fixed", "value": "没有这个选项"}}
    c = TestClient(web.app)
    r = c.post("/api/channels/trader-tom/notion/preview", json={"run_id": "r1", "config": {"properties": props}},
               headers={"host": "127.0.0.1"}).json()
    # 本期题眼 comes from this run's outline, not from whichever run of the issue finished last
    assert r["title"] == [{"text": "交易员汤姆", "var": "channel"}, {"text": " · "}, {"text": "美股图表读图与交易执行学习导航", "var": "headline"}]
    assert r["heading"] == [{"text": "交易员汤姆", "var": "channel"}, {"text": " - "}, {"text": "20260908", "var": "date8"}, {"text": "美股市场复盘"}]
    assert r["icon"] == {"emoji": "📈"}
    assert r["problems"] == {"own": "「内容所属」没有选项「没有这个选项」"}
    rows = {p["id"]: p for p in r["properties"]}
    assert rows["up"]["value"] == [{"id": "00000005-0000-8000-a000-000000000005", "title": "交易员汤姆"}]
    assert rows["acc"]["value"] == "公开视频" and rows["ct"]["writable"] is False
    by_key = {b["key"]: b for b in r["blocks"]}
    assert by_key["src"]["origin"] == "frame" and by_key["src"]["tpl"] is True
    assert by_key["sec1:h"] == {"type": "heading_3", "text": [{"t": "01. MRNA 双重缺口回补"}], "key": "sec1:h",
                                "origin": "unit", "unit_ids": ["u1"], "tpl": False}
    assert by_key["sec1:shot0"]["type"] == "image" and by_key["sec1:shot0"]["asset_id"] == aid
    assert any(s.get("b") and s["t"] == "成交量" for b in r["blocks"] if b["key"].startswith("sec1:p") for s in b.get("text", []))
    assert by_key["runinfo"]["origin"] == "tail" and by_key["runinfo"]["children"]
    assert all(m == "GET" for m, _ in n.calls)
    assert M.get("trader-tom") == saved  # a preview never saves


def test_adopt_pins_or_follows_the_newest_finished_run(home):
    from fastapi.testclient import TestClient
    from assetizer import db, tuning
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO sources(id, channel_id, current_run_id, created_at, updated_at) VALUES ('s1','w','r2',?,?)", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?)", t)
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at, finished_at) VALUES ('r1','s1',?,'COMPLETE',?,'2026-10-01')", v1, t)
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at, finished_at) VALUES ('r2','s1',?,'COMPLETE',?,'2026-10-02')", v2, t)
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    assert c.post("/api/sources/s1/adopt", json={"run_id": "r1"}, headers=h).json() == {"ok": True, "run_id": "r1", "pinned": True}
    wb = tuning.workbench("w")
    assert wb["samples"][0]["adopt"] == {"run_id": "r1", "pinned": True}
    assert {p["version"]: p["adopt_count"] for p in wb["playbooks"]} == {1: 1, 2: 0}
    assert c.post("/api/sources/s1/adopt", json={"pinned": False}, headers=h).json() == {"ok": True, "run_id": "r2", "pinned": False}
    assert db.one("SELECT current_run_id, adopt_pinned FROM sources WHERE id='s1'") == {"current_run_id": "r2", "adopt_pinned": 0}


def test_headline_follows_the_adopted_run(home):
    from fastapi.testclient import TestClient
    from assetizer import config, db
    from assetizer.pipeline.runner import adopt_completed
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO sources(id, channel_id, title, current_run_id, adopt_pinned, created_at, updated_at) VALUES ('s1','w','原标题','r1',1,?,?)", t, t)
    for rid, pb, head in (("r1", v1, "旧题眼"), ("r2", v2, "新题眼")):
        db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at, finished_at) VALUES (?,'s1',?,'COMPLETE',?,?)", rid, pb, t, t)
        (config.get().runs_dir / rid / "article").mkdir(parents=True)
        (config.get().runs_dir / rid / "article" / "outline.json").write_text(json.dumps({"headline": head}))
    row = lambda: db.one("SELECT current_run_id, headline FROM sources WHERE id='s1'")  # noqa: E731
    adopt_completed(db.one("SELECT * FROM sources WHERE id='s1'"), "r2")  # pinned: a trial that finishes does not take over
    assert row() == {"current_run_id": "r1", "headline": None}
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    assert c.get("/api/sources/s1?run=r2", headers=h).json()["source"]["headline"] == "新题眼"
    db.exec("UPDATE sources SET adopt_pinned=0 WHERE id='s1'")
    adopt_completed(db.one("SELECT * FROM sources WHERE id='s1'"), "r2")
    assert row() == {"current_run_id": "r2", "headline": "新题眼"}
    c.post("/api/sources/s1/adopt", json={"run_id": "r1"}, headers=h)
    assert row() == {"current_run_id": "r1", "headline": "旧题眼"}


def _lock_fixture():
    from assetizer import db
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    v3 = db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('w',3,'NEEDS_CONFIRMATION','{}',?)", now())
    t = now()
    # s1 new to the library · s2 a page Assetizer wrote · s3 still running a newer version · s4 the user's own library page
    for sid, page, cur, pb in [("s1", None, "r1", v2), ("s2", "pg-2", "r2", v1), ("s3", None, "r3", v1), ("s4", None, "r5", v1)]:
        db.exec("INSERT INTO sources(id, channel_id, title, notion_page_id, current_run_id, created_at, updated_at) VALUES (?,'w',?,?,?,?,?)",
                sid, f"样片{sid}", page, cur, t, t)
        db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w',?,?)", sid, t)
        db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at) VALUES (?,?,?,'COMPLETE',?)", cur, sid, pb, t)
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at) VALUES ('r4','s3',?,'RUNNING',?)", v2, t)
    return v1, v2, v3


def test_media_of_runs_made_elsewhere_resolve_through_the_assets_table(home, monkeypatch):
    """composition.json keeps the absolute paths of the machine a run was made on (OVH before the move to the box);
    the page plan and the preview take each clip and screenshot from the assets table instead."""
    from fastapi.testclient import TestClient
    from assetizer import config, db, delivery
    from assetizer import preview as PV
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    aid = _preview_fixture(home)
    p = config.get().runs_dir / "r1" / "composition.json"
    comp = json.loads(p.read_text())
    comp["blocks"][1]["screenshots"][0].update(path="/home/debian/assetizer/data/runs/r1/shot.jpg", asset_id=aid)
    p.write_text(json.dumps(comp))
    shot = str(config.get().runs_dir / "r1" / "shot.jpg")
    src = db.one("SELECT * FROM sources WHERE id='s1'")
    for target in ({}, M.get("trader-tom")):  # the default page and the channel's template
        assert [it["media"]["path"] for it in delivery.build_plan(src, "r1", target) if it.get("media")] == [shot]
    monkeypatch.setattr(web, "_notion", lambda: FakeNotion())
    PV.forget()
    r = TestClient(web.app).post("/api/channels/trader-tom/notion/preview", json={"run_id": "r1", "config": {}},
                                 headers={"host": "127.0.0.1"}).json()
    assert next(b for b in r["blocks"] if b["key"] == "sec1:shot0")["asset_id"] == aid


def test_lock_preview_says_what_each_sample_would_get(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import delivery
    from assetizer.web import app as web
    v1, v2, v3 = _lock_fixture()
    monkeypatch.setattr(delivery, "registered_many", lambda src, ids: {
        "s2": {"id": "pg2", "url": "https://notion.so/pg2", "title": "样片s2"}, "s4": {"id": "pg-x", "url": "https://notion.so/x", "title": "旧页"}})
    r = TestClient(web.app).get(f"/api/channels/w/lock-preview?playbook_id={v2}", headers={"host": "127.0.0.1"}).json()
    rows = {s["source_id"]: s for s in r["samples"]}
    assert r["tested"] is True and r["library_checked"] is True
    assert rows["s1"]["library"] is None and rows["s1"]["adopted"] == {"run_id": "r1", "version": 2, "pinned": False}
    assert rows["s2"]["library"]["owned"] is True and rows["s2"]["can_replace"] is True
    assert rows["s4"]["library"]["owned"] is False and rows["s4"]["can_replace"] is False
    assert rows["s3"]["running"] == {"run_id": "r4", "version": 2, "state": "RUNNING"}
    assert {c["version"]: c["tested"] for c in r["candidates"]} == {3: False, 2: True, 1: True}


def test_lock_writes_keeps_replaces_or_waits_per_sample_all_or_nothing(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.web import app as web
    v1, v2, v3 = _lock_fixture()
    db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, status, created_at) VALUES ('w','s1',?,'PENDING','2026-10-26')", v3)
    queued = lambda: db.one("SELECT status FROM trial_queue WHERE channel_id='w'")["status"]  # noqa: E731
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    lock = lambda pid, samples, ingest=None: c.post("/api/channels/w/lock", headers=h,  # noqa: E731
                                                     json={"playbook_id": pid, "samples": samples, "ingest": ingest})
    r = lock(v3, [])
    assert r.status_code == 409 and "还没在样片上跑过" in r.json()["detail"]
    r = lock(v2, [{"source_id": "s4", "action": "replace"}])
    assert r.status_code == 409 and "只能保留" in r.json()["detail"]
    assert lock(v2, [{"source_id": "s1", "action": "wait"}]).status_code == 409
    assert db.one("SELECT stage FROM channels WHERE id='w'")["stage"] == "TUNING" and queued() == "PENDING"  # refused: nothing changed
    r = lock(v2, [{"source_id": "s2", "action": "replace"}, {"source_id": "s3", "action": "wait"}, {"source_id": "s4", "action": "keep"}],
             {"enabled": True, "min_minutes": 20})
    assert r.status_code == 200 and r.json()["write"] == 2 and r.json()["keep"] == 1 and r.json()["wait"] == 1
    src = {s["id"]: s for s in db.q("SELECT id, delivery, delivery_reason, adopt_pinned, deliver_after_run, current_run_id FROM sources")}
    assert db.one("SELECT stage FROM channels WHERE id='w'")["stage"] == "LOCKED" and queued() == "CANCELLED"
    assert db.one("SELECT status FROM playbooks WHERE id=?", v2)["status"] == "ACTIVE"
    assert src["s1"]["delivery"] == "NOT_STARTED" and src["s1"]["adopt_pinned"] == 1
    assert src["s2"]["delivery"] == "STALE"
    assert (src["s3"]["delivery"], src["s3"]["deliver_after_run"], src["s3"]["adopt_pinned"]) == ("HELD", "r4", 0)
    assert (src["s4"]["delivery"], src["s4"]["delivery_reason"]) == ("REGISTERED", "库里已有，定稿时选择保留")
    assert db.setting("ingest:w")["min_minutes"] == 20
    # the awaited run finishes: it becomes the issue's version and the hold is gone
    runner.adopt_completed(db.one("SELECT * FROM sources WHERE id='s3'"), "r4")
    assert db.one("SELECT current_run_id, deliver_after_run FROM sources WHERE id='s3'") == {"current_run_id": "r4", "deliver_after_run": None}


def test_a_held_sample_falls_back_to_its_adopted_version_when_the_run_fails(home):
    from assetizer import db, tuning
    from assetizer.pipeline import runner
    v1, v2, v3 = _lock_fixture()
    tuning.lock("w", v2, [{"source_id": "s3", "action": "wait"}])
    runner._fail("r4", RuntimeError("片段超时"))
    assert db.one("SELECT current_run_id, delivery, deliver_after_run FROM sources WHERE id='s3'") == \
        {"current_run_id": "r3", "delivery": "NOT_STARTED", "deliver_after_run": None}


def test_retune_drops_holds(home):
    from assetizer import db, tuning
    v1, v2, v3 = _lock_fixture()
    tuning.lock("w", v2, [{"source_id": "s3", "action": "wait"}])
    tuning.retune("w")
    assert db.one("SELECT delivery, deliver_after_run FROM sources WHERE id='s3'") == {"delivery": "NOT_STARTED", "deliver_after_run": None}


# ---------------------------------------------------------------- channel page v2: 稿纸, workbench tracks, compare by content, retarget
def test_clauses_remember_where_they_stand_in_the_prompt():
    from assetizer.compiler import split_clauses
    raw = "# 一、目标\n\n我想学习**读图**。不要硬凑数量；\n\n例如：\n* “这个缺口”\n"
    cl = split_clauses(raw)
    assert [raw[c["start"]:c["end"]] for c in cl] == ["我想学习**读图**。", "不要硬凑数量；", "例如：\n* “这个缺口”"]


def _annotated_playbooks():
    from assetizer import db
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, created_at, updated_at) VALUES ('tom','汤姆','TRACKED',?,?)", t, t)
    text = "# 一、学习地图\n\n找出**读图片段**。不要硬凑数量。\n\n简单报涨跌幅可以忽略。\n\n处理完后保存成文件。"
    pr = db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, created_at) VALUES ('tom',1,?,'h',?)", text, t)
    old = {"rules": [{"id": "rule_count", "rule": "根据实际值得学习的片段数量输出，不硬凑"}]}
    new = {"rules": [{"id": "rule_count", "label": "选段与数量", "rule": "根据实际值得学习的片段数量输出，不按固定数量硬凑"},
                     {"id": "rule_filter", "rule": "忽略简单报涨跌幅与闲聊"}],
           "facets": [{"key": "tasks", "label": "观察任务", "type": "list", "description": "截图后看哪里", "clause_ids": ["C001"]}]}
    audit = {"clauses": [{"id": "C001", "text": "找出读图片段。", "section": "一、学习地图"},
                         {"id": "C002", "text": "不要硬凑数量。", "section": "一、学习地图"},
                         {"id": "C003", "text": "简单报涨跌幅可以忽略。", "section": "一、学习地图"},
                         {"id": "C004", "text": "处理完后保存成文件。", "section": "一、学习地图"}],
             "clause_map": [{"clause_id": "C001", "disposition": "RULE", "target": "rule_count", "reason": ""},
                            {"clause_id": "C002", "disposition": "RULE", "target": "rule_count", "reason": ""},
                            {"clause_id": "C003", "disposition": "RULE", "target": "rule_filter", "reason": ""},
                            {"clause_id": "C004", "disposition": "ORCHESTRATION", "target": "", "reason": "保存路径由系统承担"}],
             "policy_conflicts": []}
    db.exec("INSERT INTO playbooks(channel_id, version, status, data, audit, origin, created_at) VALUES ('tom',1,'ACTIVE',?,'{}','seed:run-014',?)",
            json.dumps(old), t)
    return db.exec("INSERT INTO playbooks(channel_id, prompt_id, version, status, data, audit, origin, created_at) VALUES ('tom',?,2,'NEEDS_CONFIRMATION',?,?,'compiler:p1',?)",
                   pr, json.dumps(new), json.dumps(audit), t), text


def test_annotations_place_each_clause_in_the_prompt_and_name_what_changed(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    pid, text = _annotated_playbooks()
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    a = c.get(f"/api/playbooks/{pid}/annotations", headers=h).json()
    assert a["has_provenance"] is True and a["prompt"]["version"] == 1
    assert [text[x["start"]:x["end"]] for x in a["clauses"]] == ["找出**读图片段**。", "不要硬凑数量。", "简单报涨跌幅可以忽略。", "处理完后保存成文件。"]
    assert [(x["disposition"], x["rule_id"]) for x in a["clauses"]][-1] == ("ORCHESTRATION", None)
    assert [r["label"] for r in a["rules"]] == ["选段与数量", "忽略简单报涨跌幅…"]  # no short name yet: its opening words
    assert a["coverage"] == {"clauses": 4, "used": 3, "system": 1, "redundant": 0, "ignored": 0}
    assert [x["to"]["label"] for x in a["vs_previous"]["changed"]] == ["选段与数量"]
    assert [x["label"] for x in a["vs_previous"]["added"]] == ["忽略简单报涨跌幅…"] and a["vs_previous"]["removed"] == []
    imported = db.one("SELECT id FROM playbooks WHERE version=1")["id"]
    assert c.get(f"/api/playbooks/{imported}/annotations", headers=h).json()["has_provenance"] is False
    assert c.get("/api/playbooks/999/annotations", headers=h).status_code == 404


def test_recompile_drafts_a_new_version_from_the_same_goal(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import compiler, db
    from assetizer.util import now
    from assetizer.web import app as web
    drafted, _ = _annotated_playbooks()
    imported = db.one("SELECT id FROM playbooks WHERE version=1")["id"]
    calls, later = [], []
    monkeypatch.setattr(web, "_bg", lambda fn, *a, label="": later.append((fn, a)))
    monkeypatch.setattr(compiler, "compile_prompt", lambda cid, pid, again=False: calls.append((cid, pid, again)) or db.exec(
        "INSERT INTO playbooks(channel_id, prompt_id, version, status, data, audit, origin, created_at) "
        "VALUES (?,?,3,'NEEDS_CONFIRMATION','{}','{}','compiler:p1',?)", cid, pid, now()))
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    goal = db.one("SELECT id FROM channel_prompts WHERE channel_id='tom'")["id"]
    assert c.post(f"/api/playbooks/{imported}/recompile", headers=h, json={}).json() == {"prompt_id": goal, "compiling": True}
    tom = lambda: next(x for x in c.get("/api/channels", headers=h).json()["channels"] if x["id"] == "tom")  # noqa: E731
    assert tom()["compile"]["state"] == "running"  # the goal already has v2, so only a newer version means done
    assert c.post(f"/api/playbooks/{drafted}/recompile", headers=h, json={}).status_code == 409
    fn, args = later[0]
    fn(*args)
    assert calls == [("tom", goal, True)] and tom()["compile"]["state"] == "done"
    a = c.get(f"/api/playbooks/{drafted}/annotations", headers=h).json()
    assert a["has_labels"] is False  # rule_filter has no short name of its own
    assert c.post("/api/playbooks/999/recompile", headers=h, json={}).status_code == 404
    uni = db.exec("INSERT INTO playbooks(channel_id, version, status, data, audit, origin, created_at) VALUES (NULL,1,'ACTIVE','{}','{}','seed:universal',?)", now())
    assert c.post(f"/api/playbooks/{uni}/recompile", headers=h, json={}).status_code == 400


V7_SPANS = [[0, 605], [605, 1015], [1015, 1600], [1600, 1858], [1858, 2257], [2257, 2580], [2580, 2865], [2865, 2890], [2890, 3037],
            [3037, 3075], [3075, 3147], [3147, 3249], [3249, 3336], [3336, 3382], [3382, 3448], [3448, 3515], [3515, 3610], [3610, 3767]]
V8_SPANS = [[0, 605], [605, 1020], [1020, 1620], [1620, 2100], [2100, 2400], [2400, 2864], [2865, 2889], [2890, 3037], [3038, 3075],
            [3076, 3147], [3148, 3197], [3198, 3248], [3249, 3335], [3336, 3382], [3383, 3448], [3449, 3514], [3515, 3610], [3611, 3650],
            [3651, 3707], [3708, 3767]]


def test_compare_names_split_merge_new_and_gone_units():
    from assetizer.tuning import changes
    A = [{"start": a, "end": b} for a, b in V7_SPANS]
    B = [{"start": a, "end": b} for a, b in V8_SPANS]
    old, new = changes(A, B)
    assert (new.count("new"), new.count("split"), new.count("merge"), old.count("gone")) == (1, 5, 1, 1)
    assert old[5] == "gone" and new[4] == "new" and new[3] == "merge" and old[11] == "split"


def test_workbench_draws_tracks_and_estimates_the_missing_trials(home):
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO sources(id, channel_id, duration, created_at, updated_at) VALUES ('s1','w',3600,?,?), ('s2','w',1800,?,?)", t, t, t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?), ('w','s2',?)", t, t)
    db.exec("""INSERT INTO runs(id, source_id, playbook_id, production, created_at, started_at, finished_at)
               VALUES ('r1','s1',?,'COMPLETE',?,'2026-10-01T10:00:00+00:00','2026-10-01T11:30:00+00:00')""", v1, t)
    db.exec("INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, learning_priority, data) VALUES ('u1','r1','s1','u1',0,'缺口',0,600,3,'{}')")
    db.exec("""INSERT INTO flags(source_id, run_id, target_kind, target_id, kind, severity, title, origin, created_at)
               VALUES ('s1','r1','UNIT','u1','AI_X','REVIEW','边界',  'AI_CHECK',?)""", t)
    wb = tuning.workbench("w")
    run = wb["samples"][0]["runs"][0]
    assert run["track"] == [[0.0, 600.0, 3]] and run["flag_at"] == [300.0] and run["eta_seconds"] is None
    per = {p["version"]: p for p in wb["playbooks"]}
    assert per[1]["missing"] == 1 and per[2]["missing"] == 2
    # 90 minutes for a 60-minute video: 1.5 min per video minute → s2 (30 min) is 45, both samples 135
    assert per[1]["est_minutes"] == 45 and per[2]["est_minutes"] == 135


def test_retarget_carries_mappings_by_name_and_asks_to_repick_relations(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import db, preview as PV
    from assetizer.notion import mapping as M
    from assetizer.util import now
    from assetizer.web import app as web
    _preview_fixture(home)
    lib2 = {"properties": {name: {**p, "id": p["id"] + "2"} for name, p in LIB["properties"].items()}}

    class Two(FakeNotion):
        def _req(self, method, path, idempotent=None, **kw):
            if method == "GET" and path == "/databases/11111111-2222-3333-4444-555555555555":
                self.calls.append((method, path))
                return {"id": "11111111-2222-3333-4444-555555555555", "url": "u", "title": [{"plain_text": "知识领袖长期追踪库"}], **lib2}
            return super()._req(method, path, idempotent, **kw)
    monkeypatch.setattr(web, "_notion", lambda: Two())
    PV.forget()
    db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_MAPPING：x' WHERE id='s1'")
    r = TestClient(web.app).post("/api/channels/trader-tom/notion/target", headers={"host": "127.0.0.1"},
                                 json={"database": "https://www.notion.so/111111112222333344445555555555555", "is_test_copy": False}).json()
    cfg = M.get("trader-tom")
    assert cfg["database_id"] == "11111111-2222-3333-4444-555555555555" and cfg["is_test_copy"] is False
    assert set(cfg["properties"]) == {"title2", "own2", "acc2", "date2", "up2", "sum2", "vl2"}
    assert "value" not in cfg["properties"]["up2"] and r["repick"] == ["Re-上级 项目"] and r["test_copy"] is False
    assert db.one("SELECT delivery FROM sources WHERE id='s1'")["delivery"] == "NOT_STARTED"
    assert r["dropped"] == []
    assert now()  # keeps the import used


def test_retarget_goes_by_current_names_and_names_what_it_drops(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import preview as PV
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    _preview_fixture(home)
    old = _option_ids(LIB)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(old), "0000000500008000a000000000000005"))
    # renamed in the old database after the mapping was saved; the new one already has the new name, no AI 摘要
    old["properties"]["作者"] = old["properties"].pop("内容所属")
    new = _option_ids({"properties": {name: {**p, "id": p["id"] + "2"} for name, p in old["properties"].items() if name != "AI 摘要"}}, "new-")

    class Two(FakeNotion):
        def _req(self, method, path, idempotent=None, **kw):
            if method == "GET" and path == "/databases/11111111-2222-3333-4444-555555555555":
                return {"id": "11111111-2222-3333-4444-555555555555", "url": "u", "title": [{"plain_text": "测试副本"}], **new}
            return super()._req(method, path, idempotent, **kw)
    monkeypatch.setattr(web, "_notion", lambda: Two(old))
    PV.forget()
    r = TestClient(web.app).post("/api/channels/trader-tom/notion/target", headers={"host": "127.0.0.1"},
                                 json={"database": "11111111-2222-3333-4444-555555555555", "is_test_copy": True}).json()
    props = M.get("trader-tom")["properties"]
    assert props["own2"] == {"name": "作者", "type": "select", "source": "fixed", "value": "@交易员汤姆",
                             "option_ids": {"@交易员汤姆": "new-@交易员汤姆"}}  # pinned again in the new database
    assert "sum2" not in props and r["dropped"] == ["AI 摘要"] and r["repick"] == ["Re-上级 项目"]


def test_channels_report_paused_deliveries_and_recent_issues(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    _preview_fixture(home)
    db.exec("UPDATE sources SET delivery='BLOCKED', delivery_reason='NOTION_MAPPING：会员/公开没有选项' WHERE id='s1'")
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    ch = next(x for x in c.get("/api/channels", headers=h).json()["channels"] if x["id"] == "trader-tom")
    assert ch["blocked"] == 1 and ch["held"] == 0 and ch["ingest"]["every_minutes"] == 30
    db.exec("UPDATE sources SET current_run_id='r1' WHERE id='s1'")
    issues = c.get("/api/channels/trader-tom/recent", headers=h).json()["issues"]
    assert [(i["id"], i["units"], i["notes"]) for i in issues] == [("s1", 2, 0)]


# ---------------------------------------------------------------- 兔子 API provider
@pytest.fixture
def fake_tuzi(home, monkeypatch):
    """A local stand-in for api.tu-zi.com's Gemini streaming endpoint; set ``reply`` to (status, body) per test."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assetizer.providers import tuzi
    state = {"requests": [], "reply": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            status, payload = state["reply"]
            data = payload.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(tuzi, "BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets" / "tuzi-api-key").write_text("sk-test")
    yield state
    httpd.shutdown()


def _sse(*chunks):
    import json
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)


def test_tuzi_streams_the_answer_with_the_video_inline_and_the_schema_enforced(fake_tuzi, home):
    import base64
    from assetizer.providers.tuzi import MEDIA_NOTE, TuziProvider
    clip = home / "chunk.mp4"
    clip.write_bytes(b"video-bytes")
    fake_tuzi["reply"] = (200, _sse(
        {"candidates": [{"content": {"parts": [{"text": "想一想", "thought": True}, {"text": '{"ok": '}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "true}"}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 7, "thoughtsTokenCount": 40}}))
    r = TuziProvider().call(home / "call", "用 view_file 读取视频", kind="understand", media=[str(clip)],
                            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}}, effort="medium")
    assert r.text == '{"ok": true}' and r.receipt["status"] == "SUCCESS"
    assert r.receipt["usage"] == {"input_tokens": 900, "output_tokens": 7, "thinking_tokens": 40}
    req = fake_tuzi["requests"][0]
    assert req["path"].startswith("/v1beta/models/gemini-3.8-flash:streamGenerateContent") and req["auth"] == "Bearer sk-test"
    media, text = req["body"]["contents"][0]["parts"]
    assert base64.b64decode(media["inline_data"]["data"]) == b"video-bytes" and media["inline_data"]["mime_type"] == "video/mp4"
    assert text["text"].endswith(MEDIA_NOTE)
    cfg = req["body"]["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingLevel": "medium"} and cfg["responseMimeType"] == "application/json"
    again = TuziProvider().call(home / "call", "用 view_file 读取视频", kind="understand", media=[str(clip)])
    assert again.reused and len(fake_tuzi["requests"]) == 1  # a finished call is never paid for twice


@pytest.mark.parametrize("status,body,kind", [
    (401, '{"error": {"message": "invalid token"}}', "AGY_AUTH_REQUIRED"),
    (429, '{"error": {"message": "upstream saturated"}}', "PROVIDER_TRANSIENT"),
    (403, '{"error": {"message": "用户额度不足, 剩余额度: ¥0.01"}}', "PROVIDER_QUOTA"),
    (400, '{"error": {"message": "insufficient_user_quota"}}', "PROVIDER_QUOTA"),
    (502, "bad gateway", "PROVIDER_TRANSIENT"),
])
def test_tuzi_http_errors_map_to_the_pipeline_kinds(fake_tuzi, home, status, body, kind):
    from assetizer.providers.agy import ProviderError
    from assetizer.providers.tuzi import TuziProvider
    fake_tuzi["reply"] = (status, body)
    with pytest.raises(ProviderError) as e:
        TuziProvider().call(home / "call", "hi", kind="t")
    assert e.value.kind == kind and e.value.receipt["status"] == "FAILED"


def test_tuzi_cut_off_at_the_output_limit_is_an_output_limit(fake_tuzi, home):
    from assetizer.providers.agy import ProviderError
    from assetizer.providers.tuzi import TuziProvider
    fake_tuzi["reply"] = (200, _sse({"candidates": [{"content": {"parts": [{"text": "很长"}]}, "finishReason": "MAX_TOKENS"}]}))
    with pytest.raises(ProviderError) as e:
        TuziProvider().call(home / "call", "hi", kind="t")
    assert e.value.kind == "PROVIDER_OUTPUT_LIMIT"


def test_tuzi_stream_cut_before_its_finish_reason_is_retried_not_taken(fake_tuzi, home):
    from assetizer.providers.agy import ProviderError
    from assetizer.providers.tuzi import TuziProvider
    fake_tuzi["reply"] = (200, _sse({"candidates": [{"content": {"parts": [{"text": '{"selections": [{"unit'}]}}]}))
    with pytest.raises(ProviderError) as e:
        TuziProvider().call(home / "call", "hi", kind="selection")
    assert e.value.kind == "PROVIDER_TRANSIENT" and e.value.transient


def test_the_box_setting_picks_the_provider(home):
    from assetizer import db, providers
    from assetizer.providers.agy import AgyProvider
    from assetizer.providers.tuzi import TuziProvider
    assert isinstance(providers.get(), AgyProvider)
    db.set_setting("llm_provider", "tuzi")
    assert isinstance(providers.get(), TuziProvider) and providers.get().version() == "tuzi:gemini-3.8-flash"


def test_a_rejected_answer_is_regenerated_with_the_reason_it_was_rejected(home):
    import json
    from assetizer.llm import LLM, ContractError
    from assetizer.providers.agy import CallResult
    prompts = []

    class Echo:  # answers in full only when told what the last answer lacked
        def call(self, call_dir, prompt, **kw):
            prompts.append(prompt)
            n = 3 if "上一次的输出没有通过校验" in prompt else 2
            return CallResult(json.dumps({"items": list(range(n))}), {"status": "SUCCESS"})

    def check(o):
        if len(o["items"]) < 3:
            raise ContractError("MISSING_UNITS", f"{len(o['items'])}/3 items")

    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "integer"}}}, "required": ["items"]}
    o = LLM(provider=Echo()).structured(home / "calls", "selection", "选出全部单元", schema, check=check)
    assert o == {"items": [0, 1, 2]} and len(prompts) == 2
    assert "MISSING_UNITS: 2/3 items" in prompts[1] and "上一次的输出没有通过校验" not in prompts[0]


# ---------------------------------------------------------------- weekly benchmark
def test_bench_score_finds_units_by_time_and_ticker():
    from assetizer.bench.quality import score
    units = [{"tickers": ["NTLA"], "start": 0, "end": 98}, {"tickers": ["MRNA"], "start": 99, "end": 163},
             {"tickers": [], "start": 1000, "end": 1100}, {"tickers": ["TSLA"], "start": 2000, "end": 2100}]
    topics = [{"tickers": ["ntla", "TEM"], "start": 10, "end": 90}, {"tickers": ["AAPL"], "start": 100, "end": 160},
              {"tickers": [], "start": 1150, "end": 1300}, {"tickers": ["TSLA"], "start": 2300, "end": 2400}]
    s = score(units, topics)
    # NTLA by ticker and time; MRNA overlaps only an AAPL topic; the ticker-less unit by time within the 60 s window;
    # TSLA's topic starts 200 s after the unit ends
    assert s == {"recall": 0.5, "start_error_median": 80.0, "within_60s": 0.25}


def test_bench_chunks_the_video_like_the_pipeline():
    from assetizer.bench.quality import chunk_plan
    assert chunk_plan(3504) == [(0.0, 3000), (2700, 804)]
    assert chunk_plan(1800) == [(0.0, 1800)]


def test_bench_golden_files_hold_together():
    from assetizer.bench import golden
    gs = golden()
    assert gs and all(g["url"].endswith(g["id"]) for g in gs)
    for g in gs:
        ids = {u["id"] for u in g["units"]}
        assert all(0 <= u["start"] < u["end"] <= g["duration"] + 1 for u in g["units"])
        assert all(b["unit"] in ids and b["start"] < b["end"] for b in g["boundaries"])


class _BenchProvider:
    """Reads the canary number back while the clip is small enough, and answers the long list in full only when the
    schema is enforced."""

    def __init__(self, limit_mb):
        self.limit = limit_mb

    def version(self):
        return "fake"

    def call(self, call_dir, prompt, *, kind, schema=None, media=None, effort=None, timeout=None):
        import json
        from pathlib import Path
        from assetizer.providers.agy import CallResult, ProviderError
        if media:
            if Path(media[0]).stat().st_size > self.limit * 1024 * 1024:
                raise ProviderError("PROVIDER_FAILURE", "HTTP 413: request too large")
            return CallResult(Path(media[0]).with_suffix(".code").read_text(), {"wall_seconds": 1, "usage": {"input_tokens": 900}})
        n = 300 if schema else 120
        return CallResult(json.dumps({"items": [{"n": i, "text": "要点"} for i in range(1, n + 1)]}),
                          {"wall_seconds": 2, "usage": {"output_tokens": n * 150}})


def test_bench_probes_measure_the_size_limit_and_the_output_length(home, monkeypatch):
    from assetizer.bench import probes
    monkeypatch.setattr(probes, "_canary_clip", lambda path, code, size: (path.write_bytes(b"x" * size),
                                                                          path.with_suffix(".code").write_text(code)))
    r = probes.run_all(_BenchProvider(limit_mb=50), home / "probes")
    assert [x["ok"] for x in r["media_size"]["rows"]] == [True, True, False] and r["media_size"]["largest_ok_mb"] == 48
    assert "413" in r["media_size"]["rows"][2]["error"]
    ol = r["output_length"]
    assert ol["enforced"]["complete"] and not ol["embedded"]["complete"] and ol["embedded"]["items"] == 120


def test_bench_report_compares_with_last_week_and_says_what_could_go(home):
    from assetizer.bench import report
    q = lambda wr, cr: {"units": 30, "variants": {  # noqa: E731
        "whole": {"recall": wr, "start_error_median": 40.0, "within_60s": 0.6, "topics": 20, "calls": 1, "input_tokens": 1, "wall": 1},
        "chunked": {"recall": cr, "start_error_median": 38.0, "within_60s": 0.7, "topics": 28, "calls": 2, "input_tokens": 1, "wall": 1}}}
    probes = {"media_size": {"rows": [{"mb": 48, "ok": True}], "largest_ok_mb": 48},
              "output_length": {"items_asked": 300, "enforced": {"complete": True, "output_tokens": 45000}, "embedded": {"complete": False}}}
    report.write({"stamp": "2026-10-27_1000", "provider": "fake", "probes": probes, "quality": {"vid": q(0.4, 0.9)}}, home / "reports")
    r = report.write({"stamp": "2026-11-03_1000", "provider": "fake", "probes": probes, "quality": {"vid": q(0.88, 0.9)}}, home / "reports")
    md = (home / "reports" / "2026-11-03_1000.md").read_text()
    assert "对比上次 2026-10-27_1000" in md and "88%（上次 40%）" in md
    said = dict(report.verdicts(r))
    assert "放宽" in said["C1"] and "取消分批" in said["C4"] and "强制 schema" in said["C5"] and "追平" in said["C2/C3"]


# ---------------------------------------------------------------- magpie: an image model in place of the video model
@pytest.fixture
def fake_magpie(home, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assetizer import db
    from assetizer.providers import magpie
    state = {"requests": [], "reply": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            state["requests"].append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            status, payload = state["reply"]
            data = payload.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    db.set_setting("magpie_url", f"http://127.0.0.1:{httpd.server_address[1]}")
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets" / "magpie-api-key").write_text("sk-magpie-test")

    def fake_frames(video, out):
        out.mkdir(parents=True, exist_ok=True)
        (out / "a.jpg").write_bytes(b"jpeg-a")
        (out / "b.jpg").write_bytes(b"jpeg-b")
        return [(0.0, out / "a.jpg"), (65.0, out / "b.jpg")]
    monkeypatch.setattr(magpie, "frames", fake_frames)
    monkeypatch.setattr(magpie, "transcript", lambda video, out: [{"start": 1.0, "end": 4.0, "text": "看一下特斯拉"}])
    yield state
    httpd.shutdown()


def _events(*evs):
    import json
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in evs)


def test_magpie_sends_frames_and_speech_in_place_of_the_video(fake_magpie, home):
    import base64
    from assetizer.providers.magpie import MEDIA_NOTE, MagpieProvider
    clip = home / "chunk.mp4"
    clip.write_bytes(b"video")
    fake_magpie["reply"] = (200, _events(
        {"type": "codex.rate_limits", "primary": {"used_percent": 12}},
        {"type": "response.output_text.delta", "delta": '{"ok": '}, {"type": "response.output_text.delta", "delta": "true}"},
        {"type": "response.completed", "response": {"status": "completed", "usage": {
            "input_tokens": 2500, "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 40}}}}))
    r = MagpieProvider().call(home / "call", "用 view_file 读取视频", kind="understand-CH01", media=[str(clip)],
                              schema={"type": "object", "properties": {"ok": {"type": "boolean"}}}, effort="medium")
    assert r.text == '{"ok": true}' and r.receipt["status"] == "SUCCESS"
    assert r.receipt["usage"] == {"input_tokens": 2500, "output_tokens": 60, "thinking_tokens": 40}
    assert r.receipt["rate_limits"] == {"primary": {"used_percent": 12}} and r.receipt["frames"] == 2
    req = fake_magpie["requests"][0]
    assert req["path"] == "/v1/responses" and req["auth"] == "Bearer sk-magpie-test"
    body = req["body"]
    assert body["model"] == "codex/gpt-6.1-terra" and body["stream"] and body["reasoning"] == {"effort": "medium"}
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is False
    parts = body["input"][0]["content"]
    assert parts[0]["text"].endswith(MEDIA_NOTE)
    assert [p["text"] for p in parts if p["type"] == "input_text"][1:3] == ["画面 [00:00:00]", "画面 [00:01:05]"]
    images = [p for p in parts if p["type"] == "input_image"]
    assert base64.b64decode(images[1]["image_url"].split(",", 1)[1]) == b"jpeg-b"
    assert "[00:00:01–00:00:04] 看一下特斯拉" in parts[-1]["text"]


@pytest.mark.parametrize("reply,kind", [
    ((200, _events({"type": "response.output_text.delta", "delta": "很长"},
                   {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}})),
     "PROVIDER_OUTPUT_LIMIT"),
    ((429, '{"error": {"message": "You have hit your usage limit. Try again in 3 hours."}}'), "PROVIDER_QUOTA"),
    ((200, _events({"type": "response.output_text.delta", "delta": '{"cut'})), "PROVIDER_TRANSIENT"),
    ((401, '{"error": {"message": "invalid gateway key"}}'), "AGY_AUTH_REQUIRED"),
])
def test_magpie_failures_map_to_the_pipeline_kinds(fake_magpie, home, reply, kind):
    from assetizer.providers.agy import ProviderError
    from assetizer.providers.magpie import MagpieProvider
    fake_magpie["reply"] = reply
    with pytest.raises(ProviderError) as e:
        MagpieProvider().call(home / "call", "hi", kind="t")
    assert e.value.kind == kind


def test_a_frame_reading_provider_takes_the_sharpest_proxy_in_one_encode(home, monkeypatch):
    from assetizer.media import ffmpeg
    tried = []

    def fake_proxy(src, start, length, out, fps=1.0, crf=30, max_height=1080, audio_kbps=48):
        tried.append(crf)
        out.write_bytes(b"x" * 100)
        return out
    monkeypatch.setattr(ffmpeg, "model_proxy", fake_proxy)
    monkeypatch.setattr(ffmpeg, "VIEW_FILE_LIMIT", 24)
    out, enc = ffmpeg.fit_proxy(home / "src.mkv", 0, 3000, home / "p.mp4", limit=None)
    assert tried == [32] and enc["height"] == 1080


def test_the_box_setting_can_pick_magpie(home):
    from assetizer import db, providers
    db.set_setting("llm_provider", "magpie")
    p = providers.get()
    assert p.version() == "magpie:codex/gpt-6.1-terra" and p.media_size_limit is None


def test_stage_providers_send_text_stages_to_another_provider(home, monkeypatch):
    from assetizer import db, providers
    from assetizer.llm import LLM
    from assetizer.providers.agy import CallResult
    asked = []

    def fake_call(name):
        def call(self, d, prompt, **kw):
            asked.append((kw["kind"], name))
            return CallResult('{"ok": true}', {"status": "SUCCESS"}, False)
        return call
    from assetizer.providers.agy import AgyProvider
    from assetizer.providers.magpie import MagpieProvider
    monkeypatch.setattr(AgyProvider, "call", fake_call("agy"))
    monkeypatch.setattr(MagpieProvider, "call", fake_call("magpie"))
    db.set_setting("stage_providers", {"knowledge": "magpie", "selection": "magpie"})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    llm = LLM()
    for stage in ("knowledge-detail-03", "selection", "article-outline"):
        llm.structured(home / "calls", stage, "p", schema)
    assert [n for _, n in asked] == ["magpie", "magpie", "agy"]
    assert [type(p).__name__ for p in providers.in_use()] == ["AgyProvider", "MagpieProvider"]
    assert providers.stage_key("selection:repair") == "selection"


# ---------------------------------------------------------------- 原片栏的低清版 (playback.py)
def _playback_fixture(home, monkeypatch, sizes=None):
    """Channel w in tuning with samples a, b; channel tom locked with sample c; d is no sample. All originals cached.
    The encode is faked: it writes `sizes[sid]` bytes (default 1000)."""
    from assetizer import db, playback
    from assetizer.media import ffmpeg
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('w','王','TRACKED','TUNING',?,?), ('tom','汤姆','TRACKED','LOCKED',?,?)", t, t, t, t)
    for sid, ch in (("a", "w"), ("b", "w"), ("c", "tom"), ("d", None)):
        src = home / "media" / sid / "src.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"x")
        db.exec("INSERT INTO sources(id, channel_id, media_path, media_state, created_at, updated_at) VALUES (?,?,?,'CACHED',?,?)", sid, ch, str(src), t, t)
        if ch:
            db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES (?,?,?)", ch, sid, t)
    made = []

    def fake(src, out, height=360):
        made.append(out.stem)
        out.write_bytes(b"v" * (sizes or {}).get(out.stem, 1000))
        return out
    monkeypatch.setattr(ffmpeg, "playback_copy", fake)
    monkeypatch.setattr(playback, "_failed", {})
    monkeypatch.setattr(playback, "_capped", {})
    monkeypatch.setattr(playback, "MIN_FREE", 0)
    return made


def _playback_drain():
    import time
    from assetizer import playback
    for _ in range(200):
        if not playback._busy.locked():
            return
        time.sleep(0.01)
    raise AssertionError("encode still running")


def test_playback_copies_only_tuning_samples_and_goes_when_written(home, monkeypatch):
    from assetizer import db, playback
    made = _playback_fixture(home, monkeypatch)
    monkeypatch.setattr(playback, "MIN_FREE", float("inf"))
    assert not playback.tick() and not made  # a full disk is left to production
    monkeypatch.setattr(playback, "MIN_FREE", 0)
    while playback.tick():
        _playback_drain()
    assert sorted(made) == ["a", "b"]  # a locked channel's sample (c) and a non-sample (d) never get one
    assert playback.ready("a") and playback.ready("b") and not playback.ready("c")
    # an original already purged: nothing to make the copy from, the page falls back to the YouTube embed
    db.exec("DELETE FROM samples WHERE source_id='b'")
    playback.sweep()
    assert not playback.ready("b")  # removed from the samples
    db.exec("UPDATE channels SET stage='LOCKED' WHERE id='w'")
    playback.sweep()
    assert not playback.ready("a")  # locked: its samples were written to Notion
    db.exec("UPDATE channels SET stage='TUNING' WHERE id='w'")
    db.exec("UPDATE sources SET media_state='PURGED' WHERE id='a'")
    assert not playback.tick() and not playback.ready("a")


def test_playback_rolls_off_idle_copies_and_keeps_under_the_cap(home, monkeypatch):
    import os
    import time
    from assetizer import db, playback
    made = _playback_fixture(home, monkeypatch, sizes={"a": 600, "b": 600})
    old = time.time() - (playback.KEEP_DAYS + 1) * 86400
    db.exec("UPDATE samples SET added_at=? WHERE source_id='a'", "2026-01-01T00:00:00+00:00")
    while playback.tick():
        _playback_drain()
    os.utime(playback.path("a"), (old, old))
    playback.sweep()
    assert not playback.ready("a") and playback.ready("b")  # a: no activity for KEEP_DAYS
    # over the cap the least recently active goes first, and is not made again only to push out a newer one
    db.set_setting("playback_max_gb", 1e-6)  # 1000 bytes: room for one copy
    db.exec("UPDATE samples SET added_at=? WHERE source_id='a'", "2099-01-01T00:00:00+00:00")  # a is now the most recent
    made.clear()
    assert playback.tick()
    _playback_drain()
    assert made == ["a"] and playback.ready("a") and not playback.ready("b")
    assert not playback.tick() and made == ["a"]  # b, pushed out by the cap, is not made again only to go again
    db.exec("UPDATE samples SET added_at=? WHERE source_id='b'", "2099-06-01T00:00:00+00:00")  # tried again: newest
    assert playback.tick()
    _playback_drain()
    assert made == ["a", "b"] and playback.ready("b") and not playback.ready("a")


def test_playback_streams_with_ranges_and_marks_activity(home, monkeypatch):
    import os
    import time
    from fastapi.testclient import TestClient
    from assetizer import playback
    from assetizer.web import app as web
    _playback_fixture(home, monkeypatch, sizes={"a": 5000})
    while playback.tick():
        _playback_drain()
    c = TestClient(web.app)
    h = {"host": "127.0.0.1"}
    assert c.get("/api/sources/a", headers=h).json()["playback"] is True
    assert c.get("/api/sources/c", headers=h).json()["playback"] is False
    old = time.time() - 7200
    os.utime(playback.path("a"), (old, old))
    r = c.get("/api/sources/a/playback", headers={**h, "range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"] == "video/mp4"
    assert playback.path("a").stat().st_mtime > old + 3600  # played: counts as activity
    assert c.get("/api/sources/c/playback", headers=h).status_code == 404
    assert c.get("/api/sources/..%2Fx/playback", headers=h).status_code == 404


# ---------------------------------------------------------------- agy quota (/usage)
# what agy 1.2.16 printed for `agy -p /usage --output-format json` on 2026-10-28 (the weekly Gemini limit was used up)
USAGE_JSON = {
    "conversation_id": "", "status": "SUCCESS",
    "response": "Gemini Models\tWeekly Limit Remaining\t0%\t2026-10-30T23:34:21Z\nGemini Models\tFive Hour Limit Remaining\tdisabled\t\n"
                "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-03T16:07:47Z\n"
                "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-10-27T21:07:47Z\n",
    "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "description": "Models within this group: Gemini Flash, Gemini Pro", "buckets": [
            {"id": "gemini-weekly", "name": "Weekly Limit Remaining", "description": "You have hit your weekly limit, it refreshes in 3 days, 7 hours.",
             "window": "weekly", "remaining_fraction": 0, "reset_time": "2026-10-30T23:34:21Z"},
            {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h", "disabled": True, "remaining_fraction": 1}]},
        {"name": "Claude and GPT models", "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS", "buckets": [
            {"id": "3p-weekly", "name": "Weekly Limit Remaining", "window": "weekly", "remaining_fraction": 1, "reset_time": "2026-11-03T16:07:47Z"},
            {"id": "3p-5h", "name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 0.42, "reset_time": "2026-10-27T21:07:47Z"}]},
        {"name": "Gemini 4 Argon", "description": "For paid Pro plans, Argon quota is separate.", "buckets": [
            {"id": "gemini-argon-wtu-weekly", "name": "Gemini 4 Argon Weekly Limit", "window": "weekly", "remaining_fraction": 1}]}]}},
}

FAKE_USAGE = """#!{python}
import json, os, sys
if "--version" in sys.argv:
    print("1.4.8"); sys.exit(0)
assert sys.argv[1:3] == ["-p", "/usage"], sys.argv
assert sys.stdin.read() == ""  # an empty pipe: a signed-out agy must not wait for a pasted code
mode = os.environ.get("FAKE_USAGE", "ok")
if mode == "signed-out":
    print("Error: authentication required. Run 'agy' to log in, then retry.", file=sys.stderr)
    print(json.dumps({"status": "ERROR", "error": "authentication failed or timed out"})); sys.exit(1)
if mode == "network":
    print(json.dumps({"status": "ERROR", "error": "Post \\"https://cloudcode-pa.googleapis.com\\": dial tcp: i/o timeout"})); sys.exit(1)
print(json.dumps(json.loads(os.environ["FAKE_USAGE_JSON"])))
"""


@pytest.fixture
def fake_usage(home, monkeypatch):
    import dataclasses
    import sys
    from assetizer import config
    from assetizer.providers.agy import AgyProvider
    exe = home / "agy-usage"
    exe.write_text(FAKE_USAGE.replace("{python}", sys.executable))
    exe.chmod(0o755)
    config.override(dataclasses.replace(config.get(), agy_bin=str(exe), agy_model="gemini-3.8-flash-high"))
    monkeypatch.setattr(AgyProvider, "_version", None)
    monkeypatch.setenv("FAKE_USAGE_JSON", json.dumps(USAGE_JSON))

    def use(mode="ok", payload=None):
        monkeypatch.setenv("FAKE_USAGE", mode)
        if payload is not None:
            monkeypatch.setenv("FAKE_USAGE_JSON", json.dumps(payload))
    return use


def test_a_click_does_not_clear_agys_sign_in_but_agy_answering_does(fake_usage, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.web import app as web
    monkeypatch.setattr(web, "_throttle_at", {})
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    open_ = lambda: db.one("SELECT 1 FROM alerts WHERE key='AGY_AUTH_REQUIRED' AND resolved_at IS NULL")  # noqa: E731
    fake_usage("signed-out")
    runner.auth_alert("agy")
    db.set_setting("queue_paused", {"reason": "AGY_AUTH_REQUIRED"})
    assert c.post("/api/queue/resume", headers=h, json={}).json()["ok"] and not db.setting("queue_paused")
    for t in threading.enumerate():  # agy is asked in the background: still signed out
        if t.name == "agy-usage":
            t.join(30)
    assert open_() and db.setting("agy_quota")["error"] == "AGY_AUTH_REQUIRED"
    fake_usage("ok")  # signed in at the terminal; 刷新 reads the quota, and agy answering clears it
    assert c.post("/api/agy/quota", headers=h, json={}).json()["ok"] and not open_()
    runner.auth_alert("magpie")  # a key file: the click is all there is
    c.post("/api/queue/resume", headers=h, json={})
    assert not open_()


def test_usage_reads_both_windows_of_every_group():
    from assetizer.providers.agy import parse_usage, quota_group
    groups = parse_usage(USAGE_JSON)
    assert [g["name"] for g in groups] == ["Gemini Models", "Claude and GPT models", "Gemini 4 Argon"]
    gem = groups[0]["buckets"]
    assert gem[0] == {"window": "weekly", "remaining": 0.0, "reset_at": "2026-10-30T23:34:21Z", "disabled": False,
                      "note": "You have hit your weekly limit, it refreshes in 3 days, 7 hours."}
    assert gem[1]["window"] == "5h" and gem[1]["disabled"] and gem[1]["reset_at"] is None
    assert groups[1]["buckets"][1]["remaining"] == 0.42
    # production draws on the group holding the configured model's family, not on Argon or the third-party one
    assert quota_group(groups, "gemini-3.8-flash-high")["name"] == "Gemini Models"
    assert quota_group(groups[1:], "gemini-3.8-flash-high")["name"] == "Gemini 4 Argon"  # only Gemini group left
    # an answer without the structured part: the tab-separated text says the same
    text = parse_usage({"status": "SUCCESS", "response": USAGE_JSON["response"]})
    assert [b["window"] for b in text[0]["buckets"]] == ["weekly", "5h"] and text[0]["buckets"][0]["remaining"] == 0.0
    assert text[0]["buckets"][1]["disabled"] and text[0]["buckets"][1]["remaining"] is None
    assert text[1]["buckets"][1] == {"window": "5h", "remaining": 1.0, "reset_at": "2026-10-27T21:07:47Z", "disabled": False, "note": None}
    assert parse_usage({}) == [] and parse_usage({"command": {"data": {"groups": [{"name": "x", "buckets": []}]}}}) == []


def test_usage_probe_records_a_week_of_the_production_group(fake_usage, home):
    from assetizer import db
    from assetizer.providers.agy import AgyProvider, record_usage
    fake_usage("ok")
    q = record_usage(AgyProvider().usage())
    assert q["ok"] and q["production_group"] == "Gemini Models" and q["version"] == "1.4.8"
    assert db.setting("agy_quota")["groups"][0]["buckets"][0]["remaining"] == 0.0
    log = db.setting("agy_quota_log")
    assert len(log) == 1 and log[0][1:] == [None, 0.0]  # [time, 5-hour (disabled → None), weekly]
    # points older than eight days fall off
    db.set_setting("agy_quota_log", [["2020-01-01T00:00:00+00:00", 1, 1]] + log)
    record_usage(AgyProvider().usage())
    assert [x[0][:4] for x in db.setting("agy_quota_log")] != ["2020"] and len(db.setting("agy_quota_log")) == 2


def test_usage_probe_says_signed_out_and_keeps_the_last_numbers(fake_usage, home):
    from assetizer import db
    from assetizer.providers.agy import AgyProvider, record_usage
    fake_usage("signed-out")
    q = AgyProvider().usage()
    assert not q["ok"] and q["error"] == "AGY_AUTH_REQUIRED"
    fake_usage("ok")
    record_usage(AgyProvider().usage())
    fake_usage("network")
    q = record_usage(AgyProvider().usage())
    assert q["error"] == "PROVIDER_TRANSIENT"
    kept = db.setting("agy_quota")
    assert kept["groups"] and not kept["ok"] and kept["error"] == "PROVIDER_TRANSIENT" and kept["failed_at"]


def test_magpie_rate_limits_read_as_quota_buckets():
    from assetizer.providers.magpie import quota_view
    q = quota_view({"at": "2026-10-28T02:00:00+00:00",
                    "primary": {"used_percent": 30, "window_minutes": 300, "resets_in_seconds": 3600},
                    "secondary": {"used_percent": 82.5, "window_minutes": 10080, "resets_at": 1793145600}})
    assert [b["window"] for b in q["buckets"]] == ["5h", "weekly"]
    assert q["buckets"][0]["remaining"] == 0.7 and q["buckets"][0]["reset_at"] == "2026-10-28T03:00:00+00:00"
    assert q["buckets"][1]["remaining"] == pytest.approx(0.175) and q["buckets"][1]["reset_at"].startswith("2026-")
    assert quota_view({"at": "2026-10-28T02:00:00+00:00", "rate_limits": {"primary": {"used_percent": 100}}})["buckets"][0]["remaining"] == 0
    assert quota_view(None) is None and quota_view({"at": "x", "primary": {}}) is None


# ---------------------------------------------------------------- signing agy in from the 机房 page
# agy's sign-in screen, as agy 1.2.16 draws it: a menu, a link it also hands to the system browser, a field for the code
FAKE_SIGNIN = """#!{python}
import json, os, subprocess, sys, time
marker = os.path.join(os.environ["HOME"], "signed-in")
if sys.argv[1:3] == ["-p", "/usage"]:
    sys.stdin.read()
    if os.path.exists(marker):
        print(json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [{"name": "Gemini Models",
              "description": "Models within this group: Gemini Flash", "buckets": [{"window": "weekly", "remaining_fraction": 0.5}]}]}}}))
        sys.exit(0)
    print("Error: authentication required. Run 'agy' to log in, then retry.", file=sys.stderr)
    print(json.dumps({"status": "ERROR", "error": "authentication failed or timed out"}))
    sys.exit(1)
if "--version" in sys.argv:
    print("1.4.8")
    sys.exit(0)
with open(os.path.join(os.environ["HOME"], "tui-pid"), "w") as f:
    f.write(str(os.getpid()))
print("Welcome to the Antigravity CLI. You are currently not signed in.")
print("Select login method:")
print(" > 1. Google OAuth")
print("   2. Use a Google Cloud project", flush=True)
sys.stdin.readline()
url = "https://accounts.google.com/o/oauth2/auth?client_id=fake&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&state=st4te"
if os.environ.get("FAKE_NO_BROWSER"):
    print("  Your browser should open automatically. If not:")
    print("  " + url[:60])
    print("  " + url[60:], flush=True)
else:
    subprocess.run([os.environ["BROWSER"], url])
print("", flush=True)
print(" If you aren't automatically redirected, paste the authorization code below:", flush=True)
code = sys.stdin.readline().strip()
if code == "4/0Agood-code":
    open(marker, "w").write("1")
    print("Signed in.", flush=True)
else:
    print("Error: invalid_grant", flush=True)
time.sleep(120)
"""


@pytest.fixture
def signin(home, monkeypatch):
    import dataclasses
    import sys
    from assetizer import config
    from assetizer.providers import agy_login
    from assetizer.providers.agy import AgyProvider
    exe = home / "agy-signin"
    exe.write_text(FAKE_SIGNIN.replace("{python}", sys.executable))
    exe.chmod(0o755)
    config.override(dataclasses.replace(config.get(), agy_bin=str(exe), agy_model="gemini-3.8-flash-high"))
    monkeypatch.setattr(AgyProvider, "_version", None)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agy_login, "SETTLE", 0.2)
    monkeypatch.setattr(agy_login, "_session", None)
    yield agy_login
    agy_login.shutdown()


def _until(agy_login, *states, seconds=20.0):
    import time
    end = time.time() + seconds
    while time.time() < end:
        v = agy_login.current()
        if v["state"] in states:
            return v
        time.sleep(0.1)
    raise AssertionError(f"still {agy_login.current()}")


@pytest.mark.parametrize("browser", [True, False])
def test_signing_in_from_the_page_resumes_what_waited_for_it(signin, home, monkeypatch, browser):
    import os
    from assetizer import db
    from assetizer.util import now
    if not browser:  # no browser stand-in got the link: it is read off the screen, glued back across the wrap
        monkeypatch.setenv("FAKE_NO_BROWSER", "1")
    db.raise_alert("AGY_AUTH_REQUIRED", "critical", "模型登录已失效，生产已暂停", "…")
    db.set_setting("queue_paused", {"reason": "AGY_AUTH_REQUIRED", "since": now()})
    _waiting_run()
    db.exec("UPDATE runs SET wait_reason='AGY_AUTH_REQUIRED' WHERE id='v-r1'")
    assert signin.start()["state"] == "checking"
    v = _until(signin, "url", "failed")
    assert v["state"] == "url" and v["url"].endswith("state=st4te") and v["expires_in"] > 500
    assert signin.start()["state"] == "url"  # asking again rejoins the same session
    # pasted as the whole callback address: the code is taken out of it
    assert signin.submit("https://antigravity.google/oauth-callback?code=4/0Agood-code&state=st4te")["state"] == "verifying"
    v = _until(signin, "done", "failed")
    assert v["state"] == "done" and v["quota"]["production_group"] == "Gemini Models"
    assert db.setting("queue_paused") is None and db.setting("agy_quota")["ok"]
    assert db.one("SELECT resolved_at FROM alerts WHERE key='AGY_AUTH_REQUIRED'")["resolved_at"]
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "QUEUED"
    pid = int((home / "tui-pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the interface is gone
    assert not (home / "run" / "agy-login").exists()


def test_a_refused_code_ends_the_session_and_a_signed_in_agy_is_left_alone(signin, home):
    from assetizer import db
    signin.start()
    _until(signin, "url")
    with pytest.raises(ValueError):
        signin.submit("not a code")
    signin.submit("4/0Awrong-code")
    v = _until(signin, "failed", "done")
    assert v["state"] == "failed" and "授权码" in v["error"]
    assert not (home / "signed-in").exists() and db.setting("queue_paused") is None
    # already signed in: no interface is started, the quota comes back
    (home / "signed-in").write_text("1")
    (home / "tui-pid").unlink()
    signin.start()
    v = _until(signin, "already", "failed")
    assert v["state"] == "already" and v["quota"]["ok"] and not (home / "tui-pid").exists()
    with pytest.raises(ValueError):
        signin.submit("4/0Agood-code")


def test_cancelling_a_sign_in_kills_the_interface(signin, home):
    import os
    signin.start()
    _until(signin, "url")
    pid = int((home / "tui-pid").read_text())
    assert signin.cancel()["state"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_the_code_is_taken_from_what_was_pasted():
    from assetizer.providers.agy_login import code_of
    assert code_of("  4/0AVGzR1C-abc_DEF  ") == "4/0AVGzR1C-abc_DEF"
    assert code_of("https://antigravity.google/oauth-callback?state=x&code=4%2F0Abc-123456&scope=a") == "4/0Abc-123456"
    assert code_of("") is None and code_of("short") is None and code_of("has spaces in it") is None
    assert code_of("https://antigravity.google/oauth-callback?state=x") is None


# ---------------------------------------------------------------- 机房 statistics, 刊 production lines
def _stats_fixture():
    from datetime import datetime, timedelta, timezone
    from assetizer import db
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - d).isoformat(timespec="seconds")  # noqa: E731
    for sid, dur in (("a", 3600), ("b", 1800), ("c", 2400), ("d", 600), ("x", 900)):
        db.exec("INSERT INTO sources(id, duration, delivery, created_at, updated_at) VALUES (?,?, 'DELIVERED', ?, ?)", sid, dur, iso(timedelta(0)), iso(timedelta(0)))
    runs = [("a-r1", "a", "COMPLETE", timedelta(hours=2), "PRODUCTION"), ("b-r1", "b", "COMPLETE_PARTIAL", timedelta(days=3), "PRODUCTION"),
            ("c-r1", "c", "FAILED", timedelta(hours=5), "PRODUCTION"), ("d-r1", "d", "COMPLETE", timedelta(days=20), "PRODUCTION"),
            ("d-r0", "d", "COMPLETE", timedelta(days=40), "PRODUCTION"), ("x-r1", "x", "COMPLETE", timedelta(hours=1), "IMPORT"),
            ("c-r2", "c", "RUNNING", None, "PRODUCTION")]
    for rid, sid, state, ago, kind in runs:
        db.exec("INSERT INTO runs(id, source_id, kind, production, created_at, started_at, finished_at) VALUES (?,?,?,?,?,?,?)",
                rid, sid, kind, state, iso(timedelta(days=50)), iso(timedelta(days=50)), iso(ago) if ago else None)
        for name, secs in (("UNDERSTAND", 600), ("CLIPS", 1800), ("TRANSCRIBE", 900), ("CLEANUP", 5)):
            db.exec("INSERT INTO stages(run_id, name, ord, status, wall_seconds) VALUES (?,?,0,'DONE',?)", rid, name, secs)
    for model, stage, ago, tokens, status in (("gemini-3.8-flash-high", "understanding-01", timedelta(hours=2), 1000, "SUCCESS"),
                                              ("codex/gpt-6.1-terra", "knowledge-skeleton", timedelta(hours=3), 500, "SUCCESS"),
                                              ("gemini-3.8-flash-high", "article-outline", timedelta(days=2), 300, "FAILED"),
                                              ("gemini-3.8-flash-high", "review-check-01", timedelta(days=45), 9999, "SUCCESS")):
        db.exec("""INSERT INTO model_calls(run_id, stage, model, status, wall_seconds, input_tokens, output_tokens, thinking_tokens, created_at)
                   VALUES ('a-r1',?,?,?,10,?,0,0,?)""", stage, model, status, tokens, iso(ago))
    db.exec("UPDATE sources SET current_run_id='a-r1', created_at=?, delivered_at=? WHERE id='a'", iso(timedelta(hours=3)), iso(timedelta(hours=1)))
    db.exec("UPDATE runs SET error_class='PROVIDER_TIMEOUT' WHERE id='c-r1'")
    return iso


def test_engine_stats_count_by_window_and_by_model_family(home):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    iso = _stats_fixture()
    c = TestClient(web.app)
    s = c.get("/api/engine/stats", headers={"host": "127.0.0.1"}).json()
    week, month = s["windows"]["7d"], s["windows"]["30d"]
    # imported runs and runs older than the window are left out; a failure is not "done"
    assert (week["done"], week["partial"], week["failed"], month["done"]) == (2, 1, 1, 3)
    assert week["video_seconds"] == 5400 and week["reasons"] == [{"kind": "PROVIDER_TIMEOUT", "n": 1}]
    # minutes per hour of video, the median over the runs; TRANSCRIBE (background) and CLEANUP are not stages here
    assert [(x["name"], x["minutes"]) for x in week["per_hour"]] == [("CLIPS", 45.0), ("UNDERSTAND", 15.0)]
    assert [(x["name"], x["minutes"]) for x in month["per_hour"]] == [("CLIPS", 60.0), ("UNDERSTAND", 20.0)]
    assert week["latency_median"] == 7200  # in at -3 h, in Notion at -1 h
    m = week["calls"]
    assert (m["n"], m["failed"], m["tokens"]) == (3, 1, 1800) and m["by_family"]["chatgpt"] == {"n": 1, "tokens": 500, "wall": 10.0}
    assert [(x["key"], round(x["share"], 2)) for x in m["gemini_by_stage"]] == [("understanding", 0.77), ("article", 0.23)]
    assert len(s["days"]) == 30 and sum(d["done"] for d in s["days"]) == 3 and sum(d["failed"] for d in s["days"]) == 1
    assert sum(d["video_seconds"] for d in s["days"]) == 6000 and s["runway"] is None
    # the weekly quota fell 0.21 (a reading that wavers up is not quota coming back), was reset, fell 0.5 more
    db.set_setting("agy_quota", {"ok": True, "production_group": "G", "groups": [{"name": "G", "models": "", "buckets": [
        {"window": "weekly", "remaining": 0.5, "reset_at": "x", "disabled": False, "note": None}]}]})
    db.set_setting("agy_quota_log", [[iso(timedelta(days=9)), 1, 0.1], [iso(timedelta(days=6)), 1, 0.9], [iso(timedelta(days=5)), 1, 0.7],
                                     [iso(timedelta(days=4.8)), 1, 0.75], [iso(timedelta(days=4.6)), 1, 0.69],
                                     [iso(timedelta(days=4)), 1, 1.0], [iso(timedelta(hours=1)), 1, 0.5]])
    assert c.get("/api/engine/stats", headers={"host": "127.0.0.1"}).json()["runway"] is None  # 2 issues over it: too few to tell
    db.exec("INSERT INTO runs(id, source_id, kind, production, created_at, finished_at) VALUES ('e-r1','x','PRODUCTION','COMPLETE',?,?)",
            iso(timedelta(days=1)), iso(timedelta(days=1)))
    r = c.get("/api/engine/stats", headers={"host": "127.0.0.1"}).json()["runway"]
    assert round(r["per_issue"], 3) == 0.237 and r["issues"] == 2  # 0.71 over 3 issues, 0.5 left


def test_the_new_reads_answer_on_an_empty_database(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    s = c.get("/api/engine/stats", headers=h).json()
    assert s["windows"]["7d"]["done"] == 0 and s["windows"]["30d"]["latency_median"] is None and s["runway"] is None
    assert c.get("/api/status", headers=h).json() == {"alerts": [], "decisions": 0, "running": 0, "queued": 0, "paused": None}
    o = c.get("/api/overview", headers=h).json()
    assert o["running"] == [] and o["drain_seconds"] == 0
    db.exec("INSERT INTO channels(id, name, mode, created_at, updated_at) VALUES ('x','X','DISCOVERY',?,?)", now(), now())
    b = c.get("/api/channels/x/board", headers=h).json()
    assert b["stats"]["issues"] == 0 and b["stats"]["last"] is None and b["backfill"]["pending"] == 0 and b["issues"] == []
    assert c.get("/api/channels", headers=h).json()["channels"][0]["stats"]["weeks"] == [0] * web.WEEKS


def test_overview_lists_every_run_in_production_and_where_a_decision_leads(home):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    _stats_fixture()
    db.exec("INSERT INTO runs(id, source_id, production, created_at, started_at) VALUES ('b-r2','b','RUNNING','t','2026-01-01T00:00:00+00:00')")
    for i, name in enumerate(("UNDERSTAND", "CLIPS")):
        db.exec("INSERT OR REPLACE INTO stages(run_id, name, ord, status) VALUES ('b-r2',?,?,'PENDING')", name, i)
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('w','W','TRACKED','TUNING','t','t')")
    pb = db.exec("INSERT INTO playbooks(channel_id, version, status, data, created_at) VALUES ('w',1,'NEEDS_CONFIRMATION','{}','t')")
    db.decision("CONFIRM_PLAYBOOK", str(pb), "为「W」定稿")
    db.set_setting("worker_slots", 3)
    o = TestClient(web.app).get("/api/overview", headers={"host": "127.0.0.1"}).json()
    assert {r["id"] for r in o["running"]} == {"c-r2", "b-r2"} and o["slots"] == 3
    assert all("eta_seconds" in r and r["source"] for r in o["running"])
    assert o["decisions"][0]["channel_id"] == "w"
    assert next(i for i in o["issues"] if i["id"] == "a")["ready_at"]
    # never sooner than the longest run in production (b-r2: understanding and clips of 30 minutes of video)
    assert o["drain_seconds"] == round((12 + 30) * 30)
    whole = sum(web.STAGE_COST.values())
    for rid, sid in (("d-r9", "d"), ("a-r9", "a")):  # queued: each to the slot free first
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?,?,'QUEUED','t')", rid, sid)
    assert web._drain() == round(whole * 60)  # a's hour of video, on the third slot, ends last


# ---------------------------------------------------------------- 频道看板: stats, scans, 补录
def _board_fixture(home, monkeypatch, stage="LOCKED"):
    from assetizer import db, ingest
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('t','Tom','UCt','TRACKED',?,?,?)",
            stage, t, t)
    db.set_setting("ingest:t", {"enabled": True, "since": "2026-10-09T15:00:00+00:00", "min_minutes": 10})
    db.exec("INSERT INTO sources(id, channel_id, title, duration, delivery, current_run_id, created_at, updated_at) VALUES ('AAAAAAAAAAA','t','A',3600,'DELIVERED','AAAAAAAAAAA-r1',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, summary, created_at, finished_at) VALUES ('AAAAAAAAAAA-r1','AAAAAAAAAAA','COMPLETE',?,?,?)",
            json.dumps({"units": 12, "assets": {"clip:GENERATED": 9}}), t, t)
    entries = [{"id": "AAAAAAAAAAA", "title": "have", "published": "2026-10-20T00:00:00+00:00", "duration": 3600, "approx": True},
               {"id": "BBBBBBBBBBB", "title": "short", "published": "2026-10-21T00:00:00+00:00", "duration": 120, "approx": True},
               {"id": "CCCCCCCCCCC", "title": "new", "published": "2026-10-22T00:00:00+00:00", "duration": 2400, "approx": True},
               {"id": "DDDDDDDDDDD", "title": "before intake", "published": "2026-10-01T00:00:00+00:00", "duration": 2400, "approx": True}]
    monkeypatch.setattr(ingest, "look", lambda cid, limit=15, shorts=False: (entries, None, {"videos", "streams"}))
    monkeypatch.setattr(ingest, "listing", lambda cid, limit=15: entries)
    import assetizer.delivery as d
    monkeypatch.setattr(d, "registered_in_target", lambda src: None)
    from assetizer.web import app as web
    monkeypatch.setattr(web, "_listings", {})
    monkeypatch.setattr(web, "_feeds", {})
    return entries


def _listed(c, url, h):
    """The 补录 listing is read in the background: ask until it is there."""
    import time
    for _ in range(200):
        r = c.get(url, headers=h)
        if r.status_code != 200 or not r.json().get("listing"):
            return r
        time.sleep(0.02)
    raise AssertionError("still listing")


def test_a_scan_is_counted_and_kept_with_the_channel(home, monkeypatch):
    from assetizer import db, ingest
    _board_fixture(home, monkeypatch)
    out = ingest.scan(db.one("SELECT * FROM channels WHERE id='t'"), db.setting("ingest:t"))
    assert (out["found"], out["queued"], out["have"], out["short"], out["error"]) == (3, 1, 1, 1, None) and out["ids"] == ["CCCCCCCCCCC"]
    cfg = db.setting("ingest:t")
    assert cfg["last_poll"] == out["at"] and cfg["scans"][0]["queued"] == 1 and "ids" not in cfg["scans"][0]
    for _ in range(12):
        ingest.scan(db.one("SELECT * FROM channels WHERE id='t'"), db.setting("ingest:t"))
    assert len(db.setting("ingest:t")["scans"]) == ingest.SCANS_KEPT and db.setting("ingest:t")["enabled"]


def test_a_scan_racing_another_and_one_channel_failing_do_not_stop_intake(home, monkeypatch):
    import sqlite3
    from assetizer import db, ingest
    from assetizer.pipeline import runner
    from assetizer.util import now
    _board_fixture(home, monkeypatch)

    def taken(*a, **k):  # a 立即扫描 and the worker's round both reached the new video: the other one inserted it first
        raise sqlite3.IntegrityError("UNIQUE constraint failed: sources.id")
    monkeypatch.setattr(runner, "submit", taken)
    out = ingest.scan(db.one("SELECT * FROM channels WHERE id='t'"), db.setting("ingest:t"))
    assert (out["found"], out["queued"], out["have"], out["short"]) == (3, 0, 2, 1) and db.setting("ingest:t")["scans"]
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('u','Ursa','UCu','TRACKED','LOCKED',?,?)", t, t)
    db.set_setting("ingest:u", {"enabled": True, "since": t, "min_minutes": 10})
    seen = []

    def scan(c, cfg):
        seen.append(c["id"])
        if c["id"] == "t":
            raise RuntimeError("database is locked")
        return {"ids": ["X"]}
    monkeypatch.setattr(ingest, "scan", scan)
    assert ingest.next_round() is None  # no round yet: no time to tell
    assert ingest.poll() == {"queued": ["X"]} and sorted(seen) == ["t", "u"]
    assert db.one("SELECT 1 FROM events WHERE kind='ingest' AND message LIKE 'Tom 扫描出错%'")
    from datetime import datetime  # the next round is the worker's, not the last 立即扫描's
    assert (datetime.fromisoformat(ingest.next_round()) - datetime.fromisoformat(db.setting("ingest_round"))).seconds == ingest.EVERY


def _tabs(monkeypatch, tabs: dict, how: dict | None = None, stderr: str = ""):
    """yt-dlp's flat listing faked by tab ({tab: entries}; a tab left out fails), and the login it was given."""
    import contextlib
    import subprocess
    from assetizer import cookies, ingest
    asked = {"tabs": [], "logins": 0}

    @contextlib.contextmanager
    def login():
        asked["logins"] += 1
        yield ["--cookies", "x"], how or {"source": "cookiecloud", "signed_in": True, "code": None}

    def run(cmd, **kw):
        tab = cmd[-1].rsplit("/", 1)[1]
        asked["tabs"].append(tab)
        if tab not in tabs:
            return subprocess.CompletedProcess(cmd, 1, "", f"ERROR: [youtube:tab] This channel does not have a {tab} tab\n")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"entries": tabs[tab]}), stderr)
    monkeypatch.setattr(cookies, "ytdlp_login", login)
    monkeypatch.setattr(ingest.subprocess, "run", run)
    return asked


def test_the_listing_reads_every_tab_with_one_login_and_says_what_keeps_members_videos_out(home, monkeypatch):
    from assetizer import ingest
    day = 1792800000
    tabs = {"videos": [{"id": "v" * 11, "title": "v", "duration": 1800, "timestamp": day}],
            "streams": [{"id": "m" * 11, "title": "会员", "duration": 3600, "timestamp": day + 86400, "availability": "subscriber_only"}],
            "shorts": [{"id": "s" * 11, "title": "s"}]}
    asked = _tabs(monkeypatch, tabs)
    entries, members, read = ingest.look("UCt", shorts=True)
    assert [e["id"] for e in entries] == ["m" * 11, "v" * 11, "s" * 11] and members is None and read == {"videos", "streams", "shorts"}
    assert asked == {"tabs": ["videos", "streams", "shorts"], "logins": 1} and entries[2]["published"] == ""
    assert ingest.listing("UCt") and asked["tabs"][-2:] == ["videos", "streams"]  # 补录 and 试一期: no shorts
    for how, said, verdict in (({"source": "cookiecloud", "signed_in": False, "code": None}, "", "not_signed_in"),
                               ({"source": "cookiecloud", "signed_in": True, "code": None},
                                "WARNING: The provided YouTube account cookies are no longer valid.", "rejected"),
                               ({"source": "browser", "signed_in": None, "code": "token_revoked"}, "", "cookiecloud"),
                               ({"source": "browser", "signed_in": None, "code": None}, "", None),
                               ({"source": None, "signed_in": None, "code": None}, "", "none")):
        _tabs(monkeypatch, tabs, how, said)
        assert ingest.look("UCt")[1] == verdict
    _tabs(monkeypatch, {"videos": tabs["videos"]})  # no live replays: that tab is left out, as read and empty
    assert [e["id"] for e in ingest.look("UCt")[0]] == ["v" * 11] and ingest.look("UCt")[2] == {"videos", "streams"}
    _tabs(monkeypatch, {})  # no tab read: RSS, which leaves members-only videos out
    monkeypatch.setattr(ingest, "rss", lambda cid: [{"id": "r" * 11, "title": "r", "published": "2026-10-28T00:00:00+00:00"}])
    assert ingest.look("UCt") == ([{"id": "r" * 11, "title": "r", "published": "2026-10-28T00:00:00+00:00"}], "rss", set())
    with pytest.raises(RuntimeError, match="does not have a streams tab"):
        ingest.listing("UCt")  # 补录 has no RSS to fall back on: yt-dlp's own words

    def down(cid):
        raise RuntimeError("HTTP 404")
    monkeypatch.setattr(ingest, "rss", down)
    with pytest.raises(RuntimeError, match="频道列表与 RSS 都读不到"):
        ingest.look("UCt")


def test_shorts_are_read_below_three_minutes_and_new_when_not_listed_before(home, monkeypatch):
    from assetizer import db, ingest
    entries = _board_fixture(home, monkeypatch)
    s1, s2 = "s1" + "x" * 9, "s2" + "x" * 9
    short = lambda vid: {"id": vid, "title": vid, "published": "", "duration": None, "approx": True, "tab": "shorts"}  # noqa: E731
    listed, asked, read = [short(s1)], [], {"videos", "streams", "shorts"}

    def look(cid, limit=15, shorts=False):
        asked.append(shorts)
        return entries + (listed if shorts else []), None, read if shorts else {"videos", "streams"}
    monkeypatch.setattr(ingest, "look", look)
    ch = lambda: db.one("SELECT * FROM channels WHERE id='t'")  # noqa: E731
    ingest.scan(ch(), db.setting("ingest:t"))
    assert asked == [False] and "shorts_seen" not in db.setting("ingest:t")  # 10 minutes: shorts are not read
    db.set_setting("ingest:t", {**db.setting("ingest:t"), "min_minutes": 0})
    out = ingest.scan(ch(), db.setting("ingest:t"))  # the first scan that reads them only notes what is there
    assert asked[-1] is True and out["ids"] == ["BBBBBBBBBBB"] and db.setting("ingest:t")["shorts_seen"] == [s1]
    listed.insert(0, short(s2))
    out = ingest.scan(ch(), db.setting("ingest:t"))
    assert out["ids"] == [s2] and db.setting("ingest:t")["shorts_seen"] == [s2, s1]
    read.discard("shorts")  # the shorts tab failed this time: what was listed before stays
    listed[:] = []
    ingest.scan(ch(), db.setting("ingest:t"))
    assert db.setting("ingest:t")["shorts_seen"] == [s2, s1]
    db.set_setting("ingest:t", {**db.setting("ingest:t"), "min_minutes": 5})  # shorts out again: noted anew next time
    ingest.scan(ch(), db.setting("ingest:t"))
    assert "shorts_seen" not in db.setting("ingest:t")


def test_a_scan_keeps_what_keeps_members_videos_out_and_tells_once(home, monkeypatch):
    from assetizer import db, ingest
    entries = _board_fixture(home, monkeypatch)
    verdict = ["rejected"]
    monkeypatch.setattr(ingest, "look", lambda cid, limit=15, shorts=False: (entries, verdict[0], {"videos", "streams"}))
    ch = lambda: db.one("SELECT * FROM channels WHERE id='t'")  # noqa: E731
    for _ in range(2):
        ingest.scan(ch(), db.setting("ingest:t"))
    assert db.setting("ingest:t")["scans"][0]["members"] == "rejected"
    assert db.one("SELECT COUNT(*) n FROM events WHERE message LIKE '%登录态失效%'")["n"] == 1  # not every half hour
    verdict[0] = None
    ingest.scan(ch(), db.setting("ingest:t"))
    assert db.setting("ingest:t")["scans"][0]["members"] is None


def test_backfill_by_link_takes_only_this_channels_video(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import db, youtube
    from assetizer.web import app as web
    _board_fixture(home, monkeypatch)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    info = {"channel_id": "UCt", "channel": "Tom", "title": "不公开的一期", "duration": 2400}
    monkeypatch.setattr(youtube, "metadata", lambda url: info)
    r = c.post("/api/channels/t/backfill/link", headers=h, json={"url": "https://youtu.be/UUUUUUUUUUU?si=x"})
    assert r.json() == {"queued": 1, "have": 0}
    assert db.one("SELECT title, duration FROM backfill_queue WHERE source_id='UUUUUUUUUUU'") == {"title": "不公开的一期", "duration": 2400}
    assert c.post("/api/channels/t/backfill/link", headers=h, json={"url": "https://youtu.be/UUUUUUUUUUU"}).json()["have"] == 1
    info.update(channel_id="UCo", channel="Other")
    r = c.post("/api/channels/t/backfill/link", headers=h, json={"url": "https://www.youtube.com/watch?v=OOOOOOOOOOO"})
    assert r.status_code == 409 and "Other" in r.json()["detail"]
    assert c.post("/api/channels/t/backfill/link", headers=h, json={"url": "https://example.com/x"}).status_code == 422


def test_the_channel_list_says_test_copy_and_an_empty_listing_is_an_error(home, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import db, ingest
    from assetizer.web import app as web
    _board_fixture(home, monkeypatch)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    db.set_setting("notion:t", {"database_id": "d", "database_title": "🧪 汤姆 · 测试副本"})
    assert c.get("/api/channels", headers=h).json()["channels"][0]["notion"]["test_copy"]  # an older config: by its name
    db.set_setting("notion:t", {"database_id": "d", "database_title": "🧪 汤姆 · 测试副本", "is_test_copy": False})
    assert not c.get("/api/channels", headers=h).json()["channels"][0]["notion"]["test_copy"]
    entries = ingest.listing("UCt")
    monkeypatch.setattr(ingest, "listing", lambda cid, limit=15: [])  # yt-dlp listed nothing: an error, and not kept
    r = _listed(c, "/api/channels/t/backfill/candidates", h)
    assert r.status_code == 502 and "读不到" in r.json()["detail"]
    monkeypatch.setattr(ingest, "listing", lambda cid, limit=15: entries)
    assert len(_listed(c, "/api/channels/t/backfill/candidates", h).json()["candidates"]) == 4


def test_backfill_lists_just_deep_enough_for_its_range(home, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    from assetizer import ingest
    from assetizer.web import app as web
    _board_fixture(home, monkeypatch)
    today = datetime.now(timezone.utc).replace(hour=12)
    asked = []

    def listing(cid, limit=15):  # a video a day
        asked.append(limit)
        return [{"id": f"v{i:010d}", "title": str(i), "duration": 1800, "approx": True,
                 "published": (today - timedelta(days=i)).isoformat()} for i in range(limit)]
    monkeypatch.setattr(ingest, "listing", listing)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    since = (today - timedelta(days=200)).date().isoformat()
    r = _listed(c, f"/api/channels/t/backfill/candidates?since={since}", h).json()
    assert asked == [60, 274] and r["complete"] and len(r["candidates"]) == 201 and r["listed"] == 274
    until = (today - timedelta(days=100)).date().isoformat()
    r = _listed(c, f"/api/channels/t/backfill/candidates?since={since}&until={until}", h).json()
    assert asked == [60, 274] and len(r["candidates"]) == 101  # the listing is kept: no second read
    r = _listed(c, "/api/channels/t/backfill/candidates", h).json()  # all of it: as deep as it goes
    assert asked[-1] == web.LISTING_MAX and not r["complete"] and len(r["candidates"]) == web.LISTING_MAX
    assert c.get("/api/channels/t/backfill/candidates?since=21-10-2026", headers=h).status_code == 422
    assert c.get("/api/channels/t/backfill/candidates?since=2026-02-30", headers=h).status_code == 422  # no such day


def test_board_scan_and_backfill_through_the_api(home, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    _board_fixture(home, monkeypatch)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    b = c.get("/api/channels/t/board", headers=h).json()
    assert b["stats"]["issues"] == 1 and b["stats"]["units"] == 12 and b["stats"]["clips"] == 9 and b["stats"]["delivered"] == 1
    assert sum(b["stats"]["weeks"]) == 1 and len(b["stats"]["weeks"]) == web.WEEKS and b["issues"][0]["id"] == "AAAAAAAAAAA"
    assert c.get("/api/channels", headers=h).json()["channels"][0]["stats"]["issues"] == 1
    assert c.post("/api/channels/t/scan", headers=h, json={}).json() == {"scanning": True}
    for _ in range(50):
        if not c.get("/api/channels/t/board", headers=h).json()["ingest"]["scanning"]:
            break
        time.sleep(0.05)
    assert db.setting("ingest:t")["scans"][0]["queued"] == 1
    v = _listed(c, "/api/channels/t/backfill/candidates", h).json()["candidates"]
    assert [(x["here"], x["short"]) for x in v] == [(True, False), (False, True), (True, False), (False, False)]  # CCC: the scan
    r = _listed(c, "/api/channels/t/backfill/candidates?since=2026-10-21&until=2026-10-21", h).json()
    assert [x["id"] for x in r["candidates"]] == ["BBBBBBBBBBB"] and r["complete"]
    assert [x["id"] for x in c.get("/api/channels/t/candidates?limit=2", headers=h).json()["candidates"]] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert c.post("/api/channels/t/backfill", headers=h, json={"videos": [{"id": "../etc"}]}).status_code == 422
    r = c.post("/api/channels/t/backfill", headers=h, json={"videos": [
        {"id": "DDDDDDDDDDD", "title": "before intake", "duration": 2400}, {"id": "AAAAAAAAAAA"}]}).json()
    assert r == {"queued": 1, "have": 1}  # lined up, not queued for production yet
    assert _listed(c, "/api/channels/t/backfill/candidates", h).json()["candidates"][3]["queued"]
    assert not db.one("SELECT 1 FROM runs WHERE source_id='DDDDDDDDDDD'")
    assert c.get("/api/channels/t/board", headers=h).json()["backfill"] == {"pending": 1, "seconds": 2400, "running": None, "held": None}
    # 试这一期: right away, normal priority, outside the 补录 line
    r = c.post("/api/channels/t/backfill", headers=h, json={"videos": [{"id": "EEEEEEEEEEE", "title": "try"}], "now": True}).json()
    assert r["queued"] == 1 and db.one("SELECT priority, origin FROM runs WHERE id=?", r["run_id"]) == {"priority": 0, "origin": "manual"}
    # intake switched off: no scan; back to tuning: the line is dropped and 补录 refused
    db.set_setting("ingest:t", {**db.setting("ingest:t"), "enabled": False})
    assert c.post("/api/channels/t/scan", headers=h, json={}).status_code == 409
    from assetizer import tuning
    tuning.retune("t")
    assert db.one("SELECT status FROM backfill_queue WHERE source_id='DDDDDDDDDDD'")["status"] == "CANCELLED"
    assert c.post("/api/channels/t/backfill", headers=h, json={"videos": [{"id": "FFFFFFFFFFF"}]}).status_code == 409


def test_backfill_goes_one_at_a_time_when_idle_and_the_quota_has_room(home, monkeypatch):
    from assetizer import db, ingest
    from assetizer.util import now
    _board_fixture(home, monkeypatch)
    ingest.enqueue_backfill("t", [{"id": "DDDDDDDDDDD", "title": "d"}, {"id": "GGGGGGGGGGG", "title": "g"}])
    db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, created_at) VALUES ('t','x',1,?)", now())
    assert ingest.dispatch_backfill() is None  # a trial waits: it goes first
    db.exec("DELETE FROM trial_queue")
    db.set_setting("agy_quota", {"ok": True, "checked_at": now(), "production_group": "G", "groups": [{"name": "G", "models": "", "buckets": [
        {"window": "weekly", "remaining": 0.2, "reset_at": None, "disabled": False, "note": None}]}]})
    assert ingest.dispatch_backfill() is None and ingest.backfill_held() == "weekly"  # the week's quota is kept for new videos
    db.set_setting("agy_quota", {"ok": True, "checked_at": now(), "production_group": "G", "groups": [{"name": "G", "models": "", "buckets": [
        {"window": "weekly", "remaining": 0.8, "reset_at": None, "disabled": False, "note": None},
        {"window": "5h", "remaining": 0.1, "reset_at": None, "disabled": False, "note": None}]}]})
    assert ingest.dispatch_backfill() is None and ingest.backfill_held() == "5h"  # and so is most of the 5 hours
    db.set_setting("agy_quota", {**db.setting("agy_quota"), "checked_at": "2020-01-01T00:00:00+00:00"})
    rid = ingest.dispatch_backfill()  # a stale reading does not hold it back
    assert rid and db.one("SELECT priority, origin FROM runs WHERE id=?", rid) == {"priority": -1, "origin": "backfill"}
    assert db.one("SELECT channel_id FROM sources WHERE id='DDDDDDDDDDD'")["channel_id"] == "t"
    assert ingest.dispatch_backfill() is None  # one 补录 run at a time
    db.exec("UPDATE runs SET production='COMPLETE' WHERE id=?", rid)
    import assetizer.delivery as d
    monkeypatch.setattr(d, "registered_in_target", lambda src: {"url": "https://notion.so/x"})
    assert ingest.dispatch_backfill() is None  # already in the library: skipped, and no 已登记 row floods 往期
    assert db.one("SELECT status FROM backfill_queue WHERE source_id='GGGGGGGGGGG'")["status"] == "SKIPPED"
    assert not db.one("SELECT 1 FROM sources WHERE id='GGGGGGGGGGG'")


def test_a_cancel_while_backfill_asks_the_library_leaves_the_one_already_taken(home, monkeypatch):
    import sqlite3
    from assetizer import db, ingest
    _board_fixture(home, monkeypatch)
    ingest.enqueue_backfill("t", [{"id": "DDDDDDDDDDD", "title": "d"}, {"id": "GGGGGGGGGGG", "title": "g"}])
    import assetizer.delivery as d
    cancelled = []

    def asked(src):  # 取消排队 while the library is asked about the first one (seconds)
        cancelled.append(ingest.cancel_backfill("t"))
        return None
    monkeypatch.setattr(d, "registered_in_target", asked)
    rid = ingest.dispatch_backfill()
    assert rid and cancelled == [1]  # the one under way had gone already: the cancel counts the other
    assert {r["source_id"]: (r["status"], r["run_id"]) for r in db.q("SELECT * FROM backfill_queue")} == \
        {"DDDDDDDDDDD": ("DISPATCHED", rid), "GGGGGGGGGGG": ("CANCELLED", None)}
    db.exec("UPDATE runs SET production='COMPLETE' WHERE id=?", rid)
    ingest.enqueue_backfill("t", [{"id": "HHHHHHHHHHH", "title": "h"}])

    def taken(*a, **k):  # intake reached the same video at once
        raise sqlite3.IntegrityError("UNIQUE constraint failed: sources.id")
    monkeypatch.setattr(ingest, "take", taken)
    assert ingest.dispatch_backfill() is None  # skipped once, not tried again on every tick
    assert db.one("SELECT status FROM backfill_queue WHERE source_id='HHHHHHHHHHH'")["status"] == "SKIPPED"


# ---------------------------------------------------------------- 使用场景边界 review（2026-10-28）：注释里的 Sxx 是场景编号
def test_the_library_lookup_reads_every_page_of_its_answer(home, monkeypatch):
    # S01: fifty videos asked at once can match more than a page of results (a library with duplicates)
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.util import now
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('trader-tom','Tom','TRACKED','LOCKED',?,?)", now(), now())
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    ids = [f"vid{i:08d}" for i in range(50)]
    pages = [{"id": f"pg{i}", "url": f"https://notion/{i}", "properties": {"视频链接": {"type": "url", "url": f"https://youtu.be/{v}"}}}
             for i, v in enumerate([ids[i // 3] for i in range(102)] + ids[34:])]

    class Paged:
        def _req(self, method, path, **kw):
            if path.endswith("/query"):
                at = int(kw["json"].get("start_cursor") or 0)
                size = kw["json"]["page_size"]
                more = at + size < len(pages)
                return {"results": pages[at:at + size], "has_more": more, "next_cursor": str(at + size) if more else None}
            return LIB
    monkeypatch.setattr(delivery, "client", lambda: Paged())
    assert sorted(delivery.registered_many({"channel_id": "trader-tom"}, ids)) == ids


def test_the_backfill_list_says_when_the_library_could_not_be_checked(home, monkeypatch):
    # S01: a failed lookup is not "none of them are in the library"; asked again once Notion answers
    from fastapi.testclient import TestClient
    from assetizer import delivery
    from assetizer.web import app as web
    _board_fixture(home, monkeypatch)

    def down(src, ids):
        raise RuntimeError("Notion 502")
    monkeypatch.setattr(delivery, "registered_many", down)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    assert _listed(c, "/api/channels/t/backfill/candidates", h).json()["library_checked"] is False
    monkeypatch.setattr(delivery, "registered_many", lambda src, ids: {"CCCCCCCCCCC": {"id": "p", "url": "u", "title": "t"}})
    r = _listed(c, "/api/channels/t/backfill/candidates", h).json()
    assert r["library_checked"] is True and next(x for x in r["candidates"] if x["id"] == "CCCCCCCCCCC")["library"]


def test_samples_are_not_counted_as_issues(home):
    # S03: a channel still being tuned has samples, run again and again and not in Notion: they are not issues
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('w','Wang','TRACKED','TUNING',?,?), ('t','Tom','TRACKED','LOCKED',?,?)",
            t, t, t, t)
    for sid, cid, delivery in [("W1", "w", "NOT_STARTED"), ("W2", "w", "NOT_STARTED"), ("T1", "t", "DELIVERED")]:
        db.exec("INSERT INTO sources(id, channel_id, title, duration, delivery, current_run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                sid, cid, sid, 1800, delivery, sid + "-r2", t, t)
        for r in ("r1", "r2"):
            db.exec("INSERT INTO runs(id, source_id, production, summary, created_at, finished_at) VALUES (?,?,'COMPLETE',?,?,?)",
                    f"{sid}-{r}", sid, json.dumps({"units": 5}), t, t)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    issues = {i["id"]: i for i in c.get("/api/overview", headers=h).json()["issues"]}
    assert [issues[k]["sample"] for k in ("W1", "W2", "T1")] == [True, True, False]
    assert issues["T1"]["no"] == 1 and issues["W1"]["no"] is None  # a sample takes no issue number
    stats = {ch["id"]: ch["stats"] for ch in c.get("/api/channels", headers=h).json()["channels"]}
    assert (stats["w"]["issues"], stats["w"]["units"], stats["t"]["issues"], stats["t"]["units"]) == (0, 0, 1, 5)


def test_a_page_that_cannot_be_archived_is_not_written_a_second_time(home, monkeypatch):
    # S06: replacing the page of an issue run again: when archiving the old page fails, no second page is made
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.notion.client import NotionError
    _run_fixture(home)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    db.exec("UPDATE sources SET current_run_id='r', notion_page_id='old', notion_synced_run='r0', notion_database_id='lib', delivery='STALE'")
    calls = []

    class Fake:
        status = 502

        def _req(self, method, path, **kw):
            calls.append((method, path))
            if path == "/pages":
                raise NotionError(418, "stop: a page would be made here")
            return {"results": []} if path.endswith("/query") or path.startswith("/comments") else LIB

        def page(self, pid):
            return {"archived": False, "icon": None}

        def me(self):
            return {"id": "bot"}

        def children(self, pid):  # the old page as Assetizer left it (a07)
            return []

        def archive_page(self, pid):
            raise NotionError(self.status, "<!DOCTYPE html>")
    fake = Fake()
    monkeypatch.setattr(delivery, "client", lambda: fake)
    assert delivery.deliver("v")["status"] == "FAILED"
    assert ("POST", "/pages") not in calls and db.one("SELECT notion_page_id FROM sources")["notion_page_id"] == "old"
    fake.status = 404  # the old page is gone already (deleted in Notion): a new one is right
    delivery.deliver("v")
    assert ("POST", "/pages") in calls


def test_a_page_made_but_whose_answer_was_lost_is_taken_up_not_registered(home, monkeypatch):
    # S07: Notion made the page but the answer was lost; the next try finds it in the library: it is ours, carry on
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.notion.client import NotionError
    _run_fixture(home)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    db.exec("UPDATE sources SET current_run_id='r'")
    calls, made = [], []

    class Fake:
        def _req(self, method, path, **kw):
            calls.append((method, path))
            if path == "/pages":
                made.append({"id": "made", "url": "https://notion/made", "archived": False, "created_by": {"id": "bot"},
                             "properties": {"Name": {"type": "title", "title": [{"plain_text": "交易员汤姆-20260908-题眼"}]}}})
                raise NotionError(599, "the answer was lost on the way back")
            if path.endswith("/query"):
                return {"results": made}
            if path.startswith("/blocks"):
                raise NotionError(418, "stop: writing would go on here")
            return LIB

        def page(self, pid):
            return {"archived": False, "icon": None}

        def me(self):
            return {"id": "bot", "type": "bot"}
    monkeypatch.setattr(delivery, "client", lambda: Fake())
    assert delivery.deliver("v")["status"] == "FAILED"
    assert delivery.deliver("v")["status"] != "REGISTERED"
    assert db.one("SELECT notion_page_id FROM sources")["notion_page_id"] == "made" and calls.count(("POST", "/pages")) == 1


def test_a_run_waiting_for_the_youtube_login_keeps_waiting_through_an_error_nobody_knows(home, monkeypatch):
    # S12: nine days waiting for the login, then one retry is answered 403 — that is no reason to give the issue up
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    from assetizer.youtube import AcquireError
    t = now()
    for sid, wait in (("m", "YOUTUBE_AUTH"), ("p", None)):
        db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES (?,'NOT_STARTED',?,?)", sid, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, wait_reason, attempts, created_at) VALUES (?,?,'QUEUED',?,7,?)",
                f"{sid}-r1", sid, wait, t)

    def acquire(ctx):
        raise AcquireError("UNKNOWN", "ERROR: unable to download video data: HTTP Error 403: Forbidden")
    monkeypatch.setattr(runner, "STAGES", [("ACQUIRE", acquire, True)])
    assert runner._execute("m-r1") == "WAITING"
    # and waiting is not a restart: the count starts again, so the next worker restart does not end it
    assert db.one("SELECT production, wait_reason, attempts FROM runs WHERE id='m-r1'") == \
        {"production": "WAITING", "wait_reason": "YOUTUBE_AUTH", "attempts": 0}
    assert runner._execute("p-r1") == "FAILED"  # not waiting before: an unknown error is a failure as ever


def test_the_youtube_login_alert_stays_while_a_video_still_waits_for_it(home, monkeypatch):
    # S12 / S50: a video whose metadata is cached finishing RESOLVE says nothing about the login a members' video needs
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    t = now()
    for sid, state, wait in (("m", "WAITING", "YOUTUBE_AUTH"), ("p", "QUEUED", None), ("q", "QUEUED", None), ("r", "QUEUED", None)):
        db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES (?,'NOT_STARTED',?,?)", sid, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, wait_reason, created_at) VALUES (?,?,?,?,?)", f"{sid}-r1", sid, state, wait, t)
    db.raise_alert("YOUTUBE_AUTH", "critical", "YouTube 需要登录或会员权限", "…")
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", lambda ctx: {"cached": True}, True)])
    runner._execute("p-r1")
    assert db.one("SELECT 1 FROM alerts WHERE key='YOUTUBE_AUTH' AND resolved_at IS NULL")
    db.exec("UPDATE runs SET production='COMPLETE', wait_reason=NULL WHERE id='m-r1'")  # the members' video got through
    runner._execute("q-r1")  # nobody waits now, but this one did not ask YouTube either
    assert db.one("SELECT 1 FROM alerts WHERE key='YOUTUBE_AUTH' AND resolved_at IS NULL")
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", lambda ctx: {"title": "read from YouTube"}, True)])
    runner._execute("r-r1")
    assert not db.one("SELECT 1 FROM alerts WHERE key='YOUTUBE_AUTH' AND resolved_at IS NULL")
    assert "视频测试" in runner.ALERT_TEXT["YOUTUBE_AUTH"][1]  # how to go on at once, not only "every 30 minutes"


def test_an_outage_while_reading_the_video_holds_the_run(home, monkeypatch):
    # S14: the box offline for hours — reading the video's metadata (RESOLVE) waits for the network like the rest
    import subprocess
    from assetizer import db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now
    t = now()
    outages = [youtube.AcquireError(youtube.classify(s), s) for s in (
        "ERROR: [youtube] x: Unable to download API page: <urlopen error [Errno 113] No route to host>",
        "ERROR: [youtube] x: Unable to download webpage: <urlopen error [Errno -2] Name or service not known>",
        "ERROR: [youtube] x: Unable to download API page: HTTP Error 503: Service Unavailable",
        "ERROR: [youtube] x: HTTP Error 429: Too Many Requests")] + [subprocess.TimeoutExpired("yt-dlp", 180)]
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", acquire.resolve, True)])
    for i, e in enumerate(outages):
        db.exec("INSERT INTO sources(id, url, delivery, created_at, updated_at) VALUES (?,?,'NOT_STARTED',?,?)", f"v{i}", f"v{i}", t, t)
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?,?,'QUEUED',?)", f"v{i}-r1", f"v{i}", t)

        def metadata(url, e=e):
            raise e
        monkeypatch.setattr(youtube, "metadata", metadata)
        assert runner._execute(f"v{i}-r1") == "WAITING", e
        assert db.one("SELECT wait_reason FROM runs WHERE id=?", f"v{i}-r1")["wait_reason"] == "NETWORK"


def test_the_ways_a_dead_network_shows_in_agy_are_outages():
    # S14: Go's resolver and dialer when the box has no network
    for said in ('dial tcp: lookup cloudcode-pa.googleapis.com on 127.0.0.11:53: server misbehaving',
                 'Post "https://cloudcode-pa.googleapis.com/v1": dial tcp 142.250.1.1:443: connect: network is unreachable',
                 'dial tcp: lookup oauth2.googleapis.com: Temporary failure in name resolution',
                 'dial tcp 142.250.1.1:443: connect: no route to host'):
        assert classify_error(said) == "PROVIDER_TRANSIENT", said


def test_an_outage_in_a_stage_that_may_fail_holds_the_run_instead_of_skipping_it(home, monkeypatch):
    # S14: 选片 or 边界 meeting a dead network wait for it; going on without them delivers a part of the issue
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.providers.agy import ProviderError
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)

    def select(ctx):
        raise ProviderError("PROVIDER_TRANSIENT", "dial tcp: connect: network is unreachable")
    monkeypatch.setattr(runner, "STAGES", [("SELECT", select, False)])
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT production, wait_reason FROM runs WHERE id='v-r1'") == {"production": "WAITING", "wait_reason": "NETWORK"}


def test_a_download_that_runs_out_of_time_waits_instead_of_failing(home, monkeypatch):
    # S45: a three-hour replay throttled past the download's six hours: not the video's fault, try again later
    import subprocess
    from assetizer import db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, url, duration, delivery, created_at, updated_at) VALUES ('v','v',10800,'NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)
    monkeypatch.setattr(acquire, "_free_bytes", lambda p: 10**12)

    def download(url, dest):
        raise subprocess.TimeoutExpired("yt-dlp", 6 * 3600)
    monkeypatch.setattr(youtube, "download", download)
    monkeypatch.setattr(runner, "STAGES", [("ACQUIRE", acquire.acquire, True)])
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "NETWORK"


def test_one_signed_out_answer_is_checked_against_agys_usage_before_it_pauses_production(home, monkeypatch):
    # S16: right when a quota window resets one call can answer 401; agy's /usage (no turn, no quota) decides
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.providers import agy
    from assetizer.providers.agy import ProviderError
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)

    def knowledge(ctx):
        raise ProviderError("AGY_AUTH_REQUIRED", "HTTP 401 UNAUTHENTICATED")
    monkeypatch.setattr(runner, "STAGES", [("KNOWLEDGE", knowledge, True)])
    monkeypatch.setattr(agy.AgyProvider, "usage", lambda self: {"ok": True, "checked_at": t, "groups": []})
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "NETWORK"
    assert not db.setting("queue_paused") and not db.one("SELECT 1 FROM alerts WHERE key='AGY_AUTH_REQUIRED'")
    # really signed out: /usage says so too
    db.exec("UPDATE runs SET production='QUEUED' WHERE id='v-r1'")
    monkeypatch.setattr(agy.AgyProvider, "usage", lambda self: {"ok": False, "error": "AGY_AUTH_REQUIRED", "checked_at": t})
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "AGY_AUTH_REQUIRED"
    assert db.setting("queue_paused")["reason"] == "AGY_AUTH_REQUIRED"


def test_the_weekly_limit_in_any_words_is_the_quota():
    # S16: agy's own words for the weekly limit are no network outage
    assert classify_error("You have hit your weekly limit for Gemini Pro. It refreshes in 2 days.") == "PROVIDER_QUOTA"
    assert classify_error("Error 429 RESOURCE_EXHAUSTED: usage limit reached for this week") == "PROVIDER_QUOTA"


def test_the_quota_alert_says_when_the_quota_comes_back(home):
    # S16: the 刊 and Telegram should say when production goes on, not only that it probes every 10 minutes
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    db.set_setting("agy_quota", {"ok": True, "production_group": "Gemini Pro", "groups": [{"name": "Gemini Pro", "buckets": [
        {"window": "5h", "remaining": 0.4, "reset_at": "2026-10-30T02:00:00+00:00", "disabled": False},
        {"window": "weekly", "remaining": 0, "reset_at": "2026-10-30T23:34:00+00:00", "disabled": False}]}]})
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)
    runner._wait("v-r1", "PROVIDER_QUOTA", "quota")
    assert "周六 07:34" in db.one("SELECT body FROM alerts WHERE key='PROVIDER_QUOTA'")["body"]


def test_a_retry_after_the_video_was_cleaned_up_fetches_it_again(home):
    # S17: a failed issue's video goes with retention after 7 days; retrying then must not start from 理解 without it
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    t = now()
    db.exec("INSERT INTO sources(id, delivery, media_state, media_path, created_at, updated_at) VALUES ('v','NOT_STARTED','PURGED','/gone/v.mkv',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, error_class, created_at) VALUES ('v-r1','v','FAILED','MODALITY_ROUTING_DEVIATION',?)", t)
    for i, (name, status) in enumerate([("RESOLVE", "DONE"), ("PLAYBOOK", "DONE"), ("ACQUIRE", "DONE"), ("CHUNK", "DONE"), ("UNDERSTAND", "FAILED")]):
        db.exec("INSERT INTO stages(run_id, name, ord, status) VALUES ('v-r1',?,?,?)", name, i, status)
    assert TestClient(web.app).post("/api/runs/v-r1/retry", headers={"host": "127.0.0.1"}, json={}).json() == {"ok": True}
    assert {r["name"]: r["status"] for r in db.q("SELECT name, status FROM stages WHERE run_id='v-r1'")} == \
        {"RESOLVE": "DONE", "PLAYBOOK": "DONE", "ACQUIRE": "PENDING", "CHUNK": "PENDING", "UNDERSTAND": "PENDING"}


def test_what_telegram_could_not_take_is_sent_once_it_answers_and_the_engine_shows_it(home, monkeypatch):
    # S18: the box's way out to Telegram is down while the YouTube login runs out: nothing is lost, it comes later
    from fastapi.testclient import TestClient
    from assetizer import db, notify
    from assetizer.web import app as web
    secrets = home / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "telegram-bot-token").write_text("123:test")
    (secrets / "telegram-chat-id").write_text("42")
    sent, up = [], [False]

    def api(token, method, **payload):
        if not up[0]:
            return {"ok": False, "description": "ConnectError"}
        sent.append(payload["text"])
        return {"ok": True}
    monkeypatch.setattr(notify, "_api", api)
    db.raise_alert("YOUTUBE_AUTH", "critical", "YouTube 需要登录或会员权限", "同步一次 CookieCloud")
    notify.run_failed("x", "UNKNOWN", "boom")  # no such run: nothing to say
    notify.send_digest()
    assert not sent and not db.setting("digest_date")  # the digest is not marked sent while it was not
    t = TestClient(web.app).get("/api/engine", headers={"host": "127.0.0.1"}).json()["telegram"]
    assert t["token"] and t["chat"] and not t["ok"] and t["pending"] == 1
    up[0] = True
    notify.flush()
    assert len(sent) == 1 and "YouTube 需要登录或会员权限" in sent[0] and "没发出去" in sent[0]
    assert TestClient(web.app).get("/api/engine", headers={"host": "127.0.0.1"}).json()["telegram"]["pending"] == 0
    notify.send_digest()
    assert db.setting("digest_date")


def test_two_trial_requests_that_overlap_queue_the_run_once(home, monkeypatch):
    # S20: 「试跑」 clicked twice on a slow line: the second request checks before the first one's row is in
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO sources(id, channel_id, created_at, updated_at) VALUES ('s1','w',?,?)", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?)", t)
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('x',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('busy','x','QUEUED',?)", t)  # holds dispatch
    db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, created_at) VALUES ('w','s1',?,?)", v2, t)
    real = db.one
    monkeypatch.setattr(tuning.db, "one", lambda sql, *a: None if "FROM trial_queue q" in sql else real(sql, *a))
    out = tuning.trial("w", v2, ["s1"])
    monkeypatch.setattr(tuning.db, "one", real)
    assert out == [{"source_id": "s1", "playbook_id": v2, "skipped": "已在排队"}]
    assert db.one("SELECT COUNT(*) n FROM trial_queue WHERE status='PENDING'")["n"] == 1


def test_two_dispatchers_at_once_send_a_queued_trial_once(home, monkeypatch):
    # S20: the web (right after queueing) and the worker both dispatch: the second must not submit it again (was a 500)
    from assetizer import db, tuning
    from assetizer.pipeline import runner
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, created_at) VALUES ('w','s1',?,?)", v2, t)
    stale = db.q("SELECT * FROM trial_queue WHERE status='PENDING' ORDER BY id")
    db.exec("UPDATE trial_queue SET status='DISPATCHED'")  # the other dispatcher took it just now
    real = db.q
    monkeypatch.setattr(tuning.db, "q", lambda sql, *a: stale if "FROM trial_queue WHERE status='PENDING'" in sql else real(sql, *a))
    monkeypatch.setattr(runner, "submit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("submitted twice")))
    assert tuning.dispatch_next() is None


def test_two_trial_requests_at_the_same_moment_queue_a_pair_once(home, monkeypatch):
    # S20: two 「试跑」 at the same moment (two tabs, or a click while the worker writes): the later one read the queue
    # before the first one's row was in, and wrote after that row had been dispatched — no PENDING row to collide with
    import threading
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    t, vid = now(), "SSSSSSSSSS1"
    db.exec("INSERT INTO sources(id, channel_id, created_at, updated_at) VALUES (?,'w',?,?)", vid, t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w',?,?)", vid, t)
    looked, first_done = threading.Event(), threading.Event()
    real = db.one

    def one(sql, *a):
        r = real(sql, *a)
        if threading.current_thread().name == "later" and "FROM trial_queue q" in sql and not looked.is_set():
            looked.set()
            first_done.wait(1.5)  # the first request goes through meanwhile — unless the later one holds the queue
        return r
    monkeypatch.setattr(tuning.db, "one", one)
    later = threading.Thread(target=tuning.trial, args=("w", v2, [vid]), name="later")
    later.start()
    looked.wait(5)
    tuning.trial("w", v2, [vid])
    first_done.set()
    later.join(10)
    monkeypatch.setattr(tuning.db, "one", real)
    assert db.one("SELECT COUNT(*) n FROM trial_queue WHERE status!='CANCELLED'")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM runs WHERE source_id=?", vid)["n"] == 1


def test_two_dispatchers_at_the_same_moment_start_a_sample_once(home, monkeypatch):
    # S20: the web (right after queueing) and the worker dispatch at the same moment, each the next version of one
    # sample: the later one must neither start the video again nor book its version on the other one's run
    import threading
    from assetizer import db, tuning
    from assetizer.util import now
    v1, v2 = _tuning_fixture()
    t, vid = now(), "SSSSSSSSSS1"
    db.exec("INSERT INTO sources(id, channel_id, created_at, updated_at) VALUES (?,'w',?,?)", vid, t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w',?,?)", vid, t)
    for v in (v1, v2):
        db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, created_at) VALUES ('w',?,?,?)", vid, v, t)
    looked, first_done = threading.Event(), threading.Event()
    real_one, real_q = db.one, db.q
    later = lambda: threading.current_thread().name == "later"  # noqa: E731

    def q(sql, *a):  # the later one read the line just after the first one took v1
        rows = real_q(sql, *a)
        return [r for r in rows if r["playbook_id"] == v2] if later() and "FROM trial_queue WHERE status='PENDING'" in sql else rows

    def one(sql, *a):
        r = real_one(sql, *a)
        if later() and sql.startswith("SELECT 1 FROM runs WHERE source_id=?") and not looked.is_set():
            looked.set()
            first_done.wait(1.5)  # the first one dispatches meanwhile — unless the later one holds the queue
        return r
    monkeypatch.setattr(tuning.db, "q", q)
    monkeypatch.setattr(tuning.db, "one", one)
    th = threading.Thread(target=tuning.dispatch_next, name="later")
    th.start()
    looked.wait(5)
    tuning.dispatch_next()
    first_done.set()
    th.join(10)
    monkeypatch.setattr(tuning.db, "q", real_q)
    monkeypatch.setattr(tuning.db, "one", real_one)
    runs = db.q("SELECT id, playbook_id FROM runs WHERE source_id=?", vid)
    assert len(runs) == 1
    for row in db.q("SELECT playbook_id, run_id FROM trial_queue WHERE status='DISPATCHED'"):
        assert {"id": row["run_id"], "playbook_id": row["playbook_id"]} in runs
    assert db.one("SELECT COUNT(*) n FROM trial_queue WHERE status='PENDING'")["n"] == 1


def test_saving_the_same_learning_goal_again_while_it_compiles_compiles_it_once(home, monkeypatch):
    # S20: 「保存并编译」 twice (a double click, a second tab): one compile, one new draft
    from fastapi.testclient import TestClient
    from assetizer.web import app as web
    _tuning_fixture()
    started = []
    monkeypatch.setattr(web, "_bg", lambda fn, *a, label=None: started.append(a))
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    text = "学习目标：" + "看懂主讲人对宏观与资产配置的判断，记下条件与依据。" * 3
    for _ in range(2):
        assert c.post("/api/channels/w/prompt", headers=h, json={"text": text, "note": None}).status_code == 200
    assert len(started) == 1


def test_retrying_a_run_twice_is_not_an_error(home):
    # S20: the second click of 「重试」 finds the run queued already: fine, said in Chinese otherwise
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','FAILED',?)", t)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    assert c.post("/api/runs/v-r1/retry", headers=h, json={}).status_code == 200
    assert c.post("/api/runs/v-r1/retry", headers=h, json={}).status_code == 200
    db.exec("UPDATE runs SET production='COMPLETE' WHERE id='v-r1'")
    r = c.post("/api/runs/v-r1/retry", headers=h, json={})
    assert r.status_code == 409 and "重试" in r.json()["detail"]


def test_a_database_locked_past_its_timeout_sends_the_run_back_to_the_queue(home, monkeypatch):
    # S21: eight runs writing at once on a slow disk; a write waits out the busy timeout: not this issue's failure
    import sqlite3
    from assetizer import db, notify
    from assetizer.pipeline import runner
    from assetizer.util import now
    t = now()
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: sent.append(text))
    for sid in ("a", "b"):
        db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES (?,'NOT_STARTED',?,?)", sid, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?,?,'QUEUED',?)", f"{sid}-r1", sid, t)

    def locked(ctx):
        raise sqlite3.OperationalError("database is locked")
    for rid, blocking in (("a-r1", True), ("b-r1", False)):
        monkeypatch.setattr(runner, "STAGES", [("CLIPS", locked, blocking)])
        runner._execute(rid)
        assert db.one("SELECT production, error_class FROM runs WHERE id=?", rid) == {"production": "QUEUED", "error_class": None}
    assert not sent


def test_a_crash_around_a_run_does_not_leave_it_marked_running(home, monkeypatch):
    # S21: when even recording the failure hit the lock, the run stayed RUNNING with no thread on it until a restart
    import time
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','RUNNING',?)", t)

    def crash(run_id):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(runner, "execute", crash)
    w = runner.Worker()
    w.start("v-r1")
    for _ in range(100):
        if not w.active["v-r1"].is_alive():
            break
        time.sleep(0.01)
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "QUEUED"


def test_resuming_a_delivery_keeps_what_you_added_or_changed_on_the_page(home, monkeypatch):
    # S24: the page looked done (only the last write, the properties, failed); you added a note and changed a property
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.notion.client import NotionError
    _run_fixture(home)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    db.exec("UPDATE sources SET current_run_id='r'")
    blocks, props, calls, fail = [], {}, [], [True]

    schema = M.schema_of(LIB)

    def canon(key, v):  # a property as Notion answers it, by name, with its id along with the value
        pid = key if key in schema else next(k for k, x in schema.items() if x["name"] == key)
        return schema[pid]["name"], {"id": pid, **v}

    class Fake:
        def _req(self, method, path, **kw):
            calls.append((method, path))
            if path == "/databases/lib":
                return LIB
            if path.endswith("/query"):
                return {"results": []}
            if method == "POST" and path == "/pages":
                props.update(canon(k, v) for k, v in kw["json"]["properties"].items())
                return {"id": "pg", "url": "https://notion/pg", "properties": dict(props)}
            if method == "PATCH" and path == "/pages/pg":
                if fail[0]:
                    raise NotionError(502, "Bad Gateway")
                props.update(canon(k, v) for k, v in kw["json"]["properties"].items())
                return {"id": "pg", "properties": dict(props)}
            if method == "DELETE":
                blocks[:] = [b for b in blocks if b["id"] != path.rsplit("/", 1)[1]]
                return {}
            raise AssertionError(f"unexpected {method} {path}")

        def page(self, pid):
            return {"id": pid, "archived": False, "icon": None, "properties": dict(props)}

        def me(self):
            return {"id": "bot"}

        def upload(self, path, content_type):
            return {"id": "up"}

        def append(self, pid, children):
            out = []
            for c in children:
                b = {**c, "id": f"b{len(blocks)}", "created_by": {"id": "bot"}}
                if c["type"] in ("image", "video"):
                    b[c["type"]] = {"type": "file"}
                blocks.append(b)
                out.append(b)
            return {"results": out}

        def children(self, pid):
            return list(blocks)
    monkeypatch.setattr(delivery, "client", lambda: Fake())
    assert delivery.deliver("v")["status"] == "FAILED"
    blocks.append({"id": "note", "type": "paragraph", "created_by": {"id": "you"},
                   "paragraph": {"rich_text": [{"type": "text", "text": {"content": "我的笔记"}, "plain_text": "我的笔记"}]}})
    props.update([canon("AI 摘要", {"type": "rich_text", "rich_text": [{"plain_text": "我改的摘要"}]})])
    fail[0] = False
    assert delivery.deliver("v")["status"] == "DELIVERED"
    assert any(b["id"] == "note" for b in blocks) and ("DELETE", "/blocks/note") not in calls
    assert props["AI 摘要"]["rich_text"] == [{"plain_text": "我改的摘要"}]


def test_a_video_whose_length_is_not_known_yet_waits_instead_of_being_too_short(home, monkeypatch):
    # S27: a live just over (or a premiere) has no duration yet; intake took it in — it is not "too short" for that
    from datetime import datetime, timedelta, timezone
    from assetizer import db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('t','Tom','UCt','TRACKED','LOCKED',?,?)", t, t)
    db.set_setting("ingest:t", {"enabled": True, "since": t, "min_minutes": 10})
    db.exec("INSERT INTO sources(id, url, channel_id, delivery, created_at, updated_at) VALUES ('v','v','t','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)
    db.event("ingest", "收录：Tom · live", "v-r1")
    monkeypatch.setattr(youtube, "metadata", lambda url: {"id": "v", "title": "live", "channel_id": "UCt", "channel": "Tom",
                                                          "duration": None, "live_status": "post_live", "upload_date": "20261027"})
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", acquire.resolve, True)])
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT production, wait_reason FROM runs WHERE id='v-r1'") == {"production": "WAITING", "wait_reason": "LIVE_IN_PROGRESS"}
    # a day on and still no length: it is taken as it is rather than waited for forever
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    db.exec("UPDATE runs SET production='QUEUED', created_at=? WHERE id='v-r1'", day_ago)
    assert runner._execute("v-r1") == "COMPLETE"


def test_a_video_cancelled_as_too_short_while_its_length_was_unknown_is_taken_again(home, monkeypatch):
    # S27: until now a video whose length could not be read yet was taken for 0 and cancelled as too short, and every
    # scan after counted it as 已在; listed again with a length long enough, it is judged again
    from datetime import datetime, timedelta, timezone
    from assetizer import db, delivery, ingest
    from assetizer.util import now
    t = now()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, stage, created_at, updated_at) VALUES ('t','Tom','UCt','TRACKED','LOCKED',?,?)", t, t)
    db.set_setting("ingest:t", {"enabled": True, "since": week_ago, "min_minutes": 10})
    for vid, production, error_class in (("LiveEnded01", "CANCELLED", "SKIPPED_SHORT"), ("Produced001", "COMPLETE", None)):
        db.exec("INSERT INTO sources(id, url, channel_id, created_at, updated_at) VALUES (?,?,'t',?,?)", vid, vid, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, error_class, created_at) VALUES (?,?,?,?,?)", f"{vid}-r1", vid, production, error_class, t)
    entries = [{"id": v, "title": v, "published": t, "duration": 5400, "approx": True, "tab": "streams"} for v in ("LiveEnded01", "Produced001")]
    monkeypatch.setattr(ingest, "look", lambda cid, limit=15, shorts=False: (entries, None, {"videos", "streams"}))
    monkeypatch.setattr(delivery, "registered_in_target", lambda src: None)
    out = ingest.scan(db.one("SELECT * FROM channels WHERE id='t'"), db.setting("ingest:t"))
    assert (out["queued"], out["have"], out["ids"]) == (1, 1, ["LiveEnded01"])
    assert db.one("SELECT production FROM runs WHERE id='LiveEnded01-r2'") == {"production": "QUEUED"}
    assert db.one("SELECT 1 FROM events WHERE run_id='LiveEnded01-r2' AND kind='ingest'")  # held to the minimum again


def test_relocking_while_the_library_cannot_be_checked_keeps_the_page_already_written(home, monkeypatch):
    # S33: 回到调试期 → 再定稿 with Notion not answering: the sample whose page Assetizer wrote keeps it by default
    from fastapi.testclient import TestClient
    from assetizer import db, delivery
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("""INSERT INTO sources(id, channel_id, title, delivery, notion_page_id, notion_url, notion_synced_run, current_run_id,
               created_at, updated_at) VALUES ('s1','w','样片','STALE','pg','https://notion/pg','r1','r2',?,?)""", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?)", t)
    for rid in ("r1", "r2"):
        db.exec("INSERT INTO runs(id, source_id, production, playbook_id, created_at, finished_at) VALUES (?,'s1','COMPLETE',?,?,?)", rid, v2, t, t)

    def down(src, ids):
        raise RuntimeError("Notion 502")
    monkeypatch.setattr(delivery, "registered_many", down)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    lp = c.get(f"/api/channels/w/lock-preview?playbook_id={v2}", headers=h).json()
    s = next(x for x in lp["samples"] if x["source_id"] == "s1")
    assert lp["library_checked"] is False and s["library"] and s["library"]["owned"]  # the sheet then defaults to 保留
    assert c.post("/api/channels/w/lock", headers=h, json={"playbook_id": v2, "samples": [{"source_id": "s1", "action": "write"}]}).status_code == 200
    assert db.one("SELECT delivery FROM sources WHERE id='s1'")["delivery"] != "STALE"  # its page is not archived for a new one


def test_moving_to_a_library_where_a_property_holds_one_option_or_many_carries_the_value_over():
    # S38 (0.4.21): 换库 to a library whose same-named property is a multi-select, or a select — no 500, no ['复', '盘']
    from assetizer.notion import mapping as M
    cfg = {"database_id": "a", "properties": {
        "k": {"name": "类型", "type": "select", "source": "fixed", "value": "复盘"},
        "m": {"name": "标签", "type": "multi_select", "source": "fixed", "value": ["美股", "期权"]},
        "n": {"name": "主题", "type": "multi_select", "source": "fixed", "value": ["宏观"]}}}
    schema = {"k2": {"name": "类型", "type": "multi_select", "options": ["复盘"], "option_ids": {"o1": "复盘"}},
              "m2": {"name": "标签", "type": "select", "options": ["美股", "期权"], "option_ids": {"o2": "美股", "o3": "期权"}},
              "n2": {"name": "主题", "type": "status", "options": ["宏观"], "option_ids": {"o4": "宏观"}}}
    out, repick, dropped = M.remap(cfg, schema)
    assert out["properties"]["k2"]["value"] == ["复盘"] and out["properties"]["n2"]["value"] == "宏观"
    assert out["properties"]["m2"].get("value") is None and repick == ["标签"]  # two options where one goes: pick again


OUTDATED = ["ERROR: [youtube] x: Unable to extract initial player response; please report this issue on https://github.com/yt-dlp/yt-dlp/issues",
            "ERROR: [youtube] x: Failed to extract any player response",
            "WARNING: [youtube] x: Signature extraction failed: Some formats may be missing\nERROR: [youtube] x: Requested format is not available",
            "WARNING: [youtube] x: n challenge solving failed: Some formats may be missing\nERROR: unable to download video data: HTTP Error 403: Forbidden",
            "WARNING: [youtube] x: Only images are available for download. use --list-formats to see them",
            "ERROR: [youtube:tab] UCt: The page needs to be reloaded."]


def test_youtube_changing_under_yt_dlp_is_said_once_as_the_tool_being_out_of_date(home, monkeypatch):
    # S43: every video failing on its own as 未知错误 hides the one thing to do — update yt-dlp
    from assetizer import db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now
    assert {youtube.classify(s) for s in OUTDATED} == {"TOOL_OUTDATED"}
    t = now()
    db.exec("INSERT INTO sources(id, url, delivery, created_at, updated_at) VALUES ('v','v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)

    def metadata(url):
        raise youtube.AcquireError(youtube.classify(OUTDATED[0]), OUTDATED[0])
    monkeypatch.setattr(youtube, "metadata", metadata)
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", acquire.resolve, True)])
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "YTDLP_OUTDATED"
    assert "下载工具" in db.one("SELECT title FROM alerts WHERE key='YTDLP_OUTDATED' AND resolved_at IS NULL")["title"]
    assert "YTDLP_OUTDATED" in runner.RETRIED_WAITS  # tried again every 30 minutes: a passing hiccup heals by itself


def test_a_channel_listing_that_fails_because_yt_dlp_is_out_of_date_raises_the_same_alert(home, monkeypatch):
    # S43: intake reading the channel meets the same thing before any video does
    from assetizer import db, ingest
    _board_fixture(home, monkeypatch)

    def look(cid, limit=15, shorts=False):
        raise RuntimeError(f"频道列表与 RSS 都读不到：{OUTDATED[-1]}")
    monkeypatch.setattr(ingest, "look", look)
    ingest.scan(db.one("SELECT * FROM channels WHERE id='t'"), db.setting("ingest:t"))
    assert db.one("SELECT 1 FROM alerts WHERE key='YTDLP_OUTDATED' AND resolved_at IS NULL")


def test_runs_held_for_agy_reading_video_go_on_once_its_self_test_passes_after_a_manual_resume(home, monkeypatch):
    # S46: 「恢复队列」 clicked while agy could not read video left these runs waiting, and the alert up, for good
    import time
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('v','NOT_STARTED',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, wait_reason, created_at) VALUES ('v-r1','v','WAITING','AGY_MEDIA_READ',?)", t)
    db.raise_alert("AGY_MEDIA_READ", "critical", "agy 读不了视频，生产已暂停", "…")
    db.set_setting("agy_media_version", "1.4.8")  # agy itself did not change since
    monkeypatch.setattr(runner, "media_self_test", lambda: {"ok": True, "version": "1.4.8"})
    monkeypatch.setattr(runner.providers, "get", lambda: type("P", (), {"version": lambda self: "1.4.8", "name": "agy"})())
    w = runner.Worker()
    m = time.monotonic()
    w.last.update({k: m for k in w.last}, media=m - runner.MEDIA_CHECK_EVERY - 1)
    w.housekeeping()
    assert db.one("SELECT production FROM runs WHERE id='v-r1'")["production"] == "QUEUED"
    assert not db.one("SELECT 1 FROM alerts WHERE key='AGY_MEDIA_READ' AND resolved_at IS NULL")


def test_a_notion_error_page_reads_as_one_plain_sentence():
    # S49: Cloudflare's 502 page used to land in the log, the issue's reason and the channel page as raw HTML
    from assetizer.notion.client import NotionError
    page = "<!DOCTYPE html>\n<!--[if lt IE 7]> <html class=\"no-js ie6 oldie\" lang=\"en-US\"> <![endif]-->\n<html><head><title>502</title>"
    e = NotionError(502, page)
    assert str(e) == "Notion 暂时不可用（502）" and e.body == page and e.status == 502
    j = NotionError(400, '{"object":"error","status":400,"code":"validation_error","message":"body.properties.Name is invalid"}')
    assert str(j) == "Notion 400: body.properties.Name is invalid"


def test_several_links_pasted_at_once_are_all_queued(home):
    # S55: three links from notes, one per line (or run together by a one-line field): every one is queued, once
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    for n, sep in enumerate(("\n", " ", "")):
        ids = [f"{x * 10}{n}" for x in "ABC"]
        links = [f"https://youtu.be/{ids[0]}?si=AbCdEfGhIjKl", f"https://www.youtube.com/watch?v={ids[1]}&t=60s",
                 f"https://www.youtube.com/live/{ids[2]}"]
        r = c.post("/api/submit", headers=h, json={"url": sep.join(links + links[:1])}).json()
        assert [i["source_id"] for i in r["items"]] == ids, repr(sep)
        assert r["source_id"] == ids[0]  # the one-link answer stays as it was
        assert db.one("SELECT COUNT(*) n FROM runs WHERE source_id IN (?,?,?)", *ids)["n"] == 3
    one = c.post("/api/submit", headers=h, json={"url": "https://youtu.be/BBBBBBBBBB0"}).json()
    assert "items" not in one and one["existing"]

def test_the_shortest_length_to_take_in_is_kept_within_bounds(home, monkeypatch):
    # S57: -5, 999, "abc" — or the string "30" through 定稿 — no longer go in as they are (999 took in nothing at all,
    # "30" failed every run at RESOLVE); a refusal is one sentence, not a list the page shows as [object Object]
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.web import app as web
    _board_fixture(home, monkeypatch)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    for bad in (-5, 999, "abc", 2.5, ""):
        r = c.post("/api/channels/t/ingest", headers=h, json={"enabled": True, "min_minutes": bad})
        assert r.status_code == 400 and isinstance(r.json()["detail"], str) and "0–240" in r.json()["detail"], bad
    assert db.setting("ingest:t")["min_minutes"] == 10
    assert c.post("/api/channels/t/ingest", headers=h, json={"enabled": True, "min_minutes": 30}).status_code == 200
    v1, v2 = _tuning_fixture()
    t = db.one("SELECT created_at FROM channels WHERE id='t'")["created_at"]
    db.exec("INSERT INTO sources(id, channel_id, created_at, updated_at) VALUES ('s1','w',?,?)", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?)", t)
    db.exec("INSERT INTO runs(id, source_id, production, playbook_id, created_at) VALUES ('s1-r1','s1','COMPLETE',?,?)", v2, t)
    r = c.post("/api/channels/w/lock", headers=h, json={"playbook_id": v2, "samples": [], "ingest": {"enabled": True, "min_minutes": 999}})
    assert r.status_code == 400 and "0–240" in r.json()["detail"]


def test_a_page_left_open_on_another_device_after_lock_cannot_trial_or_adopt(home):
    # S62: locked on the desktop; the phone still shows 调试中 — 试跑 says the channel is locked (409: the page reloads),
    # and 采用 no longer swaps the version already in Notion (it marked the page for replacement)
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("INSERT INTO sources(id, channel_id, delivery, current_run_id, notion_page_id, created_at, updated_at) VALUES ('s1','w','DELIVERED','s1-r2','pg',?,?)", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','s1',?)", t)
    for rid, pb in (("s1-r1", v1), ("s1-r2", v2)):
        db.exec("INSERT INTO runs(id, source_id, production, playbook_id, created_at, finished_at) VALUES (?,'s1','COMPLETE',?,?,?)", rid, pb, t, t)
    db.exec("UPDATE channels SET stage='LOCKED' WHERE id='w'")
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    r = c.post("/api/channels/w/trial", headers=h, json={"playbook_ids": [v1], "source_ids": None})
    assert r.status_code == 409 and "定稿" in r.json()["detail"]
    r = c.post("/api/sources/s1/adopt", headers=h, json={"run_id": "s1-r1", "pinned": True})
    assert r.status_code == 409
    assert db.one("SELECT current_run_id, delivery FROM sources WHERE id='s1'") == {"current_run_id": "s1-r2", "delivery": "DELIVERED"}


def test_an_empty_headline_leaves_the_title_without_a_dangling_separator(home):
    # S63: no 题眼 → 「交易员汤姆-20261027」, not 「交易员汤姆-20261027-」 nor the video's own title in its place
    from assetizer import db
    from assetizer import preview as PV
    from assetizer.notion import mapping as M
    _run_fixture(home)
    db.exec("UPDATE sources SET headline=NULL")
    ctx = M.context(db.one("SELECT * FROM sources WHERE id='v'"), "r", {"name": "交易员汤姆"})
    assert ctx["headline"] == ""
    assert M.fill("{channel}-{date8}-{headline}", ctx) == "交易员汤姆-20260908"
    assert M.fill("{headline} · {channel}", ctx) == "交易员汤姆"
    assert "".join(p["text"] for p in PV.parts("{channel}-{date8}-{headline}", ctx)) == "交易员汤姆-20260908"
    assert M.value({"source": "template", "value": "{headline}"}, ctx) == ctx["video_title"]  # never an empty title


def test_a_runs_sections_and_events_are_found_by_index(home):
    # S64: a year in (2000 issues), the 调试台 matrix took 0.8–1.3 s scanning every section for each run; 36 ms indexed
    from assetizer import db
    plan = lambda sql: " ".join(r["detail"] for r in db.q("EXPLAIN QUERY PLAN " + sql, "r"))  # noqa: E731
    assert "USING INDEX sections_run" in plan("SELECT kind, COUNT(*) FROM sections WHERE run_id=? GROUP BY kind")
    assert "USING INDEX events_run" in plan("SELECT * FROM events WHERE run_id=? ORDER BY id")


# ---------------------------------------------------------------- the decisions on the edge review (a01–a19, 2026-10-28)
def test_a_trial_that_fails_is_seen_on_the_bench_not_pushed(sent):
    # S04 (a04): a trial failing while its channel is tuned is on the 调试台 already; only a real issue's failure is pushed
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('w','王','TRACKED','TUNING',?,?), ('t','汤姆','TRACKED','LOCKED',?,?)",
            t, t, t, t)
    for sid, cid, origin in (("W1", "w", "trial"), ("W2", "w", "manual"), ("T1", "t", "ingest")):
        db.exec("INSERT INTO sources(id, channel_id, title, delivery, created_at, updated_at) VALUES (?,?,?,'NOT_STARTED',?,?)", sid, cid, sid, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, origin, created_at) VALUES (?,?,'RUNNING',?,?)", f"{sid}-r1", sid, origin, t)
        runner._fail(f"{sid}-r1", RuntimeError("UNKNOWN: boom"))
    assert len(sent) == 1 and "T1" in sent[0] and "❌" in sent[0]
    assert {r["production"] for r in db.q("SELECT production FROM runs")} == {"FAILED"}


def test_a_partial_issue_waits_for_a_retry_that_completes_it(home, monkeypatch):
    # S09 (a08): an issue missing what a non-blocking stage did not finish (clips, stills, checks) is not written, by
    # whatever way it comes to Notion (定稿 pins it, 重新交付); once a retry of that version completes, it is
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.pipeline import runner
    _run_fixture(home)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    db.exec("UPDATE runs SET production='COMPLETE_PARTIAL'")
    db.exec("UPDATE sources SET current_run_id='r', adopt_pinned=1, delivery='NOT_STARTED'")  # as 定稿 leaves a sample

    class Fake:
        def _req(self, method, path, **kw):
            pytest.fail(f"nothing is written: {method} {path}")
    monkeypatch.setattr(delivery, "client", lambda: Fake())
    delivery.deliver_pending()
    assert db.one("SELECT delivery, delivery_reason FROM sources") == {"delivery": "HELD", "delivery_reason": "PARTIAL"}
    assert delivery.deliver_pending() is False and delivery.deliver("v")["status"] == "HELD"
    db.exec("UPDATE runs SET production='COMPLETE'")  # 重试 filled in what was missing
    runner.adopt_completed(db.one("SELECT * FROM sources"), "r")
    assert db.one("SELECT delivery, delivery_reason FROM sources") == {"delivery": "NOT_STARTED", "delivery_reason": None}


def test_youtube_refusing_the_download_waits_for_either_fix(home, monkeypatch):
    # S12 S43 (a09): HTTP 403 alone does not say whether the login or yt-dlp is at fault: the issue waits (retried every
    # 30 minutes) under an alert naming both, instead of failing as 未知错误
    from assetizer import db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now
    refused = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    assert youtube.classify(refused) == "YOUTUBE_REFUSED"
    assert youtube.classify("WARNING: [youtube] The provided YouTube account cookies are no longer valid.\n" + refused) == "YOUTUBE_REFUSED"
    assert youtube.classify(OUTDATED[3]) == "TOOL_OUTDATED"  # n challenge said: that one is known
    t = now()
    db.exec("INSERT INTO sources(id, url, delivery, duration, youtube_channel_id, created_at, updated_at) VALUES ('v','v','NOT_STARTED',600,'UC',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)

    def download(url, dest):
        raise youtube.AcquireError(youtube.classify(refused), refused)
    monkeypatch.setattr(youtube, "download", download)
    monkeypatch.setattr(acquire, "_free_bytes", lambda p: 10 ** 12)
    monkeypatch.setattr(runner, "STAGES", [("ACQUIRE", acquire.acquire, True)])
    assert runner._execute("v-r1") == "WAITING"
    assert db.one("SELECT wait_reason FROM runs WHERE id='v-r1'")["wait_reason"] == "YOUTUBE_REFUSED"
    a = db.one("SELECT severity, title, body FROM alerts WHERE key='YOUTUBE_REFUSED' AND resolved_at IS NULL")
    assert a["severity"] == "critical" and "拒绝" in a["title"] and "登录" in a["body"] and "yt-dlp" in a["body"]
    assert "YOUTUBE_REFUSED" in runner.RETRIED_WAITS


def test_an_alert_that_clears_within_ten_minutes_is_not_pushed(sent, monkeypatch):
    # S50 (a17): a hiccup that heals itself sends nothing; one still there after 10 minutes is pushed then, its recovery
    # after it; the urgent ones (sign-ins, quota, agy reading video, yt-dlp, the Notion token) still go at once
    import datetime as dt
    from assetizer import db, notify
    ago = lambda m: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=m)).isoformat(timespec="seconds")  # noqa: E731
    db.raise_alert("cookiecloud", "warning", "读不到 CookieCloud 的 YouTube 登录态", "x")
    notify.send_due_alerts()
    db.resolve_alert("cookiecloud")
    assert sent == []
    db.raise_alert("cookiecloud", "warning", "读不到 CookieCloud 的 YouTube 登录态", "x")  # back again: a new occurrence
    assert db.one("SELECT resolved_at FROM alerts WHERE key='cookiecloud'")["resolved_at"] is None
    notify.send_due_alerts()
    assert sent == []
    db.exec("UPDATE alerts SET created_at=? WHERE key='cookiecloud'", ago(11))
    notify.send_due_alerts()
    notify.send_due_alerts()
    assert len(sent) == 1 and "读不到 CookieCloud" in sent[0]
    db.resolve_alert("cookiecloud")
    assert len(sent) == 2 and sent[1].startswith("✅ 已恢复")
    db.raise_alert("cookiecloud", "warning", "读不到 CookieCloud 的 YouTube 登录态", "x")  # a third time: counted afresh
    notify.send_due_alerts()
    assert len(sent) == 2
    for key in ("YOUTUBE_AUTH", "PROVIDER_QUOTA", "notion-auth"):
        db.raise_alert(key, "warning", f"{key} 告警", "x")
    assert len(sent) == 5


def test_a_property_deleted_and_made_again_alike_is_taken_up():
    # S38 (a13): 内容所属 deleted in Notion and a select of that name made again: to the user the same property — taken
    # up; not when its type changed, nor when another mapping is on it already (those still stop for a pick)
    import copy
    from assetizer.notion import mapping as M
    cfg = M.preset("trader-tom", "lib", M.schema_of(LIB), None)

    def remade(kind="select"):
        lib = copy.deepcopy(LIB)
        lib["properties"].pop("内容所属")
        lib["properties"]["内容所属"] = {"id": "own2", "type": kind, kind: {"options": [{"id": "n1", "name": "@交易员汤姆"}]}}
        return M.schema_of(lib)
    s = remade()
    out = M.sync(cfg, s)
    assert "own" not in out["properties"] and M.problems(out, s) == {}
    assert out["properties"]["own2"] == {**cfg["properties"]["own"], "option_ids": {"@交易员汤姆": "n1"}}
    s = remade("multi_select")
    assert "own" in M.problems(M.sync(cfg, s), s)
    s = remade()
    taken = {**cfg, "properties": {**cfg["properties"], "own2": {"name": "内容所属", "type": "select", "source": "fixed", "value": "@交易员汤姆"}}}
    assert "own" in M.problems(M.sync(taken, s), s)


def _shut_out(monkeypatch, stderr, signed_in=True):
    """yt-dlp refusing with ``stderr`` under a CookieCloud login that carries (or not) a YouTube sign-in."""
    import contextlib
    import subprocess
    from assetizer import youtube

    @contextlib.contextmanager
    def login():
        yield ["--cookies", "/dev/null"], {"source": "cookiecloud", "signed_in": signed_in, "code": None}
    monkeypatch.setattr(youtube, "ytdlp_login", login)
    monkeypatch.setattr(youtube.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", stderr))


PRIVATE = "ERROR: [youtube] v: Private video. Sign in if you've been granted access to this video"
MEMBERS = "ERROR: [youtube] v: Join this channel to get access to members-only content like this video, and other exclusive perks."
REMOVED = "ERROR: [youtube] v: Video unavailable. This video has been removed by the uploader"


def test_a_video_no_login_can_open_is_said_unavailable_and_can_be_put_away(home, monkeypatch):
    # S44 (a15): signed in and still refused as private or members-only, or removed: 视频不可用 with why — no login to
    # wait for; 忽略 puts it away (only such a failure)
    from fastapi.testclient import TestClient
    from assetizer import db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now
    from assetizer.web import app as web
    rotated = "WARNING: [youtube] The provided YouTube account cookies are no longer valid.\n"
    assert youtube.classify(PRIVATE, signed_in=True) == "VIDEO_PRIVATE" and youtube.classify(PRIVATE) == "AUTH_REQUIRED"
    assert youtube.classify(MEMBERS, signed_in=True) == "VIDEO_MEMBERS" and youtube.classify(rotated + MEMBERS, signed_in=True) == "AUTH_REQUIRED"
    assert youtube.classify(REMOVED) == "VIDEO_REMOVED"
    assert youtube.classify("ERROR: [youtube] v: Video unavailable. The uploader has not made this video available in your country") == "SOURCE_UNAVAILABLE"
    t = now()
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", acquire.resolve, True)])
    for vid, said, signed_in in (("p", PRIVATE, True), ("q", PRIVATE, False), ("f", "ERROR: boom", True)):
        db.exec("INSERT INTO sources(id, url, title, delivery, created_at, updated_at) VALUES (?,?,?,'NOT_STARTED',?,?)", vid, vid, vid, t, t)
        db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES (?,?,'QUEUED',?)", f"{vid}-r1", vid, t)
        _shut_out(monkeypatch, said, signed_in)
        runner._execute(f"{vid}-r1")
    assert {r["id"]: (r["production"], r["wait_reason"] or r["error_class"]) for r in db.q("SELECT * FROM runs")} == {
        "p-r1": ("FAILED", "VIDEO_PRIVATE"), "q-r1": ("WAITING", "YOUTUBE_AUTH"), "f-r1": ("FAILED", "UNKNOWN")}
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    issues = {i["id"]: i for i in c.get("/api/overview", headers=h).json()["issues"]}
    assert issues["p"]["error_class"] == "VIDEO_PRIVATE"
    assert c.post("/api/runs/f-r1/ignore", headers=h, json={}).status_code == 409  # a failure to fix is not put away
    assert c.post("/api/runs/p-r1/ignore", headers=h, json={}).status_code == 200
    assert db.one("SELECT production, error_class FROM runs WHERE id='p-r1'") == {"production": "CANCELLED", "error_class": "VIDEO_PRIVATE"}


def test_a_video_taken_down_after_its_download_is_made_from_the_copy_here(home, monkeypatch):
    # S44 (a15 note): YouTube no longer shows it, but the video is downloaded already: the run does not ask YouTube again
    from assetizer import config, db, youtube
    from assetizer.pipeline import acquire, runner
    from assetizer.util import now, sha256_file
    media = config.get().media_dir / "v" / "source.mkv"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"video")
    t = now()
    db.exec("""INSERT INTO sources(id, url, delivery, duration, youtube_channel_id, media_state, media_path, media_sha256, media_bytes,
               created_at, updated_at) VALUES ('v','v','NOT_STARTED',600,'UC','CACHED',?,?,5,?,?)""", str(media), sha256_file(media), t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('v-r1','v','QUEUED',?)", t)
    _shut_out(monkeypatch, REMOVED)
    monkeypatch.setattr(youtube.subprocess, "run", lambda *a, **kw: pytest.fail("YouTube is not asked again"))
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", acquire.resolve, True), ("ACQUIRE", acquire.acquire, True)])
    assert runner._execute("v-r1") == "COMPLETE"


class _Lib:
    """The library as the a05/a07 tests need it: the schema, what a query finds (a link lookup finds nothing; the
    channel's pages with no link are ``hand``), the old page of ours (``blocks``, ``comments``; None: no right to read
    them), writes recorded. Making a new page is refused, which ends a delivery there."""

    def __init__(self, hand=(), blocks=(), comments=()):
        from assetizer.notion.client import NotionError
        self.err, self.hand, self.blocks, self.comments, self.calls = NotionError, list(hand), list(blocks), comments, []

    def _req(self, method, path, idempotent=None, **kw):
        self.calls.append((method, path))
        if method == "GET" and path == "/databases/lib":
            return LIB
        if method == "GET" and path == "/users/me":
            return {"id": "bot"}
        if path.endswith("/query"):
            return {"results": self.hand if "and" in kw["json"]["filter"] else [], "has_more": False}
        if method == "GET" and path.startswith("/blocks/"):
            return {"results": self.blocks, "has_more": False}
        if method == "GET" and path.startswith("/comments"):
            if self.comments is None:
                raise self.err(403, "Insufficient permissions for this endpoint.")
            return {"results": list(self.comments), "has_more": False}
        if method == "GET" and path.startswith("/pages/"):
            return {"id": path.split("/")[2], "archived": False, "properties": {}}
        if method == "PATCH" and path.startswith("/pages/"):
            return {"archived": True}
        if method == "POST" and path == "/pages":
            raise self.err(418, "stop: a page would be made here")
        raise AssertionError(f"unexpected Notion call {method} {path}")

    def page(self, pid):
        return self._req("GET", f"/pages/{pid}")

    def archive_page(self, pid):
        return self._req("PATCH", f"/pages/{pid}")

    def children(self, pid):
        return self._req("GET", f"/blocks/{pid}/children")["results"]

    def me(self):
        return self._req("GET", "/users/me")


def test_an_issue_a_hand_written_page_may_be_waits_for_a_word(home, monkeypatch):
    # S05 (a05): the library has 汤姆's page of that day with no video link (written by hand): a 补录 issue is not written
    # beside it — the 刊 asks 照写 or 算已有. By the day for a channel titled by date (YouTube's, the US Eastern one, the
    # MM/DD its title names), by the title for one titled with the video's own
    import datetime as dt
    from fastapi.testclient import TestClient
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    at = dt.datetime(2026, 10, 20, 0, 30, tzinfo=dt.timezone.utc).timestamp()  # 10/19 20:30 in New York
    src = {"title": "Tom 盘后复盘", "published_at": "20261020", "info": json.dumps({"timestamp": at})}
    assert delivery.days_of(src) == {"2026-10-20", "2026-10-19"}
    assert delivery.days_of({"title": "12/31 年终复盘", "published_at": "20270102", "info": "{}"}) == {"2027-01-02", "2026-12-31"}
    pages = [{"id": "p", "url": "u", "title": "王海滨：美联储的下一步！", "date": "2026-10-19"}]
    assert delivery.maybe_written(pages, delivery.days_of(src), src["title"], by_title=False)["id"] == "p"
    assert delivery.maybe_written(pages, {"2026-10-18"}, src["title"], by_title=False) is None
    assert delivery.maybe_written(pages, set(), "王海滨 美联储的下一步", by_title=True)["id"] == "p"
    _run_fixture(home)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    db.exec("UPDATE runs SET origin='backfill'")
    db.exec("UPDATE sources SET current_run_id='r', delivery='NOT_STARTED'")
    hand = {"id": "hand", "url": "https://notion/hand", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "汤姆 9月8日复盘"}]}, "内容发布日期": {"type": "date", "date": {"start": "2026-09-08"}}}}
    lib = _Lib(hand=[hand])
    monkeypatch.setattr(delivery, "client", lambda: lib)
    assert delivery.deliver("v")["status"] == "HELD" and not any(p == "/pages" for _, p in lib.calls)
    assert db.one("SELECT delivery, delivery_reason FROM sources") == {"delivery": "HELD", "delivery_reason": "MAYBE_HAVE"}
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    it = next(i for i in c.get("/api/overview", headers=h).json()["issues"] if i["id"] == "v")
    assert it["maybe"] == {"id": "hand", "url": "https://notion/hand", "title": "汤姆 9月8日复盘", "date": "2026-09-08"}
    assert c.post("/api/sources/v/maybe", headers=h, json={"write": True}).status_code == 200
    assert delivery.deliver("v")["status"] == "FAILED" and ("POST", "/pages") in lib.calls  # 照写: on to a page of its own
    db.exec("UPDATE sources SET delivery='NOT_STARTED'")
    db.set_setting("write_anyway:v", None)
    assert delivery.deliver("v")["status"] == "HELD"
    assert c.post("/api/sources/v/maybe", headers=h, json={"write": False}).status_code == 200
    assert db.one("SELECT delivery, notion_url FROM sources") == {"delivery": "REGISTERED", "notion_url": "https://notion/hand"}
    assert c.post("/api/sources/v/maybe", headers=h, json={"write": True}).status_code == 409  # nothing waits on a word now
    # an issue that did not come in by 补录 is written as before
    db.exec("UPDATE runs SET origin='ingest'")
    db.exec("UPDATE sources SET delivery='NOT_STARTED', notion_url=NULL")
    lib.calls.clear()
    assert delivery.deliver("v")["status"] == "FAILED" and ("POST", "/pages") in lib.calls


def test_the_backfill_list_marks_what_a_hand_written_page_may_be(home, monkeypatch):
    # S05 (a05): 可能已有 in the list, left unticked: the list's dates are YouTube's rough ones, the title's MM/DD is not
    from fastapi.testclient import TestClient
    from assetizer import delivery
    from assetizer.web import app as web
    entries = _board_fixture(home, monkeypatch)
    entries[2]["title"] = "10/22 盘后复盘"
    seen = []

    def maybe_many(src, es):
        seen.append([e["id"] for e in es])
        return {"CCCCCCCCCCC": {"id": "hand", "url": "https://notion/hand", "title": "汤姆 10/22", "date": "2026-10-22"}}
    monkeypatch.setattr(delivery, "registered_many", lambda src, ids: {})
    monkeypatch.setattr(delivery, "maybe_many", maybe_many)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    r = _listed(c, "/api/channels/t/backfill/candidates", h).json()
    marks = {x["id"]: x["maybe"] for x in r["candidates"]}
    assert marks["CCCCCCCCCCC"]["url"] == "https://notion/hand" and marks["DDDDDDDDDDD"] is None and seen


def test_pasting_an_issue_already_in_notion_finds_its_page_rather_than_printing_it_again(home):
    # S06 (a06): pasted again, a video Assetizer wrote to Notion answers with its page; printing it again is asked for
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    t = now()
    db.exec("""INSERT INTO sources(id, url, delivery, notion_url, current_run_id, created_at, updated_at) VALUES
               ('AAAAAAAAAAA','u','DELIVERED','https://notion/a','AAAAAAAAAAA-r1',?,?), ('BBBBBBBBBBB','u','REGISTERED','https://notion/hand',NULL,?,?)""",
            t, t, t, t)
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('AAAAAAAAAAA-r1','AAAAAAAAAAA','COMPLETE',?)", t)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    r = c.post("/api/submit", headers=h, json={"url": "https://youtu.be/AAAAAAAAAAA?si=x"}).json()
    assert (r["kept"], r["notion_url"]) == ("DELIVERED", "https://notion/a")
    r = c.post("/api/submit", headers=h, json={"url": "AAAAAAAAAAA BBBBBBBBBBB CCCCCCCCCCC"}).json()
    assert [i.get("kept") for i in r["items"]] == ["DELIVERED", "REGISTERED", None]
    assert {x["id"] for x in db.q("SELECT id FROM runs")} == {"AAAAAAAAAAA-r1", "CCCCCCCCCCC-r1"}
    r = c.post("/api/submit", headers=h, json={"url": "https://youtu.be/AAAAAAAAAAA", "reprint": True}).json()
    assert r["run_id"] == "AAAAAAAAAAA-r2" and not r["existing"] and "kept" not in r


def test_a_page_someone_wrote_on_is_not_replaced_without_asking(home, monkeypatch):
    # S24 (a07): before an old page goes (重印, 换成新版本), what someone else did on it — a paragraph added, one of ours
    # edited, a property, a comment — stops it: the page stays and the 刊 asks; nothing of theirs, and it goes as before
    from fastapi.testclient import TestClient
    from assetizer import db, delivery
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    _run_fixture(home)
    M.save("trader-tom", M.preset("trader-tom", "lib", M.schema_of(LIB), None))
    stale = "UPDATE sources SET current_run_id='r', notion_page_id='old', notion_synced_run='r0', notion_database_id='lib', delivery='STALE'"
    db.exec(stale)
    mine = {"id": "b1", "type": "paragraph", "created_by": {"id": "bot"}, "last_edited_by": {"id": "bot"}}
    lib = _Lib(blocks=[mine], comments=None)  # comments not readable: taken as none
    monkeypatch.setattr(delivery, "client", lambda: lib)
    assert delivery.deliver("v")["status"] == "FAILED" and ("PATCH", "/pages/old") in lib.calls  # on to the new page
    db.exec(stale)
    lib = _Lib(blocks=[{**mine, "last_edited_by": {"id": "me"}}, {"id": "b2", "type": "paragraph", "created_by": {"id": "me"},
                                                                    "last_edited_by": {"id": "me"}}],
               comments=[{"id": "c1", "created_by": {"id": "me"}}])
    monkeypatch.setattr(delivery, "client", lambda: lib)
    assert delivery.deliver("v")["status"] == "HELD"
    assert ("PATCH", "/pages/old") not in lib.calls and not any(p == "/pages" for _, p in lib.calls)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    it = next(i for i in c.get("/api/overview", headers=h).json()["issues"] if i["id"] == "v")
    assert it["delivery_reason"] == "YOURS_ON_PAGE" and it["yours"] == ["加了段落", "改了段落", "加了评论"]
    assert c.post("/api/sources/v/replace", headers=h, json={"replace": False}).status_code == 200
    assert db.one("SELECT delivery, notion_page_id FROM sources") == {"delivery": "DELIVERED", "notion_page_id": "old"}
    db.exec("UPDATE sources SET delivery='STALE'")  # asked for again later: it asks again
    assert delivery.deliver("v")["status"] == "HELD"
    assert c.post("/api/sources/v/replace", headers=h, json={"replace": True}).status_code == 200
    assert delivery.deliver("v")["status"] == "FAILED" and ("PATCH", "/pages/old") in lib.calls


def test_clearing_the_trial_queue_stops_the_trials_under_way_too(home, monkeypatch):
    # S19 (a10): 清空 means stop spending: what waits in line is dropped; what waits or runs stops — a running one's model
    # call is killed — keeping the stages done for 重试, and adopted by nothing
    import threading
    from fastapi.testclient import TestClient
    from assetizer import db, tuning
    from assetizer.pipeline import runner
    from assetizer.providers import agy
    from assetizer.providers.agy import ProviderError
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    for sid in ("AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"):
        db.exec("INSERT INTO sources(id, url, channel_id, delivery, created_at, updated_at) VALUES (?,?, 'w','NOT_STARTED',?,?)", sid, sid, t, t)
        db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w',?,?)", sid, t)
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, origin, created_at) VALUES ('AAAAAAAAAAA-r1','AAAAAAAAAAA',?,'RUNNING','trial',?)", v1, t)
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, wait_reason, origin, created_at) VALUES ('BBBBBBBBBBB-r1','BBBBBBBBBBB',?,'WAITING','NETWORK','trial',?)", v1, t)
    db.exec("INSERT INTO trial_queue(channel_id, source_id, playbook_id, created_at) VALUES ('w','CCCCCCCCCCC',?,?)", v1, t)
    for i, name in enumerate(("RESOLVE", "UNDERSTAND")):
        db.exec("INSERT INTO stages(run_id, name, ord, status) VALUES ('AAAAAAAAAAA-r1',?,?,?)", name, i, "DONE" if i == 0 else "PENDING")
    db.set_setting("trial_batch", 2)
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    r = c.post("/api/channels/w/trial-queue/clear", headers=h, json={}).json()
    assert (r["cancelled"], r["stopped"]) == (1, 2) and db.setting("trial_batch") is None  # no 「试跑全部跑完」 for this
    assert db.one("SELECT production, error_class FROM runs WHERE id='BBBBBBBBBBB-r1'") == {"production": "CANCELLED", "error_class": "STOPPED"}
    assert runner._execute("BBBBBBBBBBB-r1") == "CANCELLED"  # the worker took it up a moment too late: not started
    # the worker kills the running one's model call...
    killed = []
    monkeypatch.setattr(agy, "LIVE_RUNS", {4242: "AAAAAAAAAAA-r1", 4343: "x-r1"})
    monkeypatch.setattr(runner.os, "killpg", lambda pg, sig: killed.append(pg))
    w = runner.Worker()
    w.active["AAAAAAAAAAA-r1"] = threading.current_thread()
    w.halt()
    assert killed == [4242]

    def cut_off(ctx):  # ...so the call under way fails: 清空 came while it ran
        db.exec("UPDATE runs SET stop_at=? WHERE id='AAAAAAAAAAA-r1'", now())
        raise ProviderError("PROVIDER_FAILURE", "agy exited -9")
    monkeypatch.setattr(runner, "STAGES", [("RESOLVE", lambda ctx: {}, True), ("UNDERSTAND", cut_off, True)])
    db.exec("UPDATE runs SET stop_at=NULL WHERE id='AAAAAAAAAAA-r1'")
    assert runner._execute("AAAAAAAAAAA-r1") == "CANCELLED"
    assert db.one("SELECT production, error_class FROM runs WHERE id='AAAAAAAAAAA-r1'") == {"production": "CANCELLED", "error_class": "STOPPED"}
    assert db.one("SELECT status FROM stages WHERE run_id='AAAAAAAAAAA-r1' AND name='RESOLVE'")["status"] == "DONE"
    assert db.one("SELECT current_run_id FROM sources WHERE id='AAAAAAAAAAA'")["current_run_id"] is None
    assert c.post("/api/runs/AAAAAAAAAAA-r1/retry", headers=h, json={}).status_code == 200  # goes on from where it stopped
    assert db.one("SELECT production, stop_at FROM runs WHERE id='AAAAAAAAAAA-r1'") == {"production": "QUEUED", "stop_at": None}
    assert tuning.clear_queue("w") == 0


def test_relocking_offers_what_the_channel_posted_while_tuned_for_backfill(home, monkeypatch):
    # S25 (a11): back in 调试期 intake stopped; at 定稿 again the videos posted meanwhile are in the 定稿单 (ticked) and go
    # into the 补录 line — not lost, nor put ahead of new ones
    from fastapi.testclient import TestClient
    from assetizer import db, delivery, ingest, tuning
    from assetizer.util import now
    from assetizer.web import app as web
    v1, v2 = _tuning_fixture()
    t = now()
    db.exec("UPDATE channels SET youtube_channel_id='UCw'")
    db.exec("INSERT INTO sources(id, channel_id, delivery, current_run_id, created_at, updated_at) VALUES ('SSSSSSSSSSS','w','DELIVERED','SSSSSSSSSSS-r1',?,?)", t, t)
    db.exec("INSERT INTO samples(channel_id, source_id, added_at) VALUES ('w','SSSSSSSSSSS',?)", t)
    db.exec("INSERT INTO runs(id, source_id, playbook_id, production, created_at, finished_at) VALUES ('SSSSSSSSSSS-r1','SSSSSSSSSSS',?,'COMPLETE',?,?)", v2, t, t)
    tuning.confirm(v2)  # locked, intake on
    db.exec("UPDATE playbooks SET status='NEEDS_CONFIRMATION' WHERE id=?", v2)
    tuning.retune("w")
    paused = db.setting("ingest:w")["paused_at"]
    entries = [{"id": "NNNNNNNNNNN", "title": "调试期间发的", "published": paused[:10] + "T12:00:00+00:00", "duration": 3000, "approx": True},
               {"id": "SSSSSSSSSSS", "title": "样片", "published": paused[:10] + "T12:00:00+00:00", "duration": 3000, "approx": True},
               {"id": "OOOOOOOOOOO", "title": "调试之前的", "published": "2026-01-01T00:00:00+00:00", "duration": 3000, "approx": True}]
    monkeypatch.setattr(ingest, "look", lambda cid, limit=15, shorts=False: (entries, None, {"videos"}))
    monkeypatch.setattr(delivery, "registered_many", lambda src, ids: {})
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    lp = c.get(f"/api/channels/w/lock-preview?playbook_id={v2}", headers=h).json()
    assert [m["id"] for m in lp["missed"]] == ["NNNNNNNNNNN"]
    r = c.post("/api/channels/w/lock", headers=h, json={"playbook_id": v2, "samples": [], "backfill": [lp["missed"][0]]})
    assert r.status_code == 200 and r.json()["backfill"] == 1
    assert [q["source_id"] for q in db.q("SELECT source_id FROM backfill_queue WHERE status='PENDING'")] == ["NNNNNNNNNNN"]
    assert "paused_at" not in db.setting("ingest:w") and db.setting("ingest:w")["enabled"]


def test_saving_notion_settings_another_device_changed_meanwhile_asks_first(home, monkeypatch):
    # S30 (a12): the page read the settings at one revision and another device saved since: this save is refused (412),
    # not let undo the other one unseen; 用我的覆盖 sends it again with force. Renames Notion made are not a change
    from fastapi.testclient import TestClient
    from assetizer.notion import mapping as M
    from assetizer.web import app as web
    _preview_fixture(home)
    monkeypatch.setattr(web, "_notion", lambda: FakeNotion())
    c, h = TestClient(web.app), {"host": "127.0.0.1"}
    rev = c.get("/api/channels/trader-tom/notion", headers=h).json()["rev"]
    assert c.get("/api/channels/trader-tom/notion", headers=h).json()["rev"] == rev
    draft = {"properties": M.get("trader-tom")["properties"], "template": "reading-guide", "heading": "手机上改的", "icon": "none"}
    assert c.post("/api/channels/trader-tom/notion", headers=h, json={**draft, "rev": rev}).status_code == 200
    r = c.post("/api/channels/trader-tom/notion", headers=h, json={**draft, "heading": "电脑上改的", "rev": rev})
    assert r.status_code == 412 and "别处" in r.json()["detail"] and M.get("trader-tom")["heading"] == "手机上改的"
    assert c.post("/api/channels/trader-tom/notion", headers=h, json={**draft, "heading": "电脑上改的", "rev": rev, "force": True}).status_code == 200
    assert M.get("trader-tom")["heading"] == "电脑上改的"


def test_the_journal_and_a_board_ask_the_database_as_often_however_many_issues(home):
    # S64 (a19): 2000 issues took 13,923 queries for one look at the 刊, a round of several per issue: a few batched now
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import app as web
    t = now()
    db.exec("INSERT INTO channels(id, name, mode, stage, created_at, updated_at) VALUES ('t','Tom','TRACKED','LOCKED',?,?)", t, t)

    def add(k):
        for _ in range(k):
            sid = f"S{db.one('SELECT COUNT(*) n FROM sources')['n']:010d}"
            db.exec("INSERT INTO sources(id, channel_id, title, delivery, current_run_id, created_at, updated_at) VALUES (?,?,?,'DELIVERED',?,?,?)",
                    sid, "t", sid, sid + "-r1", t, t)
            db.exec("INSERT INTO runs(id, source_id, production, summary, created_at, finished_at) VALUES (?,?,'COMPLETE',?,?,?)",
                    sid + "-r1", sid, json.dumps({"units": 2, "assets": {"clip:GENERATED": 1}}), t, t)
            for u, pri in (("a", 3), ("b", 1)):
                db.exec("""INSERT INTO units(id, run_id, source_id, label, ord, title, start, end, learning_priority, data)
                           VALUES (?,?,?,?,0,?,10,20,?,'{}')""", sid + u, sid + "-r1", sid, u, f"单元{u}", pri)
            db.exec("INSERT INTO flags(source_id, run_id, target_kind, kind, severity, title, origin, created_at) VALUES (?,?,'RUN','x','REVIEW','校注','MACHINE_QA',?)",
                    sid, sid + "-r1", t)
            db.exec("INSERT INTO assets(run_id, source_id, unit_id, kind, status, path, created_at) VALUES (?,?,?,'screenshot','GENERATED','/x.jpg',?)",
                    sid + "-r1", sid, sid + "a", t)

    def asked(fn, *args):
        n = [0]
        db.connect().set_trace_callback(lambda sql: n.__setitem__(0, n[0] + 1))
        try:
            return n, json.loads(fn(*args).body)
        finally:
            db.connect().set_trace_callback(None)
    add(3)
    few, _ = asked(web.overview)
    board, _ = asked(web.channel_board, "t")
    add(12)
    same, out = asked(web.overview)
    assert same[0] == few[0] and len(out["issues"]) == 15
    assert asked(web.channel_board, "t")[0][0] == board[0]
    it = out["issues"][0]
    assert (it["no"], it["units"], it["must"], it["clips"], it["notes"], [m["title"] for m in it["musts"]]) == (15, 2, 1, 1, 1, ["单元a"])
    assert it["cover"] and it["latest_run"] == it["id"] + "-r1" and it["production"] == "COMPLETE"


# ---------------------------------------------------------------- logs to iterate on (agy, yt-dlp change under us)
def _ago(days: float) -> str:
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")


def test_logs_are_kept_thirty_days(home):
    # what iterating reads — yt-dlp's words, each model call's log, the events — goes after 30 days; a call's receipt and
    # answer stay (a retry reuses them), and so does an event of a run still under way (intake tells its videos by one)
    import os
    import time
    from assetizer import config, db, logs
    s = config.get()
    old, new = time.time() - 31 * 86400, time.time() - 86400
    call = s.runs_dir / "r" / "calls" / "understand-CH01" / "attempt-1"
    call.mkdir(parents=True)
    for name in ("stderr.log", "raw-output.jsonl", "prompt.md", "invocation.json", "response.txt"):
        (call / name).write_text("x")
        os.utime(call / name, (old, old))
    yt = s.logs_dir / "yt-dlp"
    yt.mkdir(parents=True)
    for name, at in (("a.log", old), ("b.log", new)):
        (yt / name).write_text("x")
        os.utime(yt / name, (at, at))
    db.exec("INSERT INTO sources(id, delivery, created_at, updated_at) VALUES ('w','NOT_STARTED',?,?)", _ago(40), _ago(40))
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('w-r1','w','WAITING',?)", _ago(40))
    for at, rid, msg in ((31, None, "old"), (31, "w-r1", "taken"), (1, None, "new")):
        db.exec("INSERT INTO events(ts, run_id, level, kind, message) VALUES (?,?,'info','x',?)", _ago(at), rid, msg)
    out = logs.prune()
    assert sorted(p.name for p in call.iterdir()) == ["invocation.json", "response.txt"]
    assert [p.name for p in yt.iterdir()] == ["b.log"]
    assert [e["message"] for e in db.q("SELECT message FROM events ORDER BY id")] == ["taken", "new"]
    assert out == {"files": 4, "events": 1}


def test_the_week_s_logs_say_when_a_scan_is_worth_it_and_go_out_without_secrets(home, monkeypatch, sent):
    # the Monday digest reads the week: failures, waits and model calls that went wrong, of the kinds iterating is about
    # (not a network hiccup, a live not over, a video gone); any kind not seen before, or enough of them, asks for a scan.
    # The export a developer's agent reads carries no token or cookie
    import datetime as dt
    from assetizer import config, db, logs, notify
    from assetizer.util import now
    t = now()
    db.exec("INSERT INTO sources(id, title, delivery, created_at, updated_at) VALUES ('v','v','NOT_STARTED',?,?)", t, t)
    for i, (prod, cls, days) in enumerate((("FAILED", "YOUTUBE_REFUSED", 2), ("FAILED", "UNKNOWN", 3), ("FAILED", "UNKNOWN", 20),
                                           ("FAILED", "VIDEO_PRIVATE", 1), ("COMPLETE_PARTIAL", None, 1))):
        db.exec("INSERT INTO runs(id, source_id, production, error_class, error, created_at, finished_at) VALUES (?,?,?,?,?,?,?)",
                f"v-r{i}", "v", prod, cls, f"{cls}: bot123456789:{'A' * 35} said no", _ago(days), _ago(days))
    for reason, days in (("NETWORK", 1), ("YTDLP_OUTDATED", 2)):
        db.exec("INSERT INTO events(ts, run_id, level, kind, message) VALUES (?,'v-r1','warn','run',?)", _ago(days), f"等待：{reason}")
    db.exec("""INSERT INTO model_calls(run_id, stage, attempt, model, agy_version, status, error_class, created_at)
               VALUES ('v-r1','understand-CH01','attempt-1','gemini','1.4.9','FAILED','MEDIA_READ_FAILED',?)""", _ago(1))
    stderr = config.get().runs_dir / "v-r1" / "calls" / "understand-CH01" / "attempt-1" / "stderr.log"
    stderr.parent.mkdir(parents=True)
    stderr.write_text("Authorization: Bearer ya29.abcdefghijklmnopqrstuvwxyz\nview_file refused: too large")
    w = logs.summary()
    assert w["failed"] == {"YOUTUBE_REFUSED": 1, "UNKNOWN": 1} and w["waited"] == {"YTDLP_OUTDATED": 1}
    assert w["calls"] == {"MEDIA_READ_FAILED": 1} and w["partial"] == 1 and w["agy"] == ["1.4.9"]
    assert w["new"] == ["MEDIA_READ_FAILED", "YOUTUBE_REFUSED", "YTDLP_OUTDATED"] and w["many"]  # UNKNOWN came 20 days ago too
    lines = logs.digest_lines()
    assert lines[0].startswith("🔧 迭代") and any("扫一遍" in x for x in lines)
    out = json.dumps(logs.export(), ensure_ascii=False)
    assert "view_file refused" in out and "ya29" not in out and "A" * 35 not in out
    db.exec("DELETE FROM runs WHERE error_class IN ('YOUTUBE_REFUSED','UNKNOWN')")
    db.exec("DELETE FROM events")
    db.exec("DELETE FROM model_calls")
    assert not logs.summary()["many"]

    class Monday(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 11, 2, 9, 30, tzinfo=tz)
    monkeypatch.setattr(notify.dt, "datetime", Monday)
    assert "🔧 迭代" in notify.digest()  # Mondays only


# ---------------------------------------------------------------- 机房的盒子设置 (knobs.py)
def test_knobs_are_checked_whole_then_written_and_read_clamped(cc_api, home):
    from assetizer import db, knobs
    assert knobs.get("worker_slots") == 1 and knobs.get("download_slots") == 2 and knobs.get("digest_hour") == 9
    r = cc_api.post("/api/knobs", json={"worker_slots": 3, "download_slots": 9})
    assert r.status_code == 400 and r.json()["field"] == "download_slots"
    assert db.setting("worker_slots") is None  # nothing written when one value is wrong
    r = cc_api.post("/api/knobs", json={"worker_slots": 3, "backfill_min_weekly": 0.5, "llm_provider": "agy",
                                        "stage_providers": {"knowledge": "magpie", "article": "agy"}})
    assert r.status_code == 200, r.text
    assert r.json()["worker_slots"] == 3 and r.json()["backfill_min_weekly"] == 0.5
    assert db.setting("stage_providers") == {"knowledge": "magpie"}  # a stage sent to the default needs no entry
    assert "同时印 1 → 3" in db.one("SELECT message FROM events WHERE kind='config' ORDER BY id DESC")["message"]
    assert cc_api.post("/api/knobs", json={"worker_slots": None}).json()["worker_slots"] == 1
    assert cc_api.post("/api/knobs", json={"nope": 1}).status_code == 400
    assert cc_api.post("/api/knobs", json={"digest_hour": 7.5}).status_code == 400
    db.set_setting("download_slots", 40)  # what `set` may have stored
    db.set_setting("playback_max_gb", "lots")
    assert knobs.get("download_slots") == 4 and knobs.get("playback_max_gb") == 5.0
    assert cc_api.get("/api/engine").json()["knobs"]["download_slots"] == 4


def test_download_gate_follows_its_limit_while_running(home):
    import threading
    import time
    from assetizer import db
    from assetizer.pipeline.acquire import _Gate
    from assetizer import knobs
    db.set_setting("download_slots", 1)
    gate = _Gate(lambda: knobs.get("download_slots"))
    entered = threading.Event()
    gate.__enter__()

    def second():
        with gate:
            entered.set()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.2)
    assert not entered.is_set()  # one slot, taken
    db.set_setting("download_slots", 2)
    with gate.cv:
        gate.cv.notify_all()  # in production the waiter looks again within 30 s
    assert entered.wait(2)
    gate.__exit__(None, None, None)
    t.join(2)
    assert gate.n == 0


@pytest.fixture
def gpu_box():
    """A stand-in for scripts/gpu_asr/server.py: /healthz, and /transcribe that checks the token before the body."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(200, {"ok": True, "model": "large-v3-turbo", "loaded": False})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            ok = self.headers.get("Authorization") == "Bearer right-token"
            self._reply(400 if ok else 401, {"error": "empty" if ok else "bad token"})

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_gpu_asr_is_set_write_only_and_tested_without_audio(cc_api, home, gpu_box, monkeypatch):
    from assetizer import db
    monkeypatch.delenv("ASSETIZER_ASR_TOKEN", raising=False)
    assert cc_api.post("/api/asr", json={"url": "not a url"}).status_code == 400
    r = cc_api.post("/api/asr", json={"url": gpu_box + "/", "token": "wrong-token"})
    assert r.status_code == 200 and "wrong-token" not in r.text
    assert r.json()["url"] == gpu_box and r.json()["token"] is True
    assert (home / "secrets" / "asr-token").stat().st_mode & 0o777 == 0o600
    r = cc_api.post("/api/asr/test", json={})
    assert r.status_code == 200, r.text
    t = r.json()["test"]
    assert not t["ok"] and t["code"] == "BAD_TOKEN"
    cc_api.post("/api/asr", json={"token": "right-token"})
    assert db.setting("asr_state") is None  # a new token: the last test no longer says anything
    monkeypatch.setattr(web_app(), "_throttle_at", {})
    t = cc_api.post("/api/asr/test", json={}).json()["test"]
    assert t["ok"] and t["model"] == "large-v3-turbo"
    r = cc_api.post("/api/asr", json={"clear": ["url", "token"]}).json()
    assert r["url"] is None and r["token"] is False and not (home / "secrets" / "asr-token").exists()


def web_app():
    from assetizer.web import app as web
    return web


def test_telegram_token_is_write_only_and_a_new_bot_forgets_the_chat(cc_api, home, monkeypatch):
    from assetizer import db
    for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv("ASSETIZER_" + n, raising=False)
    r = cc_api.post("/api/telegram", json={"token": "nonsense-value"})
    assert r.status_code == 400 and "nonsense-value" not in r.text
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets" / "telegram-chat-id").write_text("42")
    db.set_setting("telegram_chat", {"chat": "old"})
    token = "123456:" + "A" * 35
    r = cc_api.post("/api/telegram", json={"token": token})
    assert r.status_code == 200 and token not in r.text
    assert r.json() == {"token": True, "chat": False, "linked": None, "digest_hour": 9, "ok": None, "pending": 0}
    assert (home / "secrets" / "telegram-bot-token").read_text() == token
    assert cc_api.post("/api/telegram", json={"clear": ["token"]}).json()["token"] is False


def test_notion_default_page_moves_the_assetizer_database(cc_api, home, monkeypatch):
    from assetizer import db
    from assetizer.web import app as web

    class FakeNotion:
        def _req(self, method, path, **kw):
            assert (method, path) == ("GET", "/pages/01234567-89ab-cdef-0123-456789abcdef")
            return {"properties": {"Name": {"type": "title", "title": [{"plain_text": "收件箱"}]}}}

    monkeypatch.setattr(web, "_notion", lambda: FakeNotion())
    db.set_setting("notion_database_id", "old-db")
    r = cc_api.post("/api/notion/default", json={"parent": "https://www.notion.so/x-0123456789abcdef0123456789abcdef"})
    assert r.status_code == 200 and r.json()["title"] == "收件箱"
    assert db.setting("notion_parent_page_id") == "01234567-89ab-cdef-0123-456789abcdef"
    assert db.setting("notion_database_id") is None  # made again under the new page at the next delivery
    assert cc_api.post("/api/notion/default", json={"parent": "no link"}).status_code == 400


# --------------------------------------------------------------- 原话模式 (design §R7)
def _lines(*texts, step=10.0):
    return [{"start": i * step, "end": (i + 1) * step - 1, "text": t} for i, t in enumerate(texts)]


def test_partition_gives_every_line_to_exactly_one_unit():
    from assetizer.pipeline import fidelity
    segs = _lines("一一一", "二二二", "三三三", "四四四", "五五五")
    units = [{"label": "a", "spans": [{"from": 0, "to": 1}, {"from": 3, "to": 3}]},
             {"label": "b", "spans": [{"from": 1, "to": 2}]},       # line 1 taken by a: nothing is written twice
             {"label": "c", "spans": [{"from": 0, "to": 0}]}]       # left with no line of its own
    owned, info = fidelity.partition(units, segs)
    assert [(s["from"], s["to"]) for s in owned["a"]] == [(0, 1), (3, 4)]  # the missed line 4 joins the line before it
    assert [(s["from"], s["to"]) for s in owned["b"]] == [(2, 2)]
    assert info == {"missed_lines": 1, "dropped_units": ["c"]}
    assert owned["a"][1]["start"] == 30.0 and owned["a"][1]["end"] == 49.0
    assert fidelity.text_of(segs, owned["a"]).splitlines()[1].endswith("四四四五五五")


def test_partition_sends_back_a_plan_that_loses_speech():
    from assetizer.llm import ContractError
    from assetizer.pipeline import fidelity
    segs = _lines("一" * 50, "二" * 50)
    with pytest.raises(ContractError):
        fidelity.check_partition([{"label": "a", "spans": [{"from": 0, "to": 0}]}], segs)
    fidelity.check_partition([{"label": "a", "spans": [{"from": 0, "to": 1}]}], segs)


def test_section_problems_catch_compression_lost_numbers_and_process_words():
    from assetizer.pipeline import fidelity
    source = "（00:01:00）" + "他讲了很长一段话" * 10 + "人口2850万，原来4000多万，伤亡100万，债务300亿"
    assert fidelity.section_problems("TOPIC", source, "他讲了话。") != []
    full = "他讲了很长一段话" * 10 + "人口2,850万，原来4000多万，伤亡100万，债务300亿"
    assert fidelity.section_problems("TOPIC", source, full) == []  # thousands separators do not count as lost
    assert "2850" in fidelity.missing_numbers(source, "4000 100 300")
    assert any("主题记录" in p for p in fidelity.section_problems("TOPIC", source, full + "主题记录未说明"))
    assert any("记录中" in p for p in fidelity.section_problems("TOPIC", source, full + "记录中没有"))
    assert fidelity.section_problems("TOPIC", source, full + "记录中西早餐的差别") == []
    assert fidelity.section_problems("BACKGROUND", source, "简短要点") == []  # a roll-up is meant to be short


def test_cut_silences_keeps_speech_and_a_margin():
    from assetizer.pipeline import fidelity
    # the 2026-11-04 clip: he reads his phone for 83 s, with one stray sound in the middle
    spans = [[1987.0, 2204.0]]
    quiet = [(2005.4, 2031.5), (2031.7, 2075.2), (2075.2, 2088.5), (2117.0, 2122.5), (2190.0, 2193.0)]
    assert [pytest.approx(x) for x in fidelity.cut_silences(spans, quiet)] == [[1987.0, 2005.8], [2088.1, 2117.4], [2122.1, 2204.0]]
    assert fidelity.cut_silences([[10.0, 20.0]], [(0.0, 12.0)]) == [[10.0, 20.0]]  # only 2 s of it falls inside
    assert fidelity.cut_silences([[10.0, 30.0]], [(0.0, 18.0)]) == [[17.6, 30.0]]
    assert fidelity.cut_silences([[10.0, 12.0]], [(0.0, 50.0)]) == [[10.0, 12.0]]  # silence throughout: keep it
    assert fidelity.cut_silences([[50.0, 60.0], [10.0, 20.0]], []) == [[10.0, 20.0], [50.0, 60.0]]


def test_clip_time_maps_across_the_cuts():
    from assetizer.pipeline import fidelity
    keep = [[100.0, 110.0], [200.0, 205.0]]
    assert [fidelity.to_local(t, keep) for t in (100.0, 105.0, 110.0, 150.0, 200.0, 204.0, 300.0)] == [0, 5, 10, 10, 10, 14, 15]
    assert fidelity.inside(101, 102, keep) and fidelity.inside(201, 204, keep)
    assert not fidelity.inside(108, 201, keep) and not fidelity.inside(150, 151, keep)


def test_boundaries_follow_the_units_text_and_skip_the_roll_ups(home):
    from assetizer.pipeline import media_stages
    from assetizer.pipeline.context import RunContext
    ctx = RunContext(run={"id": "fid-r1"}, source={"id": "fid", "duration": 1000.0})
    units = [{"id": "u1", "label": "topic", "title": "T", "role": "TOPIC",
              "segments": [{"start": 300.0, "end": 310.0}, {"start": 100.0, "end": 120.0}]},
             {"id": "u2", "label": "qa", "title": "Q", "role": "QA_DIGEST", "segments": [{"start": 400.0, "end": 410.0}]},
             {"id": "u3", "label": "bg", "title": "B", "role": "BACKGROUND", "segments": [{"start": 0.0, "end": 10.0}]},
             {"id": "u4", "label": "other", "title": "O", "role": "TOPIC", "segments": [{"start": 500.0, "end": 510.0}]}]
    media_stages._boundaries_from_spans(ctx, units, [{"unit_id": "u4", "verdict": "REJECT", "publication_priority": "P2"}])
    b = ctx.load("machine/boundaries.json")
    assert set(b) == {"u1", "u4"}  # every section with a topic has its video; the roll-ups have none
    assert b["u1"]["spans"] == [[99.85, 120.3], [299.85, 310.3]]
    assert (b["u4"]["selection"], b["u4"]["publication_priority"]) == ("CONTEXT_DEPENDENT", "P2")


def test_a_clip_can_be_rendered_from_pieces(tmp_path):
    import shutil
    import subprocess
    from assetizer.media import ffmpeg
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    out = ffmpeg.render_pieces(src, [[1.0, 3.0], [6.0, 9.0]], tmp_path / "c.mp4")
    assert abs(ffmpeg.duration(out) - 5.0) < 0.2
    streams = {s["codec_type"] for s in ffmpeg.probe(out)["streams"]}
    assert streams == {"video", "audio"}
    assert abs(ffmpeg.duration(ffmpeg.render_pieces(src, [[1.0, 3.0]], tmp_path / "d.mp4")) - 2.0) < 0.2


def test_a_time_scope_outside_the_list_only_loses_the_hint():
    from assetizer.pipeline.semantic import clean_time_scope
    o = {"topics": [{"claims": [{"time_scope": "PLAN"}, {"time_scope": "NOW"}, {"time_scope": None}]}]}
    assert [c["time_scope"] for c in clean_time_scope(o)["topics"][0]["claims"]] == [None, "NOW", None]
    assert clean_time_scope("not an object") == "not an object"


def test_only_a_channel_that_opted_in_compiles_to_transcript_mode(home, monkeypatch):
    from assetizer import compiler, db
    from assetizer.llm import LLM
    db.exec("INSERT INTO channels(id, name, stage, created_at, updated_at) VALUES ('fid-ch','F','TUNING','2026-01-01','2026-01-01')")
    answer = {"clause_map": [], "rules": [], "facets": [], "article_style": "x", "policy_conflicts": []}
    monkeypatch.setattr(LLM, "structured", lambda self, *a, **k: answer if a[1] == "compile" else {"grades": []})
    first = db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, origin, created_at) VALUES ('fid-ch',1,'保留全部信息。','a','manual','2026-01-01')")
    plain = compiler.compile_prompt("fid-ch", first)
    assert "fidelity" not in db.unj(db.one("SELECT data FROM playbooks WHERE id=?", plain)["data"], {})
    db.set_setting("fidelity:fid-ch", "transcript")
    second = db.exec("INSERT INTO channel_prompts(channel_id, version, text, sha256, origin, created_at) VALUES ('fid-ch',2,'保留全部信息，一字不漏。','b','manual','2026-01-01')")
    opted = compiler.compile_prompt("fid-ch", second)
    assert db.unj(db.one("SELECT data FROM playbooks WHERE id=?", opted)["data"], {})["fidelity"] == "transcript"
    assert "fidelity" not in db.unj(db.one("SELECT data FROM playbooks WHERE id=?", plain)["data"], {})  # versions already compiled keep running as they did


def test_transcript_mode_end_to_end(home, tmp_path, monkeypatch):
    """Knowledge → article → clips → subtitles → review → compose, with the model answers canned and real ffmpeg."""
    import json as _json
    import shutil
    import subprocess
    from assetizer import db
    from assetizer.contracts import prompts as P
    from assetizer.llm import LLM
    from assetizer.pipeline import finish, media_stages, semantic
    from assetizer.pipeline.context import RunContext
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    media = tmp_path / "source.mp4"
    # 40 s of tone with 12 s of silence in the middle of the first topic
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=40",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
                    "-af", "volume=enable='between(t,5,17)':volume=0", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(media)], check=True)
    db.connect()
    db.exec("INSERT INTO sources(id, url, title, duration, media_path, media_sha256, created_at, updated_at) VALUES ('fid','u','T',40,?,'x','2026','2026')", str(media))
    db.exec("INSERT INTO runs(id, source_id, kind, production, created_at) VALUES ('fid-r1','fid','PRODUCTION','RUNNING','2026')")
    run = db.one("SELECT * FROM runs WHERE id='fid-r1'")
    ctx = RunContext(run=run, source=db.one("SELECT * FROM sources WHERE id='fid'"))
    ctx.playbook = {"fidelity": "transcript", "identity": {"channel": "F", "speaker": "S", "speaker_key": "host"}, "rules": [], "facets": []}
    texts = ["今年美国选举结果出来了他赢个毛啊", "人口只有2850万了真是惨", "观众问抖音危险吗", "我觉得没问题", "今天去家具店耗了很久",
             "我们接着讲选举对市场的影响", "这个国运的事情我就不多讲了", "好了下播"]
    spans = [(0, 4), (18, 22), (22, 24), (24, 26), (26, 28), (28, 33), (33, 37), (37, 39)]
    segs = [{"start": float(a), "end": float(b), "text": t} for (a, b), t in zip(spans, texts)]
    words = []
    for s in segs:
        n = len(s["text"])
        words += [{"word": c, "start": s["start"] + i * (s["end"] - s["start"]) / n, "end": s["start"] + (i + 1) * (s["end"] - s["start"]) / n}
                  for i, c in enumerate(s["text"])]
    ctx.save("machine/transcript.json", {"segments": segs, "words": words, "media_sha256": "x"})
    topic = {"id": "CH01:t1", "title": "选举", "start": "00:00", "end": "00:40", "start_s": 0.0, "end_s": 40.0, "summary": "s",
             "tickers": [], "claims": [], "teacher_reasoning": "", "visual_evidence": [], "uncertainties": [], "chunk": "CH01"}
    ctx.save("machine/chunks.json", [{"part_id": "CH01", "speakers": [], "topics": [topic], "uncertainties": []}])
    ctx.save("machine/merged.json", {"topics": [{"id": "M01", "title": "选举", "chunk_topic_refs": ["CH01:t1"], "start_s": 0.0, "end_s": 40.0}],
                                     "speakers": [], "conflicts": []})
    base = {"topic_refs": ["M01"], "focus": "", "knowledge_type": "ARGUMENT", "learning_priority": 2, "tickers": [], "depends_on": [], "why": "w"}
    skeleton = {"uncertainties": [], "units": [
        {**base, "label": "unit_vote", "title": "选举", "role": "TOPIC", "clip_candidate": True, "spans": [{"from": 0, "to": 1}, {"from": 5, "to": 6}]},
        {**base, "label": "unit_qa", "title": "问答", "role": "QA_DIGEST", "clip_candidate": False, "spans": [{"from": 2, "to": 3}]},
        {**base, "label": "unit_bg", "title": "杂谈", "role": "BACKGROUND", "clip_candidate": False, "spans": [{"from": 4, "to": 4}, {"from": 7, "to": 7}]}]}
    seen = {}

    def answer(self, root, stage, prompt, schema, **kw):
        seen[stage] = prompt
        if stage == "knowledge-skeleton":
            kw["check"](skeleton)
            return skeleton
        if stage.startswith("knowledge-detail"):
            labels = [u["label"] for u in skeleton["units"] if u["label"] in prompt]
            return {"uncertainties": [], "units": [{"label": x, "summary": "s", "speaker_key": "host", "teacher_reasoning": "", "visual_evidence": [],
                                                    "screenshot_anchors": [], "required_context": "", "claims": [], "facets": {}, "uncertainties": []} for x in labels]}
        if stage == "article-sections-01":
            return {"sections": [{"label": "unit_vote", "markdown": "## 选举\n\n主题记录太短"},
                                 {"label": "unit_qa", "markdown": "## 问答\n\n- **问：抖音危险吗**　答：没问题"},
                                 {"label": "unit_bg", "markdown": "## 杂谈\n\n- 去了家具店"}]}
        if stage == "article-rewrite-unit_vote":
            return {"sections": [{"label": "unit_vote", "markdown": "## 选举\n\n今年美国选举结果出来了，「他赢个毛啊」。人口只有 2850 万，真是惨。接着讲选举对市场的影响。国运的事情他说不多讲了。"}]}
        if stage == "article-outline":
            assert "他赢个毛啊" in prompt  # the opening and the lists are written from the finished text
            return {"title": "T", "headline": "H", "intro_markdown": "导语", "outro_markdown": "要点"}
        if stage.startswith("review-check"):
            return {"checks": []}
        raise AssertionError(stage)

    monkeypatch.setattr(LLM, "structured", answer)
    semantic.knowledge(ctx)
    units = {u["label"]: u for u in semantic._units(ctx)}
    assert units["unit_vote"]["role"] == "TOPIC" and units["unit_bg"]["role"] == "BACKGROUND"
    assert "他赢个毛啊" in seen["knowledge-detail-01"] and "[5 00:00:28]" in seen["knowledge-skeleton"]
    semantic.article(ctx)
    text = (ctx.dir / "article" / "article.md").read_text()
    assert "主题记录" not in text and "他赢个毛啊" in text  # rewritten once for the process word and the compression
    assert list(seen).index("article-outline") > list(seen).index("article-sections-01")
    ctx.save("machine/selection.json", {"selections": []})  # selection failed: the videos must not depend on it
    media_stages.boundary(ctx)
    assert media_stages.clips(ctx)["clips"] == 1
    clip = db.one("SELECT * FROM assets WHERE run_id='fid-r1' AND kind='clip'")
    keep = db.unj(clip["meta"], {})["keep"]
    assert clip["unit_id"] == units["unit_vote"]["id"]
    # first topic line, then 18–22 after the 12 s of silence, then 28–37: nothing of the question or the furniture
    assert len(keep) == 3 and keep[0][1] < 6 and keep[1][0] > 16 and keep[2][0] >= 27
    from assetizer.media import ffmpeg
    assert abs(ffmpeg.duration(Path(clip["path"])) - sum(b - a for a, b in keep)) < 0.3
    media_stages.subtitle_stage(ctx)
    srt = (Path(clip["path"]).parent / "subtitle.srt").read_text()
    assert "家具" not in srt and "下播" not in srt and "选举" in srt
    finish.review_check(ctx)
    checked = "".join(v for k, v in seen.items() if k.startswith("review-check"))
    assert "家具" in checked and "没问题" in checked  # the sections without a video are checked too
    comp = finish.compose(ctx)
    assert comp["videos"] == 1
