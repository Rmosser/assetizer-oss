"""专题原片和底稿的只增网盘副本（design §R8-8）。

只有这里读写 rclone.conf。网页只得到布尔状态；凭据不经过 setting、事件或子进程参数。
web 与 worker 共用主文件；rclone 用私有目录里的快照，短锁内比较 token 后才合并刷新。
失败只在它用的登录仍是当前登录时才算数，判断和写入在同一把锁内。
GET 不启动 rclone。后台一次传一期，不占读片槽；远端副本永远不删除。
"""
from __future__ import annotations

import base64
import configparser
import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from .. import config, db
from ..media import ffmpeg
from ..util import now
from . import draft_dir, media_dir

COMMANDS = frozenset({"copyto", "copy", "lsjson", "about", "mkdir"})
TRANSFER_TIMEOUT = 6 * 3600  # 大原片可传数十分钟；给六小时，不把短暂超时当凭据失效
QUERY_TIMEOUT = 120
LOGIN_TTL = 15 * 60
USAGE_EVERY = 3600
DEFAULT_FOLDER = "课代表/专题备份"
REDIRECT = "http://127.0.0.1:53682/"
SCOPE = "https://www.googleapis.com/auth/drive.file"
_conf_lock = threading.RLock()
_conf_held = threading.local()  # 同一线程重入时不再 flock：另开 fd 的 flock 会和自己互斥
_login_lock = threading.RLock()
_transfer_lock = threading.Lock()
media_lock = threading.Lock()  # worker 内，选中/登记与清理的重新检查、删除共用
_claimed: set[str] = set()
_pending: dict | None = None
_uploading: str | None = None


class BackupError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False, missing: bool = False, stale: bool = False):
        super().__init__(message)
        self.auth, self.missing, self.stale = auth, missing, stale
        self.identity: tuple | None = None  # rclone 调用开始时的登录身份；其余错误不带


def _conf_path() -> Path:
    return config.get().secrets_dir / "rclone.conf"


def _load_conf(path: Path) -> dict[str, str]:
    """rclone 会刷新并原子替换此文件；每次重新读，不缓存 token，不把解析错误原文带出去。"""
    c = configparser.ConfigParser(interpolation=None)
    try:
        c.read(path, encoding="utf-8")
        return dict(c["gdrive"]) if c.has_section("gdrive") else {}
    except (OSError, configparser.Error):
        return {}


def _read_conf() -> dict[str, str]:
    return _load_conf(_conf_path())


def _cleanup_runs(d: Path) -> None:
    """调用者持主配置锁。每次调用的副本在私有目录 .run-<id>/ 里（rclone 刷新 token 时的临时文件和旧文件也落在里面），
    旁边的 .run-<id>.lock 由调用进程全程 flock；共享卷上跨进程、跨容器都能判断，不看 PID。
    锁得上就是主人已不在，连目录一起删；没有租约的 .run-* 都是残留。
    逐个兜底：判断不了的当作还有人在用，删不掉的跳过，不挡住这次调用、断开、清除客户端和 worker 启动。"""
    held = set()
    for lease in d.glob(".run-*.lock"):
        stem = lease.name[:-len(".lock")]
        try:
            fd = os.open(lease, os.O_RDWR)
        except FileNotFoundError:
            continue
        except OSError:
            held.add(stem)
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # BlockingIOError 是正在用；其他错误判断不了，同样留着
                held.add(stem)
                continue
            with contextlib.suppress(OSError):
                _remove(d / stem)
                _remove(lease)
        finally:
            os.close(fd)
    for path in d.glob(".run-*"):
        if path.name in held or (path.name.endswith(".lock") and path.name[:-len(".lock")] in held):
            continue
        with contextlib.suppress(OSError):
            _remove(path)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def reap() -> None:
    """worker 启动时清掉被杀进程留下的副本（例如断开之后才被杀）；没有残留时什么都不碰，不建锁文件。
    清不了（卷只读、I/O 错误）只记一条事件，worker 照常启动。"""
    d = config.get().secrets_dir
    try:
        if d.is_dir() and any(d.glob(".run-*")):
            with _locked_conf():
                _cleanup_runs(d)
    except OSError as e:
        _event(f"清理网盘配置副本失败：{e.strerror or type(e).__name__}", "warn")


