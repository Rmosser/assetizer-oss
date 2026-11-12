"""Webhook contract and worker reads, with isolated secrets and a memory-only Notion."""
import datetime as dt
import dataclasses
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import time
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from test_topic_notion import BOT, RESEARCH, FakeNotion, fake, home  # noqa: F401
from assetizer import config, db
from assetizer.notion import hook
from assetizer.topics import comments as TC, notion as TN, run
from assetizer.util import now
from assetizer.web import app as web

SECRET = "secret_synthetic_notion_hook_never_in_diagnostics"
URL = "/api/notion/webhook"


def ago(seconds):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def event(eid="evt-1", page=RESEARCH, parent=None, kind="comment.created", authors=None):
    return {"id": eid, "timestamp": now(), "type": kind, "workspace_id": "w", "integration_id": "i",
            "subscription_id": "s", "attempt_number": 1, "entity": {"type": "comment", "id": "comment-1"},
            "authors": authors if authors is not None else [{"id": "you-user", "type": "person"}],
            "data": {"page_id": page, "parent": {"type": "block" if parent else "page", "id": parent or page}}}


def signed(body, token=SECRET):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    return raw, {"Content-Type": "application/json", "X-Notion-Signature": "sha256=" + hmac.new(token.encode(), raw, hashlib.sha256).hexdigest()}


@pytest.fixture
def c(home, monkeypatch):
    hook._requests.clear()
    monkeypatch.setattr(web, "TRUST_LAZYCAT", True)
    monkeypatch.setattr(hook, "TUNNEL_TOKEN_FILE", home / "hook-tunnel/tunnel-token")
    with TestClient(web.app, base_url="http://localhost") as client:
        yield client
    for key in ("h", "c"):
        thread = run._watching.get(key)
        if thread:
            thread.join(5)
    hook._requests.clear()


def verify(c, value=SECRET):
    return c.post(URL, json={"verification_token": value})


def send(c, body=None, **kw):
    raw, headers = signed(body or event())
    return c.post(URL, content=raw, headers={**headers, **kw})


