"""Structured model calls: schema enforcement, local validation, bounded repair.

Policy (ADR-3):
  1. ordinary call with ``--json-schema`` (agy returns ``structured_output``);
  2. local JSON-Schema validation — a provider SUCCESS with incomplete JSON is
     still a failure (Run-013 S03);
  3. at most one same-model *contract repair* without media, which may only
     reformat the frozen response, never add evidence;
  4. transient provider errors retry with backoff (max 2), everything else
     surfaces with its classification.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .providers.agy import AgyProvider, ProviderError
from .util import read_json, write_json

Recorder = Callable[[dict], None]


class ContractError(RuntimeError):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind


def extract_json(text: str) -> Any:
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    # tolerate prose around exactly one complete object (Run-014 I001)
    dec = json.JSONDecoder(strict=False)
    found = []
    for i, c in enumerate(s):
        if c != "{":
            continue
        try:
            v, _ = dec.raw_decode(s[i:])
        except ValueError:
            continue
        if isinstance(v, dict):
            found.append(v)
            break
    if len(found) == 1:
        return found[0]
    raise ContractError("FORMAT_NONCOMPLIANCE", "no parseable JSON object")


def _strip_extras(obj: Any, schema: dict) -> Any:
    """agy's finish tool adds toolAction/toolSummary keys; drop unknown top-level keys."""
    if isinstance(obj, dict) and schema.get("type") == "object" and "properties" in schema:
        return {k: v for k, v in obj.items() if k in schema["properties"]}
    return obj


def validate(obj: Any, schema: dict) -> None:
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError as e:
        path = "/".join(str(x) for x in e.absolute_path)
        raise ContractError("SCHEMA_VIOLATION", f"{path}: {e.message[:200]}") from None


class LLM:
    def __init__(self, provider: AgyProvider | None = None, recorder: Recorder | None = None):
        self.p = provider or AgyProvider()
        self.recorder = recorder

    def _record(self, stage: str, attempt: str, receipt: dict) -> None:
        if self.recorder:
            self.recorder({"stage": stage, "attempt": attempt, **receipt})

    def _call(self, d: Path, stage: str, prompt: str, **kw):
        last: ProviderError | None = None
        for n in range(3):
            sub = d if n == 0 else d.with_name(d.name + f"-retry{n}")
            try:
                r = self.p.call(sub, prompt, **kw)
                if not r.reused:
                    self._record(stage, sub.name, r.receipt)
                return r
            except ProviderError as e:
                self._record(stage, sub.name, e.receipt or {"status": "FAILED", "error_class": e.kind})
                last = e
                if not e.transient:
                    raise
                time.sleep(20 * (n + 1))
        assert last
        raise last

    def text(self, call_root: Path, stage: str, prompt: str, *, min_chars: int = 200,
             media: list[str] | None = None, effort: str | None = None) -> str:
        d = Path(call_root) / stage
        accepted = d / "accepted.md"
        if accepted.exists():
            return accepted.read_text()
        r = self._call(d / "attempt-1", stage, prompt, kind=stage, media=media, effort=effort)
        if len(r.text.strip()) < min_chars:
            raise ContractError("CONTENT_FAILURE", f"response only {len(r.text)} chars")
        accepted.write_text(r.text)
        return r.text

    def structured(self, call_root: Path, stage: str, prompt: str, schema: dict, *,
                   media: list[str] | None = None, effort: str | None = None,
                   check: Callable[[Any], None] | None = None) -> Any:
        """Return validated JSON. ``check`` may raise ContractError for semantic checks."""
        d = Path(call_root) / stage
        accepted = d / "accepted.json"
        if accepted.exists():
            return read_json(accepted)
        r = self._call(d / "attempt-1", stage, prompt, kind=stage, schema=schema, media=media, effort=effort)
        try:
            obj = _strip_extras(extract_json(r.text), schema)
            validate(obj, schema)
            if check:
                check(obj)
        except ContractError as first:
            write_json(d / "invalid.json", {"classification": first.kind, "detail": str(first)})
            if first.kind not in {"FORMAT_NONCOMPLIANCE", "SCHEMA_VIOLATION"}:
                raise
            repair_prompt = (
                "禁止使用任何工具、媒体与外部知识。你的唯一任务是把下面冻结的响应转换为符合给定 JSON Schema 的对象。"
                "不得改写事实、增加观点或补充证据；原响应没有的信息用 null 或空数组，并在可用的 uncertainties 字段记录缺失。"
                f"\n失败原因：{first}\n\nJSON Schema：\n{json.dumps(schema, ensure_ascii=False)}"
                f"\n\n冻结响应：\n{r.text}"
            )
            rr = self._call(d / "contract-repair", stage + ":repair", repair_prompt, kind="contract-repair", schema=schema)
            obj = _strip_extras(extract_json(rr.text), schema)
            validate(obj, schema)
            if check:
                check(obj)
        write_json(accepted, obj)
        return obj
