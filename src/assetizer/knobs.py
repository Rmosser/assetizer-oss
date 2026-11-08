"""The box's settings the 设置 page can change (design §R6-7): how much runs at once, which provider each stage uses, how
often a model call is tried again on each kind of error, how long media is kept, what 补录 leaves to new videos, when the
digest goes out, where speech is transcribed.

Each is a row in `settings` read when it is needed, so a change applies to the next run, download, cleanup or digest
without a restart. A stored value out of range is read as the nearest allowed one; an unreadable one as the default.
The CLI's `set <键> <值>` writes the same rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import config, db
from .providers import resolve

PROVIDERS = ("agy", "magpie", "tuzi")  # tuzi stays accepted for boxes still on it; the page offers agy's and magpie's models
MODEL_ID = re.compile(r"[\w.\-]+/[\w.\-@:+ /]{1,120}")  # a model of the gateway: codex/gpt-6.1-terra, grok/grok-4.7@500k, group/<id>
AGY_ID = re.compile(r"agy/gemini-[\w.\-]{1,100}")  # one of agy's: goes on its command line
STAGES = ("understanding", "merge", "knowledge", "article", "selection", "boundary", "review")  # providers.stage_key
STAGE_NAMES = dict(zip(STAGES, ("理解", "合并", "知识", "文章", "选片", "边界", "预审")))
VIDEO_STAGES = ("understanding", "boundary")
# 专题 (topics/): its own four steps, routed apart from production (setting `topic_providers`), so their quotas, logins and
# models never pause the channels' queue or hold back 补录 (providers.routes leaves them out)
TOPIC_STAGES = ("draft", "look", "second", "topic")
TOPIC_NAMES = dict(zip(TOPIC_STAGES, ("底稿", "回看", "复核", "写作")))
TOPIC_VIDEO = ("draft", "look")      # watch video: Gemini, or a model of the gateway that takes images
TOPIC_TEXT = "claude/claude-opus-5-5"  # the plans, notes and answers, when magpie lists it (else where 知识 goes)
# How often the call that failed is tried again, by what failed it (design §R6-10). Kinds not listed are never tried again:
# a login, a quota or a model taken off the list waits for the user or the health probe, not for another call.
RETRY_DEFAULTS = {"CONTENT_SAFETY": 2, "CONTRACT": 1, "MODALITY_ROUTING_DEVIATION": 2, "MEDIA_READ_FAILED": 0,
                  "PROVIDER_FAILURE": 0, "PROVIDER_OUTPUT_LIMIT": 0, "PROVIDER_TRANSIENT": 2, "PROVIDER_TIMEOUT": 2}
RETRY_NAMES = {"CONTENT_SAFETY": "被内容安全过滤拦下", "CONTRACT": "输出不合规", "MODALITY_ROUTING_DEVIATION": "模型越界读文件",
               "MEDIA_READ_FAILED": "读不了视频", "PROVIDER_FAILURE": "模型出错", "PROVIDER_OUTPUT_LIMIT": "输出超长",
               "PROVIDER_TRANSIENT": "模型暂时不可用", "PROVIDER_TIMEOUT": "模型超时"}
RETRY_MAX = 5
RETRY_ALIAS = {"MEDIA_READ_COUNT": "MODALITY_ROUTING_DEVIATION"}  # read the file a different number of times: the same deviation
NO_FALLBACK = "off"


@dataclass(frozen=True)
class Knob:
    label: str
    kind: str  # "int" "float" "provider" "stages" "topic_stages" "url" "hours" "burst" "retries" "fallback"
    lo: float = 0
    hi: float = 0
    default: Any = None


KNOBS: dict[str, Knob] = {
    "worker_slots": Knob("同时印", "int", 1, 16, 1),
    # 7 downloads at once through the box's one egress got their TLS connections cut (acquire.DOWNLOADS)
    "download_slots": Knob("同时下载", "int", 1, 4, 2),
    "llm_provider": Knob("默认模型", "provider", default="agy"),
    "stage_providers": Knob("工序分工", "stages", default={}),
    "retry_counts": Knob("自动重试", "retries", 0, RETRY_MAX, RETRY_DEFAULTS),  # stored: only what differs from the defaults
    "safety_fallback": Knob("还被拦就换", "fallback", default=None),  # None: where 知识 goes; "off": stay; else a route
    "asr_url": Knob("GPU 转写地址", "url", default=None),
    "retention_days": Knob("原片保留（天）", "int", 1, 60, None),  # None: ASSETIZER_RETENTION_DAYS, else 7
    "delivered_retention_days": Knob("交付后原片保留（天）", "int", 0, 30, 2),
    "playback_keep_days": Knob("低清版保留（天）", "int", 1, 60, 14),  # playback.KEEP_DAYS
    "playback_max_gb": Knob("低清版上限（GB）", "float", 0, 50, 5.0),  # playback.MAX_GB; 0: no copies
    "backfill_min_weekly": Knob("补录留给新视频的周额度", "float", 0, 0.9, 0.3),
    "backfill_min_5h": Knob("补录留给新视频的 5 小时额度", "float", 0, 0.9, 0.2),
    "backfill_fast_slots": Knob("补录高频路数", "int", 1, 16, 8),
    "backfill_hours": Knob("补录高频时段", "hours", default=[]),  # hours of the day, 东八区
    "backfill_burst": Knob("补录临时高频", "burst", default=None),  # {"until": when | None (until it is switched off)}
    "digest_hour": Knob("每日摘要（点）", "int", 0, 23, 9),
    "topic_providers": Knob("专题分工", "topic_stages", default={}),
    "topic_slots": Knob("专题同时读", "int", 1, 8, 3),
    "topic_min_weekly": Knob("专题留下的周额度", "float", 0, 0.9, 0.2),
    "topic_min_5h": Knob("专题留下的 5 小时额度", "float", 0, 0.9, 0.15),
}
_URL = re.compile(r"https?://[^\s/]+(:\d+)?/?")


class KnobError(ValueError):
    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key, self.message = key, message


def route_ok(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    return bool(AGY_ID.fullmatch(v)) if v.startswith("agy/") else v in PROVIDERS or bool(MODEL_ID.fullmatch(v))


def sees(route: str, inputs: list[str]) -> bool:
    """Whether a model of the gateway can take a stage that watches video: it is sent frames, so it has to take images. Not
    Claude's subscription: magpie runs it through Claude Code, where images go in by paste, and from about 20 in one message
    it ends without an answer (tried 2026-11-05); a video takes more."""
    return "image" in inputs and not route.startswith("claude/")


def watches(route: str) -> bool:
    """Whether a route can take a stage that watches video: Gemini watches it itself; a model of the gateway as magpie lists
    it (magpie_state.model_view, what the 设置 page offers), one it does not list as `sees` can tell from the name."""
    route = resolve(route)
    listed = next((m for m in (db.setting("magpie_state") or {}).get("models") or [] if m.get("id") == route), None)
    return sees(route, listed.get("inputs") or ["text"]) if listed else not route.startswith("claude/")


def _burst(v: Any) -> dict | None:
    """A 临时高频 still in force, or None."""
    if not isinstance(v, dict) or "until" not in v:
        return None
    if v["until"] is None:
        return {"until": None}
    try:
        return {"until": v["until"]} if datetime.fromisoformat(v["until"]) > datetime.now(timezone.utc) else None
    except (TypeError, ValueError):
        return None


def hours_text(hours: list[int]) -> str:
    """[23, 0, 1, 13] → "23:00–02:00、13:00–14:00": stretches of hours, read across midnight."""
    on = set(hours)
    if len(on) == 24:
        return "全天"
    out = []
    for h in sorted(on):
        if (h - 1) % 24 in on:
            continue
        end = h
        while (end + 1) % 24 in on:
            end = (end + 1) % 24
        out.append(f"{h:02d}:00–{(end + 1) % 24:02d}:00")
    return "、".join(out)


def default(key: str) -> Any:
    if key == "retention_days":
        return config.get().source_retention_days
    if key == "retry_counts":
        return dict(RETRY_DEFAULTS)
    return KNOBS[key].default


def _clamp(k: Knob, v: Any) -> Any:
    x = float(v)
    if x != x:  # NaN
        raise ValueError
    x = min(k.hi, max(k.lo, x))
    return int(round(x)) if k.kind in ("int", "retries") else x


def get(key: str) -> Any:
    """The value in effect: the stored one (clamped) or the default."""
    k = KNOBS[key]
    v = db.setting(key)
    if v is None or v == "":
        return default(key)
    try:
        if k.kind in ("int", "float"):
            return _clamp(k, v)
        if k.kind == "provider":
            return v if route_ok(v) else default(key)
        if k.kind == "stages":
            return {s: p for s, p in v.items() if s in STAGES and route_ok(p)} if isinstance(v, dict) else {}
        if k.kind == "topic_stages":
            return {s: p for s, p in v.items() if s in TOPIC_STAGES and route_ok(p)} if isinstance(v, dict) else {}
        if k.kind == "hours":
            return sorted({h for h in v if isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23}) if isinstance(v, list) else []
        if k.kind == "burst":
            return _burst(v)
        if k.kind == "retries":
            return {**RETRY_DEFAULTS, **{e: _clamp(k, n) for e, n in v.items() if e in RETRY_DEFAULTS}} if isinstance(v, dict) else default(key)
        if k.kind == "fallback":
            return v if v == NO_FALLBACK or route_ok(v) else default(key)
        return str(v).strip().rstrip("/") or None
    except (TypeError, ValueError, AttributeError):
        return default(key)


def view() -> dict:
    return {key: get(key) for key in KNOBS}


def retry_kind(kind: str) -> str:
    """The name a provider's error goes by in `retry_counts`."""
    return RETRY_ALIAS.get(kind, kind)


