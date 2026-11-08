"""专题 (design §R8): one question and a batch of videos, each read for that question, then put together.

Each video is first recorded with no question in mind: the 底稿 (draft.py) — what the picture shows (the video model
on a 1 fps proxy, ten minutes at a time), what is said (whisper, its language detected) and the text on full-resolution
frames (the video model again, reading only). The 底稿 is kept by video and shared by every topic that has the video.

For the question a text model reads the 底稿, says how much the video has for it, plans where to look again and writes
the brief for the notes (write.plan). The video model answers each look from full-resolution frames, a crop or a short
clip, and a second model answers again from the same images where one model alone goes wrong: left and right, hands,
small print on objects, what the screen is doing (look.py). The text model writes the notes, lists what it could not
settle, those are looked at once more, and it revises (write.write / write.revise). Once the videos are read, it puts
the notes together by sub-question (write.synth).

A topic is not an issue: nothing of it goes to the 刊 or to Notion, and nothing of it is a run. The worker takes its
videos when it has room (run.py); the pages are under /topics (web/topics.py)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .. import config

PROMPTS = Path(__file__).with_name("prompts")
# Model calls of topics at once, by the kind of route: agy's are processes of their own, each up to ~1 GB (eight runs at
# once peaked at 9.1 GB on prodbox), and they share the box with production; magpie's are requests to one gateway.
_GATES = {"agy": threading.BoundedSemaphore(6), "magpie": threading.BoundedSemaphore(4)}


class Stopped(Exception):
    """The topic was paused or deleted, its question changed, or the video taken out of it: this read stops here."""


@contextmanager
def gate(stage: str) -> Iterator[None]:
    """Hold one of the topic's model call slots for the route `stage` goes to (knobs.topic_routes)."""
    from .. import knobs
    from ..providers import is_agy, resolve, stage_key
    route = resolve(knobs.topic_routes()[stage_key(stage)])
    with _GATES["agy" if is_agy(route) else "magpie"]:
        yield


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def fill(template: str, **values: object) -> str:
    """Placeholders in braces, replaced one by one: the prompts hold JSON examples whose braces must stay."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def root() -> Path:
    return config.get().topics_dir


def media_dir(vid: str) -> Path:
    return root() / "media" / vid


def draft_dir(vid: str) -> Path:
    return root() / "drafts" / vid


def work_dir(tid: int, rev: int, vid: str) -> Path:
    return root() / str(tid) / f"r{rev}" / vid


def synth_dir(tid: int) -> Path:
    return root() / str(tid) / "synth"
