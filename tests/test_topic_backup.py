"""专题网盘备份：真实子进程协议，网盘、凭据和 OAuth 全是临时替身。"""
import dataclasses
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from test_topics import Worker, home, stand_in  # noqa: F401 — 共用真实读片替身

VID = "abcdefghijk"
OTHER = "lmnopqrstuv"
CLIENT_SECRET = "synthetic-client-secret"
TOKEN = {"access_token": "ya29.synthetic-access", "refresh_token": "1//synthetic-refresh", "token_type": "Bearer",
         "expiry": "2099-01-01T00:00:00+00:00"}

FAKE = r'''#!PYTHON
import configparser, hashlib, json, os, shutil, sys, time
from pathlib import Path
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f: f.write(json.dumps(a) + "\n")
assert a[0] == "--config"
c = configparser.ConfigParser(interpolation=None); c.read(a[1]); c = c["gdrive"]
cmd, args = a[2], a[3:]
assert cmd in {"copyto", "copy", "lsjson", "about", "mkdir", "purge"}
fail = os.environ.get("FAKE_FAIL", "")
if fail and (os.environ.get("FAKE_FAIL_CMD", "") in ("", cmd)):
    print(fail, file=sys.stderr); sys.exit(1)
root = Path(os.environ["FAKE_DRIVE"])
base = root / c["root_folder_id"] if c.get("root_folder_id") else root
def path(s): return base / s[len("gdrive:"):] if s.startswith("gdrive:") else Path(s)
def save(conf):
    # 照 rclone 1.75 configfile.Save：同目录写 <conf><随机>，旧文件改名 <conf>.old<随机>，再换上新文件。
    crash, tmp, old = os.environ.get("FAKE_SAVE_CRASH", ""), a[1] + "4815162342", a[1] + ".old4815162342"
    with os.fdopen(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f: conf.write(f)  # os.CreateTemp 是 0600
    if crash == "new_written": die()
    os.rename(a[1], old)
    if crash == "old_renamed": die()
    os.rename(tmp, a[1]); os.unlink(old)
def die():  # 模拟调用 rclone 的 Python 进程在 rclone 写配置的半路被杀
    import signal; os.kill(os.getppid(), signal.SIGKILL); sys.exit(9)
def row(p):
    return {"Name": p.name, "Path": p.name, "ID": str(p.relative_to(root)), "IsDir": p.is_dir(),
            "Size": 0 if p.is_dir() else p.stat().st_size,
            "Hashes": {} if p.is_dir() else {"md5": "bad" if os.environ.get("FAKE_BAD_MD5") else hashlib.md5(p.read_bytes()).hexdigest()}}
if cmd == "about": print(json.dumps({"total": 30000000000000, "used": 123, "free": 29999999999877}))
elif cmd == "mkdir": path(args[0]).mkdir(parents=True, exist_ok=True)
elif cmd == "lsjson":
    p = path(args[0])
    marker = os.environ.get("FAKE_POST_COPY_STAT")
    if marker and Path(marker).exists() and "--stat" in args:
        Path(marker).unlink(); print("network stat unavailable", file=sys.stderr); sys.exit(1)
    if not p.exists(): print("object not found", file=sys.stderr); sys.exit(3)
    entries = sorted(p.rglob("*") if "--recursive" in args else p.iterdir()) if p.is_dir() else []
    print(json.dumps(row(p) if "--stat" in args else [row(x) for x in entries
          if ("--dirs-only" not in args or x.is_dir()) and ("--files-only" not in args or x.is_file())]))
elif cmd == "purge":
    import re
    assert len(args) == 2 and re.fullmatch(r"gdrive:[A-Za-z0-9_-]{11}", args[0])
    assert args[1] == "--drive-use-trash=true" and c.get("root_folder_id")
    gate = os.environ.get("FAKE_GATE")
    if gate and os.environ.get("FAKE_GATE_CMD") == "purge":
        Path(gate + ".started").touch()
        until = time.monotonic() + 12
        while not Path(gate + ".release").exists():
            if time.monotonic() > until: raise RuntimeError("fake gate timed out")
            time.sleep(.01)
        if os.environ.get("FAKE_GATE_ERROR"):
            print(os.environ["FAKE_GATE_ERROR"], file=sys.stderr); sys.exit(1)
    p = path(args[0])
    if not p.exists(): print("directory not found", file=sys.stderr); sys.exit(3)
    trash = root / ".trash" / c["root_folder_id"] / (p.name + "-" + str(time.time_ns()))
    trash.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), trash)  # 移走整个目录，可恢复；绝不 unlink/rmtree 远端内容
elif cmd in {"copy", "copyto"}:
    gate = os.environ.get("FAKE_GATE")
    direction = "restore" if args[0].startswith("gdrive:") else "upload"
    if gate and cmd == os.environ.get("FAKE_GATE_CMD", "copyto") and direction == os.environ.get("FAKE_GATE_DIRECTION", "upload"):
        if os.environ.get("FAKE_REFRESH"):
            conf = configparser.ConfigParser(interpolation=None); conf.read(a[1])
            token = json.loads(conf["gdrive"]["token"]); token["access_token"] = "ya29.refreshed-in-child"
            conf["gdrive"]["token"] = json.dumps(token)
            save(conf)
        Path(gate + ".started").touch()
        until = time.monotonic() + 12
        while not Path(gate + ".release").exists():
            if time.monotonic() > until: raise RuntimeError("fake gate timed out")
            time.sleep(.01)
        if os.environ.get("FAKE_GATE_ERROR"):
            print(os.environ["FAKE_GATE_ERROR"], file=sys.stderr); sys.exit(1)
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
    src, dst = map(path, args[:2])
    # 和真 rclone 一样：copyto 覆盖已有的目标，--immutable 对它不起作用，只有 --ignore-existing 让它跳过。
    skip = cmd == "copyto" and "--ignore-existing" in args and dst.exists()
    if cmd == "copyto" and not skip: dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dst)
    if cmd == "copyto" and direction == "upload":
        if os.environ.get("FAKE_CORRUPT_UPLOAD") and not skip: dst.write_bytes(b"corrupted-upload")
        if os.environ.get("FAKE_POST_COPY_STAT"): Path(os.environ["FAKE_POST_COPY_STAT"]).touch()
    elif cmd == "copy":
        for p in src.rglob("*"):
            rel = p.relative_to(src)
            if p.is_file() and not ("--exclude" in args and rel.parts[0] == "calls"):
                out = dst / rel; out.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(p, out)
'''


