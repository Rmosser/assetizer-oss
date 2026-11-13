"""专题 (design §R8): a question and a batch of videos, each read for it, then put together (topics/)."""
import json
import subprocess
import threading

import pytest


@pytest.fixture
def home(tmp_path):
    import dataclasses
    from assetizer import config, db
    config.override(dataclasses.replace(config.load(), home=tmp_path))
    db._local.conn = None
    yield tmp_path
    db._local.conn = None


# ---------------------------------------------------------------- the draft's pieces (no model)
RECORD = """Let me think about the segment first.
[00:00-00:20] 类型：软件界面
画面：切片软件的支撑设置页
文字：Support density 80%
[00:20-00:40] 类型：讲者
画面：讲者对镜头
声音：有人说话
"""


def test_a_record_keeps_its_last_full_run_of_entries():
    from assetizer.topics.draft import final_record, problem, thinking
    once = "[00:00-00:40] 类型：讲者\n画面：讲者\n[00:40-01:30] 类型：图表\n画面：K 线\n"
    again = "Let me think.\n" + once + "Check: all fields done.\n" + once + "- Self-check: fine\n"
    rec = final_record(again)
    assert rec == once.strip()  # the copy written out again, without the thinking around it
    assert problem(rec, 90) is None
    assert "没记完" in problem("[00:00-00:10] 类型：讲者", 120)
    assert "超出" in problem("[00:00-03:00] 类型：讲者", 60)
    assert thinking("Wait, the prompt says each entry needs fields.\n[00:00-00:05] 类型：讲者") == 1
    assert thinking("Support density 80%\nLet's Print — a channel") == 0  # English on a screen is not thinking


def test_segments_overlap_and_their_entries_meet_in_the_middle():
    from assetizer.topics.draft import entries, rows, segments
    plan = segments(1500)
    assert [p[0] for p in plan] == ["CH01", "CH02", "CH03"] and plan[1][1] == 580 and segments(650) == [("CH01", 0.0, 650)]
    a = {"pid": "CH01", "start": 0, "length": 600, "record": "[09:30-09:50] 类型：讲者\n[09:52-10:00] 类型：讲者"}
    b = {"pid": "CH02", "start": 580, "length": 600, "record": "[00:00-00:15] 类型：讲者\n[00:16-00:30] 类型：图表"}
    got = rows([b, a])
    assert [r["start"] for r in got] == [570, 596]  # CH01 up to 590, CH02 from 590 (580 + 16)
    assert entries("[01:00-01:10] 类型：图表\n画面：K 线", 60)[0] == {"start": 120, "end": 130, "head": "类型：图表", "body": ["画面：K 线"]}
    gap = rows([{"pid": "CH01", "start": 0, "length": 600, "error": "没记完"}])
    assert gap[0]["head"] == "[这段没有画面记录]"


def test_the_text_pass_takes_frames_where_text_is_worth_reading():
    from assetizer.topics.draft import frame_times, wants
    screen = {"start": 0, "end": 10, "head": "类型：软件界面", "body": []}
    face = {"start": 10, "end": 20, "head": "类型：讲者", "body": ["文字：画面底部有对白字幕"]}
    plate = {"start": 20, "end": 80, "head": "类型：实物", "body": ["文字：Elegoo Saturn 4"]}
    assert wants(screen) and not wants(face) and wants(plate)
    assert frame_times([screen, face, plate], 100) == [5.0, 23.0, 38.0, 53.0, 68.0]


def test_the_draft_interleaves_speech_and_drops_a_hallucinated_transcript():
    from assetizer.topics.draft import entries, render
    rows_ = entries(RECORD.split("\n", 1)[1], 0)
    t = {"language": "zh", "device": "cuda", "segments": [{"start": 2, "end": 4, "text": "密度设到八十"}, {"start": 25, "end": 28, "text": "好"}]}
    md = render({"id": "abcdefghijk", "title": "加支撑", "channel": "Lab"}, 40, rows_, t, [{"t": 10.0, "text": "Support density 80%"}])
    assert "说：密度设到八十" in md and "高清帧 00:10.0：Support density 80%" in md and md.startswith("# 加支撑")
    ghost = {"segments": [{"start": i, "end": i + 1, "text": "谢谢观看"} for i in range(6)]}
    assert "疑似幻听" in render({"id": "x"}, 40, rows_, ghost, [])
    brief = render({"id": "x"}, 40, rows_, {}, [{"t": 10.0, "text": "字" * 300}], brief=160)
    assert "…（共 300 字）" in brief


def test_look_lists_are_cleaned_and_numbered():
    from assetizer.topics.look import at, clean
    got = clean([{"id": "L01", "start": "00:05", "question": "读数"}, {"id": "L01", "start": "01:02.5", "question": "再看"},
                 {"id": "../x", "times": ["00:01", "1:00:02"], "mode": "frames", "question": "字幕"},
                 {"id": "L09", "question": "没有时间"}, {"id": "L10", "start": "00:01", "question": " "}], "L")
    assert [x["id"] for x in got] == ["L01", "L02", "L03"] and got[2]["start"] == "00:01" and got[1]["mode"] == "frame"
    assert at("1:00:02") == 3602 and at("01:02.5") == 62.5 and at(7) == 7.0
    # the second round's are R: an L01 written there would open the first round's look
    assert [x["id"] for x in clean([{"id": "L01", "start": "00:05", "question": "再看"}, {"id": "R01", "start": "00:09", "question": "又看"}], "R")] == ["R01", "R02"]


def test_notes_end_with_their_json_block():
    from assetizer.topics.write import split
    md, tail = split('## 支撑\n密度 80% [00:05.0 帧]\n\n```json\n{"fit": "high", "gist": "八十", "looks": []}\n```\n')
    assert md == "## 支撑\n密度 80% [00:05.0 帧]" and tail == {"fit": "high", "gist": "八十", "looks": []}
    assert split("只有笔记") == ("只有笔记", {})


# ---------------------------------------------------------------- where the steps go
def test_topic_steps_route_apart_from_production(home):
    from assetizer import db, knobs, providers
    db.set_setting("magpie_state", {"models": [{"id": "claude/claude-opus-5-5", "inputs": ["text", "image"]},
                                               {"id": "codex/gpt-6.1-terra", "inputs": ["text", "image"]}]})
    db.set_setting("stage_providers", {"knowledge": "magpie"})
    r = knobs.topic_routes()
    assert r["draft"] == "agy" and r["look"] == "agy" and r["second"] == r["topic"] == "claude/claude-opus-5-5"
    assert "claude/claude-opus-5-5" not in providers.routes()  # its quota never pauses the channels
    with pytest.raises(knobs.KnobError, match="回看要看视频"):
        knobs.save({"topic_route": {"look": "claude/claude-opus-5-5"}})
    knobs.save({"topic_route": {"second": "codex/gpt-6.1-terra"}})
    assert knobs.topic_routes()["second"] == "codex/gpt-6.1-terra"
    assert type(providers.for_stage("second-a01")).__name__ == "MagpieProvider"
    knobs.save({"topic_route": {"second": None}})
    assert knobs.get("topic_providers") == {} and knobs.topic_routes()["second"] == "claude/claude-opus-5-5"
    db.set_setting("magpie_state", {"models": []})
    assert knobs.topic_routes()["topic"] == "magpie"  # no Claude on this box: where 知识 goes