@pytest.fixture
def page(fake):
    """A real FakeNotion tree, with a heading and a source row; no delivery is needed to set it up."""
    fake.kids[RESEARCH] = [fake._block({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "密度"}}]}}),
                           fake._block({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "外圈 80%"}}]}}),
                           fake._block({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "薄壁另加"}}]}})]
    row = fake.create_page(RESEARCH, "来源一行", children=[{"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "笔记"}}]}}])["id"]
    db.exec("UPDATE topics SET notion_page_id=?, notion_rows=?, comments_at=?, synth_state='DONE' WHERE id=1",
            RESEARCH, db.j({"aaaaaaaaaaa": {"page": row}}), now())
    fake.calls.clear()
    return SimpleNamespace(n=fake, pid=RESEARCH, block=fake.kids[RESEARCH][1]["id"],
                           other=fake.kids[RESEARCH][2]["id"], row=row, note=fake.kids[row][0]["id"])


def test_raw_signature_and_constant_time(c, monkeypatch):
    assert verify(c).status_code == 200
    raw, headers = signed(event())
    compare = hmac.compare_digest
    calls = []
    monkeypatch.setattr(hook.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or compare(a, b))
    assert c.post(URL, content=raw, headers=headers).status_code == 200
    assert calls
    changed = raw.replace(b'"w"', b'"x"')
    spaced = json.dumps(json.loads(raw), ensure_ascii=False, indent=2).encode()
    for body, hdr in [(changed, headers), (spaced, headers), (raw, {"Content-Type": "application/json"}),
                      (raw, {**headers, "X-Notion-Signature": headers["X-Notion-Signature"][7:]}),
                      (raw, {**headers, "X-Notion-Signature": "sha256=" + "0" * 64})]:
        assert c.post(URL, content=body, headers=hdr).status_code == 401
    assert len(db.q("SELECT * FROM notion_events")) == 1
    assert hook.events()["rejected"] == 5
    assert len(db.q("SELECT * FROM events WHERE level='warn'")) == 1


def test_first_token_rearm_expiry_and_no_secret_in_database_or_logs(c, home, caplog):
    assert c.get("/api/notion/hook/token").status_code == 404
    assert verify(c).status_code == 200
    p = home / "secrets/notion-hook-verification"
    assert p.read_text() == SECRET and p.stat().st_mode & 0o777 == 0o600
    assert c.get("/api/notion/hook/token").json() == {"token": SECRET}
    assert verify(c, "replacement-synthetic").status_code == 200
    assert p.read_text() == SECRET
    assert send(c).status_code == 200
    assert hook.events()["signed_at"]
    assert c.post("/api/notion/hook/rearm", json={}).json()["rearm_until"]
    assert verify(c, "replacement-synthetic").status_code == 200
    assert p.read_text() == "replacement-synthetic"
    assert hook.rearm_until() is None and hook.events()["signed_at"] is None
    assert verify(c).status_code == 200 and p.read_text() == "replacement-synthetic"
    db.set_setting("notion_hook_rearm_until", ago(1))
    assert verify(c).status_code == 200 and p.read_text() == "replacement-synthetic"
    assert c.get("/api/notion/hook").json()["rearm_until"] is None
    from assetizer import logs
    exported = json.dumps(logs.export(), ensure_ascii=False)
    assert SECRET not in exported and "replacement-synthetic" not in exported
    dump = "\n".join(db.connect().iterdump())
    captured = dump + caplog.text + "\n".join(p.read_bytes().decode(errors="ignore") for p in home.glob("assetizer.db*"))
    assert SECRET not in captured and "replacement-synthetic" not in captured
    assert SECRET not in json.dumps(c.get("/api/notion/hook").json())
    warning = db.one("SELECT message FROM events WHERE level='warn'")["message"]
    assert "设置 → Notion" in warning and "机房" not in warning
    assert warning.endswith("Notion 点「Resend token」")


@pytest.mark.parametrize("method,path", [("GET", URL), ("POST", "/api/knobs"), ("POST", URL + "/"),
    ("POST", "/api/notion//webhook"), ("POST", "/api/notion%2Fwebhook"), ("POST", "/api%2Fnotion/webhook"),
    ("POST", "/api/notion/%2e/webhook"), ("POST", "/api/notion/%2e%2e/notion/webhook"),
    ("GET", "/api/notion/hook"), ("GET", "/api/notion/hook/token"), ("POST", "/api/notion/hook/rearm")])
def test_hook_host_has_one_exact_route(c, method, path):
    assert c.request(method, path, headers={"Host": "notion-hook", "Content-Type": "application/json"}, content=b"{}").status_code == 404


def test_guard_gateway_public_and_local(c, monkeypatch):
    gateway = {"Host": "web:8770", "X-Forwarded-Host": "assetizer.devbox.heiyu.space", "X-HC-User-Id": "demo"}
    assert c.post(URL, json={}, headers=gateway).status_code == 404
    assert c.get("/api/notion/hook", headers=gateway).status_code == 200
    assert c.get("/api/notion/hook", headers={"Host": "web"}).status_code == 403
    assert c.post(URL, json={"verification_token": SECRET}, headers={"Host": "notion-hook"}).status_code == 200
    assert c.post(URL, content=b"{}", headers={"Host": "notion-hook"}).status_code == 415
    monkeypatch.setattr(web, "PUBLIC_HOST", "kan.example")
    monkeypatch.setattr(web, "_access_ok", lambda token: True)
    for path in [URL, "/api/notion/hook", "/api/notion/hook/token", "/api/notion/hook/rearm"]:
        assert c.post(path, json={}, headers={"Host": "kan.example"}).status_code == 404
    assert c.get("/healthz").status_code == 200


def test_endpoint_limits_errors_duplicate_and_other_type(c, monkeypatch):
    assert c.post(URL, content=b"x" * (hook.LIMIT + 1), headers={"Content-Type": "application/json"}).status_code == 413
    for raw in [b"[]", b"null", b"1", b"bad-json", b"\xff"]:
        assert c.post(URL, content=raw, headers={"Content-Type": "application/json"}).status_code == 400
    assert send(c).status_code == 503
    assert verify(c).status_code == 200
    assert send(c).status_code == send(c).status_code == 200
    assert len(db.q("SELECT * FROM notion_events")) == 1
    first = db.one("SELECT * FROM notion_events")
    body = event()
    body["attempt_number"] = 7
    assert send(c, body).status_code == 200
    assert db.one("SELECT * FROM notion_events") == first
    assert send(c, event("other", kind="page.updated")).status_code == 200
    assert len(db.q("SELECT * FROM notion_events")) == 1 and hook.events()["total"] == 4
    hook._requests.clear()
    monkeypatch.setattr(hook.time, "monotonic", lambda: 1000)
    for _ in range(120):
        assert c.post(URL, json=[]).status_code == 400
    resp = c.post(URL, json=[])
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "60"
    monkeypatch.setattr(hook.time, "monotonic", lambda: 1060)
    assert c.post(URL, json=[]).status_code == 400


def test_status_contract_tunnel_probe_knob_and_doctor(c, home, monkeypatch, capsys):
    from assetizer import cli, knobs
    initial = c.get("/api/notion/hook").json()
    assert initial == {"url": None, "token": "none", "token_at": None, "rearm_until": None, "tunnel": "off",
                       "events": {"total": 0, "rejected": 0, "rejected_at": None, "last_at": None, "last_type": None, "signed_at": None}}
    url = "https://notion-hook-dev.example.com/api/notion/webhook"
    assert c.post("/api/knobs", json={"notion_hook_url": url}).status_code == 200
    assert knobs.get("notion_hook_url") == url
    cli.cmd_set(SimpleNamespace(key="notion_hook_url", value=url))
    assert c.get("/api/notion/hook").json()["url"] == url
    tunnel = hook.TUNNEL_TOKEN_FILE
    tunnel.parent.mkdir()
    tunnel.write_text("synthetic-tunnel-value")
    def get(url, **kw):
        assert url == "http://hook-tunnel:20241/ready" and kw == {"timeout": 1, "trust_env": False}
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(hook.httpx, "get", get)
    assert c.get("/api/notion/hook").json()["tunnel"] == "up"
    monkeypatch.setattr(hook.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=503))
    assert c.get("/api/notion/hook").json()["tunnel"] == "down"
    def timeout(*a, **k):
        raise httpx.ReadTimeout("synthetic")
    monkeypatch.setattr(hook.httpx, "get", timeout)
    assert c.get("/api/notion/hook").json()["tunnel"] == "down"
    tunnel.write_text("")
    assert c.get("/api/notion/hook").json()["tunnel"] == "off"
    verify(c)
    cli.cmd_doctor(SimpleNamespace(agy=False))
    output = capsys.readouterr().out
    assert '"notion_hook"' in output and SECRET not in output and "synthetic-tunnel-value" not in output


def queue(c, page, parent=None, eid="evt-1", **kw):
    assert send(c, event(eid, page, parent, **kw)).status_code == 200


def test_worker_next_tick_target_then_quiet_queues_answer(c, page, monkeypatch):
    verify(c)
    word = page.n.say(page.block, "课代表：这里漏了薄壁")
    word["created_time"] = now()
    before = db.one("SELECT comments_at FROM topics")["comments_at"]
    queue(c, page.pid, page.block)
    t = 10000.0
    monkeypatch.setattr(run, "_last", dict.fromkeys(run._last, t))
    monkeypatch.setattr(run, "_watching", {})
    run.housekeeping(t)  # no 10-minute timer has elapsed, but the event starts the comment line
    run._watching["h"].join(5)
    assert not run._watching["h"].is_alive()
    assert page.n.calls == [("GET", "/users/me"), ("GET", f"/comments?block_id={page.block}&page_size=100"), ("GET", f"/blocks/{page.block}")]
    stored = db.one("SELECT * FROM topic_comments")
    assert stored["asked"] == 1 and stored["quote"] == "外圈 80%" and stored["kind"] == "paragraph" and stored["under"] is None
    assert db.one("SELECT text FROM asks")["text"] == "这里漏了薄壁"
    assert db.one("SELECT outcome,topic_id,done FROM notion_events")["outcome"] == "topic"
    assert db.one("SELECT comments_at,synth_state FROM topics") == {"comments_at": before, "synth_state": "DONE"}
    db.exec("UPDATE topic_comments SET created=?", ago(TC.QUIET + 1))
    TC.due()
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "QUEUED"
    assert len(page.n.calls) == 3  # ripe() did not need another page read or a model
    hook.process()
    assert len(page.n.calls) == 3


@pytest.mark.parametrize("target", ["row", "page", "row_page"])
def test_source_row_and_page_discussions_read_only_the_target(c, page, target):
    verify(c)
    pid, bid = (page.row, page.note) if target == "row" else (page.row if target == "row_page" else page.pid, None)
    page.n.say(bid or pid, "@课代表 请解释一下")
    queue(c, pid, bid)
    hook.process()
    expected = [("GET", "/users/me"), ("GET", f"/comments?block_id={bid or pid}&page_size=100")]
    if bid:
        expected += [("GET", f"/blocks/{bid}")]
    assert page.n.calls == expected
    assert db.one("SELECT outcome FROM notion_events")["outcome"] == ("row" if pid == page.row else "topic")
    assert db.one("SELECT video_id FROM topic_comments")["video_id"] == ("aaaaaaaaaaa" if pid == page.row else None)


def test_target_reconciliation_edit_delete_preserves_other_blocks_and_under(c, page):
    verify(c)
    a = page.n.say(page.block, "课代表：原文")
    other = page.n.say(page.other, "课代表：别的块")
    page.n.say(page.block, "〔课代表〕已改", by=BOT, thread=a["discussion_id"])
    assert TC.scan(1) == 2
    db.exec("UPDATE topic_comments SET seen=4")
    old_other = db.one("SELECT * FROM topic_comments WHERE id=?", other["id"])
    a["rich_text"] = [{"plain_text": "课代表：改过", "text": {"content": "课代表：改过"}}]
    before = db.one("SELECT comments_at FROM topics")["comments_at"]
    queue(c, page.pid, page.block, kind="comment.updated")
    hook.process()
    new = db.one("SELECT * FROM topic_comments WHERE id=?", a["id"])
    assert new["seen"] is None and new["under"] == "密度" and new["asked"] == 1
    assert db.one("SELECT text FROM asks WHERE id=?", a["id"])["text"] == "改过"
    assert db.one("SELECT * FROM topic_comments WHERE id=?", other["id"]) == old_other
    page.n.comments[page.block] = []
    queue(c, page.pid, page.block, eid="delete", kind="comment.deleted")
    hook.process()
    assert all(not r["open"] for r in db.q("SELECT * FROM topic_comments WHERE block=?", page.block))
    assert db.one("SELECT * FROM topic_comments WHERE id=?", other["id"]) == old_other
    assert db.one("SELECT comments_at FROM topics")["comments_at"] == before


@pytest.mark.parametrize("which,outcome", [("ours", "ours"), ("channel", "channel"), ("unknown", "unknown"), ("archived", "unknown")])
def test_ignored_events_do_not_read_comments_blocks_or_pages(c, page, which, outcome):
    verify(c)
    pid, authors = page.pid, None
    if which == "ours":
        authors = [{"id": BOT, "type": "bot"}]
    elif which == "channel":
        pid = "channel-page"
        db.exec("INSERT INTO sources(id, notion_page_id, created_at, updated_at) VALUES ('vid',?,?,?)", pid, now(), now())
    elif which == "unknown":
        pid = "unknown-page"
    else:
        db.exec("UPDATE topics SET archived_at=?", now())
    queue(c, pid, page.block, authors=authors)
    hook.process()
    assert page.n.calls == ([("GET", "/users/me")] if which == "ours" else [])
    assert db.one("SELECT outcome FROM notion_events")["outcome"] == outcome


def test_three_events_and_reordered_delivery_read_current_block_once(c, page):
    verify(c)
    current = page.n.say(page.block, "课代表：最新的文字")
    for i, kind in enumerate(["comment.deleted", "comment.updated", "comment.created"]):
        body = event(f"e{i}", page.pid, page.block, kind)
        body["timestamp"] = ago((i + 1) * 20)
        assert send(c, body).status_code == 200
    hook.process()
    assert len(page.n.calls) == 3
    assert db.one("SELECT text FROM topic_comments WHERE id=?", current["id"])["text"] == "课代表：最新的文字"
    assert [e["outcome"] for e in db.q("SELECT * FROM notion_events")] == ["topic"] * 3
    queue(c, page.pid, page.block, eid="e0")  # done event cannot be resurrected by a retry
    hook.process()
    assert len(page.n.calls) == 3


def test_mixed_batch_identifies_once_and_refreshes_identity_next_batch(c, page):
    verify(c)
    page.n.say(page.block, "课代表：继续读用户的意见")
    queue(c, page.pid, page.other, eid="ours", authors=[{"id": BOT, "type": "bot"}])
    mixed = [{"id": BOT, "type": "bot"}, {"id": "you-user", "type": "person"}]
    queue(c, page.pid, page.block, eid="topic", authors=mixed)
    queue(c, page.row, eid="row")
    hook.process()
    assert page.n.calls == [("GET", "/users/me"),
                            ("GET", f"/comments?block_id={page.block}&page_size=100"), ("GET", f"/blocks/{page.block}"),
                            ("GET", f"/comments?block_id={page.row}&page_size=100")]
    assert {e["id"]: e["outcome"] for e in db.q("SELECT * FROM notion_events")} == {"ours": "ours", "topic": "topic", "row": "row"}
    assert db.unj(db.one("SELECT bots FROM notion_events WHERE id='topic'")["bots"]) == mixed
    # Changing the integration between batches must not reuse the previous bot's identity.
    page.n.bot = "replacement-bot"
    page.n.calls.clear()
    queue(c, page.pid, page.block, eid="new-ours", authors=[{"id": page.n.bot, "type": "bot"}])
    queue(c, page.pid, page.other, eid="old-bot", authors=[{"id": BOT, "type": "bot"}])
    hook.process()
    assert page.n.calls == [("GET", "/users/me"),
                            ("GET", f"/comments?block_id={page.other}&page_size=100"), ("GET", f"/blocks/{page.other}")]
    assert db.one("SELECT outcome FROM notion_events WHERE id='new-ours'")["outcome"] == "ours"
    assert db.one("SELECT outcome FROM notion_events WHERE id='old-bot'")["outcome"] == "topic"


def test_channel_only_batch_never_identifies_and_preserves_authors(c, page, monkeypatch):
    verify(c)
    db.exec("INSERT INTO sources(id, notion_page_id, created_at, updated_at) VALUES ('vid',?,?,?)", "channel-page", now(), now())
    authors = [{"id": BOT, "type": "bot"}, {"id": "agent-one", "type": "agent"}, {"id": "you-user", "type": "person"}]
    queue(c, "channel-page", eid="channel-one", authors=authors)
    queue(c, "channel-page", eid="channel-two", authors=[{"id": BOT, "type": "bot"}])
    def forbidden():
        pytest.fail("channel-only batches must not create a Notion client")
    monkeypatch.setattr(TN, "client", forbidden)
    hook.process()
    assert page.n.calls == []
    assert [e["outcome"] for e in db.q("SELECT * FROM notion_events")] == ["channel", "channel"]
    assert db.unj(db.one("SELECT bots FROM notion_events WHERE id='channel-one'")["bots"]) == authors


def test_failed_identity_is_not_retried_and_targets_are_still_read(c, page, monkeypatch):
    verify(c)
    page.n.say(page.block, "课代表：身份暂时不明也要读")
    page.n.say(page.row, "课代表：行页也读")
    def failed_me(**kw):
        page.n.calls.append(("GET", "/users/me"))
        page.n._err(503)
    monkeypatch.setattr(page.n, "me", failed_me)
    queue(c, page.pid, page.block, eid="topic", authors=[{"id": BOT, "type": "bot"}])
    queue(c, page.row, eid="row", authors=[{"id": BOT, "type": "bot"}])
    hook.process()
    assert page.n.calls == [("GET", "/users/me"),
                            ("GET", f"/comments?block_id={page.block}&page_size=100"), ("GET", f"/blocks/{page.block}"),
                            ("GET", f"/comments?block_id={page.row}&page_size=100")]
    assert [e["outcome"] for e in db.q("SELECT * FROM notion_events")] == ["topic", "row"]
    assert len(db.q("SELECT * FROM topic_comments WHERE asked=1")) == 2


def test_no_read_missing_block_and_error(c, page, monkeypatch):
    verify(c)
    page.n.say(page.block, "课代表：改这里")
    TC.scan_one(1, page.pid, page.block)
    page.n.can.remove("read")
    before = db.one("SELECT comments_at FROM topics")["comments_at"]
    queue(c, page.pid, page.block)
    hook.process()
    assert db.one("SELECT comments_err,comments_at FROM topics") == {"comments_err": "NO_READ", "comments_at": before}
    assert db.one("SELECT open FROM topic_comments")["open"] == 1
    page.n.can.add("read")
    page.n.kids[page.pid] = [b for b in page.n.kids[page.pid] if b["id"] != page.block]
    queue(c, page.pid, page.block, eid="gone")
    hook.process()
    assert db.one("SELECT open FROM topic_comments")["open"] == 0
    assert db.one("SELECT comments_err FROM topics")["comments_err"] is None
    def boom(*a, **kw):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(TC, "scan_one", boom)
    queue(c, page.pid, page.other, eid="error")
    hook.process()
    assert db.one("SELECT outcome FROM notion_events WHERE id='error'")["outcome"] == "error"
    assert db.one("SELECT 1 FROM events WHERE level='warn'")
    assert SECRET not in "\n".join(db.connect().iterdump())
    hook.process()  # errors are terminal too


def test_full_scan_and_target_read_have_same_rows_except_under(c, page):
    verify(c)
    a = page.n.say(page.block, "课代表：请补充")
    page.n.say(page.block, "继续说", thread=a["discussion_id"])
    page.n.say(page.block, "〔课代表〕已答", by=BOT, thread=a["discussion_id"])
    page.n.say(page.block, "普通笔记")
    page.n.say(page.note, "课代表 这期也要看")
    assert TC.scan_one(1, page.pid, page.block) == 2
    assert TC.scan_one(1, page.row, page.note) == 1
    pointed = db.q("SELECT * FROM topic_comments ORDER BY id")
    assert TC.scan(1) == 0
    full = db.q("SELECT * FROM topic_comments ORDER BY id")
    assert [{k: v for k, v in x.items() if k != "under"} for x in pointed] == [{k: v for k, v in x.items() if k != "under"} for x in full]
    assert any(a["under"] != b["under"] for a, b in zip(pointed, full))


def test_event_processing_and_full_scan_are_serial(c, page, monkeypatch):
    verify(c)
    page.n.say(page.block, "课代表：请改")
    queue(c, page.pid, page.block)
    entered, release, scanned = threading.Event(), threading.Event(), threading.Event()
    original = TC._list
    failures = []
    def slow(n, bid):
        if threading.current_thread().name == "hook-test" and bid == page.block:
            entered.set()
            assert release.wait(5)
        if threading.current_thread().name == "scan-test":
            scanned.set()
        return original(n, bid)
    monkeypatch.setattr(TC, "_list", slow)
    def invoke(fn):
        try:
            fn()
        except Exception as e:
            failures.append(e)
    a = threading.Thread(target=lambda: invoke(hook.process), name="hook-test")
    b = threading.Thread(target=lambda: invoke(lambda: TC.scan(1)), name="scan-test")
    a.start()
    assert entered.wait(5)
    b.start()
    assert not scanned.wait(0.15)
    # A second housekeeping tick must not create a competing comment thread either.
    monkeypatch.setattr(run, "_watching", {"h": a})
    monkeypatch.setattr(run, "_last", dict.fromkeys(run._last, 10000.0))
    run.housekeeping(10000.0)
    assert run._watching["h"] is a
    release.set()
    a.join(5)
    b.join(5)
    assert scanned.is_set() and not failures and not a.is_alive() and not b.is_alive()


def test_thirty_day_retention(c):
    from assetizer import logs
    verify(c)
    queue(c, RESEARCH, eid="old")
    queue(c, RESEARCH, eid="recent")
    db.exec("UPDATE notion_events SET at=? WHERE id='old'", ago(31 * 86400))
    logs.prune()
    assert [e["id"] for e in db.q("SELECT * FROM notion_events")] == ["recent"]


def test_manifest_and_tunnel_process():
    import yaml
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "lzc-manifest.yml").read_text())
    service = manifest["services"]["hook-tunnel"]
    assert not any("proxy" in x.lower() for x in service.get("environment", []))
    assert "healthcheck" not in service and service["mem_limit"] == "128m"
    assert service["binds"] == ["/lzcapp/var/assetizer/hook-tunnel:/ingress"]
    assert "/ingress/tunnel-token" in (root / "scripts/lzc/hook-tunnel.sh").read_text()
    subprocess.run(["sh", "-n", str(root / "scripts/lzc/hook-tunnel.sh")], check=True)


