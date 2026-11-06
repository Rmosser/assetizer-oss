"""Model providers. The setting ``llm_provider`` (per box) picks a route: "agy" (default, Antigravity CLI on the Google
AI Pro quota), "tuzi" (Gemini through the 兔子 API gateway, pay-as-you-go), "magpie" (the gateway's default model) or
a model the gateway lists, by its id: "codex/gpt-6.1-terra", "claude/claude-sonnet-5-5", "group/<id>". What goes
through magpie reads frames and a transcript in place of video.

``stage_providers`` sends some stages elsewhere, e.g. {"knowledge": "codex/gpt-6.1-terra", "selection": "magpie"}:
the text-only stages that need accuracy rather than prose go to the ChatGPT quota, so the Gemini quota is spent on
watching video and writing the article (design §R5)."""
from __future__ import annotations


def is_model(choice: str | None) -> bool:
    """A model id of the gateway rather than a provider's name."""
    return bool(choice) and "/" in choice


def _make(choice: str):
    if choice == "tuzi":
        from .tuzi import TuziProvider
        return TuziProvider()
    if choice == "magpie" or is_model(choice):
        from .magpie import MagpieProvider
        return MagpieProvider(model=choice if is_model(choice) else None)
    from .agy import AgyProvider
    return AgyProvider()


def get():
    from .. import db
    return _make(db.setting("llm_provider") or "agy")


def stage_key(stage: str) -> str:
    """'knowledge-detail-03' / 'selection:repair' / 'review-check-01' → 'knowledge' / 'selection' / 'review'.
    The understanding stage calls its calls 'understand-CH01': it goes by the key the settings and the page know it by."""
    key = stage.split(":")[0].split("-")[0]
    return "understanding" if key == "understand" else key


def resolve(choice: str | None) -> str:
    """What a stored route means now: "agy", "tuzi" or a model id ("magpie" is the gateway's default model)."""
    if choice == "magpie":
        from .. import db
        from .magpie import DEFAULT_MODEL
        return db.setting("magpie_model") or DEFAULT_MODEL
    return choice or "agy"


def for_stage(stage: str):
    from .. import db
    choice = (db.setting("stage_providers") or {}).get(stage_key(stage))
    return _make(choice) if choice else get()


def routes() -> list[str]:
    """The default route and every one a stage is sent to, resolved, each once."""
    from .. import db
    names = [db.setting("llm_provider") or "agy"] + list((db.setting("stage_providers") or {}).values())
    return list(dict.fromkeys(resolve(n) for n in names))


def in_use() -> list:
    """One provider per subscription in use: a quota pause only lifts when all of them answer. Two models of one
    subscription draw on one quota, so asking one is enough; a routing group may span several, so each is asked."""
    seen: dict[str, str] = {}
    for r in routes():
        seen.setdefault(r if r.startswith("group/") or not is_model(r) else r.split("/")[0], r)
    return [_make(r) for r in seen.values()]