def test_a_channels_draft_routes_on_its_own_to_carbon(home):
    """A channel's 底稿 while it is tuned (design §R-21): Gemini 4 Carbon when agy lists it, else where 理解 goes; never
    production's stages nor the topics', and its quota never pauses the channels."""
    from assetizer import db, knobs, providers
    from assetizer.providers.agy import AgyProvider
    base = providers.resolve("agy")
    assert knobs.channel_draft_route() == "agy"  # agy not asked yet: where 理解 goes
    db.set_setting("agy_models", [{"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash", "levels": ["low", "medium", "high"]},
                                  {"id": "gemini-4-carbon-high", "name": "Gemini 4 Carbon", "levels": ["low", "medium", "high"]}])
    assert knobs.channel_draft_route() == "agy/gemini-4-carbon-high"
    p = providers.for_stage("channel_draft-visual-P001")
    assert isinstance(p, AgyProvider) and p.model == "gemini-4-carbon-high"
    assert providers.for_stage("understand-CH01").model == providers.for_stage("draft-visual-P001").model == base[len("agy/"):]
    assert "agy/gemini-4-carbon-high" not in providers.routes()
    db.set_setting("magpie_state", {"models": [{"id": "claude/claude-opus-5-5", "inputs": ["text", "image"]}]})
    with pytest.raises(knobs.KnobError, match="频道底稿要看视频"):
        knobs.save({"channel_draft": "claude/claude-opus-5-5"})
    knobs.save({"channel_draft": "agy/gemini-3.8-flash-high"})
    assert knobs.channel_draft_route() == "agy/gemini-3.8-flash-high"
    assert db.one("SELECT message FROM events ORDER BY id DESC LIMIT 1")["message"] == \
        "设置：频道底稿 agy/gemini-4-carbon-high → agy/gemini-3.8-flash-high"
    knobs.save({"channel_draft": "agy/gemini-4-carbon-high"})  # its default needs no entry
    assert knobs.get("channel_draft") is None and db.setting("channel_draft") is None
    knobs.save({"effort": {"channel_draft": "high"}})
    assert knobs.effort("channel_draft-text-B001") == "high" and knobs.effort("draft-text-B001") is None


def test_a_channels_draft_is_made_by_its_own_calls_and_its_pictures_may_think_long(stand_in, home):
    from assetizer.topics import draft, media_dir
    from assetizer.llm import LLM
    d = draft.Draft("abcdefghijk", {"id": "abcdefghijk", "title": "加支撑", "channel": "Lab"}, media_dir("abcdefghijk") / "source.mkv", LLM(), lambda: None,
                    lambda done, of: None, step="channel_draft").make()
    assert not d["gaps"] and "model" not in d  # which model answered is in each call's receipt, not one word for all
    assert {k.rsplit("-", 1)[0] for k in stand_in} == {"channel_draft-visual", "channel_draft-text"}
    # pictures may think long, asked of whichever model each call lands on (agy.quiet_for keeps Flash at 5 minutes)
    assert all(QUIET[k] == draft.QUIET == 1800 for k in stand_in if "-visual-" in k)
    assert all(QUIET[k] is None for k in stand_in if "-text-" in k)  # reading frames is quick: the usual 5 minutes


def test_a_drafts_quota_goes_by_its_model():
    from assetizer.topics.draft import rate
    assert rate("agy/gemini-3.8-flash-high") == 0.0034
    assert rate("agy/gemini-4-hydrogen-high") == 0.007
    assert rate("agy/gemini-4-carbon-high") == rate("agy/gemini-4-argon-high") == rate("agy/gemini-5-pro-high") == 0.026


def test_drafting_a_channel_prompt_is_not_a_topic_draft(home, monkeypatch):
    """The 学习目标 draft goes where the compiler's calls go, whatever the topic's 底稿 is set to."""
    from assetizer import compiler, knobs, providers
    knobs.save({"topic_route": {"draft": "codex/gpt-6.1-terra"}})
    asked = []

    class Stand:
        def structured(self, root, stage, *a, **k):
            asked.append(stage)
            return {"raw_prompt": "我想学……"}
    monkeypatch.setattr(compiler, "LLM", Stand)
    assert compiler.draft_prompt("Lab", "学切片", []) == "我想学……"
    assert type(providers.for_stage(asked[0])) is type(providers.get())
    assert type(providers.for_stage("draft-visual-CH01")).__name__ == "MagpieProvider"


def test_magpie_sends_images_as_they_are(home):
    from assetizer.providers.magpie import MagpieProvider
    a, b = home / "a.jpg", home / "b.png"
    a.write_bytes(b"\xff\xd8jpg")
    b.write_bytes(b"\x89PNG")
    parts, info = MagpieProvider()._media_parts([str(a), str(b)])
    assert [p["text"] for p in parts if p["type"] == "input_text"] == ["图 1", "图 2"]
    assert parts[3]["image_url"].startswith("data:image/png;base64,") and info["images"] == 2


# ---------------------------------------------------------------- a read, start to end, with a stand-in model
WRITE = ("写作提示词：按问题写这期的做法和参数，每个数字标来源。" * 12)


def answer(kind: str) -> str:
    if kind.startswith(("draft-visual", "channel_draft-visual")):
        return RECORD
    if kind.startswith(("draft-text", "channel_draft-text")):
        return "## 图1\nSupport density 80%\n## 图2\nLift 1.2 mm"
    if kind == "topic-plan":
        return json.dumps({"fit": "high", "reason": "讲支撑参数", "write": WRITE, "looks": [
            {"id": "L01", "start": "00:05", "end": "00:08", "mode": "frame", "question": "支撑密度是多少"},
            {"id": "L02", "start": "00:10", "end": "00:12", "mode": "frames", "step": 0.5, "second": True, "question": "哪只手"},
            {"id": "L03", "start": "00:10", "mode": "zoom", "target": "右上角的数字", "question": "读数"},
            {"id": "L04", "start": "00:20", "end": "00:25", "mode": "clip", "question": "转了几圈"}]}, ensure_ascii=False)
    if kind.endswith("-locate"):
        return '{"box": [100, 100, 400, 400]}'
    if kind.startswith("look-"):
        return '{"answer": "80%", "evidence": "第 1 张", "sure": true}'
    if kind.startswith("second-"):
        return '[{"id": "L02", "answer": "右手", "evidence": "图 2", "sure": true}]'
    if kind == "topic-write":
        return ("## 支撑\n\n密度 80% [00:10.0 帧] [L01]，用哪只手未核实。" + "细节。" * 80 +
                '\n\n```json\n{"fit": "high", "gist": "支撑密度 80%", "looks": [{"id": "R01", "start": "00:30", "end": "00:32", '
                '"mode": "frame", "question": "再看一眼"}]}\n```')
    if kind == "topic-revise":
        return "## 支撑\n\n密度 80% [L01]，右手 [R01]。" + "细节。" * 80 + '\n\n```json\n{"fit": "high", "gist": "支撑密度 80%，右手"}\n```'
    if kind == "topic-synth":
        return json.dumps({"answer": "密度设 80% [V1 L01]。", "certainty": {"level": "中", "why": "只有一期"},
                           "claims": [{"id": "C1", "type": "事实", "text": "密度 80% [V1 L01]", "v": [1]}],
                           "first": [{"v": 1, "why": "参数全"}], "sections": [{"title": "参数", "claims": ["C1"]}],
                           "videos": [{"v": 1, "adds": "密度"}]}, ensure_ascii=False)
    raise AssertionError(kind)


ASKED: dict[str, str] = {}  # the last prompt of each kind the stand-in was given
QUIET: dict[str, float | None] = {}  # how long each kind of call was let go without a word (agy.QUIET)