@contextlib.contextmanager
def _locked_conf():
    if getattr(_conf_held, "depth", 0):
        with _conf_lock:
            _conf_held.depth += 1
            try:
                yield
            finally:
                _conf_held.depth -= 1
        return
    d = config.get().secrets_dir
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    with _conf_lock:
        fd = os.open(d / ".rclone.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _conf_held.depth = 1
            try:
                yield
            finally:
                _conf_held.depth = 0
        finally:
            os.close(fd)


def _identity(data: dict) -> tuple | None:
    """登录身份：客户端加 refresh token。同一次登录里刷新只换 access token，身份不变；
    断开、重新登录、换或清客户端都会变。没有 token 时为 None；token 里缺 refresh token 时退回整段。"""
    token = data.get("token")
    if not token:
        return None
    try:
        refresh = json.loads(token).get("refresh_token")
    except (ValueError, TypeError, AttributeError):
        refresh = None
    return (data.get("client_id"), data.get("client_secret"), refresh or token)


def _current(error: object) -> bool:
    """调用者持主配置锁：这次失败是否属于当前登录。不来自 rclone 的错误算当前；
    来自 rclone 的，登录身份变了就标 stale，不再当作当前登录的失败或登录失效。"""
    if not isinstance(error, BackupError):
        return True
    if not error.stale and error.identity is not None and _identity(_read_conf()) != error.identity:
        error.stale = True
    if error.stale:
        error.auth = False
    return not error.stale


def _report(error: object) -> None:
    """把失败写进全局状态；判断归属和写入在同一把锁内，新登录插不进中间。"""
    with _locked_conf():
        if _current(error):
            _state(str(error), isinstance(error, BackupError) and error.auth)


def _conf_text(data: dict) -> str:
    c = configparser.ConfigParser(interpolation=None)
    c["gdrive"] = {**data, "type": "drive", "scope": "drive.file"}
    out = io.StringIO(); c.write(out)
    return out.getvalue()


def _write_conf(data: dict) -> None:
    """调用者持独占锁；主配置始终原子替换。"""
    fd, name = tempfile.mkstemp(prefix=".rclone-", dir=config.get().secrets_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(_conf_text(data))
            f.flush(); os.fsync(f.fileno())
        os.replace(name, _conf_path())
        dfd = os.open(config.get().secrets_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _update_conf(**values: str | None) -> None:
    with _locked_conf():
        data = _read_conf()
        for k, v in values.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        _write_conf(data)


def redact(value: object, *, private: dict | None = None) -> str:
    text = str(value)
    values = []
    for data in (_read_conf(), private or {}):
        values.append(data.get("client_secret"))
        with contextlib.suppress(ValueError, TypeError):
            token = json.loads(data.get("token", "{}"))
            if isinstance(token, dict):
                values += [token.get(k) for k in ("access_token", "refresh_token", "id_token")]
    for raw in values:
        if isinstance(raw, str) and raw:
            text = text.replace(raw, "[已隐藏]")
    text = re.sub(r'(ya29\.|1//)[^\s\x22\x27<>&,}]+', "[已隐藏]", text)
    text = re.sub(r'(?i)([\x22\x27](?:access_token|refresh_token|client_secret|id_token)[\x22\x27]\s*:\s*)([\x22\x27])(?:\\.|(?!\2).)*?\2',
                  r'\1"[已隐藏]"', text)
    # 日志可能把 JSON 再转义一层，或用表单、INI、Go 结构体和 HTTP 头表示凭据。
    text = re.sub(r'(?i)(\\"(?:access_token|refresh_token|client_secret|id_token)\\"\s*:\s*\\").*?\\"',
                  r'\1[已隐藏]\\"', text)
    text = re.sub(r'(?im)((?:\b(?:access_token|refresh_token|client_secret|id_token)\s*=|[?&]code=|^code=)\s*)[^\s&#\x22\x27<>]*',
                  r'\1[已隐藏]', text)
    text = re.sub(r'(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+', r'\1[已隐藏]', text)
    return re.sub(r'(?i)(\b(?:RefreshToken|AccessToken)\s*:\s*)[^\s,}\x22\x27]+', r'\1[已隐藏]', text)


def _event(message: str, level: str = "info", data: dict | None = None) -> None:
    db.event("backup", redact(message), level=level, data=data)


def _state(error: str | None = None, auth: bool = False) -> None:
    db.set_setting("gdrive_state", {"error": redact(error) if error else None, "auth": auth})


def _auth_error(text: str) -> bool:
    text = re.sub(r'/\S+', '', text)
    text = re.sub(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])',
                  lambda m: m[0] if m[0].lower() in {"credentials", "permissions"} else "", text)
    if re.search(r'invalid_grant|invalid_client|invalid credentials|unauthenticated|invalid authentication', text, re.I):
        return True
    status = re.search(r'\b(?:Error|HTTP|status|code\s*:)\s*(401|403)\b', text, re.I)
    if status and (status[1] == "401" or re.search(r'auth|credential|permission|scope', text, re.I)):
        return True
    return bool(re.search(r'\btoken\b', text, re.I) and re.search(r'invalid_grant|unauthorized|invalid_client|revoked', text, re.I))


def _run(command: str, *args: str) -> str:
    if command not in COMMANDS:
        raise ValueError("网盘命令不在只读或只增白名单中")
    d = config.get().secrets_dir
    run_dir = lease = None
    lease_fd = None
    data, original, identity = {}, None, None
    try:
        with _locked_conf():
            _cleanup_runs(d)
            data = _read_conf()
            original, identity = data.get("token"), _identity(data)
            if not original:
                raise BackupError("网盘已断开，请重新登录", stale=True)
            stem = ".run-" + secrets.token_hex(16)
            # 顺序固定：先建租约并锁上，再建私有目录写副本；都在主配置锁内，清理插不进来。
            lease = d / (stem + ".lock")
            lease_fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.fchmod(lease_fd, 0o600)
            fcntl.flock(lease_fd, fcntl.LOCK_EX)
            run_dir = d / stem
            os.mkdir(run_dir, 0o700)
            fd = os.open(run_dir / "rclone.conf", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(_conf_text(data))
        try:
            p = subprocess.run([config.get().rclone_bin, "--config", str(run_dir / "rclone.conf"), command, *map(str, args)],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               timeout=TRANSFER_TIMEOUT if command in {"copy", "copyto"} else QUERY_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackupError("网盘传输超时" if isinstance(e, subprocess.TimeoutExpired) else "无法启动 rclone") from None
        if p.returncode:
            message = redact(p.stderr + "\n" + p.stdout, private=data).strip()
            raise BackupError(message[:1500] or "网盘没有完成请求", auth=_auth_error(message),
                              missing=p.returncode in (3, 4) or bool(re.search(r"(?:object|directory|file) not found", message, re.I)))
        return redact(p.stdout, private=data)
    except (BackupError, OSError) as e:
        error = e if isinstance(e, BackupError) else BackupError("无法写入网盘配置")
        error.identity = identity
        if identity is None:
            error.stale = True
        with _locked_conf():
            if _current(error) and error.auth:
                _state(str(error), auth=True)
        raise error from None
    finally:
        try:
            if run_dir:
                with _locked_conf():
                    try:
                        # 回写仍比整段 token：同一次登录里别的调用已经写回过更新的，就丢掉自己的。
                        refreshed = _load_conf(run_dir / "rclone.conf").get("token")
                        current = _read_conf()
                        if refreshed and refreshed != original and current.get("token") == original:
                            current["token"] = refreshed
                            _write_conf(current)
                    finally:
                        _remove(run_dir)
        finally:
            if lease_fd is not None:
                try:
                    with _locked_conf():
                        lease.unlink(missing_ok=True)
                finally:
                    os.close(lease_fd)


def _json(command: str, *args: str):
    try:
        return json.loads(_run(command, *args))
    except (ValueError, TypeError):
        raise BackupError("网盘返回的数据无法识别") from None


def _stat(path: str) -> dict | None:
    try:
        row = _json("lsjson", f"gdrive:{path}", "--stat", "--hash")
        return row if isinstance(row, dict) and not row.get("IsDir") else None
    except BackupError as e:
        if e.missing and not e.auth and not e.stale:
            return None
        raise


def _folder() -> dict:
    return db.setting("gdrive") or {}


def available() -> bool:
    c = _read_conf()
    return bool(shutil.which(config.get().rclone_bin) and c.get("client_id") and c.get("client_secret")
                and c.get("token") and _folder().get("folder_id") and c.get("root_folder_id") == _folder().get("folder_id"))


def _valid_video(vid: str) -> None:
    if not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise ValueError("视频 ID 不合法")


def uploading(vid: str) -> bool:
    try:
        _valid_video(vid)
    except ValueError:
        return False
    return _uploading == vid or vid in _claimed


@contextlib.contextmanager
def claim(vid: str):
    """登记媒体使用权；长传输不持锁，清理在同一把锁内检查登记并删除。"""
    _valid_video(vid)
    with media_lock:
        free = not uploading(vid)
        if free:
            _claimed.add(vid)
    try:
        yield free
    finally:
        if free:
            with media_lock:
                _claimed.discard(vid)


def _login_view() -> dict | None:
    p = _pending
    if p and p["home"] == str(config.get().home) and p["expires"] > time.time():
        return {"pending": True, "url": p["url"], "expires_at": dt.datetime.fromtimestamp(p["expires"], dt.timezone.utc).isoformat()}
    return None


def view() -> dict:
    c, f, state = _read_conf(), _folder(), db.setting("gdrive_state") or {}
    binary = bool(shutil.which(config.get().rclone_bin))
    client, token = bool(c.get("client_id") and c.get("client_secret")), bool(c.get("token"))
    fid = f.get("folder_id")
    folder = {"id": fid, "path": f.get("folder_path"), "url": f"https://drive.google.com/drive/folders/{quote(fid, safe='')}"} if fid else None
    s = ("off" if not binary else "client" if not client else "login" if not token else "folder" if not fid or c.get("root_folder_id") != fid else
         "auth" if state.get("auth") else "failing" if state.get("error") else "ok")
    cached = db.setting("gdrive_usage")
    inventory = db.setting("gdrive_inventory") or {}
    records = list(inventory.get("videos", {}).values()) if inventory.get("folder") == fid else []
    usage = {"videos": len(records), "bytes": sum(r["bytes"] for r in records), "drive_free": cached.get("free"),
             "drive_total": cached.get("total"), "checked_at": cached.get("checked_at")} if cached else None
    queue = db.one("""SELECT COALESCE(SUM(backup_due),0) due,
                      COUNT(backup_failed_at) failed, COUNT(backup_missed_at) missed FROM videos""")
    if not available():
        queue["due"] = 0
    vid = _uploading or (db.setting("gdrive_upload") or {}).get("video")
    title = db.one("SELECT title FROM videos WHERE id=?", vid) if vid else None
    return {"rclone": binary, "client": client, "token": token, "folder": folder, "state": s,
            "error": redact(state["error"]) if state.get("error") else None, "login": _login_view(),
            "usage": usage, "queue": queue, "uploading": {"id": vid, "title": title["title"] if title else None} if vid else None}


def save_client(client_id: str = "", client_secret: str = "", *, clear: bool = False) -> dict:
    global _pending
    with _login_lock:
        if clear:
            with _locked_conf():
                _cleanup_runs(config.get().secrets_dir)
                _conf_path().unlink(missing_ok=True)
        else:
            if not client_id or not client_secret or any(ord(x) < 32 or ord(x) == 127 for x in client_id + client_secret):
                raise ValueError("客户端 ID 和密钥必须一起填写，且不能含换行或控制字符")
            # 更换客户端要重新授权；保留本应用建过的文件夹，下一次登录仍按 ID 访问。
            _update_conf(client_id=client_id, client_secret=client_secret, token=None,
                         root_folder_id=_folder().get("folder_id"))
        _pending = None
        _state()
    return view()


def start_login() -> dict:
    global _pending
    with _login_lock:
        c = _read_conf()
        if not c.get("client_id") or not c.get("client_secret"):
            raise ValueError("请先填写客户端 ID 和密钥")
        if not _login_view():
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
                "client_id": c["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE,
                "access_type": "offline", "prompt": "consent", "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256"})
            _pending = {"state": state, "verifier": verifier, "url": url, "expires": time.time() + LOGIN_TTL,
                        "home": str(config.get().home)}
    return view()


def cancel_login() -> dict:
    global _pending
    with _login_lock:
        _pending = None
    return view()


def submit_code(value: str) -> dict:
    global _pending
    with _login_lock:
        if not _login_view():
            raise ValueError("登录已过期，请重新登录")
        p = _pending
        invalid = "授权地址或授权码格式不正确，请重新粘贴"
        if not value or len(value) > 4096 or re.search(r"[\s\x00-\x1f\x7f]", value):
            raise ValueError(invalid)
        is_url = "://" in value
        q = {}
        if is_url:
            u = urlparse(value)
            if u.scheme != "http" or u.netloc not in ("127.0.0.1:53682", "localhost:53682") or u.path != "/" or u.fragment:
                raise ValueError(invalid)
            q = parse_qs(u.query, keep_blank_values=True)
            if any(len(q.get(k, [])) > 1 for k in ("state", "code", "error")) or ("error" in q and "code" in q):
                raise ValueError(invalid)
        if "state" in q and not re.fullmatch(r"[A-Za-z0-9_-]+", q["state"][0]):
            raise ValueError(invalid)
        if "state" in q and not secrets.compare_digest(q["state"][0], p["state"]):
            raise ValueError("授权地址的 state 不匹配，请使用这次登录的地址")
        if q.get("error") == ["access_denied"]:
            raise ValueError("你在 Google 那边取消了授权")
        if q.get("error"):
            raise ValueError("Google 没有同意这次授权，请重新登录")
        code = (q.get("code") or [""])[0] if is_url else value
        if not re.fullmatch(r"[A-Za-z0-9/_\-.~]{10,2048}", code):
            raise ValueError(invalid)
        c = _read_conf()
        try:
            with httpx.Client(timeout=60) as client:  # 默认 trust_env=True，沿用盒子的 HTTPS_PROXY
                response = client.post("https://oauth2.googleapis.com/token", data={
                    "grant_type": "authorization_code", "client_id": c.get("client_id"), "client_secret": c.get("client_secret"),
                    "code": code, "redirect_uri": REDIRECT, "code_verifier": p["verifier"]})
            if response.status_code != 200:
                raise ValueError("Google 换取登录凭据失败，请重新登录并粘贴新地址")
            try:
                result = response.json()
                expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=float(result["expires_in"]))).isoformat()
            except (ValueError, KeyError, TypeError, OverflowError):
                raise ValueError("Google 返回的登录凭据格式不完整，请重新登录") from None
            if not isinstance(result.get("refresh_token"), str) or not result["refresh_token"]:
                raise ValueError("Google 未返回 refresh_token，请重新登录并同意离线授权")
            if not isinstance(result.get("access_token"), str) or not result["access_token"]:
                raise ValueError("Google 未返回登录凭据，请重新登录")
            token = {k: result[k] for k in ("access_token", "refresh_token")}
            token.update(token_type=result.get("token_type", "Bearer"), expiry=expiry)
        except httpx.HTTPError:
            raise ValueError("无法换取 Google 登录凭据，请稍后重新登录") from None
        _update_conf(token=json.dumps(token), root_folder_id=_folder().get("folder_id"))
        _pending = None
        try:
            refresh_usage(force=True)
        except BackupError as e:
            _report(e)
            raise ValueError("网盘登录验证失败：" + redact(e)) from None
        _state()
        _event("Google 网盘登录成功")
    return view()


