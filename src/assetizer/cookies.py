"""YouTube login state for yt-dlp: the user's own Chrome session through CookieCloud, else the server Chromium profile.

CookieCloud is read through its public read-only ingress (docs/cookiecloud-read-ingress.md):
``GET {origin}/api/v3/cookies?format=netscape&domains=youtube.com`` behind Cloudflare Access (service token)
plus a read-only App Token. The Netscape text is written to a 0600 file for a single yt-dlp call and
removed right after; cookie values never reach logs, events or the database.

Configuration (all optional; without it the server Chromium profile is used as before):
  setting  cookiecloud_origin        e.g. "https://cookiecloud-read.example.com"
  setting  cookiecloud_domains       default "youtube.com"
  secrets  cookiecloud-token, cf-access-client-id, cf-access-client-secret
The 机房 page sets them through save_credentials() (docs/cookiecloud-credentials-ui.md). Secrets are write-only:
nothing here returns a value, or any part of one.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import re
import secrets
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Iterator

import httpx

from . import config, db
from .util import now

# what to tell the user for each failure, by CookieCloud `code` (or our own)
FIX: dict[str, str] = {
    "not_configured": "还没有配置 CookieCloud。",
    "unreachable": "连不上 CookieCloud 的公网入口：检查 Cloudflare Tunnel 与 ingress 是否在线。",
    "tunnel_down": "Cloudflare 连不上 CookieCloud 只读入口背后的隧道（HTTP 530）：懒猫微服上给只读入口用的 cloudflared 可能断了，检查它是否在线。",
    "proxy_down": "隧道在线，但连不上懒猫微服上的只读代理（read-proxy，HTTP 502）：检查 CookieCloud 应用里的 read-proxy 是否在运行。",
    "ip_not_allowed": "Cloudflare 拦下了这个出口 IP：把它加进 CookieCloud 只读入口的 IP 白名单，令牌不用改。",
    "access_denied": "Cloudflare Access 不认这组服务令牌：检查 Access Client ID / Client Secret 是否填全、是否过期。",
    "rate_limited": "Cloudflare 限流（每个 IP 每 10 秒 5 次，与正常下载共用）：等十几秒再试。",
    "ingress_rate_limited": "CookieCloud 只读入口限流（每分钟 30 次）：等一分钟再试。",
    "not_found": "只读入口不开放这个地址：通常是 origin 填错了，应为 https://cookiecloud-read.example.com。",
    "upstream_unavailable": "懒猫微服上的 CookieCloud 没有响应：检查它是否在运行。",
    "upstream_timeout": "懒猫微服上的 CookieCloud 响应超时：检查它是否在运行，稍后再试。",
    "token_not_found": "CookieCloud App Token 不存在或没带上：在机房「YouTube 会话」里重新填写 App Token。",
    "token_revoked": "CookieCloud App Token 已被吊销：在 CookieCloud Setup 里新建一个只读 Token，再到机房里更新。",
    "missing_scope": "App Token 没有 cookies:read 权限：重建 Token。",
    "domain_not_allowed": "App Token 的域名白名单不包含 youtube.com：调整 Token 或 cookiecloud_domains。",
    "sync_source_not_configured": "CookieCloud 还没有同步源：在 Chrome 的 CookieCloud 插件里完成配对并同步一次。",
    "cookie_payload_missing": "Chrome 插件还没上传过 cookies：在插件里手动同步一次。",
    "no_youtube_cookies": "CookieCloud 里没有 YouTube 的 cookies：确认 Chrome 已登录 YouTube，并让插件同步一次。",
    "payload_recovery_required": "CookieCloud 暂时不提供这份 cookies，重试后仍然如此：让插件同步一次；还不行就到 CookieCloud 设置页看恢复状态。",
}
# failures where CookieCloud accepted the token, so its name and domain whitelist help explain them
_TOKEN_HINT = {"domain_not_allowed", "missing_scope", "sync_source_not_configured", "cookie_payload_missing",
               "no_youtube_cookies"}
_REDIRECT = (301, 302, 303, 307, 308)
# statuses Cloudflare itself answers with when it cannot reach the origin behind the tunnel
_CLOUDFLARE_ORIGIN = {502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}
# CookieCloud refuses reads while the plugin's sync is being written; one such read used to send a members-only download
# to the logged-out server Chromium, and the wait it caused held 补录 back for up to 30 minutes
_SYNC_WRITE_WAITS = (2, 5, 10)
KEPT_LOGIN = "youtube-cookies-last-good"
KEPT_GRACE = 6 * 3600  # seconds CookieCloud may be down, with a kept login to go on, before it is worth an alert


def _cloudflare_error(r) -> bool:
    """Whether this is Cloudflare's own error rather than an answer from the box: an HTML or text page, or, when JSON
    was asked for, JSON marked ``cloudflare_error``. The ingress proxy's and CookieCloud's JSON errors are not."""
    if "json" not in r.headers.get("content-type", ""):
        return True
    try:
        return (r.json() or {}).get("cloudflare_error") is True
    except (ValueError, AttributeError):
        return False


class CookieError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.detail = code, message or None  # detail: an exception class, "HTTP 403" or the egress IP, never a secret


def fix(code: str) -> str:
    """The sentence that explains a failure code; unknown ones still get one."""
    if code in FIX:
        return FIX[code]
    if code.startswith("http_"):
        return f"CookieCloud 返回了意外的响应（HTTP {code[5:]}）：检查 origin 是否填对，或稍后再试。"
    return "CookieCloud 读取失败，见错误码。"


def origin() -> str | None:
    o = (db.setting("cookiecloud_origin") or "").strip().rstrip("/")
    return o or None


def domains() -> str:
    return (db.setting("cookiecloud_domains") or "youtube.com").replace(" ", "")


def configured() -> bool:
    return bool(origin() and config.get().secret("cookiecloud-token"))


def _headers(accept: str = "text/plain") -> dict:
    s = config.get()
    h = {"Authorization": f"Bearer {s.secret('cookiecloud-token')}", "Accept": accept}
    cid, sec = s.secret("cf-access-client-id"), s.secret("cf-access-client-secret")
    if cid and sec:
        h["CF-Access-Client-Id"], h["CF-Access-Client-Secret"] = cid, sec
    return h


def _get(path: str, params: dict | None, timeout: float, accept: str = "text/plain"):
    """GET through the read-only ingress. A dropped connection, or Cloudflare's own page for an origin it cannot reach
    (the tunnel to the box flaps), is tried once more two seconds later: one blip used to send a download to the
    logged-out server Chromium. A timeout is not tried again."""
    for attempt in (1, 2):
        try:
            r = httpx.get(f"{origin()}{path}", params=params, headers=_headers(accept), timeout=timeout,
                          follow_redirects=False)
        except httpx.TimeoutException as e:
            raise CookieError("unreachable", type(e).__name__) from None
        except httpx.HTTPError as e:
            if attempt == 2:
                raise CookieError("unreachable", type(e).__name__) from None
        else:
            if attempt == 2 or r.status_code not in _CLOUDFLARE_ORIGIN or not _cloudflare_error(r):
                return r
        time.sleep(2)


def _ip_blocked(r) -> bool:
    """The WAF rule's block page. The rule turns away a source IP off the allowlist and a request without the service
    token headers, so with both headers sent it is the IP; a bad service token gets 401 from Access further on."""
    s = config.get()
    return (r.status_code == 403 and "you have been blocked" in r.text.lower()
            and bool(s.secret("cf-access-client-id") and s.secret("cf-access-client-secret")))


def egress_ip(timeout: float = 8) -> str | None:
    """The address Cloudflare sees this server at, through the same proxy settings as the reads."""
    try:
        m = re.search(r"^ip=(\S+)$", httpx.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=timeout).text, re.M)
    except httpx.HTTPError:
        return None
    return m.group(1) if m else None


def _error_code(r) -> str:
    """Name a non-200 answer. JSON from the ingress proxy or CookieCloud carries its own `code`; everything else is
    Cloudflare in front of them (docs/cookiecloud-credentials-ui.md §3.2, docs/cookiecloud-read-tunnel-530.md)."""
    status = r.status_code
    if not _cloudflare_error(r):
        try:
            code = (r.json() or {}).get("code")
        except (ValueError, AttributeError):
            code = None
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            return code
    if _ip_blocked(r):
        return "ip_not_allowed"
    if status in _REDIRECT or status in (401, 403):
        return "access_denied"  # Cloudflare Access: service token missing or invalid
    if status == 429:
        return "rate_limited"   # Cloudflare's edge rate limit
    if status == 404:
        return "not_found"
    if status == 530:
        return "tunnel_down"    # no healthy cloudflared connector for the tunnel (Cloudflare 1033)
    if status == 502:
        return "proxy_down"     # cloudflared is connected but got no valid answer from read-proxy
    if status >= 500:
        return "unreachable"    # Cloudflare's own error page: tunnel or ingress down
    return f"http_{status}"


def _cookie_lines(text: str) -> list[str]:
    """Netscape lines that are cookies (``#HttpOnly_`` lines are cookies, other ``#`` lines are comments)."""
    return [ln for ln in text.splitlines() if ln.strip() and "\t" in ln and (not ln.startswith("#") or ln.startswith("#HttpOnly_"))]


def _hosts(lines: list[str]) -> list[str]:
    return sorted({ln.split("\t", 1)[0].removeprefix("#HttpOnly_") for ln in lines})


def fetch(timeout: float = 30) -> str:
    """Netscape cookie text for yt-dlp; raises CookieError with a code from FIX."""
    if not configured():
        raise CookieError("not_configured")
    for wait in (*_SYNC_WRITE_WAITS, None):
        r = _get("/api/v3/cookies", {"format": "netscape", "domains": domains()}, timeout)
        if wait is None or r.status_code == 200 or _error_code(r) != "payload_recovery_required":
            break
        time.sleep(wait)
    if r.status_code != 200:
        code = _error_code(r)
        ip = egress_ip() if code == "ip_not_allowed" else None
        raise CookieError(code, f"出口 IP {ip}" if ip else f"HTTP {r.status_code}")
    if "html" in r.headers.get("content-type", ""):
        raise CookieError("not_found", "HTML page")  # some other site answered: the origin is wrong
    lines = _cookie_lines(r.text)
    if not any("youtube.com" in ln.split("\t", 1)[0] for ln in lines):
        raise CookieError("no_youtube_cookies", f"{len(lines)} cookies")
    return r.text if r.text.endswith("\n") else r.text + "\n"


def introspect(timeout: float = 15) -> dict | None:
    """The App Token's name, scopes and domain whitelist (none of them secret), or None when not available."""
    if not configured():
        return None
    try:
        r = _get("/api/v3/token/introspect", None, timeout, accept="application/json")
        tok = (r.json() or {}).get("token") if r.status_code == 200 else None
    except (CookieError, ValueError, AttributeError):
        return None
    if not isinstance(tok, dict):
        return None

    def strs(v) -> list[str]:
        return [x[:100] for x in v if isinstance(x, str)][:20] if isinstance(v, list) else []
    name = tok.get("name")
    return {"name": name[:100] if isinstance(name, str) else None, "scopes": strs(tok.get("scopes")),
            "allowed_domains": strs(tok.get("allowed_domains"))}


def _record(ok: bool, source: str, code: str | None = None, count: int | None = None, detail: str | None = None) -> None:
    """The last read. `since`: when the reads started failing."""
    was = db.setting("youtube_cookies_state") or {}
    since = None if ok else (was.get("since") if not was.get("ok", True) else None) or now()
    db.set_setting("youtube_cookies_state", {"ok": ok, "source": source, "code": code, "count": count, "detail": detail,
                                             "at": now(), "since": since})


def probe() -> dict:
    """One read through CookieCloud, as the 机房 test button and `assetizer cookies` report it:
    {ok, count, hosts} or {ok, code, fix}, plus the token's name and whitelist when CookieCloud accepted it.
    The result is recorded like a real download's."""
    if not configured():
        return {"ok": False, "code": "not_configured", "fix": fix("not_configured")}
    try:
        lines = _cookie_lines(fetch())
    except CookieError as e:
        _record(False, "cookiecloud", e.code, detail=e.detail)
        out = {"ok": False, "code": e.code, "fix": fix(e.code), "detail": e.detail}
    else:
        _record(True, "cookiecloud", count=len(lines))
        db.resolve_alert("cookiecloud")
        out = {"ok": True, "count": len(lines), "hosts": _hosts(lines)}
    if out["ok"] or out["code"] in _TOKEN_HINT:
        out["token"] = introspect()
    return out


# the cookies yt-dlp signs YouTube requests with: without one of them YouTube sees a signed-out visitor
AUTH_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")


def _signed_in(lines: list[str]) -> bool:
    """Whether the cookies carry a YouTube sign-in, judged by cookie names (values are never looked at)."""
    return any(len(f) >= 7 and "youtube.com" in f[0] and f[5] in AUTH_COOKIES for f in (ln.split("\t") for ln in lines))


@contextlib.contextmanager
def cookiecloud_file() -> Iterator[tuple[str, dict]]:
    """Fresh CookieCloud cookies in a 0600 file for one yt-dlp call, with no fallback to the server Chromium (the 机房
    video test). Yields (path, {count, signed_in}) and removes the file afterwards; raises CookieError."""
    try:
        text = fetch()
    except CookieError as e:
        _record(False, "cookiecloud", e.code, detail=e.detail)
        raise
    lines = _cookie_lines(text)
    _record(True, "cookiecloud", count=len(lines))
    db.resolve_alert("cookiecloud")
    run_dir = config.get().secrets_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    fd, path = tempfile.mkstemp(prefix="yt-cookies-", suffix=".txt", dir=run_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)
        yield path, {"count": len(lines), "signed_in": _signed_in(lines)}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def browser_args() -> list[str]:
    s = config.get()
    return ["--cookies-from-browser", f"chromium:{s.browser_profile}"] if s.browser_profile else []


def _cookie_temp(text: str) -> str:
    """A 0600 working copy for one yt-dlp call. Each call gets its own: yt-dlp writes the file back when it exits, and
    several run at once."""
    run_dir = config.get().secrets_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    fd, path = tempfile.mkstemp(prefix="yt-cookies-", suffix=".txt", dir=run_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return path


def _keep_login(text: str) -> None:
    """Keep the latest cookies that carry a YouTube sign-in, in plain text (0600 in secrets/), for when CookieCloud
    cannot be read. Failing to keep them is no reason to fail the read."""
    d = config.get().secrets_dir
    with contextlib.suppress(OSError):
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f"{KEPT_LOGIN}.", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, d / KEPT_LOGIN)  # mkstemp made it 0600
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _kept_login() -> str | None:
    try:
        return (config.get().secrets_dir / KEPT_LOGIN).read_text() or None
    except OSError:
        return None


def _kept_at() -> str | None:
    """When the kept login was read from CookieCloud (the file's time: nothing in it is looked at)."""
    try:
        t = (config.get().secrets_dir / KEPT_LOGIN).stat().st_mtime
    except OSError:
        return None
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat(timespec="seconds")


def _down_alert(e: CookieError, kept: bool) -> None:
    """A failed read with the kept login to go on is the lamp's business; an alert only once it has lasted. Without one
    the downloads that need a login are in trouble now."""
    body = f"{fix(e.code)}（错误码 {e.code}{f'，{e.detail}' if e.code in ('unreachable', 'ip_not_allowed') and e.detail else ''}）"
    if not kept:
        db.raise_alert("cookiecloud", "warning", "读不到 CookieCloud 的 YouTube 登录态，已改用服务器 Chromium", body)
        return
    since = (db.setting("youtube_cookies_state") or {}).get("since")
    try:
        long = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(since)).total_seconds() >= KEPT_GRACE
    except (TypeError, ValueError):
        long = False
    if long:
        db.raise_alert("cookiecloud", "info", f"CookieCloud 读不到已超过 {KEPT_GRACE // 3600} 小时，还在用上一份登录态", body)