@pytest.fixture
def stand_in(home, monkeypatch):
    """Every model answered by `answer`; whisper by a line; a 40 s test video already downloaded."""
    from assetizer import db, providers
    from assetizer.media import asr
    from assetizer.providers.agy import CallResult
    from assetizer.topics import media_dir
    calls: list[str] = []
    ASKED.clear()
    QUIET.clear()

    class Stand:
        name, media_size_limit, model = "agy", None, "stand-in"

        def call(self, call_dir, prompt, *, kind, schema=None, media=None, effort=None, timeout=None, quiet=None):
            calls.append(kind)
            ASKED[kind] = prompt
            QUIET[kind] = quiet
            call_dir.mkdir(parents=True, exist_ok=True)
            return CallResult(answer(kind), {"status": "SUCCESS", "actual_model": "stand-in", "usage": {"input_tokens": 1}})

        def health(self):
            return {"ok": True}
    monkeypatch.setattr(providers, "_make", lambda route: Stand())
    monkeypatch.setattr(asr, "transcribe", lambda wav, language=None, **_: {
        "segments": [{"start": 1.0, "end": 4.0, "text": "支撐密度設到八十"}], "words": [], "language": "zh", "device": "cpu"})
    src = media_dir("abcdefghijk") / "source.mkv"
    src.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=40",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=40", "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    from assetizer.util import now
    db.exec("INSERT INTO topics(question, notes, created_at, updated_at) VALUES ('光固化怎么加支撑', '要参数', ?, ?)", now(), now())
    db.exec("""INSERT INTO videos(id, url, title, channel, duration, media_path, media_state, created_at)
               VALUES ('abcdefghijk', 'https://www.youtube.com/watch?v=abcdefghijk', '加支撑', 'Lab', 40, ?, 'CACHED', ?)""", str(src), now())
    db.exec("INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (1, 'abcdefghijk', 1, 'QUEUED', 1, ?)", now())
    return calls


class Worker:
    def __init__(self):
        self.active: dict[str, threading.Thread] = {}

    def settle(self):
        for t in list(self.active.values()):
            t.join(timeout=120)
        self.active.clear()


def test_a_video_is_read_for_the_question_and_put_together(stand_in, home, monkeypatch):
    from assetizer import db, knobs, notify
    from assetizer.topics import draft_dir, run, work_dir
    told: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: told.append(text) or True)
    db.set_setting("worker_slots", 4)
    w = Worker()
    assert run.dispatch(w) and list(w.active) == ["topic:1:abcdefghijk"]
    w.settle()
    item = db.one("SELECT * FROM topic_videos")
    assert item["state"] == "DONE", item["error"]
    assert item["fit"] == "high" and item["gist"] == "支撑密度 80%，右手" and item["done_rev"] == 1
    d = work_dir(1, 1, "abcdefghijk")
    looks = json.loads((d / "looks.json").read_text())
    assert [x["id"] for x in looks] == ["L01", "L02", "L03", "L04"]
    assert looks[1]["answer2"] == "右手" and "answer2" not in looks[0]  # only `second` items go to the second model
    assert looks[2]["media"][0] == "media/L03/crop.png" and looks[3]["media"][0].endswith(".mp4")
    assert "[R01]" in (d / "notes.md").read_text() and json.loads((d / "looks2.json").read_text())[0]["id"] == "R01"
    full = (draft_dir("abcdefghijk") / "full.md").read_text()
    assert "说：支撑密度设到八十" in full and "高清帧" in full  # simplified, interleaved
    assert not (draft_dir("abcdefghijk") / "proxies").exists() and not (draft_dir("abcdefghijk") / "frames").exists()
    assert {r["run_id"] for r in db.q("SELECT run_id FROM model_calls")} == {"topic-1"}
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "QUEUED"  # every video read: the answer is due
    assert run.dispatch(w) and list(w.active) == ["topic-synth:1"]
    w.settle()
    t = db.one("SELECT * FROM topics")
    assert t["synth_state"] == "DONE" and json.loads(t["synth"])["first"] == [{"v": 1, "why": "参数全"}]
    assert t["synth_of"] == run.signature(1)
    assert len(told) == 1 and told[0].startswith("🔎 专题读完了（1 期）：光固化怎么加支撑")  # the batch is read: a word on the phone
    # queued again with nothing changed (a video left out and back in): no new answer is asked for
    asked = stand_in.count("topic-synth")
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    assert run.dispatch(w)
    w.settle()
    assert stand_in.count("topic-synth") == asked and db.one("SELECT synth_state FROM topics")["synth_state"] == "DONE"
    # an answer asked for while a video is still to be read is on the page: nothing on the phone
    from assetizer.util import now
    db.exec("UPDATE topic_videos SET finished_at='2030-01-01T00:00:00+00:00', gist='支撑密度 80%，右手，读过两遍'")  # read again since
    db.exec("INSERT INTO videos(id, url, created_at) VALUES ('bbbbbbbbbbb', 'https://www.youtube.com/watch?v=bbbbbbbbbbb', ?)", now())
    db.exec("INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (1, 'bbbbbbbbbbb', 2, 'WAITING', 1, ?)", now())
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    run.put_together(1)
    assert stand_in.count("topic-synth") == asked + 1 and len(told) == 1
    db.exec("DELETE FROM topic_videos WHERE video_id='bbbbbbbbbbb'")
    # read again for a new question: the draft stays, the plan and looks are asked again
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    page = c.get("/api/topics/1").json()
    assert page["synth"]["answer"].startswith("密度") and not page["synth_stale"] and page["items"][0]["fit"] == "high"
    v = c.get("/api/topics/1/videos/abcdefghijk").json()
    assert v["notes"].startswith("## 支撑") and len(v["looks"]) == 5 and v["looks"][2]["media"][0].endswith("crop.png?r=1")
    assert c.get(v["looks"][0]["media"][0]).status_code == 200
    assert c.get("/api/topics/1/videos/abcdefghijk/files/../plan.json?r=1").status_code == 404
    assert c.get("/api/topics/1/videos/abcdefghijk/frame?t=12.3").headers["content-type"] == "image/jpeg"
    n = len(stand_in)
    page = c.post("/api/topics/1", json={"question": "光固化支撑要多密"}).json()
    assert page["topic"]["rev"] == 2 and page["items"][0]["state"] == "QUEUED" and page["items"][0]["stale"] and page["synth_stale"]
    run.dispatch(w)
    w.settle()
    again = stand_in[n:]
    assert not any(k.startswith("draft-") for k in again) and "topic-plan" in again
    assert db.one("SELECT done_rev FROM topic_videos")["done_rev"] == 2 and not work_dir(1, 1, "abcdefghijk").exists()
    assert knobs.get("topic_slots") == 3


def test_an_unrelated_video_stops_after_its_plan_unless_asked(stand_in, home, monkeypatch):
    from assetizer import db
    from assetizer.topics import run
    import sys
    me = sys.modules[__name__]
    real = me.answer
    monkeypatch.setattr(me, "answer", lambda kind: json.dumps({"fit": "none", "reason": "讲的是 FDM", "looks": [], "write": ""})
                        if kind == "topic-plan" else real(kind))
    db.set_setting("worker_slots", 2)
    w = Worker()
    run.dispatch(w)
    w.settle()
    item = db.one("SELECT * FROM topic_videos")
    assert item["state"] == "DONE" and item["fit"] == "none" and item["gist"] == "讲的是 FDM"
    assert not any(k.startswith("look-") for k in stand_in)
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    monkeypatch.setattr(me, "answer", lambda kind: json.dumps({"fit": "none", "reason": "仍然无关", "looks": [
        {"id": "L01", "start": "00:05", "question": "读数"}], "write": WRITE}) if kind.startswith("topic-plan") else real(kind))
    assert c.post("/api/topics/1/videos/abcdefghijk", json={"action": "force"}).json()["items"][0]["state"] == "QUEUED"
    run.dispatch(w)
    w.settle()
    item = db.one("SELECT * FROM topic_videos")
    assert item["state"] == "DONE" and item["fit"] == "high" and "topic-plan-forced" in stand_in


