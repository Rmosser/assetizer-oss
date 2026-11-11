"""Channel pages use the topic placement rules, with lazy files and durable keyed checkpoints. No network."""
from copy import deepcopy
import hashlib
import json

import pytest

from assetizer import config, db, delivery
from assetizer.notion import blocks as B
from assetizer.notion.client import NotionError
from assetizer.notion.sync import _new, _shape, plan_hash
from assetizer.util import now
from test_topic_notion import BOT, YOU, OWN_DB, FakeNotion, home  # shared realistic Notion and isolated home


class ChannelNotion(FakeNotion):
    def __init__(self):
        super().__init__()
        self.dbs[OWN_DB]["properties"] = {k: {"id": k, "type": next(iter(v)), **v}
                                              for k, v in delivery.DB_PROPS.items()}
        self.uploaded, self.attached, self.requests = {}, {}, []
        self.appends = self.upload_attempts = 0
        self.fail_append = self.fail_upload = self.fail_delete = None
        self.lose_append = self.fail_props = False
        self.on_append = None

    def _block(self, b, by=None):
        made = super()._block(b, by)
        kind = made["type"]
        if kind in ("image", "video") and made[kind].get("type") == "file_upload":
            uid = made[kind]["file_upload"]["id"]
            assert uid in self.uploaded and uid not in self.attached
            self.attached[uid] = made["id"]
            made[kind] = {"type": "file", "file": {"url": f"https://files.invalid/{uid}?expires=soon"},
                          "caption": made[kind]["caption"]}
        return made

    def upload(self, path, content_type):
        self.upload_attempts += 1
        self.calls.append(("UPLOAD", str(path)))
        self.requests.append(("UPLOAD", str(path), content_type))
        if self.upload_attempts == self.fail_upload:
            raise NotionError(502, "upload interrupted")
        uid = f"up-{self.upload_attempts}"
        self.uploaded[uid] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "path": str(path)}
        return {"id": uid}

    def append(self, bid, children, after=None, *, position=None):
        self.appends += 1
        self.requests.append(("PATCH", f"/blocks/{bid}/children", {"children": deepcopy(children), "after": after}))
        if position:
            self.requests[-1][2]["position"] = position
        if self.on_append:
            self.on_append()
        if self.appends == self.fail_append:
            self.calls.append(("PATCH", f"/blocks/{bid}/children", len(children), after))
            raise NotionError(502, "append interrupted")
        got = super().append(bid, children, after, position=position)
        if self.lose_append:
            self.lose_append = False
            raise NotionError(502, "append applied but its answer lost")
        return got

    def _req(self, method, path, idempotent=None, **kw):
        self.requests.append((method, path, deepcopy(kw.get("json"))))
        assert not (kw.get("json") or {}).get("archived"), "page must never be archived"
        if method == "PATCH" and path.startswith("/pages/") and self.fail_props:
            raise NotionError(502, "properties interrupted")
        if method == "DELETE" and path == f"/blocks/{self.fail_delete}":
            raise NotionError(502, "delete interrupted")
        got = super()._req(method, path, idempotent, **kw)
        if method == "POST" and path == "/pages":
            pid = got["id"]
            self.pages[pid]["url"] = got["url"] = f"https://www.notion.so/{pid}"
        return got

    def archive_page(self, pid):
        raise AssertionError("page must never be archived")

    def writes(self):
        return [x for x in self.requests if x[0] != "GET"]


def text(key, words):
    return {"key": key, "block": B.block("paragraph", words)}


def media(home, key, data, kind="image", caption="一张图"):
    p = home / f"{key}.{'mp4' if kind == 'video' else 'jpg'}"
    p.write_bytes(data)
    return {"key": key, "media": {"path": str(p), "kind": kind, "caption": caption}}


def _channel_fixture(home, monkeypatch):
    n = ChannelNotion()
    db.set_setting("notion_database_id", OWN_DB)
    db.set_setting("notion_schema", 2)
    t = now()
    db.exec("INSERT INTO sources(id, url, title, current_run_id, created_at, updated_at) VALUES ('s','https://youtu.be/s','一期','r1',?,?)", t, t)
    db.exec("INSERT INTO runs(id, source_id, production, summary, created_at) VALUES ('r1','s','COMPLETE','{}',?)", t)
    monkeypatch.setattr(delivery, "client", lambda: n)
    plan = [text("head", "开头"), text("same", "没变"), text("change", "旧字"),
            media(home, "image", b"old-image"), media(home, "video", b"same-video", "video", "一段视频"),
            text("remove", "要删的一节"),
            {"key": "runinfo", "block": B.toggle("运行信息", [B.block("paragraph", "旧运行")])},
            {"key": "notes", "block": B.toggle("校注", [B.block("bulleted_list_item", "旧校注")])}]
    monkeypatch.setattr(delivery, "_default_plan", lambda *a: deepcopy(plan))
    monkeypatch.setattr(delivery, "_tail", lambda *a: [])
    return n, plan


@pytest.fixture
def channel(home, monkeypatch):
    return _channel_fixture(home, monkeypatch)


def page(n):
    return db.one("SELECT notion_page_id FROM sources WHERE id='s'")["notion_page_id"]


def mapped():
    return {r["block_key"]: r for r in db.q("SELECT * FROM notion_blocks WHERE source_id='s'")}


def delivered(n):
    result = delivery.deliver("s")
    assert result["status"] == "DELIVERED", result
    return page(n)


def stable(n, pid, uploads):
    assert page(n) == pid
    assert len([c for c in n.calls if c[:2] == ("POST", "/pages")]) == 1
    assert all(not pg.get("archived") for pg in n.pages.values())
    assert n.upload_attempts == uploads


