"""专题存到 Notion (design §R8-1, §R8-12, topics/notion.py): a reading copy of the answer where the user picks, links
to the YouTube moments; written again, what someone else wrote, edited or commented on stays."""
import itertools
import json

import pytest

BOT, YOU = "bot-user", "you-user"
RESEARCH = "aaaaaaaa-0000-0000-0000-000000000001"   # a page
READING = "bbbbbbbb-0000-0000-0000-000000000002"    # a database of the user's
TOM_DB = "cccccccc-0000-0000-0000-000000000003"     # a channel's database
OWN_DB = "dddddddd-0000-0000-0000-000000000004"     # 课代表's default database


@pytest.fixture
def home(tmp_path, monkeypatch):
    import dataclasses
    from assetizer import config, db
    config.override(dataclasses.replace(config.load(), home=tmp_path))
    db._local.conn = None
    monkeypatch.setenv("ASSETIZER_PUBLIC_HOST", "assetizer.example.space")
    yield tmp_path
    db._local.conn = None


def _rules(blocks, depth=1):
    """What Notion refuses in a request: more than 100 blocks in a list, more than two levels of nesting, more than 100
    pieces of text in a block or 2000 characters in a piece. Returns how many blocks it has (1000 at most in all)."""
    assert len(blocks) <= 100, f"{len(blocks)} blocks in one list"
    n = 0
    for b in blocks:
        body = b.get(b["type"]) or {}
        for pieces in ([body["rich_text"]] if "rich_text" in body else []) + list(body.get("cells") or []):
            assert len(pieces) <= 100, f"{len(pieces)} pieces of text in a {b['type']}"
            assert all(len(x["text"]["content"]) <= 2000 for x in pieces)
        kids = body.get("children") or []
        assert not kids or depth < 3, "nested more than two levels in one request"
        n += 1 + _rules(kids, depth + 1)
    return n