def test_topics_leave_the_channels_their_quota_and_slots(stand_in, home):
    from assetizer import db
    from assetizer.topics import run
    from assetizer.util import now
    db.set_setting("worker_slots", 1)
    w = Worker()
    w.active["run-x"] = threading.Thread()  # a channel's run holds the only slot
    assert not run.dispatch(w)
    w.active.clear()
    db.set_setting("agy_quota", {"ok": True, "checked_at": now(), "groups": [
        {"name": "Gemini Models", "models": "", "buckets": [{"window": "weekly", "remaining": 0.15, "reset_at": "2026-11-12T00:00:00Z"},
                                                             {"window": "5h", "remaining": 0.9, "reset_at": None}]}]})
    assert run.held() == {"window": "weekly", "reset_at": "2026-11-12T00:00:00Z", "remaining": 0.15, "vendor": "agy",
                          "group": "Gemini Models"} and not run.dispatch(w)
    db.set_setting("topic_min_weekly", 0.1)
    assert run.held() is None and run.dispatch(w)
    w.settle()


def test_a_topic_gives_its_slot_back_to_a_new_video(stand_in, home):
    from assetizer import db
    from assetizer.topics import Stopped, run
    from assetizer.util import now
    db.set_setting("worker_slots", 1)
    db.exec("UPDATE topic_videos SET state='RUNNING'")
    job = run.Job(1, "abcdefghijk")
    job.check()  # nothing else wants the slot
    db.exec("INSERT INTO sources(id, created_at, updated_at) VALUES ('s',?,?)", now(), now())
    db.exec("INSERT INTO runs(id, source_id, production, created_at) VALUES ('r','s','QUEUED',?)", now())
    with pytest.raises(Stopped):
        job.check()
    job.release()
    assert db.one("SELECT state, attempts FROM topic_videos") == {"state": "QUEUED", "attempts": 0}
    db.set_setting("worker_slots", 2)
    db.exec("UPDATE topic_videos SET state='RUNNING'")
    job.check()  # room for both
    db.set_setting("queue_paused", {"reason": "AGY_AUTH_REQUIRED"})
    db.set_setting("worker_slots", 1)
    job.check()  # the run is not going to start: the topic goes on


def test_two_topics_with_one_video_make_its_draft_once(stand_in, home):
    from assetizer import db
    from assetizer.topics import run
    from assetizer.util import now
    db.exec("INSERT INTO topics(question, created_at, updated_at) VALUES ('树脂怎么选', ?, ?)", now(), now())
    db.exec("INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (2, 'abcdefghijk', 1, 'QUEUED', 1, ?)", now())
    db.set_setting("worker_slots", 4)
    w = Worker()
    assert run.dispatch(w) and list(w.active) == ["topic:1:abcdefghijk"]  # the other waits for the draft
    w.settle()
    drafted = [k for k in stand_in if k.startswith("draft-")]
    assert run.dispatch(w) and set(w.active) == {"topic-synth:1", "topic:2:abcdefghijk"}
    w.settle()
    assert [r["state"] for r in db.q("SELECT state FROM topic_videos ORDER BY topic_id")] == ["DONE", "DONE"]
    assert [k for k in stand_in if k.startswith("draft-")] == drafted


def test_the_speech_does_not_wait_for_a_visual_to_make_the_drafts_folder(stand_in, home, monkeypatch):
    import time
    from assetizer import db
    from assetizer.topics import draft, run
    visual = draft.Draft._visual

    def late(self, *args, **kwargs):  # a busy host: the speech thread gets to ffmpeg first
        time.sleep(0.5)
        return visual(self, *args, **kwargs)
    monkeypatch.setattr(draft.Draft, "_visual", late)
    db.set_setting("worker_slots", 4)
    w = Worker()
    run.dispatch(w)
    w.settle()
    item = db.one("SELECT state, error FROM topic_videos")
    assert item["state"] == "DONE", item["error"]


def test_a_read_paused_while_its_call_is_tried_again_goes_back_in_the_queue(stand_in, home, monkeypatch):
    from assetizer import db
    from assetizer.providers.agy import ProviderError
    from assetizer.topics import run
    import sys
    me = sys.modules[__name__]
    real = me.answer

    def paused_meanwhile(kind):
        if kind == "topic-plan":
            db.exec("UPDATE topics SET paused=1")
            raise ProviderError("PROVIDER_FAILURE", "cut short")
        return real(kind)
    monkeypatch.setattr(me, "answer", paused_meanwhile)
    w = Worker()
    run.dispatch(w)
    w.settle()
    assert db.one("SELECT state, error_class FROM topic_videos") == {"state": "QUEUED", "error_class": None}


def test_a_paused_read_goes_back_in_the_queue_and_restarts_resume(stand_in, home):
    from assetizer import db
    from assetizer.topics import Stopped, run
    db.exec("UPDATE topic_videos SET state='RUNNING'")
    job = run.Job(1, "abcdefghijk")
    db.exec("UPDATE topics SET paused=1")
    with pytest.raises(Stopped):
        job.check()
    job.release()
    assert db.one("SELECT state FROM topic_videos")["state"] == "QUEUED"
    db.exec("UPDATE topic_videos SET state='RUNNING', attempts=5")
    run.recover()
    assert db.one("SELECT state, error_class FROM topic_videos") == {"state": "FAILED", "error_class": "TOO_MANY_RESTARTS"}


def test_numbers_are_not_given_twice_and_a_question_is_words(home):
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    one = [{"id": "aaaaaaaaaaa"}]
    assert c.post("/api/topics", json={"question": "   ", "videos": one}).status_code == 422
    tid = c.post("/api/topics", json={"question": " 光固化 ", "videos": [*one, {"id": "bbbbbbbbbbb"}]}).json()["id"]
    assert c.post(f"/api/topics/{tid}", json={"question": "　 "}).status_code == 422
    c.post(f"/api/topics/{tid}/videos/bbbbbbbbbbb", json={"action": "remove"})
    page = c.post(f"/api/topics/{tid}/videos", json={"videos": [{"id": "ccccccccccc"}]}).json()
    assert page["topic"]["question"] == "光固化" and [(i["id"], i["no"]) for i in page["items"]] == [("aaaaaaaaaaa", 1), ("ccccccccccc", 3)]


def test_a_basket_sent_twice_makes_one_topic(home):
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    body = {"question": "怎么加支撑", "videos": [{"id": "aaaaaaaaaaa"}], "batch": "b4sket-0001"}
    first = c.post("/api/topics", json=body).json()
    again = c.post("/api/topics", json=body).json()
    assert again == {"id": first["id"], "again": True}
    assert len(c.get("/api/topics").json()["topics"]) == 1
    assert c.post("/api/topics", json={**body, "batch": "no good!"}).status_code == 422


def test_samples_keep_their_low_copies_before_topics(home):
    from assetizer import playback
    assert playback._rank({}, 1.0) > playback._rank({"topic": True}, 9.0)


def test_topic_media_is_released_and_folders_of_deleted_topics_go(stand_in, home):
    from assetizer import db, playback
    from assetizer.topics import media_dir, root, run
    assert "abcdefghijk" in playback.wanted()  # the page plays a 360p copy of it
    db.exec("UPDATE topic_videos SET state='DONE'")
    db.exec("UPDATE videos SET used_at='2026-01-01T00:00:00+00:00'")
    (root() / "9").mkdir(parents=True)
    assert run.retention() == {"purged": 1, "dropped": 0}
    assert not media_dir("abcdefghijk").exists() and not (root() / "9").exists()
    assert db.one("SELECT media_state FROM videos")["media_state"] == "PURGED"


