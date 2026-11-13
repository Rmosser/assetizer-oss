"""Structured model calls: schema enforcement, local validation, bounded repair.

Policy (ADR-3):
  1. ordinary call with ``--json-schema`` (agy returns ``structured_output``);
  2. local JSON-Schema validation — a provider SUCCESS with incomplete JSON is
     still a failure (Run-013 S03);
  3. at most one same-model *contract repair* without media, which may only
     reformat the frozen response, never add evidence;
  4. each kind of error is tried again as often as the 设置 page says (knobs.retries, design §R6-10), only the call that
     failed; the safety filter then gets one try at another model, whose answer has to pass the same checks; everything
     else surfaces with its classification, and with what the call was put through (`retry` on the error).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import jsonschema

from . import knobs, providers
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


def _refuse(r: Any, kind: str) -> None:
    """An answer that was not taken is not a success to reuse when the run is tried again: that would fail the same way."""
    sub = (getattr(r, "meta", None) or {}).get("dir")
    if sub:
        write_json(sub / "invocation.json", {**r.receipt, "status": "FAILED", "error_class": kind})


Recorder = Callable[[dict], None]


def call_recorder(run_id: str) -> Recorder:
    """The same provider receipt columns for production, topics and channel comments."""
    from . import db
    from .util import now

    def record(r: dict) -> None:
        u = r.get("usage") or {}
        db.exec("""INSERT INTO model_calls(run_id, stage, attempt, conversation_id, model, agy_version, status, error_class,
                   wall_seconds, input_tokens, output_tokens, thinking_tokens, peak_rss_kib, view_files, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                run_id, r.get("stage"), r.get("attempt"), r.get("conversation_id"), r.get("actual_model"),
                r.get("agy_version"), r.get("status"), r.get("error_class"), r.get("wall_seconds"),
                u.get("input_tokens"), u.get("output_tokens"), u.get("thinking_tokens"), r.get("peak_rss_kib"),
                r.get("view_file_count"), now())
    return record


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
    """The filter's words handed back as the answer: prose where JSON was asked for. An answer may well discuss filters."""
    t = (text or "")[:600]
    return not t.lstrip().startswith(("{", "[", "```")) and bool(re.search(r"blocked by .{0,40}filter", t, re.I))


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
    def __init__(self, provider: AgyProvider | None = None, recorder: Recorder | None = None, session: str | None = None,
                 watch: Any = None, retry: bool = True):
        self.fixed = provider  # an explicit provider answers every stage (bench, tests); otherwise `stage_providers` routes
        self.p = provider or providers.get()
        self.recorder = recorder
        self.session = session
        self.watch = watch  # the run these calls are for (RunContext): told of retries, and only for a run do we change model
        self.retry = retry  # channel comments: one call, one contract regeneration, then wait for a new ask

    def _bound(self, p):
        if self.session and hasattr(p, "session"):
            p.session = self.session
        return p

    def provider(self, stage: str):
        return self._bound(self.fixed or providers.for_stage(stage))

    def _record(self, stage: str, attempt: str, receipt: dict) -> None:
        if self.recorder:
            self.recorder({"stage": stage, "attempt": attempt, **receipt})

    def _again(self, kind: str, stage: str, retry: int, of: int, fallback: str | None = None) -> bool:
        """About to try again. False: the run was asked to stop, this is where its calls end."""
        return not self.watch or self.watch.retrying({"kind": kind, "call": stage, "retry": retry, "of": of, "fallback": fallback})

    def _settled(self, stage: str, tried: dict[str, int], rescued: bool, fallback: str | None = None) -> None:
        if self.watch and (fallback or any(tried.values())):
            self.watch.settled(stage, tried, rescued, fallback)

    def _attempt(self, p, sub: Path, stage: str, prompt: str, **kw):
        """One call, recorded; the answer knows where it is kept (`meta["dir"]`). The filter's words handed back as an
        answer fail like the filter's error. A level set on the 设置 page for the stage stands over the call's own."""
        if not self.fixed and "effort" in kw and (set_ := knobs.effort(stage)):
            kw = {**kw, "effort": set_}
        try:
            r = p.call(sub, prompt, **kw)
            if is_safety_block(r.text):
                failed = {**r.receipt, "status": "FAILED", "error_class": "CONTENT_SAFETY"}
                write_json(sub / "invocation.json", failed)  # not a success to reuse when the run is tried again
                raise ProviderError("CONTENT_SAFETY", r.text[:300], failed)
        except ProviderError as e:
            self._record(stage, sub.name, e.receipt or {"status": "FAILED", "error_class": e.kind})
            raise
        if not r.reused:
            self._record(stage, sub.name, r.receipt)
        if isinstance(getattr(r, "meta", None), dict):
            r.meta["dir"] = sub
        return r

    def _call(self, d: Path, stage: str, prompt: str, accept: Callable[[str], Any] | None = None, **kw):
        """One call, tried again as the 设置 page says for what failed it. Failing in the end, the error says what the call
        was put through (`retry`, the shape of runs.retry: 试了 4 次). `accept`: the checks another model's answer must pass."""
        p = self.provider(stage)
        if not self.retry:
            return self._attempt(p, d, stage, prompt, **kw)
        tried: dict[str, int] = {}
        while True:
            n = sum(tried.values())
            try:
                r = self._attempt(p, d if n == 0 else d.with_name(f"{d.name}-retry{n}"), stage, prompt, **kw)
                self._settled(stage, tried, True)
                return r
            except ProviderError as e:
                kind, of = knobs.retry_kind(e.kind), knobs.retries(e.kind)
                if tried.get(kind, 0) < of and self._again(kind, stage, tried.get(kind, 0) + 1, of):
                    tried[kind] = tried.get(kind, 0) + 1
                    if e.transient:
                        time.sleep(20 * tried[kind])
                    continue
                r, route = self._another_model(d, stage, prompt, tried, of, accept, **kw) if kind == "CONTENT_SAFETY" else (None, None)
                self._settled(stage, tried, r is not None, route)
                if r is not None:
                    return r
                if sum(tried.values()) or route:
                    e.retry = {"kind": kind, "call": stage, "retry": sum(tried.values()), "of": of, "fallback": route}
                raise

    def _another_model(self, d: Path, stage: str, prompt: str, tried: dict[str, int], of: int,
                       accept: Callable[[str], Any] | None = None, **kw):
        """The safety filter kept stopping this call: one try at another model, for the answer and the model tried. The
        answer counts only once `accept` takes it. That model's failure is no news (its quota or login must not pause the
        queue, nor its video frames fail the run, over a call the first model refused anyway): the block stands."""
        other = None if self.fixed or not self.watch else providers.safety_fallback(stage)
        if not other or not self._again("CONTENT_SAFETY", stage, tried.get("CONTENT_SAFETY", 0), of, other[0]):
            return None, None
        sub = d.with_name(f"{d.name}-fallback")
        try:
            r = self._attempt(self._bound(other[1]), sub, stage, prompt, **kw)
            if accept:
                try:
                    accept(r.text)
                except ContractError as e:
                    _refuse(r, e.kind)
                    raise
            return r, other[0]
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:  # the provider's, the checks', the frames'
            if not isinstance(e, ProviderError):
                self.watch.event(f"换 {other[0]} 没有写成：{str(e)[:300]}", "warn")
            return None, other[0]

    def text(self, call_root: Path, stage: str, prompt: str, *, min_chars: int = 200,
             media: list[str] | None = None, effort: str | None = None, quiet: float | None = None) -> str:
        """`quiet`: how long the call may go without a word before agy's watchdog takes it for hung (agy.QUIET)."""
        d = Path(call_root) / stage
        accepted = d / "accepted.md"
        key = _fingerprint(prompt, media, effort)
        if _reusable(accepted, key):
            return accepted.read_text()

        def enough(text: str) -> None:
            if len(text.strip()) < min_chars:
                raise ContractError("CONTENT_FAILURE", f"response only {len(text)} chars")
        r = self._call(d / "attempt-1", stage, prompt, accept=enough, kind=stage, media=media, effort=effort,
                       **({"quiet": quiet} if quiet else {}))
        try:
            enough(r.text)
        except ContractError as e:
            _refuse(r, e.kind)
            raise
        accepted.write_text(r.text)
        accepted.with_suffix(".key").write_text(key)
        return r.text

    def structured(self, call_root: Path, stage: str, prompt: str, schema: dict, *,
                   media: list[str] | None = None, effort: str | None = None,
                   check: Callable[[Any], None] | None = None, normalize: Callable[[Any], Any] | None = None,
                   enforce: bool = False) -> Any:
        """Return validated JSON. ``check`` may raise ContractError for semantic checks; ``normalize`` may
        mend a near-miss the stage can fix without the model, before the schema is validated.

        ``enforce=True`` uses agy ``--json-schema`` (function-call output). Only for small
        outputs: on a 50-min chunk it burned the output budget on thinking and returned
        an empty list (2026-10-05). Large stages embed the schema in the prompt — the
        Run-014 validated path — and are validated locally.

        Recovery policy:
          * a provider's error → `_call` tries that call again as the 设置 page says;
          * empty / parseable-but-invalid → regenerate with the reason (CONTRACT, once by default);
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
            if normalize:
                o = normalize(o)
            validate(o, schema)
            if check:
                check(o)
            return o

        attempts_again = knobs.retries("CONTRACT") if self.retry else 1
        n = 0
        feedback = ""  # why the previous answer was rejected: the same prompt again got the same answer (2026-10-27)

        def given_up(e: ContractError) -> ContractError:
            self._settled(stage, {"CONTRACT": n - 1}, False)
            if n > 1:
                e.retry = {"kind": "CONTRACT", "call": stage, "retry": n - 1, "of": attempts_again, "fallback": None}
            return e

        while True:
            n += 1
            try:
                r = self._call(d / f"attempt-{n}", stage, full_prompt + feedback, accept=parse, **kw)
            except ProviderError:
                self._settled(stage, {"CONTRACT": n - 1}, False)  # the answers rewritten so far count, the call failing after
                raise
            try:
                obj = parse(r.text)
                write_json(accepted, obj)
                accepted.with_suffix(".key").write_text(key)
                self._settled(stage, {"CONTRACT": n - 1}, True)
                return obj
            except ContractError as e:
                _refuse(r, e.kind)
                write_json(d / f"invalid-{n}.json", {"classification": e.kind, "detail": str(e)})
                last = e
                if e.kind == "FORMAT_NONCOMPLIANCE" and len(r.text.strip().strip("}{[] \n")) >= 20:
                    if not self.retry and n > 1:
                        raise given_up(e)  # regeneration already spent the single extra request
                    break  # real content in a broken envelope → repair below; not a try of its own
                if n > attempts_again or not self._again("CONTRACT", stage, n, attempts_again):
                    raise given_up(e)
                feedback = f"\n\n【上一次的输出没有通过校验】{e}。请重新完整输出，不要遗漏任何要求的条目。"
        repair_prompt = (
            "禁止使用任何工具、媒体与外部知识。你的唯一任务是把下面冻结的响应转换为符合给定 JSON Schema 的对象。"
            "不得改写事实、增加观点或补充证据；原响应没有的信息用空字符串或空数组，并在可用的 uncertainties 字段记录缺失。"
            "绝不允许为了满足 Schema 编造占位内容。"
            f"\n失败原因：{last}\n\nJSON Schema：\n{json.dumps(gemini_schema(schema), ensure_ascii=False)}"
            f"\n\n冻结响应：\n{r.text}"
        )
        try:
            rr = self._call(d / "contract-repair", stage + ":repair", repair_prompt, kind="contract-repair")
        except ProviderError:
            self._settled(stage, {"CONTRACT": n - 1}, False)
            raise
        try:
            obj = parse(rr.text)
        except ContractError as e:
            _refuse(rr, e.kind)
            raise given_up(e)
        write_json(accepted, obj)
        accepted.with_suffix(".key").write_text(key)
        self._settled(stage, {"CONTRACT": n - 1}, True)
        return obj
