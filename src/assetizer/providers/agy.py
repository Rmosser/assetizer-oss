"""agy (Antigravity CLI) model provider.

Contract learned in Run-003…014:

* one call = one isolated working directory holding prompt, schema, raw
  stream, response and an ``invocation.json`` receipt;
* media calls may use exactly one tool — the media read (``MEDIA_READ``) on the
  allowed file(s); any other tool is a MODALITY_ROUTING_DEVIATION and the call
  is killed;
* prompts larger than ~120 KB go through stream-json stdin (Linux argv limit);
* every call leaves ~600 MB of conversation state under
  ``~/.gemini/antigravity-cli``; it is deleted once the receipt is on disk.

agy updates itself. ``media_health`` checks after each update that a video can
still be read the allowed way (pipeline.runner pauses production when not).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
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
# How agy reports the model reading a media file: tool name → the parameter holding the path. agy ≤ 1.2 reported
# view_file(AbsolutePath); 1.4 reports the same tool as read_file(Path) (2026-10-22: verified on a clip, the
# description matched the video). A new spelling first shows up as a deviation that names the allowed file; the
# media self-test then pauses production, and its alert says which tool and parameters to add here.
MEDIA_READ = {"view_file": "AbsolutePath", "read_file": "Path"}
LIVE_PGIDS: set[int] = set()  # process groups of in-flight agy calls (killed on worker shutdown)
LIVE_RUNS: dict[int, str] = {}  # those working for a run: the run (清空 stops a trial's call, a10)
USAGE_LOCK = threading.Lock()  # `agy -p /usage`: one at a time in a process
# after an error step at a media read, how long agy gets to say why before it is stopped
MEDIA_ERROR_GRACE = 30.0


def run_of(path: Path | str) -> str | None:
    """The run a path under the runs directory belongs to (None: no run's, e.g. a compile)."""
    try:
        return Path(path).resolve().relative_to(config.get().runs_dir.resolve()).parts[0]
    except (ValueError, IndexError):
        return None


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


# A model's safety filter or its refusal: another try may well pass, the answer being what was stopped ("blocked by content
# safety filters … violate Google's Generative AI Prohibited Use policy", 2026-11; "blocked by Gemini's filters", before).
SAFETY_RE = re.compile(r"blocked by .{0,40}filter|safety filter|prohibited[ _]use|prohibited[ _]content|content[ _]filter|"
                       r"content[ _]policy|\brefusal\b", re.I)
_TRANSIENT_RE = re.compile(r"\b(429|500|502|503|504)\b|resource_exhausted|unavailable|deadline exceeded|timed out|"
                           r"temporarily|overloaded|rate limit|connection reset|broken pipe|unexpected eof|"
                           r"connection refused|tls handshake|no such host|i/o timeout|"
                           # the box without a network: Go's resolver and dialer
                           r"network is unreachable|host is unreachable|no route to host|server misbehaving|"
                           r"temporary failure in name resolution|name or service not known|"
                           # Go's net/http when the connection drops mid-request: `Post "https://…": EOF`
                           # (2026-10-25: two runs failed at once on prodbox during a short egress outage)
                           r"\": eof\b|"
                           # agy's own result when the model stream drops; until calls ended at their result the
                           # watchdog killed these and they counted as timeouts (VigCKskxyeE-r1, 2026-10-26)
                           r"stream was interrupted", re.I)
# the weekly limit in agy's own words ("You have hit your weekly limit… refreshes in 2 days"): no outage to wait out
_QUOTA_RE = re.compile(r"weekly limit|usage limit|limit will reset|refreshes in", re.I)
_AUTH_RE = re.compile(r"unauthenticated|\b401\b|not logged in|please log ?in|sign in again|invalid_grant|"
                      r"oauth token (?:expired|invalid)|re-?authenticate", re.I)
# agy on a --model it does not have (1.4.9): "invalid model selection (…): model x is not recognized as a known model…"
_MODEL_GONE = re.compile(r"invalid model selection|not recognized as a known model", re.I)
# what a signed-out agy answers to `-p /usage` with stdin closed (1.2.16): "Error: authentication required. Run 'agy'
# to log in" (its JSON adds "authentication failed or timed out", which a timeout says too: not enough on its own)
_SIGNED_OUT = re.compile(r"authentication required|not (?:signed|logged) in|please (?:log|sign) ?in|sign in again|"
                         r"invalid_grant|unauthenticated|re-?authenticate", re.I)


_BENIGN_CMD = re.compile(r"\s*(ls(\s+-[a-zA-Z]+)*(\s+[./~\w-]+)*|pwd)\s*")


def _benign(name: str | None, params: dict) -> bool:
    """Read-only directory listings — no fidelity or safety impact. agy 1.4 models list a few directories in one
    command, one `ls` per line, after reading the media (2026-10-22); each line must be a plain `ls` or `pwd`."""
    if name == "list_dir":
        return True
    if name == "run_command":
        lines = [ln for ln in (params.get("CommandLine") or "").split("\n") if ln.strip()]
        return bool(lines) and all(_BENIGN_CMD.fullmatch(ln) for ln in lines)
    return False


def model_for(base: str, effort: str | None) -> str:
    """agy encodes effort in the model id (gemini-3.8-flash-{low,medium,high}); --effort conflicts."""
    if not effort:
        return base
    m = re.match(r"^(.*)-(low|medium|high)$", base)
    return f"{m[1]}-{effort}" if m and effort in ("low", "medium", "high") else base


def _seconds(spec: str) -> float:
    spec = spec.strip()
    mult = {"s": 1, "m": 60, "h": 3600}.get(spec[-1:], 1)
    return float(spec[:-1] if spec[-1:] in "smh" else spec) * mult


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


def _stop(pgid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def classify_error(text: str) -> str:
    t = (text or "").lower()
    if SAFETY_RE.search(t):
        return "CONTENT_SAFETY"
    if "output token limit" in t or "max_tokens" in t or "output limit" in t:
        return "PROVIDER_OUTPUT_LIMIT"
    if _MODEL_GONE.search(t):
        return "PROVIDER_MODEL"
    if "invalid_argument" in t and ("schema" in t or "function_declarations" in t):
        return "PROVIDER_SCHEMA_REJECTED"
    if "quota" in t or "exceeded your" in t or "billing" in t or _QUOTA_RE.search(t):
        return "PROVIDER_QUOTA"
    if _AUTH_RE.search(t):
        return "AGY_AUTH_REQUIRED"
    if _TRANSIENT_RE.search(t):
        return "PROVIDER_TRANSIENT"
    return "PROVIDER_FAILURE"


class AgyProvider:
    name = "agy"

    _version: tuple[float, str] | None = None  # (when looked up, version); agy updates itself, so look again

    def __init__(self, settings: config.Settings | None = None, model: str | None = None):
        self.s = settings or config.get()
        self.model = model or self.s.agy_model

    def version(self) -> str:
        if AgyProvider._version is None or time.monotonic() - AgyProvider._version[0] > 600:
            try:
                v = subprocess.run([self.s.agy_bin, "--version"], capture_output=True, text=True,
                                   timeout=30).stdout.strip() or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                v = "unknown"
            AgyProvider._version = (time.monotonic(), v)
        return AgyProvider._version[1]

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
        # this call's own input files: agy 1.4 models on long chunks list the call directory and re-read the prompt
        # (MUBB14vJc9U-r5, 2026-10-22). They hold nothing the model was not already given, so reading them is benign.
        own_inputs = {str((call_dir / n).resolve()) for n in ("prompt.md", "schema.json", "stdin-message.jsonl")}
        write_text(call_dir / "prompt.md", prompt)
        model = model_for(self.model, effort)
        cmd = [
            self.s.agy_bin, "--model", model,
            "--disable-slash-commands", "--dangerously-skip-permissions",
            "--print-timeout", timeout or self.s.agy_print_timeout,
            "--output-format", "stream-json",
        ]
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
            "provider": self.name, "kind": kind, "model_requested": model, "agy_version": self.version(),
            "started_utc": now(), "allowed_view_files": allowed, "deviation": None,
            "schema": schema is not None, "prompt_bytes": len(prompt.encode()),
        }
        peak = [0]
        done = threading.Event()
        t0 = time.monotonic()
        result: dict | None = None
        seen_tool_steps: set = set()
        media_pending: str | None = None  # a media read whose outcome (answer or error step) has not come yet
        last_error_text = ""
        grace: list[threading.Timer] = []  # stops agy after an error step at a media read
        with (call_dir / "stderr.log").open("w") as err, (call_dir / "raw-output.jsonl").open("w") as out:
            ws = call_dir / "ws"  # empty workspace: nothing for an agentic model to explore
            ws.mkdir(exist_ok=True)
            proc = subprocess.Popen(cmd, cwd=ws, stdin=stdin_file or subprocess.DEVNULL,
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

            LIVE_PGIDS.add(proc.pid)
            if rid := run_of(call_dir):
                LIVE_RUNS[proc.pid] = rid
            threading.Thread(target=monitor, daemon=True).start()
            limit = _seconds(timeout or self.s.agy_print_timeout) + 300
            last_line = [time.monotonic()]
            killed = [None]

            def watchdog() -> None:
                while not done.is_set():
                    idle = time.monotonic() - last_line[0]
                    total = time.monotonic() - t0
                    if idle > 300 or total > limit:
                        killed[0] = "NO_STREAM_EVENTS_5MIN" if idle > 300 else "WALL_TIMEOUT"
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return
                    time.sleep(5)

            threading.Thread(target=watchdog, daemon=True).start()
            assert proc.stdout is not None
            for line in proc.stdout:
                last_line[0] = time.monotonic()
                out.write(line)
                out.flush()
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if receipt.get("media_error"):  # only listening for why the read failed
                    if e.get("event") == "error":
                        last_error_text = json.dumps(e, ensure_ascii=False)
                    if e.get("event") == "result":
                        result = e.get("result") or {}
                        break
                    continue
                if e.get("event") == "init":
                    receipt["conversation_id"] = e.get("conversation_id") or e.get("init", {}).get("conversation_id")
                    receipt["actual_model"] = e.get("init", {}).get("model")
                su = e.get("step_update") or {}
                if su.get("conversation_id") and not receipt.get("conversation_id"):
                    receipt["conversation_id"] = su["conversation_id"]
                if receipt.get("conversation_id") and not (call_dir / "conversation.txt").exists():
                    (call_dir / "conversation.txt").write_text(receipt["conversation_id"])  # survives a kill
                if media_pending and su.get("step_type") not in (None, "tool"):
                    if su["step_type"] == "error_message":
                        # agy refused the media file (2026-10-22: a 26 MB proxy) or the turn failed at the read for
                        # another reason (2026-10-26: the weekly quota ran out there, and the queue paused as if
                        # agy could not read media). Give agy a moment to say which, then stop it: without the
                        # media the model only flails.
                        receipt["media_error"] = {"path": media_pending, "bytes": Path(media_pending).stat().st_size}
                        grace.append(threading.Timer(MEDIA_ERROR_GRACE, _stop, (proc.pid,)))
                        grace[-1].daemon = True
                        grace[-1].start()
                        continue
                    media_pending = None
                if su.get("step_type") == "tool" and su.get("tool_name") == "finish":
                    pass  # structured output is delivered through the finish tool
                elif su.get("step_type") == "tool":
                    name = su.get("tool_name")
                    params = (su.get("tool_info") or {}).get("parameters") or {}
                    path = params.get(MEDIA_READ[name]) if name in MEDIA_READ else None
                    ok = bool(path) and str(Path(path).resolve()) in allowed
                    if ok:
                        receipt["media_tool"] = name
                        media_pending = str(Path(path).resolve())
                    if not ok and (_benign(name, params) or (path and str(Path(path).resolve()) in own_inputs)):
                        receipt.setdefault("benign_tools", []).append({"tool": name, "parameters": params})
                        continue
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
                    # nothing follows the result, but agy 1.4.7 often stays up after it: 73 of 324 calls on
                    # prodbox idled into the 5-minute watchdog (2026-10-25), 6.9 of 16.5 call-hours
                    break
            try:
                proc.wait(timeout=60 if result is None else 5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            done.set()
            for g in grace:
                g.cancel()
            LIVE_PGIDS.discard(proc.pid)
            LIVE_RUNS.pop(proc.pid, None)
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

        if killed[0]:
            receipt["watchdog"] = killed[0]
        # what agy said on stderr counts too: the quota running out at the media read was said only there (2026-10-25)
        cause = classify_error(" ".join([str((result or {}).get("error", "")), last_error_text, stderr_tail]))
        if receipt.get("media_error") and cause != "PROVIDER_FAILURE":  # quota, auth, network: not the media
            receipt["status"] = "FAILED"
            kind_err = receipt["error_class"] = cause
            receipt["error_excerpt"] = " ".join([str((result or {}).get("error", "")), last_error_text])[-1500:]
        elif receipt.get("media_error"):
            receipt["status"] = "MEDIA_REFUSED"
            kind_err = "MEDIA_READ_FAILED"
            receipt["error_excerpt"] = f"agy refused the media file ({receipt['media_error']['bytes']} bytes)"
        elif receipt["deviation"]:
            receipt["status"] = "DEVIATION"
            kind_err = "MODALITY_ROUTING_DEVIATION"
        elif status == "SUCCESS":
            receipt["status"] = "SUCCESS"
            kind_err = None
        else:
            blob = " ".join([str((result or {}).get("error", "")), last_error_text, stderr_tail])
            kind_err = classify_error(blob)
            if killed[0] or (result is None and proc.returncode not in (0, None) and "timeout" in blob.lower()):
                kind_err = "PROVIDER_TIMEOUT"
            receipt["status"] = "FAILED"
            receipt["error_class"] = kind_err
            receipt["error_excerpt"] = blob[-1500:]
        if receipt.get("actual_model") and receipt["actual_model"] != model and kind_err is None:
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

    def usage(self) -> dict:
        """Quota left on the signed-in account: `agy -p /usage` (alias /quota), which agy ≥ 1.1.11 answers by itself —
        no turn, no quota spent (~10 s). stdin is an empty pipe on purpose: with a terminal or /dev/null there, a signed-out
        agy starts its sign-in and waits 60 s for a pasted code; with a pipe it says so and exits."""
        d = self.s.home / "health" / "agy-usage"
        d.mkdir(parents=True, exist_ok=True)
        out: dict[str, Any] = {"ok": False, "checked_at": now(), "version": self.version()}
        # one at a time per process (the web container has 1–2 GB; agy here peaks near 200 MB)
        with USAGE_LOCK:
            try:
                p = subprocess.Popen([self.s.agy_bin, "-p", "/usage", "--output-format", "json"], cwd=d, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                     env=dict(os.environ, GOGC="50", GOMEMLIMIT="384MiB"))
            except OSError as e:
                return {**out, "error": "PROVIDER_FAILURE", "detail": str(e)[:300]}
            try:
                stdout, stderr = p.communicate(input="", timeout=90)
            except subprocess.TimeoutExpired:
                _stop(p.pid, signal.SIGKILL)  # its whole group: agy and what it started
                p.communicate()
                return {**out, "error": "PROVIDER_TIMEOUT"}
        try:
            payload = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
        except ValueError:
            payload = {}
        groups = parse_usage(payload) if payload.get("status") == "SUCCESS" else []
        if not groups:
            said = " ".join([str(payload.get("error") or ""), stderr[-1500:]])
            kind = "AGY_AUTH_REQUIRED" if _SIGNED_OUT.search(said) else classify_error(said)
            return {**out, "error": kind, "detail": said.strip()[-300:]}
        return {**out, "ok": True, "groups": groups}

    def models(self) -> list[dict]:
        """The Gemini models agy offers (`agy models`, no turn, no quota spent); [] when it does not say."""
        d = self.s.home / "health" / "agy-usage"
        d.mkdir(parents=True, exist_ok=True)
        with USAGE_LOCK:
            try:
                out = subprocess.run([self.s.agy_bin, "models"], cwd=d, input="", capture_output=True, text=True, timeout=90,
                                     env=dict(os.environ, GOGC="50", GOMEMLIMIT="384MiB")).stdout
            except (OSError, subprocess.TimeoutExpired):
                return []
        return parse_models(out, self.s.agy_model)

    def media_health(self) -> dict:
        """Can agy still read a video the way the watchdog allows, at the size proxies are made to? A 10-second clip with
        a random number in its middle, padded to just under VIEW_FILE_LIMIT: the answer must be that number, so the
        model really saw the frames and agy still takes a file that large. When it cannot, the answer says how: the
        tool and parameters agy used instead (what MEDIA_READ would need), or that it refused the file's size."""
        from ..media.ffmpeg import VIEW_FILE_LIMIT
        d = self.s.home / "health" / "agy-media"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        out = {"ok": False, "version": self.version(), "model": self.model, "tool": None, "parameters": None, "error": None, "bytes": None,
               "checked_at": now()}
        code = str(random.randint(1000, 9999))
        clip = d / "canary.mp4"
        try:
            _canary_clip(clip, code, int(VIEW_FILE_LIMIT * 0.92))
        except (OSError, subprocess.SubprocessError) as e:
            return {**out, "error": "CANARY_UNAVAILABLE", "detail": type(e).__name__}
        out["bytes"] = clip.stat().st_size
        prompt = f"用 view_file 读取视频 {clip}（只读一次）。只回复画面中央的数字，不要别的文字。读完后不要再调用任何工具。"
        try:
            r = self.call(d / "call", prompt, kind="media-health", media=[str(clip)], timeout="3m")
        except ProviderError as e:
            dev = e.receipt.get("deviation") or {}
            return {**out, "error": e.kind, "tool": dev.get("tool"), "parameters": sorted(dev.get("parameters") or {})}
        right = re.sub(r"\D", "", r.text) == code
        return {**out, "ok": right, "tool": r.receipt.get("media_tool"), "error": None if right else "WRONG_ANSWER"}


def parse_models(text: str, prefer: str) -> list[dict]:
    """`agy models` (id, tab, name; one line per level) as one row per model. Each stage says its own level (model_for),
    so the page offers models, not levels: the id kept has the level of the box's model, which is what a call that says
    none runs at."""
    level = re.fullmatch(r".*-(low|medium|high)", prefer)
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != 2 or not cells[0].startswith("gemini-"):
            continue
        m = re.fullmatch(r"(.*)-(low|medium|high)", cells[0])
        if (m[1] if m else cells[0]) not in rows or (m and level and m[2] == level[1]):
            rows[m[1] if m else cells[0]] = {"id": cells[0], "name": re.sub(r"\s*\((?:low|medium|high)\)\s*$", "", cells[1], flags=re.I)}
    return list(rows.values())


def read_models() -> list[dict]:
    """Keep what agy offers (setting agy_models); what it last said stands while it says nothing."""
    from .. import db
    rows = AgyProvider().models()
    if rows:
        db.set_setting("agy_models", rows)
    return rows


def _name(model: str) -> str:
    """A name for a model agy has not listed yet: gemini-3.8-flash-high → Gemini 3.8 Flash."""
    return " ".join(w.capitalize() for w in re.sub(r"-(low|medium|high)$", "", model).split("-"))


def route_models() -> list[dict]:
    """agy's models as the page's model list has them (magpie_state.model_view), each with the group of agy's quota it
    draws on (`quota`; None until a reading places it). Until agy has been asked: the models the routes use, so a stage
    reads by its model's name and is not taken for one that was withdrawn."""
    from .. import db
    from . import AGY_PREFIX, is_agy, resolve, routes
    rows = db.setting("agy_models") or [{"id": r[len(AGY_PREFIX):], "name": _name(r[len(AGY_PREFIX):])}
                                        for r in dict.fromkeys([resolve("agy"), *routes()]) if is_agy(r)]
    groups = (db.setting("agy_quota") or {}).get("groups") or []
    return [{"id": AGY_PREFIX + m["id"], "name": m["name"], "vendor": "agy", "group": False, "watches": True,
             "quota": (quota_group(groups, m["id"]) or {}).get("name")} for m in rows]


def missing_routes() -> list[str]:
    """The routes on agy's models that agy no longer lists."""
    from .. import db
    from . import AGY_PREFIX, is_agy, routes
    listed = {AGY_PREFIX + m["id"] for m in db.setting("agy_models") or []}
    return [r for r in routes() if is_agy(r) and listed and r not in listed]


def parse_usage(payload: dict) -> list[dict]:
    """The groups of `/usage` (models that share a 5-hour and a weekly limit), each bucket as
    {window: "5h" | "weekly" | None, remaining: 0–1 | None, reset_at, disabled, note}. agy 1.2 sends them structured in
    command.data; older answers only carry the tab-separated text (group, bucket, "42%" | "disabled", reset time)."""
    def window(b: dict) -> str | None:
        w = str(b.get("window") or "").lower()
        name = str(b.get("name") or b.get("id") or "").lower()
        if w in ("5h", "weekly"):
            return w
        return "5h" if "5h" in name or "five hour" in name else "weekly" if "week" in name else (w or None)

    data = (payload.get("command") or {}).get("data") or {}
    groups = []
    for g in data.get("groups") or []:
        buckets = []
        for b in g.get("buckets") or []:
            frac = b.get("remaining_fraction")
            buckets.append({"window": window(b), "remaining": None if frac is None else max(0.0, min(1.0, float(frac))),
                            "reset_at": b.get("reset_time") or None, "disabled": bool(b.get("disabled")),
                            "note": b.get("description") or None})
        if buckets:
            groups.append({"name": g.get("name") or "", "models": g.get("description") or "", "buckets": buckets})
    if groups:
        return groups
    by: dict[str, list[dict]] = {}
    for line in str(payload.get("response") or "").splitlines():
        cells = line.split("\t")
        if len(cells) < 3:
            continue
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%\s*", cells[2])
        b = {"window": window({"name": cells[1]}), "remaining": float(m[1]) / 100 if m else None,
             "reset_at": (cells[3].strip() if len(cells) > 3 else "") or None, "disabled": cells[2].strip().lower() == "disabled",
             "note": None}
        by.setdefault(cells[0].strip(), []).append(b)
    return [{"name": k, "models": "", "buckets": v} for k, v in by.items()]


def _words(text: str) -> str:
    """The words that name a model, versions left out: "Gemini 4 Argon" → "gemini argon", gemini-3.8-flash → "gemini flash"."""
    return " ".join(w for w in (x.strip(".") for x in re.split(r"[^a-z0-9.]+", text.lower())) if w and not re.fullmatch(r"[\d.]+", w))


def _gemini(g: dict) -> bool:
    return "gemini" in (g["name"] + (g.get("models") or "")).lower()


def quota_group(groups: list[dict], model: str) -> dict | None:
    """The group of `/usage` a Gemini model draws on. agy keeps a quota per group of models and `/usage` says which: on
    agy 1.5 Flash and Pro share "Gemini Models" ("Models within this group: Gemini Flash, Gemini Pro"); 1.2.16 also had a
    "Gemini 4 Argon" of its own. The group named after the model, else the one listing its family, the longest name first
    (a -flash-lite model: "Gemini Flash Lite", then "Gemini Flash"). A model neither places (Argon on agy 1.5) is counted in
    the Gemini group named for no model, if there is just one; otherwise it has none."""
    gem = [g for g in groups if _gemini(g)]
    name = _words(re.sub(r"-(low|medium|high)$", "", model)).split()
    for n in range(len(name), 1, -1):
        said = f" {' '.join(name[:n])} "
        for part in ("name", "models"):
            if g := next((g for g in gem if said in f" {_words(g.get(part) or '')} "), None):
                return g
    plain = [g for g in gem if not set(_words(g["name"]).split()) - {"gemini", "models", "model"}]
    return plain[0] if len(plain) == 1 else None


def groups_in_use(q: dict | None = None) -> list[dict]:
    """The groups of a `/usage` reading (by default the latest kept) that the stages on agy draw on, each once, in the order
    of the routes (the default first)."""
    from .. import db
    from . import AGY_PREFIX, is_agy, routes
    groups = ((db.setting("agy_quota") or {}) if q is None else q).get("groups") or []
    out: dict[str, dict] = {}
    for r in routes():
        if is_agy(r) and (g := quota_group(groups, r[len(AGY_PREFIX):])):
            out.setdefault(g["name"], g)
    return list(out.values())


def quota_logs() -> dict[str, list]:
    """A week of each Gemini group's two windows (setting agy_quota_logs: {group: [[time, 5-hour left, weekly left], …]}),
    for the trend and 约够印 on the 机房 page. Until that is first written: the one list 0.4.35 and earlier keep
    (agy_quota_log), as the readings of the group the box's model drew on (production_group). That list is no longer
    written and keeps its form, so a version rolled back to goes on with it (an older version reads a newer database)."""
    from .. import db
    logs = db.setting("agy_quota_logs")
    if logs is not None:
        return logs
    old = db.setting("agy_quota_log")
    g = (db.setting("agy_quota") or {}).get("production_group")
    return {g: old} if g and isinstance(old, list) and old else {}


def record_usage(q: dict) -> dict:
    """Keep the latest `/usage` answer (setting agy_quota) and, when it worked, the readings of every Gemini group
    (quota_logs; a stage sent to another model has its group's trend from the start). Returned with `in_use`: the names of
    the groups the stages draw on (groups_in_use)."""
    from .. import db
    if q.get("ok"):
        cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat(timespec="seconds")
        logs = quota_logs()
        for g in filter(_gemini, q["groups"]):
            left = {b["window"]: (None if b["disabled"] else b["remaining"]) for b in g["buckets"]}
            logs[g["name"]] = (logs.get(g["name"]) or []) + [[q["checked_at"], left.get("5h"), left.get("weekly")]]
        logs = {k: [x for x in v if x[0] >= cut][-1200:] for k, v in logs.items()}
        db.set_setting("agy_quota_logs", {k: v for k, v in logs.items() if v})
        db.set_setting("agy_quota", q)
        return {**q, "in_use": [g["name"] for g in groups_in_use(q)]}
    # keep the last numbers that were read, say why this read failed
    last = db.setting("agy_quota") or {}
    db.set_setting("agy_quota", {**last, "ok": False, "error": q.get("error"), "detail": q.get("detail"),
                                 "failed_at": q.get("checked_at")} if last.get("groups") else q)
    return q


def _canary_clip(path: Path, code: str, target_bytes: int) -> None:
    """10 seconds of noise with the number in a white box, encoded CBR so the file comes out at about target_bytes."""
    font = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                             "/System/Library/Fonts/Supplemental/Arial Bold.ttf") if Path(f).exists()), None)
    if font is None:
        font = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True, timeout=30).stdout.strip()
    kbps = max(200, target_bytes * 8 // 10 // 1000 - 64)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=1280x720:r=30:d=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-vf",
                    f"noise=alls=60:allf=t,drawtext=fontfile={font}:text={code}:fontsize=160:fontcolor=black:box=1:"
                    "boxcolor=white:boxborderw=30:x=(w-text_w)/2:y=(h-text_h)/2",
                    "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-minrate", f"{kbps}k", "-maxrate", f"{kbps}k",
                    "-bufsize", f"{kbps}k", "-x264-params", "nal-hrd=cbr", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                    "-shortest", str(path)],
                   stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=120)
