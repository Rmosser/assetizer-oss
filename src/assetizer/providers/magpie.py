"""An image model in place of the video model: any model magpie lists (GPT-6.1 Terra by default), through magpie, the
subscription gateway on prodbox. What the gateway says about its subscriptions and models is in magpie_state.

These models read images and text, not video or audio, so a video handed to ``call`` is turned into what a viewer would
need instead: frames taken when the picture changes (and at least every MAX_GAP seconds), each labelled with its time
in that video, plus the speech of the same video transcribed by whisper (the GPU box when ``asr_url`` is set), with
times. The stages and prompts stay as they are; their timecodes keep meaning "time in this video".

Same contract as AgyProvider.call. Requests go to magpie's OpenAI Responses endpoint, streamed; the gateway translates
for the vendors that do not speak it. Codex's ``codex.rate_limits`` event is kept in the receipt so the subscription's
headroom shows in every call, and the reply's ``X-Magpie-Model`` says who really answered (a routing group picks).
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, db
from ..util import fmt_tc, now, read_json, write_json, write_text
from .agy import SAFETY_RE, CallResult, ProviderError, _canary_clip, _seconds, gemini_schema
from . import LEVELS, nearest
from .tuzi import IDLE_TIMEOUT, _effort, _http_kind  # noqa: F401

DEFAULT_URL = "https://magpie.prodbox.heiyu.space"
DEFAULT_MODEL = "codex/gpt-6.1-terra"
USER_AGENT = "assetizer"  # magpie names the caller by this on its Usage page
MIN_GAP, MAX_GAP = 3.0, 15.0  # seconds between frames: on a change at most this often, and at least this often
SCENE = 0.06                  # ffmpeg scene score that counts as a change (charts redraw a small part of the frame)
MAX_FRAMES = 300              # ~1,100 tokens each at 1280 wide: stays well inside a 500K context
FRAME_TOKENS, FRAME_SHARE = 1100, 0.6  # a smaller context gets fewer frames, at most this share of it
FRAME_BYTES = 36 * 1024 * 1024  # of frames in one call, about 48 MB once in base64: Codex takes 52 MB and refuses 71 (tried 2026-11-05)
FRAME_WIDTH = 1280
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MEDIA_NOTE = ("\n\n【视频的给法】上文要求读取的视频没有直接给你，而是换成了下面两样：按画面变化截取的帧（每帧前标注它在本视频中的时间），"
              "以及同一段的语音转写（同样是本视频中的时间）。把它们当作那段视频来理解：画面看帧，说话看转写。"
              "时间码一律用这两样里给出的本视频时间，不要自己推算。你没有任何工具可调用。")


def _media_id(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]


def frames(video: Path, out: Path) -> list[tuple[float, Path]]:
    """Frames at picture changes, at least every MAX_GAP s and at most every MIN_GAP s, scaled to FRAME_WIDTH."""
    done = out / "frames.json"
    if done.exists():
        return [(t, out / n) for t, n in read_json(done)]
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    # unquoted, with escaped commas: inside '…' ffmpeg would hand the backslashes to the expression parser
    pick = (f"isnan(prev_selected_t)+gte(t-prev_selected_t\\,{MAX_GAP})"
            f"+gt(scene\\,{SCENE})*gte(t-prev_selected_t\\,{MIN_GAP})")
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-an",
                        "-vf", f"select={pick},scale='min({FRAME_WIDTH},iw)':-2,showinfo", "-fps_mode", "vfr",
                        "-q:v", "3", str(out / "f%05d.jpg")], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise ProviderError("MEDIA_READ_FAILED", f"frame extraction failed: {r.stderr[-300:]}")
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    files = sorted(out.glob("f*.jpg"))
    picked = list(zip(times, (f.name for f in files)))
    if len(picked) > MAX_FRAMES:  # a very busy video: keep an even spread
        step = len(picked) / MAX_FRAMES
        picked = [picked[int(i * step)] for i in range(MAX_FRAMES)]
    write_json(done, picked)
    return [(t, out / n) for t, n in picked]


def transcript(video: Path, out: Path) -> list[dict]:
    """The video's speech, in its own time, simplified Chinese."""
    done = out / "transcript.json"
    if done.exists():
        return read_json(done)
    from ..media import asr, ffmpeg
    out.mkdir(parents=True, exist_ok=True)
    wav = ffmpeg.extract_wav(video, out / "audio.wav")
    try:
        r = asr.transcribe(wav)
    finally:
        wav.unlink(missing_ok=True)
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in r["segments"] if s["text"].strip()]
    try:
        import opencc
        cc = opencc.OpenCC("t2s")
        for s in segs:
            s["text"] = cc.convert(s["text"])
    except ImportError:
        pass
    write_json(done, segs)
    return segs


