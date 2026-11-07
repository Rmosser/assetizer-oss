"""The box's settings the 机房 can change (design §R6-7): how much runs at once, which provider each stage uses, how
long media is kept, what 补录 leaves to new videos, when the digest goes out, where speech is transcribed.

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

PROVIDERS = ("agy", "magpie", "tuzi")  # tuzi stays accepted for boxes still on it; the 机房 offers agy and magpie's models
MODEL_ID = re.compile(r"[\w.\-]+/[\w.\-@:+ /]{1,120}")  # a model of the gateway: codex/gpt-6.1-terra, grok/grok-4.7@500k, group/<id>
AGY_ID = re.compile(r"agy/gemini-[\w.\-]{1,100}")  # one of agy's: goes on its command line
STAGES = ("understanding", "merge", "knowledge", "article", "selection", "boundary", "review")  # providers.stage_key
STAGE_NAMES = dict(zip(STAGES, ("理解", "合并", "知识", "文章", "选片", "边界", "预审")))
VIDEO_STAGES = ("understanding", "boundary")


@dataclass(frozen=True)
class Knob:
    label: str
    kind: str  # "int" "float" "provider" "stages" "url" "hours" "burst"
    lo: float = 0
    hi: float = 0
    default: Any = None


KNOBS: dict[str, Knob] = {
    "worker_slots": Knob("同时印", "int", 1, 16, 1),
    # 7 downloads at once through the box's one egress got their TLS connections cut (acquire.DOWNLOADS)
    "download_slots": Knob("同时下载", "int", 1, 4, 2),
    "llm_provider": Knob("默认模型", "provider", default="agy"),
    "stage_providers": Knob("工序分工", "stages", default={}),
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


def watches(route: str) -> bool:
    """Whether a route can take a stage that watches video. magpie runs Claude's subscription through Claude Code, where
    images go in by paste: from about 20 in one message it ends without an answer (tried 2026-11-05), and a video takes more."""
    return not route.startswith("claude/")


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
    return KNOBS[key].default


def _clamp(k: Knob, v: Any) -> Any:
    x = float(v)
    if x != x:  # NaN
        raise ValueError
    x = min(k.hi, max(k.lo, x))
    return int(round(x)) if k.kind == "int" else x


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
        if k.kind == "hours":
            return sorted({h for h in v if isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23}) if isinstance(v, list) else []
        if k.kind == "burst":
            return _burst(v)
        return str(v).strip().rstrip("/") or None
    except (TypeError, ValueError, AttributeError):
        return default(key)


def view() -> dict:
    return {key: get(key) for key in KNOBS}


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
    if k.kind == "hours":
        if not isinstance(v, list) or not all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in v):
            raise KnobError(key, f"{k.label}要是 0 到 23 点")
        return sorted(set(v)) or None
    if k.kind == "burst":
        if not isinstance(v, dict) or set(v) != {"until"} or _burst(v) is None:
            raise KnobError(key, f"{k.label}的结束时间不对")
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
        if v is None:
            db.exec("DELETE FROM settings WHERE key=?", key)
        else:
            db.set_setting(key, v)
        now = get(key)
        if now != before and KNOBS[key].kind == "stages":  # just the stages that moved, with what they go to now
            changed += [f"{STAGE_NAMES[s]} {before.get(s, main_before)} → {now.get(s, main)}" for s in STAGES
                        if before.get(s) != now.get(s) and before.get(s, main_before) != now.get(s, main)]
        elif now != before:
            changed.append(f"{KNOBS[key].label} {_show(before)} → {_show(now)}")
    if changed:
        db.event("config", "机房设置：" + "；".join(changed))
    return view()


def _show(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return hours_text(v) or "无"
    if isinstance(v, dict):
        return f"到 {v['until']}" if v.get("until") else "开"
    return "无" if v is None else str(v)