def aligned(n, plan, extra=()):
    rows = mapped()
    assert set(rows) == {it["key"] for it in plan}
    got = n.children(page(n))
    live = {b["id"]: b for b in got}
    assert len({r["block_id"] for r in rows.values()}) == len(plan)
    assert set(live) == {r["block_id"] for r in rows.values()} | set(extra)
    assert [r["block_id"] for r in (rows[it["key"]] for it in plan)] == [b["id"] for b in got if b["id"] not in extra]
    for it in plan:
        assert rows[it["key"]]["hash"] == plan_hash(it)
        want = it.get("block") or {"type": it["media"]["kind"]}
        def shape(b):
            return _shape(b, [shape(k) for k in n.children(b["id"])])
        assert shape(live[rows[it["key"]]["block_id"]]) == _new(want, _shape)
    # Sync.check verifies nested shapes; the keyed mapping must name each actual root exactly once.


@pytest.mark.parametrize("new_run", [False, True])
def test_channel_content_and_new_run_keep_page_and_unchanged_blocks(channel, home, new_run):
    n, plan = channel
    pid = delivered(n)
    before, start = mapped(), len(n.requests)
    plan[2] = text("change", "新字")
    plan[3] = media(home, "other-image", b"new-image") | {"key": "image"}
    # Same file bytes at another path keep the file block and never upload again.
    plan[4] = media(home, "other-video", b"same-video", "video", "一段视频") | {"key": "video"}
    plan.pop(5)
    plan.insert(5, text("added", "新的一节"))
    if new_run:
        db.exec("INSERT INTO runs(id, source_id, production, summary, created_at) VALUES ('r2','s','COMPLETE','{}',?)", now())
        db.exec("UPDATE sources SET current_run_id='r2', delivery='STALE' WHERE id='s'")
    delivered(n)
    stable(n, pid, 3)
    after = mapped()
    for k in ("head", "same", "video", "runinfo", "notes"):
        assert after[k]["block_id"] == before[k]["block_id"]
    assert after["change"]["block_id"] != before["change"]["block_id"]
    assert after["image"]["block_id"] != before["image"]["block_id"]
    assert not any(c[:2] == ("POST", "/pages") for c in n.requests[start:])
    assert db.one("SELECT notion_synced_run FROM sources")["notion_synced_run"] == ("r2" if new_run else "r1")
    aligned(n, plan)
    event = db.q("SELECT message FROM events WHERE kind='delivery'")[-1]["message"]
    assert all(x in event for x in ("写入", "保留", "删除", "上传"))


@pytest.mark.parametrize("key,remove", [("change", False), ("remove", True), ("image", False), ("runinfo", False), ("notes", False)])
def test_channel_comments_protect_block_until_next_delivery_after_resolving(channel, home, key, remove):
    n, plan = channel
    pid = delivered(n)
    old = mapped()[key]["block_id"]
    on = n.kids[old][0]["id"] if key in ("runinfo", "notes") else old
    comment = n.say(on, "这里有疑问")
    at = next(i for i, it in enumerate(plan) if it["key"] == key)
    if remove:
        plan.pop(at)
    elif key == "image":
        plan[at] = media(home, "replacement", b"changed-image") | {"key": key}
    elif key in ("runinfo", "notes"):
        plan[at] = {"key": key, "block": B.toggle("新折叠", [B.block("paragraph", "新内容")])}
    else:
        plan[at] = text(key, "改正这一行")
    delivered(n)
    assert any(b["id"] == old for b in n.children(pid))
    assert ("DELETE", f"/blocks/{old}") not in [c[:2] for c in n.calls]
    aligned(n, plan, [old])
    if not remove:
        ids = [b["id"] for b in n.children(pid)]
        assert abs(ids.index(old) - ids.index(mapped()[key]["block_id"])) == 1
    delivered(n)  # unresolved across a retry: no duplicate correction
    aligned(n, plan, [old])
    comment["resolved"] = True
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 3 if key == "image" else 2)
    assert ("DELETE", f"/blocks/{old}") in [c[:2] for c in n.calls]


@pytest.mark.parametrize("edit", ["edited", "child", "edited-child"])
def test_channel_human_edits_and_children_are_preserved(channel, edit):
    n, plan = channel
    pid = delivered(n)
    key = "change" if edit == "edited" else "runinfo"
    old = mapped()[key]["block_id"]
    block = next(b for b in n.kids[pid] if b["id"] == old)
    if edit == "edited":
        block["last_edited_by"] = {"id": YOU}
        block["paragraph"]["rich_text"] = B.rich("用户改的字")
    elif edit == "child":
        n.kids[old].append(n._block(B.block("paragraph", "用户补的"), YOU))
    else:
        n.kids[old][0]["last_edited_by"] = {"id": YOU}
    snapshot = deepcopy((block, n.kids.get(old)))
    delivered(n)
    assert (block, n.kids.get(old)) == snapshot
    aligned(n, plan, [old])
    stable(n, pid, 2)


def test_channel_user_block_stays_between_unchanged_neighbors(channel):
    n, plan = channel
    pid = delivered(n)
    before = mapped()
    note = n._block(B.block("paragraph", "用户自己的笔记"), YOU)
    n.kids[pid].insert(2, note)
    plan[2] = text("change", "换一行")
    delivered(n)
    ids = [b["id"] for b in n.children(pid)]
    assert ids.index(before["head"]["block_id"]) < ids.index(note["id"]) < ids.index(before["image"]["block_id"])
    assert ids[ids.index(before["same"]["block_id"]) + 1] == note["id"]
    aligned(n, plan, [note["id"]])
    stable(n, pid, 2)


def test_channel_comments_403_is_no_comments_and_page_discussions_are_untouched(channel):
    n, plan = channel
    pid = delivered(n)
    old = mapped()["change"]["block_id"]
    n.say(old, "看不到的块评论")
    n.say(pid, "整页讨论不影响逐块写")
    n.can.remove("read")
    plan[2] = text("change", "新字")
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 2)
    assert not any(c[1].startswith(f"/comments?block_id={pid}&") for c in n.calls)


