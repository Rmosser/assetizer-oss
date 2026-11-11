"""规则、频道、清理、回收站与 API 合约。所有媒体、OAuth、rclone 都是临时替身。"""
import datetime as dt
import dataclasses
import gc
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_topic_backup import VID, OTHER, api, cloud, logs  # noqa: F401
from assetizer import config, db, knobs, youtube
from assetizer.topics import backup, backup_rules as rules, draft_dir, media_dir, run
from assetizer.pipeline import acquire as A, runner
from assetizer.pipeline.context import RunContext, Waiting
from assetizer.util import now, sha256_file

# d97754b 时的 db.py 与 topics/backup.py；这个提交不在远端分支上，新克隆取不到，所以原样存一份
BASELINE = Path(__file__).parent / 'fixtures' / 'backup-baseline-d97754b'

AUTHOR = "UCabcdefghijklmnopqrstuv"


@pytest.fixture
def home(tmp_path):
    # 本文件有大量参数化库；不要沿用旧 fixture 只丢引用的做法，把 FD 压力带到后面的 agy select 测试。
    config.override(dataclasses.replace(config.load(), home=tmp_path))
    previous = getattr(db._local, "conn", None)
    if previous is not None:
        previous.close()
    db._local.conn = None
    yield tmp_path
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
    db._local.conn = None
    gc.collect()  # 已结束的传输线程里 sqlite 连接的循环引用也在这里回收


def seed(vid=VID, *, line="topic", choice=None, author=AUTHOR, availability="public", cached=True, age=0):
    stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age)).isoformat()
    info = {"channel_id": author, "channel": "作者", "title": vid}
    if availability is not None:
        info["availability"] = availability
    if line == "topic":
        tid = db.exec("INSERT INTO topics(question,backup,created_at,updated_at) VALUES ('问题',?,?,?)", choice, stamp, stamp)
        p = media_dir(vid) / "source.mkv"
        db.exec("""INSERT INTO videos(id,url,title,channel,youtube_channel_id,info,media_path,media_state,used_at,created_at)
                   VALUES (?,? ,?,'作者',?,?,?, ?,?,?)""", vid, "url", vid, author, db.j(info), str(p) if cached else None,
                "CACHED" if cached else "PURGED", stamp, stamp)
        db.exec("INSERT INTO topic_videos(topic_id,video_id,no,state,added_at) VALUES (?,?,1,'DONE',?)", tid, vid, stamp)
    else:
        tid = None
        p = config.get().media_dir / vid / "source.mkv"
        db.exec("""INSERT INTO sources(id,url,title,channel_title,youtube_channel_id,info,media_path,media_state,delivery,delivered_at,updated_at,created_at)
                   VALUES (?,? ,?,'作者',?,?,?,?,'DELIVERED',?,?,?)""", vid, "url", vid, author, db.j(info), str(p) if cached else None,
                "CACHED" if cached else "PURGED", stamp, stamp, stamp)
    if cached:
        p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes((line + vid).encode())
    rules.track(vid)
    return tid, p


def author_rule(member=None, public=None):
    db.exec("INSERT OR REPLACE INTO backup_authors(id,name,member,public) VALUES (?,'作者',?,?)", AUTHOR, member, public)


def confirmed(vid=VID):
    fid = backup._folder()["folder_id"]
    db.exec("UPDATE video_backups SET backup_at=?,backup_folder=?,backup_bytes=42 WHERE id=?", now(), fid, vid)


def wait_delete():
    until = time.monotonic() + 8
    while backup._busy("batch"):
        assert time.monotonic() < until, "删除线程没有结束"
        time.sleep(.01)


@pytest.mark.parametrize("member,public,topic,availability,expected", [
    (0, 0, 0, "public", False), (1, 0, 0, "public", False), (0, 1, 0, "public", True),
    (0, 0, 1, "public", True), (None, None, 1, "public", True), (0, None, 0, "public", None),
    (0, 0, None, "public", None), (1, 0, 0, "subscriber_only", True),
    (1, 0, 0, "premium_only", True), (0, 1, 0, "subscriber_only", False),
    (0, 1, 0, None, True), (1, 0, 0, None, False),
])
def test_decision_matrix(home, member, public, topic, availability, expected):
    seed(choice=topic, availability=availability); author_rule(member, public)
    assert rules.decision(VID) is expected


def test_multiple_topics_removed_archived_unknown_author(home):
    tid, _ = seed(choice=0); author_rule(0, 0)
    other = db.exec("INSERT INTO topics(question,backup,archived_at,created_at,updated_at) VALUES ('归档',1,?,?,?)", now(), now(), now())
    db.exec("INSERT INTO topic_videos(topic_id,video_id,no,state,added_at) VALUES (?,?,1,'DONE',?)", other, VID, now())
    assert rules.decision(VID) is True
    db.exec("DELETE FROM topic_videos WHERE topic_id=?", other)
    db.exec("INSERT INTO topic_removed(topic_id,video_id,item,removed_at) VALUES (?,?, '{}',?)", other, VID, now())
    assert rules.decision(VID) is False
    db.exec("UPDATE videos SET youtube_channel_id=NULL,info='{}'")
    db.exec("UPDATE video_backups SET youtube_channel_id=NULL")
    assert rules.decision(VID) is None
    db.exec("UPDATE topics SET backup=1 WHERE id=?", tid)
    assert rules.decision(VID) is True


def test_migration_once_legacy_and_inventory_survive_restart(home):
    seed(choice=None)
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('existing','老频道',?,?,?)", AUTHOR, now(), now())
    c = db.connect()
    legacy = {"backup_due": ("INTEGER", 1), "backup_gen": ("INTEGER", 7), "backup_at": ("TEXT", "checked"),
              "backup_folder": ("TEXT", "fid"), "backup_bytes": ("INTEGER", 123), "backup_draft_at": ("TEXT", "draft"),
              "backup_draft_folder": ("TEXT", "fid"), "backup_src_md5": ("TEXT", "pending"), "backup_bad_md5": ("TEXT", "bad"),
              "backup_error": ("TEXT", "error"), "backup_failed_at": ("TEXT", "failed"), "backup_missed_at": ("TEXT", "missed"),
              "restored_at": ("TEXT", "restored"), "draft_restored_at": ("TEXT", "draft-restored")}
    for key, (kind, value) in legacy.items():
        c.execute(f"ALTER TABLE videos ADD COLUMN {key} {kind}")
        c.execute(f"UPDATE videos SET {key}=?", (value,))
    db.set_setting("gdrive_inventory", {"folder": "fid", "videos": {OTHER: {"bytes": 88, "md5": "old"}}})
    db.exec("DELETE FROM backup_authors")  # 旧版没有规则表
    db.exec("DELETE FROM settings WHERE key='migrated:backup_rules_v1'")
    db._migrate(c)
    migrated = db.one("SELECT * FROM video_backups WHERE id=?", VID)
    for key, (_, value) in legacy.items():
        assert migrated[key] == (None if key == "backup_at" else value)
    assert db.one("SELECT backup_bytes FROM video_backups WHERE id=?", OTHER)["backup_bytes"] == 88
    assert db.setting("gdrive_inventory") is None
    assert not any(r["name"].startswith("backup_") for r in db.q("PRAGMA table_info(videos)"))
    assert db.one("SELECT backup FROM topics")["backup"] == 1
    assert db.one("SELECT member,public FROM backup_authors WHERE id=?", AUTHOR) == {"member": 1, "public": 1}
    assert db.one("SELECT youtube_channel_id FROM videos")["youtube_channel_id"] == AUTHOR
    db.exec("UPDATE topics SET backup=0"); author_rule(0, 0)
    # 新频道和专题不在预填集合；重复迁移、关闭重开均不能改变手工选择。
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('new','新','UCnew',?,?)", now(), now())
    new = db.exec("INSERT INTO topics(question,created_at,updated_at) VALUES ('新专题',?,?)", now(), now())
    db._migrate(c); c.close(); db._local.conn = None
    db.connect(); rules.sync()
    assert db.one("SELECT backup FROM topics WHERE id=1")["backup"] == 0
    assert db.one("SELECT backup FROM topics WHERE id=?", new)["backup"] is None
    assert db.one("SELECT member,public FROM backup_authors WHERE id=?", AUTHOR) == {"member": 0, "public": 0}
    assert db.one("SELECT member,public FROM backup_authors WHERE id='UCnew'") == {"member": None, "public": None}


def test_rules_atomic_validation_queue_and_dequeue(cloud):
    tid, _ = seed(choice=0); author_rule(0, 0)
    assert not rules.queue(VID)
    with pytest.raises(ValueError):
        rules.update({"topics": [{"id": tid, "backup": True}, {"id": 999, "backup": False}]})
    assert rules.decision(VID) is False
    rules.update({"authors": [{"id": AUTHOR, "public": True}]})
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 1
    rules.update({"authors": [{"id": AUTHOR, "public": False}]})
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 0
    for value in (None, 1, "true"):
        with pytest.raises(ValueError):
            rules.update({"topics": [{"id": tid, "backup": value}]})


def test_channel_acquire_queues_uploads_original_only_and_deduplicates(cloud, monkeypatch):
    _, p = seed(line="source", cached=False); author_rule(0, 1)
    def download(url, dest):
        dest.mkdir(parents=True); p.write_bytes(b"channel-download"); return p
    monkeypatch.setattr(youtube, "download", download)
    monkeypatch.setattr(A.ffmpeg, "probe", lambda _: {"streams": [{"codec_type": "video", "height": 1080}], "format": {"duration": "60"}})
    ctx = RunContext({"id": "channel"}, db.one("SELECT * FROM sources"))
    assert A.acquire(ctx)["height"] == 1080
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 1
    backup.due()
    assert (cloud[0] / VID / "source.mkv").read_bytes() == b"channel-download"
    assert not (cloud[0] / VID / "draft").exists()
    source_copies = len([c for c in logs(cloud) if c[2] == "copyto"])
    # 两条线独立下载，文件不同；已核对的一份已足够，不比本机另一条线的 md5。
    tid, other_path = seed(choice=1)
    assert other_path.read_bytes() != p.read_bytes()
    rules.queue(VID, force=True); backup.due()
    state = db.one("SELECT * FROM video_backups")
    assert state["backup_at"] and not state["backup_error"] and not state["backup_bad_md5"]
    assert len([c for c in logs(cloud) if c[2] == "copyto"]) == source_copies
    assert api().get("/api/gdrive/rules").json()["authors"][0]["videos"] == 1
    assert rules.view()["topics"][0]["drive"]["videos"] == 1


@pytest.mark.parametrize("kind", ["VIDEO_REMOVED", "VIDEO_PRIVATE", "AUTH_REQUIRED", "YOUTUBE_REFUSED", "TOOL_OUTDATED"])
@pytest.mark.parametrize("height", [720, 480])
def test_channel_rescue_preserves_errors_and_checks_height(cloud, monkeypatch, kind, height):
    _, p = seed(line="source", cached=False); author_rule(0, 0)
    remote = cloud[0] / VID / "source.mkv"; remote.parent.mkdir(); remote.write_bytes(b"remote")
    def fail(*args):
        raise youtube.AcquireError(kind, "original-error")
    monkeypatch.setattr(youtube, "download", fail)
    monkeypatch.setattr(A.ffmpeg, "probe", lambda _: {"streams": [{"codec_type": "video", "height": height}], "format": {"duration": "60"}})
    ctx = RunContext({"id": "channel"}, db.one("SELECT * FROM sources"))
    if height >= 720:
        result = A.acquire(ctx)
        assert result["sha256"] == sha256_file(p)
        source = db.one("SELECT * FROM sources")
        assert source["media_state"] == "CACHED" and source["media_height"] == 720 and source["media_bytes"] == 6
        assert db.one("SELECT restored_at FROM video_backups")["restored_at"]
        assert db.q("SELECT * FROM events WHERE message LIKE '%已从网盘取回原片%'")
    else:
        with pytest.raises((Waiting, youtube.AcquireError), match="original-error"):
            A.acquire(ctx)
        assert not p.exists() and db.one("SELECT media_state FROM sources")["media_state"] == "PURGED"
        assert not db.one("SELECT restored_at FROM video_backups")["restored_at"]


@pytest.mark.parametrize("kind", ["NETWORK", "RATE_LIMITED"])
def test_channel_transient_errors_never_restore(cloud, monkeypatch, kind):
    seed(line="source", cached=False)
    def fail(*args): raise youtube.AcquireError(kind, "transient")
    monkeypatch.setattr(youtube, "download", fail)
    monkeypatch.setattr(backup, "restore_source", lambda *a, **k: pytest.fail("不应从网盘取回"))
    with pytest.raises(Waiting):
        A.acquire(RunContext({"id": "channel"}, db.one("SELECT * FROM sources")))