class FakeNotion:
    """Pages, blocks, databases and comments in memory, as Notion keeps them: who made a block, who last edited it;
    a request it would refuse fails the test."""

    def __init__(self):
        self.ids = (f"{n:08x}-1111-1111-1111-{n:012x}" for n in itertools.count(100))
        self.pages: dict[str, dict] = {
            RESEARCH: {"id": RESEARCH, "parent": {"type": "workspace"}, "icon": {"type": "emoji", "emoji": "📚"},
                       "properties": {"title": {"id": "title", "type": "title", "title": [{"plain_text": "研究"}]}}},
            "eeeeeeee-0000-0000-0000-000000000005": {
                "id": "eeeeeeee-0000-0000-0000-000000000005", "parent": {"type": "database_id", "database_id": TOM_DB},
                "properties": {"Name": {"id": "title", "type": "title", "title": [{"plain_text": "汤姆 09/08"}]}}}}
        self.dbs = {
            READING: {"id": READING, "title": [{"plain_text": "阅读库"}], "properties": {
                "名字": {"id": "title", "type": "title"}, "期数": {"id": "n", "type": "number"},
                "专题页": {"id": "u", "type": "url"}, "标签": {"id": "t", "type": "multi_select"}}},
            TOM_DB: {"id": TOM_DB, "title": [{"plain_text": "知识领袖长期追踪库"}], "properties": {}},
            OWN_DB: {"id": OWN_DB, "title": [{"plain_text": "课代表"}], "properties": {}}}
        self.kids: dict[str, list[dict]] = {}
        self.comments: dict[str, list[dict]] = {}   # by the page or block they are on, oldest first
        self.can = {"read", "insert"}                # the integration's comment capabilities
        self.calls: list[tuple] = []
        self.down = None  # a status every call answers with
        self.bot = BOT
        self._t = itertools.count(1)

    def say(self, on, text, by=YOU, thread=None):
        """A comment on a page or a block: a new discussion, or a reply in `thread`."""
        cid = next(self.ids)
        c = {"id": cid, "discussion_id": thread or f"d-{cid}", "created_by": {"object": "user", "id": by},
             "created_time": f"2026-11-10T00:{next(self._t):02}:00.000Z",
             "rich_text": [{"type": "text", "text": {"content": text}, "plain_text": text}]}
        self.comments.setdefault(on, []).append(c)
        return c

    def _comment(self, c, on):
        """A comment as Notion gives it back (the ones a test put in by hand say less)."""
        kind = "page_id" if on in self.pages else "block_id"
        return {"object": "comment", "parent": {"type": kind, kind: on}, "discussion_id": f"d-{c['id']}",
                "created_time": "2026-11-10T00:00:00.000Z", "rich_text": [],
                **{k: v for k, v in c.items() if k != "resolved"}}

    def _err(self, status, body="{}"):
        from assetizer.notion.client import NotionError
        raise NotionError(status, body)

    @staticmethod
    def _rich(pieces):
        """Notion fills defaults and coalesces adjacent runs with the same kind, link and annotations."""
        out = []
        for piece in pieces:
            x = json.loads(json.dumps(piece))
            x.setdefault("type", "text")
            x["plain_text"] = x.get("plain_text", (x.get("text") or {}).get("content", (x.get("equation") or {}).get("expression", "")))
            x["href"] = x.get("href") or ((x.get("text") or {}).get("link") or {}).get("url")
            x["annotations"] = {"bold": False, "italic": False, "strikethrough": False, "underline": False,
                                "code": False, "color": "default", **x.get("annotations", {})}
            if out and all(out[-1][k] == x[k] for k in ("type", "href", "annotations")):
                out[-1]["plain_text"] += x["plain_text"]
                if x["type"] == "text":
                    out[-1]["text"]["content"] = out[-1]["plain_text"]
            else:
                out.append(x)
        return out

    def _page_of(self, bid):
        seen = set()
        while bid not in self.pages and bid not in seen:
            seen.add(bid)
            bid = next((parent for parent, bs in self.kids.items() if any(b["id"] == bid for b in bs)), None)
        return self.pages.get(bid, {})

    def _live(self, bid, reading=False):
        pg = self._page_of(bid)
        if not pg and bid not in self.dbs:
            self._err(404, '{"message": "Could not find block"}')
        if pg.get("archived") or pg.get("in_trash"):
            self._err(404 if reading else 400, '{"message": "Can\'t edit block that is archived or has an archived ancestor."}')

    def _block(self, b, by=None):
        by = by or self.bot
        bid = next(self.ids)
        kids = (b.get(b["type"]) or {}).pop("children", None)
        nb = {**b, "id": bid, "created_by": {"id": by}, "last_edited_by": {"id": by}, "has_children": bool(kids)}
        if kids:
            self.kids[bid] = [self._block(k, by) for k in kids]
        return nb

    def _read_block(self, b):
        # The fixture keeps request bodies for direct test inspection; API responses are canonical Notion values.
        b = json.loads(json.dumps(b))
        body = b.get(b["type"]) or {}
        for field in ("rich_text", "caption"):
            if field in body:
                body[field] = self._rich(body[field])
        if "cells" in body:
            body["cells"] = [self._rich(cell) for cell in body["cells"]]
        b["has_children"] = bool(self.kids.get(b["id"]))
        return b

    def me(self, **kw):
        self.calls.append(("GET", "/users/me"))
        return {"id": self.bot}

    def search(self, query="", kind="database", page_size=20):
        if self.down:
            self._err(self.down)
        src = self.dbs if kind == "database" else self.pages
        return [json.loads(json.dumps(o)) for o in src.values() if not o.get("archived")]

    def page(self, pid):
        self.calls.append(("GET", f"/pages/{pid}"))
        if self.down:
            self._err(self.down)
        if pid not in self.pages:
            self._err(404, '{"message": "Could not find page"}')
        got = json.loads(json.dumps(self.pages[pid]))
        got.setdefault("in_trash", bool(got.get("archived")))
        return got

    def create_page(self, parent, title, icon=None, children=None):
        self.calls.append(("POST", "/pages", parent))
        if self.down:
            self._err(self.down)
        self._live(parent)
        if self.pages.get(parent, {}).get("archived"):
            self._err(400, '{"message": "Can\'t edit block that is archived."}')
        pid = next(self.ids)
        self.pages[pid] = {"id": pid, "parent": {"type": "page_id", "page_id": parent}, "created_by": {"id": self.bot},
                           "properties": {"title": {"id": "title", "type": "title", "title": [{"plain_text": title}]}}}
        assert _rules(children or []) <= 1000
        self.kids[pid] = [self._block(b) for b in json.loads(json.dumps(children or []))]
        return {"id": pid}

    def children(self, bid):
        self.calls.append(("GET", f"/blocks/{bid}/children"))
        self._live(bid, reading=True)
        return [self._read_block(b) for b in self.kids.get(bid, [])]

    def append(self, bid, children, after=None, *, position=None):
        self.calls.append(("PATCH", f"/blocks/{bid}/children", len(children), after))
        if self.down:
            self._err(self.down)
        self._live(bid)
        assert _rules(children) <= 1000
        made = [self._block(b) for b in json.loads(json.dumps(children))]
        kids = self.kids.setdefault(bid, [])
        if after or position:
            at = next(i + 1 for i, b in enumerate(kids) if b["id"] == after) if after else 0
            assert after or position == {"type": "start"}
            kids[at:at] = made
            return {"results": [self._read_block(b) for b in kids[at:]]}
        else:
            kids.extend(made)
        return {"results": [self._read_block(b) for b in made]}

    def database(self, page_id, title, properties):
        """An inline database: its block goes at the end of the page."""
        self.calls.append(("POST", "/databases", page_id))
        self._live(page_id)
        did = next(self.ids)
        self.dbs[did] = {"id": did, "title": [{"plain_text": title}], "created_by": {"id": BOT},
                         "parent": {"type": "page_id", "page_id": page_id},
                         "properties": {k: {"id": "title" if "title" in v else f"p{i}", "type": next(iter(v)), **v}
                                        for i, (k, v) in enumerate(properties.items())}}
        self.kids.setdefault(page_id, []).append({"id": did, "type": "child_database", "child_database": {"title": title},
                                                  "created_by": {"id": BOT}, "last_edited_by": {"id": BOT}})
        return {"id": did}

    def rows(self, did):
        self.calls.append(("POST", f"/databases/{did}/query"))
        return [json.loads(json.dumps(p)) for p in self.pages.values()
                if p["parent"].get("database_id") == did and not p.get("archived")]

    def row(self, did, no):
        """The table's row of a video, by its 期."""
        return next(p for p in self.pages.values() if p["parent"].get("database_id") == did and p["properties"]["期"]["number"] == no)

    def _req(self, method, path, idempotent=None, **kw):
        self.calls.append((method, path))
        if self.down:
            self._err(self.down)
        body = kw.get("json") or {}
        if method == "GET" and path.startswith("/databases/"):
            d = self.dbs.get(path.split("/")[2])
            return json.loads(json.dumps(d)) if d else self._err(404)
        if method == "GET" and path.startswith("/blocks/"):
            bid = path.split("/")[2]
            self._live(bid, reading=True)
            return self._read_block(next(b for kids in self.kids.values() for b in kids if b["id"] == bid))
        if method == "POST" and path == "/pages":
            self._live(body["parent"]["database_id"])
            pid = next(self.ids)
            schema = self.dbs[body["parent"]["database_id"]]["properties"]
            props = {k: {"id": schema[k]["id"], "type": schema[k]["type"], **v} for k, v in body["properties"].items()}
            self.pages[pid] = {"id": pid, "parent": {"type": "database_id", **body["parent"]}, "created_by": {"id": self.bot},
                               "properties": props}
            assert _rules(body.get("children") or []) <= 1000
            self.kids[pid] = [self._block(b) for b in json.loads(json.dumps(body.get("children") or []))]
            return json.loads(json.dumps(self.pages[pid]))
        if method == "PATCH" and path.startswith("/pages/"):
            self._live(path.split("/")[2])
            pg = self.pages[path.split("/")[2]]
            for k, v in body.get("properties", {}).items():
                pg["properties"][k] = {**pg["properties"].get(k, {"id": k}), **v}
            return json.loads(json.dumps(pg))
        if method == "DELETE" and path.startswith("/blocks/"):
            bid = path.split("/")[2]
            self._live(bid)
            for k in self.kids.values():
                k[:] = [b for b in k if b["id"] != bid]
            return {}
        if method == "GET" and path.startswith("/comments"):
            if "read" not in self.can:
                self._err(403, '{"message": "Insufficient permissions for this endpoint."}')
            q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&"))
            on, at, size = q["block_id"], int(q.get("start_cursor") or 0), int(q.get("page_size") or 100)
            assert size <= 100
            got = [self._comment(c, on) for c in self.comments.get(on, []) if not c.get("resolved")]  # never the resolved
            more = at + size < len(got)
            return {"object": "list", "results": got[at:at + size], "has_more": more, "next_cursor": str(at + size) if more else None}
        if method == "POST" and path == "/comments":
            if "insert" not in self.can:
                self._err(403, '{"message": "Insufficient permissions for this endpoint."}')
            assert set(body) == {"discussion_id", "rich_text"}, body  # a reply in a thread
            text = body["rich_text"]
            assert len(text) <= 100 and all(len(x["text"]["content"]) <= 2000 for x in text)
            on = next((k for k, cs in self.comments.items() if any(self._comment(c, k)["discussion_id"] == body["discussion_id"]
                                                                  for c in cs)), None)
            if on is None:
                self._err(404, '{"message": "Could not find discussion"}')
            c = self.say(on, "".join(x["text"]["content"] for x in text), BOT, body["discussion_id"])
            return self._comment(c, on)
        raise AssertionError(f"unexpected Notion call {method} {path}")