@pytest.mark.parametrize("failure,k", [("append", 1), ("append", 2), ("append", 3), ("upload", 1), ("upload", 2)])
def test_channel_interrupted_batches_checkpoint_and_retry_only_missing_files(channel, failure, k):
    n, plan = channel
    if failure == "append" and k == 3:
        plan.extend(text(f"extra-{i}", f"第三批 {i}") for i in range(40))
    setattr(n, f"fail_{failure}", k)
    assert delivery.deliver("s")["status"] == "FAILED"
    pid = page(n)
    live = {b["id"] for b in n.children(pid)}
    assert {r["block_id"] for r in mapped().values()} == live
    attached_before = set(n.attached)
    uploads_before = n.upload_attempts
    missing_before = sum(it.get("media") is not None and it["key"] not in mapped() for it in plan)
    n.fail_append = n.fail_upload = None
    delivered(n)
    assert n.upload_attempts == uploads_before + missing_before
    assert attached_before <= set(n.attached)
    aligned(n, plan)
    stable(n, pid, uploads_before + missing_before)


def test_channel_each_append_is_checkpointed_before_another_request(channel, home):
    n, plan = channel
    plan[4:4] = [text("between-images", "图片间的文字"), media(home, "second-image", b"second-image")]
    def check_previous():
        assert {r["block_id"] for r in mapped().values()} == {b["id"] for b in n.children(page(n))}
    n.on_append = check_previous
    original_upload = n.upload
    def upload(*args):
        check_previous()
        return original_upload(*args)
    n.upload = upload
    pid = delivered(n)
    aligned(n, plan)
    stable(n, pid, 3)
    # Several images and text can share a batch; all uploads are contiguous immediately before its append.
    writes = n.writes()
    pending = []
    batches = []
    for req in writes:
        if req[0] == "UPLOAD":
            pending.append(req[1])
        elif req[1].endswith("/children"):
            batch = req[2]["children"]
            batches.append([b["type"] for b in batch])
            attached = [n.uploaded[b[b["type"]]["file_upload"]["id"]]["path"]
                        for b in batch if b["type"] in ("image", "video")]
            assert pending == attached
            pending = []
        else:
            assert not pending
    assert batches == [["paragraph", "paragraph", "paragraph", "image", "paragraph", "image", "video"],
                       ["paragraph", "toggle", "toggle"]]
    assert not pending


def test_channel_applied_text_append_lost_answer_is_reconciled(channel):
    n, plan = channel
    plan[:] = [text("a", "一"), text("b", "二")]
    n.lose_append = True
    assert delivery.deliver("s")["status"] == "FAILED"
    pid = page(n)
    ids = [b["id"] for b in n.children(pid)]
    delivered(n)
    assert [b["id"] for b in n.children(pid)] == ids
    aligned(n, plan)
    stable(n, pid, 0)


def test_channel_retry_after_delete_failure_has_no_orphan_or_duplicate(channel):
    n, plan = channel
    pid = delivered(n)
    n.fail_delete = mapped()["change"]["block_id"]
    plan[2] = text("change", "新字")
    assert delivery.deliver("s")["status"] == "FAILED"
    new = mapped()["change"]["block_id"]
    n.fail_delete = None
    delivered(n)
    assert mapped()["change"]["block_id"] == new
    aligned(n, plan)
    stable(n, pid, 2)


def test_channel_legacy_hashes_match_text_and_replace_media_once(channel):
    n, plan = channel
    pid = delivered(n)
    before = mapped()
    db.exec("UPDATE notion_blocks SET hash='0123456789abcdef'")
    delivered(n)
    for it in plan:
        if "block" in it:
            assert mapped()[it["key"]]["block_id"] == before[it["key"]]["block_id"]
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 4)


@pytest.mark.parametrize("commented", [False, True])
def test_channel_untracked_media_never_matches(channel, commented):
    n, plan = channel
    pid = delivered(n)
    old = mapped()["image"]["block_id"]
    db.exec("DELETE FROM notion_blocks WHERE block_id=?", old)
    if commented:
        n.say(old, "要留着")
    delivered(n)
    assert mapped()["image"]["block_id"] != old
    aligned(n, plan, [old] if commented else [])
    stable(n, pid, 3)


def test_channel_same_path_and_size_different_bytes_uploads_again(channel, home):
    n, plan = channel
    pid = delivered(n)
    before = mapped()["image"]["block_id"]
    path = home / "image.jpg"
    path.write_bytes(b"NEW-image")
    assert path.stat().st_size == len(b"old-image")
    delivered(n)
    assert mapped()["image"]["block_id"] != before
    aligned(n, plan)
    stable(n, pid, 3)


@pytest.mark.parametrize("old_access", [True, False])
def test_channel_database_change_creates_new_page_leaves_old_untouched(channel, old_access):
    n, plan = channel
    old = delivered(n)
    snapshot = deepcopy((n.pages[old], n.kids[old]))
    new_db = "ffffffff-0000-0000-0000-000000000006"
    n.dbs[new_db] = deepcopy(n.dbs[OWN_DB]) | {"id": new_db}
    db.set_setting("notion_database_id", new_db)
    if not old_access:
        original = n.page
        def no_old_page(pid):
            if pid == old:
                raise NotionError(403, "old database is no longer accessible")
            return original(pid)
        n.page = no_old_page
    new = delivered(n)
    assert new != old
    assert (n.pages[old], n.kids[old]) == snapshot
    assert len([c for c in n.calls if c[:2] == ("POST", "/pages")]) == 2
    assert n.upload_attempts == 4
    aligned(n, plan)


