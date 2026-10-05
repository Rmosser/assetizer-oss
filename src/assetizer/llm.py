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

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .providers.agy import AgyProvider, ProviderError, gemini_schema
from .util import read_json, write_json


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _reusable(accepted: Path, key: str) -> bool:
    """An accepted result is reused only for the same request (crash resume). A changed prompt or schema must
    not be answered from cache; results written before fingerprints existed keep resuming as before."""
    if not accepted.exists():
        return False
    k = accepted.with_suffix(".key")
    if not k.exists() or k.read_text().strip() == key:
        return True
    accepted.rename(accepted.with_name(f"{accepted.stem}.stale-{int(time.time())}{accepted.suffix}"))
    return False

Recorder = Callable[[dict], None]


class ContractError(RuntimeError):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind


def extract_json(text: str, required: list[str] | None = None) -> Any:
    """Parse a JSON object; tolerate prose and schema echoes (Run-009/011/014).

    Falls back to scanning every top-level object and accepts exactly one that has
    all ``required`` keys (port of Run-014 ``runtime.parse``).
    """
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        o = json.loads(s)
        if not required or (isinstance(o, dict) and all(k in o for k in required)):
            return o
    except ValueError:
        pass
    dec = json.JSONDecoder(strict=False)
    found, i = [], 0
    while i < len(s):
        if s[i] != "{":
            i += 1
            continue
        try:
            v, end = dec.raw_decode(s[i:])
        except ValueError:
            i += 1
            continue
        if isinstance(v, dict) and (not required or all(k in v for k in required)):
            found.append(v)
        i += end
    if len(found) == 1:
        return found[0]
    raise ContractError("FORMAT_NONCOMPLIANCE", f"{len(found)} candidate objects with required keys")


def is_safety_block(text: str) -> bool:
    t = (text or "")[:600].lower()
    return "blocked by gemini's filters" in t or "blocked by gemini’s filters" in t


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
        key = _fingerprint(prompt, media, effort)
        if _reusable(accepted, key):
            return accepted.read_text()
        r = self._call(d / "attempt-1", stage, prompt, kind=stage, media=media, effort=effort)
        if len(r.text.strip()) < min_chars:
            raise ContractError("CONTENT_FAILURE", f"response only {len(r.text)} chars")
        accepted.write_text(r.text)
        accepted.with_suffix(".key").write_text(key)
        return r.text

    def structured(self, call_root: Path, stage: str, prompt: str, schema: dict, *,
                   media: list[str] | None = None, effort: str | None = None,
                   check: Callable[[Any], None] | None = None, enforce: bool = False) -> Any:
        """Return validated JSON. ``check`` may raise ContractError for semantic checks.

        ``enforce=True`` uses agy ``--json-schema`` (function-call output). Only for small
        outputs: on a 50-min chunk it burned the output budget on thinking and returned
        an empty list (2026-10-05). Large stages embed the schema in the prompt — the
        Run-014 validated path — and are validated locally.

        Recovery policy:
          * deviation / empty / parseable-but-invalid → regenerate once (fresh call);
          * unparseable text → one media-free contract repair (pure reformatting);
          * repair output must pass the same validation and check, otherwise fail.
        """
        d = Path(call_root) / stage
        accepted = d / "accepted.json"
        key = _fingerprint(prompt, schema, media, effort, enforce)
        if _reusable(accepted, key):
            return read_json(accepted)
        full_prompt = prompt if enforce else (
            prompt + "\n\n输出格式：只输出一个 JSON 对象，不要 Markdown 代码块、不要任何解释文字。必须符合以下 JSON Schema：\n"
            + json.dumps(gemini_schema(schema), ensure_ascii=False))
        kw = dict(kind=stage, schema=schema if enforce else None, media=media, effort=effort)

        def parse(text: str) -> Any:
            o = _strip_extras(extract_json(text, schema.get("required")), schema)
            validate(o, schema)
            if check:
                check(o)
            return o

        last: ContractError | ProviderError | None = None
        r = None
        deviations = 0
        for n in (1, 2, 3, 4):
            if n == 4 or (n == 3 and deviations == 0):
                break
            try:
                r = self._call(d / f"attempt-{n}", stage, full_prompt, **kw)
            except ProviderError as e:
                if e.kind not in ("MODALITY_ROUTING_DEVIATION", "MEDIA_READ_COUNT"):
                    raise
                deviations += 1
                last = e
                continue
            if is_safety_block(r.text):
                # Gemini filters occasionally false-positive on trading content; a fresh call usually passes
                write_json(d / f"invalid-{n}.json", {"classification": "PROVIDER_SAFETY_BLOCK", "detail": r.text[:300]})
                last = ContractError("PROVIDER_SAFETY_BLOCK", "blocked by Gemini filters")
                deviations += 1  # allow the third attempt
                continue
            try:
                obj = parse(r.text)
                write_json(accepted, obj)
                accepted.with_suffix(".key").write_text(key)
                return obj
            except ContractError as e:
                write_json(d / f"invalid-{n}.json", {"classification": e.kind, "detail": str(e)})
                last = e
                if e.kind == "FORMAT_NONCOMPLIANCE" and len(r.text.strip().strip("}{[] \n")) >= 20:
                    break  # real content in a broken envelope → repair below
        if r is None and isinstance(last, ProviderError):
            raise last
        if r is None or not isinstance(last, ContractError) or last.kind != "FORMAT_NONCOMPLIANCE" \
                or len(r.text.strip().strip("}{[] \n")) < 20:
            raise last if last else ContractError("CONTENT_FAILURE")
        repair_prompt = (
            "禁止使用任何工具、媒体与外部知识。你的唯一任务是把下面冻结的响应转换为符合给定 JSON Schema 的对象。"
            "不得改写事实、增加观点或补充证据；原响应没有的信息用空字符串或空数组，并在可用的 uncertainties 字段记录缺失。"
            "绝不允许为了满足 Schema 编造占位内容。"
            f"\n失败原因：{last}\n\nJSON Schema：\n{json.dumps(gemini_schema(schema), ensure_ascii=False)}"
            f"\n\n冻结响应：\n{r.text}"
        )
        rr = self._call(d / "contract-repair", stage + ":repair", repair_prompt, kind="contract-repair")
        obj = parse(rr.text)
        write_json(accepted, obj)
        accepted.with_suffix(".key").write_text(key)
        return obj