@contextlib.contextmanager
def ytdlp_args() -> Iterator[list[str]]:
    """yt-dlp arguments carrying the login: fresh CookieCloud cookies in a 0600 file (removed afterwards),
    falling back to the server Chromium profile when CookieCloud is not configured or fails."""
    with ytdlp_login() as (args, _):
        yield args


@contextlib.contextmanager
def ytdlp_login() -> Iterator[tuple[list[str], dict]]:
    """ytdlp_args() and which login they carry: {"source": "cookiecloud" | "saved" | "browser" | None, "signed_in": whether
    the cookies carry a YouTube sign-in (None for the server Chromium: not looked at), "code": the CookieCloud failure
    the saved copy or the server Chromium stands in for}."""
    path, args = None, None
    how: dict = {"source": None, "signed_in": None, "code": None}
    if configured():
        try:
            text = fetch()
            lines = _cookie_lines(text)
            signed_in = _signed_in(lines)
            path = _cookie_temp(text)
            args = ["--cookies", path]
            how.update(source="cookiecloud", signed_in=signed_in)
            _record(True, "cookiecloud", count=len(lines))
            db.resolve_alert("cookiecloud")
            if signed_in:
                _keep_login(text)
        except CookieError as e:
            how["code"] = e.code
            _record(False, "cookiecloud", e.code, detail=e.detail)
            kept = _kept_login()
            with contextlib.suppress(OSError):
                if kept:
                    path = _cookie_temp(kept)
                    args = ["--cookies", path]
                    # "saved", not "cookiecloud": a refusal may mean this copy has gone stale, not that the video is shut
                    how.update(source="saved", signed_in=True)
            _down_alert(e, bool(args))
        except OSError:
            # Preparation can fail after the file is made (the status update). Remove the plaintext cookie file before
            # falling back, and never hand yt-dlp a partial file.
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            path = None
            args = None
    if args is None:
        args = browser_args()
        how.update(source="browser" if args else None, signed_in=None)
        if not configured():
            _record(bool(args), "browser")
    try:
        yield args, how
    finally:
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)


