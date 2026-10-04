"""Per-run execution context shared by all stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config, db
from ..llm import LLM
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
        def record(r: dict) -> None:
            u = r.get("usage") or {}
            db.exec("""INSERT INTO model_calls(run_id, stage, attempt, conversation_id, model, agy_version, status, error_class,
                       wall_seconds, input_tokens, output_tokens, thinking_tokens, peak_rss_kib, view_files, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self.id, r.get("stage"), r.get("attempt"), r.get("conversation_id"), r.get("actual_model"),
                    r.get("agy_version"), r.get("status"), r.get("error_class"), r.get("wall_seconds"),
                    u.get("input_tokens"), u.get("output_tokens"), u.get("thinking_tokens"), r.get("peak_rss_kib"),
                    r.get("view_file_count"), now())
        return LLM(recorder=record)

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