def disconnect() -> dict:
    global _pending
    with _login_lock:
        with _locked_conf():
            _cleanup_runs(config.get().secrets_dir)
            if _conf_path().exists():
                c = _read_conf()
                c.pop("token", None)
                _write_conf(c)
        _pending = None
        _state()
        _event("Google 网盘已断开，远端副本保留")
    return view()


def refresh_usage(*, force: bool = False) -> None:
    cached = db.setting("gdrive_usage") or {}
    if not force and time.time() - max(cached.get("at", 0), db.setting("gdrive_usage_attempt", 0)) < USAGE_EVERY:
        return
    db.set_setting("gdrive_usage_attempt", time.time())
    u = _json("about", "gdrive:", "--json")
    db.set_setting("gdrive_usage", {"total": u.get("total"), "used": u.get("used"), "free": u.get("free"),
                                    "checked_at": now(), "at": time.time()})


def create_folder(path: str = DEFAULT_FOLDER) -> dict:
    with _login_lock:
        if _folder().get("folder_id"):
            raise FileExistsError("已经建过备份文件夹")
        if not _read_conf().get("token"):
            raise ValueError("请先登录 Google 网盘")
        path = path.strip().strip("/")
        if not path or len(path) > 512 or any(p in ("", ".", "..") for p in path.split("/")) or re.search(r"[:\\\x00-\x1f\x7f]", path):
            raise ValueError("文件夹路径不合法")
        try:
            _run("mkdir", f"gdrive:{path}")
            parent, _, name = path.rpartition("/")
            rows = _json("lsjson", f"gdrive:{parent}", "--dirs-only")
            matches = [r for r in rows if r.get("Name") == name and r.get("IsDir") and r.get("ID")]
            if len(matches) != 1:
                raise BackupError("无法唯一确认新建的备份文件夹")
            fid = matches[0]["ID"]
            _update_conf(root_folder_id=fid)
            db.set_setting("gdrive", {"folder_id": fid, "folder_path": path})
            for v in db.q("SELECT id,media_path FROM videos"):
                if (media_dir(v["id"]) / "source.mkv").exists() or (v["media_path"] and Path(v["media_path"]).exists()) or (draft_dir(v["id"]) / "full.md").exists():
                    db.exec("UPDATE videos SET backup_due=1,backup_gen=backup_gen+1 WHERE id=?", v["id"])
            _state()
            _event("Google 网盘备份文件夹已建好")
        except BackupError as e:
            _report(e)
            raise ValueError("创建网盘文件夹失败：" + redact(e)) from None
    return view()


