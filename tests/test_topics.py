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
    if kind.startswith("draft-visual"):
        return RECORD
    if kind.startswith("draft-text"):
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
        return json.dumps({"answer": "密度设 80% [V1 L01]。", "first": [{"v": 1, "why": "参数全"}],
                           "sections": [{"title": "参数", "body": "80% [V1 L01]"}], "videos": [{"v": 1, "adds": "密度"}]},
                          ensure_ascii=False)
    raise AssertionError(kind)


@pytest.fixture
def stand_in(home, monkeypatch):
    """Every model answered by `answer`; whisper by a line; a 40 s test video already downloaded."""
    from assetizer import db, providers
    from assetizer.media import asr
    from assetizer.providers.agy import CallResult
    from assetizer.topics import media_dir
    calls: list[str] = []

    class Stand:
        name, media_size_limit = "agy", None

        def call(self, call_dir, prompt, *, kind, schema=None, media=None, effort=None, timeout=None):
            calls.append(kind)
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
