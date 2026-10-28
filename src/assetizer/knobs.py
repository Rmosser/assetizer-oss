"""The box's settings the 机房 can change (design §R6-7): how much runs at once, which provider each stage uses, how
long media is kept, what 补录 leaves to new videos, when the digest goes out, where speech is transcribed.

Each is a row in `settings` read when it is needed, so a change applies to the next run, download, cleanup or digest
without a restart. A stored value out of range is read as the nearest allowed one; an unreadable one as the default.
The CLI's `set <键> <值>` writes the same rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import config, db

PROVIDERS = ("agy", "magpie", "tuzi")  # tuzi stays accepted for boxes still on it; the 机房 offers agy and magpie
STAGES = ("understanding", "merge", "knowledge", "article", "selection", "boundary", "review")  # providers.stage_key


@dataclass(frozen=True)
class Knob:
    label: str
    kind: str  # "int" "float" "provider" "stages" "url"
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
    "digest_hour": Knob("每日摘要（点）", "int", 0, 23, 9),
}
_URL = re.compile(r"https?://[^\s/]+(:\d+)?/?")


class KnobError(ValueError):
    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key, self.message = key, message


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
            return v if v in PROVIDERS else default(key)
        if k.kind == "stages":
            return {s: p for s, p in v.items() if s in STAGES and p in PROVIDERS} if isinstance(v, dict) else {}
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
        if v not in PROVIDERS:
            raise KnobError(key, f"{k.label}只能是 {'、'.join(PROVIDERS)}")
        return v
    if k.kind == "stages":
        if not isinstance(v, dict) or not all(s in STAGES and p in PROVIDERS for s, p in v.items()):
            raise KnobError(key, f"{k.label}的工序或模型不认识")
        return v
    if not isinstance(v, str) or not _URL.fullmatch(v.strip()):
        raise KnobError(key, f"{k.label}要像 http://192.168.1.50:8790 这样")
    return v.strip().rstrip("/") or None


def save(body: Any) -> dict:
    """Partial update {key: value | null}; every value is checked before anything is written."""
    if not isinstance(body, dict) or not body:
        raise KnobError("", "没有要修改的内容")
    if unknown := set(body) - set(KNOBS):
        raise KnobError(sorted(unknown)[0], f"不认识的设置：{'、'.join(sorted(unknown))}")
    values = {key: _check(key, v) for key, v in body.items()}
    main = (values["llm_provider"] or default("llm_provider")) if "llm_provider" in values else get("llm_provider")
    changed = []
    for key, v in values.items():
        before = get(key)
        if KNOBS[key].kind == "stages":  # a stage sent where the default goes anyway needs no entry
            v = {s: p for s, p in (v or {}).items() if p != main} or None
        if v is None:
            db.exec("DELETE FROM settings WHERE key=?", key)
        else:
            db.set_setting(key, v)
        if get(key) != before:
            changed.append(f"{KNOBS[key].label} {_show(before)} → {_show(get(key))}")
    if changed:
        db.event("config", "机房设置：" + "；".join(changed))
    return view()


def _show(v: Any) -> str:
    if isinstance(v, dict):
        return "、".join(f"{s}={p}" for s, p in v.items()) or "全部默认"
    if isinstance(v, float):
        return f"{v:g}"
    return "无" if v is None else str(v)