def test() -> dict:
    c, fid = _read_conf(), _folder().get("folder_id")
    if not shutil.which(config.get().rclone_bin) or not c.get("client_id") or not c.get("client_secret") or not c.get("token") or not fid:
        raise ValueError("请先完成网盘客户端、登录和文件夹设置")
    try:
        if not c.get("root_folder_id"):
            _update_conf(root_folder_id=fid)
        if _read_conf().get("root_folder_id") != fid:
            raise BackupError("备份文件夹与配置不一致")
        refresh_usage(force=True)
        _json("lsjson", "gdrive:", "--dirs-only")
        _state()
    except (BackupError, OSError) as e:
        _report(e)
    return view()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while block := f.read(4 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def _hash(row: dict) -> str:
    return (row.get("Hashes") or {}).get("md5", "").lower()


def _remember(vid: str, size: int, digest: str, fid: str) -> None:
    _valid_video(vid)
    with db.tx():
        i = db.setting("gdrive_inventory") or {}
        videos = i.get("videos", {}) if i.get("folder") == fid else {}
        videos[vid] = {"bytes": size, "md5": digest}
        db.set_setting("gdrive_inventory", {"folder": fid, "videos": videos})


def _failed(vid: str, error: object) -> None:
    """记一期的失败；判断归属和写入在同一把锁内（A15）。旧登录的失败不记，这一期留在队列里。"""
    _valid_video(vid)
    with _locked_conf():
        if not _current(error):
            return
        message = redact(error)[:1500]
        db.exec("UPDATE videos SET backup_error=?,backup_failed_at=? WHERE id=?", message, now(), vid)
        _state(message, isinstance(error, BackupError) and error.auth)
        _warn_once(vid, f"备份失败：{message}")


def _warn_once(vid: str, message: str) -> None:
    _valid_video(vid)
    message = redact(message)[:1500]
    # rclone 的每次 stderr 都带新时间；去掉时间后识别同一种失败，不每半小时再记一条。
    normalized = re.sub(r"\b\d{4}[-/]\d\d[-/]\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?\s*", "", message)
    key = hashlib.sha256(normalized.encode()).hexdigest()
    if not db.one("""SELECT 1 FROM events WHERE kind='backup' AND json_extract(data,'$.video')=?
                     AND json_extract(data,'$.error_key')=?""", vid, key):
        _event(f"{vid} {message}", "warn", {"video": vid, "error_key": key})


def _upload(v: dict) -> None:
    vid = v["id"]
    _valid_video(vid)
    fid = _folder()["folder_id"]
    row = _stat(f"{vid}/source.mkv")  # 问远端：本地行可能删过又重新加入
    mismatch = f"网盘上 {vid}/source.mkv 与本地不一致，请在网盘里删掉它后点重试"
    if row and v.get("backup_bad_md5") and _hash(row) == v["backup_bad_md5"]:
        raise BackupError(mismatch)
    pending_md5 = v.get("backup_src_md5")
    local = Path(v["media_path"]) if v["media_path"] else media_dir(vid) / "source.mkv"
    if not row and local.is_file():
        digest = _md5(local)
        pending_md5 = digest
        # 网盘上没有这一份：旧的确认（换文件夹前、或用户删掉远端前）不再成立，本次上传核对前不算已备份。
        db.exec("UPDATE videos SET backup_src_md5=?,backup_at=NULL,backup_folder=NULL WHERE id=?", digest, vid)
        # copyto 不理 --immutable（只有 copy/sync 检查它；2026-11-10 devbox 实测会覆盖）。--ignore-existing 让核对后
        # 才出现的远端同名原片原样留着，下面的 md5 核对再把它当作不一致报出来。
        _run("copyto", str(local), f"gdrive:{vid}/source.mkv", "--ignore-existing")
        row = _stat(f"{vid}/source.mkv")
        if not row or _hash(row) != digest:
            db.exec("UPDATE videos SET backup_bad_md5=?,backup_at=NULL WHERE id=?", _hash(row) if row else None, vid)
            raise BackupError(mismatch)
    if row:
        if not _hash(row):
            raise BackupError("网盘没有返回原片的 md5")
        if pending_md5 and _hash(row) != pending_md5:
            db.exec("UPDATE videos SET backup_bad_md5=?,backup_at=NULL WHERE id=?", _hash(row), vid)
            raise BackupError(mismatch)
        db.exec("UPDATE videos SET backup_at=?,backup_bytes=?,backup_folder=?,backup_missed_at=NULL,backup_bad_md5=NULL,backup_src_md5=NULL WHERE id=?",
                now(), row["Size"], fid, vid)
        _remember(vid, row["Size"], _hash(row), fid)
    if (draft_dir(vid) / "full.md").exists():
        _run("copy", str(draft_dir(vid)), f"gdrive:{vid}/draft", "--exclude", "calls/**")
        db.exec("UPDATE videos SET backup_draft_at=?,backup_draft_folder=? WHERE id=?", now(), fid, vid)
    # 读片可能在传输中又完成一次（新截的帧）；保留新的排队标记，下一轮补传。
    db.exec("""UPDATE videos SET backup_due=CASE WHEN backup_gen=? THEN 0 ELSE 1 END,
               backup_error=NULL,backup_failed_at=NULL WHERE id=?""", v["backup_gen"], vid)
    if not db.one("SELECT 1 FROM videos WHERE backup_failed_at IS NOT NULL"):
        _state()


def due() -> None:
    """housekeeping 的短后台任务：直到队列空或登录失效；失败等 RETRY_EVERY，不放弃。"""
    global _uploading
    from .run import RETRY_EVERY
    if not available() or (db.setting("gdrive_state") or {}).get("auth") or not _transfer_lock.acquire(blocking=False):
        return
    try:
        while True:
            if not available() or (db.setting("gdrive_state") or {}).get("auth"):
                break
            retry_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=RETRY_EVERY)).isoformat()
            with media_lock:
                candidates = db.q("""SELECT * FROM videos WHERE backup_due=1 AND (backup_failed_at IS NULL OR backup_failed_at<=?)
                                      ORDER BY created_at,id""", retry_before)
                v = next((v for v in candidates if not uploading(v["id"])), None)
                if not v:
                    break
                _claimed.add(v["id"])
                _uploading = v["id"]
            db.set_setting("gdrive_upload", {"video": _uploading, "started_at": now()})
            try:
                _upload(v)
            except Exception as e:  # 文件丢失、进程错误、磁盘失败都留队列，不影响读片和生产
                _failed(v["id"], e)  # 旧登录的失败在这里被标 stale，不记
                if isinstance(e, BackupError) and (e.stale or e.auth):
                    break
            finally:
                with media_lock:
                    _claimed.discard(v["id"])
                    _uploading = None
                db.set_setting("gdrive_upload", None)
        if available() and not (db.setting("gdrive_state") or {}).get("auth"):
            try:
                refresh_usage()
            except BackupError as e:
                _report(e)
    finally:
        _uploading = None
        _transfer_lock.release()


