"""Signing agy in from the 机房 page, without a terminal on the box.

agy's own sign-in (Google OAuth) runs in its full-screen interface: pick "Google OAuth", open the accounts.google.com
link it prints, sign in, and paste the authorization code that antigravity.google/oauth-callback shows back into it.
The print mode offers the same paste but gives up after 60 s, too short for opening a link on a phone and coming back;
the interface waits (still waiting after 140 s, agy 1.2.16). So the web process runs the interface in a pseudo-terminal
with the production HOME — the credentials land on the shared volume the worker reads — presses Enter on the first
login method, takes the link from a stand-in browser opener (the screen wraps it; parsing the screen is the fallback),
and types the pasted code. Success is decided by `agy -p /usage` answering, never by reading the screen; the interface
is then killed (SIGKILL: no cancel handler gets to undo a half-finished sign-in).

One session at a time. The code goes to the terminal only: it is not logged, stored or sent back.
"""
from __future__ import annotations

import atexit
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import urllib.parse
from typing import Any

from .. import config, db
from ..util import now

MENU = re.compile(r"Select (?:a )?(?:login|sign-in) method|Continue with Google|currently not signed in", re.I)
URL = re.compile(r"https://accounts\.google\.com/o/oauth2/[^\s\"'<>]+")
REFUSED = re.compile(r"invalid_grant|invalid (?:authorization )?code|authentication failed|could not (?:sign|log) in|"
                     r"token exchange failed|bad request", re.I)
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=$]*[ -/]*[@-~]|\x1b[@-_]")
TTL = 600          # seconds a link stays good on our side
START_WAIT = 40    # seconds for the menu and then the link to show up
VERIFY_WAIT = 60   # seconds after the code for /usage to start answering
SETTLE = 3.0       # seconds between typing the code and the first /usage, and between two /usage

_lock = threading.Lock()
_session: "Session | None" = None


def _probe() -> dict:
    from .agy import AgyProvider
    return AgyProvider().usage()


def agy_answers() -> None:
    from ..pipeline.runner import agy_answers as lift
    lift()


