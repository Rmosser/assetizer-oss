"""Model providers. The setting ``llm_provider`` (per box) picks a route: "agy" (default, Antigravity CLI on the Google
AI Pro quota, with the box's ASSETIZER_MODEL), a model `agy models` lists ("agy/gemini-4-argon-high"), "tuzi" (Gemini
through the 兔子 API gateway, pay-as-you-go), "magpie" (the gateway's default model) or a model the gateway lists, by
its id: "codex/gpt-6.1-terra", "claude/claude-sonnet-5-5", "group/<id>". What goes through magpie reads frames and a
transcript in place of video.

``stage_providers`` sends some stages elsewhere, e.g. {"knowledge": "codex/gpt-6.1-terra", "selection": "magpie"}:
the text-only stages that need accuracy rather than prose go to the ChatGPT quota, so the Gemini quota is spent on
watching video and writing the article (design §R5)."""
from __future__ import annotations


AGY_PREFIX = "agy/"
# How hard a model thinks, lowest first: magpie's models list theirs (`reasoning_levels`), Gemini through agy or 兔子 takes
# low, medium and high. A level a model does not take goes to the nearest it does (`nearest`).
LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
GEMINI_LEVELS = ("low", "medium", "high")


def nearest(level: str, have) -> str:
    """The level of `have` closest to the one asked (ties go lower); one not in LEVELS is taken as high."""
    at = LEVELS.index(level) if level in LEVELS else LEVELS.index("high")
    known = sorted((abs(LEVELS.index(x) - at), LEVELS.index(x), x) for x in have if x in LEVELS)
    return known[0][2] if known else list(have)[0]


def is_model(choice: str | None) -> bool:
    """A model id of the gateway rather than a provider's name."""
    return bool(choice) and "/" in choice and not is_agy(choice)


def is_agy(choice: str | None) -> bool:
    """One of agy's models rather than the gateway's."""
    return bool(choice) and choice.startswith(AGY_PREFIX)


def _make(choice: str):
    if choice == "tuzi":
        from .tuzi import TuziProvider
        return TuziProvider()
    if choice == "magpie" or is_model(choice):
        from .magpie import MagpieProvider
        return MagpieProvider(model=choice if is_model(choice) else None)
    from .agy import AgyProvider
    return AgyProvider(model=choice[len(AGY_PREFIX):] if is_agy(choice) else None)


def get():
    from .. import db
    return _make(db.setting("llm_provider") or "agy")


def stage_key(stage: str) -> str:
    """'knowledge-detail-03' / 'selection:repair' / 'review-check-01' → 'knowledge' / 'selection' / 'review'.
    The understanding stage calls its calls 'understand-CH01': it goes by the key the settings and the page know it by."""
    key = stage.split(":")[0].split("-")[0]
    return "understanding" if key == "understand" else key


def resolve(choice: str | None) -> str:
    """What a stored route means now: "tuzi", or a model id ("magpie" is the gateway's default model, "agy" the box's)."""
    if choice == "magpie":
        from .. import db
        from .magpie import DEFAULT_MODEL
        return db.setting("magpie_model") or DEFAULT_MODEL
    if not choice or choice == "agy":
        from .. import config
        return AGY_PREFIX + config.get().agy_model
    return choice


def for_stage(stage: str):
    from .. import db, knobs
    key = stage_key(stage)
    if key in knobs.TOPIC_STAGES:  # a topic's step: routed on its own (knobs.topic_routes)
        return _make(knobs.topic_routes()[key])
    if key == knobs.CHANNEL_DRAFT:  # a channel's 底稿 while it is tuned: likewise (knobs.channel_draft_route)
        return _make(knobs.channel_draft_route())
    choice = (db.setting("stage_providers") or {}).get(key)
    return _make(choice) if choice else get()


def safety_fallback(stage: str) -> tuple[str, object] | None:
    """(route, provider) for the last try of a call the safety filter keeps stopping (design §R6-10), or None: none is set,
    it is the model that stopped it, or the stage watches video and that model cannot. Not one of `in_use`: its quota or
    login does not pause the queue."""
    from .. import db, knobs
    route = knobs.fallback_route()
    if not route:
        return None
    key = stage_key(stage)
    if key in knobs.TOPIC_STAGES:
        own = knobs.topic_routes()[key]
    elif key == knobs.CHANNEL_DRAFT:
        own = knobs.channel_draft_route()
    else:
        own = (db.setting("stage_providers") or {}).get(key) or db.setting("llm_provider") or "agy"
    video = key in knobs.VIDEO_STAGES or key in knobs.TOPIC_VIDEO or key == knobs.CHANNEL_DRAFT
    if resolve(route) == resolve(own) or (video and not knobs.watches(resolve(route))):
        return None
    return resolve(route), _make(route)


def routes() -> list[str]:
    """The default route and every one a stage is sent to, resolved, each once."""
    from .. import db
    names = [db.setting("llm_provider") or "agy"] + [v for k, v in (db.setting("stage_providers") or {}).items() if k != "comments"]
    return list(dict.fromkeys(resolve(n) for n in names))


def in_use() -> list:
    """One provider per quota in use: a quota pause only lifts when all of them answer. Two models of one magpie
    subscription draw on one quota, so asking one is enough; a routing group may span several, so each is asked. agy
    keeps a quota per group of models, as its `/usage` groups them (agy.quota_group; on agy 1.5 all Gemini models count
    in "Gemini Models"), so one model per group is asked, and one that no reading of agy's quota places yet is asked
    for itself."""
    from .. import db
    from .agy import quota_group
    groups = (db.setting("agy_quota") or {}).get("groups") or []

    def quota(r: str) -> str:
        if is_agy(r):
            return f"agy:{g['name']}" if (g := quota_group(groups, r[len(AGY_PREFIX):])) else r
        return r if r.startswith("group/") or "/" not in r else r.split("/")[0]
    seen: dict[str, str] = {}
    for r in routes():
        seen.setdefault(quota(r), r)
    return [_make(r) for r in seen.values()]