def test_channel_old_replacement_hold_migrates_and_api_is_gone(channel):
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    n, plan = channel
    pid = delivered(n)
    db.exec("UPDATE sources SET delivery='HELD', delivery_reason='YOURS_ON_PAGE'")
    db.set_setting("replace_anyway:s", True)
    db.set_setting("yours_on_page:s", ["改了字"])
    db.set_setting("write_anyway:other", True)
    db._local.conn.close()
    db._local.conn = None
    assert db.one("SELECT delivery, delivery_reason FROM sources") == {"delivery": "STALE", "delivery_reason": None}
    assert not db.q("SELECT key FROM settings WHERE key GLOB 'replace_anyway:*' OR key GLOB 'yours_on_page:*'")
    assert db.setting("write_anyway:other") is True
    c, h = TestClient(app), {"host": "127.0.0.1"}
    assert c.post("/api/sources/s/replace", headers=h, json={"replace": True}).status_code in (404, 405)
    assert "/api/sources/{sid}/replace" not in app.openapi()["paths"]
    assert all("yours" not in it for it in c.get("/api/overview", headers=h).json()["issues"])
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 2)


@pytest.mark.parametrize("change", ["caption", "kind"])
def test_channel_media_caption_and_kind_are_part_of_identity(channel, change):
    n, plan = channel
    pid = delivered(n)
    old = mapped()["image"]["block_id"]
    plan[3]["media"][change] = "video" if change == "kind" else "新的说明"
    delivered(n)
    assert mapped()["image"]["block_id"] != old
    aligned(n, plan)
    stable(n, pid, 3)


@pytest.mark.parametrize("failure,k", [("append", 1), ("append", 2), ("append", 3), ("upload", 1), ("upload", 2)])
def test_channel_interrupted_replacement_reuses_completed_batches(channel, home, failure, k):
    n, plan = channel
    pid = delivered(n)
    plan[2] = text("change", "新文字")
    plan[3] = media(home, "new-image", b"new-image") | {"key": "image"}
    plan[4] = media(home, "new-video", b"new-video", "video", "新片段") | {"key": "video"}
    plan[5] = text("new-section", "新的一节")
    plan[6]["block"] = B.toggle("运行信息", [B.block("paragraph", "新运行")])
    if failure == "append" and k == 3:
        plan[7:7] = [text(f"extra-{i}", f"第三批 {i}") for i in range(40)]
    setattr(n, f"fail_{failure}", (n.appends if failure == "append" else n.upload_attempts) + k)
    assert delivery.deliver("s")["status"] == "FAILED"
    complete = {key: row["block_id"] for key, row in mapped().items()
                if key in {it["key"] for it in plan} and row["hash"] == plan_hash(next(it for it in plan if it["key"] == key))}
    uploads = n.upload_attempts
    missing = sum(it.get("media") is not None and it["key"] not in complete for it in plan)
    n.fail_append = n.fail_upload = None
    delivered(n)
    for key, bid in complete.items():
        assert mapped()[key]["block_id"] == bid
    aligned(n, plan)
    stable(n, pid, uploads + missing)


def test_channel_large_insert_stays_in_order_across_batches(channel):
    n, plan = channel
    pid = delivered(n)
    before = mapped()
    plan[2:3] = [text(f"insert-{i}", f"长段落 {i}") for i in range(125)]
    n.fail_append = n.appends + 3
    assert delivery.deliver("s")["status"] == "FAILED"
    n.fail_append = None
    delivered(n)
    aligned(n, plan)
    for key in ("head", "same", "image", "video", "runinfo", "notes"):
        assert mapped()[key]["block_id"] == before[key]["block_id"]
    sizes = [len(c[2]["children"]) for c in n.requests if c[1].endswith("/children") and c[0] == "PATCH"]
    assert max(sizes) == 40
    stable(n, pid, 2)


def test_channel_readback_media_is_still_required(channel):
    n, plan = channel
    pid = delivered(n)
    image = mapped()["image"]["block_id"]
    b = next(b for b in n.kids[pid] if b["id"] == image)
    b["image"]["type"] = "external"
    result = delivery.deliver("s")
    assert result["status"] == "FAILED" and "READBACK_MEDIA" in result["error"]
    stable(n, pid, 2)


def test_channel_readback_rejects_wrong_nested_shape(channel):
    n, plan = channel
    original = n.append
    def wrong(*args, **kwargs):
        got = original(*args, **kwargs)
        for b in got["results"]:
            if b["type"] == "toggle":
                n.kids[b["id"]][0]["type"] = "quote"
        return got
    n.append = wrong
    result = delivery.deliver("s")
    assert result["status"] == "FAILED" and "Notion 读回" in result["error"]
    assert db.one("SELECT notion_synced_run FROM sources")["notion_synced_run"] is None
    stable(n, page(n), 2)


def test_channel_new_plan_keys_claim_matching_blocks_and_drop_old_keys(channel):
    n, plan = channel
    pid = delivered(n)
    before = [r["block_id"] for r in mapped().values()]
    for it in plan:
        it["key"] = "new:" + it["key"]
    delivered(n)
    assert set(before) == {r["block_id"] for r in mapped().values()}
    aligned(n, plan)
    stable(n, pid, 2)


@pytest.mark.parametrize('state', ['archived', 'in_trash', '404'])
@pytest.mark.parametrize('mode', ['same', 'changed', 'newrun'])
def test_channel_deleted_page_is_recreated_once(channel, state, mode):
    n, plan = channel
    old = delivered(n)
    snapshot = deepcopy(n.kids[old])
    if state == '404':
        del n.pages[old]
    else:
        n.pages[old][state] = True
    if mode == 'changed':
        plan[2] = text('change', '新字')
    elif mode == 'newrun':
        db.exec("INSERT INTO runs(id,source_id,production,summary,created_at) VALUES ('r2','s','COMPLETE','{}',?)", now())
        db.exec("UPDATE sources SET current_run_id='r2'")
    cut = len(n.requests)
    new = delivered(n)
    assert new != old
    assert n.kids[old] == snapshot
    assert not any(r[0] != 'GET' and old in r[1] for r in n.requests[cut:])
    delivered(n)
    assert len([r for r in n.requests if r[:2] == ('POST', '/pages')]) == 2
    assert n.upload_attempts == 4
    assert len(db.q("SELECT * FROM events WHERE message='原来那页在 Notion 里已删除，另建了一页'")) == 1
    aligned(n, plan)