# ------------------------------------------------------------------ credentials from the 机房 page
# API field → (label in messages, secret file; None for the origin, which is a setting)
FIELDS: dict[str, tuple[str, str | None]] = {
    "origin": ("origin", None),
    "access_client_id": ("Access Client ID", "cf-access-client-id"),
    "access_client_secret": ("Access Client Secret", "cf-access-client-secret"),
    "app_token": ("App Token", "cookiecloud-token"),
}
# what a value must look like; the messages describe the format and never repeat the value
_FORMAT: dict[str, tuple[re.Pattern, str]] = {
    "access_client_id": (re.compile(r"[0-9a-f]{32}\.access"), "Access Client ID 的格式不对：应为 32 位小写十六进制，加上「.access」"),
    "access_client_secret": (re.compile(r"[\x21-\x7e]{32,128}"), "Access Client Secret 的格式不对：应为 32–128 个可打印 ASCII 字符，中间不能有空白"),
    "app_token": (re.compile(r"ccat_[A-Za-z0-9]{42}"), "App Token 的格式不对：应为「ccat_」加 42 位字母或数字"),
}
_ORIGIN_RULE = "origin 必须以 https:// 开头，而且只有主机名：不能带路径、查询参数或用户名"


class CredentialError(ValueError):
    """A refused credentials change: HTTP status plus a message that never contains a submitted value."""

    def __init__(self, status: int, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.message, self.field = status, message, field


def _clean_origin(v: str) -> str:
    if len(v) > 300 or re.search(r"[\x00-\x20\x7f]", v):
        raise CredentialError(400, _ORIGIN_RULE, "origin")
    try:
        u = urllib.parse.urlsplit(v)
    except ValueError:
        raise CredentialError(400, _ORIGIN_RULE, "origin") from None
    host = u.hostname or ""
    if (u.scheme != "https" or u.netloc.lower() != host or u.path not in ("", "/") or u.query or u.fragment
            or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host)):
        raise CredentialError(400, _ORIGIN_RULE, "origin")  # the netloc check also rules out userinfo and ports
    return f"https://{host}"


