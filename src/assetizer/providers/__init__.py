"""Model providers. The setting ``llm_provider`` (per box) picks one: "agy" (default, Antigravity CLI on the Google
AI Pro quota) "tuzi" (Gemini through the 兔子 API gateway, pay-as-you-go) or "magpie" (GPT-6.1 Terra on the ChatGPT
subscription through magpie, reading frames and a transcript in place of video).

``stage_providers`` sends some stages elsewhere, e.g. {"knowledge": "magpie", "review": "magpie", "selection": "magpie"}:
the text-only stages that need accuracy rather than prose go to the ChatGPT quota, so the Gemini quota is spent on
watching video and writing the article (design §R5)."""
from __future__ import annotations


def _make(choice: str):
    if choice == "tuzi":
        from .tuzi import TuziProvider
        return TuziProvider()
    if choice == "magpie":
        from .magpie import MagpieProvider
        return MagpieProvider()
    from .agy import AgyProvider
    return AgyProvider()


def get():
    from .. import db
    return _make(db.setting("llm_provider") or "agy")


def stage_key(stage: str) -> str:
    """'knowledge-detail-03' / 'selection:repair' / 'review-check-01' → 'knowledge' / 'selection' / 'review'."""
    return stage.split(":")[0].split("-")[0]


def for_stage(stage: str):
    from .. import db
    choice = (db.setting("stage_providers") or {}).get(stage_key(stage))
    return _make(choice) if choice else get()


def in_use() -> list:
    """The default provider and every one a stage is routed to: a quota pause only lifts when all of them answer."""
    from .. import db
    names = [db.setting("llm_provider") or "agy"] + list((db.setting("stage_providers") or {}).values())
    return [_make(n) for n in dict.fromkeys(names)]