def test_channel_append_receipts_with_siblings_and_start_keep_all_ids(channel):
    n, plan = channel
    plan[:] = [text('a', 'A'), text('z', 'Z')]
    pid = delivered(n)
    old = mapped()
    plan[1:1] = [text(f'm-{i}', f'中间 {i}') for i in range(85)]
    delivered(n)
    assert all(mapped()[k]['block_id'] == old[k]['block_id'] for k in ('a', 'z'))
    plan[:0] = [text(f'p-{i}', f'页首 {i}') for i in range(85)]
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 0)
    starts = [r for r in n.requests if r[0] == 'PATCH' and r[1].endswith('/children') and r[2].get('position')]
    assert len(starts) == 1 and starts[0][2]['position'] == {'type': 'start'}


@pytest.mark.parametrize('fault', ['short', 'extra-old-prefix', 'wrong-type'])
def test_channel_malformed_append_receipt_retries_by_actual_content(channel, fault):
    n, plan = channel
    plan[:] = [text('a', 'A'), text('b', '旧B'), text('c', '旧C'), text('d', 'D')]
    pid = delivered(n)
    old = deepcopy(n.kids[pid][0])
    plan[1:3] = [text('b', '新B'), text('c', '新C')]
    original = n.append
    def corrupt(*args, **kwargs):
        got = deepcopy(original(*args, **kwargs))
        if fault == 'short':
            got['results'] = got['results'][:1]
        elif fault == 'extra-old-prefix':
            got['results'].insert(0, old)
        else:
            got['results'][0]['type'] = 'quote'
        return got
    n.append = corrupt
    result = delivery.deliver('s')
    assert result['status'] == 'FAILED' and 'READBACK_MISMATCH' in result['error']
    n.append = original
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 0)


def test_channel_new_integration_recognizes_old_authors_and_human_edits(channel):
    n, plan = channel
    pid = delivered(n)
    before = mapped()
    old_fold = before['runinfo']['block_id']
    n.kids[old_fold][0]['last_edited_by'] = {'id': YOU}
    snapshot = deepcopy(n.kids[old_fold])
    n.bot = 'new-integration'
    delivered(n)
    for key in ('head', 'same', 'change', 'image', 'video', 'notes'):
        assert mapped()[key]['block_id'] == before[key]['block_id']
    plan[2] = text('change', '新 integration 写的新字')
    delivered(n)
    assert mapped()['change']['block_id'] != before['change']['block_id']
    assert n.kids[old_fold] == snapshot
    aligned(n, plan, [old_fold])
    stable(n, pid, 2)


def test_channel_human_property_survives_two_new_runs(channel):
    n, plan = channel
    pid = delivered(n)
    n.pages[pid]['properties']['名称']['title'] = B.rich('我的私人标题')
    baseline = deepcopy(db.setting('notion_props:s'))
    for rid in ('r2', 'r3'):
        db.exec("INSERT INTO runs(id,source_id,production,summary,created_at) VALUES (?,'s','COMPLETE','{}',?)", rid, now())
        db.exec('UPDATE sources SET current_run_id=?', rid)
        delivered(n)
        assert B.plain({'type': 'p', 'p': {'rich_text': n.pages[pid]['properties']['名称']['title']}}) == '我的私人标题'
        assert n.pages[pid]['properties']['Run']['rich_text'][0]['text']['content'] == rid
        assert db.setting('notion_props:s')['名称'] == baseline['名称']
    stable(n, pid, 2)


@pytest.mark.parametrize('protection', ['comment', 'edit'])
@pytest.mark.parametrize('kind', ['text', 'media'])
def test_channel_adjacent_corrections_follow_each_protected_key_with_user_between(channel, home, protection, kind):
    n, plan = channel
    def item(key, version):
        return text(key, version + key) if kind == 'text' else media(home, version + key, (version + key).encode()) | {'key': key}
    plan[:] = [text('head', '开头'), item('b', '旧'), item('c', '旧'), text('tail', '结尾')]
    pid = delivered(n)
    old = mapped()
    user = n._block(B.block('paragraph', '自己的笔记'), YOU)
    n.kids[pid].insert(2, user)
    for key in ('b', 'c'):
        bid = old[key]['block_id']
        if protection == 'comment':
            n.say(bid, key)
        else:
            next(b for b in n.kids[pid] if b['id'] == bid)['last_edited_by'] = {'id': YOU}
    plan[1:3] = [item('b', '新'), item('c', '新')]
    delivered(n)
    rows = mapped()
    assert [b['id'] for b in n.children(pid)] == [old['head']['block_id'], old['b']['block_id'], rows['b']['block_id'],
                                                 user['id'], old['c']['block_id'], rows['c']['block_id'], old['tail']['block_id']]
    delivered(n)
    aligned(n, plan, [old['b']['block_id'], old['c']['block_id'], user['id']])
    stable(n, pid, 4 if kind == 'media' else 0)


@pytest.mark.parametrize('change', ['link', 'bold', 'color', 'code'])
def test_channel_rich_text_binding_and_format_changes_replace_block(channel, change):
    n, plan = channel
    plan[:] = [text('a', 'AB')]
    if change == 'link':
        plan[0] = text('a', '[A](https://example.org)B')
    pid = delivered(n)
    old = mapped()['a']['block_id']
    if change == 'link':
        plan[0] = text('a', 'A[B](https://example.org)')
    else:
        plan[0]['block']['paragraph']['rich_text'][0]['annotations'] = {change: 'red' if change == 'color' else True}
    delivered(n)
    assert mapped()['a']['block_id'] != old
    got = n.children(pid)[0]['paragraph']['rich_text']
    if change == 'link':
        assert [(x['plain_text'], x['href']) for x in got] == [('A', None), ('B', 'https://example.org')]
    else:
        assert got[0]['annotations'][change] == ('red' if change == 'color' else True)
    stable(n, pid, 0)