# ---------------------------------------------------------------- the answer is made of claims (design §R8-10)
def test_notes_know_the_day_and_what_the_speaker_gains(stand_in, home, monkeypatch):
    """A time word is read against the day the video came out; ads, courses, positions and what is relayed are kept,
    through the 补看 too; a video handed over without its day gets it from YouTube before its notes."""
    from assetizer import db, youtube
    from assetizer.topics import run
    import sys
    me = sys.modules[__name__]
    real = me.answer
    tail = {"fit": "high", "gist": "明年上半年见顶", "interests": ["持仓：重仓 XX [00:12 说]"], "relays": ["高盛 10 月的报告"],
            "looks": [{"id": "R01", "start": "00:30", "end": "00:32", "mode": "frame", "question": "再看一眼"}]}
    monkeypatch.setattr(me, "answer", lambda kind: ("## 判断\n\n明年上半年（2026 年上半年）见顶 [00:05 说]。" + "细节。" * 80 +
                                                    "\n\n```json\n" + json.dumps(tail, ensure_ascii=False) + "\n```")
                        if kind == "topic-write" else real(kind))
    monkeypatch.setattr(youtube, "metadata", lambda url: {"title": "加支撑", "channel": "Lab", "upload_date": "20251203"})
    db.set_setting("worker_slots", 2)
    w = Worker()
    run.dispatch(w)
    w.settle()
    item = db.one("SELECT * FROM topic_videos")
    assert item["state"] == "DONE", item["error"]
    assert db.one("SELECT published_at FROM videos")["published_at"] == "20251203"
    assert "2025-12-03" in ASKED["topic-write"] and "2025-12-03" in ASKED["topic-revise"]
    assert json.loads(item["card"]) == {"interests": ["持仓：重仓 XX [00:12 说]"], "relays": ["高盛 10 月的报告"]}


CLAIMS = {
    "answer": "会提价 [V2 00:20 帧]。", "certainty": {"level": "medium", "why": "一期事实推翻了旧预测"},
    "sections": [{"title": "价格", "claims": ["C1", "C2", "C3", "C9"]}, {"title": "渠道", "claims": [], "note": "这批视频都没讲"}],
    "claims": [
        {"id": "C1", "type": "预测", "text": "会降价 [V1 00:10 说]", "v": [1], "independent": 1, "certainty": "高", "status": "现行",
         "due": "2025-12-31", "condition": "年底前官网标价低于 3 月"},
        {"id": "C2", "type": "fact", "text": "已经提价 [V2 00:20 帧]", "v": [2, 9], "independent": 3, "certainty": "高"},
        {"id": "C3", "type": "解读", "text": "没有出处的话", "v": []},
        {"id": "C2", "type": "解读", "text": "同号的第二条", "v": [2]},
    ],
    "disputes": [{"id": "D1", "about": "会不会降价", "claims": ["C1", "C3"]},
                 {"id": "D2", "about": "价格往哪走", "claims": ["C1", "C2"], "decide": "年底的标价", "lean": "提价，V2 是标价截图"}],
    "sources": [{"v": 1, "stance": "看降", "interests": "赞助：某券商"}, {"v": 9, "stance": "?"}],
    "balance": "", "first": [{"v": 2, "why": "有标价"}, {"v": 9, "why": "?"}], "videos": [{"v": 1, "adds": "旧预测"}],
    "changes": ["C1 落空：V2 显示已经提价"], "answer_changed": True,
}


def test_the_answer_is_made_of_claims_each_with_its_kind_videos_and_certainty(home):
    """A claim without a source, a video not in the batch and a side with no other side drop out; a claim one source
    holds up is never 高; the ids stay; what the user marked stays as they marked it."""
    from assetizer.topics import write as W
    got: dict = {}

    class Fake:
        def structured(self, root, stage, ask, schema, normalize=None, check=None):
            got["ask"], got["schema"] = ask, schema
            o = normalize(json.loads(json.dumps(CLAIMS)))
            if check:
                check(o)
            return o
    videos = [{"no": 1, "title": "三月", "channel": "甲", "published": "20250301", "fit": "high", "gist": "会降价",
               "asset": "会降价 [00:10 说]", "card": {"interests": ["赞助：某券商 [01:00 说]"], "relays": []}},
              {"no": 2, "title": "十月", "channel": "乙", "published": "20251001", "fit": "high", "gist": "已提价",
               "asset": "已经提价 [00:20 帧]", "card": {}}]
    prev = {"answer": "会降价 [V1 00:10 说]。", "certainty": {"level": "低", "why": "只有一期"},
            "claims": [{"id": "C1", "type": "预测", "text": "会降价 [V1 00:10 说]", "v": [1], "status": "现行"}]}
    marks = {"C1": {"status": "落空", "why": "已经提价", "at": "2026-11-01T00:00:00+00:00"}}
    o = W.synth(Fake(), home, "topic-synth", "会降价吗", None, videos, prev=prev, marks=marks, today="2026-11-09")
    ask = got["ask"]
    assert "发布：2025-03-01" in ask and "利益相关：赞助：某券商 [01:00 说]" in ask and "今天是 2026-11-09" in ask
    assert '"C1"' in ask and "落空" in ask and "已经提价" in ask  # the version before and the user's mark
    assert "claims" in got["schema"]["required"] and "certainty" in got["schema"]["required"]
    by = {c["id"]: c for c in o["claims"]}
    assert list(by) == ["C1", "C2"]  # C3 has no source; the second C2 is a double
    assert by["C1"]["status"] == "落空" and by["C1"]["certainty"] == "中"  # the user's mark; one source is never 高
    assert by["C2"]["type"] == "事实" and by["C2"]["v"] == [2] and by["C2"]["independent"] == 1 and by["C2"]["certainty"] == "中"
    assert o["certainty"]["level"] == "中" and o["sections"][0]["claims"] == ["C1", "C2"] and o["sections"][1]["note"] == "这批视频都没讲"
    assert [d["id"] for d in o["disputes"]] == ["D2"] and [s["v"] for s in o["sources"]] == [1]
    assert [f["v"] for f in o["first"]] == [2] and o["answer_changed"] is True


def test_each_answer_is_kept_as_a_version_and_the_next_one_is_told_the_last(stand_in, home, monkeypatch):
    from assetizer import db, notify
    from assetizer.topics import run
    import sys
    me = sys.modules[__name__]
    real = me.answer
    monkeypatch.setattr(notify, "send", lambda text, keep=True: True)
    monkeypatch.setattr(me, "answer", lambda kind: json.dumps({**CLAIMS, "answer": "会提价 [V1 00:10 说]。"}, ensure_ascii=False)
                        if kind == "topic-synth" else real(kind))
    db.set_setting("worker_slots", 2)
    w = Worker()
    run.dispatch(w)
    w.settle()
    run.dispatch(w)
    w.settle()
    assert [r["n"] for r in db.q("SELECT n FROM topic_versions WHERE topic_id=1")] == [1]
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    page = c.post("/api/topics/1/marks", json={"claim": "C1", "status": "落空", "why": "后来提价了"}).json()
    c1 = next(x for x in page["synth"]["claims"] if x["id"] == "C1")
    assert c1["status"] == "落空" and page["since"]["marks"] == 1  # marked: shown at once, the answer still to be redone
    assert c.post("/api/topics/1/marks", json={"claim": "C7", "status": "落空"}).status_code == 404
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "DONE"  # a mark never asks a model by itself
    assert c.post("/api/topics/1/synth", json={}).status_code == 200
    run.dispatch(w)
    w.settle()
    assert db.one("SELECT synth_state, synth_error FROM topics") == {"synth_state": "DONE", "synth_error": None}
    assert [r["n"] for r in db.q("SELECT n FROM topic_versions WHERE topic_id=1")] == [1, 2]
    assert "后来提价了" in ASKED["topic-synth"] and "会提价 [V1 00:10 说]" in ASKED["topic-synth"]  # the last version, the mark
    page = c.get("/api/topics/1").json()
    assert page["since"]["marks"] == 0 and [x["n"] for x in page["versions"]] == [2, 1]
    assert next(x for x in page["synth"]["claims"] if x["id"] == "C1")["status"] == "落空"