def retries(kind: str) -> int:
    """How often a call that failed this way is tried again. Read at each failure, so a change applies to the next one."""
    return get("retry_counts").get(retry_kind(kind), 0)


def fallback_route() -> str | None:
    """Where a call the safety filter keeps stopping gets its last try: the route set, by default the one 知识 goes to, a
    stage that wants accuracy rather than prose and so is usually on another subscription than the writing."""
    v = get("safety_fallback")
    return None if v == NO_FALLBACK else v or get("stage_providers").get("knowledge") or get("llm_provider")


def topic_default(stage: str) -> str:
    """Where a step of a topic goes when it is not set: the steps that watch video where 理解 goes (agy if that cannot
    watch), the second look and the writing to Claude Opus when magpie lists it (an independent eye, and the long drafts
    fit its context), else where 知识 goes."""
    stages = get("stage_providers")
    if stage in TOPIC_VIDEO:
        video = stages.get("understanding") or get("llm_provider")
        return video if watches(video) else "agy"
    listed = {m.get("id") for m in (db.setting("magpie_state") or {}).get("models") or []}
    return TOPIC_TEXT if TOPIC_TEXT in listed else stages.get("knowledge") or get("llm_provider")


def topic_routes() -> dict[str, str]:
    """Where each step of a topic goes now: as set on the 设置 page, else its default."""
    set_ = get("topic_providers")
    return {s: set_.get(s) or topic_default(s) for s in TOPIC_STAGES}