@pytest.fixture
def cloud(home, monkeypatch):
    from assetizer import config, db
    from assetizer.topics import backup, run
    drive, log, binary = home / "fake-drive", home / "rclone.log", home / "fake-rclone"
    drive.mkdir()
    binary.write_text(FAKE.replace("#!PYTHON", "#!" + sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("ASSETIZER_RCLONE", str(binary))
    monkeypatch.setenv("FAKE_DRIVE", str(drive))
    monkeypatch.setenv("FAKE_LOG", str(log))
    config.override(dataclasses.replace(config.get(), rclone_bin=str(binary)))
    monkeypatch.setattr(backup, "_pending", None)
    monkeypatch.setattr(backup, "_uploading", None)
    # 就算反向检查故意破坏 state 校验，也不能碰到真正的 Google。
    def unexpected(request):
        raise AssertionError("OAuth 必须由当前测试显式提供替身")
    real_http = httpx.Client
    monkeypatch.setattr(backup, "httpx", types.SimpleNamespace(
        Client=lambda **kw: real_http(transport=httpx.MockTransport(unexpected), **kw), HTTPError=httpx.HTTPError))
    monkeypatch.setattr(run, "_last", dict.fromkeys(run._last, 0.0))
    monkeypatch.setattr(run, "_watching", {})
    # 测试机的实际余量会波动；低水位用例自己覆盖，其他用例固定为宽裕磁盘。
    monkeypatch.setattr(run.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(10**12, 0, 10**12))
    backup.save_client("synthetic-client-id", CLIENT_SECRET)
    backup._update_conf(token=json.dumps(TOKEN))
    # 旧的安全回归用例显式选择传；新规则矩阵由 test_backup_rules 覆盖。
    db.exec("UPDATE topics SET backup=1")
    backup.create_folder()
    return drive / "课代表" / "专题备份", log


def api():
    from assetizer.web.app import app
    return TestClient(app, base_url="http://localhost")


def read_once(calls):
    from assetizer import db
    from assetizer.topics import run
    db.set_setting("worker_slots", 4)
    w = Worker()
    assert run.dispatch(w)
    w.settle()
    r = db.one("SELECT * FROM topic_videos WHERE video_id=?", VID)
    assert r["state"] == "DONE", r["error"]
    return w


def purge():
    from assetizer import db
    from assetizer.topics import media_dir
    shutil.rmtree(media_dir(VID), ignore_errors=True)
    db.exec("UPDATE videos SET media_state='PURGED', media_path=NULL WHERE id=?", VID)


def reread(monkeypatch, kind):
    from assetizer import youtube
    def fail(*a, **kw):
        raise youtube.AcquireError(kind, "synthetic YouTube failure")
    monkeypatch.setattr(youtube, "download", fail)
    monkeypatch.setattr(youtube, "metadata", fail)
    assert api().post("/api/topics/1", json={"question": "换一个问题"}).status_code == 200


def logs(cloud):
    return [json.loads(x) for x in cloud[1].read_text().splitlines()]


def test_01_read_done_uploads_source_draft_and_serial_background(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, draft_dir, media_dir, run
    read_once(stand_in)
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 1
    (draft_dir(VID) / "shots").mkdir(exist_ok=True)
    (draft_dir(VID) / "shots" / "1.jpg").write_bytes(b"shot")
    (draft_dir(VID) / "calls" / "private.txt").write_text("model log")
    # 传第一期时才来第二期，线程要一直排空；反复 housekeeping 也不会多开线程或占读的槽。
    original_upload = backup._upload
    def upload(v):
        original_upload(v)
        if v["id"] == VID:
            db.exec("INSERT INTO videos(id,url,created_at) VALUES (?, 'url', ?)", OTHER, __import__("assetizer.util", fromlist=["now"]).now())
            db.exec("INSERT INTO video_backups(id,backup_due) VALUES (?,1)", OTHER)
            db.exec("UPDATE videos SET youtube_channel_id='UCtest' WHERE id=?", OTHER)
            db.exec("INSERT OR REPLACE INTO backup_authors(id,member,public) VALUES ('UCtest',1,1)")
            media_dir(OTHER).mkdir(parents=True); (media_dir(OTHER) / "source.mkv").write_bytes(b"other")
    monkeypatch.setattr(backup, "_upload", upload)
    monkeypatch.setenv("FAKE_DELAY", "0.08")
    monkeypatch.setattr(run, "remind", lambda: None)
    from assetizer.topics import comments, watch
    monkeypatch.setattr(comments, "due", lambda: None); monkeypatch.setattr(watch, "due", lambda: None)
    run.housekeeping(100000)
    th = run._watching["b"]
    run.housekeeping(100061)
    assert run._watching["b"] is th and th.name == "topic-backup"
    th.join(30); assert not th.is_alive()
    rows = db.q("SELECT * FROM videos LEFT JOIN video_backups USING(id) ORDER BY id")
    assert all(r["backup_at"] and not r["backup_due"] for r in rows)
    v = rows[0]
    assert v["backup_draft_at"] and v["backup_folder"] == backup.view()["folder"]["id"]
    assert v["backup_bytes"] == (media_dir(VID) / "source.mkv").stat().st_size
    assert hashlib.md5((cloud[0] / VID / "source.mkv").read_bytes()).digest() == hashlib.md5((media_dir(VID) / "source.mkv").read_bytes()).digest()
    assert (cloud[0] / VID / "draft" / "full.md").exists()
    assert (cloud[0] / VID / "draft" / "shots" / "1.jpg").exists()
    assert not (cloud[0] / VID / "draft" / "calls").exists()
    copies = [x for x in logs(cloud) if x[2] in {"copy", "copyto"}]
    assert [x[4].split(":", 1)[-1].split("/")[0] for x in copies] == [VID, VID, OTHER]
    assert backup.view()["uploading"] is None
    import os
    if out := os.environ.get("BACKUP_SAMPLE_OUT"):
        c = api(); topic = c.get("/api/topics/1").json()
        sample = {"settings.gdrive": c.get("/api/settings").json()["gdrive"], "topic.backup": topic["backup"],
                  "item.backup": topic["items"][0]["backup"]}
        Path(out).with_name("api-success-samples.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("age,low,deleted", [(8, False, False), (15, False, True), (8, True, True)])
def test_02_failed_upload_redacted_and_retention_grace(stand_in, cloud, monkeypatch, age, low, deleted):
    from assetizer import db
    from assetizer.topics import backup, media_dir, run
    read_once(stand_in)
    monkeypatch.setenv("FAKE_FAIL_CMD", "copyto")
    monkeypatch.setenv("FAKE_FAIL", 'failure ya29.fake 1//fake {"client_secret":"' + CLIENT_SECRET + '"} ?code=bad&client_secret=bad')
    backup.due()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert v["backup_failed_at"] and v["backup_error"]
    assert all(x not in v["backup_error"] for x in ("ya29.", "1//", CLIENT_SECRET, "code=bad", "secret=bad"))
    before = len(logs(cloud)); backup.due(); assert len(logs(cloud)) == before  # RETRY_EVERY
    db.exec("UPDATE videos SET used_at=?", (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age)).isoformat())
    if low:
        monkeypatch.setattr(run.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(10**12, 10**12, 0))
    assert run.retention()["purged"] == int(deleted)
    assert (media_dir(VID) / "source.mkv").exists() != deleted
    assert bool(db.one("SELECT backup_missed_at FROM video_backups")["backup_missed_at"]) == deleted


def test_03_gone_restored_source_runs_looks_and_keeps_gone(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, run
    read_once(stand_in); backup.due(); purge(); reread(monkeypatch, "VIDEO_REMOVED")
    n = len(stand_in); read_once(stand_in)
    assert any(x.startswith("look-") for x in stand_in[n:])
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert v["gone"] == "VIDEO_REMOVED" and v["gone_at"] and v["restored_at"]
    c = api(); item = c.get("/api/topics/1").json()["items"][0]
    detail = c.get(f"/api/topics/1/videos/{VID}").json()
    assert item["gone"] == "VIDEO_REMOVED" and item["backup"]["restored"]
    assert detail["backup"]["restored"] and detail["restored_at"] == v["restored_at"]


@pytest.mark.parametrize("kind", ["AUTH_REQUIRED", "BOT_CHALLENGE", "COOKIE_UNREADABLE", "YOUTUBE_REFUSED", "TOOL_OUTDATED"])
def test_04_waits_restore_and_finish(stand_in, cloud, monkeypatch, kind):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in); backup.due(); purge(); reread(monkeypatch, kind)
    n = len(stand_in); read_once(stand_in)
    assert any(x.startswith("look-") for x in stand_in[n:])
    assert db.one("SELECT videos.gone,video_backups.restored_at FROM videos LEFT JOIN video_backups USING(id)") == {"gone": None, "restored_at": db.one("SELECT restored_at FROM video_backups")["restored_at"]}
    assert db.one("SELECT restored_at FROM video_backups")["restored_at"]


def test_05_gone_failed_source_restore_uses_local_draft(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in); backup.due(); purge(); reread(monkeypatch, "VIDEO_PRIVATE")
    monkeypatch.setenv("FAKE_FAIL_CMD", "copyto"); monkeypatch.setenv("FAKE_FAIL", "unreachable")
    n = len(stand_in); read_once(stand_in)
    assert not any(x.startswith("look-") for x in stand_in[n:])
    assert "topic-write" in stand_in[n:] and db.one("SELECT gone FROM videos")["gone"] == "VIDEO_PRIVATE"


@pytest.mark.parametrize("source", [True, False])
def test_06_deleted_video_row_recovers_by_id(stand_in, cloud, monkeypatch, source):
    from assetizer import db
    from assetizer.topics import backup, draft_dir, run
    read_once(stand_in); backup.due()
    c = api(); assert c.post(f"/api/topics/1/videos/{VID}", json={"action": "remove"}).status_code == 200
    db.exec("DELETE FROM topic_removed")
    db.exec("UPDATE videos SET used_at='2026-01-01T00:00:00+00:00'")
    assert run.retention()["dropped"] == 1
    assert not db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)") and not draft_dir(VID).exists()
    if not source: (cloud[0] / VID / "source.mkv").unlink()
    r = c.post("/api/topics/1/videos", json={"videos": [{"id": VID, "title": "重新加入", "channel": "Lab", "duration": 40}]})
    assert r.status_code == 200, r.text
    reread(monkeypatch, "VIDEO_REMOVED")
    n = len(stand_in); read_once(stand_in)
    assert (draft_dir(VID) / "full.md").exists()
    assert not any(x.startswith("draft-") for x in stand_in[n:])
    assert any(x.startswith("look-") for x in stand_in[n:]) == source
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert v["restored_at"] if source else v["draft_restored_at"] and not v["restored_at"]


def test_07_existing_source_is_never_uploaded_again(stand_in, cloud):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in); backup.due()
    cloud[1].write_text("")
    db.exec("UPDATE video_backups SET backup_at=NULL,backup_due=1")
    backup.due()
    assert not [x for x in logs(cloud) if x[2] == "copyto"]
    assert db.one("SELECT backup_at FROM video_backups")["backup_at"]


@pytest.mark.parametrize("missing", ["binary", "client", "token", "folder"])
def test_08_unavailable_is_inert(stand_in, cloud, monkeypatch, missing):
    from assetizer import config, db
    from assetizer.topics import backup, run
    if missing == "binary": config.override(dataclasses.replace(config.get(), rclone_bin="/no/rclone"))
    elif missing == "client": backup.save_client(clear=True)
    elif missing == "token": backup.disconnect()
    else: db.set_setting("gdrive", {})
    expected = {"binary": "off", "client": "client", "token": "login", "folder": "folder"}[missing]
    assert backup.view()["state"] == expected and not backup.available()
    read_once(stand_in)
    before = len(logs(cloud)); backup.due(); assert len(logs(cloud)) == before
    assert api().get("/api/topics/1").json()["backup"] is None
    purge(); reread(monkeypatch, "AUTH_REQUIRED")
    w = Worker(); run.dispatch(w); w.settle()
    assert db.one("SELECT state FROM topic_videos")["state"] == "WAITING"


def test_09_protocol_secrets_permissions_and_cached_get(stand_in, cloud, monkeypatch, tmp_path):
    from assetizer import config, db
    from assetizer.topics import backup
    read_once(stand_in)
    evil = 'ya29.leak 1//leak {"access_token":"access-leak","refresh_token":"refresh-leak","client_secret":"secret-leak","id_token":"id-leak"} https://x/?code=code-leak&client_secret=url-leak ' + CLIENT_SECRET
    monkeypatch.setenv("FAKE_FAIL", "2026/11/10 12:00:00 " + evil); monkeypatch.setenv("FAKE_FAIL_CMD", "copyto")
    backup.due()
    db.exec("UPDATE video_backups SET backup_failed_at=NULL")  # 真正再传一次，不能靠重试冷却掩盖事件重复
    monkeypatch.setenv("FAKE_FAIL", "2026/11/10 12:30:00 " + evil)
    backup.due()
    assert len(db.q("SELECT * FROM events WHERE kind='backup' AND message LIKE '% 备份失败：%'")) == 1
    before = len(logs(cloud))
    c = api(); settings = c.get("/api/settings").json()["gdrive"]; topic = c.get("/api/topics/1").json()
    assert settings["state"] == "failing" and topic["backup"]["failed"] == 1
    assert topic["items"][0]["backup"]["state"] == "failed"
    assert len(logs(cloud)) == before
    exposed = json.dumps([settings, topic, db.q("SELECT * FROM events"), db.one("SELECT backup_error FROM video_backups")])
    assert all(x not in exposed for x in (CLIENT_SECRET, *TOKEN.values(), "ya29.", "1//", "access-leak", "refresh-leak", "secret-leak", "id-leak", "code-leak", "url-leak") if x not in {"Bearer", TOKEN["expiry"]})
    assert stat.S_IMODE((config.get().secrets_dir / "rclone.conf").stat().st_mode) == 0o600
    assert stat.S_IMODE(config.get().secrets_dir.stat().st_mode) == 0o700
    assert all(x[:1] == ["--config"] and x[2] in {"copyto", "copy", "lsjson", "about", "mkdir"} for x in logs(cloud))
    for cmd in ("delete", "deletefile", "purge", "rmdir", "rmdirs", "sync", "move", "moveto", "cleanup", "dedupe", "backend", "config", "unknown"):
        with pytest.raises(ValueError): backup._run(cmd)
    # 报告样例只含临时测试数据；通过显式环境变量写到任务 OUT。
    import os
    if out := os.environ.get("BACKUP_SAMPLE_OUT"):
        Path(out).write_text(json.dumps({"settings.gdrive": settings, "topic.backup": topic["backup"], "item.backup": topic["items"][0]["backup"]}, ensure_ascii=False, indent=2))


def test_10_oauth_pkce_validation_write_only_and_disconnect(cloud, monkeypatch):
    from assetizer import config, db
    from assetizer.topics import backup
    c = api(); c.post("/api/gdrive/disconnect", json={})
    response = c.post("/api/gdrive/login", json={})
    assert response.status_code == 200, response.text
    start = response.json()
    q = parse_qs(urlparse(start["login"]["url"]).query)
    assert q["client_id"] == ["synthetic-client-id"]
    assert q["redirect_uri"] == ["http://127.0.0.1:53682/"]
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["response_type"] == ["code"]
    assert q["state"] and q["code_challenge"] and q["code_challenge_method"] == ["S256"]
    assert c.post("/api/gdrive/login", json={}).json()["login"] == start["login"]
    assert c.post("/api/gdrive/login/code", json={"url": "http://127.0.0.1:53682/?state=wrong&code=secret-code"}).status_code == 400
    denied = c.post("/api/gdrive/login/code", json={"url": "http://127.0.0.1:53682/?state=" + q["state"][0] + "&error=access_denied"})
    assert denied.json()["detail"] == "你在 Google 那边取消了授权"
    c.post("/api/gdrive/login/cancel", json={}); assert backup.view()["login"] is None
    q = parse_qs(urlparse(c.post("/api/gdrive/login", json={}).json()["login"]["url"]).query)
    requests = []
    def token(req):
        body = parse_qs(req.content.decode()); requests.append(body)
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert challenge == q["code_challenge"][0]
        assert body["grant_type"] == ["authorization_code"] and body["client_secret"] == [CLIENT_SECRET]
        assert req.url == "https://oauth2.googleapis.com/token"
        return httpx.Response(200, json={**TOKEN, "expires_in": 3600})
    real_client = httpx.Client
    monkeypatch.setattr(backup.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(token), **kw))
    result = c.post("/api/gdrive/login/code", json={"url": "http://127.0.0.1:53682/?state=" + q["state"][0] + "&code=secret-code"})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "ok" and result.json()["usage"]["drive_total"] == 30000000000000
    conf = backup._read_conf(); t = json.loads(conf["token"])
    assert set(t) == {"access_token", "refresh_token", "token_type", "expiry"}
    assert t["refresh_token"] == TOKEN["refresh_token"] and dt.datetime.fromisoformat(t["expiry"]).tzinfo
    assert requests and result.json()["login"] is None
    assert c.post("/api/gdrive/folder", json={}).status_code == 409
    folder = result.json()["folder"]
    after = c.post("/api/gdrive/disconnect", json={}).json()
    assert not after["token"] and after["client"] and after["folder"] == folder
    assert "token" not in backup._read_conf()
    bad = c.post("/api/gdrive/client", json={"client_secret": CLIENT_SECRET})
    assert bad.status_code == 400 and CLIENT_SECRET not in bad.text
    clear = c.post("/api/gdrive/client", json={"clear": ["client"]})
    assert clear.status_code == 200 and not (config.get().secrets_dir / "rclone.conf").exists()
    assert c.post("/api/gdrive/login", json={}).status_code == 400