def test_a_channel_s_predictions_are_counted_over_every_topic_once(home):
    """The sources show how a channel's predictions went: over every topic's answer, the user's marks applied; one
    prediction from one video in two topics counts once, settled over open; replaced or 不算 does not count."""
    from assetizer import db
    from assetizer.util import now
    from assetizer.web import topics as T
    for vid, ch in (("aaaaaaaaaaa", "甲"), ("bbbbbbbbbbb", "乙")):
        db.exec("INSERT INTO videos(id, url, title, channel, created_at) VALUES (?, ?, ?, ?, ?)", vid, f"https://youtu.be/{vid}", vid, ch, now())

    def topic(n: int, claims: list[dict], marks: dict | None = None):
        db.exec("INSERT INTO topics(question, created_at, updated_at, synth, marks) VALUES (?, ?, ?, ?, ?)", f"问题{n}", now(), now(),
                json.dumps({"answer": "", "claims": claims, "sections": [], "first": [], "videos": []}, ensure_ascii=False),
                json.dumps(marks, ensure_ascii=False) if marks else None)
        for no, vid in ((1, "aaaaaaaaaaa"), (2, "bbbbbbbbbbb")):
            db.exec("INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (?, ?, ?, 'DONE', 1, ?)", n, vid, no, now())
    p = {"type": "预测", "v": [1], "due": "2026-06-30", "status": "现行"}
    topic(1, [{**p, "id": "C1", "text": "会降价"}, {**p, "id": "C2", "text": "会裁员", "due": "2026-03-31", "status": "落空"},
              {**p, "id": "C3", "text": "会扩产", "v": [1, 2], "due": "2027-01-01"}, {**p, "id": "C4", "text": "旧说法", "status": "被取代"},
              {"id": "C5", "type": "事实", "text": "提价了", "v": [2], "status": "现行"}])
    topic(2, [{**p, "id": "C1", "text": "年中前降价"}, {**p, "id": "C2", "text": "会上市", "due": "2026-09-30"}],
          marks={"C1": {"status": "应验", "at": now()}, "C2": {"status": "不算", "at": now()}})
    from assetizer.topics import write as W
    assert W.record({r["channel"] for r in T._items(1)}) == {"甲": {"hit": 1, "miss": 1, "open": 1}, "乙": {"hit": 0, "miss": 0, "open": 1}}


def _second_read(home):
    """A second video of the stand-in topic, read already (its notes on disk)."""
    from assetizer import db
    from assetizer.topics import work_dir
    from assetizer.util import now
    db.exec("INSERT INTO videos(id, url, title, channel, created_at) VALUES ('bbbbbbbbbbb', 'https://youtu.be/bbbbbbbbbbb', '十月', '乙', ?)", now())
    db.exec("""INSERT INTO topic_videos(topic_id, video_id, no, state, rev, done_rev, fit, gist, finished_at, added_at)
               VALUES (1, 'bbbbbbbbbbb', 2, 'DONE', 1, 1, 'high', '已经提价', ?, ?)""", now(), now())
    d = work_dir(1, 1, "bbbbbbbbbbb")
    d.mkdir(parents=True, exist_ok=True)
    (d / "notes.md").write_text("已经提价 [00:20 帧]")


def test_an_answer_done_again_is_told_only_when_it_changed_and_leaving_a_video_out_waits(stand_in, home, monkeypatch):
    """Put together again, the phone hears of it only when the conclusion or how sure it is changed, and is told
    what changed. A video left out does not put the answer together again by itself; the page says the answer still
    cites it, and the next answer is asked for there."""
    from fastapi.testclient import TestClient
    from assetizer import db, notify
    from assetizer.topics import run
    from assetizer.web.app import app
    import sys
    me = sys.modules[__name__]
    real = me.answer
    told: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: told.append(text) or True)
    replies = iter([CLAIMS, {**CLAIMS, "answer_changed": False, "changes": ["措辞"]},
                    {**CLAIMS, "answer_changed": True, "changes": ["结论：从会降价改为会提价"]}])
    monkeypatch.setattr(me, "answer", lambda kind: json.dumps(next(replies), ensure_ascii=False) if kind == "topic-synth" else real(kind))
    _second_read(home)
    db.set_setting("worker_slots", 2)
    w = Worker()
    c = TestClient(app, base_url="http://localhost")

    def synth():
        run.dispatch(w)
        w.settle()
    synth()  # V1 read
    synth()  # the first answer
    assert len(told) == 1 and told[0].startswith("🔎 专题读完了（2 期）")
    db.exec("UPDATE topic_videos SET finished_at='2030-01-01T00:00:00+00:00' WHERE video_id='abcdefghijk'")  # read again
    run.settle(1)
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "QUEUED"  # a read the answer does not have
    synth()
    assert len(told) == 1  # the same conclusion, as sure: nothing on the phone
    page = c.post("/api/topics/1/videos/bbbbbbbbbbb", json={"action": "exclude"}).json()
    assert page["synth_state"] == "DONE" and page["since"]["out"] == 1  # left out: said on the page, not done again by itself
    page = c.post("/api/topics/1/videos/bbbbbbbbbbb", json={"action": "include"}).json()
    assert page["synth_state"] == "DONE" and page["since"]["out"] == 0
    from assetizer.topics import work_dir
    (work_dir(1, 1, "abcdefghijk") / "notes.md").write_text("已经提价了 [00:12 说]")  # read again, and it says something new
    db.exec("UPDATE topic_videos SET finished_at='2031-01-01T00:00:00+00:00' WHERE video_id='abcdefghijk'")
    run.settle(1)
    synth()
    assert len(told) == 2 and told[1].startswith("🔎 专题更新了") and "结论：从会降价改为会提价" in told[1]


def test_an_archived_topic_keeps_its_answer_and_stops(stand_in, home, monkeypatch):
    """A question answered is archived with how it came out: its answer stays, nothing more is read, put together,
    watched or told; taken back, it goes on."""
    from fastapi.testclient import TestClient
    from assetizer import db, notify
    from assetizer.topics import run
    from assetizer.web.app import app
    monkeypatch.setattr(notify, "send", lambda text, keep=True: True)
    db.set_setting("worker_slots", 2)
    w = Worker()
    run.dispatch(w)
    w.settle()
    run.dispatch(w)
    w.settle()
    c = TestClient(app, base_url="http://localhost")
    page = c.post("/api/topics/1/archive", json={"how": "应验", "why": "12 月真降了"}).json()
    assert page["topic"]["archived"]["how"] == "应验" and page["topic"]["archived"]["why"] == "12 月真降了" and page["synth"]
    assert c.post("/api/topics/1/archive", json={"how": "大概"}).status_code == 422
    assert c.post("/api/topics/1/synth", json={}).status_code == 409
    assert c.post("/api/topics/1/videos", json={"videos": [{"id": "ccccccccccc"}]}).status_code == 409
    assert c.post("/api/topics/1/watch", json={"channels": [], "words": ["降息"]}).status_code == 409
    db.exec("UPDATE topic_videos SET state='QUEUED'")
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    assert not run.dispatch(w)  # nothing read, nothing put together
    assert c.get("/api/topics").json()["topics"][0]["archived"]["how"] == "应验"
    page = c.post("/api/topics/1/archive", json={"how": None}).json()
    assert page["topic"]["archived"] is None
    assert run.dispatch(w)
    w.settle()


def test_a_prediction_due_is_told_once_to_be_settled(home, monkeypatch):
    """A prediction past its day, not settled, is told once (one word for a topic's); settled, marked, still to
    come or archived it is not."""
    from assetizer import db, notify
    from assetizer.topics import run
    from assetizer.util import now
    told: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: told.append(text) or True)
    p = {"type": "预测", "v": [1], "status": "现行"}
    claims = [{**p, "id": "C1", "text": "年底前降两次 [V1 00:10 说]", "due": "2026-01-31"},
              {**p, "id": "C2", "text": "明年回到 3% [V1 00:20 说]", "due": "2099-12-31"},
              {**p, "id": "C3", "text": "11 月就降 [V1 00:30 说]", "due": "2025-11-30"},
              {**p, "id": "C4", "text": "已经结算的 [V1]", "due": "2025-10-31", "status": "落空"},
              {"id": "C5", "type": "事实", "text": "降了 [V1]", "v": [1], "status": "现行"}]
    for q, arch in (("降息几次", None), ("美元", now())):
        db.exec("""INSERT INTO topics(question, created_at, updated_at, synth, synth_state, marks, archived_at)
                   VALUES (?, ?, ?, ?, 'DONE', ?, ?)""", q, now(), now(),
                json.dumps({"answer": "", "claims": claims, "sections": [], "first": [], "videos": []}, ensure_ascii=False),
                json.dumps({"C3": {"status": "应验", "at": now()}}), arch)
    run.remind(today="2026-11-09")
    assert len(told) == 1 and "降息几次" in told[0] and "年底前降两次" in told[0] and "[V1" not in told[0]
    assert "明年回到" not in told[0] and "11 月就降" not in told[0] and "已经结算" not in told[0]
    run.remind(today="2026-11-10")
    assert len(told) == 1