def _secrets_dir() -> Path:
    d = config.get().secrets_dir
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _fsync_dir(d: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_secret(name: str, value: str) -> None:
    """0600 temp file created with O_EXCL next to the target, fsync, then os.replace: readers see old or new, never half."""
    d = _secrets_dir()
    tmp = d / f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(d)


def _remove_secret(name: str) -> None:
    d = _secrets_dir()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(d / name)
    _fsync_dir(d)


def _env_set(f: str) -> str | None:
    """The ASSETIZER_* variable that overrides this field's secret file, when it is set."""
    name = FIELDS[f][1]
    env = config.secret_env(name) if name else None
    return env if env and os.environ.get(env) else None


def save_credentials(body) -> None:
    """Partial update from the 机房 form: {origin?, access_client_id?, access_client_secret?, app_token?, clear?: [...]}.
    Omitted or empty means unchanged. Every check runs before anything is written."""
    if not isinstance(body, dict):
        raise CredentialError(400, "请求体必须是 JSON 对象")
    if set(body) - set(FIELDS) - {"clear"}:
        raise CredentialError(400, "请求里有不认识的字段；可用的是 origin、access_client_id、access_client_secret、app_token、clear")
    clear = body.get("clear") or []
    if not isinstance(clear, list) or not all(isinstance(f, str) and f in FIELDS for f in clear):
        raise CredentialError(400, "clear 必须是字段名列表：origin、access_client_id、access_client_secret、app_token")
    clear = list(dict.fromkeys(clear))
    sets: dict[str, str] = {}
    for f, (label, _) in FIELDS.items():
        v = body.get(f)
        if v is None:
            continue
        if not isinstance(v, str):
            raise CredentialError(400, f"{label} 必须是字符串", f)
        if v.strip():
            sets[f] = v.strip()
    if both := [f for f in clear if f in sets]:
        raise CredentialError(400, f"{FIELDS[both[0]][0]} 不能同时修改和清除", both[0])
    if not sets and not clear:
        raise CredentialError(400, "没有要修改的内容")
    for f in [*sets, *clear]:
        if env := _env_set(f):
            raise CredentialError(409, f"{FIELDS[f][0]} 由服务器环境变量 {env} 提供，这里改不了：请在服务器上修改", f)
    if "origin" in sets:
        sets["origin"] = _clean_origin(sets["origin"])
    for f, (rule, message) in _FORMAT.items():
        if f in sets and not rule.fullmatch(sets[f]):
            raise CredentialError(400, message, f)

    for f, v in sets.items():
        if name := FIELDS[f][1]:
            _write_secret(name, v)
        else:
            db.set_setting("cookiecloud_origin", v)
    for f in clear:
        if name := FIELDS[f][1]:
            _remove_secret(name)
        else:
            db.set_setting("cookiecloud_origin", None)
    with contextlib.suppress(OSError):
        (config.get().secrets_dir / KEPT_LOGIN).unlink(missing_ok=True)  # may be another account's
    # the last read and the last video test used the old credentials; keep only the test video's address
    db.set_setting("youtube_cookies_state", None)
    last = db.setting("youtube_video_test") or {}
    db.set_setting("youtube_video_test", {"url": last["url"]} if last.get("url") else None)
    parts = [f"已更新 {'、'.join(FIELDS[f][0] for f in sets)}" if sets else "",
             f"已清除 {'、'.join(FIELDS[f][0] for f in clear)}" if clear else ""]
    db.event("config", "CookieCloud 凭据：" + "；".join(p for p in parts if p))


def _field(name: str) -> dict:
    """Whether a secret is set and where it comes from: never its value, nor any part of it."""
    s = config.get()
    if os.environ.get(config.secret_env(name)):
        return {"configured": bool(s.secret(name)), "source": "env", "updated_at": None}
    try:
        mtime = (s.secrets_dir / name).stat().st_mtime
    except OSError:
        mtime = None
    if mtime is None or not s.secret(name):
        return {"configured": False, "source": None, "updated_at": None}
    return {"configured": True, "source": "file",
            "updated_at": _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc).isoformat(timespec="seconds")}


def status() -> dict:
    """What the 机房 shows: where the login comes from, how the last read and the last video test went (no values)."""
    s = config.get()
    return {"cookiecloud": {"configured": configured(), "origin": origin(), "domains": domains(),
                            "access_token": bool(s.secret("cf-access-client-id") and s.secret("cf-access-client-secret")),
                            "fields": {f: _field(name) for f, (_, name) in FIELDS.items() if name}},
            "state": db.setting("youtube_cookies_state"), "kept_at": _kept_at(),
            "video_test": db.setting("youtube_video_test"), "fix": FIX}