def test_channel_merged_rich_text_roundtrip_is_not_a_change(channel):
    n, plan = channel
    plan[:] = [text('a', '')]
    plan[0]['block']['paragraph']['rich_text'] = [
        {'type': 'text', 'text': {'content': '粗'}, 'annotations': {'bold': True}},
        {'type': 'text', 'text': {'content': '体'}, 'annotations': {'bold': True}}]
    pid = delivered(n)
    before = mapped()
    cut = len(n.requests)
    delivered(n)
    assert mapped() == before
    assert not [r for r in n.requests[cut:] if r[0] != 'GET' and not r[1].startswith('/pages/')]
    got = n.children(pid)[0]['paragraph']['rich_text']
    assert len(got) == 1 and got[0]['plain_text'] == '粗体' and got[0]['annotations']['bold']
    stable(n, pid, 0)


def test_channel_deletion_failure_leaves_only_one_replacement_segment(channel):
    n, plan = channel
    plan[:] = [text('a', '开头'), text('b', '旧B'), text('keep', '没变'), text('c', '旧C'), text('tail', '结尾')]
    pid = delivered(n)
    old = mapped()
    plan[1] = text('b', '新B')
    plan[3] = text('c', '新C')
    n.fail_delete = old['b']['block_id']
    assert delivery.deliver('s')['status'] == 'FAILED'
    assert [B.plain(b) for b in n.children(pid)] == ['开头', '旧B', '新B', '没变', '旧C', '结尾']
    assert mapped()['c']['block_id'] == old['c']['block_id']
    n.fail_delete = None
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 0)


def test_channel_nested_children_are_read_once_per_sync(channel):
    n, plan = channel
    pid = delivered(n)
    cut = len(n.calls)
    delivered(n)
    reads = [r[1] for r in n.calls[cut:] if r[0] == 'GET' and r[1].endswith('/children')]
    assert len(reads) == len(set(reads)) == 3  # page, runinfo, notes; no duplicate traversal for media readback
    stable(n, pid, 2)


def test_channel_retained_event_counts_only_generated_blocks(channel):
    n, plan = channel
    pid = delivered(n)
    n.kids[pid].append(n._block(B.block('paragraph', '用户的'), YOU))
    delivered(n)
    assert '保留 8 块' in db.q("SELECT message FROM events WHERE kind='delivery'")[-1]['message']
    stable(n, pid, 2)


def test_topic_prepend_uses_start_and_keeps_unchanged_first_block():
    from assetizer.notion.sync import Sync
    from test_topic_notion import RESEARCH
    n = FakeNotion()
    a = B.block('paragraph', '原首段')
    pid = n.create_page(RESEARCH, '专题', children=[a])['id']
    old = n.children(pid)[0]['id']
    new = [B.block('paragraph', f'新前缀 {i}') for i in range(205)] + [a]
    s = Sync(n, BOT)
    s.write(pid, new, n.children(pid))
    s.check(pid, new)
    assert n.children(pid)[-1]['id'] == old
    assert [B.plain(b) for b in n.children(pid)] == [f'新前缀 {i}' for i in range(205)] + ['原首段']
    assert not any(c[0] == 'DELETE' for c in n.calls)


@pytest.mark.parametrize('path,obsolete', [('docs/channel-page.md', 'YOURS_ON_PAGE'),
                                          ('web/src/components/channel/LockSheet.tsx', '旧页移到 Notion 归档')])
def test_channel_current_docs_and_lock_sheet_describe_in_place_sync(path, obsolete):
    from pathlib import Path
    root = Path(delivery.__file__).resolve().parents[2]
    assert obsolete not in (root / path).read_text()


def test_notion_client_sends_start_position_without_after():
    from assetizer.notion.client import Notion
    n = object.__new__(Notion)
    sent = []
    n._req = lambda method, path, **kw: sent.append((method, path, kw)) or {'results': []}
    n.append('page', [B.block('paragraph', '新首段')], position={'type': 'start'})
    body = sent[0][2]['json']
    assert body['position'] == {'type': 'start'} and 'after' not in body


def test_fake_notion_archive_and_response_contracts():
    from test_topic_notion import RESEARCH
    n = FakeNotion()
    pid = n.create_page(RESEARCH, '协议', children=[B.block('paragraph', x) for x in ('A', 'B', 'C')])['id']
    a, b, c = [x['id'] for x in n.children(pid)]
    got = n.append(pid, [B.block('paragraph', 'A1'), B.block('paragraph', 'A2')], after=a)['results']
    assert [B.plain(x) for x in got] == ['A1', 'A2', 'B', 'C']
    got = n.append(pid, [B.block('paragraph', '首')], position={'type': 'start'})['results']
    assert [B.plain(x) for x in got] == ['首', 'A', 'A1', 'A2', 'B', 'C']
    n.pages[pid]['archived'] = True
    assert n.page(pid)['in_trash'] is True
    for action, status in [(lambda: n.children(pid), 404), (lambda: n.append(pid, [B.block('paragraph', 'X')]), 400),
                           (lambda: n._req('DELETE', f'/blocks/{b}'), 400)]:
        with pytest.raises(NotionError) as error:
            action()
        assert error.value.status == status


