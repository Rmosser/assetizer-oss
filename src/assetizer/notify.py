"""Telegram notifications: what the user must know without opening the 机房.

Two secrets configure it: ``telegram-bot-token`` (from @BotFather) and ``telegram-chat-id``, which
``assetizer telegram-setup`` finds once the user has sent the bot a message. Without them every call is a no-op.
Sending never raises and never logs the token: a notification must not break or leak production.

What is sent: a run that failed, an alert newly raised or resolved (login, quota, agy media, disk, Notion,
memory), the worker coming back from a crash or OOM, the 调试台 queue running dry, and a daily digest.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

import httpx

from . import config, db

API = "https://api.telegram.org/bot{token}/{method}"
TZ = ZoneInfo("Asia/Shanghai")
DIGEST_HOUR = 9  # local time of the daily digest
LIMIT = 4000     # Telegram rejects messages over 4096 characters


class SetupError(RuntimeError):
    pass


def _token() -> str | None:
    return config.get().secret("telegram-bot-token")


def configured() -> bool:
    s = config.get()
    return bool(s.secret("telegram-bot-token") and s.secret("telegram-chat-id"))


def link(path: str = "") -> str:
    host = os.environ.get("ASSETIZER_PUBLIC_HOST", "").strip().lower()
    return f"https://{host}/{path.lstrip('/')}" if host else ""


def _api(token: str, method: str, **payload) -> dict:
    try:
        r = httpx.post(API.format(token=token, method=method), json=payload, timeout=10)
        o = r.json()
    except Exception as e:  # noqa: BLE001 — the exception text would carry the URL, and with it the token
        return {"ok": False, "description": type(e).__name__}
    return o if isinstance(o, dict) else {"ok": False, "description": f"HTTP {r.status_code}"}


def send(text: str) -> bool:
    s = config.get()
    token, chat = s.secret("telegram-bot-token"), s.secret("telegram-chat-id")
    if not (token and chat):
        return False
    o = _api(token, "sendMessage", chat_id=chat, text=text[:LIMIT], disable_web_page_preview=True)
    if not o.get("ok"):
        db.event("notify", f"Telegram 通知没发出去：{str(o.get('description'))[:200]}", level="warn")
    return bool(o.get("ok"))


def setup() -> dict:
    """Find the chat that last messaged the bot, keep its id as the telegram-chat-id secret, send a test."""
    token = _token()
    if not token:
        raise SetupError("还没有 telegram-bot-token：先在 @BotFather 建 bot，把 token 存进 secrets/telegram-bot-token")
    me = _api(token, "getMe")
    if not me.get("ok"):
        raise SetupError(f"bot token 不可用：{me.get('description')}")
    up = _api(token, "getUpdates")
    chats = [m["chat"] for u in up.get("result") or []
             for m in [u.get("message") or u.get("my_chat_member") or {}] if m.get("chat")]
    if not chats:
        raise SetupError(f"还没收到消息：先在 Telegram 里给 @{me['result'].get('username')} 发一句话，再运行一次")
    chat = chats[-1]
    p = config.get().secrets_dir / "telegram-chat-id"
    p.write_text(str(chat["id"]))
    p.chmod(0o600)
    ok = send(f"课代表通知已接通。之后生产失败、告警、worker 异常重启和每日摘要都会发到这里。\n{link()}".strip())
    return {"bot": me["result"].get("username"), "chat_id": chat["id"],
            "chat": chat.get("title") or chat.get("username") or chat.get("first_name"), "test_sent": ok}


# ---------------------------------------------------------------- what is worth a message
def _title(source_id: str) -> str:
    r = db.one("SELECT title FROM sources WHERE id=?", source_id)
    return (r and r["title"]) or source_id


def run_failed(run_id: str, kind: str, error: str) -> None:
    r = db.one("SELECT source_id, playbook_id FROM runs WHERE id=?", run_id)
    if not r:
        return
    pb = db.one("SELECT version FROM playbooks WHERE id=?", r["playbook_id"]) if r["playbook_id"] else None
    version = f"（规程 v{pb['version']}）" if pb else ""
    first = re.sub(r"\s+", " ", (error or "").split("\nTraceback")[0]).strip()[:300]
    send(f"❌ 生产失败：{_title(r['source_id'])}\n{run_id}{version}\n{kind}：{first}\n{link()}".strip())


def alert_raised(severity: str, title: str, body: str) -> None:
    send(f"{'🔴' if severity == 'critical' else '🟠'} {title}\n{body[:1500]}\n{link()}".strip())


def alert_resolved(title: str) -> None:
    send(f"✅ 已恢复：{title}")


def worker_recovered(reason: str, interrupted: list[str]) -> None:
    runs = "、".join(interrupted) if interrupted else "没有进行中的生产"
    send(f"⚠️ Worker 非正常退出后已由 systemd 重启（{reason}）。\n从断点继续：{runs}")


def trials_done(n: int) -> None:
    send(f"🧪 调试台排队的试跑都跑完了（这一批 {n} 个），可以去对比投票。\n{link()}".strip())


def digest() -> str:
    """The last 24 hours in a few lines."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat(timespec="seconds")
    done = {r["production"]: r["n"] for r in db.q(
        "SELECT production, COUNT(*) n FROM runs WHERE finished_at>=? GROUP BY production", since)}
    lines = [f"📋 课代表日报（{dt.datetime.now(TZ):%m-%d %H:%M}）",
             "过去 24 小时：完成 {} · 部分完成 {} · 失败 {}".format(
                 done.get("COMPLETE", 0), done.get("COMPLETE_PARTIAL", 0), done.get("FAILED", 0))]
    run = db.one("SELECT id, stage FROM runs WHERE production='RUNNING' LIMIT 1")
    queued = db.one("SELECT COUNT(*) n FROM runs WHERE production='QUEUED'")["n"]
    trials = db.one("SELECT COUNT(*) n FROM trial_queue WHERE status='PENDING'")["n"]
    waiting = db.one("SELECT COUNT(*) n FROM runs WHERE production='WAITING'")["n"]
    lines.append(f"正在跑：{run['id']}（{run['stage'] or '准备中'}）" if run else "正在跑：无")
    lines.append(f"排队：生产 {queued} · 试跑 {trials}" + (f" · 等待中 {waiting}" if waiting else ""))
    alerts = db.q("SELECT title FROM alerts WHERE resolved_at IS NULL ORDER BY created_at")
    lines.append("未解决告警：" + ("；".join(a["title"] for a in alerts) if alerts else "无"))
    try:
        st = os.statvfs(config.get().home)
        lines.append(f"磁盘剩余：{st.f_bavail * st.f_frsize / 1e9:.0f} GB")
    except OSError:
        pass
    if link():
        lines.append(link())
    return "\n".join(lines)


def digest_due() -> bool:
    t = dt.datetime.now(TZ)
    return t.hour >= DIGEST_HOUR and db.setting("digest_date") != t.date().isoformat()


def send_digest() -> None:
    db.set_setting("digest_date", dt.datetime.now(TZ).date().isoformat())
    send(digest())