def restore_source(vid: str) -> Path | None:
    dest = media_dir(vid) / "source.mkv"
    if not available() or dest.exists():
        return None
    try:
        _valid_video(vid)
    except ValueError:
        return None
    with claim(vid) as free:
        if not free:
            return None
        return _restore_source(vid, dest)


def _restore_source(vid: str, dest: Path) -> Path | None:
    _valid_video(vid)
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        for old in dest.parent.glob(".restore-*"):
            if old.is_file():
                old.unlink(missing_ok=True)
        row = _stat(f"{vid}/source.mkv")
        if not row:
            return None
        known = db.one("SELECT backup_bad_md5 FROM videos WHERE id=?", vid)
        if known and known["backup_bad_md5"] and _hash(row) == known["backup_bad_md5"]:
            raise BackupError("网盘原片是此前校验不一致的副本，请在网盘处理后重试")
        fd, name = tempfile.mkstemp(prefix=".restore-", suffix=".mkv", dir=dest.parent)
        os.close(fd); tmp = Path(name)
        _run("copyto", f"gdrive:{vid}/source.mkv", str(tmp))
        if not _hash(row) or _md5(tmp) != _hash(row):
            raise BackupError("取回原片的 md5 校验不一致")
        ffmpeg.probe(tmp)
        try:
            os.link(tmp, dest)  # 原子、不覆盖：下载中途出现的本地原片归原来的写入者
        except FileExistsError:
            return None
        db.exec("UPDATE videos SET restored_at=? WHERE id=?", now(), vid)
        _event(f"{vid} 已从网盘取回原片")
        return dest
    except Exception as e:
        with _locked_conf():
            if _current(e):
                _warn_once(vid, f"从网盘取回原片失败：{redact(e)}")
        return None
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def restore_draft(vid: str) -> bool:
    if not available() or (draft_dir(vid) / "full.md").exists():
        return False
    try:
        _valid_video(vid)
    except ValueError:
        return False
    try:
        if not _stat(f"{vid}/draft/full.md"):
            return False
        d = draft_dir(vid)
        d.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=d.parent) as temp:
            _run("copy", f"gdrive:{vid}/draft", temp, "--exclude", "calls/**")
            if not all((Path(temp) / name).is_file() for name in ("full.md", "index.md", "draft.json")):
                raise BackupError("取回的底稿不完整")
            # full.md 是复用底稿的门槛，最后原子放入；复制中断不留下半份「已取回」。
            shutil.copytree(temp, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("full.md"))
            os.replace(Path(temp) / "full.md", d / "full.md")
        db.exec("UPDATE videos SET draft_restored_at=? WHERE id=?", now(), vid)
        _event(f"{vid} 已从网盘取回底稿")
        return True
    except Exception as e:
        with _locked_conf():
            if _current(e):
                _warn_once(vid, f"从网盘取回底稿失败：{redact(e)}")
        return False


def video_view(v: dict) -> dict:
    fid = _folder().get("folder_id")
    state = ("failed" if v.get("backup_failed_at") else "missed" if v.get("backup_missed_at") else
             "due" if v.get("backup_due") and available() else
             "done" if fid and ((v.get("backup_at") and v.get("backup_folder") == fid) or
                               (v.get("backup_draft_at") and v.get("backup_draft_folder") == fid)) else None)
    return {"state": state, "error": redact(v["backup_error"]) if v.get("backup_error") else None,
            "restored": bool(v.get("restored_at"))}


def topic_view(rows: list[dict]) -> dict | None:
    if not available():
        return None
    errors = sorted((r for r in rows if r.get("backup_failed_at")), key=lambda r: r["backup_failed_at"], reverse=True)
    return {"failed": len(errors), "missed": sum(bool(r.get("backup_missed_at")) for r in rows),
            "error": redact(errors[0]["backup_error"]) if errors else None,
            "auth": bool((db.setting("gdrive_state") or {}).get("auth"))}