@pytest.mark.parametrize("line", ["topic", "source"])
@pytest.mark.parametrize("choice", [True, None, False])
@pytest.mark.parametrize("copy", [True, False])
@pytest.mark.parametrize("low", [True, False])
@pytest.mark.parametrize("expired", [True, False])
def test_retention_matrix(cloud, monkeypatch, line, choice, copy, low, expired):
    days = knobs.get("retention_days" if line == "topic" else "delivered_retention_days")
    _, p = seed(line=line, choice=0, age=days + (8 if expired else 1))
    author_rule(0, choice)
    if copy: confirmed()
    monkeypatch.setattr(run.shutil, "disk_usage", lambda _: __import__('shutil')._ntuple_diskusage(10**12, 0, 1 if low else 10**12))
    (run.retention if line == "topic" else runner.retention)()
    grace = choice is None or (choice is True and not copy)
    deleted = not (grace and not low and not expired)
    assert (not p.exists()) == deleted
    assert bool(db.one("SELECT backup_missed_at FROM video_backups")["backup_missed_at"]) == (deleted and choice is True and not copy)


@pytest.mark.parametrize("line", ["topic", "source"])
def test_retention_protects_claims_and_active_work(cloud, line):
    _, p = seed(line=line, choice=0, age=100); author_rule(0, 0)
    fn = run.retention if line == "topic" else runner.retention
    with backup.claim(VID) as free:
        assert free
        fn(); assert p.exists()
    if line == "topic":
        db.exec("UPDATE topic_videos SET state='RUNNING'")
    else:
        db.exec("INSERT INTO runs(id,source_id,production,created_at) VALUES ('active',?,'WAITING',?)", VID, now())
    fn(); assert p.exists()


def test_pending_alert_earliest_rounds_up_and_resolves_without_push(cloud, monkeypatch):
    days = knobs.get("retention_days")
    t, _ = seed(choice=0, age=days + 1.2); author_rule(0, None)
    backup.pending_alert()
    a = db.one("SELECT * FROM alerts WHERE key='BACKUP_RULES_PENDING'")
    assert a["title"] == "1 期原片 6 天后清理，作者还没定"
    assert db.unj(a["action"]) == {"label": "去设置", "href": "/settings#gdrive"}
    seed(OTHER, choice=None, age=days + 5.2)
    backup.pending_alert()
    assert db.one("SELECT title FROM alerts")["title"] == "2 期原片 2 天后清理，备份规则还没定"
    from assetizer import notify
    told = []; monkeypatch.setattr(notify, "send", lambda *a, **k: told.append(a))
    db.exec("UPDATE alerts SET created_at='2000-01-01'"); notify.send_due_alerts()
    assert not told and "BACKUP_RULES_PENDING" not in notify.URGENT
    author_rule(0, 1); backup.pending_alert()
    assert db.one("SELECT resolved_at FROM alerts")["resolved_at"]


def test_delete_trashes_entire_directory_and_state_then_can_requeue(cloud):
    tid, _ = seed(choice=0); author_rule(0, 1)
    d = draft_dir(VID); d.mkdir(parents=True); (d / "full.md").write_text("draft")
    rules.queue(VID); backup.due(); author_rule(0, 0)
    assert backup.deletable()["total"]["videos"] == 1
    result = backup.start_delete([VID]); assert result["deleting"] == {"total": 1, "done": 0}
    wait_delete()
    assert not (cloud[0] / VID).exists()
    trash = list((cloud[0].parents[1] / ".trash").rglob("source.mkv"))
    assert len(trash) == 1 and (trash[0].parent / "draft" / "full.md").exists()
    v = db.one("SELECT * FROM video_backups")
    assert not v["backup_at"] and not v["backup_draft_at"] and not v["backup_due"]
    assert backup.restore_source(VID, config.get().media_dir / VID / "source.mkv") is None
    assert rules.view()["authors"][0]["drive"] == {"videos": 0, "bytes": 0}
    rules.update({"authors": [{"id": AUTHOR, "public": True}]})
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 1
    backup.due(); assert (cloud[0] / VID / "source.mkv").exists()
    assert [c for c in logs(cloud) if c[2] == "purge"][0][3:] == ["gdrive:" + VID, "--drive-use-trash=true"]


@pytest.mark.parametrize("path,flags", [("gdrive:", ["--drive-use-trash=true"]), ("gdrive:" + VID + "/draft", ["--drive-use-trash=true"]),
    ("gdrive:../" + VID, ["--drive-use-trash=true"]), ("gdrive:" + VID, ["--drive-use-trash=false"]),
    ("gdrive:" + VID, ["--drive-use-trash=true", "--drive-use-trash=false"])])
def test_delete_command_rejects_invalid_paths_and_flags(cloud, path, flags):
    before = len(logs(cloud))
    with pytest.raises(ValueError): backup._run("purge", path, *flags)
    assert len(logs(cloud)) == before


def test_delete_checks_root_identity(cloud):
    backup._update_conf(root_folder_id="wrong")
    with pytest.raises(backup.BackupError, match="文件夹"):
        backup._run("purge", "gdrive:" + VID, "--drive-use-trash=true")
    assert not any(c[2] == "purge" for c in logs(cloud))


def test_deletable_last_copy_kept_group_and_unknown_author(cloud):
    t, p = seed(choice=0); author_rule(0, 0); confirmed()
    p.unlink(); db.exec("UPDATE videos SET gone='VIDEO_REMOVED'")
    result = api().get("/api/gdrive/deletable").json()
    assert set(result["last_copy"][0]) == {"id", "title", "bytes", "author"}
    assert set(result["last_copy"][0]["author"]) == {"id", "name"}
    assert result["authors"] == [] and result["last_copy"][0]["author"]["id"] == AUTHOR
    assert result["total"] == {"videos": 1, "bytes": 42}
    db.exec("UPDATE topics SET backup=1 WHERE id=?", t)
    result = api().get("/api/gdrive/deletable").json()
    assert result["total"]["videos"] == 0 and result["kept"] == [{"topic": {"id": t, "question": "问题"}, "videos": 1}]
    db.exec("UPDATE topics SET backup=0"); author_rule(0, 1)
    kept = api().get("/api/gdrive/deletable").json()["kept"]
    assert kept == [{"author": {"id": AUTHOR, "name": "作者"}, "videos": 1}]
    author_rule(0, 0)
    db.exec("UPDATE topics SET backup=0"); db.exec("UPDATE videos SET youtube_channel_id=NULL,info='{}'")
    db.exec("UPDATE video_backups SET youtube_channel_id=NULL")
    assert backup.deletable()["total"]["videos"] == 0


def test_delete_rechecks_rules_after_listing_and_claim_conflicts(cloud):
    t, _ = seed(choice=0); author_rule(0, 0); confirmed()
    assert backup.deletable()["total"]["videos"] == 1
    db.exec("UPDATE topics SET backup=1")
    backup.start_delete([VID]); wait_delete()
    assert not any(c[2] == "purge" for c in logs(cloud))
    db.exec("UPDATE topics SET backup=0")
    with backup.claim(VID) as free:
        assert free and backup.deletable()["total"]["videos"] == 0
        backup.start_delete([VID]); wait_delete()
    assert not any(c[2] == "purge" for c in logs(cloud))
    assert len(db.q("SELECT * FROM events WHERE message LIKE '%已不可删除%'") ) == 2


def test_delete_batch_409_progress_and_auth_stops(cloud, monkeypatch):
    seed(choice=0); seed(OTHER, choice=0); author_rule(0, 0); confirmed(); confirmed(OTHER)
    entered, release = threading.Event(), threading.Event()
    original = backup._run
    def slow(cmd, *args, **kwargs):
        if cmd == "purge":
            entered.set(); assert release.wait(5)
        return original(cmd, *args, **kwargs)
    monkeypatch.setattr(backup, "_run", slow)
    c = api(); response = c.post("/api/gdrive/delete", json={"videos": [VID, OTHER]})
    assert response.status_code == 202 and response.json()["deleting"] == {"total": 2, "done": 0}
    assert entered.wait(3)
    try:
        assert c.post("/api/gdrive/delete", json={"videos": [OTHER]}).status_code == 409
        assert backup.view()["deleting"] == {"total": 2, "done": 0}
        monkeypatch.setenv("FAKE_FAIL", "invalid_grant"); monkeypatch.setenv("FAKE_FAIL_CMD", "purge")
    finally:
        release.set(); wait_delete()
    assert len([c for c in logs(cloud) if c[2] == "purge"]) == 1
    assert backup.view()["state"] == "auth" and backup.view()["deleting"] is None
    assert all(v["backup_at"] for v in db.q("SELECT * FROM video_backups"))


def test_delete_single_failure_continues(cloud, monkeypatch):
    seed(choice=0); seed(OTHER, choice=0); author_rule(0, 0); confirmed(); confirmed(OTHER)
    calls = []
    original = backup._run
    def fail_one(cmd, *args, **kwargs):
        if cmd == "purge":
            calls.append(args[0])
            if len(calls) == 1: raise backup.BackupError("network")
        return original(cmd, *args, **kwargs)
    monkeypatch.setattr(backup, "_run", fail_one)
    backup.start_delete([VID, OTHER]); wait_delete()
    assert len(calls) == 2
    assert db.one("SELECT backup_at FROM video_backups WHERE id=?", VID)["backup_at"]
    assert not db.one("SELECT backup_at FROM video_backups WHERE id=?", OTHER)["backup_at"]