def test_11_retry_only_this_topics_failures(stand_in, cloud):
    from assetizer import db
    from assetizer.util import now
    db.exec("INSERT INTO topics(question,created_at,updated_at) VALUES ('other',?,?)", now(), now())
    db.exec("INSERT INTO videos(id,url,created_at) VALUES (?, 'url', ?)", OTHER, now())
    db.exec("INSERT INTO video_backups(id,backup_error,backup_failed_at) VALUES (?,'bad',?)", OTHER, now())
    db.exec("INSERT INTO topic_videos(topic_id,video_id,no,state,rev,added_at) VALUES (2,?,1,'DONE',1,?)", OTHER, now())
    db.exec("UPDATE video_backups SET backup_error='bad',backup_failed_at=?,backup_due=0 WHERE id=?", now(), VID)
    r = api().post("/api/topics/1/backup/retry", json={})
    assert r.status_code == 200 and r.json() == {"ok": True, "due": 1}
    rows = {v["id"]: v for v in db.q("SELECT * FROM videos LEFT JOIN video_backups USING(id)")}
    assert rows[VID]["backup_due"] and rows[VID]["backup_failed_at"] is None
    assert not rows[OTHER]["backup_due"] and rows[OTHER]["backup_failed_at"]


def test_auth_failure_stops_batch_and_test_recovers(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in)
    db.exec("INSERT INTO videos(id,url,created_at) VALUES (?, 'url', '2099-01-01')", OTHER)
    db.exec("INSERT INTO video_backups(id,backup_due) VALUES (?,1)", OTHER)
    monkeypatch.setenv("FAKE_FAIL", "invalid_grant ya29.synthetic"); monkeypatch.setenv("FAKE_FAIL_CMD", "copyto")
    backup.due()
    assert backup.view()["state"] == "auth" and db.one("SELECT backup_failed_at FROM video_backups WHERE id=?", OTHER)["backup_failed_at"] is None
    assert not any(OTHER in " ".join(x) for x in logs(cloud))
    before = len(logs(cloud)); backup.due(); assert len(logs(cloud)) == before
    monkeypatch.delenv("FAKE_FAIL")
    from assetizer.web.app import _throttle_at
    _throttle_at.pop("gdrive", None)
    assert api().post("/api/gdrive/test", json={}).json()["state"] == "ok"
    assert api().post("/api/gdrive/test", json={}).status_code == 429


def test_md5_mismatch_never_commits_upload_or_restore(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, media_dir
    read_once(stand_in)
    monkeypatch.setenv("FAKE_BAD_MD5", "1"); backup.due()
    assert not db.one("SELECT backup_at FROM video_backups")["backup_at"]
    purge()
    assert backup.restore_source(VID) is None and not (media_dir(VID) / "source.mkv").exists()


def test_live_upload_never_overwrites_a_remote_that_appears_after_the_check(stand_in, cloud, monkeypatch):
    """devbox 实测（2026-11-10）：真 rclone 的 copyto 不理 --immutable，会覆盖网盘上已有的同名原片。"""
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in)
    remote = cloud[0] / VID / "source.mkv"
    remote.parent.mkdir(parents=True, exist_ok=True); remote.write_bytes(b"the copy already on the drive")
    real, asked = backup._stat, []
    def late(path):  # 核对时还没有，复制前才出现（另一个上传者，或网盘列表延迟）
        if path == f"{VID}/source.mkv" and not asked:
            asked.append(path); return None
        return real(path)
    monkeypatch.setattr(backup, "_stat", late)
    before = len(logs(cloud)); backup.due()
    assert remote.read_bytes() == b"the copy already on the drive"
    copies = [a for a in logs(cloud)[before:] if a[2] == "copyto"]
    assert copies and "--ignore-existing" in copies[0] and "--immutable" not in copies[0]
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert not v["backup_at"] and v["backup_bad_md5"] == backup._md5(remote)
    assert f"{VID}/source.mkv 与本地不一致" in v["backup_error"]


@pytest.mark.parametrize("kind", ["NETWORK", "RATE_LIMITED", "UNKNOWN"])
def test_transient_download_errors_never_restore(stand_in, cloud, monkeypatch, kind):
    from assetizer.topics import backup, run
    read_once(stand_in); backup.due(); purge(); reread(monkeypatch, kind)
    before = len(logs(cloud)); w = Worker(); run.dispatch(w); w.settle()
    assert len(logs(cloud)) == before