def test_channel_mapped_own_database_recreates_trashed_page_once(channel):
    from assetizer.notion import mapping as M
    n, plan = channel
    t = now()
    db.exec("INSERT INTO channels(id,name,created_at,updated_at) VALUES ('mapped','映射频道',?,?)", t, t)
    db.exec("UPDATE sources SET channel_id='mapped'")
    M.save('mapped', {'database_id': OWN_DB, 'template': 'default', 'icon': 'none', 'properties': {
        '名称': {'name': '名称', 'type': 'title', 'source': 'video_title'},
        '来源': {'name': '来源', 'type': 'url', 'source': 'video_url'}}})
    original = n._req
    queries = []
    def query(method, path, *args, **kwargs):
        if method == 'POST' and path == f'/databases/{OWN_DB}/query':
            queries.append(deepcopy(kwargs['json']))
            assert kwargs['json']['filter'] == {'property': '来源', 'url': {'contains': 's'}}
            return {'results': [deepcopy(pg) for pg in n.pages.values()
                                if pg.get('parent', {}).get('database_id') == OWN_DB
                                and not pg.get('archived') and not pg.get('in_trash')
                                and 's' in (pg['properties'].get('来源', {}).get('url') or '')]}
        return original(method, path, *args, **kwargs)
    n._req = query
    old = delivered(n)
    n.pages[old]['archived'] = n.pages[old]['in_trash'] = True
    snapshot = deepcopy((n.pages[old], n.kids[old]))
    cut = len(n.requests)
    new = delivered(n)
    delivered(n)
    assert new != old and page(n) == new
    assert (n.pages[old], n.kids[old]) == snapshot
    assert len(queries) == 2
    assert len([c for c in n.calls if c[:2] == ('POST', '/pages')]) == 2
    assert not [r for r in n.requests[cut:] if r[1] in (f'/pages/{old}', f'/blocks/{old}/children') and r[0] != 'GET']
    assert n.upload_attempts == 4
    aligned(n, plan)


def test_channel_duplicate_append_receipt_ids_fail_then_converge(channel):
    n, plan = channel
    plan[:] = [text('a', '首段'), text('b', '旧B'), text('c', '旧C'), text('d', '尾段')]
    pid = delivered(n)
    plan[1:3] = [text('b', '新B'), text('c', '新C')]
    original = n.append
    def duplicate(*args, **kwargs):
        got = deepcopy(original(*args, **kwargs))
        assert len(got['results']) >= 2
        got['results'][1]['id'] = got['results'][0]['id']
        return got
    n.append = duplicate
    result = delivery.deliver('s')
    assert result['status'] == 'FAILED' and 'READBACK_MISMATCH' in result['error']
    n.append = original
    delivered(n)
    aligned(n, plan)
    stable(n, pid, 0)


@pytest.mark.parametrize('fault', ['delete', 'comment'])
def test_channel_historical_author_survives_all_keys_replaced(channel, fault):
    n, plan = channel
    plan[:] = [text(str(i), f'旧文{i}') for i in range(5)]
    pid = delivered(n)
    old = mapped()['2']['block_id']
    if fault == 'comment':
        comment = n.say(old, '请改正')
    else:
        n.fail_delete = old
    n.bot = 'replacement-integration'
    plan[:] = [text(str(i), f'新文{i}') for i in range(5)]
    result = delivery.deliver('s')
    assert result['status'] == ('FAILED' if fault == 'delete' else 'DELIVERED')
    assert old not in {r['block_id'] for r in mapped().values()}
    assert set(db.setting('notion_authors:s')) == {BOT, n.bot}
    # Reopen the connection: retries must not depend on the in-memory Sync instance.
    db._local.conn.close()
    db._local.conn = None
    n.fail_delete = None
    if fault == 'comment':
        comment['resolved'] = True
    delivered(n)
    assert old not in {b['id'] for b in n.children(pid)}
    aligned(n, plan)
    stable(n, pid, 0)


def test_channel_authors_persist_before_append_and_never_learn_unmapped_users(channel):
    n, plan = channel
    plan[:] = [text('a', '旧文')]
    pid = delivered(n)
    extras = [n._block(B.block('paragraph', '自己的笔记'), who) for who in (YOU, 'another-integration')]
    n.kids[pid].extend(extras)
    snapshot = deepcopy(extras)
    n.bot = 'replacement-integration'
    plan[:] = [text('a', '新文')]
    def persisted():
        assert set(db.setting('notion_authors:s')) == {BOT, n.bot}
    n.on_append = persisted
    delivered(n)
    assert n.kids[pid][-2:] == snapshot
    assert YOU not in db.setting('notion_authors:s') and 'another-integration' not in db.setting('notion_authors:s')
    aligned(n, plan, [b['id'] for b in extras])
    stable(n, pid, 0)


@pytest.mark.parametrize('reason', ['no-page', 'trash', '404', 'database'])
def test_channel_new_page_resets_recognized_authors(channel, reason):
    n, plan = channel
    plan[:] = [text('a', '一期')]
    old = delivered(n)
    db.set_setting('notion_authors:s', [BOT, 'historical-integration'])
    db.set_setting('notion_props_pending:s', {'名称': ['title', '旧页待确认']})
    n.bot = 'replacement-integration'
    if reason == 'no-page':
        db.exec("UPDATE sources SET notion_page_id=NULL, notion_url=NULL")
    elif reason == 'trash':
        n.pages[old]['archived'] = True
    elif reason == '404':
        del n.pages[old]
    else:
        other = 'new-database'
        n.dbs[other] = deepcopy(n.dbs[OWN_DB]) | {'id': other}
        db.set_setting('notion_database_id', other)
    snapshot = deepcopy(n.kids[old])
    def reset_before_append():
        assert db.setting('notion_authors:s') == [n.bot]
        assert not db.setting('notion_props_pending:s')
    n.on_append = reset_before_append
    new = delivered(n)
    assert new != old and n.kids[old] == snapshot
    assert db.setting('notion_authors:s') == [n.bot]
    assert len([c for c in n.calls if c[:2] == ('POST', '/pages')]) == 2
    assert n.upload_attempts == 0
    aligned(n, plan)