def spread(items: list, most: int) -> list:
    """At most `most` of them, evenly spread."""
    if len(items) <= most:
        return items
    step = len(items) / most
    return [items[int(i * step)] for i in range(most)]


def within(shots: list[tuple[float, Path]], budget: int) -> list[tuple[float, Path]]:
    """As many as `budget` bytes hold, evenly spread: a busy video's frames weigh more."""
    while (total := sum(f.stat().st_size for _, f in shots)) > budget:
        shots = spread(shots, int(len(shots) * budget / total))
    return shots


class MagpieProvider:
    name = "magpie"
    media_size_limit = None  # frames are taken from the proxy, so proxies need not fit a model's file size limit
    session: str | None = None  # the run this call belongs to: magpie's Usage page tells it as a session

    def __init__(self, settings: config.Settings | None = None, model: str | None = None):
        self.s = settings or config.get()
        self.model = model or db.setting("magpie_model") or DEFAULT_MODEL
        self.url = (db.setting("magpie_url") or DEFAULT_URL).rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # magpie is on the same box

    def _catalog_entry(self) -> dict:
        return next((m for m in (db.setting("magpie_state") or {}).get("models") or [] if m.get("id") == self.model), {})

    def _frame_cap(self) -> int:
        context = self._catalog_entry().get("context")
        return min(MAX_FRAMES, max(30, int(context * FRAME_SHARE / FRAME_TOKENS))) if context else MAX_FRAMES

    def _level(self, level: str) -> str | None:
        """The level this model takes: the one asked, else the nearest it has; None for one that does not think.
        A model the catalog does not (yet) tell of is asked as asked."""
        e = self._catalog_entry()
        if "levels" not in e:
            return level
        have = e["levels"]
        if not have:
            return None
        return level if level in have else nearest(level, have)

    def version(self) -> str:
        return f"magpie:{self.model}"

    def _key(self) -> str:
        key = self.s.secret("magpie-api-key")
        if not key:
            raise ProviderError("AGY_AUTH_REQUIRED", "secrets/magpie-api-key is missing")
        return key

    def _media_parts(self, media: list[str]) -> tuple[list[dict], dict]:
        """A video becomes its frames and transcript; an image goes as it is, labelled 图 1, 图 2… in the order given (the
        prompt names them so: topics.look's second look)."""
        parts, info = [], {"frames": 0, "transcript_segments": 0}
        videos = [m for m in media if Path(m).suffix.lower() not in IMAGE_TYPES]
        n = 0
        for m in media:
            v = Path(m)
            if mime := IMAGE_TYPES.get(v.suffix.lower()):
                n += 1
                parts.append({"type": "input_text", "text": f"图 {n}"})
                parts.append({"type": "input_image", "detail": "high",
                              "image_url": f"data:{mime};base64," + base64.b64encode(v.read_bytes()).decode()})
                info["images"] = n
                continue
            cache = self.s.home / "cache" / "magpie-media" / _media_id(v)
            shots = within(spread(frames(v, cache / "frames"), self._frame_cap()), FRAME_BYTES // len(videos))
            speech = transcript(v, cache)
            for t, f in shots:
                parts.append({"type": "input_text", "text": f"画面 [{fmt_tc(t)}]"})
                parts.append({"type": "input_image", "detail": "high",
                              "image_url": "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()})
            lines = "\n".join(f"[{fmt_tc(s['start'])}–{fmt_tc(s['end'])}] {s['text']}" for s in speech)
            parts.append({"type": "input_text", "text": "语音转写（本视频中的时间）：\n" + (lines or "（没有可辨认的语音）")})
            info["frames"] += len(shots)
            info["transcript_segments"] += len(speech)
        return parts, info

    def call(self, call_dir: Path, prompt: str, *, kind: str, schema: dict | None = None,
             media: list[str] | None = None, effort: str | None = None, timeout: str | None = None) -> CallResult:
        call_dir = Path(call_dir)
        call_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = call_dir / "invocation.json"
        if receipt_path.exists():
            r = read_json(receipt_path)
            if r.get("status") == "SUCCESS" and (call_dir / "response.txt").exists():
                return CallResult((call_dir / "response.txt").read_text(), r, reused=True)
        level = effort if effort in LEVELS else _effort(self.s, None)  # the gateway's models go past high
        text_prompt = prompt + (MEDIA_NOTE if any(Path(m).suffix.lower() not in IMAGE_TYPES for m in media or []) else "")
        write_text(call_dir / "prompt.md", text_prompt)
        receipt: dict = {
            "provider": self.name, "kind": kind, "model_requested": f"{self.model}@{level}", "agy_version": self.version(),
            "started_utc": now(), "allowed_view_files": sorted(str(Path(m).resolve()) for m in (media or [])),
            "deviation": None, "schema": schema is not None, "prompt_bytes": len(text_prompt.encode()),
            "view_file_count": len(media or []),
        }
        t0 = time.monotonic()
        content = [{"type": "input_text", "text": text_prompt}]
        if media:
            extra, info = self._media_parts(media)
            content += extra
            receipt.update(info, media_prep_seconds=round(time.monotonic() - t0, 1))
        body: dict = {"model": self.model, "stream": True, "input": [{"role": "user", "content": content}]}
        if (asked := self._level(level)) is not None:
            body["reasoning"] = {"effort": asked}
        if schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": re.sub(r"\W", "_", kind)[:60] or "out",
                                       "schema": gemini_schema(schema), "strict": False}}
            write_json(call_dir / "schema.json", schema)
        limit = _seconds(timeout or self.s.agy_print_timeout)
        text, final, rate, error, served, answered = "", None, None, None, None, None
        headers = {"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.session:
            headers["X-Magpie-Session"] = self.session
        try:
            req = urllib.request.Request(f"{self.url}/v1/responses", data=json.dumps(body).encode(), method="POST", headers=headers)
            with self.opener.open(req, timeout=IDLE_TIMEOUT) as resp, (call_dir / "raw-output.jsonl").open("w") as raw:
                answered, served = resp.headers.get("X-Magpie-Model"), resp.headers.get("X-Magpie-Provider")
                for line in resp:
                    if time.monotonic() - t0 > limit:
                        error = ("PROVIDER_TIMEOUT", f"over the {limit:.0f} s budget")
                        break
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    e = json.loads(line[5:])
                    kind_ = e.get("type", "")
                    if kind_ == "response.output_text.delta":
                        text += e.get("delta", "")
                        continue  # the deltas are the bulk of the stream: keep the log to the events that matter
                    raw.write(line[5:].strip()[:4000] + "\n")
                    if kind_ == "codex.rate_limits":
                        rate = {k: v for k, v in e.items() if k != "type"}
                    elif kind_ in ("response.completed", "response.incomplete", "response.failed"):
                        final = e.get("response") or {}
                    elif kind_ == "error":
                        error = (_magpie_kind(500, json.dumps(e)), json.dumps(e)[:600])
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:1500]
            error = (_magpie_kind(e.code, detail), f"HTTP {e.code}: {detail}")
        except (socket.timeout, TimeoutError):
            error = ("PROVIDER_TIMEOUT", f"no data for {IDLE_TIMEOUT:.0f} s")
        except (urllib.error.URLError, OSError, ValueError) as e:
            error = ("PROVIDER_TRANSIENT", f"{type(e).__name__}: {e}")
        status = (final or {}).get("status")
        if error is None and final is None:
            error = ("PROVIDER_TRANSIENT", f"stream ended without a final event after {len(text)} chars")
        elif error is None and status == "incomplete":
            why = ((final.get("incomplete_details") or {}).get("reason") or "")
            error = ("PROVIDER_OUTPUT_LIMIT" if "token" in why else "CONTENT_SAFETY" if SAFETY_RE.search(why) else "PROVIDER_FAILURE",
                     f"incomplete: {why}")
        elif error is None and status != "completed":  # Claude's refusals come this way, after a 200
            why = json.dumps(final.get("error"), ensure_ascii=False)[:300]
            error = (_magpie_kind(500, why), f"response {status}: {why}")
        elif error is None and not text.strip():
            error = ("PROVIDER_TRANSIENT", "empty response")
        u = (final or {}).get("usage") or {}
        receipt.update(
            finished_utc=now(), wall_seconds=round(time.monotonic() - t0, 3), actual_model=answered or self.model,
            served_by=served, rate_limits=rate,
            usage={"input_tokens": u.get("input_tokens"),
                   "output_tokens": (u.get("output_tokens") or 0) - ((u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
                   if u else None,
                   "thinking_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens")},
        )
        write_text(call_dir / "response.txt", text)
        if rate:  # the subscription's headroom as of this call, for the 机房 page
            db.set_setting("magpie_quota", {"at": receipt["finished_utc"], **rate})
        if error:
            receipt.update(status="FAILED", error_class=error[0], error_excerpt=error[1][-1500:])
            write_json(receipt_path, receipt)
            raise ProviderError(error[0], error[1][:300], receipt)
        receipt["status"] = "SUCCESS"
        write_json(receipt_path, receipt)
        return CallResult(text, receipt)

    def purge_conversation(self, conversation_id: str | None) -> int:
        return 0

    def health(self) -> dict:
        tmp = self.s.home / "health" / "magpie"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            r = self.call(tmp, "只回复两个字：在线", kind="health", effort="low", timeout="2m")
            return {"ok": "在线" in r.text, "model": self.version(), "wall_seconds": r.receipt.get("wall_seconds"),
                    "checked_at": now()}
        except ProviderError as e:
            return {"ok": False, "error": e.kind, "checked_at": now()}

    def media_health(self) -> dict:
        """The whole video path: frames taken from a clip with a random number in it must let the model read it back."""
        d = self.s.home / "health" / "magpie-media"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        out = {"ok": False, "version": self.version(), "tool": None, "parameters": None, "error": None, "bytes": None,
               "checked_at": now()}
        code = str(random.randint(1000, 9999))
        clip = d / "canary.mp4"
        try:
            _canary_clip(clip, code, 2 * 1024 * 1024)
        except (OSError, subprocess.SubprocessError) as e:
            return {**out, "error": "CANARY_UNAVAILABLE", "detail": type(e).__name__}
        out["bytes"] = clip.stat().st_size
        try:
            r = self.call(d / "call", "只回复画面中央的数字，不要别的文字。", kind="media-health", media=[str(clip)],
                          effort="low", timeout="5m")
        except ProviderError as e:
            return {**out, "error": e.kind}
        right = re.sub(r"\D", "", r.text) == code
        return {**out, "ok": right, "error": None if right else "WRONG_ANSWER"}


def quota_view(q: dict | None) -> dict | None:
    """The last `codex.rate_limits` (setting magpie_quota) as buckets shaped like agy's /usage. Codex reports a primary
    and a secondary window, each with used_percent, window_minutes (300 = 5 hours, 10080 = a week) and reset_at (unix
    seconds) or reset_after_seconds, counted from the call; older gateways: resets_at, resets_in_seconds."""
    if not q:
        return None
    src = q["rate_limits"] if isinstance(q.get("rate_limits"), dict) else q
    try:
        at = datetime.fromisoformat(q["at"])
    except (KeyError, TypeError, ValueError):
        return None
    buckets = []
    for key in ("primary", "secondary"):
        w = src.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        mins = w.get("window_minutes")
        window = {300: "5h", 10080: "weekly"}.get(mins) or (f"{mins}m" if mins else "5h" if key == "primary" else "weekly")
        reset = None
        stamp = next((w[k] for k in ("reset_at", "resets_at") if isinstance(w.get(k), (int, float))), None)
        after = next((w[k] for k in ("reset_after_seconds", "resets_in_seconds") if isinstance(w.get(k), (int, float))), None)
        if stamp is not None:
            reset = datetime.fromtimestamp(stamp, timezone.utc)
        elif after is not None:
            reset = at + timedelta(seconds=after)
        buckets.append({"window": window, "remaining": max(0.0, min(1.0, 1 - w["used_percent"] / 100)),
                        "reset_at": reset.isoformat(timespec="seconds") if reset else None, "disabled": False, "note": None})
    return {"at": q["at"], "buckets": buckets} if buckets else None


# what each says of a model it does not have: Codex (400 model_not_found), Claude (a failed stream), magpie itself (404)
MODEL_GONE = re.compile(r"unknown model|model_not_found|knows no model|issue with the selected model", re.I)


def _magpie_kind(code: int, detail: str) -> str:
    if SAFETY_RE.search(detail):  # before the status: Claude's refusals come after a 200, and read here as a 500
        return "CONTENT_SAFETY"
    # a subscription's own limit: production waits for it like any quota
    if re.search(r"usage (limit|cap)|rate limit reached|limit reached|try again (in|at)", detail, re.I):
        return "PROVIDER_QUOTA"
    if MODEL_GONE.search(detail) or (code == 404 and "model" in detail.lower()):  # taken off the gateway while a stage uses it
        return "PROVIDER_MODEL"
    return _http_kind(code, detail)
