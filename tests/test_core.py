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