def takes_images(route: str) -> bool:
    """Whether a route can be the second look: shown at most twelve images in a call (topics.look.SECOND_IMAGES), so
    Claude's subscription can, as long as the model takes images at all."""
    route = resolve(route)
    if route.startswith("agy/"):
        return True
    listed = next((m for m in (db.setting("magpie_state") or {}).get("models") or [] if m.get("id") == route), None)
    return "image" in (listed.get("inputs") or []) if listed else True


def _check(key: str, v: Any) -> Any:
    """The value to store, or KnobError. None resets to the default."""
    k = KNOBS[key]
    if v is None:
        return None
    if k.kind in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise KnobError(key, f"{k.label}必须是数字")
        if not k.lo <= v <= k.hi:
            raise KnobError(key, f"{k.label}要在 {k.lo:g} 到 {k.hi:g} 之间")
        if k.kind == "int" and v != int(v):
            raise KnobError(key, f"{k.label}必须是整数")
        return int(v) if k.kind == "int" else float(v)
    if k.kind == "provider":
        if not route_ok(v):
            raise KnobError(key, f"{k.label}只能是 {'、'.join(PROVIDERS)} 或 magpie 的模型")
        return v
    if k.kind == "stages":
        if not isinstance(v, dict) or not all(s in STAGES and route_ok(p) for s, p in v.items()):
            raise KnobError(key, f"{k.label}的工序或模型不认识")
        if blind := [s for s, p in v.items() if s in VIDEO_STAGES and not watches(p)]:
            raise KnobError(key, f"{STAGE_NAMES[blind[0]]}要看视频，这个模型看不了")
        return v
    if k.kind == "topic_stages":
        if not isinstance(v, dict) or not all(s in TOPIC_STAGES and route_ok(p) for s, p in v.items()):
            raise KnobError(key, f"{k.label}的工序或模型不认识")
        if blind := [s for s, p in v.items() if s in TOPIC_VIDEO and not watches(p)]:
            raise KnobError(key, f"{TOPIC_NAMES[blind[0]]}要看视频，这个模型看不了")
        if "second" in v and not takes_images(v["second"]):
            raise KnobError(key, "复核要看图，这个模型看不了")
        return v
    if k.kind == "hours":
        if not isinstance(v, list) or not all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in v):
            raise KnobError(key, f"{k.label}要是 0 到 23 点")
        return sorted(set(v)) or None
    if k.kind == "burst":
        if not isinstance(v, dict) or set(v) != {"until"} or _burst(v) is None:
            raise KnobError(key, f"{k.label}的结束时间不对")
        return v
    if k.kind == "retries":
        if not isinstance(v, dict) or set(v) - set(RETRY_DEFAULTS):
            raise KnobError(key, f"{k.label}的错误类型不认识")
        for n in v.values():
            if isinstance(n, bool) or not isinstance(n, int) or not k.lo <= n <= k.hi:
                raise KnobError(key, f"{k.label}的次数要在 {k.lo:g} 到 {k.hi:g} 之间")
        return v
    if k.kind == "fallback":
        if v != NO_FALLBACK and not route_ok(v):
            raise KnobError(key, f"{k.label}的模型不认识")
        return v
    if not isinstance(v, str) or not _URL.fullmatch(v.strip()):
        raise KnobError(key, f"{k.label}要像 http://192.168.1.50:8790 这样")
    return v.strip().rstrip("/") or None