def test_orphan_draft_grace_and_upload_protection(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, draft_dir, media_dir, run
    read_once(stand_in)
    api().post(f"/api/topics/1/videos/{VID}", json={"action": "remove"}); db.exec("DELETE FROM topic_removed")
    db.exec("UPDATE videos SET used_at=?", (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).isoformat())
    monkeypatch.setattr(backup, "_uploading", VID)
    assert run.retention() == {"purged": 0, "dropped": 0}
    assert (media_dir(VID) / "source.mkv").exists()
    monkeypatch.setattr(backup, "_uploading", None)
    assert run.retention()["dropped"] == 0 and draft_dir(VID).exists()
    db.exec("UPDATE videos SET used_at='2026-01-01T00:00:00+00:00'")
    assert run.retention()["dropped"] == 1 and not draft_dir(VID).exists()


def test_oauth_expiry_and_missing_refresh_never_write(cloud, monkeypatch):
    from assetizer.topics import backup
    c = api(); c.post("/api/gdrive/disconnect", json={}); c.post("/api/gdrive/login", json={})
    backup._pending["expires"] = 0
    assert c.post("/api/gdrive/login/code", json={"url": "secret-code"}).status_code == 400
    c.post("/api/gdrive/login", json={})
    real = httpx.Client
    monkeypatch.setattr(backup.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"access_token": "ya29.bad", "expires_in": 3600})), **kw))
    r = c.post("/api/gdrive/login/code", json={"url": "secret-code"})
    assert r.status_code == 400 and "refresh_token" in r.json()["detail"] and not backup.view()["token"]


def test_folder_creation_enqueues_local_files(stand_in, cloud):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in)
    db.set_setting("gdrive", {}); backup._update_conf(root_folder_id=None)
    db.exec("UPDATE video_backups SET backup_due=0")
    assert api().post("/api/gdrive/folder", json={"path": "课代表/另一个备份"}).status_code == 200
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 1


def test_config_atomic_update_preserves_refreshed_token_and_ignores_env(cloud, monkeypatch):
    from assetizer import config
    from assetizer.topics import backup
    monkeypatch.setenv("ASSETIZER_GDRIVE_CLIENT_SECRET", "unused-environment-secret")
    refreshed = {**TOKEN, "access_token": "ya29.refreshed"}
    backup._update_conf(token=json.dumps(refreshed))  # 模拟 rclone 已回写刷新后的 token
    original = backup.os.replace
    wrote = []
    def replace(src, dest):
        assert stat.S_IMODE(Path(src).stat().st_mode) == 0o600
        assert Path(dest).name == "rclone.conf"
        wrote.append(Path(src).read_text())
        original(src, dest)
    monkeypatch.setattr(backup.os, "replace", replace)
    backup._update_conf(root_folder_id=backup.view()["folder"]["id"])
    c = backup._read_conf()
    assert c["client_secret"] == CLIENT_SECRET and c["scope"] == "drive.file"
    assert json.loads(c["token"])["access_token"] == "ya29.refreshed" and wrote
    assert not list(config.get().secrets_dir.glob(".rclone-*"))