@pytest.fixture
def fake(home, monkeypatch):
    from assetizer import db
    from assetizer.topics import comments as TC, notion as TN, work_dir
    from assetizer.util import now
    n = FakeNotion()
    monkeypatch.setattr(TN, "client", lambda: n)
    monkeypatch.setattr(TC, "PAUSE", 0)
    t = now()
    db.exec("INSERT INTO channels(id, name, youtube_channel_id, mode, created_at, updated_at) VALUES ('tom','交易员汤姆','UCt','TRACKED',?,?)", t, t)
    db.set_setting("notion:tom", {"database_id": TOM_DB.replace("-", ""), "is_test_copy": False})
    db.set_setting("notion_database_id", OWN_DB)
    db.exec("INSERT INTO topics(question, created_at, updated_at) VALUES ('光固化怎么加支撑', ?, ?)", t, t)
    for no, vid, title in [(1, "aaaaaaaaaaa", "加支撑"), (2, "bbbbbbbbbbb", "支撑密度"), (3, "ccccccccccc", "没读的")]:
        db.exec("INSERT INTO videos(id, url, title, channel, created_at) VALUES (?,?,?,'Lab',?)", vid, f"https://youtu.be/{vid}", title, t)
        db.exec("INSERT INTO topic_videos(topic_id, video_id, no, state, rev, done_rev, gist, added_at) VALUES (1,?,?,?,1,?,?,?)",
                vid, no, "DONE" if no < 3 else "QUEUED", 1 if no < 3 else None, f"要点{no}", t)
    d = work_dir(1, 1, "bbbbbbbbbbb")
    d.mkdir(parents=True)
    (d / "looks.json").write_text(json.dumps([{"id": "L03", "start": "01:02:03"}]))
    d = work_dir(1, 1, "aaaaaaaaaaa")
    d.mkdir(parents=True)
    (d / "notes.md").write_text("## 支撑密度\n\n- 外圈 80% [12:34 说]\n  - 薄壁另加 [13:02 帧]\n- 内圈 50%\n\n"
                                "| 参数 | 值 |\n| --- | --- |\n| 密度 | 80% |\n")
    synth = {"answer": "支撑密度设到 80% [V1 12:34 说][V2 L03]。\n\n薄壁要另加 [V2]。",
             "first": [{"v": 2, "why": "参数全 [V2 00:15.5 帧]"}],
             "sections": [{"title": "密度", "body": "- 80% [V1 12:34 说，13:02 帧]\n- 不是 [标题] 里那个"}],
             "videos": [{"v": 1, "adds": "讲了密度"}]}
    sig = json.dumps([1, [["aaaaaaaaaaa", 1, t], ["bbbbbbbbbbb", 1, t]]])
    db.exec("UPDATE topics SET synth=?, synth_of=?, synth_at='2026-11-09T02:00:00+00:00', synth_state='DONE' WHERE id=1",
            json.dumps(synth, ensure_ascii=False), sig)
    return n


def _rich(b):
    return b[b["type"]].get("rich_text") or []


def _said(b):
    return "".join(x["text"]["content"] for x in _rich(b))


def _links(b):
    return [(x["text"]["content"], x["text"]["link"]["url"]) for x in _rich(b) if x["text"].get("link")]


def _kinds(n, pid):
    return [b["type"] for b in n.kids[pid]]


def _mine(n, pid, text, by=YOU):
    """A paragraph someone wrote on the page, right after the block `pid` (a block id) — or at the end of a page."""
    b = {"id": f"yours-{text}", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
         "created_by": {"id": by}, "last_edited_by": {"id": by}}
    for kids in n.kids.values():
        at = next((i for i, x in enumerate(kids) if x["id"] == pid), None)
        if at is not None:
            kids.insert(at + 1, b)
            return b
    n.kids[pid].append(b)
    return b