def test_watched_channels_offer_new_videos_without_adding_them(home, monkeypatch):
    """A topic watches channels and words; what is new and fits is offered (要加吗), never added or read by itself;
    what was there when the watch began, or does not fit, is not offered; taken or dropped, it is not offered again."""
    from fastapi.testclient import TestClient
    from assetizer import db, notify
    from assetizer.topics import run, watch
    from assetizer.util import now
    from assetizer.web.app import app
    told: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: told.append(text) or True)
    db.exec("INSERT INTO topics(question, created_at, updated_at) VALUES ('美联储会降息几次', ?, ?)", now(), now())
    db.exec("""INSERT INTO videos(id, url, title, channel, info, created_at) VALUES ('aaaaaaaaaaa', 'https://youtu.be/aaaaaaaaaaa',
               '九月', '利率观察', ?, ?)""", json.dumps({"channel_url": "https://www.youtube.com/channel/UCrates"}), now())
    db.exec("INSERT INTO topic_videos(topic_id, video_id, no, state, rev, added_at) VALUES (1, 'aaaaaaaaaaa', 1, 'DONE', 1, ?)", now())
    c = TestClient(app, base_url="http://localhost")
    page = c.get("/api/topics/1").json()
    assert page["watch"]["can"] == [{"name": "利率观察", "url": "https://www.youtube.com/channel/UCrates"}]
    up = [{"id": "old00000000", "title": "降息的老视频", "channel": "利率观察"}]
    monkeypatch.setattr(watch, "listing", lambda target, most=15: [dict(e) for e in up])
    page = c.post("/api/topics/1/watch", json={"channels": ["https://www.youtube.com/channel/UCrates"], "words": ["降息"]}).json()
    assert page["watch"]["channels"] == [{"name": "利率观察", "url": "https://www.youtube.com/channel/UCrates"}]
    watch.check(1)  # the first look: what is there already is not new
    assert not told and c.get("/api/topics/1").json()["candidates"] == []
    up[:0] = [{"id": "new00000001", "title": "12 月降息前瞻", "channel": "利率观察", "duration": 600, "upload_date": "20261108"},
              {"id": "new00000002", "title": "开箱一台相机", "channel": "利率观察"},
              {"id": "aaaaaaaaaaa", "title": "九月", "channel": "利率观察"}]
    watch.check(1)
    assert len(told) == 1 and "12 月降息前瞻" in told[0] and "开箱" not in told[0]
    page = c.get("/api/topics/1").json()
    assert [x["id"] for x in page["candidates"]] == ["new00000001"] and page["counts"]["videos"] == 1  # offered, not added
    page = c.post("/api/topics/1/candidates/new00000001", json={"take": True}).json()
    assert page["candidates"] == [] and [it["id"] for it in page["items"]] == ["aaaaaaaaaaa", "new00000001"]
    assert page["items"][1]["state"] == "QUEUED"
    watch.check(1)
    assert len(told) == 1  # nothing new
    assert db.one("SELECT watched_at FROM topics")["watched_at"]
    t = db.one("SELECT watched_at FROM topics")["watched_at"]
    run.housekeeping(10**9)  # not due again within the day
    assert db.one("SELECT watched_at FROM topics")["watched_at"] == t


# ---------------------------------------------------------------- what goes wrong in use (第四批)
def test_a_video_left_out_says_why(stand_in, home):
    """不算这期 says why — 离题, 质量差 or 转述别人 — on the row; taken back in, the reason goes with it."""
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    _second_read(home)
    url = "/api/topics/1/videos/bbbbbbbbbbb"
    assert c.post(url, json={"action": "exclude", "why": "不喜欢"}).status_code == 422
    row = lambda page: next(i for i in page["items"] if i["id"] == "bbbbbbbbbbb")  # noqa: E731
    page = c.post(url, json={"action": "exclude", "why": "转述别人"}).json()
    assert row(page)["excluded"] and row(page)["excluded_why"] == "转述别人"
    page = c.post(url, json={"action": "include"}).json()
    assert not row(page)["excluded"] and row(page)["excluded_why"] is None


def test_a_video_waiting_half_a_day_does_not_hold_the_answer_back(stand_in, home, monkeypatch):
    """A video YouTube keeps refusing holds the answer back half a day at most: then the others are put together,
    and the phone is told which one waits and why; once it is read, the answer is put together again with it."""
    import time
    from assetizer import db, notify
    from assetizer.topics import run
    told: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: told.append(text) or True)
    _second_read(home)
    wait = lambda: (db.exec("UPDATE topic_videos SET state='RUNNING' WHERE video_id='abcdefghijk'"),  # noqa: E731
                    run.Job(1, "abcdefghijk").wait("YOUTUBE_AUTH", "要登录"))
    wait()
    first = db.one("SELECT state, wait_since FROM topic_videos WHERE video_id='abcdefghijk'")
    assert first["state"] == "WAITING" and first["wait_since"]
    assert db.one("SELECT synth_state FROM topics")["synth_state"] is None  # it may be read any minute: the answer waits
    wait()  # tried again (every half hour): the first wait is what counts
    assert db.one("SELECT wait_since FROM topic_videos WHERE video_id='abcdefghijk'")["wait_since"] == first["wait_since"]
    db.exec("UPDATE topic_videos SET wait_since='2026-01-01T00:00:00+00:00' WHERE video_id='abcdefghijk'")
    t = time.time()
    monkeypatch.setattr(run, "_last", {"retry": 0.0, "retention": t, "remind": t, "watch": t, "comments": t})
    run.housekeeping(t)  # half a day on: tried again, and the answer no longer waits for it
    assert db.one("SELECT state FROM topic_videos WHERE video_id='abcdefghijk'")["state"] == "QUEUED"
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "QUEUED"
    run.put_together(1)
    assert len(told) == 1 and told[0].startswith("🔎 专题读完了（1 期，1 期还在等）")
    assert "V1 还在等：YouTube 需登录，读完再补上" in told[0]
    w = Worker()
    run.dispatch(w)  # read at last: put in
    w.settle()
    assert db.one("SELECT state, wait_since FROM topic_videos WHERE video_id='abcdefghijk'") == {"state": "DONE", "wait_since": None}
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "QUEUED"