def save(body: Any) -> dict:
    """Partial update {key: value | null}; every value is checked before anything is written."""
    if not isinstance(body, dict) or not body:
        raise KnobError("", "没有要修改的内容")
    if "stage_route" in body:  # one stage's pick goes on what is stored: picks in quick succession must not undo each other
        pick = body["stage_route"]
        if not isinstance(pick, dict):
            raise KnobError("stage_route", f"{KNOBS['stage_providers'].label}的工序或模型不认识")
        body = {**{k: v for k, v in body.items() if k != "stage_route"}, "stage_providers": {**get("stage_providers"), **pick}}
    if "topic_route" in body:  # likewise one step of the topics; null puts it back to its default
        pick = body["topic_route"]
        if not isinstance(pick, dict):
            raise KnobError("topic_route", f"{KNOBS['topic_providers'].label}的工序或模型不认识")
        both = {**get("topic_providers"), **pick}
        body = {**{k: v for k, v in body.items() if k != "topic_route"},
                "topic_providers": {s: p for s, p in both.items() if p is not None}}
    if "retry_count" in body:  # likewise one kind of error's count; null puts it back to the default
        pick = body["retry_count"]
        if not isinstance(pick, dict):
            raise KnobError("retry_count", f"{KNOBS['retry_counts'].label}的错误类型不认识")
        both = {**get("retry_counts"), **{e: RETRY_DEFAULTS.get(e, 0) if n is None else n for e, n in pick.items()}}
        body = {**{k: v for k, v in body.items() if k != "retry_count"}, "retry_counts": both}
    if unknown := set(body) - set(KNOBS):
        raise KnobError(sorted(unknown)[0], f"不认识的设置：{'、'.join(sorted(unknown))}")
    values = {key: _check(key, v) for key, v in body.items()}
    main_before = get("llm_provider")
    main = (values["llm_provider"] or default("llm_provider")) if "llm_provider" in values else main_before
    if not watches(main):  # the stages that watch video and had no route of their own stay where they were going
        stages = values["stage_providers"] or {} if "stage_providers" in values else get("stage_providers")
        stay = main_before if watches(main_before) else "agy"
        values["stage_providers"] = {**{s: stay for s in VIDEO_STAGES}, **stages}
    changed = []
    for key, v in values.items():
        before = get(key)
        if KNOBS[key].kind == "stages":  # a stage sent where the default goes anyway needs no entry
            v = {s: p for s, p in (v or {}).items() if resolve(p) != resolve(main)} or None
        if KNOBS[key].kind == "retries":  # nor a count the same as the default, so a later better default reaches it
            v = {e: n for e, n in (v or {}).items() if n != RETRY_DEFAULTS[e]} or None
        if KNOBS[key].kind == "topic_stages":  # nor a step sent where it goes anyway
            v = {s: p for s, p in (v or {}).items() if resolve(p) != resolve(topic_default(s))} or None
        if v is None:
            db.exec("DELETE FROM settings WHERE key=?", key)
        else:
            db.set_setting(key, v)
        now = get(key)
        if now != before and KNOBS[key].kind == "stages":  # just the stages that moved, with what they go to now
            changed += [f"{STAGE_NAMES[s]} {before.get(s, main_before)} → {now.get(s, main)}" for s in STAGES
                        if before.get(s) != now.get(s) and before.get(s, main_before) != now.get(s, main)]
        elif now != before and KNOBS[key].kind == "topic_stages":
            changed += [f"专题{TOPIC_NAMES[s]} {resolve(before.get(s) or topic_default(s))} → {resolve(now.get(s) or topic_default(s))}"
                        for s in TOPIC_STAGES if before.get(s) != now.get(s)]
        elif now != before and KNOBS[key].kind == "retries":
            changed += [f"{KNOBS[key].label}：{RETRY_NAMES[e]} {before[e]} → {now[e]} 次" for e in RETRY_DEFAULTS if before[e] != now[e]]
        elif now != before:
            changed.append(f"{KNOBS[key].label} {_show(before)} → {_show(now)}")
    if changed:
        db.event("config", "设置：" + "；".join(changed))
    return view()


def _show(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return hours_text(v) or "无"
    if isinstance(v, dict):
        return f"到 {v['until']}" if v.get("until") else "开"
    return "无" if v is None else str(v)
