"""按规则共用的频道原片、专题原片和底稿副本（design §R8-8）。

只有这里读写 rclone.conf。网页只得到布尔状态；凭据不经过 setting、事件或子进程参数。
web 与 worker 共用主文件；rclone 用私有目录里的快照，短锁内比较 token 后才合并刷新。
失败只在它用的登录仍是当前登录时才算数，判断和写入在同一把锁内。
GET 不启动 rclone。后台一次传一期，不占读片槽；用户确认的副本只移到 Google 回收站。
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
from . import backup_rules as rules

COMMANDS = frozenset({"copyto", "copy", "lsjson", "about", "mkdir", "purge"})
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
media_lock = threading.RLock()  # worker 内，选中/登记与清理的重新检查、删除共用
_claimed: set[str] = set()
_pending: dict | None = None
_uploading: str | None = None
_delete_lock = threading.Lock()


class BackupError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False, missing: bool = False, stale: bool = False, skipped: bool = False):
        super().__init__(message)
        self.auth, self.missing, self.stale, self.skipped = auth, missing, stale, skipped
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


def _identity_key(data: dict) -> str:
    # 身份持久化只保存摘要，不能把 refresh token/client secret 带进数据库。
    return hashlib.sha256(json.dumps(_identity(data), ensure_ascii=True).encode()).hexdigest()


def _run(command: str, *args: str, expected: tuple[str, str] | None = None) -> str:
    if command != "purge" or expected is None:
        return _invoke(command, *args, expected=expected)
    # 放在调用入口内：等待启动的命令尚未进入最终临界区，成功的规则写能使它跳过。
    with rules.mutation(writer=False):
        vid = args[0].removeprefix("gdrive:")
        # 删除的线性化点：元数据写只能在这次复核之前或意图提交之后生效。
        with db.tx():
            if rules.decision(vid) is not False or not (verified(vid) or verified(vid, draft=True)):
                raise BackupError("规则或副本状态已变化，跳过", skipped=True)
            intent = {"folder": expected[0], "identity": expected[1], "started_at": now()}
            db.exec("UPDATE video_backups SET delete_intent=? WHERE id=?", db.j(intent), vid)
        try:
            result = _invoke(command, *args, expected=expected)
        except BackupError as e:
            if e.skipped:  # 快照未生成，确定没有执行远端操作
                db.exec("UPDATE video_backups SET delete_intent=NULL WHERE id=?", vid)
            if not e.missing or e.auth or e.stale:
                raise
            result = ""
        _finish_delete(vid, "网盘副本已移到回收站")
        return result


def _invoke(command: str, *args: str, expected: tuple[str, str] | None = None) -> str:
    if command not in COMMANDS:
        raise ValueError("网盘命令不在白名单中")
    if command == "purge":
        if len(args) != 2 or not re.fullmatch(r"gdrive:[A-Za-z0-9_-]{11}", args[0]) or args[1] != "--drive-use-trash=true":
            raise ValueError("删除只允许视频目录，并且必须进回收站")
    d = config.get().secrets_dir
    run_dir = lease = None
    lease_fd = None
    data, original, identity = {}, None, None
    try:
        with _locked_conf():
            _cleanup_runs(d)
            data = _read_conf()
            original, identity = data.get("token"), _identity(data)
            if expected is not None and (data.get("root_folder_id"), _identity_key(data)) != expected:
                raise BackupError("备份文件夹或登录身份已变化，跳过", stale=True, skipped=True)
            if expected is not None and _folder().get("folder_id") != expected[0]:
                raise BackupError("备份文件夹已变化，跳过", stale=True, skipped=True)
            if command == "purge" and (not _folder().get("folder_id") or data.get("root_folder_id") != _folder()["folder_id"]):
                raise BackupError("备份文件夹与配置不一致")
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
                               timeout=TRANSFER_TIMEOUT if command in {"copy", "copyto", "purge"} else QUERY_TIMEOUT)
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


def _lease_path(name: str) -> Path:
    return config.get().home / "cache" / "gdrive-locks" / (name + ".lock")


def _busy(name: str) -> bool:
    p = _lease_path(name)
    if not p.exists():
        return False
    try:
        with p.open("rb") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True  # 判断不了也保护媒体


def uploading(vid: str) -> bool:
    try:
        _valid_video(vid)
    except ValueError:
        return False
    return _uploading == vid or vid in _claimed or _busy(vid)


@contextlib.contextmanager
def claim(vid: str):
    """登记媒体使用权；flock 让 web 的删除与 worker 的上传、取回跨进程互斥。"""
    _valid_video(vid)
    fd = None
    with media_lock:
        free = not uploading(vid)
        if free:
            p = _lease_path(vid)
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _claimed.add(vid)
            except OSError:
                os.close(fd); fd = None; free = False
    try:
        yield free
    finally:
        if free:
            with media_lock:
                _claimed.discard(vid)
                os.close(fd)


@contextlib.contextmanager
def retention_claim(vid: str):
    # 未配置时不新建锁文件；仍尊重断开前已开始的传输。历史 ID 不用来构造远端路径。
    if not available() or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        yield not uploading(vid)
    else:
        with claim(vid) as free:
            yield free


def require_ready() -> None:
    if not available():
        raise FileExistsError("请先完成网盘客户端、登录和文件夹设置")


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
    records = [v for v in _copies() if verified(v["id"])]
    usage = {"videos": len(records), "bytes": sum(r["backup_bytes"] or 0 for r in records), "drive_free": cached.get("free"),
             "drive_total": cached.get("total"), "checked_at": cached.get("checked_at")} if cached else None
    queue = db.one("""SELECT COALESCE(SUM(backup_due),0) due,
                      COUNT(backup_failed_at) failed, COUNT(backup_missed_at) missed FROM video_backups""")
    if not available():
        queue["due"] = 0
    vid = _uploading or (db.setting("gdrive_upload") or {}).get("video")
    title = rules.metadata(vid).get("title") if vid else None
    rv = rules.view() if available() else None
    counts = None
    if rv:
        authors = {k: 0 for k in ("on", "off", "pending")}
        topics = dict(authors)
        for a in rv["authors"]:
            authors["pending" if a["member"] is None or a["public"] is None else "on" if a["member"] or a["public"] else "off"] += 1
        for t in rv["topics"]:
            topics["pending" if t["backup"] is None else "on" if t["backup"] else "off"] += 1
        counts = {"authors": authors, "topics": topics}
    return {"rclone": binary, "client": client, "token": token, "folder": folder, "state": s,
            "error": redact(state["error"]) if state.get("error") else None, "login": _login_view(),
            "usage": usage, "queue": queue, "uploading": {"id": vid, "title": title} if vid else None,
            "rules": counts, "deletable": deletable()["total"] if available() else None,
            "deleting": db.setting("gdrive_delete") if _busy("batch") else None}


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
        pending_alert()
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
            rules.reconcile()
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


def _failed(vid: str, error: object) -> None:
    """记一期的失败；判断归属和写入在同一把锁内（A15）。旧登录的失败不记，这一期留在队列里。"""
    _valid_video(vid)
    with _locked_conf():
        if not _current(error):
            return
        message = redact(error)[:1500]
        db.exec("UPDATE video_backups SET backup_error=?,backup_failed_at=? WHERE id=?", message, now(), vid)
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
    local = rules.source_path(vid)
    if not row and local:
        digest = _md5(local)
        pending_md5 = digest
        # 网盘上没有这一份：旧的确认（换文件夹前、或用户删掉远端前）不再成立，本次上传核对前不算已备份。
        db.exec("UPDATE video_backups SET backup_src_md5=?,backup_at=NULL,backup_folder=NULL WHERE id=?", digest, vid)
        # copyto 不理 --immutable（只有 copy/sync 检查它；2026-11-10 devbox 实测会覆盖）。--ignore-existing 让核对后
        # 才出现的远端同名原片原样留着，下面的 md5 核对再把它当作不一致报出来。
        _run("copyto", str(local), f"gdrive:{vid}/source.mkv", "--ignore-existing")
        row = _stat(f"{vid}/source.mkv")
        if not row or _hash(row) != digest:
            db.exec("UPDATE video_backups SET backup_bad_md5=?,backup_at=NULL WHERE id=?", _hash(row) if row else None, vid)
            raise BackupError(mismatch)
    if row:
        if not _hash(row):
            raise BackupError("网盘没有返回原片的 md5")
        if pending_md5 and _hash(row) != pending_md5:
            db.exec("UPDATE video_backups SET backup_bad_md5=?,backup_at=NULL WHERE id=?", _hash(row), vid)
            raise BackupError(mismatch)
        db.exec("UPDATE video_backups SET backup_at=?,backup_bytes=?,backup_folder=?,backup_md5=?,backup_missed_at=NULL,backup_bad_md5=NULL,backup_src_md5=NULL WHERE id=?",
                now(), row["Size"], fid, _hash(row), vid)
    if (draft_dir(vid) / "full.md").exists():
        _run("copy", str(draft_dir(vid)), f"gdrive:{vid}/draft", "--exclude", "calls/**")
        files = _json("lsjson", f"gdrive:{vid}/draft", "--recursive", "--files-only")
        size = sum(max(0, r.get("Size", 0)) for r in files if not r.get("IsDir"))
        db.exec("UPDATE video_backups SET backup_draft_at=?,backup_draft_folder=?,backup_draft_bytes=? WHERE id=?", now(), fid, size, vid)
    # 读片可能在传输中又完成一次（新截的帧）；保留新的排队标记，下一轮补传。
    db.exec("""UPDATE video_backups SET backup_due=CASE WHEN backup_gen=? THEN 0 ELSE 1 END,
               backup_error=NULL,backup_failed_at=NULL WHERE id=?""", v["backup_gen"], vid)
    if not db.one("SELECT 1 FROM video_backups WHERE backup_failed_at IS NOT NULL"):
        _state()


def due() -> None:
    """独立后台串行上传；每期开始前再判定规则。"""
    global _uploading
    from .run import RETRY_EVERY
    if not available() or (db.setting("gdrive_state") or {}).get("auth") or not _transfer_lock.acquire(blocking=False):
        return
    try:
        recover_deletions()
        rules.sync()
        pending_alert()
        while available() and not (db.setting("gdrive_state") or {}).get("auth"):
            retry_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=RETRY_EVERY)).isoformat()
            candidates = db.q("""SELECT * FROM video_backups WHERE backup_due=1 AND delete_intent IS NULL AND (backup_failed_at IS NULL OR backup_failed_at<=?)
                                  ORDER BY id""", retry_before)
            v = next((v for v in candidates if not uploading(v["id"])), None)
            if not v:
                break
            try:
                _valid_video(v["id"])
            except ValueError:
                db.exec("UPDATE video_backups SET backup_due=0 WHERE id=?", v["id"])
                continue
            with claim(v["id"]) as free:
                if not free:
                    continue
                if rules.decision(v["id"]) is not True:
                    db.exec("UPDATE video_backups SET backup_due=0 WHERE id=?", v["id"])
                    continue
                _uploading = v["id"]
                db.set_setting("gdrive_upload", {"video": _uploading, "started_at": now()})
                try:
                    _upload(v)
                except Exception as e:
                    _failed(v["id"], e)
                    if isinstance(e, BackupError) and (e.stale or e.auth):
                        break
                finally:
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


def restore_source(vid: str, dest: Path | None = None, *, min_height: int = 0) -> Path | None:
    dest = dest if dest is not None else media_dir(vid) / "source.mkv"
    if not available() or dest.exists():
        return None
    try:
        _valid_video(vid)
    except ValueError:
        return None
    with claim(vid) as free:
        if not free:
            return None
        return _restore_source(vid, dest, min_height=min_height)


def _restore_source(vid: str, dest: Path, *, min_height: int = 0) -> Path | None:
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
        known = db.one("SELECT backup_bad_md5 FROM video_backups WHERE id=?", vid)
        if known and known["backup_bad_md5"] and _hash(row) == known["backup_bad_md5"]:
            raise BackupError("网盘原片是此前校验不一致的副本，请在网盘处理后重试")
        fd, name = tempfile.mkstemp(prefix=".restore-", suffix=".mkv", dir=dest.parent)
        os.close(fd); tmp = Path(name)
        _run("copyto", f"gdrive:{vid}/source.mkv", str(tmp))
        if not _hash(row) or _md5(tmp) != _hash(row):
            raise BackupError("取回原片的 md5 校验不一致")
        pr = ffmpeg.probe(tmp)
        if min_height and not any(x.get("codec_type") == "video" and (x.get("height") or 0) >= min_height for x in pr["streams"]):
            raise BackupError("网盘原片高度不足 " + str(min_height))
        try:
            os.link(tmp, dest)  # 原子、不覆盖：下载中途出现的本地原片归原来的写入者
        except FileExistsError:
            return None
        _record_restore(vid, "restored_at")
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
    if not available():
        return False
    try:
        _valid_video(vid)
    except ValueError:
        return False
    with claim(vid) as free:
        return _restore_draft(vid) if free else False


def _restore_draft(vid: str) -> bool:
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
        _record_restore(vid, "draft_restored_at")
        _event(f"{vid} 已从网盘取回底稿")
        return True
    except Exception as e:
        with _locked_conf():
            if _current(e):
                _warn_once(vid, f"从网盘取回底稿失败：{redact(e)}")
        return False


def _record_restore(vid: str, field: str) -> None:
    # 文件已校验并原子落盘；辅助归集失败不撤销取回，后台 sync 会补齐事实。
    db.exec(f"INSERT INTO video_backups(id,{field}) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET {field}=excluded.{field}", vid, now())
    try:
        rules.track(vid)
    except Exception as e:
        _warn_once(vid, f"取回后归集元数据失败：{redact(e)}")


def video_view(v: dict) -> dict:
    v = db.one("SELECT * FROM video_backups WHERE id=?", v.get("id") or v.get("video_id")) or {}
    fid = _folder().get("folder_id")
    state = ("failed" if v.get("backup_failed_at") else "missed" if v.get("backup_missed_at") else
             "due" if v.get("backup_due") and available() else
             "done" if v.get("id") and (verified(v["id"]) or verified(v["id"], draft=True)) else None)
    return {"state": state, "error": redact(v["backup_error"]) if v.get("backup_error") else None,
            "restored": bool(v.get("restored_at"))}


def topic_view(rows: list[dict]) -> dict | None:
    if not available():
        return None
    rows = [db.one("SELECT * FROM video_backups WHERE id=?", r.get("id") or r.get("video_id")) or {} for r in rows]
    errors = sorted((r for r in rows if r.get("backup_failed_at")), key=lambda r: r["backup_failed_at"], reverse=True)
    return {"failed": len(errors), "missed": sum(bool(r.get("backup_missed_at")) for r in rows),
            "error": redact(errors[0]["backup_error"]) if errors else None,
            "auth": bool((db.setting("gdrive_state") or {}).get("auth"))}


def verified(vid: str, *, draft: bool = False) -> bool:
    v = db.one("SELECT * FROM video_backups WHERE id=?", vid) or {}
    prefix = "backup_draft" if draft else "backup"
    return bool(v.get(prefix + "_at") and v.get(prefix + "_folder") == _folder().get("folder_id")
                and not v.get("delete_intent") and not (v.get("backup_bad_md5") or v.get("backup_src_md5")))


def pending_alert() -> None:
    """仅刊首页；实际期限来自 backup_retention.plan，不另算保留/宽限。"""
    import math
    from .backup_retention import plan
    key = "BACKUP_RULES_PENDING"
    pending = {}
    if available():
        for v in db.q("SELECT id FROM videos WHERE media_state='CACHED' UNION SELECT id FROM sources WHERE media_state='CACHED'"):
            vid = v["id"]
            p = plan(vid)
            if p["pending"] and p["in_grace"] and not uploading(vid) and rules.source_path(vid):
                pending[vid] = max(0, math.ceil((p["media_at"] - p["clock"]).total_seconds() / 86400))
    if pending:
        topic_pending = any(any(r["kind"] == "topic" and r["value"] is None for r in rules.applicable(vid)) for vid in pending)
        suffix = "备份规则还没定" if topic_pending else "作者还没定"
        db.raise_alert(key, "info", f"{len(pending)} 期原片 {min(pending.values())} 天后清理，{suffix}",
                       action={"label": "去设置", "href": "/settings#gdrive"})
    else:
        db.resolve_alert(key)


def _copies() -> list[dict]:
    fid = _folder().get("folder_id")
    if not fid:
        return []
    return [v for v in db.q("SELECT * FROM video_backups ORDER BY id") if verified(v["id"]) or verified(v["id"], draft=True)]


def deletable() -> dict:
    if not available():
        return {"ready": False}
    authors, last, kept = {}, [], {}
    for v in _copies():
        vid = v["id"]
        try:
            _valid_video(vid)
        except ValueError:
            continue
        if uploading(vid):
            continue
        choice = rules.decision(vid)
        m = rules.metadata(vid)
        author = {"id": m["youtube_channel_id"], "name": m["author_name"] or m["youtube_channel_id"]}
        size = ((v["backup_bytes"] or 0) if verified(vid) else 0) + ((v["backup_draft_bytes"] or 0) if verified(vid, draft=True) else 0)
        item = {"id": vid, "title": m["title"] or vid, "bytes": size}
        if choice is False:
            local = rules.source_path(vid)
            # 历史元数据不能证明 YouTube 现在仍可用；没有本机原片时保守默认不勾。
            if local is None:
                last.append({**item, "author": author})
            else:
                group = authors.setdefault(author["id"], {**author, "bytes": 0, "videos": []})
                group["videos"].append(item); group["bytes"] += size
        elif choice is True:
            applicable = rules.applicable(vid)
            if any(r["value"] == 0 for r in applicable):
                on = [r for r in applicable if r["value"] == 1]
                # applicable 的专题按 id 排序，有专题就归最小 id 的那条传规则。
                keep = next((r for r in on if r["kind"] == "topic"), on[0])
                k = (keep["kind"], keep["id"])
                group = kept.setdefault(k, {"topic": {"id": keep["id"], "question": keep["question"]}, "videos": 0}
                                        if keep["kind"] == "topic" else {"author": author, "videos": 0})
                group["videos"] += 1
    groups = sorted(authors.values(), key=lambda a: a["id"])
    return {"ready": True, "authors": groups, "last_copy": last, "kept": list(kept.values()),
            "total": {"videos": sum(len(a["videos"]) for a in groups) + len(last),
                      "bytes": sum(a["bytes"] for a in groups) + sum(v["bytes"] for v in last)}}


def start_delete(videos: list[str]) -> dict:
    require_ready()
    if not isinstance(videos, list) or not videos:
        raise ValueError("请选择要删除的视频")
    for vid in videos:
        _valid_video(vid)
    videos = list(dict.fromkeys(videos))
    if not _delete_lock.acquire(blocking=False):
        raise FileExistsError("已有一批副本正在删除")
    fd = None
    try:
        path = _lease_path("batch")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FileExistsError("已有一批副本正在删除") from None
        db.set_setting("gdrive_delete", {"total": len(videos), "done": 0})
        with _locked_conf():
            expected = (_folder()["folder_id"], _identity_key(_read_conf()))
        result = view()  # 202 一定带启动时的进度，即便测试网盘瞬间完成
        th = threading.Thread(target=_delete_batch, args=(videos, expected, fd), name="gdrive-delete", daemon=True)
        th.start()
        fd = None  # 后台持有租约直到结束
        return result
    except BaseException:
        if fd is not None:
            os.close(fd)
        _delete_lock.release()
        raise


def _delete_batch(videos: list[str], expected: tuple[str, str], fd: int) -> None:
    fid = expected[0]
    try:
        for i, vid in enumerate(videos):
            if not available() or _folder().get("folder_id") != fid or (db.setting("gdrive_state") or {}).get("auth"):
                break
            with claim(vid) as free:
                if not free or rules.decision(vid) is not False or not (verified(vid) or verified(vid, draft=True)):
                    _event(f"{vid} 已不可删除，跳过", data={"video": vid})
                else:
                    try:
                        _run("purge", "gdrive:" + vid, "--drive-use-trash=true", expected=expected)
                    except Exception as e:
                        if isinstance(e, BackupError) and e.skipped:
                            _event(f"{vid} 已不可删除，跳过：{redact(e)}", data={"video": vid})
                        with _locked_conf():
                            if _current(e) and not (isinstance(e, BackupError) and e.skipped):
                                _warn_once(vid, "删除网盘副本失败：" + redact(e))
                                _report(e)
                        if isinstance(e, BackupError) and (e.auth or e.stale):
                            break
            db.set_setting("gdrive_delete", {"total": len(videos), "done": i + 1})
            rules.yield_writers()
    finally:
        db.set_setting("gdrive_delete", None)
        os.close(fd)
        _delete_lock.release()


def _clear_copy(vid: str) -> None:
    db.exec("""UPDATE video_backups SET backup_at=NULL,backup_bytes=NULL,backup_folder=NULL,backup_md5=NULL,backup_draft_bytes=0,
               backup_draft_at=NULL,backup_draft_folder=NULL,backup_due=0,backup_src_md5=NULL,delete_intent=NULL,
               backup_bad_md5=NULL,backup_error=NULL,backup_failed_at=NULL,backup_missed_at=NULL WHERE id=?""", vid)


def _finish_delete(vid: str, message: str) -> None:
    # 清意图、提醒和重新排队一起提交：中途被杀则意图还在，后台核对会重做整段收尾。
    with db.tx():
        _clear_copy(vid)
        _event(f"{vid} {message}", data={"video": vid})
        if rules.decision(vid) is True:
            _warn_once(vid, "刚移到网盘回收站，但现在规则是传，30 天内可在 Google 网盘回收站恢复")
        rules.queue(vid)


def recover_deletions() -> None:
    """只核对，不重放 purge。身份/文件夹变更或核对失败均保持未决。"""
    if not available() or (db.setting("gdrive_state") or {}).get("auth"):
        return
    for v in db.q("SELECT id,delete_intent FROM video_backups WHERE delete_intent IS NOT NULL"):
        if (db.setting("gdrive_state") or {}).get("auth"):
            break
        vid = v["id"]
        try:
            with claim(vid) as free:
                if not free:
                    continue
                with rules.mutation(writer=False):
                    current = db.one("SELECT delete_intent FROM video_backups WHERE id=?", vid)
                    intent = db.unj(current["delete_intent"], None) if current else None
                    if not intent:
                        continue
                    if intent.get("retry_at", 0) > time.time():
                        continue
                    # 先持久退避，连核对中途再次被杀也不会每轮重查。
                    intent["attempts"] = min(intent.get("attempts", 0) + 1, 6)
                    intent["retry_at"] = time.time() + min(30, 2 ** (intent["attempts"] - 1)) * 60
                    db.exec("UPDATE video_backups SET delete_intent=? WHERE id=?", db.j(intent), vid)
                    expected = (intent["folder"], intent["identity"])
                    try:
                        raw = _run("lsjson", "gdrive:" + vid, "--stat", expected=expected)
                    except BackupError as e:
                        if not e.missing or e.auth or e.stale:
                            raise
                        _finish_delete(vid, "已核对中断的删除：目录已不在")
                    else:
                        try:
                            entry = json.loads(raw)
                        except (TypeError, ValueError):
                            raise BackupError("目录核对响应无效") from None
                        if (not isinstance(entry, dict) or entry.get("IsDir") is not True or
                                (entry.get("Name") or str(entry.get("Path", "")).rstrip("/").rsplit("/", 1)[-1]) != vid):
                            raise BackupError("目录核对响应无效")
                        db.exec("UPDATE video_backups SET delete_intent=NULL WHERE id=?", vid)
                        _event(f"{vid} 已核对中断的删除：目录仍在", data={"video": vid})
        except Exception as e:
            _warn_once(vid, "删除状态未决，保留本地内容：" + redact(e))


def deletable_author(aid: str) -> dict:
    listing = deletable()
    if not listing.get("ready"):
        return {"videos": 0, "bytes": 0}
    regular = [v for a in listing["authors"] if a["id"] == aid for v in a["videos"]]
    last = [v for v in listing["last_copy"] if v["author"]["id"] == aid]
    return {"videos": len(regular) + len(last), "bytes": sum(v["bytes"] for v in regular + last)}
