"""Gemini through the 兔子 API gateway (api.tu-zi.com): pay-as-you-go, outside Antigravity's quotas.

Same contract as AgyProvider.call, so LLM and the stages do not change. Differences that matter:

* Gemini's native ``streamGenerateContent`` endpoint, not the OpenAI-compatible one: the gateway ignores
  ``response_format`` there, while ``responseSchema`` here is enforced (2026-10-27).
* Media go inline with the request (a 20 MB, 28-minute chunk proxy took 66 s) instead of an agy ``view_file`` read,
  so prompts that say "用 view_file 读取 …" get a note that the video is attached.
* Only the base model is priced on the gateway, so effort is ``thinkingConfig.thinkingLevel`` rather than the
  ``-high``/``-medium`` model suffix agy uses.
* Streaming, so long sections (tens of thousands of output tokens) never sit idle long enough for a proxy to drop
  them; a stall of IDLE_TIMEOUT seconds or the stage's wall budget ends the call as a timeout.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config, db
from ..util import now, read_json, write_json, write_text
from .agy import CallResult, ProviderError, _canary_clip, _seconds, classify_error, gemini_schema

BASE_URL = os.environ.get("TUZI_BASE_URL", "https://api.tu-zi.com").rstrip("/")
DEFAULT_MODEL = "gemini-3.8-flash"
IDLE_TIMEOUT = 300.0
MAX_OUTPUT_TOKENS = 65536
MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".png": "image/png", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
MEDIA_NOTE = ("\n\n【附件说明】上文要求读取的视频已作为附件随本消息提供，直接观看附件即可；"
              "你没有任何工具可调用，不要尝试读取文件路径。")


def _effort(s: config.Settings, effort: str | None) -> str:
    """agy encodes the default effort in the model id (gemini-3.8-flash-high); keep that default here."""
    if effort in ("low", "medium", "high"):
        return effort
    m = re.search(r"-(low|medium|high)$", s.agy_model)
    return m[1] if m else "high"


class TuziProvider:
    name = "tuzi"

    def __init__(self, settings: config.Settings | None = None):
        self.s = settings or config.get()
        self.model = db.setting("tuzi_model") or DEFAULT_MODEL
        # the gateway is reachable from the box directly; the container's HTTP(S)_PROXY points at a US exit
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def version(self) -> str:
        return f"tuzi:{self.model}"

    def _key(self) -> str:
        key = self.s.secret("tuzi-api-key")
        if not key:
            raise ProviderError("AGY_AUTH_REQUIRED", "secrets/tuzi-api-key is missing")
        return key

    def call(self, call_dir: Path, prompt: str, *, kind: str, schema: dict | None = None,
             media: list[str] | None = None, effort: str | None = None, timeout: str | None = None) -> CallResult:
        call_dir = Path(call_dir)
        call_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = call_dir / "invocation.json"
        if receipt_path.exists():
            r = read_json(receipt_path)
            if r.get("status") == "SUCCESS" and (call_dir / "response.txt").exists():
                return CallResult((call_dir / "response.txt").read_text(), r, reused=True)
        level = _effort(self.s, effort)
        text_prompt = prompt + (MEDIA_NOTE if media else "")
        write_text(call_dir / "prompt.md", text_prompt)
        receipt: dict = {
            "provider": self.name, "kind": kind, "model_requested": f"{self.model}@{level}", "agy_version": self.version(),
            "started_utc": now(), "allowed_view_files": sorted(str(Path(m).resolve()) for m in (media or [])),
            "deviation": None, "schema": schema is not None, "prompt_bytes": len(text_prompt.encode()),
            "view_file_count": len(media or []), "media_bytes": sum(Path(m).stat().st_size for m in (media or [])),
        }
        parts = [{"inline_data": {"mime_type": MIME.get(Path(m).suffix.lower(), "application/octet-stream"),
                                  "data": base64.b64encode(Path(m).read_bytes()).decode()}} for m in (media or [])]
        parts.append({"text": text_prompt})
        cfg: dict = {"maxOutputTokens": MAX_OUTPUT_TOKENS, "thinkingConfig": {"thinkingLevel": level}}
        if schema is not None:
            cfg.update(responseMimeType="application/json", responseSchema=gemini_schema(schema))
            write_json(call_dir / "schema.json", schema)
        body = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}).encode()
        t0 = time.monotonic()
        limit = _seconds(timeout or self.s.agy_print_timeout)
        text, usage, finish, error = "", {}, None, None
        try:
            req = urllib.request.Request(f"{BASE_URL}/v1beta/models/{self.model}:streamGenerateContent?alt=sse",
                                         data=body, method="POST",
                                         headers={"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"})
            with self.opener.open(req, timeout=IDLE_TIMEOUT) as resp, (call_dir / "raw-output.jsonl").open("w") as raw:
                for line in resp:
                    if time.monotonic() - t0 > limit:
                        error = ("PROVIDER_TIMEOUT", f"over the {limit:.0f} s budget")
                        break
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    raw.write(line[5:].strip() + "\n")
                    chunk = json.loads(line[5:])
                    for c in chunk.get("candidates") or []:
                        finish = c.get("finishReason") or finish
                        for p in (c.get("content") or {}).get("parts") or []:
                            if not p.get("thought"):
                                text += p.get("text", "")
                    usage = chunk.get("usageMetadata") or usage
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:1500]
            error = (_http_kind(e.code, detail), f"HTTP {e.code}: {detail}")
        except (socket.timeout, TimeoutError):
            error = ("PROVIDER_TIMEOUT", f"no data for {IDLE_TIMEOUT:.0f} s")
        except (urllib.error.URLError, OSError, ValueError) as e:
            error = ("PROVIDER_TRANSIENT", f"{type(e).__name__}: {e}")
        if error is None and finish is None:
            # the stream closed before Gemini said it was done (K3Wl7gO--ZY-r1, 2026-10-27: 1 KB of a selection
            # arrived, a contract repair then turned it into 9 of 29 selections): the answer is cut, call again
            error = ("PROVIDER_TRANSIENT", f"stream ended without a finish reason after {len(text)} chars")
        elif error is None and finish == "MAX_TOKENS":
            error = ("PROVIDER_OUTPUT_LIMIT", f"stopped at {MAX_OUTPUT_TOKENS} output tokens")
        elif error is None and finish not in (None, "STOP"):
            error = ("PROVIDER_FAILURE", f"finishReason {finish}")  # SAFETY, RECITATION, …: a fresh call may pass
        elif error is None and not text.strip():
            error = ("PROVIDER_TRANSIENT", "empty response")
        receipt.update(
            finished_utc=now(), wall_seconds=round(time.monotonic() - t0, 3), actual_model=self.model,
            finish_reason=finish, usage={"input_tokens": usage.get("promptTokenCount"),
                                         "output_tokens": usage.get("candidatesTokenCount"),
                                         "thinking_tokens": usage.get("thoughtsTokenCount")},
        )
        write_text(call_dir / "response.txt", text)
        if error:
            receipt.update(status="FAILED", error_class=error[0], error_excerpt=error[1][-1500:])
            write_json(receipt_path, receipt)
            raise ProviderError(error[0], error[1][:300], receipt)
        receipt["status"] = "SUCCESS"
        write_json(receipt_path, receipt)
        return CallResult(text, receipt)

    def purge_conversation(self, conversation_id: str | None) -> int:
        return 0  # nothing is kept on the gateway side

    def health(self) -> dict:
        tmp = self.s.home / "health" / "tuzi"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            r = self.call(tmp, "只回复两个字：在线", kind="health", effort="low", timeout="2m")
            return {"ok": "在线" in r.text, "model": self.version(), "wall_seconds": r.receipt.get("wall_seconds"),
                    "checked_at": now()}
        except ProviderError as e:
            return {"ok": False, "error": e.kind, "checked_at": now()}

    def media_health(self) -> dict:
        """The video path end to end: a clip near the proxy size limit with a random number in it, sent inline."""
        from ..media.ffmpeg import VIEW_FILE_LIMIT
        d = self.s.home / "health" / "tuzi-media"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        out = {"ok": False, "version": self.version(), "tool": None, "parameters": None, "error": None, "bytes": None,
               "checked_at": now()}
        code = str(random.randint(1000, 9999))
        clip = d / "canary.mp4"
        try:
            _canary_clip(clip, code, int(VIEW_FILE_LIMIT * 0.92))
        except (OSError, subprocess.SubprocessError) as e:
            return {**out, "error": "CANARY_UNAVAILABLE", "detail": type(e).__name__}
        out["bytes"] = clip.stat().st_size
        try:
            r = self.call(d / "call", "只回复画面中央的数字，不要别的文字。", kind="media-health", media=[str(clip)],
                          effort="low", timeout="3m")
        except ProviderError as e:
            return {**out, "error": e.kind}
        right = re.sub(r"\D", "", r.text) == code
        return {**out, "ok": right, "error": None if right else "WRONG_ANSWER"}


def _http_kind(code: int, detail: str) -> str:
    # New API answers a drained balance with 403 "用户额度不足" (insufficient_user_quota): check that before auth
    if code == 402 or re.search(r"insufficient|余额|额度不足|quota", detail, re.I):
        return "PROVIDER_QUOTA"  # the account balance ran out: production pauses, the health probe resumes it
    if code in (401, 403):
        return "AGY_AUTH_REQUIRED"  # bad, expired or disabled key: production pauses until it is fixed
    if code == 429 or code >= 500:
        return "PROVIDER_TRANSIENT"
    return classify_error(detail)
