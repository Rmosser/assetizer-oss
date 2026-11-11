"""Per-run execution context shared by all stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config, db, knobs
from ..llm import LLM, call_recorder
from ..providers import stage_key
from ..util import now, read_json, write_json


class StageWarning(Exception):
    """Non-blocking stage problem: recorded, production continues."""


class Waiting(Exception):
    """Run must pause for an external condition (auth, disk, playbook)."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


@dataclass
class RunContext:
    run: dict
    source: dict
    playbook: dict = field(default_factory=dict)
    playbook_row: dict | None = None

    @property
    def id(self) -> str:
        return self.run["id"]

    @property
    def dir(self) -> Path:
        d = config.get().runs_dir / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def calls(self) -> Path:
        return self.dir / "calls"

    @property
    def media(self) -> Path:
        return Path(self.source["media_path"])

    @property
    def duration(self) -> float:
        return float(self.source["duration"])

    def reload_source(self) -> None:
        self.source = db.one("SELECT * FROM sources WHERE id=?", self.source["id"])

    def llm(self) -> LLM:
        return LLM(recorder=call_recorder(self.id), session=self.id, watch=self)

    # the retries of its model calls (llm.LLM, design §R6-10) ------------------
    def retrying(self, state: dict) -> bool:
        """A call is about to be tried again: the pages say so while it lasts. False if the run was asked to stop."""
        db.exec("UPDATE runs SET retry=? WHERE id=?", db.j(state), self.id)
        r = db.one("SELECT stop_at FROM runs WHERE id=?", self.id)
        return not (r and r["stop_at"])

    def settled(self, call: str, tried: dict[str, int], rescued: bool, fallback: str | None = None) -> None:
        """A call that was tried again came to its end, its answer taken or not. What it came to is counted, for the 设置
        page, and the pages stop saying it is being tried: a stage may go on without it, and a run that fails of it has
        what was tried from the error (runner._fail: 试了 4 次). An answer from another model says so in the issue."""
        counts = {k: n for k, n in tried.items() if n}
        if fallback:
            counts.setdefault("CONTENT_SAFETY", 0)
        for kind, n in counts.items():
            db.exec("INSERT INTO retries(run_id, call, kind, tries, rescued, fallback, created_at) VALUES (?,?,?,?,?,?,?)",
                    self.id, call, kind, n, int(rescued), fallback if kind == "CONTENT_SAFETY" else None, now())
        db.exec("UPDATE runs SET retry=NULL WHERE id=? AND json_extract(retry, '$.call')=?", self.id, call)
        if rescued and fallback:
            step = knobs.STAGE_NAMES.get(stage_key(call), call)
            self.flag("RUN", None, "SAFETY_FALLBACK", "INFO", f"{step}的一段被内容安全过滤拦下，换 {fallback.split('/')[-1]} 写成")

    # artifacts --------------------------------------------------------------
    def save(self, rel: str, obj: Any) -> Path:
        p = self.path(rel)
        write_json(p, obj)
        return p

    def load(self, rel: str, default: Any = None) -> Any:
        p = self.dir / rel
        return read_json(p) if p.exists() else default

    def flag(self, target_kind: str, target_id: str | None, kind: str, severity: str, title: str,
             body: str = "", evidence: Any = None, origin: str = "MACHINE_QA") -> None:
        # one open flag per (target, kind, title) per run
        if db.one("SELECT id FROM flags WHERE run_id=? AND target_id IS ? AND kind=? AND title=? AND resolution='OPEN'",
                  self.id, target_id, kind, title):
            return
        db.flag(self.source["id"], self.id, target_kind, target_id, kind, severity, title, body, evidence, origin)

    def event(self, message: str, level: str = "info", kind: str = "stage", data: Any = None) -> None:
        db.event(kind, message, self.id, level, data)