class Session:
    """state: checking → starting → url → verifying → done; or already / failed / expired / cancelled."""

    def __init__(self) -> None:
        self.state = "checking"
        self.url: str | None = None
        self.error: str | None = None
        self.started = time.time()
        self.expires: float | None = None
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.fd: int | None = None
        self.screen = ""          # the interface's text, escape sequences removed (last 64 KB)
        self.total = 0            # characters of it ever received (screen keeps only the tail)
        self.code: str | None = None
        self.quota: dict | None = None
        self.hook = config.get().home / "run" / "agy-login"
        self.thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._io = threading.Lock()  # the terminal's fd: written here, closed only by its reader

    # ------------------------------------------------------------ view
    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "error": self.error, "started_at": self.started}
        if self.state == "url":
            out.update(url=self.url, expires_in=max(0, round((self.expires or 0) - time.time())))
        if self.state in ("done", "already") and self.quota:
            out["quota"] = self.quota
        return out

    @property
    def live(self) -> bool:
        return self.state in ("checking", "starting", "url", "verifying")

    # ------------------------------------------------------------ the run
    @property
    def cancelled(self) -> bool:
        if self.state == "cancelled":
            self._kill()  # whatever was started meanwhile goes too
            return True
        return False

    def run(self) -> None:
        try:
            q = _probe()
            if self.cancelled:
                return
            if q.get("ok"):
                self.quota = q
                self._finish("already")
                return
            if q.get("error") in ("PROVIDER_TIMEOUT", "PROVIDER_TRANSIENT"):
                # a signed-in agy must not be driven through a login screen: only "not signed in" opens it
                return self._fail("agy 暂时没有回应（网络或超时），稍后再试")
            self.state = "starting"
            self._spawn()
            if not self._until(lambda: MENU.search(self.screen), START_WAIT) or self.cancelled:
                return self._fail("agy 没有显示登录选项")
            time.sleep(0.6)
            self._type("\r")  # the first method: Google OAuth (or "Continue with Google", the one used last)
            if not self._until(lambda: self._link(), START_WAIT) or self.cancelled:
                return self._fail("agy 没有给出授权链接")
            self.url = self._link()
            self.expires = time.time() + TTL
            self.state = "url"
            while True:
                if self._wake.wait(timeout=1.0):
                    self._wake.clear()
                if self.cancelled:
                    return
                if self.code is not None:
                    return self._submit()
                if time.time() > (self.expires or 0):
                    self.state, self.error = "expired", "链接已过期"
                    self._kill()
                    return
                if not self._alive():
                    return self._fail("agy 已退出")
        except Exception as e:  # noqa: BLE001 — a session must end in a state, not hang in "starting"
            self._fail(f"登录出错：{type(e).__name__}")

    def _submit(self) -> None:
        code, self.code = self.code, None
        self.state = "verifying"
        mark = self.total
        self._type((code or "") + "\r")
        deadline = time.time() + VERIFY_WAIT
        time.sleep(SETTLE)
        while time.time() < deadline and not self.cancelled:
            q = _probe()  # answering decides; the screen only says early that the code was refused
            if q.get("ok"):
                self.quota = q
                return self._finish("done")
            since = self.screen[-(self.total - mark):] if self.total > mark else ""  # what agy said after the code
            if REFUSED.search(since):
                return self._fail("授权码没被接受，重新获取链接再试")
            time.sleep(SETTLE)
        self._fail("没能确认登录成功")

    # ------------------------------------------------------------ terminal
    def _spawn(self) -> None:
        shutil.rmtree(self.hook, ignore_errors=True)
        (self.hook / "bin").mkdir(parents=True)
        (self.hook / "cwd").mkdir()
        opener = self.hook / "bin" / "open-url"
        opener.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" > '{self.hook / 'url'}'\n")
        opener.chmod(0o700)
        for name in ("xdg-open", "x-www-browser", "www-browser", "sensible-browser", "open"):
            (self.hook / "bin" / name).symlink_to(opener)
        env = {**os.environ, "TERM": "xterm-256color", "BROWSER": str(opener),
               "PATH": f"{self.hook / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "AGY_CLI_HIDE_LOGO": "1", "AGY_CLI_DISABLE_ESCAPE_SEQUENCE_OPTIMIZATIONS": "1",
               "GOGC": "50", "GOMEMLIMIT": "512MiB"}  # it shares the web container's memory
        # Popen, not pty.fork: the web process is threaded, and only Popen's exec path is safe after a fork there. agy's
        # interface needs a terminal on its standard streams, not a controlling one (checked with agy 1.2.16).
        fd, tty = pty.openpty()
        fcntl.ioctl(tty, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))  # wide: the link wraps less
        try:
            p = subprocess.Popen([config.get().agy_bin], stdin=tty, stdout=tty, stderr=tty, cwd=self.hook / "cwd", env=env,
                                 start_new_session=True, close_fds=True)
        except OSError:
            os.close(fd)
            raise
        finally:
            os.close(tty)
        self.proc, self.pid, self.fd = p, p.pid, fd
        threading.Thread(target=self._read, args=(fd,), name="agy-login-read", daemon=True).start()

    def _read(self, fd: int) -> None:
        """Drain the terminal (a full one would stall agy) into `screen` until the session lets go of it, then close it:
        only this thread closes the fd, so no read or write ever reaches a number the process has since reused."""
        try:
            while self.fd == fd:
                try:
                    r, _, _ = select.select([fd], [], [], 0.5)
                    chunk = os.read(fd, 65536) if r else b""
                except OSError:
                    return
                if r and not chunk:
                    return
                if chunk and self.fd == fd:
                    text = ANSI.sub(" ", chunk.decode("utf-8", "replace"))
                    self.total += len(text)
                    self.screen = (self.screen + text)[-65536:]
        finally:
            with self._io:
                if self.fd == fd:
                    self.fd = None
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _type(self, text: str) -> None:
        with self._io:
            if self.fd is not None:
                os.write(self.fd, text.encode())

    def _link(self) -> str | None:
        f = self.hook / "url"
        if f.exists():
            m = URL.search(f.read_text())
            if m:
                return m[0]
        # the screen wraps the link over indented lines: glue the last occurrence back together
        i = self.screen.rfind("https://accounts.google.com/o/oauth2/")
        if i < 0:
            return None
        tail = re.split(r"\n\s*\n|Copy and paste|Click|paste the authorization", self.screen[i:])[0]
        m = URL.match(re.sub(r"\s+", "", tail))
        return m[0] if m and "state=" in m[0] else None

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _until(self, ok, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if ok():
                return True
            if not self._alive():
                return False
            time.sleep(0.3)
        return bool(ok())

    def _kill(self) -> None:
        with self._io:  # the reader sees the fd let go and closes it
            proc, self.proc, self.pid, self.fd = self.proc, None, None, None
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # its own session: agy and whatever it started
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.screen = ""  # it holds the typed code (the terminal echoes it)
        shutil.rmtree(self.hook, ignore_errors=True)

    # the state changes last: a page that sees "done" or "failed" sees everything that goes with it already written
    def _fail(self, why: str) -> None:
        if self.state == "cancelled":
            return
        self._kill()
        db.event("config", f"网页登录 agy 没成功：{why}", level="warn")
        self.state, self.error = "failed", why

    def _finish(self, state: str) -> None:
        from .agy import record_usage
        if self.state == "cancelled":
            return
        self._kill()
        if self.quota:
            self.quota = record_usage(self.quota)
        db.set_setting("agy_health", {"ok": True, "model": config.get().agy_model, "checked_at": now()})
        if state == "done":
            db.event("config", "已在网页登录 agy")
        agy_answers()  # what waited for agy's sign-in goes on; a pause for magpie's or 兔子's key stays
        self.state = state


def code_of(text: str) -> str | None:
    """What was pasted: the code itself, or the whole callback address it came on (…/oauth-callback?code=…&state=…)."""
    t = (text or "").strip()
    if "code=" in t or t.lower().startswith("http"):
        t = (urllib.parse.parse_qs(urllib.parse.urlsplit(t).query).get("code") or [""])[0]
    t = t.strip()
    return t if 8 <= len(t) <= 1024 and re.fullmatch(r"[\x21-\x7e]+", t) else None


# ---------------------------------------------------------------- the web endpoints' side
def start() -> dict:
    global _session
    with _lock:
        if _session is None:
            atexit.register(shutdown)  # the web stopping takes a sign-in in progress with it
        if _session is None or not _session.live:
            _session = Session()
            _session.thread = threading.Thread(target=_session.run, name="agy-login", daemon=True)
            _session.thread.start()
        return _session.view()


def current() -> dict:
    with _lock:
        s = _session
    if s is None:
        return {"state": "idle"}
    if s.state == "url" and time.time() > (s.expires or 0):
        s._wake.set()
    return s.view()


def submit(text: str) -> dict:
    code = code_of(text)
    with _lock:
        s = _session
    if s is None or s.state != "url":
        raise ValueError("没有在等授权码的登录，重新获取链接")
    if code is None:
        raise ValueError("这不像授权码：复制授权页上的那串代码，或整段回调地址")
    s.code = code  # the session thread types it and checks; the page keeps polling
    s._wake.set()
    return {**s.view(), "state": "verifying"}


def cancel() -> dict:
    with _lock:
        s = _session
    if s is not None and s.live:
        s.state, s.error = "cancelled", None
        s._kill()
    return current()


def shutdown() -> None:
    with _lock:
        s = _session
    if s is not None:
        if s.live:
            s.state = "cancelled"
        s._kill()
        if s.thread is not None:
            s.thread.join(timeout=15)