def test_upload_status_crosses_processes_and_usage_attempt_is_hourly(cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, run
    db.set_setting("gdrive_upload", {"video": VID})
    assert backup.view()["uploading"] == {"id": VID, "title": None}  # web 没有 worker 的 Python 全局变量
    run.recover(); assert backup.view()["uploading"] is None
    backup.refresh_usage(force=True)
    before = len(logs(cloud)); backup.refresh_usage(); assert len(logs(cloud)) == before
    db.set_setting("gdrive_usage_attempt", 0)
    old = db.setting("gdrive_usage"); old["at"] = 0; db.set_setting("gdrive_usage", old)
    monkeypatch.setenv("FAKE_FAIL", "network unavailable"); monkeypatch.setenv("FAKE_FAIL_CMD", "about")
    with pytest.raises(backup.BackupError): backup.refresh_usage()
    before = len(logs(cloud)); backup.refresh_usage(); assert len(logs(cloud)) == before


@pytest.mark.parametrize("status,error,auth", [(401, "unauthorized", True), (403, "insufficient authentication scopes", True),
                                              (403, "storageQuotaExceeded", False)])
def test_authorization_errors_distinguish_full_drive(cloud, monkeypatch, status, error, auth):
    from assetizer.topics import backup
    monkeypatch.setenv("FAKE_FAIL", f"Error {status}: {error}")
    with pytest.raises(backup.BackupError) as caught: backup._run("about", "gdrive:", "--json")
    assert caught.value.auth == auth


def test_incomplete_remote_draft_never_commits_full_md(cloud):
    from assetizer.topics import backup, draft_dir
    d = cloud[0] / VID / "draft"; d.mkdir(parents=True)
    (d / "full.md").write_text("partial")
    assert not backup.restore_draft(VID) and not (draft_dir(VID) / "full.md").exists()


# 第 2 轮：期望来自修复任务书；交错由事件/子进程控制，不用预设上传状态冒充并发。
def wait_file(path):
    until = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < until, f"没有等到测试屏障 {path.name}"
        time.sleep(.01)


def child(code, *args):
    from assetizer import config
    env = {**os.environ, "ASSETIZER_HOME": str(config.get().home),
           "PYTHONPATH": str(Path(config.__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize("disconnect", [True, False])
def test_r1_process_snapshot_refresh_cannot_undo_disconnect(cloud, home, monkeypatch, disconnect):
    from assetizer.topics import backup
    gate = home / "gate"
    source = home / "upload.mkv"; source.write_bytes(b"source")
    monkeypatch.setenv("FAKE_GATE", str(gate)); monkeypatch.setenv("FAKE_REFRESH", "1")
    upload = child("from assetizer.topics import backup; import sys; backup._run('copyto',sys.argv[1],'gdrive:abcdefghijk/source.mkv')", source)
    try:
        wait_file(Path(str(gate) + ".started"))
        if disconnect:
            other = child("from assetizer.topics import backup; backup.disconnect()")
            try:
                out, err = other.communicate(timeout=2)
                assert other.returncode == 0, err
            finally:
                if other.poll() is None: other.kill(); other.communicate()
            assert "token" not in backup._read_conf()
        assert upload.poll() is None  # 此时原传输确实还在运行
    finally:
        Path(str(gate) + ".release").touch()
        out, err = upload.communicate(timeout=5)
    assert upload.returncode == 0, err
    c = backup._read_conf()
    if disconnect:
        assert "token" not in c
        with pytest.raises(backup.BackupError, match="网盘已断开"):
            backup._run("about", "gdrive:", "--json")
    else:
        assert json.loads(c["token"])["access_token"] == "ya29.refreshed-in-child"
    assert not list((home / "secrets").glob(".run-*"))
    assert all(Path(a[1]).name == "rclone.conf" and Path(a[1]).parent.name.startswith(".run-") for a in logs(cloud))


def test_r1_upload_does_not_block_restore_query_or_test(cloud, home, monkeypatch):
    from assetizer.topics import backup
    gate = home / "gate"; source = home / "upload"; source.write_bytes(b"source")
    monkeypatch.setenv("FAKE_GATE", str(gate))
    errors = []
    def upload():
        try: backup._run("copyto", str(source), f"gdrive:{VID}/source.mkv")
        except Exception as e: errors.append(e)
    th = threading.Thread(target=upload); th.start()
    result = []
    def queries():
        from assetizer.web.app import _throttle_at
        _throttle_at.pop("gdrive", None)
        result.append(backup.restore_draft(OTHER))
        result.append(api().post("/api/gdrive/test", json={}).status_code)
    try:
        wait_file(Path(str(gate) + ".started"))
        q = threading.Thread(target=queries); q.start(); q.join(2)
        assert not q.is_alive(), "读片/测试在等长上传"
        assert result == [False, 200] and th.is_alive()
    finally:
        Path(str(gate) + ".release").touch(); th.join(5); q.join(5)
    assert not errors


def test_r1_short_timeout_and_startup_cleanup(cloud, home, monkeypatch):
    from assetizer.topics import backup
    stale = home / "secrets" / ".run-orphan.conf"; stale.write_text("synthetic orphan")
    proc = child("from assetizer.topics import backup; backup._run('about','gdrive:','--json')")
    assert proc.communicate(timeout=3)[1] == "" and proc.returncode == 0
    assert not stale.exists()
    seen = []
    def fake(args, **kw):
        seen.append(kw["timeout"])
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(backup.subprocess, "run", fake)
    for cmd in ("lsjson", "about", "mkdir", "copyto", "copy"): backup._run(cmd, "gdrive:")
    assert seen[:3] == [120] * 3 and seen[3:] == [6 * 3600] * 2


def test_r7_short_config_sections_are_cross_process_exclusive(cloud, home):
    from assetizer.topics import backup
    ready, release = home / "locked", home / "release"
    code = """from assetizer.topics import backup
from pathlib import Path
import sys,time
with backup._locked_conf():
    data=backup._read_conf()
    Path(sys.argv[1]).touch()
    while not Path(sys.argv[2]).exists(): time.sleep(.01)
    data['root_folder_id']='new-root'
    backup._write_conf(data)
"""
    first = child(code, ready, release)
    second = None
    try:
        wait_file(ready)
        second = child("from assetizer.topics import backup; backup.disconnect()")
        time.sleep(.3)
        assert second.poll() is None, "配置独占锁被绕过"
    finally:
        release.touch()
        _, err = first.communicate(timeout=5)
        if second: second.communicate(timeout=5)
    assert first.returncode == 0, err
    assert second.returncode == 0
    assert backup._read_conf().get("root_folder_id") == "new-root" and "token" not in backup._read_conf()


@pytest.mark.parametrize("finished", [False, True])
def test_r2_retention_rechecks_after_real_upload_interleaving(stand_in, cloud, monkeypatch, finished):
    from assetizer import db
    from assetizer.topics import backup, media_dir, run
    read_once(stand_in)
    db.exec("UPDATE videos SET used_at='2026-01-01T00:00:00+00:00'")
    db.exec("UPDATE video_backups SET backup_due=1")
    selected, uploaded, proceed, release = (threading.Event() for _ in range(4))
    original_q, original_upload = db.q, backup._upload
    def query(sql, *a):
        rows = original_q(sql, *a)
        if threading.current_thread().name == "retention-test" and "WHERE media_state='CACHED'" in sql:
            selected.set(); assert proceed.wait(5)
        return rows
    def upload(v):
        uploaded.set()
        if finished:
            original_upload(v)
            db.exec("UPDATE videos SET used_at=? WHERE id=?", __import__('assetizer.util', fromlist=['now']).now(), VID)
        else:
            assert release.wait(5)
            original_upload(v)
    monkeypatch.setattr(db, "q", query); monkeypatch.setattr(backup, "_upload", upload)
    result = []
    cleanup = threading.Thread(name="retention-test", target=lambda: result.append(run.retention()))
    cleanup.start(); assert selected.wait(5)
    worker = threading.Thread(target=backup.due); worker.start(); assert uploaded.wait(5)
    if finished: worker.join(5); assert not worker.is_alive()
    try:
        proceed.set(); cleanup.join(3)
        assert not cleanup.is_alive()
        assert result == [{"purged": 0, "dropped": 0}]
        assert (media_dir(VID) / "source.mkv").exists()
        assert not db.one("SELECT backup_missed_at FROM video_backups")["backup_missed_at"]
    finally:
        release.set(); worker.join(5); cleanup.join(5)


def test_r3_generation_preserves_requeue_after_copy(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, draft_dir
    read_once(stand_in)
    before = db.one("SELECT backup_gen FROM video_backups")["backup_gen"]
    original = backup._run
    def run(cmd, *args):
        result = original(cmd, *args)
        if cmd == "copy":
            (draft_dir(VID) / "shots").mkdir(exist_ok=True)
            (draft_dir(VID) / "shots" / "new.jpg").write_bytes(b"new shot")
            db.exec("UPDATE video_backups SET backup_due=1,backup_gen=backup_gen+1 WHERE id=?", VID)
        return result
    monkeypatch.setattr(backup, "_run", run)
    backup._upload(db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)"))
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert v["backup_due"] == 1 and v["backup_gen"] == before + 1
    monkeypatch.setattr(backup, "_run", original); backup.due()
    assert (cloud[0] / VID / "draft" / "shots" / "new.jpg").exists()
    assert not db.one("SELECT backup_due FROM video_backups")["backup_due"]


def test_r4_known_bad_md5_stays_failed_until_remote_changes(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, media_dir, run
    read_once(stand_in); monkeypatch.setenv("FAKE_BAD_MD5", "1")
    backup.due()
    for _ in range(2):
        db.exec("UPDATE video_backups SET backup_failed_at=NULL")
        backup.due()
        v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
        assert not v["backup_at"] and v["backup_bad_md5"] == "bad" and v["backup_failed_at"]
        assert f"{VID}/source.mkv 与本地不一致" in v["backup_error"]
    age = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
    db.exec("UPDATE videos SET used_at=?", age)
    assert run.retention()["purged"] == 0 and (media_dir(VID) / "source.mkv").exists()
    purge()
    assert backup.restore_source(VID) is None  # 已知坏副本不因与自身 md5 相符而恢复
    monkeypatch.delenv("FAKE_BAD_MD5")
    remote = cloud[0] / VID / "source.mkv"
    db.exec("UPDATE video_backups SET backup_bad_md5=?", hashlib.md5(remote.read_bytes()).hexdigest())
    assert backup.restore_source(VID) is None  # 即使这份容器能被 ffprobe 解析也不采用已知坏副本
    original = remote.read_bytes()
    remote.write_bytes(original + b"changed by user")
    db.exec("UPDATE video_backups SET backup_failed_at=NULL")
    backup.due()
    assert not db.one("SELECT backup_at FROM video_backups")["backup_at"]  # S3：变了也须对上上传前记下的 md5
    remote.write_bytes(original)
    db.exec("UPDATE video_backups SET backup_failed_at=NULL")
    backup.due(); assert db.one("SELECT backup_at FROM video_backups")["backup_at"]


def test_r4_unprobeable_restored_source_falls_back_to_draft(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, media_dir
    read_once(stand_in); backup.due(); purge()
    (cloud[0] / VID / "source.mkv").write_bytes(b"not a video but md5 is correct")
    reread(monkeypatch, "VIDEO_REMOVED")
    n = len(stand_in); read_once(stand_in)
    assert not any(x.startswith("look-") for x in stand_in[n:])
    assert not (media_dir(VID) / "source.mkv").exists()
    assert db.one("SELECT state FROM topic_videos")["state"] == "DONE"


@pytest.mark.parametrize("bad", ["state", "query", "newline", "control", "long", "duplicate-state", "duplicate-code", "duplicate-error", "host"])
def test_r5_bad_callback_never_exchanges_token(cloud, monkeypatch, bad):
    from assetizer.topics import backup
    backup.disconnect(); login = backup.start_login()
    state = parse_qs(urlparse(login["login"]["url"]).query)["state"][0]
    root = f"http://127.0.0.1:53682/?state={state}"
    values = {"state": "http://127.0.0.1:53682/?state=错误&code=valid-code-123",
              "query": "state=abc&code=valid-code-123", "newline": "valid-code-123\n", "control": "valid-code-\x01x",
              "long": "x" * 2049, "duplicate-state": root + "&state=abc&code=valid-code-123",
              "duplicate-code": root + "&code=valid-code-123&code=other-code-123",
              "duplicate-error": root + "&error=access_denied&error=access_denied",
              "host": "https://evil.test/?code=valid-code-123"}
    requests = []
    real = httpx.Client
    monkeypatch.setattr(backup.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(lambda req: requests.append(req) or httpx.Response(400)), **kw))
    r = api().post("/api/gdrive/login/code", json={"url": values[bad]})
    assert r.status_code == 400 and not requests
    assert r.json()["detail"] == "授权地址或授权码格式不正确，请重新粘贴"
    assert "valid-code-123" not in r.text and "错误" not in r.text


def test_r6_old_folder_draft_is_not_confirmed_or_dropped(stand_in, cloud):
    from assetizer import db
    from assetizer.topics import backup, run, draft_dir
    read_once(stand_in); backup.due()
    db.exec("UPDATE video_backups SET backup_folder='old',backup_draft_folder='old',backup_due=0")
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert backup.video_view(v)["state"] is None
    assert backup.view()["queue"]["due"] == 0
    assert api().get("/api/topics/1").json()["items"][0]["backup"]["state"] is None
    db.exec("UPDATE video_backups SET backup_draft_folder=?", backup._folder()["folder_id"])
    assert api().get("/api/topics/1").json()["items"][0]["backup"]["state"] == "done"
    db.exec("UPDATE video_backups SET backup_draft_folder='old'")
    api().post(f"/api/topics/1/videos/{VID}", json={"action": "remove"}); db.exec("DELETE FROM topic_removed")
    db.exec("UPDATE videos SET used_at=?", (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).isoformat())
    assert run.retention()["dropped"] == 0 and draft_dir(VID).exists()


def test_r8_restore_never_overwrites_concurrent_local_file(stand_in, cloud, home, monkeypatch):
    from assetizer.topics import backup, media_dir
    read_once(stand_in); backup.due(); purge()
    dest = media_dir(VID) / "source.mkv"; dest.parent.mkdir(parents=True, exist_ok=True)
    stale = dest.parent / ".restore-orphan.mkv"; stale.write_bytes(b"old temp")
    gate = home / "restore-gate"
    monkeypatch.setenv("FAKE_GATE", str(gate)); monkeypatch.setenv("FAKE_GATE_DIRECTION", "restore")
    result = []; th = threading.Thread(target=lambda: result.append(backup.restore_source(VID))); th.start()
    try:
        wait_file(Path(str(gate) + ".started"))
        assert not stale.exists()
        dest.write_bytes(b"concurrent good local")
    finally:
        Path(str(gate) + ".release").touch(); th.join(5)
    assert result == [None] and dest.read_bytes() == b"concurrent good local"
    before = len(logs(cloud)); assert backup.restore_source(VID) is None
    assert len(logs(cloud)) == before


def test_r9_wait_restore_preserves_existing_gone(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in); backup.due(); purge()
    db.exec("UPDATE videos SET gone='VIDEO_REMOVED',gone_at='2026-01-01'")
    reread(monkeypatch, "AUTH_REQUIRED"); read_once(stand_in)
    assert db.one("SELECT gone,gone_at FROM videos") == {"gone": "VIDEO_REMOVED", "gone_at": "2026-01-01"}


@pytest.mark.parametrize("off", ["binary", "client", "token", "folder", "disconnect", "mismatch"])
def test_r10_off_hides_due_but_keeps_failures(stand_in, cloud, off):
    from assetizer import config, db
    from assetizer.topics import backup
    read_once(stand_in)
    if off == "binary": config.override(dataclasses.replace(config.get(), rclone_bin="/no/rclone"))
    elif off == "client": backup.save_client(clear=True)
    elif off in ("token", "disconnect"): backup.disconnect()
    elif off == "folder": db.set_setting("gdrive", {})
    else: backup._update_conf(root_folder_id="other")
    assert not backup.available()
    assert backup.view()["queue"]["due"] == 0
    assert backup.video_view(db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)"))["state"] is None
    db.exec("UPDATE video_backups SET backup_failed_at='now',backup_missed_at='now',backup_error='failure'")
    assert backup.view()["queue"]["failed"] == backup.view()["queue"]["missed"] == 1
    assert backup.video_view(db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)"))["state"] == "failed"


def test_r11_missing_remote_is_quiet_and_real_failure_deduplicates(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, draft_dir, run
    for i in range(6):
        if i:
            shutil.rmtree(draft_dir(VID))
            assert api().post("/api/topics/1", json={"question": f"第{i}次问题"}).status_code == 200
        read_once(stand_in)  # 每次 DRAFT 都确实查一个远端没有的底稿
    purge(); reread(monkeypatch, "AUTH_REQUIRED")
    for _ in range(6):
        db.exec("UPDATE topic_videos SET state='QUEUED'")
        w = Worker(); assert run.dispatch(w); w.settle()
        assert db.one("SELECT state FROM topic_videos")["state"] == "WAITING"
    assert not db.q("SELECT * FROM events WHERE kind='backup' AND level='warn'")
    monkeypatch.setenv("FAKE_FAIL", "2026/11/10 12:00:00 network refused")
    for _ in range(6): backup.restore_source(VID)
    assert len(db.q("SELECT * FROM events WHERE kind='backup' AND level='warn'")) == 1


@pytest.mark.parametrize("message,auth", [
    ("open /tmp/ab-401-cdef/source.mkv: permission denied", False),
    ("sizes differ src 401 vs dst 0", False),
    ("Token refresh failed try 1/5 connection refused", False),
    ("invalid_grant", True), ("Token has been expired or revoked", True), ("HTTP 401 Unauthorized", True),
    ("invalid credentials", True)])
def test_r12_auth_matching_requires_authorization_evidence(cloud, message, auth):
    from assetizer.topics import backup
    assert backup._auth_error("HTTP 403 insufficient permissions")
    assert backup._auth_error(message) == auth


def test_r13_draft_restore_has_separate_timestamp(stand_in, cloud):
    from assetizer import db
    from assetizer.topics import backup, draft_dir
    read_once(stand_in); backup.due(); shutil.rmtree(draft_dir(VID))
    assert backup.restore_draft(VID)
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert v["draft_restored_at"] and not v["restored_at"]
    assert not backup.video_view(v)["restored"]


def test_r15_contract_upload_title_auth_and_test_repair(stand_in, cloud, monkeypatch, home):
    from assetizer import db
    from assetizer.topics import backup
    from assetizer.web.app import _throttle_at
    read_once(stand_in)
    db.set_setting("gdrive_upload", {"video": VID})
    assert api().get("/api/settings").json()["gdrive"]["uploading"] == {"id": VID, "title": db.one("SELECT title FROM videos")["title"]}
    backup._state("invalid_grant", auth=True)
    assert api().get("/api/topics/1").json()["backup"]["auth"] is True
    sample = {"settings.gdrive": api().get("/api/settings").json()["gdrive"],
              "topics.backup": api().get("/api/topics/1").json()["backup"]}
    backup._update_conf(root_folder_id=None)
    _throttle_at.pop("gdrive", None)
    r = api().post("/api/gdrive/test", json={})
    assert r.status_code == 200 and r.json()["state"] == "ok"
    assert backup._read_conf()["root_folder_id"] == backup._folder()["folder_id"]
    monkeypatch.setenv("FAKE_FAIL", "network refused")
    _throttle_at.pop("gdrive", None)
    r = api().post("/api/gdrive/test", json={})
    assert r.status_code == 200 and r.json()["state"] == "failing"
    monkeypatch.setenv("FAKE_FAIL", "HTTP 401 Unauthorized")
    _throttle_at.pop("gdrive", None)
    r = api().post("/api/gdrive/test", json={})
    assert r.status_code == 200 and r.json()["state"] == "auth"
    sample["test.failure"] = r.json()
    backup.disconnect(); _throttle_at.pop("gdrive", None)
    assert api().post("/api/gdrive/test", json={}).status_code == 400
    if out := os.environ.get("BACKUP_SAMPLE_OUT"):
        (Path(out).parent / "api-round-2.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("invalid", ["../../outside", "short", "abcdefghij/", "abcdefghij\n"])
def test_r16_video_entry_points_reject_invalid_ids(cloud, invalid):
    from assetizer.topics import backup
    before = len(logs(cloud))
    assert backup.restore_source(invalid) is None
    assert backup.restore_draft(invalid) is False
    assert backup.uploading(invalid) is False
    with pytest.raises(ValueError): backup._upload({"id": invalid})
    with pytest.raises(ValueError):
        with backup.claim(invalid): pass
    assert len(logs(cloud)) == before


@pytest.mark.parametrize("text", [r'\"refresh_token\":\"opaque-sensitive\"', 'refresh_token=opaque-sensitive',
    'https://x/?access_token=opaque-sensitive', 'Authorization: Bearer opaque-sensitive',
    'RefreshToken:opaque-sensitive', 'AccessToken:opaque-sensitive', 'refresh_token = opaque-sensitive', 'code=opaque-sensitive'])
def test_r16_redaction_covers_additional_formats(cloud, text):
    from assetizer.topics import backup
    assert "opaque-sensitive" not in backup.redact(text)


def test_r3_enqueue_generations_and_round1_schema_migration(stand_in, cloud):
    from assetizer import db
    from assetizer.topics import backup
    initial = db.one("SELECT backup_gen FROM video_backups")["backup_gen"]
    read_once(stand_in)
    assert db.one("SELECT backup_gen FROM video_backups")["backup_gen"] == initial + 1
    db.set_setting("gdrive", {}); backup._update_conf(root_folder_id=None)
    backup.create_folder("课代表/新备份")
    assert db.one("SELECT backup_gen FROM video_backups")["backup_gen"] == initial + 1  # 已入队的同一期不重复递增代次
    db.exec("UPDATE video_backups SET backup_failed_at='now'")
    assert api().post("/api/topics/1/backup/retry", json={}).status_code == 200
    assert db.one("SELECT backup_gen FROM video_backups")["backup_gen"] == initial + 2
    conn = db.connect()
    for column in ("backup_gen", "backup_bad_md5", "backup_draft_folder", "draft_restored_at"):
        conn.execute(f"ALTER TABLE video_backups DROP COLUMN {column}")
    conn.commit(); conn.close(); db._local.conn = None
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id)")
    assert v["backup_gen"] == 0
    assert all(v[k] is None for k in ("backup_bad_md5", "backup_draft_folder", "draft_restored_at"))


def test_r14_draft_updates_same_name_and_keeps_remote_extras(stand_in, cloud):
    from assetizer import db
    from assetizer.topics import backup, draft_dir
    read_once(stand_in); backup.due()
    d = cloud[0] / VID / "draft"
    (d / "remote-only.txt").write_text("keep forever")
    (draft_dir(VID) / "full.md").write_text("updated draft")
    db.exec("UPDATE video_backups SET backup_due=1,backup_gen=backup_gen+1")
    backup.due()
    assert (d / "full.md").read_text() == "updated draft"
    assert (d / "remote-only.txt").read_text() == "keep forever"


# 第 3 轮：共享卷租约、失败归属与跨轮核对；凭据和子进程均为测试替身。
@pytest.mark.parametrize("action", ["query", "disconnect", "clear"])
def test_s1_live_lease_survives_other_process_first_use(cloud, home, monkeypatch, action):
    from assetizer.topics import backup
    gate = home / "lease-gate"; source = home / "source"; source.write_bytes(b"source")
    monkeypatch.setenv("FAKE_GATE", str(gate)); monkeypatch.setenv("FAKE_REFRESH", "1")
    code = "from assetizer.topics import backup; import os,sys; os.getpid=lambda:3999999; backup._run('copyto',sys.argv[1],'gdrive:abcdefghijk/source.mkv')"
    proc = child(code, source)
    try:
        wait_file(Path(str(gate) + ".started"))
        snapshots = list((home / "secrets").glob(".run-*/rclone.conf")); assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600 and stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
        leases = list((home / "secrets").glob(".run-*.lock")); assert len(leases) == 1
        assert stat.S_IMODE(leases[0].stat().st_mode) == 0o600
        operation = {"query": "backup._run('about','gdrive:','--json')", "disconnect": "backup.disconnect()",
                     "clear": "backup.save_client(clear=True)"}[action]
        other = child("from assetizer.topics import backup; " + operation)
        _, err = other.communicate(timeout=3); assert other.returncode == 0, err
        assert proc.poll() is None and snapshot.exists(), "另一进程删除了仍持有租约的配置"
    finally:
        Path(str(gate) + ".release").touch(); _, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert not list((home / "secrets").glob(".run-*"))
    if action == "query": assert json.loads(backup._read_conf()["token"])["access_token"] == "ya29.refreshed-in-child"
    else: assert "token" not in backup._read_conf()


@pytest.mark.parametrize("action", ["query", "disconnect", "clear"])
def test_s1_unheld_runs_cleaned_regardless_of_numeric_name(cloud, home, action):
    from assetizer.topics import backup
    d = home / "secrets"
    for name in (str(os.getpid()), "1", "3999999", "orphan"):
        (d / f".run-{name}").mkdir(); (d / f".run-{name}" / "rclone.conf").write_text("synthetic leftover")
        (d / f".run-{name}.lock").touch()
    (d / ".run-dir-only").mkdir(); (d / ".run-dir-only" / "rclone.conf.old123").write_text("synthetic leftover")
    (d / ".run-legacy.conf").touch(); (d / ".run-lease-only.lock").touch()
    if action == "query": backup._run("about", "gdrive:", "--json")
    elif action == "disconnect": backup.disconnect()
    else: backup.save_client(clear=True)
    assert not list(d.glob(".run-*"))


@pytest.mark.parametrize("disconnect", [False, True])
def test_s2_old_upload_cannot_fail_new_login_or_disconnect(stand_in, cloud, home, monkeypatch, disconnect):
    from assetizer import db
    from assetizer.topics import backup, media_dir
    from assetizer.web.app import _throttle_at
    read_once(stand_in)
    other = media_dir(OTHER) / "source.mkv"; other.parent.mkdir(parents=True); other.write_bytes(b"other source")
    db.exec("INSERT INTO videos(id,url,created_at,media_path,youtube_channel_id) VALUES(?,?,'2099-01-01',?,'UCtest')", OTHER, "synthetic", str(other))
    db.exec("INSERT INTO video_backups(id,backup_due) VALUES (?,1)", OTHER)
    db.exec("INSERT OR REPLACE INTO backup_authors(id,member,public) VALUES ('UCtest',1,1)")
    gate = home / "ownership-gate"; monkeypatch.setenv("FAKE_GATE", str(gate))
    if not disconnect: monkeypatch.setenv("FAKE_GATE_ERROR", "HTTP 401 Unauthorized ya29.old-token")
    th = threading.Thread(target=backup.due); th.start()
    try:
        wait_file(Path(str(gate) + ".started"))
        if disconnect:
            assert api().post("/api/gdrive/disconnect", json={}).status_code == 200
        else:
            backup._update_conf(token=json.dumps({**TOKEN, "access_token": "ya29.new-login", "refresh_token": "1//new-login"}))
            _throttle_at.pop("gdrive", None)
            response = api().post("/api/gdrive/test", json={})
            assert response.status_code == 200 and response.json()["state"] == "ok"
        assert th.is_alive()
    finally:
        Path(str(gate) + ".release").touch(); th.join(5)
    assert not th.is_alive()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert v["backup_due"] == 1 and v["backup_error"] is None and v["backup_failed_at"] is None
    assert backup.view()["state"] == ("login" if disconnect else "ok")
    assert not db.q("SELECT * FROM events WHERE kind='backup' AND level='warn'")
    assert not (cloud[0] / OTHER).exists(), "旧凭据已失效，这一轮不应再取下一期"
    if disconnect: backup._update_conf(token=json.dumps({**TOKEN, "access_token": "ya29.next-login", "refresh_token": "1//next-login"}))
    monkeypatch.delenv("FAKE_GATE"); monkeypatch.delenv("FAKE_GATE_ERROR", raising=False)
    before = len(logs(cloud)); backup.due()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert v["backup_at"] and v["backup_due"] == 0
    assert sum(a[2] == "copyto" and a[4] == f"gdrive:{VID}/source.mkv" for a in logs(cloud)[before:]) == (0 if disconnect else 1)


@pytest.mark.parametrize("failure", ["HTTP 401 Unauthorized", "network refused", "timeout", "spawn"])
def test_s2_every_old_failure_is_stale(cloud, monkeypatch, failure):
    from assetizer.topics import backup
    def run(*args, **kwargs):
        backup._update_conf(token=json.dumps({**TOKEN, "access_token": "ya29.changed", "refresh_token": "1//changed-login"}))
        if failure == "timeout": raise subprocess.TimeoutExpired("synthetic", 1)
        if failure == "spawn": raise OSError("synthetic")
        return types.SimpleNamespace(returncode=1, stdout="", stderr=failure)
    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(backup.BackupError) as error: backup._run("about", "gdrive:", "--json")
    assert error.value.stale and not error.value.auth
    assert backup.view()["state"] == "ok"


@pytest.mark.parametrize("kind", ["source", "draft"])
def test_s2_stale_restores_are_quiet(cloud, monkeypatch, kind):
    from assetizer import db
    from assetizer.topics import backup
    def stale(*args): raise backup.BackupError("old request", stale=True)
    monkeypatch.setattr(backup, "_stat", stale)
    assert (backup.restore_source(VID) if kind == "source" else backup.restore_draft(VID)) in (None, False)
    backup._failed(VID, backup.BackupError("old failure", stale=True))
    assert not db.q("SELECT * FROM events WHERE kind='backup' AND level='warn'")


@pytest.mark.parametrize("corrupt", [True, False])
def test_s3_pending_hash_survives_interrupted_post_copy_stat(stand_in, cloud, home, monkeypatch, corrupt):
    from assetizer import db
    from assetizer.topics import backup, media_dir, run
    read_once(stand_in)
    source = media_dir(VID) / "source.mkv"
    digest = backup._md5(source)
    monkeypatch.setenv("FAKE_POST_COPY_STAT", str(home / "stat-once"))
    if corrupt: monkeypatch.setenv("FAKE_CORRUPT_UPLOAD", "1")
    backup.due()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert not v["backup_at"] and v["backup_src_md5"] == digest
    # 后来的本地内容不参与比较：以这次上传之前记录的 md5 为准。
    source.write_bytes(b"new local content")
    monkeypatch.delenv("FAKE_POST_COPY_STAT"); monkeypatch.delenv("FAKE_CORRUPT_UPLOAD", raising=False)
    db.exec("UPDATE video_backups SET backup_failed_at=NULL WHERE id=?", VID)
    before = len(logs(cloud)); backup.due()
    assert not any(a[2] == "copyto" for a in logs(cloud)[before:])
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    if corrupt:
        assert not v["backup_at"] and v["backup_bad_md5"] == backup._md5(cloud[0] / VID / "source.mkv")
        assert v["backup_error"] == f"网盘上 {VID}/source.mkv 与本地不一致，请在网盘里删掉它后点重试"
        age = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=run.knobs.get("retention_days") + 1)).isoformat()
        db.exec("UPDATE videos SET used_at=? WHERE id=?", age, VID); run.retention()
        assert source.exists() and not db.one("SELECT backup_missed_at FROM video_backups WHERE id=?", VID)["backup_missed_at"]
    else:
        assert v["backup_at"] and v["backup_src_md5"] is None and not v["backup_bad_md5"]


def test_s3_pending_hash_migrates_existing_database(stand_in, cloud):
    from assetizer import db
    conn = db.connect(); conn.execute("ALTER TABLE video_backups DROP COLUMN backup_src_md5")
    conn.commit(); conn.close(); db._local.conn = None
    assert db.one("SELECT backup_src_md5 FROM video_backups")["backup_src_md5"] is None


@pytest.mark.parametrize("existing", ["absent", "directory", "orphan"])
def test_s4_unconfigured_read_paths_have_no_filesystem_writes(stand_in, home, monkeypatch, existing):
    from assetizer import db
    from assetizer.topics import backup, run
    d = home / "secrets"
    if existing != "absent":
        d.mkdir(exist_ok=True); d.chmod(0o755)
    if existing == "orphan":
        (d / ".run-orphan.conf").write_text("synthetic orphan")
    if existing == "absent" and d.exists():
        d.rmdir()
    before = {str(p.relative_to(home)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
              for p in d.rglob("*")} if d.exists() else {}
    mode = d.stat().st_mode if d.exists() else None
    # 还要覆盖一份没有租约的残留：GET 和读判断绝不清它。
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    backup._read_conf(); assert not backup.available(); backup.view()
    backup.video_view(v); backup.topic_view([v]); backup.due(); run.retention()
    assert (d.stat().st_mode if d.exists() else None) == mode
    assert api().get("/api/box").status_code == 404  # 仓库没有此路由；实际 box 包在 settings 中。
    assert api().get("/api/settings").status_code == 200
    assert api().get("/api/topics/1").status_code == 200
    after = {str(p.relative_to(home)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
             for p in d.rglob("*")} if d.exists() else {}
    assert after == before  # 原有 DB 连接初始化会建目录/chmod，接口仍不能新增任何凭据或锁文件。


@pytest.mark.parametrize("enabled", [True, False])
def test_s5_legacy_id_does_not_abort_retention(stand_in, cloud, monkeypatch, enabled):
    from assetizer import db
    from assetizer.topics import backup, media_dir, draft_dir, run
    if not enabled: backup.disconnect()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat()
    for vid in ("abcdefghij", OTHER):
        db.exec("INSERT INTO videos(id,url,title,media_state,created_at,used_at) VALUES(?,?,?,'CACHED',?,?)", vid, "synthetic", vid, old, old)
        m = media_dir(vid); m.mkdir(parents=True); (m / "source.mkv").write_bytes(b"source")
        d = draft_dir(vid); d.mkdir(parents=True); (d / "full.md").write_text("draft")
    assert run.retention() == {"purged": 2, "dropped": 2}
    assert not db.one("SELECT 1 FROM videos WHERE id IN (?,?)", "abcdefghij", OTHER)
    assert not db.q("SELECT * FROM events WHERE message LIKE '%专题清理异常%'")
    before = len(logs(cloud))
    assert backup.uploading("abcdefghij") is False
    assert backup.restore_source("abcdefghij") is None and not backup.restore_draft("abcdefghij")
    assert len(logs(cloud)) == before


def test_s5_legacy_id_acquire_with_backup_off_keeps_baseline(stand_in, home):
    from assetizer import db
    from assetizer.topics import run
    with db.tx():
        db.exec("PRAGMA defer_foreign_keys=ON")
        db.exec("UPDATE topic_videos SET video_id='abcdefghij'")
        db.exec("UPDATE videos SET id='abcdefghij'")
    w = Worker(); assert run.dispatch(w); w.settle()
    row = db.one("SELECT * FROM topic_videos")
    assert row["state"] == "DONE", row["error"]


# 第 4 轮：rclone 刷新留下的配置副产物、旧确认作废、按登录身份判断失败归属；凭据和子进程均为测试替身。
UPLOAD = "from assetizer.topics import backup; import sys; backup._run('copyto',sys.argv[1],'gdrive:abcdefghijk/source.mkv')"


@pytest.mark.parametrize("crash", ["new_written", "old_renamed"])
@pytest.mark.parametrize("action", ["query", "disconnect", "clear", "recover"])
def test_r4_rclone_save_leftovers_are_reaped(cloud, home, monkeypatch, crash, action):
    from assetizer.topics import backup, run
    d = home / "secrets"; source = home / "source"; source.write_bytes(b"source")
    monkeypatch.setenv("FAKE_GATE", str(home / "save-gate")); monkeypatch.setenv("FAKE_REFRESH", "1")
    monkeypatch.setenv("FAKE_SAVE_CRASH", crash)
    proc = child(UPLOAD, source); proc.communicate(timeout=10)
    assert proc.returncode == -signal.SIGKILL
    left = {p.name for p in d.glob(".run-*/rclone.conf*")}
    assert left - {"rclone.conf"}, "rclone 写配置的临时文件或旧文件应当留在副本目录里"
    for name in ("FAKE_GATE", "FAKE_REFRESH", "FAKE_SAVE_CRASH"): monkeypatch.delenv(name)
    if action == "query": backup._run("about", "gdrive:", "--json")
    elif action == "disconnect": backup.disconnect()
    elif action == "clear": backup.save_client(clear=True)
    else: run.recover()
    assert not list(d.glob(".run-*"))


@pytest.mark.parametrize("action", ["disconnect", "clear"])
def test_r4_killed_after_disconnect_is_reaped_when_worker_restarts(cloud, home, monkeypatch, action):
    from assetizer.topics import backup, run
    d = home / "secrets"; gate = home / "kill-gate"; source = home / "source"; source.write_bytes(b"source")
    monkeypatch.setenv("FAKE_GATE", str(gate))
    proc = child(UPLOAD, source)
    try:
        wait_file(Path(str(gate) + ".started"))
        backup.disconnect() if action == "disconnect" else backup.save_client(clear=True)
        assert list(d.glob(".run-*/rclone.conf")), "断开时正在进行的调用仍用自己的副本"
        proc.kill(); proc.communicate(timeout=5)
    finally:
        Path(str(gate) + ".release").touch()
    backup.view(); backup.available(); backup.due(); run.retention()
    assert list(d.glob(".run-*")), "只读路径不清理（S4）"
    run.recover()
    assert not list(d.glob(".run-*"))


@pytest.mark.parametrize("how", ["remote_deleted", "folder_switched"])
def test_r4_reupload_drops_the_old_confirmation(stand_in, cloud, home, monkeypatch, how):
    from assetizer import db
    from assetizer.topics import backup, media_dir, run
    read_once(stand_in); backup.due()
    assert db.one("SELECT backup_at FROM video_backups WHERE id=?", VID)["backup_at"]
    source = media_dir(VID) / "source.mkv"; digest = backup._md5(source)
    if how == "remote_deleted":
        target = cloud[0]; (target / VID / "source.mkv").unlink()
    else:
        target = cloud[0].parent / "新文件夹"; target.mkdir()
        fid = str(target.relative_to(home / "fake-drive"))
        db.set_setting("gdrive", {"folder_id": fid, "folder_path": fid}); backup._update_conf(root_folder_id=fid)
    age = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=run.knobs.get("retention_days") + 1)).isoformat()
    db.exec("UPDATE videos SET used_at=? WHERE id=?", age, VID)
    db.exec("UPDATE video_backups SET backup_due=1,backup_gen=backup_gen+1 WHERE id=?", VID)
    monkeypatch.setenv("FAKE_POST_COPY_STAT", str(home / "stat-once")); monkeypatch.setenv("FAKE_CORRUPT_UPLOAD", "1")
    backup.due()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert not v["backup_at"] and v["backup_src_md5"] == digest
    run.retention()
    assert source.exists(), "重新上传还没核对，旧的「已备份」不能让清理提前删掉原片"
    monkeypatch.delenv("FAKE_POST_COPY_STAT"); monkeypatch.delenv("FAKE_CORRUPT_UPLOAD")
    db.exec("UPDATE video_backups SET backup_failed_at=NULL WHERE id=?", VID)
    before = len(logs(cloud)); backup.due()
    assert not any(a[2] == "copyto" for a in logs(cloud)[before:])
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert not v["backup_at"] and v["backup_bad_md5"] == backup._md5(target / VID / "source.mkv")
    assert backup.video_view(v)["state"] == "failed"
    run.retention()
    assert source.exists() and not db.one("SELECT backup_missed_at FROM video_backups WHERE id=?", VID)["backup_missed_at"]


@pytest.mark.parametrize("failure", ["HTTP 401 Unauthorized: invalid_grant", "Error 403: storageQuotaExceeded"])
def test_r4_refresh_within_the_same_login_keeps_the_failure(stand_in, cloud, home, monkeypatch, failure):
    from assetizer import db
    from assetizer.topics import backup
    read_once(stand_in)
    gate = home / "refresh-gate"
    monkeypatch.setenv("FAKE_GATE", str(gate)); monkeypatch.setenv("FAKE_GATE_ERROR", failure)
    th = threading.Thread(target=backup.due); th.start()
    try:
        wait_file(Path(str(gate) + ".started"))
        # 同一次登录里另一次调用刷新并写回：只换了 access token
        backup._update_conf(token=json.dumps({**TOKEN, "access_token": "ya29.refreshed-elsewhere"}))
    finally:
        Path(str(gate) + ".release").touch(); th.join(5)
    assert not th.is_alive()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert v["backup_failed_at"] and v["backup_error"] and v["backup_due"] == 1
    assert len(db.q("SELECT * FROM events WHERE kind='backup' AND level='warn'")) == 1
    assert backup.view()["state"] == ("auth" if "401" in failure else "failing")


def test_r4_relogin_between_attribution_and_failure_write(stand_in, cloud, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup
    from assetizer.web.app import _throttle_at
    read_once(stand_in)
    monkeypatch.setenv("FAKE_FAIL", "HTTP 401 Unauthorized"); monkeypatch.setenv("FAKE_FAIL_CMD", "copyto")
    reached, go, real = threading.Event(), threading.Event(), backup._failed
    def paused(vid, error):
        reached.set(); assert go.wait(5); real(vid, error)
    monkeypatch.setattr(backup, "_failed", paused)
    th = threading.Thread(target=backup.due); th.start()
    try:
        assert reached.wait(5)
        assert backup.view()["state"] == "auth"  # rclone 调用当时的登录确实失效了
        backup._update_conf(token=json.dumps({**TOKEN, "access_token": "ya29.new-login", "refresh_token": "1//new-login"}))
        _throttle_at.pop("gdrive", None)
        response = api().post("/api/gdrive/test", json={})
        assert response.status_code == 200 and response.json()["state"] == "ok"
    finally:
        go.set(); th.join(5)
    assert not th.is_alive()
    v = db.one("SELECT * FROM videos LEFT JOIN video_backups USING(id) WHERE id=?", VID)
    assert v["backup_due"] == 1 and v["backup_failed_at"] is None and v["backup_error"] is None
    assert backup.view()["state"] == "ok"
    assert not db.q("SELECT * FROM events WHERE kind='backup' AND level='warn'")


def test_r4_identity_without_refresh_token_falls_back_to_the_whole_token(cloud, monkeypatch):
    from assetizer.topics import backup
    bare = {k: v for k, v in TOKEN.items() if k != "refresh_token"}
    backup._update_conf(token=json.dumps(bare))
    monkeypatch.setenv("FAKE_FAIL", "network refused")
    with pytest.raises(backup.BackupError) as error: backup._run("about", "gdrive:", "--json")
    assert not error.value.stale, "没有 refresh token 也要按当前登录记失败"
    backup._update_conf(token=json.dumps({**bare, "access_token": "ya29.other"}))
    with backup._locked_conf():
        assert not backup._current(error.value) and error.value.stale


def test_r4_cleanup_failures_do_not_stop_the_worker_or_disconnect(cloud, home, monkeypatch):
    from assetizer import db
    from assetizer.topics import backup, run
    d = home / "secrets"
    (d / ".run-stuck").mkdir(); (d / ".run-stuck" / "rclone.conf").write_text("synthetic leftover")
    (d / ".run-stuck.lock").touch()
    real_remove, real_lock = backup._remove, backup._locked_conf
    def refuse(path): raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(backup, "_remove", refuse)  # 例如残留带了不可变标记
    run.recover()
    view = backup.disconnect()
    assert view["state"] == "login" and "token" not in backup._read_conf()
    monkeypatch.setattr(backup, "_remove", real_remove)
    def readonly(): raise OSError(30, "Read-only file system")
    monkeypatch.setattr(backup, "_locked_conf", readonly)  # 例如卷被重新挂成只读
    run.recover()
    assert db.q("SELECT * FROM events WHERE kind='backup' AND level='warn' AND message LIKE '%清理网盘配置副本失败%'")
    monkeypatch.setattr(backup, "_locked_conf", real_lock)
    run.recover()
    assert not list(d.glob(".run-*"))
