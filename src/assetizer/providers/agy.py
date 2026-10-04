"""agy (Antigravity CLI) model provider.

Contract learned in Run-003…014:

* one call = one isolated working directory holding prompt, schema, raw
  stream, response and an ``invocation.json`` receipt;
* media calls may use exactly one tool — ``view_file`` on the allowed file(s);
  any other tool is a MODALITY_ROUTING_DEVIATION and the call is killed;
* prompts larger than ~120 KB go through stream-json stdin (Linux argv limit);
* every call leaves ~600 MB of conversation state under
  ``~/.gemini/antigravity-cli``; it is deleted once the receipt is on disk.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..util import now, read_json, write_json, write_text

ARGV_LIMIT = 120_000


class ProviderError(RuntimeError):
    """Raised with a stable classification string as ``kind``."""

    def __init__(self, kind: str, message: str = "", receipt: dict | None = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.receipt = receipt or {}

    @property
    def transient(self) -> bool:
        return self.kind in {"PROVIDER_TRANSIENT", "PROVIDER_TIMEOUT"}


@dataclass
class CallResult:
    text: str
    receipt: dict
    reused: bool = False
    meta: dict = field(default_factory=dict)


_TRANSIENT_MARKERS = ("429", "503", "500", "502", "504", "unavailable",
                      "deadline", "timed out", "timeout", "temporarily", "overloaded", "rate limit")
_AUTH_MARKERS = ("unauthenticated", "401", "login", "sign in", "oauth", "credential", "not logged")


def gemini_schema(node: Any) -> Any:
    """Down-convert JSON Schema to the subset Gemini function declarations accept.

    Gemini rejects ``null`` in enums and multi-type arrays; local validation still
    uses the full schema, so nullable fields simply arrive as strings/absent.
    """
    if isinstance(node, list):
        return [gemini_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "type" and isinstance(v, list):
            types = [t for t in v if t != "null"]
            out[k] = types[0] if types else "string"
        elif k == "enum" and isinstance(v, list):
            out[k] = [x for x in v if x is not None]
        elif k in ("pattern", "minItems", "maxItems", "minimum", "maximum"):
            continue  # enforced locally; keeps the declaration portable
        else:
            out[k] = gemini_schema(v)
    return out


def classify_error(text: str) -> str:
    t = (text or "").lower()
    if "output token limit" in t or "max_tokens" in t or "output limit" in t:
        return "PROVIDER_OUTPUT_LIMIT"
    if "invalid_argument" in t and ("schema" in t or "function_declarations" in t):
        return "PROVIDER_SCHEMA_REJECTED"
    if "quota" in t or "exceeded your" in t or "billing" in t:
        return "PROVIDER_QUOTA"
    if any(m in t for m in _AUTH_MARKERS):
        return "AGY_AUTH_REQUIRED"
    if any(m in t for m in _TRANSIENT_MARKERS):
        return "PROVIDER_TRANSIENT"
    return "PROVIDER_FAILURE"


class AgyProvider:
    name = "agy"

    _version: str | None = None

    def __init__(self, settings: config.Settings | None = None):
        self.s = settings or config.get()

    def version(self) -> str:
        if AgyProvider._version is None:
            try:
                AgyProvider._version = subprocess.run([self.s.agy_bin, "--version"], capture_output=True, text=True,
                                                      timeout=30).stdout.strip() or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                AgyProvider._version = "unknown"
        return AgyProvider._version

    # ------------------------------------------------------------------ call
    def call(
        self,
        call_dir: Path,
        prompt: str,
        *,
        kind: str,
        schema: dict | None = None,
        media: list[str] | None = None,
        effort: str | None = None,
        timeout: str | None = None,
    ) -> CallResult:
        call_dir = Path(call_dir)
        call_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = call_dir / "invocation.json"
        if receipt_path.exists():
            r = read_json(receipt_path)
            if r.get("status") == "SUCCESS" and (call_dir / "response.txt").exists():
                return CallResult((call_dir / "response.txt").read_text(), r, reused=True)
        allowed = sorted(str(Path(m).resolve()) for m in (media or []))
        write_text(call_dir / "prompt.md", prompt)
        cmd = [
            self.s.agy_bin, "--model", self.s.agy_model,
            "--disable-slash-commands", "--dangerously-skip-permissions",
            "--print-timeout", timeout or self.s.agy_print_timeout,
            "--output-format", "stream-json",
        ]
        if effort:
            cmd += ["--effort", effort]
        if schema is not None:
            write_json(call_dir / "schema.json", gemini_schema(schema))
            cmd += ["--json-schema", str((call_dir / "schema.json").resolve())]
        for d in sorted({str(Path(a).parent) for a in allowed}):
            cmd += ["--add-dir", d]
        stdin_file = None
        if len(prompt.encode()) > ARGV_LIMIT:
            msg = {"event": "user", "message": {"role": "user", "content": prompt}}
            write_text(call_dir / "stdin-message.jsonl", json.dumps(msg, ensure_ascii=False) + "\n")
            cmd += ["--input-format", "stream-json"]
            stdin_file = (call_dir / "stdin-message.jsonl").open()
        else:
            cmd += ["-p", prompt]

        env = dict(os.environ, GOGC="20", GOMEMLIMIT="6GiB")
        receipt: dict[str, Any] = {
            "provider": self.name, "kind": kind, "model_requested": self.s.agy_model, "agy_version": self.version(),
            "started_utc": now(), "allowed_view_files": allowed, "deviation": None,
            "schema": schema is not None, "prompt_bytes": len(prompt.encode()),
        }
        peak = [0]
        done = threading.Event()
        t0 = time.monotonic()
        result: dict | None = None
        seen_tool_steps: set = set()
        last_error_text = ""
        with (call_dir / "stderr.log").open("w") as err, (call_dir / "raw-output.jsonl").open("w") as out:
            proc = subprocess.Popen(cmd, cwd=call_dir, stdin=stdin_file or subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=err, text=True,
                                    start_new_session=True, env=env)

            def monitor() -> None:
                status = Path(f"/proc/{proc.pid}/status")
                while not done.is_set():
                    try:
                        for line in status.read_text().splitlines():
                            if line.startswith("VmHWM:"):
                                peak[0] = max(peak[0], int(line.split()[1]))
                    except Exception:
                        pass
                    time.sleep(0.25)

            threading.Thread(target=monitor, daemon=True).start()
            assert proc.stdout is not None
            for line in proc.stdout:
                out.write(line)
                out.flush()
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("event") == "init":
                    receipt["conversation_id"] = e.get("conversation_id") or e.get("init", {}).get("conversation_id")
                    receipt["actual_model"] = e.get("init", {}).get("model")
                su = e.get("step_update") or {}
                if su.get("conversation_id") and not receipt.get("conversation_id"):
                    receipt["conversation_id"] = su["conversation_id"]
                if su.get("step_type") == "tool" and su.get("tool_name") == "finish" and schema is not None:
                    pass  # structured output is delivered through the finish tool
                elif su.get("step_type") == "tool":
                    name = su.get("tool_name")
                    params = (su.get("tool_info") or {}).get("parameters") or {}
                    path = params.get("AbsolutePath")
                    ok = name == "view_file" and path and str(Path(path).resolve()) in allowed
                    if not ok:
                        receipt["deviation"] = {"label": "MODALITY_ROUTING_DEVIATION", "tool": name, "parameters": params}
                        os.killpg(proc.pid, signal.SIGTERM)
                        break
                    key = su.get("step_index")
                    if key not in seen_tool_steps and len(seen_tool_steps) >= len(allowed):
                        receipt["deviation"] = {"label": "REPEATED_MEDIA_READ", "tool": name}
                        os.killpg(proc.pid, signal.SIGTERM)
                        break
                    seen_tool_steps.add(key)
                if e.get("event") == "error":
                    last_error_text = json.dumps(e, ensure_ascii=False)
                if e.get("event") == "result":
                    result = e.get("result") or {}
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            done.set()
        if stdin_file:
            stdin_file.close()

        receipt.update(
            finished_utc=now(), wall_seconds=round(time.monotonic() - t0, 3),
            peak_rss_kib=peak[0], exit_code=proc.returncode, view_file_count=len(seen_tool_steps),
        )
        text = ""
        if result is not None:
            text = result.get("response") or ""
            if schema is not None and isinstance(result.get("structured_output"), dict):
                text = json.dumps(result["structured_output"], ensure_ascii=False)
            receipt["result"] = {k: v for k, v in result.items() if k != "response"}
            receipt["usage"] = result.get("usage") or {}
            receipt.setdefault("conversation_id", result.get("conversation_id"))
            write_text(call_dir / "response.txt", text)
        status = (result or {}).get("status")
        stderr_tail = (call_dir / "stderr.log").read_text()[-4000:]

        if receipt["deviation"]:
            receipt["status"] = "DEVIATION"
            kind_err = "MODALITY_ROUTING_DEVIATION"
        elif status == "SUCCESS":
            receipt["status"] = "SUCCESS"
            kind_err = None
        else:
            blob = " ".join([str((result or {}).get("error", "")), last_error_text, stderr_tail])
            kind_err = classify_error(blob)
            if result is None and proc.returncode not in (0, None) and "timeout" in blob.lower():
                kind_err = "PROVIDER_TIMEOUT"
            receipt["status"] = "FAILED"
            receipt["error_class"] = kind_err
            receipt["error_excerpt"] = blob[-1500:]
        if receipt.get("actual_model") and receipt["actual_model"] != self.s.agy_model and kind_err is None:
            receipt["status"] = "FAILED"
            kind_err = receipt["error_class"] = "MODEL_MISMATCH"
        if allowed and kind_err is None and receipt["view_file_count"] != len(allowed):
            receipt["status"] = "FAILED"
            kind_err = receipt["error_class"] = "MEDIA_READ_COUNT"
        write_json(receipt_path, receipt)
        self.purge_conversation(receipt.get("conversation_id"))
        if kind_err:
            raise ProviderError(kind_err, receipt.get("error_excerpt", "")[:300], receipt)
        return CallResult(text, receipt)

    # ---------------------------------------------------------------- hygiene
    def purge_conversation(self, conversation_id: str | None) -> int:
        """Delete agy's on-disk state for one finished conversation.

        Returns bytes freed. Never touches the OAuth token or global files.
        """
        if not conversation_id or len(conversation_id) < 16 or "/" in conversation_id:
            return 0
        root = self.s.agy_state_dir
        freed = 0
        for sub in ("conversations", "brain", "annotations", "presence", "scratch"):
            d = root / sub
            if not d.is_dir():
                continue
            for p in d.glob(f"*{conversation_id}*"):
                try:
                    if p.is_dir():
                        freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        freed += p.stat().st_size
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
        return freed

    def health(self) -> dict:
        """Cheap liveness probe — one tiny prompt, no media."""
        tmp = self.s.home / "health" / "agy"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            r = self.call(tmp, "只回复两个字：在线", kind="health", timeout="2m")
            return {"ok": "在线" in r.text, "model": r.receipt.get("actual_model"),
                    "wall_seconds": r.receipt.get("wall_seconds"), "checked_at": now()}
        except ProviderError as e:
            return {"ok": False, "error": e.kind, "checked_at": now()}