@pytest.mark.parametrize('retry,human,applied', [('r2', False, True), ('r3', False, True),
                                             ('r3', True, True), ('r3', False, False)])
def test_channel_property_pending_recovers_response_loss_without_claiming_user_edits(channel, retry, human, applied):
    n, plan = channel
    pid = delivered(n)
    for rid in ('r2', 'r3'):
        db.exec("INSERT INTO runs(id,source_id,production,summary,created_at) VALUES (?,'s','COMPLETE','{}',?)", rid, now())
    db.exec("UPDATE sources SET current_run_id='r2', title='标题 r2'")
    original = n._req
    def lose(method, path, *args, **kwargs):
        if method == 'PATCH' and path == f'/pages/{pid}':
            if applied:
                original(method, path, *args, **kwargs)
            raise NotionError(502, 'properties response lost')
        return original(method, path, *args, **kwargs)
    n._req = lose
    result = delivery.deliver('s')
    assert result['status'] == 'FAILED' and result['error'] == 'Notion 502: properties response lost'
    pending = db.setting('notion_props_pending:s')
    assert pending['名称'] == ['title', '标题 r2'] and pending['Run'] == ['rich_text', 'r2']
    n._req = original
    if human:
        n.pages[pid]['properties']['名称']['title'] = B.rich('用户的标题')
    db.exec('UPDATE sources SET current_run_id=?, title=?', retry, f'标题 {retry}')
    delivered(n)
    props = n.pages[pid]['properties']
    assert props['名称']['title'][0]['text']['content'] == ('用户的标题' if human else f'标题 {retry}')
    assert props['Run']['rich_text'][0]['text']['content'] == retry
    assert not db.setting('notion_props_pending:s')
    stable(n, pid, 2)


@pytest.mark.parametrize('kind', ['title', 'rich_text', 'select', 'date', 'datetime', 'url', 'number', 'checkbox'])
def test_channel_property_pending_normalizes_only_supported_values(channel, monkeypatch, kind):
    n, plan = channel
    # A moment written to the minute (as mapping.published_datetime does) may come back to the millisecond.
    spelled = {'2026-11-11T10:30+00:00': '2026-11-11T18:30:00.000+08:00'}
    datetime = kind == 'datetime'
    kind = 'date' if datetime else kind
    pid = delivered(n)
    # The schema has a real id distinct from its display name. Responses contain Notion-only metadata.
    values = {'title': [B.rich(x) for x in ('旧', '中', '新')],
              'rich_text': [B.rich(x) for x in ('旧', '中', '新')],
              'select': [{'name': x} for x in ('旧', '中', '新')],
              'date': [{'start': f'2026-11-{d}T10:30+00:00' if datetime else f'2026-11-{d}'} for d in ('10', '11', '12')],
              'url': ['https://example.org/' + x for x in ('old', 'middle', 'new')],
              'number': [1, 2, 3], 'checkbox': [False, True, False]}[kind]
    current = {'value': values[0]}
    monkeypatch.setattr(delivery, '_props', lambda *a: {'显示名': {kind: deepcopy(current['value'])}})
    n.pages[pid]['properties'] = {'显示名': {'id': 'prop-id', 'type': kind, kind: deepcopy(values[0])}}
    db.set_setting('notion_props:s', delivery._prop_hashes(n.pages[pid]['properties']))
    original = n._req
    def canonical(method, path, *args, **kwargs):
        if method == 'PATCH' and path == f'/pages/{pid}':
            put = kwargs['json']['properties']
            for name, value in put.items():
                value = deepcopy(value)
                v = value[kind]
                if kind in ('title', 'rich_text'):
                    value[kind] = n._rich(v)
                elif kind == 'select':
                    value[kind] = {'id': 'option-id', 'color': 'blue', **v}
                elif kind == 'date':
                    value[kind] = {'end': None, 'time_zone': None, **v, 'start': spelled.get(v['start'], v['start'])}
                n.pages[pid]['properties'][name] = {'id': 'prop-id', 'type': kind, **value}
            n.requests.append((method, path, deepcopy(kwargs['json'])))
            if current['value'] == values[1]:
                pending = db.setting('notion_props_pending:s')
                assert ('prop-id' in pending) == (kind != 'checkbox')
                raise NotionError(502, 'applied but response lost')
            return deepcopy(n.pages[pid])
        return original(method, path, *args, **kwargs)
    n._req = canonical
    current['value'] = values[1]
    assert delivery.deliver('s')['status'] == 'FAILED'
    current['value'] = values[2]
    delivered(n)
    got = n.pages[pid]['properties']['显示名'][kind]
    if kind in ('title', 'rich_text'):
        assert ''.join(t['plain_text'] for t in got) == '新'
    elif kind == 'select':
        assert got['name'] == '新'
    elif kind == 'date':
        assert got['start'] == ('2026-11-12T10:30+00:00' if datetime else '2026-11-12') and got['end'] is None
    else:
        assert got == (values[1] if kind == 'checkbox' else values[2])
    assert not db.setting('notion_props_pending:s')
    stable(n, pid, 2)


def test_channel_property_value_is_one_spelling_per_moment():
    v = lambda start, end=None: delivery._prop_value({'type': 'date', 'date': {'start': start, 'end': end}})
    assert v('2026-11-11T10:30+00:00') == v('2026-11-11T10:30:00.000+00:00') == v('2026-11-11T18:30:00.000+08:00') \
        == v('2026-11-11T10:30:00Z')
    assert v('2026-11-11T10:30+00:00') != v('2026-11-11T10:31+00:00')
    assert v('2026-11-11') != v('2026-11-11T00:00+00:00')       # a day is not its midnight
    assert v('2026-11-11T10:30') != v('2026-11-11T10:30+00:00')  # no offset: not the same moment
    assert v('2026-11-11', '2026-11-12') != v('2026-11-11')
    assert v('11月11日') is None                                  # not a date we can read: never ours