def test_a_video_taken_out_comes_back_with_its_number_and_notes(stand_in, home):
    """移出专题 hides a video for 7 days: 撤销, or adding it again, brings back its number and its notes with nothing
    read again (read again only for a question changed meanwhile); after 7 days its reads go."""
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.topics import run, work_dir
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    _second_read(home)
    url = "/api/topics/1/videos/bbbbbbbbbbb"
    rows = lambda page: [(i["id"], i["no"], i["state"]) for i in page["items"]]  # noqa: E731
    assert c.post(url, json={"action": "restore"}).status_code == 404  # nothing taken out
    page = c.post(url, json={"action": "remove"}).json()
    assert rows(page) == [("abcdefghijk", 1, "QUEUED")] and (work_dir(1, 1, "bbbbbbbbbbb") / "notes.md").exists()
    page = c.post(url, json={"action": "restore"}).json()
    assert rows(page) == [("abcdefghijk", 1, "QUEUED"), ("bbbbbbbbbbb", 2, "DONE")] and page["items"][1]["fit"] == "high"
    c.post(url, json={"action": "remove"})
    page = c.post("/api/topics/1/videos", json={"videos": [{"id": "bbbbbbbbbbb"}, {"id": "ccccccccccc"}]}).json()
    assert rows(page) == [("abcdefghijk", 1, "QUEUED"), ("bbbbbbbbbbb", 2, "DONE"), ("ccccccccccc", 3, "QUEUED")]
    c.post(url, json={"action": "remove"})
    c.post("/api/topics/1", json={"question": "支撑要多密"})
    page = c.post(url, json={"action": "restore"}).json()
    back = next(i for i in page["items"] if i["id"] == "bbbbbbbbbbb")
    assert (back["no"], back["state"], back["stale"]) == (2, "QUEUED", True)  # read for the new question; the old notes till then
    c.post(url, json={"action": "remove"})
    db.exec("UPDATE topic_removed SET removed_at='2026-01-01T00:00:00+00:00'")
    run.retention()
    assert not work_dir(1, 1, "bbbbbbbbbbb").exists() and c.post(url, json={"action": "restore"}).status_code == 404
    assert db.one("SELECT 1 FROM videos WHERE id='bbbbbbbbbbb'")  # its draft stays with the video a while, for the next topic


def test_a_video_gone_from_youtube_keeps_its_frames_and_is_read_from_its_draft(stand_in, home, monkeypatch):
    """The frames the notes cite are kept when the read ends; once the original is purged and the video is gone
    from YouTube, they still show, and a read for a new question goes on from the draft (no looks) instead of failing."""
    import sys
    from fastapi.testclient import TestClient
    from assetizer import db, youtube
    from assetizer.topics import draft_dir, run
    from assetizer.web.app import app
    me = sys.modules[__name__]
    real = me.answer
    monkeypatch.setattr(me, "answer", lambda kind: ("## 支撑\n\n密度 80% [00:10.0 帧]，右手 [R01]。" + "细节。" * 80 +
                                                    '\n\n```json\n{"fit": "high", "gist": "支撑密度 80%"}\n```')
                        if kind == "topic-revise" else real(kind))
    db.set_setting("worker_slots", 4)
    w = Worker()
    run.dispatch(w)
    w.settle()
    assert (draft_dir("abcdefghijk") / "shots" / "10.0.jpg").exists()
    db.exec("UPDATE videos SET used_at='2026-01-01T00:00:00+00:00'")
    assert run.retention()["purged"] == 1
    c = TestClient(app, base_url="http://localhost")
    v = c.get("/api/topics/1/videos/abcdefghijk").json()
    assert not v["frames"] and v["shots"] == ["10.0"]
    assert c.get("/api/topics/1/videos/abcdefghijk/frame?t=10").status_code == 200
    assert c.get("/api/topics/1/videos/abcdefghijk/frame?t=11").status_code == 404

    def gone(*_a, **_k):
        raise youtube.AcquireError("VIDEO_REMOVED", "This video has been removed by the uploader")
    monkeypatch.setattr(youtube, "download", gone)
    monkeypatch.setattr(youtube, "metadata", gone)
    c.post("/api/topics/1", json={"question": "光固化支撑要多密"})
    n = len(stand_in)
    run.dispatch(w)
    w.settle()
    item = db.one("SELECT state, done_rev, error FROM topic_videos")
    assert (item["state"], item["done_rev"]) == ("DONE", 2), item["error"]
    again = stand_in[n:]
    assert "topic-plan" in again and "topic-write" in again
    assert not any(k.startswith(("draft-", "look-", "second-", "topic-revise")) for k in again)
    assert "原片已下架" in ASKED["topic-write"]
    assert db.one("SELECT gone FROM videos")["gone"] == "VIDEO_REMOVED"
    assert c.get("/api/topics/1").json()["items"][0]["gone"] == "VIDEO_REMOVED"

    def back(_url, where):  # back on YouTube: downloaded, looked at again, no longer gone
        where.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=40",
                        "-c:v", "libx264", str(where / "source.mkv")], check=True)
    monkeypatch.setattr(youtube, "download", back)
    c.post("/api/topics/1", json={"question": "光固化支撑要多密、多高"})
    n = len(stand_in)
    run.dispatch(w)
    w.settle()
    assert db.one("SELECT state, done_rev FROM topic_videos")["state"] == "DONE"
    assert any(k.startswith("look-") for k in stand_in[n:])
    assert db.one("SELECT gone FROM videos")["gone"] is None


def test_a_frame_is_kept_at_the_moment_the_notes_cite():
    """To a tenth of a second, inside the video; with its length not known yet, where it was cited."""
    from assetizer.topics.draft import shot
    assert shot("abcdefghijk", 754.5, None)[0] == 754.5
    assert shot("abcdefghijk", 100.0, 50.0)[0] == 49.8
    assert shot("abcdefghijk", 12.04, 50.0)[1].name == "12.0.jpg"


def test_a_line_is_read_as_youtube_has_it_and_only_youtube(home, monkeypatch):
    """A phone's youtu.be link with a list is that one video; a link elsewhere (bilibili) is refused, not searched;
    a channel page without https:// is a link. A list longer than one read says how long it is and reads on from
    where it stopped. A video this account cannot open is said so (422), YouTube not answering is a retry (503)."""
    from contextlib import contextmanager
    from fastapi.testclient import TestClient
    from assetizer import cookies, youtube
    from assetizer.web import topics as T
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")

    def meta(url):
        vid = youtube.video_id(url)
        if vid == "mmmmmmmmmmm":
            raise youtube.AcquireError("VIDEO_MEMBERS", "join this channel")
        if vid == "nnnnnnnnnnn":
            raise youtube.AcquireError("NETWORK", "timed out")
        return {"id": vid, "title": "一期", "channel": "甲", "duration": 60}

    @contextmanager
    def login():
        yield [], "none"
    lists: list[list[str]] = []

    def run(cmd, **_kw):
        lists.append(cmd)
        a, b = map(int, cmd[cmd.index("--playlist-items") + 1].split("-"))
        entries = [{"id": f"v{n:010d}", "title": f"第 {n} 期"} for n in range(a, min(b, 120) + 1)]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"title": "系列", "playlist_count": 120, "entries": entries}), "")
    monkeypatch.setattr(youtube, "metadata", meta)
    monkeypatch.setattr(cookies, "ytdlp_login", login)
    monkeypatch.setattr(T.subprocess, "run", run)
    peek = lambda url, **kw: c.get("/api/topics/peek", params={"url": url, **kw})  # noqa: E731
    r = peek("https://youtu.be/abcdefghijk?si=Xy12&list=PLabcdefghij").json()
    assert r["kind"] == "video" and [v["id"] for v in r["videos"]] == ["abcdefghijk"] and not lists
    for url in ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili.com/video/BV1xx411c7mD", "b23.tv/abcd"):
        x = peek(url)
        assert (x.status_code, x.json()["detail"]) == (422, "只支持 YouTube 链接") and not lists
    r = peek("youtube.com/@macro/videos").json()
    assert lists[-1][-1] == "https://youtube.com/@macro/videos"
    assert (r["kind"], len(r["videos"]), r["total"], r["more"]) == ("list", 50, 120, True)
    r = peek("youtube.com/@macro/videos", start=101).json()
    assert (r["videos"][0]["title"], len(r["videos"]), r["more"]) == ("第 101 期", 20, False)
    peek("Node.js 教程")
    assert lists[-1][-1].startswith("ytsearch")
    x = peek("https://www.youtube.com/watch?v=mmmmmmmmmmm")
    assert (x.status_code, x.json()["detail"]) == (422, "会员视频，这个账号看不了")
    assert peek("https://www.youtube.com/watch?v=nnnnnnnnnnn").status_code == 503