def test_cross_process_claim_excludes_delete_upload_restore(cloud):
    seed(choice=0); author_rule(0, 0); confirmed()
    code = """import dataclasses,sys,time
from assetizer import config
from assetizer.topics import backup
from pathlib import Path
config.override(dataclasses.replace(config.load(),home=Path(sys.argv[1])))
with backup.claim(sys.argv[2]) as free:
 print(free,flush=True)
 sys.stdin.readline()
"""
    p = subprocess.Popen([sys.executable, "-c", code, str(config.get().home), VID], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "True"
        assert backup.uploading(VID) and backup.deletable()["total"]["videos"] == 0
        with backup.claim(VID) as free: assert not free
        assert backup.restore_source(VID, config.get().home / "restored.mkv") is None
        backup.start_delete([VID]); wait_delete()
        assert not any(c[2] == "purge" for c in logs(cloud))
    finally:
        p.communicate("release\n", timeout=5)


def test_api_contract_all_fields_and_not_ready(home):
    c = api()
    for route in ("ready", "rules", "deletable"):
        assert c.get("/api/gdrive/" + route).json() == {"ready": False}
    for method, path, body in (("put", "/api/gdrive/rules", {}), ("post", "/api/gdrive/delete", {"videos": [VID]})):
        assert getattr(c, method)(path, json=body).status_code == 409
        assert getattr(c, method)(path, content="{}").status_code == 415
    g = c.get("/api/settings").json()["gdrive"]
    assert all(g[k] is None for k in ("rules", "deletable", "deleting"))
    r = c.post("/api/topics", json={"question": "新问题", "videos": [{"id": VID}]})
    assert r.status_code == 200 and db.one("SELECT backup FROM topics")["backup"] is None


def test_api_contract_ready_rules_channel_new_topic_delete(cloud):
    tid, _ = seed(choice=0); author_rule(0, 0); confirmed()
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('c','频道',?,?,?)", AUTHOR, now(), now())
    db.exec("INSERT INTO channels(id,name,created_at,updated_at) VALUES ('noid','无 ID',?,?)", now(), now())
    c = api(); assert c.get("/api/gdrive/ready").json() == {"ready": True}
    result = c.get("/api/gdrive/rules").json()
    assert set(result) == {"ready", "authors", "topics"}
    a = result["authors"][0]
    assert set(a) == {"id", "name", "channel", "member", "public", "videos", "drive"}
    assert a["channel"] == {"id": "c", "name": "频道"} and a["videos"] == 1 and a["drive"] == {"videos": 1, "bytes": 42}
    assert set(result["topics"][0]) == {"id", "question", "backup", "videos", "drive"}
    assert result["topics"][0]["drive"] == {"videos": 1, "bytes": 42}
    result = c.get("/api/channels/c/backup").json()
    assert set(result) == {"ready", "author", "member", "public", "drive", "deletable"}
    assert result["deletable"] == {"videos": 1, "bytes": 42}
    assert result["author"] == {"id": AUTHOR, "name": "频道"} and result["member"] is False and result["public"] is False
    assert c.get("/api/channels/noid/backup").json() == {"ready": False}
    assert c.put("/api/channels/noid/backup", json={"public": True}).status_code == 409
    assert c.put("/api/channels/c/backup", json={"member": True}).json()["member"] is True
    assert c.put("/api/gdrive/rules", json={"authors": [{"id": AUTHOR, "public": False}], "topics": [{"id": tid, "backup": False}]}).status_code == 200
    assert c.put("/api/gdrive/rules", json={"authors": [{"id": "unknown", "public": True}]}).status_code == 400
    assert c.put("/api/channels/c/backup", json={"public": None}).status_code == 400
    t = c.post("/api/topics", json={"question": "新问题", "backup": True, "videos": [{"id": OTHER}]}).json()
    assert db.one("SELECT backup FROM topics WHERE id=?", t["id"])["backup"] == 1
    g = c.get("/api/settings").json()["gdrive"]
    assert set(g) == {"rclone", "client", "token", "folder", "state", "error", "login", "usage", "queue", "uploading", "rules", "deletable", "deleting"}
    assert set(g["rules"]) == {"authors", "topics"} and set(g["rules"]["authors"]) == {"on", "off", "pending"}
    assert set(g["rules"]["topics"]) == {"on", "off", "pending"}
    assert set(g["queue"]) == {"due", "failed", "missed"} and set(g["deletable"]) == {"videos", "bytes"}
    result = c.get("/api/gdrive/deletable").json()
    assert set(result) == {"ready", "authors", "last_copy", "kept", "total"}
    assert set(result["authors"][0]) == {"id", "name", "bytes", "videos"}
    assert set(result["authors"][0]["videos"][0]) == {"id", "title", "bytes"}
    assert c.post("/api/gdrive/delete", json={"videos": [VID]}).status_code == 202
    wait_delete()


def test_lock_backup_is_atomic_with_confirmation(cloud, monkeypatch):
    from assetizer import tuning
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('c','频道',?,?,?)", AUTHOR, now(), now())
    pid = db.exec("INSERT INTO playbooks(channel_id,version,status,data,created_at) VALUES ('c',1,'NEEDS_CONFIRMATION','{}',?)", now())
    monkeypatch.setattr(tuning, "tested", lambda *a: True)
    def fail(*a, **kw): raise tuning.LockRefused("拒绝定稿")
    original = tuning.confirm
    monkeypatch.setattr(tuning, "confirm", fail)
    c = api(); body = {"playbook_id": pid, "backup": {"member": True, "public": False}}
    assert c.post("/api/channels/c/lock", json=body).status_code == 409
    assert db.one("SELECT member FROM backup_authors WHERE id=?", AUTHOR) is None
    monkeypatch.setattr(tuning, "confirm", lambda *a, **k: db.exec("UPDATE channels SET stage='LOCKED' WHERE id='c'"))
    assert c.post("/api/channels/c/lock", json=body).status_code == 200
    assert db.one("SELECT member,public FROM backup_authors WHERE id=?", AUTHOR) == {"member": 1, "public": 0}


@pytest.mark.parametrize("failure", ["md5", "probe"])
def test_channel_restore_rejects_bad_md5_or_probe(cloud, monkeypatch, failure):
    seed(line="source", cached=False)
    remote = cloud[0] / VID / "source.mkv"; remote.parent.mkdir(); remote.write_bytes(b"remote")
    def fail_download(*args): raise youtube.AcquireError("AUTH_REQUIRED", "keep-original-error")
    monkeypatch.setattr(youtube, "download", fail_download)
    if failure == "md5":
        monkeypatch.setenv("FAKE_BAD_MD5", "1")
        # 隔离 md5 门槛：反向检查删掉 md5 判断时，ffprobe 必须成功，不能替它挡住坏副本。
        monkeypatch.setattr(A.ffmpeg, "probe", lambda _: {"streams": [{"codec_type": "video", "height": 1080}], "format": {"duration": "60"}})
    else:
        def bad_probe(*args): raise RuntimeError("ffprobe failed")
        monkeypatch.setattr(A.ffmpeg, "probe", bad_probe)
    with pytest.raises(Waiting, match="keep-original-error"):
        A.acquire(RunContext({"id": "channel"}, db.one("SELECT * FROM sources")))
    assert db.one("SELECT media_state,media_sha256 FROM sources") == {"media_state": "PURGED", "media_sha256": None}
    assert not (config.get().media_dir / VID / "source.mkv").exists()
    assert not db.q("SELECT * FROM events WHERE message LIKE '%已从网盘取回原片%'")


def test_rules_queue_channel_cache_and_draft_only(cloud):
    _, p = seed(line="source"); author_rule(0, 0)
    assert not rules.queue(VID)
    rules.update({"authors": [{"id": AUTHOR, "public": True}]})
    assert db.one("SELECT backup_due FROM video_backups WHERE id=?", VID)["backup_due"] == 1
    t, _ = seed(OTHER, choice=0, cached=False)
    d = draft_dir(OTHER); d.mkdir(parents=True); (d / "full.md").write_text("draft-only")
    rules.reconcile(); backup.due()
    assert (cloud[0] / VID / "source.mkv").exists()
    assert not (cloud[0] / OTHER / "source.mkv").exists()
    assert (cloud[0] / OTHER / "draft" / "full.md").read_text() == "draft-only"
    r = rules.view(); a = r["authors"][0]
    assert a["drive"]["videos"] == 2 and a["drive"]["bytes"] == len(p.read_bytes()) + 10
    author_rule(0, 0)
    assert backup.deletable()["total"]["videos"] == 2


def test_rule_counters_and_sorts_and_shared_channel_fallback(cloud):
    t, _ = seed(choice=None); author_rule(None, True)
    seed(OTHER, choice=False, author="UCother")
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('c','频道','UCother',?,?)", now(), now())
    r = rules.view()
    assert r["authors"][0]["id"] == "UCother" and r["authors"][0]["channel"]["id"] == "c"
    assert [t["id"] for t in r["topics"]] == [2, 1]
    counts = backup.view()["rules"]
    assert counts == {"authors": {"on": 0, "off": 0, "pending": 2}, "topics": {"on": 0, "off": 1, "pending": 1}}
    db.exec("INSERT INTO sources(id,channel_id,created_at,updated_at) VALUES ('source-test','c',?,?)", now(), now())
    assert rules.metadata("source-test")["youtube_channel_id"] == "UCother"


def test_draft_size_readback_counts_remote_extras(cloud):
    seed(choice=1); author_rule(0, 0)
    d = draft_dir(VID); d.mkdir(parents=True); (d / "full.md").write_text("draft")
    remote = cloud[0] / VID / "draft"; remote.mkdir(parents=True); (remote / "extra.bin").write_bytes(b"extra")
    rules.queue(VID); backup.due()
    assert db.one("SELECT backup_draft_bytes FROM video_backups")["backup_draft_bytes"] == 10
    assert (remote / "extra.bin").read_bytes() == b"extra"


def wait_file(p):
    until = time.monotonic() + 6
    while not p.exists():
        assert time.monotonic() < until, str(p)
        time.sleep(.01)


@pytest.mark.parametrize("transfer", ["upload", "restore"])
def test_delete_races_real_rclone_upload_and_restore(cloud, monkeypatch, transfer):
    _, local = seed(choice=0); author_rule(0, 1); confirmed()
    remote = cloud[0] / VID / "source.mkv"; remote.parent.mkdir(); remote.write_bytes(local.read_bytes())
    gate = config.get().home / "transfer-gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    if transfer == "upload":
        d = draft_dir(VID); d.mkdir(parents=True); (d / "full.md").write_text("draft")
        monkeypatch.setenv("FAKE_GATE_CMD", "copy")
        rules.queue(VID, force=True)
        fn = backup.due
    else:
        local.unlink(); monkeypatch.setenv("FAKE_GATE_DIRECTION", "restore")
        monkeypatch.setattr(A.ffmpeg, "probe", lambda _: {"streams": [{"codec_type": "video", "height": 1080}], "format": {"duration": "60"}})
        fn = lambda: backup.restore_source(VID)
    th = threading.Thread(target=fn); th.start()
    try:
        wait_file(Path(str(gate) + ".started")); author_rule(0, 0)
        assert backup.deletable()["total"]["videos"] == 0
        backup.start_delete([VID]); wait_delete()
        assert not any(c[2] == "purge" for c in logs(cloud))
    finally:
        Path(str(gate) + ".release").touch(); th.join(6)
    assert not th.is_alive() and remote.exists()


def test_delete_failure_from_old_login_is_stale(cloud, monkeypatch):
    from test_topic_backup import TOKEN
    seed(choice=0); author_rule(0, 0); confirmed()
    gate = config.get().home / "delete-gate"
    monkeypatch.setenv("FAKE_GATE", str(gate)); monkeypatch.setenv("FAKE_GATE_CMD", "purge")
    monkeypatch.setenv("FAKE_GATE_ERROR", "invalid_grant old-login")
    before = len(db.q("SELECT * FROM events"))
    backup.start_delete([VID])
    try:
        wait_file(Path(str(gate) + ".started"))
        backup._update_conf(token=json.dumps({**TOKEN, "refresh_token": "new-login"}))
        backup._state()
    finally:
        Path(str(gate) + ".release").touch(); wait_delete()
    assert backup.view()["state"] == "ok" and db.one("SELECT backup_at FROM video_backups")["backup_at"]
    assert len(db.q("SELECT * FROM events")) == before


def test_lock_omitted_backup_does_not_change_rules(cloud, monkeypatch):
    from assetizer import tuning
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('c','频道',?,?,?)", AUTHOR, now(), now())
    pid = db.exec("INSERT INTO playbooks(channel_id,version,status,data,created_at) VALUES ('c',1,'NEEDS_CONFIRMATION','{}',?)", now())
    monkeypatch.setattr(tuning, "tested", lambda *a: True)
    monkeypatch.setattr(tuning, "confirm", lambda *a, **k: db.exec("UPDATE channels SET stage='LOCKED' WHERE id='c'"))
    author_rule(False, True)
    assert api().post("/api/channels/c/lock", json={"playbook_id": pid}).status_code == 200
    assert db.one("SELECT member,public FROM backup_authors") == {"member": 0, "public": 1}


def test_orphan_without_draft_or_queue_needs_no_draft_grace(cloud):
    _, p = seed(choice=0, age=31); author_rule(0, 1); confirmed()
    db.exec("DELETE FROM topic_videos")
    assert run.retention()["dropped"] == 1
    assert not db.one("SELECT 1 FROM videos WHERE id=?", VID)
    assert db.one("SELECT backup_at FROM video_backups WHERE id=?", VID)["backup_at"]


@pytest.mark.parametrize("action", ["restore", "readd"])
def test_restored_done_topic_video_requeues_by_current_rule(cloud, action):
    t, _ = seed(choice=True); author_rule(0, 0); rules.queue(VID)
    c = api()
    assert c.post(f"/api/topics/{t}/videos/{VID}", json={"action": "remove"}).status_code == 200
    rules.reconcile()
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 0
    if action == "restore":
        response = c.post(f"/api/topics/{t}/videos/{VID}", json={"action": "restore"})
    else:
        response = c.post(f"/api/topics/{t}/videos", json={"videos": [{"id": VID}]})
    assert response.status_code == 200
    assert db.one("SELECT state FROM topic_videos")["state"] == "DONE"
    assert db.one("SELECT backup_due FROM video_backups")["backup_due"] == 1

# 第二轮：以下期望按故障序列与独立期限推导，不从被测实现计算。
def child_code(body, *args):
    prelude = '''import dataclasses,sys,os,json,time,signal
from pathlib import Path
from assetizer import config,db
from assetizer.topics import backup,backup_rules as rules
config.override(dataclasses.replace(config.load(),home=Path(sys.argv[1]),rclone_bin=sys.argv[2]))
'''
    return [sys.executable, '-B', '-c', prelude + body, str(config.get().home), config.get().rclone_bin, *map(str,args)]


def test_r2_a1_bad_and_pending_never_verified(cloud):
    seed(choice=1, age=knobs.get('retention_days')+1)
    confirmed()
    for field in ('backup_bad_md5','backup_src_md5'):
        db.exec(f'UPDATE video_backups SET {field}=?', 'unverified')
        assert not backup.verified(VID) and len(backup._copies()) == 0
        assert run.retention()['purged'] == 0
        db.exec(f'UPDATE video_backups SET {field}=NULL')


@pytest.mark.parametrize('after', [False, True])
def test_r2_a2_sigkill_persistent_delete_intent(cloud, after):
    _, local = seed(choice=0, age=knobs.get('retention_days')+1)
    author_rule(0,1); rules.queue(VID); backup.due(); author_rule(0,0)
    code = '''orig=backup._invoke
def crash(cmd,*args,**kw):
 if cmd=='purge' and not AFTER:os.kill(os.getpid(),9)
 result=orig(cmd,*args,**kw)
 if cmd=='purge':os.kill(os.getpid(),9)
 return result
backup._invoke=crash
backup.start_delete([sys.argv[3]])
time.sleep(20)
'''.replace('AFTER',repr(after))
    p = subprocess.run(child_code(code, VID), capture_output=True, text=True, timeout=15)
    assert p.returncode == -9, p.stderr
    assert bool((cloud[0]/VID/'source.mkv').exists()) is (not after)
    raw = db.one('SELECT delete_intent FROM video_backups')['delete_intent']
    assert raw, 'purge 前必须持久记录删除意图'
    intent = db.unj(raw)
    assert intent['folder'] == backup._folder()['folder_id'] and len(intent['identity']) == 64 and intent['started_at']
    assert not backup.verified(VID) and len(backup._copies()) == 0
    # 网络未知时不能依靠旧确认清理，也不能上传到尚未核对的目录。
    with pytest.MonkeyPatch.context() as m:
        m.setenv('FAKE_FAIL', 'network unavailable'); m.setenv('FAKE_FAIL_CMD','lsjson')
        rules.update({'authors':[{'id':AUTHOR,'public':True}]})
        backup.due()
        assert run.retention()['purged'] == 0 and local.exists()
        assert db.one('SELECT delete_intent FROM video_backups')['delete_intent']
    backup.reap()  # 启动不再同步查网盘；只由后台在退避到期后核对
    assert db.one('SELECT delete_intent FROM video_backups')['delete_intent']
    retry_at=db.unj(db.one('SELECT delete_intent FROM video_backups')['delete_intent'])['retry_at']
    with pytest.MonkeyPatch.context() as m:
        m.setattr(backup.time,'time',lambda:retry_at+1)
        backup.recover_deletions()
    assert not db.one('SELECT delete_intent FROM video_backups')['delete_intent']
    assert backup.verified(VID) is (not after)
    if after:
        assert db.one('SELECT backup_due FROM video_backups')['backup_due'] == 1
        assert run.retention()['purged'] == 0 and local.exists()
    else:
        assert (cloud[0]/VID/'source.mkv').exists()


@pytest.mark.parametrize('identity', [False,True])
def test_r2_a3_snapshot_rejects_changed_destination(cloud,monkeypatch,identity):
    seed(choice=0,cached=False); author_rule(0,0); confirmed()
    old=cloud[0]/VID/'source.mkv';old.parent.mkdir();old.write_bytes(b'approved')
    newroot=cloud[0].parents[1]/'unapproved';new=newroot/VID/'source.mkv';new.parent.mkdir(parents=True);new.write_bytes(b'unapproved')
    entered,release=threading.Event(),threading.Event(); original=backup._invoke
    def gate(cmd,*args,**kw):
        if cmd=='purge':entered.set();assert release.wait(10)
        return original(cmd,*args,**kw)
    monkeypatch.setattr(backup,'_invoke',gate)
    backup.start_delete([VID]);assert entered.wait(5)
    try:
        with backup._locked_conf():
            if identity:
                from test_topic_backup import TOKEN
                backup._update_conf(token=json.dumps({**TOKEN,'refresh_token':'new-login'}))
            else:
                backup._update_conf(root_folder_id='unapproved');db.set_setting('gdrive',{'folder_id':'unapproved'})
    finally:release.set();wait_delete()
    assert old.exists() and new.exists() and not any(x[2]=='purge' for x in logs(cloud))
    assert db.one("SELECT 1 FROM events WHERE message LIKE '%跳过%'")


@pytest.mark.parametrize('torn', [False, True])
def test_r2_a4_two_process_rule_transfer_before_purge(cloud,monkeypatch,torn):
    tid,_=seed(choice=0,cached=False);author_rule(0,0);confirmed()
    remote=cloud[0]/VID/'source.mkv';remote.parent.mkdir();remote.write_bytes(b'only-copy')
    if torn:db.exec('UPDATE topics SET backup=1 WHERE id=?',tid)
    entered,release=threading.Event(),threading.Event(); one=db.one; original=backup._run
    def gated_one(sql,*args):
        result=one(sql,*args)
        if torn and threading.current_thread().name=='gdrive-delete' and sql=='SELECT * FROM backup_authors WHERE id=?' and not entered.is_set():
            entered.set();assert release.wait(10)
        return result
    def gated_run(cmd,*args,**kw):
        if not torn and cmd=='purge':entered.set();assert release.wait(10)
        return original(cmd,*args,**kw)
    monkeypatch.setattr(db,'one',gated_one);monkeypatch.setattr(backup,'_run',gated_run)
    backup.start_delete([VID]);assert entered.wait(5)
    try:
        body={'authors':[{'id':AUTHOR,'public':True}], 'topics':[{'id':tid,'backup':False}]}
        p=subprocess.run(child_code("rules.update(json.loads(sys.argv[3]));print('committed')",json.dumps(body)), capture_output=True,text=True,timeout=8)
        assert p.returncode==0 and 'committed' in p.stdout,p.stderr
    finally:release.set();wait_delete()
    assert remote.exists() and rules.decision(VID) is True
    assert not any(x[2]=='purge' for x in logs(cloud))


def test_r2_a4_writer_wait_bounded_and_off_event_loop(cloud,monkeypatch):
    seed(choice=0);author_rule(0,0);confirmed()
    gate=config.get().home/'held-purge'
    monkeypatch.setenv('FAKE_GATE',str(gate));monkeypatch.setenv('FAKE_GATE_CMD','purge')
    backup.start_delete([VID]);wait_file(Path(str(gate)+'.started'))
    try:
        result=[]
        t=threading.Thread(target=lambda:result.append(api().put('/api/gdrive/rules',json={'authors':[{'id':AUTHOR,'public':True}]})))
        t.start();time.sleep(.1)
        start=time.monotonic();assert api().get('/api/gdrive/ready').status_code==200
        assert time.monotonic()-start<1
        t.join(5);assert result[0].status_code==409
    finally:Path(str(gate)+'.release').touch();wait_delete()


@pytest.mark.parametrize('days',[1,7,30,35,60])
@pytest.mark.parametrize('form',['linked','orphan','removed','deleted','both'])
@pytest.mark.parametrize('low',[False,True])
def test_r2_a5_deadline_alert_cleanup_matrix(cloud,monkeypatch,days,form,low):
    import math, shutil
    from assetizer.topics import backup_retention as R
    base=dt.datetime.now(dt.timezone.utc)
    tid,local=seed(choice=0);author_rule(0,None)
    stamp=base.isoformat();db.exec('UPDATE videos SET used_at=?,created_at=?',stamp,stamp)
    db.set_setting('retention_days',days);db.set_setting('delivered_retention_days',days+10)
    if form in ('orphan','removed'):db.exec('DELETE FROM topic_videos')
    if form=='removed':db.exec("INSERT INTO topic_removed(topic_id,video_id,item,removed_at) VALUES (?,?,'{}',?)",tid,VID,(base-dt.timedelta(days=8)).isoformat())
    if form=='deleted':db.exec('DELETE FROM topics WHERE id=?',tid)
    source=None
    if form=='both':
        _,source=seed(line='source');db.exec('UPDATE sources SET delivered_at=?,updated_at=?',stamp,stamp)
    monkeypatch.setattr(run.shutil,'disk_usage',lambda _:shutil._ntuple_diskusage(10**12,0,1 if low else 10**12))
    # 孤立行有独立 30 天删除路径；作者待定只增加 7 天，低水位为 0。
    normal=min(days,30) if form in ('orphan','removed','deleted','both') else days
    expiry=base+dt.timedelta(days=normal+(0 if low else 7))
    class Clock(dt.datetime):
        current=expiry-dt.timedelta(days=2)
        @classmethod
        def now(cls,tz=None):return cls.current if tz else cls.current.replace(tzinfo=None)
    monkeypatch.setattr(R.dt,'datetime',Clock)
    expected=R.plan(VID)['media_at'];assert expected==expiry
    for remaining in (2*86400,1):
        Clock.current=expiry-dt.timedelta(seconds=remaining)
        backup.pending_alert()
        alert=db.one("SELECT * FROM alerts WHERE key='BACKUP_RULES_PENDING' AND resolved_at IS NULL")
        if not low:
            assert alert and f'{math.ceil(remaining/86400)} 天后清理' in alert['title']
        run.retention();runner.retention()
        assert local.exists() and (source is None or source.exists())
    Clock.current=expiry+dt.timedelta(seconds=1)
    run.retention();runner.retention()
    assert not local.exists() or (source is not None and not source.exists())
    if not local.exists():
        assert not db.one("SELECT 1 FROM alerts WHERE key='BACKUP_RULES_PENDING' AND resolved_at IS NULL")


def baseline_db(home):
    # 用真实基线代码创建旧库，避免手写旧表结构遗漏历史迁移。
    code='''source=Path(BASELINE_DIR,'db.py').read_text(encoding='utf-8')
exec(compile(source,'baseline-db.py','exec'),db.__dict__)
db.connect()
db.exec("INSERT INTO topics(question,created_at,updated_at) VALUES ('old','2026-01-01','2026-01-01')")
db.exec("INSERT INTO videos(id,url,created_at,backup_at,backup_folder,backup_gen) VALUES ('abcdefghijk','url','2026-01-01','checked','folder',8)")
'''
    subprocess.run(child_code(code.replace('BASELINE_DIR',repr(str(BASELINE)))),check=True,capture_output=True,text=True)


def test_r2_a6_real_processes_migrate_after_barrier_and_rollback(home):
    import sqlite3
    baseline_db(home)
    path=config.get().db_path
    with sqlite3.connect(path) as c:before=list(c.iterdump())
    class Fail:
        def __init__(self,c):self.c=c;self.n=0
        def execute(self,sql,*args):
            if sql.startswith('ALTER TABLE videos DROP COLUMN'):
                self.n+=1
                if self.n==3:raise sqlite3.OperationalError('injected third DROP failure')
            return self.c.execute(sql,*args)
    c=sqlite3.connect(path,isolation_level=None);c.row_factory=sqlite3.Row
    with pytest.raises(sqlite3.OperationalError,match='injected'):db._migrate(Fail(c))
    assert list(c.iterdump())==before
    c.close()
    code='''import sqlite3
name=sys.argv[3];root=config.get().home
(root/(name+'.ready')).touch()
while not all((root/(x+'.ready')).exists() for x in ('web','worker')):time.sleep(.005)
c=sqlite3.connect(config.get().db_path,isolation_level=None,timeout=15);c.row_factory=sqlite3.Row
# 屏障在任何建表/列检查之前；只有拿到 BEGIN IMMEDIATE 的进程才能读旧列集。
db._migrate(c)
print('ok',flush=True)
'''
    ps=[subprocess.Popen(child_code(code,n),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for n in ('web','worker')]
    for p in ps:
        stdout,stderr=p.communicate(timeout=20);assert p.returncode==0 and 'ok' in stdout,stderr
    assert db.one('SELECT backup_gen FROM video_backups')['backup_gen']==8
    assert db.one('SELECT backup FROM topics')['backup']==1
    db.exec('UPDATE topics SET backup=0');db._migrate(db.connect())
    assert db.one('SELECT backup FROM topics')['backup']==0


@pytest.mark.parametrize('old_duration',[None,999])
def test_r2_a7_restore_media_fields_equal_download(cloud,monkeypatch,old_duration):
    _,p=seed(line='source',cached=False);author_rule(0,0)
    probe={'streams':[{'codec_type':'video','height':1080}], 'format':{'duration':'123.25'}}
    monkeypatch.setattr(A.ffmpeg,'probe',lambda _:probe)
    monkeypatch.setattr(A,'now',lambda:'2026-11-11T12:00:00+00:00')
    payload=b'identical-media'
    def download(*args):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload);return p
    monkeypatch.setattr(youtube,'download',download)
    db.exec('UPDATE sources SET duration=?',old_duration)
    A.acquire(RunContext({'id':'c'},db.one('SELECT * FROM sources')))
    fields=('media_path','media_sha256','media_bytes','media_height','media_state','duration','updated_at')
    normal={k:db.one('SELECT * FROM sources')[k] for k in fields}
    assert normal=={'media_path':str(p),'media_sha256':hashlib.sha256(payload).hexdigest(),'media_bytes':15,'media_height':1080,'media_state':'CACHED','duration':123.25,'updated_at':'2026-11-11T12:00:00+00:00'}
    remote=cloud[0]/VID/'source.mkv';remote.parent.mkdir();remote.write_bytes(payload)
    p.unlink();db.exec("UPDATE sources SET media_state='PURGED',media_path=NULL,media_sha256=NULL,media_bytes=NULL,media_height=NULL,duration=?",old_duration)
    def unavailable(*a):raise youtube.AcquireError('VIDEO_REMOVED','gone')
    monkeypatch.setattr(youtube,'download',unavailable)
    A.acquire(RunContext({'id':'c'},db.one('SELECT * FROM sources')))
    restored=db.one('SELECT * FROM sources')
    assert {k:restored[k] for k in fields}==normal
    assert restored['updated_at'] and db.one('SELECT restored_at FROM video_backups')['restored_at']


def test_r2_a8_get_readonly_noop_and_affected_retry(cloud,monkeypatch):
    t,_=seed(choice=1);seed(OTHER,choice=0);author_rule(0,0)
    db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('c','频道',?,?,?)",AUTHOR,now(),now())
    rules.queue(VID);db.exec("UPDATE video_backups SET backup_failed_at='2026-11-11',backup_error='network' WHERE id=?",VID)
    before=db.one('SELECT * FROM video_backups WHERE id=?',VID)
    c=api();original=db.connect;writes=[]
    def traced():
        conn=original();conn.set_trace_callback(lambda sql:writes.append(sql) if sql.lstrip().upper().startswith(('UPDATE ','INSERT ','DELETE ','REPLACE ')) else None);return conn
    monkeypatch.setattr(db,'connect',traced)
    for path in ('/api/settings','/api/gdrive/rules','/api/channels/c/backup','/api/gdrive/deletable','/api/gdrive/ready'):
        assert c.get(path).status_code==200
    assert writes==[]
    for body in ({},{'topics':[]},{'topics':[{'id':t,'backup':True}]}):assert c.put('/api/gdrive/rules',json=body).status_code==200
    rules.queue(VID)
    assert db.one('SELECT * FROM video_backups WHERE id=?',VID)==before
    assert c.put('/api/gdrive/rules',json={'topics':[{'id':2,'backup':True}]}).status_code==200
    assert db.one('SELECT * FROM video_backups WHERE id=?',VID)==before


@pytest.mark.parametrize('line',['topic','source'])
def test_r2_a9_disconnected_unavailable_fact_and_c1(cloud,monkeypatch,line):
    from test_topic_backup import TOKEN
    tid,p=seed(line=line,choice=0);author_rule(0,1);rules.queue(VID);backup.due()
    p.unlink();db.exec(f"UPDATE {'videos' if line=='topic' else 'sources'} SET media_state='PURGED'")
    backup.disconnect()
    def removed(*a):raise youtube.AcquireError('VIDEO_REMOVED','known removed')
    monkeypatch.setattr(youtube,'download',removed)
    if line=='source':
        with pytest.raises(youtube.AcquireError):A.acquire(RunContext({'id':'c'},db.one('SELECT * FROM sources')))
    else:
        reader=object.__new__(run.Job);reader.vid=VID;reader.video=db.one('SELECT * FROM videos')
        with pytest.raises(youtube.AcquireError):reader.acquire()
    assert db.one('SELECT gone FROM video_backups')['gone']=='VIDEO_REMOVED'
    backup._update_conf(token=json.dumps(TOKEN));author_rule(0,0)
    c=api();db.exec("INSERT INTO channels(id,name,youtube_channel_id,created_at,updated_at) VALUES ('c','频道',?,?,?)",AUTHOR,now(),now())
    listing=c.get('/api/gdrive/deletable').json();assert listing['authors']==[] and listing['last_copy'][0]['id']==VID
    expected={'videos':1,'bytes':listing['last_copy'][0]['bytes']}
    assert c.get('/api/channels/c/backup').json()['deletable']==expected
    assert c.put('/api/channels/c/backup',json={'public':False}).json()['deletable']==expected


def test_r2_a1_baseline_upload_corruption_then_migrate(cloud):
    # 子进程加载签名基线真实实现：成功上传、远端丢失、重新上传损坏。
    # 使用独立小库，不让当前 fixture 的已迁移数据库污染初始状态。
    oldhome=config.get().home/'legacy-evolution'
    code='''import hashlib,shutil,datetime as dt
from assetizer.topics import media_dir
for module,path in ((db,'db.py'),(backup,'topics/backup.py')):
 source=Path(BASELINE_DIR,path).read_text(encoding='utf-8')
 exec(compile(source,'baseline-'+path,'exec'),module.__dict__)
config.override(dataclasses.replace(config.get(),home=Path(sys.argv[3])))
db.connect();db.set_setting('gdrive',{'folder_id':'folder'})
t=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=8)).isoformat()
db.exec("INSERT INTO topics(question,created_at,updated_at) VALUES ('old',?,?)",t,t)
local=media_dir('abcdefghijk')/'source.mkv';local.parent.mkdir(parents=True);local.write_bytes(b'good-original')
db.exec("INSERT INTO videos(id,url,created_at,used_at,media_state,media_path) VALUES ('abcdefghijk','url',?,?,'CACHED',?)",t,t,str(local))
db.exec("INSERT INTO topic_videos(topic_id,video_id,no,state,added_at) VALUES (1,'abcdefghijk',1,'DONE',?)",t)
remote=config.get().home/'remote'
backup._stat=lambda _: {'Size':remote.stat().st_size,'Hashes':{'md5':hashlib.md5(remote.read_bytes()).hexdigest()}} if remote.exists() else None
backup._run=lambda *a:shutil.copyfile(local,remote)
backup._upload(db.one('SELECT * FROM videos'))
remote.unlink();backup._run=lambda *a:remote.write_bytes(b'corrupt')
try:backup._upload(db.one('SELECT * FROM videos'))
except backup.BackupError:pass
assert db.one('SELECT backup_at,backup_bad_md5,backup_src_md5 FROM videos')['backup_at'] is None
# inventory-only 的视频没有任何旧本地行。
inv=db.setting('gdrive_inventory');inv['videos']['lmnopqrstuv']={'bytes':19,'md5':'inventory-only'};db.set_setting('gdrive_inventory',inv)
'''
    subprocess.run(child_code(code.replace('BASELINE_DIR',repr(str(BASELINE))),oldhome),check=True,capture_output=True,text=True)
    config.override(dataclasses.replace(config.get(),home=oldhome))
    db.connect()
    monkey=pytest.MonkeyPatch()
    try:
        monkey.setattr(backup,'available',lambda:True)
        monkey.setattr(run.shutil,'disk_usage',lambda _:__import__('shutil')._ntuple_diskusage(10**12,0,10**12))
        assert not backup.verified(VID)
        assert db.one('SELECT backup_folder FROM video_backups WHERE id=?',VID)['backup_folder'] is None
        assert backup.verified(OTHER)
        assert run.retention()['purged']==0 and rules.source_path(VID).read_bytes()==b'good-original'
    finally:monkey.undo()


def test_r2_a4_decision_snapshot_across_process_transfer(cloud,monkeypatch):
    tid,_=seed(choice=1);author_rule(0,0)
    entered,release=threading.Event(),threading.Event();one=db.one;result=[]
    def gate(sql,*args):
        row=one(sql,*args)
        if threading.current_thread().name=='rule-reader' and sql=='SELECT * FROM backup_authors WHERE id=?':
            entered.set();assert release.wait(10)
        return row
    monkeypatch.setattr(db,'one',gate)
    thread=threading.Thread(target=lambda:result.append(rules.decision(VID)),name='rule-reader');thread.start()
    assert entered.wait(5)
    try:
        body={'authors':[{'id':AUTHOR,'public':True}],'topics':[{'id':tid,'backup':False}]}
        p=subprocess.run(child_code("rules.update(json.loads(sys.argv[3]))",json.dumps(body)),capture_output=True,text=True,timeout=8)
        assert p.returncode==0,p.stderr
    finally:release.set();thread.join(5)
    assert result==[True], '每个已提交状态都要保留，不能拼出不传'


def test_r2_a9_uncertain_online_without_local_defaults_last_copy(cloud):
    seed(line='source',choice=0,cached=False);author_rule(0,0);confirmed()
    assert rules.metadata(VID)['gone'] is None
    assert backup.deletable()['authors']==[]
    assert backup.deletable()['last_copy'][0]['id']==VID


def test_r2_a5_orphan_drop_marks_missing_original(cloud,monkeypatch):
    import shutil
    db.set_setting('retention_days',60)
    seed(choice=1,age=38);author_rule(0,1);db.exec('DELETE FROM topic_videos')
    monkeypatch.setattr(run.shutil,'disk_usage',lambda _:shutil._ntuple_diskusage(10**12,0,10**12))
    assert run.retention()=={'purged':0,'dropped':1}
    assert db.one('SELECT backup_missed_at FROM video_backups')['backup_missed_at']

# 第三轮：判定与破坏动作必须同锁，远端不确定时持续保盘。
def deletion_intent(vid=VID):
    intent = {'folder':backup._folder()['folder_id'], 'identity':backup._identity_key(backup._read_conf()),
              'started_at':now()}
    db.exec('UPDATE video_backups SET delete_intent=? WHERE id=?',db.j(intent),vid)
    return intent


@pytest.mark.parametrize('response', ['file', '', 'not-json', '[]', '{"IsDir":true,"Name":"other"}'])
def test_r3_a16_invalid_stat_keeps_intent_and_original(cloud, monkeypatch, response):
    _,local=seed(choice=0,age=knobs.get('retention_days')+1)
    author_rule(0,1);rules.queue(VID);backup.due();author_rule(0,0)
    code='''original=backup._invoke
def crash(cmd,*args,**kw):
 result=original(cmd,*args,**kw)
 if cmd=='purge':os.kill(os.getpid(),9)
 return result
backup._invoke=crash
backup.start_delete([sys.argv[3]])
time.sleep(20)
'''
    killed=subprocess.run(child_code(code,VID),capture_output=True,text=True,timeout=12)
    assert killed.returncode==-9,killed.stderr
    remote=cloud[0]/VID
    remote.write_bytes(b'ordinary file')
    rules.update({'authors':[{'id':AUTHOR,'public':True}]})
    original=backup._run
    def stat(cmd,*args,**kwargs):
        if cmd=='lsjson' and '--stat' in args and response!='file':return response
        return original(cmd,*args,**kwargs)
    monkeypatch.setattr(backup,'_run',stat)
    backup.recover_deletions()
    assert not backup.verified(VID), '普通文件或无效 stat 不能恢复旧确认'
    assert db.one('SELECT delete_intent FROM video_backups')['delete_intent']
    assert run.retention()['purged']==0 and local.exists()
    # 同类响应重复失败仍只记一条；绕过退避以覆盖去重。
    for _ in range(2):
        intent=db.unj(db.one('SELECT delete_intent FROM video_backups')['delete_intent']);intent['retry_at']=0
        db.exec('UPDATE video_backups SET delete_intent=?',db.j(intent));backup.recover_deletions()
    assert len(db.q("SELECT 1 FROM events WHERE kind='backup' AND level='warn' AND message LIKE '%删除状态未决%'"))==1


@pytest.mark.parametrize('path_only',[False,True])
def test_r3_a16_directory_stat_restores_confirmation(cloud,monkeypatch,path_only):
    seed(choice=0);author_rule(0,0);confirmed();deletion_intent()
    value={'IsDir':True, 'Path':'prefix/'+VID} if path_only else {'IsDir':True,'Name':VID}
    monkeypatch.setattr(backup,'_run',lambda *a,**kw:json.dumps(value))
    backup.recover_deletions()
    assert backup.verified(VID) and not db.one('SELECT delete_intent FROM video_backups')['delete_intent']


@pytest.mark.parametrize('line',['source','topic','track'])
def test_r4_a11_metadata_during_purge(cloud,monkeypatch,line):
    _,local=seed(line='source' if line=='source' else 'topic',choice=0,availability=None)
    if line=='topic':db.exec('UPDATE videos SET info=NULL')
    author_rule(1,1);rules.queue(VID);backup.due()
    author_rule(1,0)
    gate=config.get().home/'r3-metadata'
    monkeypatch.setenv('FAKE_GATE',str(gate));monkeypatch.setenv('FAKE_GATE_CMD','purge')
    monkeypatch.setattr(youtube,'metadata',lambda _: {'channel_id':AUTHOR,'channel':'作者','title':VID,
        'availability':'subscriber_only','duration':60,'upload_date':'20261111'})
    backup.start_delete([VID]);wait_file(Path(str(gate)+'.started'))
    finished=threading.Event();errors=[]
    def resolve():
        try:
            if line=='source':A.resolve(RunContext({'id':'r3'},db.one('SELECT * FROM sources')))
            elif line=='topic':run.Job(1,VID).describe()
            else:rules.track(VID,gone='VIDEO_UNAVAILABLE')
        except Exception as e:errors.append(e)
        finally:finished.set()
    monkeypatch.setattr(rules,'WRITE_WAIT',.03)
    th=threading.Thread(target=resolve);th.start()
    start=time.monotonic()
    try:
        assert finished.wait(.5), 'rclone 执行期间元数据不等规则锁'
        assert time.monotonic()-start<.5
        assert not errors
        if line=='topic':assert 'subscriber_only' in db.one('SELECT info FROM videos')['info']
        if line=='source':assert 'subscriber_only' in db.one('SELECT info FROM sources')['info']
        if line=='track':assert db.one('SELECT gone FROM video_backups')['gone']=='VIDEO_UNAVAILABLE'
    finally:Path(str(gate)+'.release').touch();th.join(8);wait_delete()
    assert finished.is_set() and not errors
    assert local.exists() and not (cloud[0]/VID).exists()
    assert rules.decision(VID) is (False if line=='track' else True)
    assert db.one('SELECT backup_due FROM video_backups')['backup_due']==(0 if line=='track' else 1)
    warnings=db.q("SELECT message FROM events WHERE message LIKE '%刚移到网盘回收站%'")
    assert len(warnings)==(0 if line=='track' else 1)
    if warnings:assert warnings[0]['message']==f'{VID} 刚移到网盘回收站，但现在规则是传，30 天内可在 Google 网盘回收站恢复'



@pytest.mark.parametrize('line',['topic','source','orphan'])
def test_r3_a15_rule_on_before_cleanup_final_check(cloud,monkeypatch,line):
    import contextlib
    from assetizer.topics import backup_retention as R
    days=30 if line=='orphan' else knobs.get('retention_days' if line=='topic' else 'delivered_retention_days')
    _,local=seed(line='source' if line=='source' else 'topic',choice=0,age=days+1);author_rule(0,0)
    if line=='orphan':db.exec('DELETE FROM topic_videos');db.set_setting('retention_days',60)
    entered,release=threading.Event(),threading.Event();original=rules.cleanup;errors=[];result=[]
    @contextlib.contextmanager
    def paused(vid):
        if not entered.is_set():entered.set();assert release.wait(5)
        with original(vid) as free:yield free
    monkeypatch.setattr(rules,'cleanup',paused)
    original_plan=R.plan
    def checked(*args,**kwargs):
        import inspect
        if threading.current_thread().name=='r3-cleanup' and inspect.currentframe().f_back.f_code.co_name=='retention':
            if entered.is_set():assert getattr(rules._held,'depth',0), '最终期限必须在 mutation 内重算'
        return original_plan(*args,**kwargs)
    monkeypatch.setattr(R,'plan',checked)
    def clean():
        try:result.append((runner.retention if line=='source' else run.retention)())
        except Exception as e:errors.append(e)
    th=threading.Thread(target=clean,name='r3-cleanup');th.start();assert entered.wait(5)
    try:
        response=api().put('/api/gdrive/rules',json={'authors':[{'id':AUTHOR,'public':True}]})
        assert response.status_code==200 and local.exists()
        assert db.one('SELECT backup_due FROM video_backups')['backup_due']==1
    finally:release.set();th.join(8)
    assert not th.is_alive() and not errors
    assert local.exists() and result[0]['purged']==0
    assert db.one('SELECT backup_due,backup_missed_at FROM video_backups')=={'backup_due':1,'backup_missed_at':None}


@pytest.mark.parametrize('line',['topic','source','orphan'])
def test_r3_a15_cleanup_lock_timeout_skips(cloud,monkeypatch,line):
    days=max(50,knobs.get('retention_days'),knobs.get('delivered_retention_days'))
    _,local=seed(line='source' if line=='source' else 'topic',choice=0,age=days);author_rule(0,0)
    if line=='orphan':db.exec('DELETE FROM topic_videos')
    entered,release=threading.Event(),threading.Event()
    def hold():
        with rules.mutation():entered.set();release.wait(5)
    th=threading.Thread(target=hold);th.start();assert entered.wait(3)
    monkeypatch.setattr(rules,'WRITE_WAIT',.03)
    try:
        for _ in range(2):(runner.retention if line=='source' else run.retention)()
        assert local.exists()
        assert len(db.q("SELECT 1 FROM events WHERE kind='backup' AND message LIKE '%跳过本轮本地清理%'"))==1
    finally:release.set();th.join(5)


def test_r3_a12_recovery_only_background(cloud,monkeypatch):
    seed(choice=0);author_rule(0,0);confirmed();deletion_intent()
    entered,release=threading.Event(),threading.Event();original=backup._run
    def slow(cmd,*args,**kw):
        if cmd=='lsjson' and args==('gdrive:'+VID,'--stat'):
            entered.set();assert release.wait(8);raise backup.BackupError('network unavailable')
        return original(cmd,*args,**kw)
    monkeypatch.setattr(backup,'_run',slow)
    start=time.monotonic();backup.reap();run.retention();runner.retention()
    assert time.monotonic()-start<.5 and not entered.is_set()
    monkeypatch.setattr(run,'_last',{k:1000.0 for k in run._last}|{'backup':0.0})
    start=time.monotonic();run.housekeeping(1001.0);elapsed=time.monotonic()-start
    try:
        assert elapsed<.5 and entered.wait(3)
        start=time.monotonic();run.retention();runner.retention()
        assert time.monotonic()-start<.5, '未决内容的清理不能等远端核对'
    finally:release.set();run._watching['b'].join(8)
    assert not run._watching['b'].is_alive()


def test_r3_a12_auth_skip_and_per_video_backoff(cloud,monkeypatch):
    import shutil
    _,local=seed(choice=0,age=400);_,source=seed(line='source',age=400)
    author_rule(0,1);confirmed();deletion_intent()
    monkeypatch.setattr(run.shutil,'disk_usage',lambda _:shutil._ntuple_diskusage(10**12,0,1))
    monkeypatch.setenv('FAKE_FAIL','invalid_grant');monkeypatch.setenv('FAKE_FAIL_CMD','lsjson')
    backup._state('invalid_grant',auth=True)
    before=len(logs(cloud))
    for _ in range(10):run.retention();runner.retention();backup.due()
    assert len(logs(cloud))==before, '登录失效时不得远端核对'
    assert local.exists() and source.exists() and not backup.verified(VID)
    backup._state();backup.due()
    assert len([x for x in logs(cloud)[before:] if x[2]=='lsjson'])==1
    # 普通网络失败按期 1,2,4,...30 分钟退避，其他未决期不被一条拖到同一退避。
    backup._state();monkeypatch.setenv('FAKE_FAIL','network timeout')
    intent=db.unj(db.one('SELECT delete_intent FROM video_backups')['delete_intent'])
    next_at=intent['retry_at'];assert intent['attempts']==1
    clock=[next_at-1];monkeypatch.setattr(backup.time,'time',lambda:clock[0])
    before=len([x for x in logs(cloud) if x[2]=='lsjson']);backup.due()
    assert len([x for x in logs(cloud) if x[2]=='lsjson'])==before
    for attempt,minutes in [(2,2),(3,4),(4,8),(5,16),(6,30),(6,30)]:
        clock[0]=next_at+1;backup.due()
        intent=db.unj(db.one('SELECT delete_intent FROM video_backups')['delete_intent'])
        assert intent['attempts']==attempt and intent['retry_at']==clock[0]+minutes*60
        next_at=intent['retry_at']
    assert local.exists() and source.exists()
    assert len(db.q("SELECT 1 FROM events WHERE kind='backup' AND level='warn' AND message LIKE '%network timeout%'"))==1


def test_r3_a14_unconfigured_writes_do_not_make_gdrive_locks(home):
    response=api().post('/api/topics',json={'question':'没有网盘','videos':[{'id':VID}]})
    assert response.status_code==200,response.text
    tid=response.json()['id']
    assert api().post(f'/api/topics/{tid}/videos',json={'videos':[{'id':OTHER}]}).status_code==200
    assert api().post(f'/api/topics/{tid}/archive',json={'archived':True}).status_code==200
    assert not (home/'cache/gdrive-locks').exists()


def test_r3_a14_intent_keeps_shared_lock_when_disconnected(cloud):
    seed(choice=0);deletion_intent();backup.disconnect()
    with rules.mutation():assert (config.get().home/'cache/gdrive-locks/rules.lock').is_file()


def test_r3_a10_rollback_instructions():
    text=(Path(__file__).resolve().parents[1]/'docs/operations.md').read_text()
    rollback=text.split('**回滚**：')[1].split('## 容器里的命令行')[0]
    assert '只补列、表和索引' in rollback and '装过未合入的第一版网盘备份' in rollback
    assert '会丢网盘确认、待核对和坏 md5 状态' in rollback
    assert '先停 web、worker' in rollback and '升级前的数据库备份' in rollback
    assert '升级后新增的规则、副本记录' in rollback and '网盘上的文件不受影响' in rollback
    assert '低水位也不清' in rollback and '先重新登录' in rollback


def test_r3_a13_a_waiting_delete_is_let_in_between_back_to_back_writes(cloud):
    stop=threading.Event()
    def write():
        while not stop.is_set():
            with rules.mutation():time.sleep(.1)  # 慢机器上每次写都要占锁一阵，写方一个接一个
    writers=[threading.Thread(target=write) for _ in range(3)]
    for th in writers:th.start()
    time.sleep(.3)
    code='''import dataclasses,sys,time
from pathlib import Path
from assetizer import config
from assetizer.topics import backup_rules
config.override(dataclasses.replace(config.load(),home=Path(sys.argv[1])))
start=time.monotonic()
with backup_rules.mutation(writer=False):print(round(time.monotonic()-start,3))
'''
    try:
        r=subprocess.run([sys.executable,'-B','-c',code,str(config.get().home)],capture_output=True,text=True,timeout=20)
    finally:
        stop.set()
        for th in writers:th.join(5)
    assert r.returncode==0,r.stderr[-300:]
    assert float(r.stdout)<1


def test_r3_a13_batch_yields_to_cross_process_writers(cloud,monkeypatch):
    vids=[f'batch{i:06d}' for i in range(10)]
    for vid in vids:
        seed(vid,choice=0);confirmed(vid)
        p=cloud[0]/vid/'source.mkv';p.parent.mkdir();p.write_bytes(b'remote')
    tid,_=seed(OTHER,choice=0,author='UCunrelated');author_rule(0,0)
    db.exec("UPDATE backup_authors SET member=0,public=0 WHERE id='UCunrelated'")
    binary=Path(config.get().rclone_bin)
    binary.write_text(binary.read_text().replace('elif cmd == "purge":','elif cmd == "purge":\n    time.sleep(2.0)'))
    code='''import dataclasses,json,sys,time
from pathlib import Path
from assetizer import config
from assetizer.topics import backup
config.override(dataclasses.replace(config.load(),home=Path(sys.argv[1])))
backup.start_delete(json.loads(sys.argv[2]))
while backup._busy('batch'):time.sleep(.01)
'''
    start=time.monotonic()
    proc=subprocess.Popen([sys.executable,'-B','-c',code,str(config.get().home),json.dumps(vids)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    until=time.monotonic()+5
    while not any(x[2]=='purge' for x in logs(cloud)):
        assert time.monotonic()<until;time.sleep(.01)
    records={k:[] for k in ('rule','create','add','get')}
    # One persistent TestClient portal, so concurrent GETs share the event loop with async writes.
    with api() as client:
        def writer(kind):
            i=0;next_rule=True
            while proc.poll() is None:
                i+=1;t=time.monotonic()
                if kind=='rule':
                    r=client.put('/api/gdrive/rules',json={'authors':[{'id':'UCunrelated','public':next_rule}]})
                    if r.status_code==200:next_rule=not next_rule
                elif kind=='create':r=client.post('/api/topics',json={'question':'并发新专题','videos':[{'id':f'create{i:05d}'}]})
                elif kind=='add':r=client.post(f'/api/topics/{tid}/videos',json={'videos':[{'id':f'addvid{i:05d}'}]})
                else:r=client.get('/api/gdrive/ready')
                records[kind].append({'at':round(t-start,4),'seconds':round(time.monotonic()-t,4),'status':r.status_code})
                time.sleep(.1 if kind=='get' else .02)
        threads=[threading.Thread(target=writer,args=(k,)) for k in records]
        for th in threads:th.start()
        stdout,stderr=proc.communicate(timeout=65)
        for th in threads:th.join(8);assert not th.is_alive()
    summary={}
    for k,rs in records.items():
        times=sorted(r['seconds'] for r in rs)
        summary[k]={'requests':len(rs),'status_200':sum(r['status']==200 for r in rs),'status_409':sum(r['status']==409 for r in rs),
                    'min_seconds':times[0],'p50_seconds':times[len(times)//2],'max_seconds':times[-1],
                    'first_success_at':next((round(r['at']+r['seconds'],4) for r in rs if r['status']==200),None)}
    print('batch-ten-contention',{'purge_delay':2,'purges':len([x for x in logs(cloud) if x[2]=='purge']),
        'elapsed':round(time.monotonic()-start,4),'child_exit':proc.returncode,'summary':summary,'records':records,'stderr':stderr})
    assert proc.returncode==0 and len([x for x in logs(cloud) if x[2]=='purge'])==10

    assert all(summary[k]['status_409']==0 for k in ('rule','create','add')),summary
    assert all(summary[k]['status_200']>0 for k in ('rule','create','add'))



def r4_channel(stage='TUNING'):
    db.exec("INSERT INTO channels(id,name,stage,mode,youtube_channel_id,created_at,updated_at) VALUES ('test','测试',?,'TRACKED',?,?,?)", stage,AUTHOR,now(),now())
    return db.one("SELECT * FROM channels WHERE id='test'")


def r4_deleting(cloud,monkeypatch):
    seed(choice=0);author_rule(0,0);confirmed()
    p=cloud[0]/VID/'source.mkv';p.parent.mkdir();p.write_bytes(b'remote')
    gate=config.get().home/'r4-delete'
    monkeypatch.setenv('FAKE_GATE',str(gate));monkeypatch.setenv('FAKE_GATE_CMD','purge')
    backup.start_delete([VID]);wait_file(Path(str(gate)+'.started'))
    return gate


def r4_finish(gate):
    Path(str(gate)+'.release').touch();wait_delete()


def test_r4_a17_sample_during_delete(cloud,monkeypatch):
    r4_channel();gate=r4_deleting(cloud,monkeypatch)
    try:
        start=time.monotonic()
        response=api().post('/api/channels/test/samples',json={'url':'https://www.youtube.com/watch?v='+OTHER,'run':False})
        assert response.status_code==200,response.text
        assert time.monotonic()-start<.5
        assert db.one('SELECT channel_id FROM sources WHERE id=?',OTHER)['channel_id']=='test'
        assert db.one('SELECT source_id FROM samples')['source_id']==OTHER
        assert db.one('SELECT youtube_channel_id FROM video_backups WHERE id=?',OTHER)['youtube_channel_id']==AUTHOR
    finally:r4_finish(gate)


@pytest.mark.parametrize('method,path,body',[
    ('put','/api/gdrive/rules',{}),
    ('put','/api/channels/test/backup',{'public':True}),
    ('post','/api/channels/test/lock',{'playbook_id':1}),
    ('post','/api/topics',{'question':'并发建专题','videos':[{'id':OTHER}]}),
    ('post','/api/topics/1/videos',{'videos':[{'id':OTHER}]}),
    ('post','/api/topics/1/videos/'+VID,{'action':'remove'}),
    ('post','/api/topics/1/archive',{}),
    ('post','/api/topics/1/candidates/'+OTHER,{'take':True}),
    ('post','/api/topics/1/delete',{}),
])
def test_r4_a17_all_rule_write_timeouts_are_409(cloud,monkeypatch,method,path,body):
    r4_channel();seed(choice=0)
    assert rules.WRITE_WAIT==3.0
    monkeypatch.setattr(rules,'WRITE_WAIT',.03)
    entered,release=threading.Event(),threading.Event()
    def hold():
        with rules.mutation(writer=False):entered.set();release.wait(5)
    th=threading.Thread(target=hold);th.start();assert entered.wait(2)
    try:
        response=getattr(api(),method)(path,json=body)
        assert response.status_code==409,response.text
        assert response.json()=={'detail':'网盘规则正在使用，请稍后重试'}
    finally:release.set();th.join(5)


def r4_backfill(monkeypatch):
    from assetizer import ingest,delivery,tuning
    c=r4_channel('LOCKED')
    monkeypatch.setattr(delivery,'registered_in_target',lambda _:None)
    monkeypatch.setattr(ingest,'backfill_pace',lambda:{'slots':1})
    monkeypatch.setattr(ingest,'backfill_stop',lambda:None)
    monkeypatch.setattr(runner,'apply_nudges',lambda:False)
    monkeypatch.setattr(runner,'worker_slots',lambda:4)
    monkeypatch.setattr(tuning,'dispatch_next',lambda:None)
    monkeypatch.setattr(run,'dispatch',lambda _:None)
    db.exec("INSERT INTO backfill_queue(channel_id,source_id,title,created_at) VALUES ('test',?,'补录',?)",OTHER,now())
    worker=runner.Worker();monkeypatch.setattr(worker,'housekeeping',lambda:None)
    return c,worker


def test_r4_a18_tick_and_poll_during_delete(cloud,monkeypatch):
    from assetizer import ingest
    c,worker=r4_backfill(monkeypatch);gate=r4_deleting(cloud,monkeypatch)
    try:
        start=time.monotonic();worker.tick();elapsed=time.monotonic()-start
        assert elapsed<.5
        assert db.one('SELECT channel_id FROM sources WHERE id=?',OTHER)['channel_id']=='test'
        r=db.one('SELECT id,production,wait_reason FROM runs WHERE source_id=?',OTHER)
        assert r['production']=='QUEUED' and r['wait_reason'] is None
        assert db.one('SELECT status,run_id FROM backfill_queue')=={'status':'DISPATCHED','run_id':r['id']}
        intake='intake00001'
        db.set_setting('ingest:test',{'enabled':True,'since':'2020-01-01T00:00:00+00:00','min_minutes':10})
        monkeypatch.setattr(ingest,'look',lambda *a,**k:([{'id':intake,'title':'新片','duration':600,'published':now(),'tab':'videos'}],None,{'videos'}))
        start=time.monotonic();value=ingest.poll();elapsed=time.monotonic()-start
        assert elapsed<.5 and value['queued']==[intake]
        assert db.one('SELECT channel_id FROM sources WHERE id=?',intake)['channel_id']=='test'
        assert db.one('SELECT production FROM runs WHERE source_id=?',intake)['production']=='QUEUED'
        assert db.one('SELECT youtube_channel_id FROM video_backups WHERE id=?',intake)['youtube_channel_id']==AUTHOR
    finally:r4_finish(gate)


def test_r4_a18_failed_binding_rolls_back_and_retries(cloud,monkeypatch):
    from assetizer import ingest
    _,worker=r4_backfill(monkeypatch);original=rules.track
    def fail(*a,**kw):raise RuntimeError('injected binding transaction failure')
    monkeypatch.setattr(rules,'track',fail)
    worker.tick()
    assert db.one('SELECT status,run_id,dispatched_at FROM backfill_queue')=={'status':'PENDING','run_id':None,'dispatched_at':None}
    assert db.one('SELECT * FROM sources WHERE id=?',OTHER) is None
    assert db.one('SELECT * FROM runs WHERE source_id=?',OTHER) is None
    assert not db.q("SELECT * FROM stages")
    monkeypatch.setattr(rules,'track',original);worker.tick()
    assert db.one('SELECT status FROM backfill_queue')['status']=='DISPATCHED'
    assert db.one('SELECT channel_id FROM sources WHERE id=?',OTHER)['channel_id']=='test'


@pytest.mark.parametrize('line',['topic','source','orphan'])
@pytest.mark.parametrize('age',[0,400])
def test_r4_a19_cleanup_never_waits_for_rule_lock(cloud,monkeypatch,line,age):
    locals=[]
    for i in range(3):
        _,local=seed(f'clean{i:06d}',line='source' if line=='source' else 'topic',choice=0,age=age);locals.append(local)
    if line=='orphan':db.exec('DELETE FROM topic_videos')
    author_rule(0,0)
    held,release=threading.Event(),threading.Event();outcomes=[];claims=[];errors=[]
    def hold():
        with rules.mutation(writer=False):held.set();release.wait(5)
    owner=threading.Thread(target=hold);owner.start();assert held.wait(2)
    def clean():
        start=time.monotonic()
        try:outcomes.append(((runner.retention if line=='source' else run.retention)(),time.monotonic()-start))
        except Exception as e:errors.append(e)
    def claim():
        start=time.monotonic()
        with backup.claim(OTHER) as free:claims.append((free,time.monotonic()-start))
    th=threading.Thread(target=clean);reader=threading.Thread(target=claim)
    try:
        th.start();reader.start();th.join(.5);reader.join(.2)
        assert not th.is_alive() and not reader.is_alive(), '清理与无关 claim 不得等待规则锁'
        assert not errors and outcomes[0][1]<.5 and claims[0][0] and claims[0][1]<.2
        assert all(p.exists() for p in locals)
        (runner.retention if line=='source' else run.retention)()
        warnings=db.q("SELECT 1 FROM events WHERE message LIKE '%跳过本轮本地清理%'")
        assert len(warnings)==(0 if age==0 else 3)
    finally:release.set();owner.join(5);th.join(5);reader.join(5)


@pytest.mark.parametrize('aux_fail',[False,True])
def test_r4_a20_acquire_restore_during_other_delete(cloud,monkeypatch,aux_fail):
    seed(OTHER,line='source',cached=False)
    p=cloud[0]/OTHER/'source.mkv';p.parent.mkdir();p.write_bytes(b'remote')
    def denied(*args):raise youtube.AcquireError('AUTH_REQUIRED','synthetic login required')
    monkeypatch.setattr(youtube,'download',denied)
    monkeypatch.setattr(A,'_free_bytes',lambda _:10**12)
    monkeypatch.setattr(backup.ffmpeg,'probe',lambda _:{'streams':[{'codec_type':'video','height':1080}],'format':{'duration':'60'}})
    gate=r4_deleting(cloud,monkeypatch)
    if aux_fail:
        db.exec('UPDATE backup_authors SET public=1')
        def fail(*a,**kw):raise RuntimeError('injected auxiliary metadata failure')
        monkeypatch.setattr(rules,'track',fail)
    try:
        start=time.monotonic();result=A.acquire(RunContext({'id':'r4'},db.one('SELECT * FROM sources WHERE id=?',OTHER)))
        assert time.monotonic()-start<1
        s=db.one('SELECT * FROM sources WHERE id=?',OTHER)
        assert s['media_state']=='CACHED' and Path(s['media_path']).exists()
        assert s['media_sha256']==result['sha256']==sha256_file(Path(s['media_path']))
        assert s['media_bytes']==result['bytes']==len(b'remote')
        assert db.one('SELECT restored_at FROM video_backups WHERE id=?',OTHER)['restored_at']
        warnings=db.q("SELECT message FROM events WHERE level='warn' AND message LIKE '%取回后归集%'")
        assert bool(warnings)==aux_fail
    finally:r4_finish(gate)


def test_r4_a20_draft_aux_failure_is_success(cloud,monkeypatch):
    seed(cached=False);remote=cloud[0]/VID/'draft';remote.mkdir(parents=True)
    for name in ['full.md','index.md','draft.json']:(remote/name).write_text('{}')
    def fail(*a,**kw):raise RuntimeError('injected auxiliary metadata failure')
    monkeypatch.setattr(rules,'track',fail)
    assert backup.restore_draft(VID) is True
    assert (draft_dir(VID)/'full.md').exists()
    assert db.one('SELECT draft_restored_at FROM video_backups')['draft_restored_at']
    assert db.one("SELECT 1 FROM events WHERE message LIKE '%取回后归集元数据失败%'")


def test_r4_a21_killed_writer_lease_reclaimed(cloud):
    code='''import dataclasses,sys
from pathlib import Path
from assetizer import config
from assetizer.topics import backup_rules as rules
config.override(dataclasses.replace(config.load(),home=Path(sys.argv[1])))
with rules.mutation(wait=10):pass
'''
    child=None
    try:
        with rules.mutation(writer=False):
            child=subprocess.Popen([sys.executable,'-B','-c',code,str(config.get().home)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            d=config.get().home/'cache/gdrive-locks';until=time.monotonic()+3
            while not list(d.glob('writer-*')):
                assert child.poll() is None and time.monotonic()<until;time.sleep(.01)
            child.kill();child.communicate(timeout=3)
        start=time.monotonic();rules.yield_writers();elapsed=time.monotonic()-start
        assert elapsed<.1 and not list(d.glob('writer-*'))
        assert child.returncode==-9
    finally:
        if child and child.poll() is None:child.kill();child.communicate(timeout=3)


def test_r4_a11_metadata_before_intent_skips_delete(cloud,monkeypatch):
    seed(line='source',choice=0,availability=None);author_rule(1,0);confirmed()
    remote=cloud[0]/VID;remote.mkdir();(remote/'source.mkv').write_bytes(b'remote')
    monkeypatch.setattr(youtube,'metadata',lambda _:{'channel_id':AUTHOR,'availability':'subscriber_only','duration':60})
    A.resolve(RunContext({'id':'r4'},db.one('SELECT * FROM sources')))
    with pytest.raises(backup.BackupError,match='规则或副本状态已变化'):
        backup._run('purge','gdrive:'+VID,'--drive-use-trash=true',expected=(backup._folder()['folder_id'],backup._identity_key(backup._read_conf())))
    assert remote.exists() and backup.verified(VID)
    assert not db.one('SELECT delete_intent FROM video_backups')['delete_intent']
    assert not any(x[2]=='purge' for x in logs(cloud))


def test_r4_a11_recheck_and_intent_are_atomic(cloud,monkeypatch):
    import contextlib
    seed(line='source',availability=None);author_rule(1,0);confirmed()
    remote=cloud[0]/VID;remote.mkdir();(remote/'source.mkv').write_bytes(b'remote')
    monkeypatch.setattr(youtube,'metadata',lambda _:{'channel_id':AUTHOR,'availability':'subscriber_only','duration':60})
    attempted,finished=threading.Event(),threading.Event();ready,go=threading.Event(),threading.Event();errors=[]
    tx=db.tx;verify=backup.verified;invoke=backup._invoke;hooked=[]
    @contextlib.contextmanager
    def observed_tx():
        if threading.current_thread().name=='r4-metadata-writer':attempted.set()
        with tx() as conn:yield conn
    monkeypatch.setattr(db,'tx',observed_tx)
    def write():
        try:
            db.connect();ready.set();assert go.wait(4)
            A.resolve(RunContext({'id':'r4'},db.one('SELECT * FROM sources')))
        except Exception as e:errors.append(e)
        finally:finished.set()
    th=threading.Thread(target=write,name='r4-metadata-writer');th.start();assert ready.wait(2)
    def checked(vid,**kw):
        result=verify(vid,**kw)
        if not hooked:
            hooked.append(True);go.set();assert attempted.wait(2)
            assert not finished.wait(.1), '复核和记录意图之间元数据写不得提交'
        return result
    monkeypatch.setattr(backup,'verified',checked)
    def outside_tx(*args,**kw):
        assert not db.connect().in_transaction, '不得让写事务跨 rclone'
        assert finished.wait(2) and not errors
        assert db.one('SELECT delete_intent FROM video_backups')['delete_intent']
        return invoke(*args,**kw)
    monkeypatch.setattr(backup,'_invoke',outside_tx)
    try:backup._run('purge','gdrive:'+VID,'--drive-use-trash=true',expected=(backup._folder()['folder_id'],backup._identity_key(backup._read_conf())))
    finally:
        if th.ident:th.join(3)
    assert not errors and not remote.exists() and rules.decision(VID) is True
    assert db.one('SELECT backup_due FROM video_backups')['backup_due']==1
    assert len(db.q("SELECT 1 FROM events WHERE message LIKE '%刚移到网盘回收站%'") )==1


@pytest.mark.parametrize('line',['source','topic'])
def test_r4_a11_metadata_original_and_track_rollback_together(cloud,monkeypatch,line):
    seed(line=line,availability=None);author_rule(1,0)
    if line=='topic':db.exec('UPDATE videos SET info=NULL')
    table='sources' if line=='source' else 'videos';before=db.one(f'SELECT * FROM {table}');before_choice=rules.decision(VID)
    monkeypatch.setattr(youtube,'metadata',lambda _:{'channel_id':AUTHOR,'availability':'subscriber_only','duration':60})
    def fail(*a,**kw):raise RuntimeError('injected track failure')
    monkeypatch.setattr(rules,'track',fail)
    with pytest.raises(RuntimeError,match='injected track failure'):
        if line=='source':A.resolve(RunContext({'id':'r4'},before))
        else:run.Job(1,VID).describe()
    assert db.one(f'SELECT * FROM {table}')==before and rules.decision(VID) is before_choice


def _r5_cleanup_metadata(monkeypatch, line):
    db.set_setting('retention_days',30);db.set_setting('delivered_retention_days',30)
    _,local=seed(line='source' if line=='source' else 'topic',choice=0,age=31,availability=None)
    author_rule(1,0)
    if line=='source':
        seed(line='topic',choice=0,cached=False,availability=None)
        db.exec('UPDATE videos SET info=NULL WHERE id=?',VID)
        db.exec("UPDATE topic_videos SET state='RUNNING' WHERE video_id=?",VID)
        change=lambda:run.Job(1,VID).describe()
    else:
        db.exec('UPDATE videos SET info=NULL WHERE id=?',VID)
        submitted=runner.submit('https://www.youtube.com/watch?v='+VID)
        ctx=RunContext(db.one('SELECT * FROM runs WHERE id=?',submitted['run_id']),
                       db.one('SELECT * FROM sources WHERE id=?',VID))
        change=lambda:A.resolve(ctx)
        if line=='orphan':
            db.exec('DELETE FROM topic_videos WHERE video_id=?',VID)
            db.set_setting('retention_days',60)  # 只让 30 天的孤立行清理到期
            d=draft_dir(VID);d.mkdir(parents=True);(d/'full.md').write_text('draft')
    monkeypatch.setattr(youtube,'metadata',lambda _:{'channel_id':AUTHOR,'availability':'subscriber_only','duration':60})
    assert rules.decision(VID) is False
    return local,change,('topic_drop' if line=='orphan' else line+'_media')


@pytest.mark.parametrize('line',['topic','source','orphan'])
def test_r5_a22_metadata_before_cleanup_transaction_keeps_grace(cloud,monkeypatch,line):
    import contextlib
    from assetizer.topics import backup_retention as R
    local,change,path=_r5_cleanup_metadata(monkeypatch,line)
    entered,release=threading.Event(),threading.Event();errors=[];result=[];cleanup=rules.cleanup
    @contextlib.contextmanager
    def paused(vid):
        with cleanup(vid) as free:
            if vid==VID and not entered.is_set():
                entered.set();assert release.wait(4)
            yield free
    monkeypatch.setattr(rules,'cleanup',paused)
    def clean():
        try:result.append((runner.retention if line=='source' else run.retention)())
        except Exception as e:errors.append(e)
    th=threading.Thread(target=clean);th.start();assert entered.wait(2)
    try:
        change()
        fresh=R.plan(VID)
        assert fresh['choice'] is True and fresh['in_grace'] and not R.due(fresh,path)
        assert local.exists() and not backup.verified(VID)
    finally:release.set();th.join(5)
    assert not errors and not th.is_alive()
    assert local.exists() and result==[{'purged':0} if line=='source' else {'purged':0,'dropped':0}]
    assert db.one('SELECT backup_missed_at FROM video_backups')['backup_missed_at'] is None
    if line=='orphan':assert (draft_dir(VID)/'full.md').exists() and db.one('SELECT id FROM videos')


@pytest.mark.parametrize('line',['topic','source','orphan'])
def test_r5_a22_cleanup_transaction_blocks_metadata_through_delete(cloud,monkeypatch,line):
    import contextlib
    from assetizer.topics import backup_retention as R
    local,change,path=_r5_cleanup_metadata(monkeypatch,line)
    ready,go,attempted,finished=(threading.Event() for _ in range(4))
    errors=[];observed={};tx=db.tx;plan=R.plan;rmtree=run.shutil.rmtree
    @contextlib.contextmanager
    def observed_tx():
        if threading.current_thread().name=='r5-metadata':attempted.set()
        with tx() as c:yield c
    monkeypatch.setattr(db,'tx',observed_tx)
    def write():
        try:
            db.connect();ready.set();assert go.wait(4)
            change()
            observed['file_after_metadata']=local.exists()
            observed['row_after_metadata']=db.one('SELECT media_state FROM '+('sources' if line=='source' else 'videos')+' WHERE id=?',VID)
        except Exception as e:errors.append(e)
        finally:
            finished.set()
            if getattr(db._local,'conn',None) is not None:db._local.conn.close();db._local.conn=None
    th=threading.Thread(target=write,name='r5-metadata');th.start();assert ready.wait(2)
    def checked_plan(vid,**kw):
        p=plan(vid,**kw)
        if vid==VID and getattr(rules._held,'depth',0) and 'final' not in observed:
            observed['final']=True
            assert db.connect().in_transaction, '最终 plan 必须在清理写事务内'
            assert R.due(p,path)
            go.set();assert attempted.wait(2)
            assert not finished.wait(.1), '元数据不得在最终判定与删除之间提交'
        return p
    def checked_remove(p,*args,**kw):
        if Path(p) in {local.parent,draft_dir(VID)}:
            observed['deleted']=True
            assert db.connect().in_transaction, '本地删除必须仍在最终判定的写事务内'
            assert not finished.is_set(), '元数据不得先于本地删除提交'
        return rmtree(p,*args,**kw)
    monkeypatch.setattr(R,'plan',checked_plan);monkeypatch.setattr(run.shutil,'rmtree',checked_remove)
    try:
        result=(runner.retention if line=='source' else run.retention)()
    finally:go.set();th.join(5)
    assert not th.is_alive() and not errors and observed.get('deleted')
    assert not local.exists() and observed['file_after_metadata'] is False
    assert result[('dropped' if line=='orphan' else 'purged')]==1
    assert observed['row_after_metadata']==(None if line=='orphan' else {'media_state':'PURGED'})
    assert rules.decision(VID) is True and not backup.verified(VID)


@pytest.mark.parametrize('cached',[False,True])
def test_r5_a23_killed_after_purge_recovery_warns_once_and_requeues(cloud,monkeypatch,cached):
    _,local=seed(line='source',cached=cached,availability=None);author_rule(1,0);confirmed()
    remote=cloud[0]/VID;remote.mkdir();(remote/'source.mkv').write_bytes(b'remote')
    root=config.get().home;finished=root/'purge.completed'
    code='''import dataclasses,sys,time
from pathlib import Path
from assetizer import config
from assetizer.topics import backup
root=Path(sys.argv[1]);config.override(dataclasses.replace(config.load(),home=root))
invoke=backup._invoke
def stop_after_remote(*args,**kw):
    result=invoke(*args,**kw)
    if args[0]=='purge':
        (root/'purge.completed').touch();time.sleep(30)
    return result
backup._invoke=stop_after_remote
backup.start_delete([sys.argv[2]])
while backup._busy('batch'):time.sleep(.01)
'''
    child=subprocess.Popen([sys.executable,'-B','-c',code,str(root),VID],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        until=time.monotonic()+5
        while not finished.exists():
            assert child.poll() is None and time.monotonic()<until
            time.sleep(.005)
        assert db.one('SELECT delete_intent FROM video_backups')['delete_intent'] and not remote.exists()
        monkeypatch.setattr(youtube,'metadata',lambda _:{'channel_id':AUTHOR,'availability':'subscriber_only','duration':60})
        A.resolve(RunContext({'id':'recovery'},db.one('SELECT * FROM sources WHERE id=?',VID)))
        child.kill();stdout,stderr=child.communicate(timeout=3)
        assert child.returncode==-9, (stdout,stderr)
        backup.recover_deletions();backup.recover_deletions()
        assert rules.decision(VID) is True and local.exists() is cached
        assert db.one('SELECT delete_intent,backup_due,backup_at FROM video_backups')=={'delete_intent':None,'backup_due':int(cached),'backup_at':None}
        assert db.q("SELECT message FROM events WHERE level='warn'")==[{'message':f'{VID} 刚移到网盘回收站，但现在规则是传，30 天内可在 Google 网盘回收站恢复'}]
        assert len(db.q("SELECT 1 FROM events WHERE message LIKE '%已核对中断的删除：目录已不在%'"))==1
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=3)


_STOP_AFTER_CLEAR = '''
clear=backup._clear_copy
def stop_clear(vid):
    clear(vid);(root/'state.cleared').touch();time.sleep(30)
backup._clear_copy=stop_clear
'''


@pytest.mark.parametrize('cached',[False,True])
@pytest.mark.parametrize('mode',['normal','recovery'])
def test_r5_a24_killed_inside_finish_keeps_intent_and_redoes_postprocess(cloud,monkeypatch,cached,mode):
    # 删除收尾（清意图、提醒、重新排队）同一事务：清掉状态后、提醒和排队前被杀，意图仍在，后台核对重做整段。
    _,local=seed(line='source',cached=cached,availability=None);author_rule(1,0);confirmed()
    remote=cloud[0]/VID;remote.mkdir();(remote/'source.mkv').write_bytes(b'remote')
    root=config.get().home
    head='''import dataclasses,sys,time
from pathlib import Path
from assetizer import config
from assetizer.topics import backup
root=Path(sys.argv[1]);config.override(dataclasses.replace(config.load(),home=root))
'''
    delete=head+'''invoke=backup._invoke
def stop_remote(*args,**kw):
    result=invoke(*args,**kw)
    if args[0]=='purge':
        (root/'purge.completed').touch()
        while not (root/'release').exists():time.sleep(.003)
    return result
backup._invoke=stop_remote
'''+_STOP_AFTER_CLEAR+'''backup.start_delete([sys.argv[2]])
while backup._busy('batch'):time.sleep(.01)
'''
    def wait(path,child):
        until=time.monotonic()+6
        while not path.exists():
            assert child.poll() is None and time.monotonic()<until,(str(path),child.poll())
            time.sleep(.003)
    child=subprocess.Popen([sys.executable,'-B','-c',delete,str(root),VID],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        wait(root/'purge.completed',child)
        assert not remote.exists()
        monkeypatch.setattr(youtube,'metadata',lambda _:{'channel_id':AUTHOR,'availability':'subscriber_only','duration':60})
        A.resolve(RunContext({'id':'after-intent'},db.one('SELECT * FROM sources WHERE id=?',VID)))
        assert rules.decision(VID) is True
        if mode=='recovery':
            child.kill();child.communicate(timeout=3)
            child=subprocess.Popen([sys.executable,'-B','-c',head+_STOP_AFTER_CLEAR+'backup.recover_deletions()\n',str(root)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        else:
            (root/'release').touch()
        wait(root/'state.cleared',child)
        child.kill();child.communicate(timeout=3)
        assert child.returncode==-9
        row=db.one('SELECT delete_intent FROM video_backups WHERE id=?',VID)
        assert row['delete_intent'], '清掉状态后被杀，收尾整段回滚，意图仍在'
        assert not db.q("SELECT 1 FROM events WHERE level='warn'")
        intent=json.loads(row['delete_intent']);intent.pop('retry_at',None)  # 退避到期
        db.exec('UPDATE video_backups SET delete_intent=? WHERE id=?',json.dumps(intent),VID)
        backup.recover_deletions();backup.recover_deletions()
        assert db.one('SELECT delete_intent,backup_due,backup_at FROM video_backups WHERE id=?',VID)=={'delete_intent':None,'backup_due':int(cached),'backup_at':None}
        assert db.q("SELECT message FROM events WHERE level='warn'")==[{'message':f'{VID} 刚移到网盘回收站，但现在规则是传，30 天内可在 Google 网盘回收站恢复'}]
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=3)