def test_a_topic_goes_where_it_is_picked_with_links_to_the_moments(fake):
    from fastapi.testclient import TestClient
    from assetizer import db
    from assetizer.topics import notion as TN
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    # what may be picked: pages and the user's databases, not a channel's database, 课代表's own, or their rows
    ts = c.get("/api/topics/notion/targets").json()["targets"]
    assert [(t["kind"], t["title"]) for t in ts] == [("page", "研究"), ("database", "阅读库")]
    said = c.post("/api/topics/notion/resolve", json={"link": f"https://www.notion.so/x-{TOM_DB.replace('-', '')}"})
    assert said.status_code == 422 and said.json()["detail"] == "这是「交易员汤姆」的频道库"
    said = c.post("/api/topics/notion/resolve", json={"link": "eeeeeeee000000000000000000000005"})
    assert said.json()["detail"] == "这是「交易员汤姆」的频道库"  # a row of it
    assert c.post("/api/topics/notion/resolve", json={"link": OWN_DB}).json()["detail"] == "这是课代表的默认库"
    research = c.post("/api/topics/notion/resolve", json={"link": f"https://www.notion.so/研究-{RESEARCH.replace('-', '')}"}).json()
    assert research == {"kind": "page", "id": RESEARCH, "title": "研究", "icon": "📚"}
    assert c.post("/api/topics/1/notion", json={"target": {**research, "kind": "database", "id": TOM_DB}}).status_code == 422

    page = c.post("/api/topics/1/notion", json={"target": research}).json()
    assert page["notion"]["target"]["title"] == "研究" and page["notion"]["state"] is None
    assert c.post("/api/topics/1/notion/write", json={"how": "write"}).json()["notion"]["state"] == "QUEUED"
    assert TN.due() == [1]
    out = TN.write(1)
    assert out["state"] == "DONE", out
    t = db.one("SELECT * FROM topics")
    pid = t["notion_page_id"]
    assert fake.pages[pid]["parent"]["page_id"] == RESEARCH and t["notion_at"] == t["synth_at"]
    # the conclusion, how many of the videos it rests on, the rest of the answer, 先看, the sections, the 来源 table
    assert _kinds(fake, pid) == ["callout", "paragraph", "paragraph", "heading_2", "numbered_list_item", "heading_2",
                                 "bulleted_list_item", "bulleted_list_item", "child_database", "divider", "paragraph"]
    blocks = fake.kids[pid]
    assert _said(blocks[0]) == "支撑密度设到 80% V1 12:34 · V2 1:02:03。"
    assert _links(blocks[0]) == [("V1 12:34", "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=754s"),
                                 ("V2 1:02:03", "https://www.youtube.com/watch?v=bbbbbbbbbbb&t=3723s")]
    assert all(x.get("annotations") == {"color": "gray"} for x in _rich(blocks[0]) if x["text"].get("link"))
    assert _said(blocks[1]) == "基于 2/3 期"
    assert _links(blocks[2]) == [("V2", "https://www.youtube.com/watch?v=bbbbbbbbbbb")]
    assert _said(blocks[6]) == "80% V1 12:34 · V1 13:02"
    assert "[标题]" in _said(blocks[7])  # a bracket that cites nothing stays
    foot = _rich(blocks[-1])
    assert "".join(x["text"]["content"] for x in foot) == "2 期 · 11月9日综合 · 专题页 ↗"
    assert foot[-1]["text"]["link"]["url"] == "https://assetizer.example.space/topics/1"
    # the 来源 table: a row for each video read (not the one still to read), its notes inside, citing its own moments
    table = t["notion_db"]
    assert blocks[8]["id"] == table and fake.dbs[table]["title"][0]["plain_text"] == "来源"
    assert list(fake.dbs[table]["properties"]) == ["视频", "期", "频道", "发布日期", "立场", "要点", "利益相关", "转述", "状态", "链接", "备注"]
    one, two = fake.row(table, 1), fake.row(table, 2)
    assert len(fake.rows(table)) == 2
    assert one["properties"]["视频"]["title"][0]["text"]["content"] == "加支撑" and one["properties"]["要点"]["rich_text"][0]["text"]["content"] == "讲了密度"
    assert two["properties"]["要点"]["rich_text"][0]["text"]["content"] == "要点2"  # no adds: its gist
    assert one["properties"]["状态"]["select"]["name"] == "算" and one["properties"]["频道"]["select"]["name"] == "Lab"
    assert one["properties"]["链接"]["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    notes = fake.kids[one["id"]]
    assert [b["type"] for b in notes] == ["heading_2", "bulleted_list_item", "bulleted_list_item", "table"]
    assert _links(notes[1]) == [("12:34", "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=754s")]
    inner = fake.kids[notes[1]["id"]]  # the list as it was nested
    assert [_said(b) for b in inner] == ["薄壁另加 13:02"] and _links(inner[0])[0][1].endswith("&t=782s")
    assert len(fake.kids[notes[3]["id"]]) == 2
    assert [_said(b) for b in fake.kids[two["id"]]] == ["要点2"]
    view = c.get("/api/topics/1").json()["notion"]
    assert view["url"] == f"https://www.notion.so/{pid.replace('-', '')}" and not view["stale"]

    # put together again: the same page; what did not change stays as it was, the conclusion is replaced
    keep = [b["id"] for b in fake.kids[pid][3:]]
    db.exec("UPDATE topics SET synth=json_set(synth, '$.answer', '换了说法 [V2]'), synth_at='2026-11-10T02:00:00+00:00'")
    assert c.get("/api/topics/1").json()["notion"]["stale"]
    fake.calls.clear()
    assert TN.write(1)["state"] == "DONE"
    assert db.one("SELECT notion_page_id FROM topics")["notion_page_id"] == pid and len(fake.pages) == 5
    assert _kinds(fake, pid) == ["callout", "paragraph", "heading_2", "numbered_list_item", "heading_2", "bulleted_list_item",
                                 "bulleted_list_item", "child_database", "divider", "paragraph"]
    assert _said(fake.kids[pid][0]) == "换了说法 V2"
    assert [b["id"] for b in fake.kids[pid][2:-1]] == keep[:-1]
    assert "11月10日综合" in _said(fake.kids[pid][-1])
    rows = {one["id"], two["id"]}
    assert not [x for x in fake.calls if x[0] in ("POST", "PATCH") and "/query" not in x[1] and x[1] != f"/blocks/{pid}/children"
                or any(r in x[1] for r in rows)]  # the rows, and the page's title, as they were
    # the pages 课代表 wrote and their tables are not places
    ts = c.get("/api/topics/notion/targets").json()["targets"]
    assert [(t["title"], t.get("recent", False)) for t in ts] == [("研究", True), ("阅读库", False)]
    assert c.post("/api/topics/notion/resolve", json={"link": table}).json()["detail"] == "这是专题「光固化怎么加支撑」的来源表"


def test_what_you_write_in_notion_stays_where_you_put_it(fake):
    """Written again, only what 课代表 wrote and nobody touched is replaced."""
    from assetizer import db
    from assetizer.topics import notion as TN, work_dir
    db.exec("UPDATE topics SET notion_target=?", json.dumps({"kind": "page", "id": RESEARCH, "title": "研究"}))
    assert TN.write(1)["state"] == "DONE"
    t = db.one("SELECT * FROM topics")
    pid, table = t["notion_page_id"], t["notion_db"]
    page = fake.kids[pid]
    first, second, seen = page[6], page[7], page[4]
    _mine(fake, first["id"], "我的批注")                 # between the section's two lines
    _mine(fake, pid, "页尾我的总结")                      # under everything
    fake.comments[second["id"]] = [{"id": "c1", "created_by": {"id": YOU}}]  # a comment on a line about to change
    fake.comments[first["id"]] = [{"id": "c2", "created_by": {"id": YOU}}]   # and on one that stays
    seen["last_edited_by"] = {"id": YOU}                 # the 先看 line, edited
    row = fake.row(table, 1)
    _mine(fake, row["id"], "这期我的看法")
    row["properties"]["要点"]["rich_text"] = [{"type": "text", "text": {"content": "我改的要点"}}]

    synth = json.loads(db.one("SELECT synth FROM topics")["synth"])
    synth["sections"][0]["body"] = "- 80% [V1 12:34 说，13:02 帧]\n- 改了的第二条\n- 新加的第三条"
    synth["videos"] = [{"v": 1, "adds": "讲了密度和薄壁"}]
    db.exec("UPDATE topics SET synth=?, synth_at='2026-11-10T02:00:00+00:00'", json.dumps(synth, ensure_ascii=False))
    (work_dir(1, 1, "aaaaaaaaaaa") / "notes.md").write_text("## 支撑密度\n\n- 外圈 85% [12:34 说]\n- 内圈 50%\n")
    out = TN.write(1)
    assert out["state"] == "DONE", out
    said = [(b["id"].startswith("yours-") and "你：" or "") + _said(b) for b in fake.kids[pid]]
    assert said[6:12] == ["密度", "80% V1 12:34 · V1 13:02", "改了的第二条", "新加的第三条", "你：我的批注", "不是 [标题] 里那个"]
    assert fake.kids[pid][7]["id"] == first["id"]          # unchanged, its comment with it
    assert fake.kids[pid][11]["id"] == second["id"]        # changed, but commented on: kept as it was, the new ones before it
    assert said[-4:] == ["", "", "2 期 · 11月10日综合 · 专题页 ↗", "你：页尾我的总结"]
    assert [b["id"] for b in fake.kids[pid] if b["type"] == "numbered_list_item"][1] == seen["id"]  # edited: yours, the new one before it
    notes = [(b["id"].startswith("yours-") and "你：" or "") + _said(b) for b in fake.kids[row["id"]]]
    assert notes == ["支撑密度", "外圈 85% 12:34", "内圈 50%", "你：这期我的看法"]
    assert row["properties"]["要点"]["rich_text"][0]["text"]["content"] == "我改的要点"
    assert db.one("SELECT notion_state FROM topics")["notion_state"] == "DONE"
    # and again: what you changed stays yours
    synth["videos"] = [{"v": 1, "adds": "又改了"}]
    db.exec("UPDATE topics SET synth=?", json.dumps(synth, ensure_ascii=False))
    assert TN.write(1)["state"] == "DONE"
    assert fake.row(table, 1)["properties"]["要点"]["rich_text"][0]["text"]["content"] == "我改的要点"
    assert sum(1 for b in fake.kids[pid] if b["id"].startswith("yours-")) == 2


def test_a_long_answer_goes_in_whole_and_a_short_read_back_is_told(fake, monkeypatch):
    """Past 100 blocks, lists nested past what a request takes, a table past 100 rows, a line past 100 pieces."""
    from assetizer import db
    from assetizer.topics import notion as TN
    rows = "\n".join(f"| 第{i}行 | {i} |" for i in range(230))
    deep = "- 一层\n  - 二层\n    - 三层\n      - 四层\n  - 二层又一条"
    many = "".join(f"第{i}处 [V1 00:{i % 60:02} 说] **粗{i}** " for i in range(60))
    lines = "\n".join(f"- 第 {i} 条 [V2]" for i in range(120))
    synth = json.loads(db.one("SELECT synth FROM topics")["synth"])
    synth["sections"] = [{"title": "长", "body": f"{deep}\n\n| 列 | 数 |\n| --- | --- |\n{rows}\n\n{many}\n\n{lines}"}]
    db.exec("UPDATE topics SET synth=?, notion_target=?", json.dumps(synth, ensure_ascii=False),
            json.dumps({"kind": "page", "id": RESEARCH, "title": "研究"}))
    out = TN.write(1)
    assert out["state"] == "DONE", out
    pid = db.one("SELECT notion_page_id FROM topics")["notion_page_id"]
    page = fake.kids[pid]
    at = next(i for i, b in enumerate(page) if _said(b) == "长")
    sec = page[at + 1:]
    assert _said(sec[0]) == "一层" and [_said(b) for b in fake.kids[sec[0]["id"]]] == ["二层", "二层又一条"]
    two = fake.kids[sec[0]["id"]][0]
    assert [_said(b) for b in fake.kids[two["id"]]] == ["三层", "四层"]  # the fourth level joins the third
    tables = [b for b in sec if b["type"] == "table"]
    assert [len(fake.kids[b["id"]]) for b in tables] == [100, 100, 33]  # each under its header
    assert all(fake.kids[b["id"]][0]["table_row"]["cells"][0][0]["text"]["content"] == "列" for b in tables)
    long = [b for b in sec if b["type"] == "paragraph" and "处" in _said(b)]
    assert len(long) > 1 and "".join(_said(b) for b in long).count("处") == 60  # on in the next blocks, nothing cut
    items = [b for b in sec if b["type"] == "bulleted_list_item" and _said(b).startswith("第 ")]
    assert [_said(b) for b in items] == [f"第 {i} 条 V2" for i in range(120)]  # in order, over several requests
    assert len(page) > 100
    # a block Notion lost on the way is told, not taken for written
    real = fake.append

    def lossy(bid, children, after=None):
        got = real(bid, children, after)
        fake.kids[bid] = [b for b in fake.kids[bid] if b["id"] != got["results"][-1]["id"]]
        return got
    monkeypatch.setattr(fake, "append", lossy)
    db.exec("UPDATE topics SET synth=json_set(synth, '$.answer', '再来一版')")
    out = TN.write(1)
    assert out["state"] == "FAILED" and out["error"]["message"].startswith("Notion 读回 ")


def test_an_answer_of_claims_reads_in_notion_as_on_its_page(fake):
    """The disputes under the conclusion, the claims by kind, the predictions, what was settled, the 来源 table with
    each video's stance and stakes, how the channels' predictions went, the versions."""
    from assetizer import db
    from assetizer.topics import notion as TN
    from assetizer.util import now
    for vid, ch, day in (("aaaaaaaaaaa", "宏观札记", "20250612"), ("bbbbbbbbbbb", "交易员阿凯", "20251020"), ("ccccccccccc", "财经早班车", "20251102")):
        db.exec("UPDATE videos SET channel=?, published_at=? WHERE id=?", ch, day, vid)
    db.exec("UPDATE topic_videos SET state='DONE', done_rev=1, excluded=1, excluded_why='转述别人' WHERE no=3")
    synth = {
        "answer": "**会降，但节奏有分歧** [V1 12:34 说]。",
        "certainty": {"level": "中", "why": "两边各一个独立来源"},
        "claims": [
            {"id": "C1", "type": "事实", "text": "CPI 2.9% [V2 L03]", "v": [2], "independent": 1, "certainty": "中", "why": "只有一期", "status": "现行"},
            {"id": "C2", "type": "解读", "text": "降息会慢 [V1 12:34 说]", "v": [1], "independent": 1, "certainty": "低", "why": "一个来源", "status": "现行"},
            {"id": "C3", "type": "解读", "text": "降息会快 [V2]", "v": [2], "independent": 1, "certainty": "低", "status": "现行"},
            {"id": "C4", "type": "预测", "text": "明年上半年降两次 [V2 00:15.5 帧]", "v": [2], "independent": 1, "certainty": "低",
             "status": "现行", "due": "2026-06-30", "condition": "6 月底前累计降 50bp"},
            {"id": "C5", "type": "预测", "text": "年底前再降一次 [V1]", "v": [1], "independent": 1, "status": "应验", "by": "C1", "due": "2025-12-31"},
            {"id": "C6", "type": "预测", "text": "11 月就会降 [V2]", "v": [2], "independent": 1, "status": "现行", "due": "2025-11-30"}],
        "sections": [{"title": "节奏", "claims": ["C1", "C2", "C3", "C4"]}, {"title": "美联储内部", "claims": [], "note": "这批视频都没讲"}],
        "disputes": [{"id": "D1", "about": "快还是慢", "claims": ["C2", "C3"], "decide": "12 月非农", "lean": "不下倾向"}],
        "sources": [{"v": 1, "stance": "看慢"}, {"v": 2, "stance": "看快", "interests": "卖课", "relays": "某投行报告"}],
        "balance": "两期各占一边，缺看空的来源", "first": [], "videos": [{"v": 1, "adds": "讲了密度"}],
        "changes": ["C5 应验", "确定性从低升到中"], "answer_changed": True}
    db.exec("""UPDATE topics SET synth=?, marks=?, notion_target=?, archived_at='2026-11-09T02:00:00+00:00', archived_how='应验',
               archived_why='降完了'""", json.dumps(synth, ensure_ascii=False),
            json.dumps({"C6": {"status": "落空", "why": "11 月没开会", "at": now()}}, ensure_ascii=False),
            json.dumps({"kind": "page", "id": RESEARCH, "title": "研究"}))
    for n, at in ((1, "2026-06-20T10:00:00+00:00"), (2, "2026-11-03T10:00:00+00:00")):
        db.exec("INSERT INTO topic_versions(topic_id, n, at, read, total, synth) VALUES (1,?,?,2,?,?)", n, at, n + 1,
                json.dumps({**synth, "changes": [] if n == 1 else synth["changes"], "certainty": {"level": "低" if n == 1 else "中"}}, ensure_ascii=False))
    out = TN.write(1)
    assert out["state"] == "DONE", out
    t = db.one("SELECT * FROM topics")
    page = fake.kids[t["notion_page_id"]]
    assert [b["type"] for b in page] == [
        "callout", "paragraph",
        "heading_2", "heading_3", "bulleted_list_item", "bulleted_list_item", "paragraph", "paragraph",
        "heading_2", "bulleted_list_item", "bulleted_list_item", "bulleted_list_item", "bulleted_list_item",
        "heading_2", "paragraph",
        "heading_2", "bulleted_list_item",
        "heading_2", "bulleted_list_item", "bulleted_list_item",
        "child_database", "paragraph", "paragraph", "toggle", "divider", "paragraph"]
    said = [_said(b) for b in page]
    assert said[0] == "已归档 · 应验 · 11月9日　降完了\n会降，但节奏有分歧 V1 12:34。\n确定性 中 · 两边各一个独立来源"
    assert said[1] == "本版：C5 应验；确定性从低升到中"
    assert said[2:8] == ["分歧", "快还是慢", "V1 · 宏观札记 · 2025-06-12　降息会慢 V1 12:34", "V2 · 交易员阿凯 · 2025-10-20　降息会快 V2",
                         "能判定　12 月非农", "倾向　不下倾向"]
    assert said[8:13] == ["节奏", "事实　CPI 2.9% V2 1:02:03　中：只有一期", "解读　降息会慢 V1 12:34　低：一个来源 · 见分歧",
                          "解读　降息会快 V2　低 · 见分歧", "预测　明年上半年降两次 V2 00:15　低 · 到期 2026-06-30 · 怎么算应验：6 月底前累计降 50bp"]
    assert said[13:15] == ["美联储内部", "这批视频都没讲"]
    assert said[15:17] == ["待验证的预测", "2026-06-30　明年上半年降两次　V2 · 交易员阿凯 · 2025-10-20"]
    assert said[17:20] == ["已结算", "应验　年底前再降一次 V1　V1 · 宏观札记 · 2025-06-12 · 依据：CPI 2.9%",
                           "落空　11 月就会降 V2　V2 · 交易员阿凯 · 2025-10-20 · 你标的：11 月没开会"]
    assert said[21:23] == ["两期各占一边，缺看空的来源", "预测战绩　宏观札记 1 应验 · 0 落空　交易员阿凯 0 应验 · 1 落空"]
    assert said[23] == "2 个版本" and [_said(b) for b in fake.kids[page[23]["id"]]] == [
        "第 2 版 · 11月3日 · 2/3 期 · 确定性 中　C5 应验；确定性从低升到中", "第 1 版 · 6月20日 · 2/2 期 · 确定性 低"]
    table = t["notion_db"]
    p1, p2, p3 = (fake.row(table, i)["properties"] for i in (1, 2, 3))
    assert (p1["立场"]["rich_text"][0]["text"]["content"], p1["发布日期"]["date"]["start"]) == ("看慢", "2025-06-12")
    assert [p2[k]["rich_text"][0]["text"]["content"] for k in ("立场", "利益相关", "转述")] == ["看快", "卖课", "某投行报告"]
    assert [p["状态"]["select"]["name"] for p in (p1, p2, p3)] == ["算", "算", "不算 · 转述别人"]  # and why
    # taken back out of the archive and a video taken out of the topic: the page says so, its row is kept as 移出
    db.exec("UPDATE topics SET archived_at=NULL, archived_how=NULL, archived_why=NULL")
    db.exec("DELETE FROM topic_videos WHERE no=3")
    assert TN.write(1)["state"] == "DONE"
    assert _said(fake.kids[t["notion_page_id"]][0]).startswith("会降")
    assert fake.row(table, 3)["properties"]["状态"]["select"]["name"] == "移出"


def test_a_database_place_gets_the_title_and_its_matching_columns(fake):
    from assetizer import db
    from assetizer.topics import notion as TN
    db.exec("UPDATE topics SET notion_target=?", json.dumps({"kind": "database", "id": READING, "title": "阅读库"}))
    assert TN.write(1)["state"] == "DONE"
    pg = fake.pages[db.one("SELECT notion_page_id FROM topics")["notion_page_id"]]
    p = pg["properties"]
    assert p["名字"]["title"][0]["text"]["content"] == "光固化怎么加支撑" and p["期数"]["number"] == 2
    assert p["专题页"]["url"] == "https://assetizer.example.space/topics/1" and "标签" not in p
    # the user changed 期数 by hand: kept when it is written again (and that alone does not hold the page)
    p["期数"] = {**p["期数"], "number": 99}
    db.exec("UPDATE topics SET question='光固化怎么加支撑才稳'")
    assert TN.write(1)["state"] == "DONE"
    assert p["期数"]["number"] == 99 and p["名字"]["title"][0]["text"]["content"] == "光固化怎么加支撑才稳"
    db.exec("UPDATE topic_videos SET state='DONE', done_rev=1 WHERE no=3")
    db.exec("UPDATE topics SET synth_of=?", json.dumps([1, [[v, 1, "t"] for v in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")]]))
    assert TN.write(1)["state"] == "DONE"  # and the time after: still yours
    assert p["期数"]["number"] == 99


def test_a_write_that_fails_says_what_to_do_and_a_deleted_page_is_not_made_again(fake, monkeypatch):
    from assetizer import db
    from assetizer.topics import notion as TN
    db.exec("UPDATE topics SET notion_target=?", json.dumps({"kind": "page", "id": RESEARCH, "title": "研究"}))
    db.exec("UPDATE topics SET notion_state='RUNNING'")  # another write of it is under way: this one does nothing
    assert TN.write(1) == {"state": "RUNNING"} and not fake.calls
    db.exec("UPDATE topics SET notion_state=NULL")
    fake.down = 401
    assert TN.write(1)["error"]["kind"] == "TOKEN"
    fake.down = None
    fake.pages[RESEARCH]["archived"] = True
    out = TN.write(1)
    assert out["error"] == {"kind": "GONE", "message": "「研究」在 Notion 里已删除"}
    assert db.one("SELECT notion_state FROM topics")["notion_state"] == "FAILED"
    del fake.pages[RESEARCH]
    assert TN.write(1)["error"]["kind"] == "NO_ACCESS"
    monkeypatch.setattr(TN, "client", lambda: None)
    assert TN.write(1)["error"]["kind"] == "TOKEN"
    assert not db.one("SELECT 1 FROM alerts")
    # written, then the page deleted in Notion: the worker does not make it again; the page asks
    monkeypatch.setattr(TN, "client", lambda: fake)
    fake.pages[RESEARCH] = {"id": RESEARCH, "parent": {"type": "workspace"}, "properties": {}}
    assert TN.write(1)["state"] == "DONE"
    fake.pages[db.one("SELECT notion_page_id FROM topics")["notion_page_id"]]["archived"] = True
    assert TN.write(1, auto=True) == {"state": None, "gone": True}
    assert db.one("SELECT notion_page_id, notion_state FROM topics") == {"notion_page_id": None, "notion_state": None}


def test_the_whole_batch_is_written_then_told_and_new_topics_take_the_default(fake, monkeypatch):
    from fastapi.testclient import TestClient
    from assetizer import db, notify
    from assetizer.topics import run
    from assetizer.web.app import app
    c = TestClient(app, base_url="http://localhost")
    research = {"kind": "page", "id": RESEARCH, "title": "研究"}
    assert c.post("/api/topics/notion/default", json={"target": research}).json()["default"]["id"] == RESEARCH
    tid = c.post("/api/topics", json={"question": "新问题", "videos": [{"id": "ddddddddddd"}]}).json()["id"]
    assert json.loads(db.one("SELECT notion_target FROM topics WHERE id=?", tid)["notion_target"])["id"] == RESEARCH
    assert db.one("SELECT notion_target FROM topics WHERE id=1")["notion_target"] is None  # topics made before: 不存

    told: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, keep=True: told.append(text) or True)
    monkeypatch.setattr(run.W, "synth", lambda *a, **k: {"answer": "答 [V1 00:10 说]", "first": [], "sections": [], "videos": []})
    db.exec("UPDATE topics SET notion_target=? WHERE id=1", json.dumps(research))
    db.exec("UPDATE topic_videos SET state='DONE' WHERE topic_id=1")
    db.exec("UPDATE topics SET synth_of=NULL, synth_state='QUEUED' WHERE id=1")
    run.put_together(1)
    t = db.one("SELECT * FROM topics WHERE id=1")
    assert t["notion_state"] == "DONE"
    assert told == [f"🔎 专题读完了（3 期）：光固化怎么加支撑\nhttps://www.notion.so/{t['notion_page_id'].replace('-', '')}\n"
                    "https://assetizer.example.space/topics/1"]
    # an answer asked for early (a video still to read) is not written by itself
    db.exec("UPDATE topic_videos SET state='QUEUED' WHERE topic_id=1 AND no=3")
    db.exec("UPDATE topics SET synth_of=NULL, synth_state='QUEUED', notion_state=NULL WHERE id=1")
    run.put_together(1)
    assert db.one("SELECT notion_state FROM topics WHERE id=1")["notion_state"] is None and len(told) == 1


def test_a_time_next_to_a_video_s_citation_is_that_video_s(fake):
    """Models write [V1 12:34 说][13:02 帧]: a bracket right after one naming its video is that video's; one standing
    alone with no video cites nothing that can be linked, and stays as it is."""
    from assetizer.topics.notion import Page
    rich = Page(1).inline("密度 80% [V1 12:34 说][13:02 帧]，另见 [V2 1:02:03 说] [12:40 说]，单独的 [12:00 说]。")
    links = [(x["text"]["content"], x["text"]["link"]["url"]) for x in rich if x["text"].get("link")]
    assert links == [("V1 12:34", "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=754s"),
                     ("V1 13:02", "https://www.youtube.com/watch?v=aaaaaaaaaaa&t=782s"),
                     ("V2 1:02:03", "https://www.youtube.com/watch?v=bbbbbbbbbbb&t=3723s"),
                     ("V2 12:40", "https://www.youtube.com/watch?v=bbbbbbbbbbb&t=760s")]
    assert "单独的 [12:00 说]。" in "".join(x["text"]["content"] for x in rich)


def _answered(got: list, replies=True, result="已改"):
    """A stand-in for the model's answer: what it was told of the comments kept in `got`, a reply to each new thread."""
    def synth(*a, **k):
        threads = k.get("comments") or []
        got.append(threads)
        return {"answer": f"第 {len(got)} 回 [V1 12:34 说]", "first": [], "videos": [],
                "sections": [{"title": "密度", "body": "- 80%，薄壁另加 [V1 12:34 说]"}],
                "replies": [{"id": t["id"], "result": result, "text": f"改了 {t['id']}"} for t in threads
                            if replies and any(x["new"] for x in t["said"])]}
    return synth


def _thread(n, on, cid):
    return next(c for c in n.comments[on] if c["id"] == cid)["discussion_id"]


def test_comments_in_notion_go_into_the_next_answer_and_are_answered_in_their_thread(fake, monkeypatch):
    """Threads that call 课代表 on the topic's page and in its rows' notes are read back (not the resolved, not 课代表's
    own replies, not the comments that do not call it), counted on the topic's page, given to the next answer as the
    user's word, and answered in their thread, 「〔课代表〕已改 · 第 N 版」, once the version that took them in is on the
    page; taken in once, they do not call the model again; each word is kept as a use (design §R8-13)."""
    from fastapi.testclient import TestClient
    from assetizer import db, notify
    from assetizer.topics import comments as TC, notion as TN, run
    from assetizer.web.app import app
    monkeypatch.setattr(notify, "send", lambda text, keep=True: True)
    c = TestClient(app, base_url="http://localhost")
    db.exec("UPDATE topics SET notion_target=?", json.dumps({"kind": "page", "id": RESEARCH, "title": "研究"}))
    db.exec("UPDATE topic_videos SET excluded=1 WHERE no=3")  # the batch is whole: each answer goes to Notion by itself
    db.exec("UPDATE topics SET synth_of=?", run.signature(1))
    assert TN.write(1)["state"] == "DONE"
    t = db.one("SELECT * FROM topics")
    pid = t["notion_page_id"]
    page = fake.kids[pid]
    line = next(b for b in page if _said(b) == "80% V1 12:34 · V1 13:02")  # under 「密度」
    row = fake.row(t["notion_db"], 1)
    note = fake.kids[row["id"]][1]  # 外圈 80% 12:34
    whole = fake.say(pid, "课代表 整体太乐观了")
    on_line = fake.say(line["id"], "课代表：这条漏了薄壁的条件")
    on_note = fake.say(note["id"], "@课代表 这里是 85% 吧")
    fake.say(row["id"], "课代表，这期是广告")
    fake.say(page[3]["id"], "课代表 看过了")["resolved"] = True  # resolved: Notion does not give it
    fake.say(pid, "自己记一笔")                                  # not to 课代表: not its business
    fake.say(line["id"], "这段可以更简洁", by="notion-ai")        # nor Notion AI's
    fake.say(pid, "〔课代表〕已改 · 第 9 版")                      # 课代表's, whoever's name is on it
    for i in range(101):                                       # a long thread, read over more than one request
        fake.say(page[2]["id"], f"课代表 先看 {i}" if i == 0 else f"先看 {i}", thread="d-long")
    assert TC.scan(1) == 105
    assert c.get("/api/topics/1").json()["since"]["notes"] == 105
    del fake.comments[page[2]["id"]]
    assert TC.scan(1) == 0 and c.get("/api/topics/1").json()["since"]["notes"] == 4  # gone from Notion: not counted

    got: list = []
    monkeypatch.setattr(run.W, "synth", _answered(got))
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    run.put_together(1)
    told = got[0]
    assert [(x["id"], x["where"], [(s["who"], s["text"], s["new"]) for s in x["said"]]) for x in told] == [
        ("N1", "整页", [("用户", "整体太乐观了", True)]),
        ("N2", "「密度」一节这一行：80% V1 12:34 · V1 13:02", [("用户", "这条漏了薄壁的条件", True)]),
        ("N3", "V1《加支撑》的笔记这一行：外圈 80% 12:34", [("用户", "这里是 85% 吧", True)]),
        ("N4", "来源表 V1《加支撑》这一行", [("用户", "这期是广告", True)])]
    t = db.one("SELECT * FROM topics")
    assert t["notion_state"] == "DONE" and _said(fake.kids[pid][0]) == "第 1 回 V1 12:34"
    answered = {x["discussion_id"]: x for cs in fake.comments.values() for x in cs if x["created_by"]["id"] == BOT}
    assert {d: _said({"type": "p", "p": x}) for d, x in answered.items()} == {
        whole["discussion_id"]: "〔课代表〕已改 · 第 1 版\n改了 N1", on_line["discussion_id"]: "〔课代表〕已改 · 第 1 版\n改了 N2",
        on_note["discussion_id"]: "〔课代表〕已改 · 第 1 版\n改了 N3",
        _thread(fake, row["id"], fake.comments[row["id"]][0]["id"]): "〔课代表〕已改 · 第 1 版\n改了 N4"}
    assert line["id"] in [b["id"] for b in fake.kids[pid]]  # commented on: it stays, the new version beside it
    view = c.get("/api/topics/1").json()
    assert view["since"]["notes"] == 0 and view["notion"]["comments"] is None and not t["replies"]
    assert TC.scan(1) == 0  # 课代表's replies are not the user's word
    run.put_together(1)
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    run.put_together(1)
    assert len(got) == 1  # nothing new: no model call

    # a word more in a thread, one resolved, one edited: the threads still open, what is new in them marked
    fake.say(line["id"], "还有内圈", thread=on_line["discussion_id"])
    whole["resolved"] = True
    on_note["rich_text"] = [{"type": "text", "text": {"content": "课代表 这里是 85%"}, "plain_text": "课代表 这里是 85%"}]
    assert TC.scan(1) == 2
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    run.put_together(1)
    assert [(x["id"], [(s["who"], s["text"], s["new"]) for s in x["said"]]) for x in got[1]] == [
        ("N1", [("用户", "这条漏了薄壁的条件", False), ("课代表", "已改 · 第 1 版\n改了 N2", False), ("用户", "还有内圈", True)]),
        ("N2", [("用户", "这里是 85%", True), ("课代表", "已改 · 第 1 版\n改了 N3", False)]),
        ("N3", [("用户", "这期是广告", False), ("课代表", "已改 · 第 1 版\n改了 N4", False)])]
    assert [_said({"type": "p", "p": x}) for x in fake.comments[line["id"]]][-1] == "〔课代表〕已改 · 第 2 版\n改了 N1"
    assert sum(1 for cs in fake.comments.values() for x in cs if x["created_by"]["id"] == BOT) == 6

    # the integration may not write comments: the answer is written all the same, the replies wait for the next write
    fake.can = {"read"}
    fake.say(pid, "课代表 再看看")
    assert TC.scan(1) == 1
    db.exec("UPDATE topics SET synth_state='QUEUED'")
    run.put_together(1)
    view = c.get("/api/topics/1").json()
    assert view["notion"]["state"] == "DONE" and view["notion"]["comments"] == "NO_INSERT"
    assert len(json.loads(db.one("SELECT replies FROM topics")["replies"])) == 1
    fake.can = {"read", "insert"}
    assert TN.write(1)["state"] == "DONE"
    assert _said({"type": "p", "p": fake.comments[pid][-1]}) == "〔课代表〕已改 · 第 3 版\n改了 N4"
    assert c.get("/api/topics/1").json()["notion"]["comments"] is None and not db.one("SELECT replies FROM topics")["replies"]
    # nor read them: none are seen (as before), the page says why
    fake.can = set()
    assert TC.scan(1) == 0 and c.get("/api/topics/1").json()["notion"]["comments"] == "NO_READ"
    # each word to 课代表 is a use, kept with how it was handled (the long thread's too, gone before an answer took it)
    asks = {a["text"]: (a["result"], bool(a["answered_at"])) for a in db.q("SELECT * FROM asks") if not a["text"].startswith("先看")}
    assert asks == {"整体太乐观了": ("已改", True), "这条漏了薄壁的条件": ("已改", True), "这里是 85%": ("已改", True),
                    "这期是广告": ("已改", True), "还有内圈": ("已改", True), "再看看": ("已改", True)}
    assert TC.used(30)["asks"] == 107 and TC.used(30)["by"]["没处理"] == 101


def test_comments_are_looked_at_now_and_then_and_forgotten_with_the_page(fake, monkeypatch):
    """Comments are read every EVERY; a word to 课代表 puts the answer together again once the user stopped writing
    (QUIET); a 待修 is said mended in its thread; how much this is used is in Monday's digest and on the settings page."""
    import datetime as dt
    from assetizer import cli, db, logs, notify
    from assetizer.topics import comments as TC, notion as TN, run
    from fastapi.testclient import TestClient
    from assetizer.web.app import app
    monkeypatch.setattr(notify, "send", lambda text, keep=True: True)
    db.exec("UPDATE topics SET notion_target=?", json.dumps({"kind": "page", "id": RESEARCH, "title": "研究"}))
    db.exec("UPDATE topic_videos SET excluded=1 WHERE no=3")
    db.exec("UPDATE topics SET synth_of=?", run.signature(1))
    assert TN.write(1)["state"] == "DONE"
    pid = db.one("SELECT notion_page_id FROM topics")["notion_page_id"]
    ask = fake.say(pid, "课代表 来源表的日期一列是空的")
    ask["created_time"] = dt.datetime.now(dt.timezone.utc).isoformat()
    fake.say(pid, "自己记一笔")
    db.exec("UPDATE topics SET comments_at='2026-11-09T00:00:00+00:00'")
    TC.due()  # looked at more than EVERY ago; written just now: the user may write more
    assert TC.fresh(1) == 1 and db.one("SELECT synth_state FROM topics")["synth_state"] == "DONE"
    db.exec("UPDATE topic_comments SET created='2026-11-09T00:00:00.000Z'")
    TC.due()  # just looked at; the word QUIET old: put together again
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "QUEUED"
    got: list = []
    monkeypatch.setattr(run.W, "synth", _answered(got, result="记进待修"))
    run.put_together(1)
    assert _said({"type": "p", "p": fake.comments[pid][-1]}) == "〔课代表〕记进待修 #1\n改了 N1"
    TC.due()
    assert db.one("SELECT synth_state FROM topics")["synth_state"] == "DONE" and len(got) == 1  # taken in: not again
    assert TC.used(30)["todo"][0]["text"] == "来源表的日期一列是空的"
    assert "📝 Notion 里点名课代表：这周 1 次，近 30 天 1 次；待修 1 条" in logs.digest_lines()
    c = TestClient(app, base_url="http://localhost")
    assert c.get("/api/settings").json()["notion"]["asks"] == {"n": 1, "todo": 1}
    cli.main(["asks", "--fixed", "1", "--version", "0.4.43"])
    TC.due()
    assert _said({"type": "p", "p": fake.comments[pid][-1]}) == "〔课代表〕已修 #1 · 0.4.43"
    assert not TC.used(30)["todo"] and TC.scan(1) == 0
    TC.due()
    assert sum(1 for x in fake.comments[pid] if "已修" in _said({"type": "p", "p": x})) == 1  # said once
    fake.say(pid, "课代表 又一条")
    TC.due()  # just looked at
    assert TC.fresh(1) == 0
    db.exec("UPDATE topics SET archived_at='2026-11-10T00:00:00+00:00', comments_at='2026-11-09T00:00:00+00:00'")
    TC.due()  # archived: the answer stays as it is
    assert TC.fresh(1) == 0
    c = TestClient(app, base_url="http://localhost")
    c.post("/api/topics/1/notion", json={"target": None})  # another place: the old page's comments are not this topic's
    assert TC.fresh(1) == 0 and not db.one("SELECT 1 FROM topic_comments")


def test_the_answer_is_told_comments_as_the_users_word_and_answers_each_new_thread(home):
    from assetizer.llm import ContractError
    from assetizer.topics import write as W
    got: dict = {}
    raw = {"answer": "会降 [V1 00:10 说]", "certainty": {"level": "中"}, "first": [], "videos": [], "sections": [],
           "claims": [{"id": "C1", "type": "预测", "text": "会降 [V1 00:10 说]", "v": [1], "status": "现行"}],
           "replies": [{"id": "N1", "result": "已改", "text": "  C1 降到低：只有 V1 讲到  "},
                       {"id": "N9", "result": "没改", "text": "没有这一串"}, {"id": "N2", "result": "收到", "text": "不是能回的"}]}

    class Fake:
        def structured(self, root, stage, ask, schema, normalize=None, check=None):
            got["ask"], got["schema"] = ask, schema
            o = normalize(json.loads(json.dumps(raw)))
            if check:
                check(o)
            return o
    videos = [{"no": 1, "title": "三月", "channel": "甲", "published": "20250301", "fit": "high", "gist": "会降", "asset": "会降 [00:10 说]"}]
    threads = [{"id": "N1", "where": "整页", "said": [{"who": "用户", "text": "太乐观了，美联储上周已经加息", "new": True}]},
               {"id": "N2", "where": "「节奏」一节这一行：会降 V1 00:10",
                "said": [{"who": "用户", "text": "来源只有一个", "new": False}, {"who": "课代表", "text": "第 1 版：降到中", "new": False}]}]
    o = W.synth(Fake(), home, "topic-synth", "会降息吗", None, videos, prev={"answer": "会降"}, comments=threads, today="2026-11-10")
    ask = got["ask"]
    assert "## 用户在 Notion 里给你的批注" in ask and "N1 · 整页" in ask and "- 用户（新）：太乐观了，美联储上周已经加息" in ask
    assert "- 课代表：第 1 版：降到中" in ask and "不是来源" in ask
    assert "replies" in got["schema"]["properties"]
    assert o["replies"] == [{"id": "N1", "result": "已改", "text": "C1 降到低：只有 V1 讲到"}]  # trimmed; no such thread or result: dropped
    assert got["schema"]["properties"]["replies"]["items"]["properties"]["result"]["enum"] == ["已改", "没改", "已答", "记进待修"]
    raw["replies"] = []
    with pytest.raises(ContractError):  # a new word with no reply: asked again
        W.synth(Fake(), home, "topic-synth", "会降息吗", None, videos, prev={"answer": "会降"}, comments=threads)
    o = W.synth(Fake(), home, "topic-synth", "会降息吗", None, videos, prev={"answer": "会降"})
    assert "## 用户在 Notion 里给你的批注" not in got["ask"] and o["replies"] == []