def test_streamed_size_boundary_and_malformed_signed_metadata(c):
    verify(c)
    prefix, suffix = b'{"type":"page.updated","padding":"', b'"}'
    raw = prefix + b'x' * (hook.LIMIT - len(prefix) - len(suffix)) + suffix
    headers = {"Content-Type": "application/json", "X-Notion-Signature": "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()}
    assert c.post(URL, content=raw, headers=headers).status_code == 200
    assert c.post(URL, content=iter([raw, b'x']), headers=headers).status_code == 413
    for body in [{"type": []}, {"type": {}}, {"type": "comment.created"}, {**event(), "authors": "bot"}]:
        raw, headers = signed(body)
        assert c.post(URL, content=raw, headers=headers).status_code == 400
    assert not db.q("SELECT * FROM notion_events")


def test_raw_dot_segments_cannot_pass_hook_guard(home):
    import asyncio
    from starlette.requests import Request
    for raw in [b'/api/notion/./webhook', b'/api/notion/../notion/webhook']:
        request = Request({"type": "http", "method": "POST", "path": URL, "raw_path": raw,
                           "query_string": b"", "headers": [(b"host", b"notion-hook"), (b"content-type", b"application/json")]})
        async def next_route(request):
            pytest.fail("a noncanonical raw path reached the endpoint")
        assert asyncio.run(web.guard(request, next_route)).status_code == 404


def test_identity_transport_failure_still_reads_topic_and_row(c, page, monkeypatch, caplog):
    verify(c)
    page.n.say(page.block, "课代表：专题仍要读")
    page.n.say(page.row, "课代表：行页仍要读")
    def unavailable(**kw):
        assert kw == {"tries": 1, "timeout": 5}
        page.n.calls.append(("GET", "/users/me"))
        raise httpx.ConnectError(SECRET)
    monkeypatch.setattr(page.n, "me", unavailable)
    queue(c, page.pid, page.block, eid="transport-topic")
    queue(c, page.row, eid="transport-row")
    hook.process()
    assert [e["outcome"] for e in db.q("SELECT * FROM notion_events")] == ["topic", "row"]
    assert page.n.calls == [("GET", "/users/me"),
                            ("GET", f"/comments?block_id={page.block}&page_size=100"), ("GET", f"/blocks/{page.block}"),
                            ("GET", f"/comments?block_id={page.row}&page_size=100")]
    assert len(db.q("SELECT * FROM topic_comments")) == 2
    assert SECRET not in "\n".join(db.connect().iterdump()) + caplog.text


@pytest.mark.parametrize("failure", [503, 429, "transport"])
def test_real_client_identity_is_one_attempt_with_five_second_timeout(c, page, monkeypatch, failure):
    from assetizer.notion import client as NC
    verify(c)
    queue(c, page.pid, page.block, eid="real-topic")
    queue(c, page.row, eid="real-row")
    calls, sleeps, identity_timeouts = [], [], []
    def serve(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/users/me"):
            identity_timeouts.append(request.extensions["timeout"])
            if failure == "transport":
                raise httpx.ConnectError("synthetic", request=request)
            return httpx.Response(failure, json={"message": "synthetic"}, headers={"Retry-After": "99"})
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        if "/blocks/" in request.url.path:
            return httpx.Response(200, json={"id": page.block, "type": "paragraph", "paragraph": {"rich_text": []}})
        pytest.fail(str(request.url))
    n = NC.Notion("synthetic")
    n.c.close()
    with httpx.Client(base_url=NC.API, transport=httpx.MockTransport(serve), timeout=120) as transport:
        n.c = transport
        monkeypatch.setattr(TN, "client", lambda: n)
        monkeypatch.setattr(NC, "time", SimpleNamespace(sleep=sleeps.append))
        hook.process()
    assert len([p for _, p in calls if p.endswith("/users/me")]) == 1
    assert identity_timeouts == [dict.fromkeys(("connect", "read", "write", "pool"), 5)]
    assert sleeps == []
    assert len(calls) == 4  # identity, block discussion, block itself, row-page discussion
    assert [e["outcome"] for e in db.q("SELECT * FROM notion_events")] == ["topic", "row"]


def test_real_client_comment_reads_keep_six_attempts(home, monkeypatch):
    from assetizer.notion import client as NC
    calls, sleeps = [], []
    def serve(request):
        calls.append(request.url.path)
        return httpx.Response(503 if len(calls) < 6 else 200, json={"results": [], "has_more": False})
    n = NC.Notion("synthetic")
    n.c.close()
    with httpx.Client(base_url=NC.API, transport=httpx.MockTransport(serve)) as transport:
        n.c = transport
        monkeypatch.setattr(NC, "time", SimpleNamespace(sleep=sleeps.append))
        monkeypatch.setattr(TC, "PAUSE", 0)
        assert TC._list(n, RESEARCH) == []
    assert calls == ["/v1/comments"] * 6
    assert sleeps == [1, 2, 4, 8, 16]


def test_ingress_database_wait_is_bounded_and_retry_commits(c, page):
    verify(c)
    normal = db.connect()
    assert normal.execute("PRAGMA busy_timeout").fetchone()[0] == 120000
    blocker = sqlite3.connect(str(config.get().db_path), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        response = send(c, event("blocked", page.pid, page.block))
        elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert 4.5 <= elapsed < 6.5
        assert db.one("SELECT 1 FROM notion_events WHERE id='blocked'") is None
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert send(c, event("blocked", page.pid, page.block)).status_code == 200
    assert db.one("SELECT 1 FROM notion_events WHERE id='blocked'")
    assert db.connect() is normal
    assert normal.execute("PRAGMA busy_timeout").fetchone()[0] == 120000


def test_ingress_receive_lock_wait_is_bounded(c):
    verify(c)
    assert hook._receive_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        response = send(c)
        elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert 4.5 <= elapsed < 6.5
        assert not db.q("SELECT * FROM notion_events")
    finally:
        hook._receive_lock.release()
    assert send(c).status_code == 200


def test_rejected_timestamp_and_rotation_reset(c):
    verify(c)
    assert c.post(URL, json=event()).status_code == 401
    stats = c.get("/api/notion/hook").json()["events"]
    assert stats["rejected"] == 1 and dt.datetime.fromisoformat(stats["rejected_at"])
    assert send(c).status_code == 200
    before = hook.events()
    c.post("/api/notion/hook/rearm", json={})
    verify(c, "replacement-synthetic")
    stats = c.get("/api/notion/hook").json()["events"]
    assert stats["rejected"] == 0 and stats["rejected_at"] is None and stats["signed_at"] is None
    assert stats["total"] == before["total"] and stats["last_at"] == before["last_at"]


def _join_thread(thread, failures):
    thread.join(5)
    assert not thread.is_alive() and not failures


def test_busy_topic_scan_does_not_block_other_topic_event(c, page, monkeypatch):
    verify(c)
    pid_b = page.n.create_page(page.pid, "专题 B")["id"]
    db.exec("INSERT INTO topics(question,notion_page_id,comments_at,created_at,updated_at) VALUES (?,?,?,?,?)",
            "B", pid_b, now(), now(), now())
    page.n.say(pid_b, "课代表：先处理 B")
    entered, release = threading.Event(), threading.Event()
    failures = []
    original = TC._list
    def stalled(n, bid):
        if threading.current_thread().name == "full-A" and bid == page.pid:
            entered.set()
            assert release.wait(5)
        return original(n, bid)
    monkeypatch.setattr(TC, "_list", stalled)
    def full_scan():
        try:
            TC.scan(1)
        except Exception as e:
            failures.append(e)
    full = threading.Thread(target=full_scan, name="full-A")
    full.start()
    try:
        assert entered.wait(2)
        # Put A first: a busy A must not make the event thread block before it reaches B.
        queue(c, page.pid, page.block, eid="busy-A")
        queue(c, pid_b, eid="free-B")
        monkeypatch.setattr(run, "_watching", {})
        monkeypatch.setattr(run, "_last", dict.fromkeys(run._last, 10000.0))
        started = time.monotonic()
        run.housekeeping(10000.0)
        while time.monotonic() - started < 1:
            if db.one("SELECT done FROM notion_events WHERE id='free-B'")["done"]:
                break
            time.sleep(0.005)
        finished_b = db.one("SELECT done,outcome FROM notion_events WHERE id='free-B'")
        assert finished_b["done"] is not None and finished_b["outcome"] == "topic"
        assert time.monotonic() - started < 1
        assert db.one("SELECT done FROM notion_events WHERE id='busy-A'")["done"] is None
        assert run._watching["h"].name == "topic-hook"
    finally:
        release.set()
        _join_thread(full, failures)
        if run._watching.get("h"):
            run._watching["h"].join(5)
    assert db.one("SELECT done FROM notion_events WHERE id='busy-A'")["done"] is not None


def test_hook_thread_drains_events_arriving_during_processing(c, page, monkeypatch):
    verify(c)
    queue(c, page.pid, page.block, eid="first")
    original = TC._list
    arrived = []
    def new_arrival(n, bid):
        if not arrived:
            arrived.append(True)
            raw, headers = signed(event("during-processing", page.pid, page.other))
            assert hook.receive(raw, headers["X-Notion-Signature"]) == 200
        return original(n, bid)
    monkeypatch.setattr(TC, "_list", new_arrival)
    monkeypatch.setattr(run, "_watching", {})
    monkeypatch.setattr(run, "_last", dict.fromkeys(run._last, 10000.0))
    run.housekeeping(10000.0)  # Only one tick; the new event must not need a second one.
    run._watching["h"].join(5)
    assert not run._watching["h"].is_alive()
    assert [e["outcome"] for e in db.q("SELECT * FROM notion_events")] == ["topic", "topic"]
    assert all(e["done"] for e in db.q("SELECT * FROM notion_events"))
    assert page.n.calls.count(("GET", "/users/me")) == 2  # one per snapshot, no identity cache across batches


def test_due_starts_while_a_large_hook_batch_is_running(c, page, monkeypatch):
    verify(c)
    for i in range(160):
        block = page.n._block({"type": "paragraph", "paragraph": {"rich_text": []}})
        page.n.kids[page.pid].append(block)
        raw, headers = signed(event(f"batch-{i}", page.pid, block["id"]))
        assert hook.receive(raw, headers["X-Notion-Signature"]) == 200
    entered, release, due_started = threading.Event(), threading.Event(), threading.Event()
    original = TC._list
    def stalled(n, bid):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(n, bid)
    monkeypatch.setattr(TC, "_list", stalled)
    monkeypatch.setattr(TC, "due", due_started.set)
    monkeypatch.setattr(run, "_watching", {})
    monkeypatch.setattr(run, "_last", dict.fromkeys(run._last, 10000.0))
    run.housekeeping(10000.0)
    try:
        assert entered.wait(2)
        run._last["comments"] = 0
        run.housekeeping(10000.0)
        assert due_started.wait(1)
        assert run._watching["h"].is_alive() and run._watching["c"].name == "topic-comments"
    finally:
        release.set()
        run._watching["h"].join(5)
        if run._watching.get("c"):
            run._watching["c"].join(5)
    assert all(e["done"] for e in db.q("SELECT * FROM notion_events"))




def test_tunnel_status_and_doctor_only_stat_the_separate_token(c, monkeypatch, capsys):
    from assetizer import cli
    tunnel = hook.TUNNEL_TOKEN_FILE
    tunnel.parent.mkdir()
    tunnel.write_text("synthetic-tunnel-value")
    original_open = Path.open
    def no_tunnel_read(path, *args, **kw):
        assert path != tunnel, "tunnel credentials must only be stat-ed"
        return original_open(path, *args, **kw)
    monkeypatch.setattr(Path, "open", no_tunnel_read)
    monkeypatch.setattr(hook.httpx, "get", lambda *a, **kw: SimpleNamespace(status_code=200))
    assert c.get("/api/notion/hook").json()["tunnel"] == "up"
    cli.cmd_doctor(SimpleNamespace(agy=False))
    output = capsys.readouterr().out
    assert json.loads(output)["notion_hook"]["tunnel_token"] is True
    assert "synthetic-tunnel-value" not in output


def test_setup_docs_and_runtime_comments_match_the_contract():
    root = Path(__file__).resolve().parents[1]
    setup = (root / "docs/operations.md").read_text().split("## Notion 批注推送", 1)[1]
    assert "机房" not in setup and "设置 → Notion" in setup
    assert "Verify 后不会主动" in setup and "写一条批注" in setup
    assert "/lzcapp/var/assetizer/hook-tunnel/tunnel-token" in setup
    assert "notion-hook-tunnel-token" not in setup
    row = next(s for s in (root / "README.md").read_text().splitlines() if s.startswith("| 开批注推送 |"))
    assert "设置 → Notion" in row and "机房" not in row
    docker = (root / "docker/runtime.Dockerfile").read_text()
    assert docker.index("# Optional Notion webhook connector") < docker.index("RUN curl -fsSL --retry 3 -o /tmp/cloudflared")
    assert docker.index("/usr/local/bin/cloudflared") < docker.index("# yt-dlp solves") < docker.index("RUN curl -fsSL -o /tmp/deno.zip")


TUNNEL = "eyJhIjoiYWNjb3VudC1zeW50aGV0aWMiLCJ0IjoidHVubmVsLXN5bnRoZXRpYyIsInMiOiJzZWNyZXQtc3ludGhldGljIn0="


def test_tunnel_token_is_placed_from_settings_write_only(c, monkeypatch):
    import base64
    assert json.loads(base64.b64decode(TUNNEL)) == {"a": "account-synthetic", "t": "tunnel-synthetic", "s": "secret-synthetic"}
    monkeypatch.setattr(hook.httpx, "get", lambda *a, **kw: SimpleNamespace(status_code=503))
    path = "/api/notion/hook/tunnel"
    for body in [{}, {"token": "not-a-tunnel-token"}, {"token": base64.b64encode(b'{"a":"x"}').decode()}, {"token": 1}, {"other": "x"}]:
        r = c.post(path, json=body)
        assert r.status_code == 400 and "not-a-tunnel" not in r.text
    assert not hook.TUNNEL_TOKEN_FILE.exists()
    r = c.post(path, json={"token": f"  {TUNNEL[:20]}\n{TUNNEL[20:]} "})  # pasted across lines: whitespace dropped
    assert r.status_code == 200 and r.json()["tunnel"] == "down" and TUNNEL not in r.text
    assert hook.TUNNEL_TOKEN_FILE.read_text() == TUNNEL
    assert hook.TUNNEL_TOKEN_FILE.stat().st_mode & 0o777 == 0o600
    assert hook.TUNNEL_TOKEN_FILE.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in hook.TUNNEL_TOKEN_FILE.parent.iterdir()] == ["tunnel-token"]
    assert c.post(path, json={"clear": ["token"]}).json()["tunnel"] == "off"
    assert not hook.TUNNEL_TOKEN_FILE.exists()
    assert c.post(path, json={"clear": ["token"]}).status_code == 200  # already gone
    assert TUNNEL not in "\n".join(e["message"] for e in db.q("SELECT message FROM events"))
    assert c.post(path, json={"token": TUNNEL}, headers={"Host": "notion-hook"}).status_code == 404


ME = {"object": "user", "id": "bot-课代表", "name": "课代表", "type": "bot", "bot": {"workspace_name": "Demo"}}
NOTION = "ntn_" + "S" * 46


def test_notion_token_is_checked_with_notion_then_kept(c, home, monkeypatch):
    from assetizer import config
    from assetizer.notion import client as NC
    asked = []
    answer = {"v": ME}

    def me(self, **kw):
        asked.append((self.c.headers["Authorization"], kw, self.c.timeout.read))
        if isinstance(answer["v"], Exception):
            raise answer["v"]
        return answer["v"]
    monkeypatch.setattr(NC.Notion, "me", me)
    path = "/api/notion/token"
    secret = home / "secrets/notion-token"
    assert c.post(path, json={}).status_code == 400
    r = c.post(path, json={"token": "nonsense-value"})
    assert r.status_code == 400 and "nonsense" not in r.text and not asked
    answer["v"] = NC.NotionError(401, '{"message":"API token is invalid."}')
    r = c.post(path, json={"token": NOTION})
    assert r.status_code == 400 and r.json()["detail"].startswith("token 不对") and not secret.exists()
    answer["v"] = httpx.ConnectError("synthetic")
    r = c.post(path, json={"token": NOTION})
    assert r.status_code == 502 and "没有保存" in r.json()["detail"] and not secret.exists()
    db.raise_alert("notion-auth", "critical", "Notion token 已失效，交付暂停")
    answer["v"] = ME
    r = c.post(path, json={"token": NOTION})
    assert r.status_code == 200 and NOTION not in r.text
    assert db.one("SELECT resolved_at FROM alerts WHERE key='notion-auth'")["resolved_at"]
    v = r.json()
    assert v["token"] is True and v["me"]["ok"] is True and v["me"]["name"] == "课代表" and v["me"]["workspace"] == "Demo"
    assert asked[-1] == (f"Bearer {NOTION}", {"tries": 1}, 5)
    assert config.get().secret("notion-token") == NOTION and secret.stat().st_mode & 0o777 == 0o600
    assert c.get("/api/settings").json()["notion"]["me"]["name"] == "课代表"

    # 测试: a failure keeps the name last seen
    answer["v"] = NC.NotionError(401, "{}")
    t = c.post("/api/notion/test", json={}).json()["me"]
    assert t["ok"] is False and t["code"] == "token" and t["name"] == "课代表"
    assert c.post("/api/notion/test", json={}).status_code == 429
    web._throttle_at.pop("notion", None)
    answer["v"] = ME
    t = c.post("/api/notion/test", json={}).json()["me"]
    assert t["ok"] is True and "code" not in t

    assert c.post(path, json={"clear": ["token"]}).json() | {"asks": None} == {
        "token": False, "me": None, "database": None, "parent": None, "title": "课代表", "asks": None}
    assert not secret.exists()
    assert c.post("/api/notion/test", json={}).status_code == 400
    assert NOTION not in "\n".join(e["message"] for e in db.q("SELECT message FROM events"))

    monkeypatch.setenv(config.secret_env("notion-token"), NOTION)
    assert c.post(path, json={"token": NOTION}).status_code == 409
