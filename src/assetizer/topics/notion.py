"""专题存到 Notion (design §R8-1, §R8-12): a reading copy of the answer, on a page or in a database picked per topic (new topics
take `topic_notion_default`). The topic's page stays what the answer is; Notion is where it is read, and written in.

Never in a channel's database nor in 课代表's own (`notion_database_id`): those hold one page per video. A topic's page
has the conclusion and what it rests on (its disputes right under it), then the 来源 table — a row for each video read,
its notes (证据档案) inside the row — then the versions. Every citation links the YouTube moment it cites. Written by the
worker once the whole batch is put together (run.put_together), or when asked on the topic's page. Written again, only
the blocks 课代表 wrote that changed are replaced: what anyone else wrote, edited or commented on stays where it is
(design §R8-12), and so do the properties and the table's columns someone changed. The comments on the page go into
the next answer and are answered in their threads (comments.py, design §R8-13)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .. import db, notify
from ..delivery import _bot, _create, _prop_hashes, client
from ..notion import blocks as B
from ..notion import mapping as M
from ..notion.client import Notion, NotionError
from ..util import parse_tc
from . import comments, work_dir
from . import write as W

TZ = ZoneInfo("Asia/Shanghai")
RECENT = 6
ICON = "🔎"
GRAY = {"color": "gray"}
BOLD = {"bold": True}
TABLE = {"type": "child_database"}   # where the 来源 table sits among the page's blocks
COLUMNS = {   # the 来源 table; 备注 is the user's, never written
    "视频": {"title": {}}, "期": {"number": {"format": "number"}}, "频道": {"select": {"options": []}},
    "发布日期": {"date": {}}, "立场": {"rich_text": {}}, "要点": {"rich_text": {}}, "利益相关": {"rich_text": {}},
    "转述": {"rich_text": {}},
    "状态": {"select": {"options": [{"name": "算", "color": "default"},
                                    *({"name": f"不算 · {why}", "color": "gray"} for why in ("离题", "质量差", "转述别人")),
                                    {"name": "不算", "color": "gray"}, {"name": "原片下架", "color": "brown"},
                                    {"name": "移出", "color": "gray"}]}},
    "链接": {"url": {}}, "备注": {"rich_text": {}},
}
TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?"
PART = re.compile(rf"\s*(?:V(\d{{1,3}})\s*)?(?:({TIME})(?:\s*[-–—~至到]\s*{TIME})?\s*(说|帧|画面)?|([LR]\d{{1,3}}))?\s*(?:[,，;；、/]\s*)?")
BRACKET = re.compile(r"\s*\[([^\[\]\n]{1,120})\](?!\()")


class Problem(RuntimeError):
    """Why a write did not happen, by what the user can do: TOKEN 去设置, NO_ACCESS 重试, GONE 换地方, OTHER 重试."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def page_url(pid: str) -> str:
    return f"https://www.notion.so/{pid.replace('-', '')}"


def _norm(i: str | None) -> str:
    try:
        return M.norm_id(i or "")
    except ValueError:
        return i or ""


def _need() -> Notion:
    n = client()
    if not n:
        raise Problem("TOKEN", "还没有 Notion token")
    return n


# ------------------------------------------------------------------ where a topic may go
def excluded() -> dict[str, str]:
    """database id → why a topic is not written there: a channel's database (its test copies and any it was delivered
    to before), or 课代表's own."""
    names = {r["id"]: r["name"] for r in db.q("SELECT id, name FROM channels")}
    out: dict[str, str] = {}
    for r in db.q("SELECT DISTINCT notion_database_id d, channel_id c FROM sources WHERE notion_database_id IS NOT NULL"):
        out[_norm(r["d"])] = f"这是「{names.get(r['c'], r['c'])}」的频道库"
    for r in db.q("SELECT key, value FROM settings WHERE key LIKE 'notion:%'"):
        cfg = db.unj(r["value"], {}) or {}
        cid = r["key"][len("notion:"):]
        if isinstance(cfg, dict) and cfg.get("database_id"):
            out[_norm(cfg["database_id"])] = f"这是「{names.get(cid, cid)}」的频道库"
    if d := db.setting("notion_database_id"):
        out[_norm(d)] = "这是课代表的默认库"
    for r in db.q("SELECT question, notion_db FROM topics WHERE notion_db IS NOT NULL"):
        out[_norm(r["notion_db"])] = f"这是专题「{r['question'][:40]}」的来源表"
    return out


def _why_not(kind: str, o: dict, ex: dict[str, str]) -> str | None:
    if kind == "database":
        return ex.get(_norm(o.get("id")))
    p = o.get("parent") or {}
    if p.get("type") == "database_id":
        return ex.get(_norm(p.get("database_id"))) or "这是库里的一行：选这个库，或别的页面"
    return None


def _title(kind: str, o: dict) -> str:
    if kind == "database":
        return "".join(t.get("plain_text", "") for t in o.get("title") or []) or "未命名"
    for v in (o.get("properties") or {}).values():
        if isinstance(v, dict) and v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v.get("title") or []) or "未命名"
    return "未命名"


def _view(kind: str, o: dict) -> dict:
    icon = o.get("icon") or {}
    return {"kind": kind, "id": _norm(o["id"]), "title": _title(kind, o),
            "icon": icon.get("emoji") if icon.get("type") == "emoji" else None}


def targets(q: str = "") -> list[dict]:
    """Pages (not rows of a database) and databases the integration sees, less the excluded; recently used first."""
    n = _need()
    ex = excluded()
    bot = _bot(n)  # what 课代表 made here are topics' pages and their 来源 tables (a channel's pages go in its database)
    q = q.strip()
    out, seen = [], set()
    for t in db.setting("topic_notion_recent") or []:
        if (not q or q.lower() in t["title"].lower()) and not (t["kind"] == "database" and t["id"] in ex):
            out.append({**t, "recent": True})
            seen.add(t["id"])
    for kind in ("page", "database"):
        try:
            found = n.search(q, kind=kind, page_size=30)
        except NotionError as e:
            raise _problem(e) from e
        for o in found:
            i = _norm(o.get("id"))
            if (o.get("archived") or i in seen or _why_not(kind, o, ex)
                    or (bot and (o.get("created_by") or {}).get("id") == bot)):
                continue
            seen.add(i)
            out.append(_view(kind, o))
    return out


def resolve(link: str) -> dict:
    """A pasted link: the page or database it is, or why it is not a place for a topic (Problem)."""
    try:
        i = M.norm_id(link)
    except ValueError as e:
        raise Problem("BAD", str(e)) from e
    n = _need()
    try:
        o, kind = n._req("GET", f"/databases/{i}"), "database"
    except NotionError as e:
        if e.status == 401:
            raise _problem(e) from e
        try:
            o, kind = n.page(i), "page"
        except NotionError as e2:
            raise _problem(e2, "这一页") from e2
    if o.get("archived"):
        raise Problem("GONE", "这一页在 Notion 的回收站里")
    if why := _why_not(kind, o, excluded()):
        raise Problem("BAD", why)
    return _view(kind, o)


def remember(target: dict) -> None:
    keep = {k: target.get(k) for k in ("kind", "id", "title", "icon")}
    rest = [t for t in db.setting("topic_notion_recent") or [] if t.get("id") != keep["id"]]
    db.set_setting("topic_notion_recent", [keep, *rest][:RECENT])


def _problem(e: Exception, what: str = "") -> Problem:
    if isinstance(e, Problem):
        return e
    if isinstance(e, NotionError):
        if e.status == 401:
            return Problem("TOKEN", "Notion token 失效")
        if e.status in (403, 404):
            return Problem("NO_ACCESS", f"Notion 里看不到{f'「{what}」' if what and what != '这一页' else '这一页'}"
                           "：把课代表的 integration 加到它的 Connections")
        if e.status == 400 and "archived" in (e.body or ""):
            return Problem("GONE", f"{f'「{what}」' if what else '那里'}在 Notion 里已删除")
        return Problem("OTHER", str(e)[:300])
    return Problem("OTHER", f"{e}"[:300])


# ------------------------------------------------------------------ the page
def parse(inner: str, v: int | None = None) -> list[dict] | None:
    """What a bracket cites ([V3 12:34 说], [V3 L03], [V3], [V3 12:34 说，13:02 帧]), or None when it is something else.
    A time or a look without a video takes the video before it (web/src/components/topic/cite.ts), or `v`: the video
    whose notes it is in."""
    out: list[dict] = []
    at = 0
    while at < len(inner):
        m = PART.match(inner, at)
        if not m or m.end() == at:
            return None
        at = m.end()
        no, time, _kind, look = m.groups()
        if not (no or time or look):
            continue
        if no:
            v = int(no)
        if v is None:
            return None
        out.append({"v": v, "at": time.split(".")[0] if time else None, "t": parse_tc(time) if time else None, "look": look})
    return out or None


def _t(text: str, ann: dict | None = None, url: str | None = None) -> dict:
    x: dict = {"type": "text", "text": {"content": text[:B.MAX_TEXT]}}
    if url:
        x["text"]["link"] = {"url": url}
    if ann:
        x["annotations"] = ann
    return x


def _words(text: str | None) -> list[dict]:
    """A column's text."""
    return [_t(text)] if text else []


def _line(pieces: list[dict], kind: str = "paragraph") -> list[dict]:
    return B.spread(kind, pieces)


class Page:
    """A topic's answer as Notion blocks: on the topic's page the conclusion and what it rests on, the 来源 table, the
    versions; in each of the table's rows, a video's notes."""

    def __init__(self, tid: int):
        self.tid = tid
        self.t = t = db.one("SELECT * FROM topics WHERE id=?", tid)
        self.synth = W.apply_marks(db.unj(t["synth"], {}) or {}, db.unj(t["marks"], {}))
        rows = db.q("""SELECT tv.no, tv.video_id, tv.done_rev, tv.gist, tv.excluded, tv.excluded_why, tv.card,
                              v.title, v.channel, v.published_at, v.gone
                       FROM topic_videos tv JOIN videos v ON v.id=tv.video_id WHERE tv.topic_id=? ORDER BY tv.no""", tid)
        self.by_no = {r["no"]: r for r in rows}
        made = {x[0] for x in (db.unj(t["synth_of"], None) or [0, []])[1]}
        self.read = [r for r in rows if r["video_id"] in made]
        self.rows = [r for r in rows if r["done_rev"] is not None]  # the 来源 table: each video read
        self.total = sum(1 for r in rows if not r["excluded"])
        self.claimed = isinstance(self.synth.get("claims"), list)
        self.versions = W.versions(tid)
        self._looks: dict[int, dict] = {}

    # citations ------------------------------------------------------------
    def _look_at(self, no: int, look: str) -> float | None:
        if no not in self._looks:
            r = self.by_no[no]
            d = work_dir(self.tid, r["done_rev"] or 1, r["video_id"])
            seen: dict = {}
            for name in ("looks.json", "looks2.json"):
                try:
                    for x in json.loads((d / name).read_text()):
                        at = x.get("start") if x.get("start") not in (None, "") else (x.get("times") or [None])[0]
                        seen[x.get("id")] = parse_tc(at) if at not in (None, "") else None
                except (OSError, ValueError, TypeError):
                    pass
            self._looks[no] = seen
        return self._looks[no].get(look)

    def _cite(self, c: dict, bare: bool = False) -> tuple[str, str]:
        r = self.by_no[c["v"]]
        url = f"https://www.youtube.com/watch?v={r['video_id']}"
        t = c["t"]
        at = c["at"]
        if c["look"]:
            t = self._look_at(c["v"], c["look"])
            at = _clock(t) if t is not None else c["look"]
        label = (at or "") if bare else f"V{c['v']}" + (f" {at}" if at else "")
        return label or f"V{c['v']}", url + (f"&t={int(t)}s" if t is not None else "")

    def inline(self, text: str, v: int | None = None) -> list[dict]:
        """Inline Markdown → rich_text, each citation grey text linking the moment; next ones run together as one:
        V3 12:34 · V5 03:10. `v`: the video whose notes these are (its citations name no video, nor do their labels)."""
        parts: list = []
        pos = 0
        for m in BRACKET.finditer(text):
            between = text[pos:m.start()]
            after = not between.strip() and parts and isinstance(parts[-1], list)  # right after a citation
            cs = parse(m[1], v)
            if cs is None and v is None and after:  # [V1 12:34 说][13:02 帧]: the second is V1's too
                cs = parse(m[1], parts[-1][-1]["v"])
            if cs is None or any(c["v"] not in self.by_no for c in cs):
                continue
            if between or not parts or not isinstance(parts[-1], list):
                parts += [between, cs]
            else:
                parts[-1] += cs
            pos = m.end()
        parts.append(text[pos:])
        out: list[dict] = []
        for p in parts:
            if isinstance(p, str):
                if p:
                    out += B.runs(p)
                continue
            out.append(_t(" "))
            for i, c in enumerate(p):
                label, url = self._cite(c, bare=v is not None)
                if i:
                    out.append(_t(" · ", GRAY))
                out.append(_t(label, GRAY, url))
        return out or [_t("")]

    def _md(self, text: str, v: int | None = None) -> list[dict]:
        return B.markdown(text or "", lambda x: self.inline(x, v), whole=True)

    # the topic's page -----------------------------------------------------
    def _video_line(self, no: int) -> list[dict]:
        r = self.by_no[no]
        out = [_t(f"V{no} ", GRAY), _t(f"《{r['title'] or r['video_id']}》", url=f"https://www.youtube.com/watch?v={r['video_id']}")]
        if r["channel"]:
            out.append(_t(f" · {r['channel']}", GRAY))
        return out

    def _who(self, c: dict) -> str:
        """V2 等 2 期 · 频道 · 2025-10-15: the first video a claim cites."""
        r = self.by_no.get(c["v"][0]) or {}
        return " · ".join(x for x in (f"V{c['v'][0]}" + (f" 等 {len(c['v'])} 期" if len(c["v"]) > 1 else ""),
                                       r.get("channel"), W.day(r.get("published_at"))) if x)

    def _top(self) -> list[dict]:
        """The conclusion (archived, how sure), what this version changed, the rest of the answer."""
        s, t = self.synth, self.t
        answer = self._md(s.get("answer") or "")
        lines: list[list[dict]] = []
        if t["archived_at"]:
            lines.append([_t(" · ".join(x for x in ("已归档", t["archived_how"], _day(t["archived_at"])) if x), BOLD),
                          *([_t("　" + t["archived_why"], GRAY)] if t["archived_why"] else [])])
        cert = (s.get("certainty") or {}) if self.claimed else {}
        sure = [[_t(f"确定性 {cert['level']}" + (f" · {cert['why']}" if cert.get("why") else ""), GRAY)]] if cert.get("level") else []
        rest = answer
        if answer and answer[0]["type"] == "paragraph" and not answer[0]["paragraph"].get("children"):
            head = answer[0]["paragraph"]["rich_text"]
            if sum(map(len, lines + sure)) + len(head) + len(lines) + len(sure) <= B.MAX_PIECES:
                lines.append(head)
                rest = answer[1:]
        lines += sure
        said: list[dict] = []
        for i, x in enumerate(lines):
            said += ([_t("\n")] if i else []) + x
        out = [{"object": "block", "type": "callout", "callout": {
            "rich_text": said or [_t("")], "icon": {"type": "emoji", "emoji": "🧭"}, "color": "gray_background"}}]
        bits = []
        if len(self.read) < self.total:
            bits.append(f"基于 {len(self.read)}/{self.total} 期")
        if self.claimed and len(self.versions) > 1 and s.get("changes"):
            bits.append("本版：" + "；".join(s["changes"]))
        if bits:
            out += _line(B.runs(" · ".join(bits), color="gray"))
        return out + rest

    def _first(self) -> list[dict]:
        out = []
        first = [f for f in self.synth.get("first") or [] if f.get("v") in self.by_no]
        if first:
            out.append(B.block("heading_2", "先看"))
            for f in first:
                out += _line(self._video_line(f["v"]) + ([_t("：")] + self.inline(f["why"]) if f.get("why") else []),
                             "numbered_list_item")
        return out

    def _claims(self) -> list[dict]:
        """The answer below its conclusion when it is made of claims, as on the topic's page (Claims.tsx): the disputes,
        先看, the sub-questions, the predictions still open, the claims settled or replaced."""
        s = self.synth
        claims = s.get("claims") or []
        by_id = {c["id"]: c for c in claims}
        dispute = {}
        for d in s.get("disputes") or []:
            for i in d["claims"]:
                dispute.setdefault(i, d["id"])
        out: list[dict] = []
        if s.get("disputes"):
            out.append(B.block("heading_2", "分歧"))
            for d in s["disputes"]:
                out += B.spread("heading_3", B.runs(d["about"]))
                for c in (by_id[i] for i in d["claims"] if i in by_id):
                    out += _line([_t(self._who(c) + "　", GRAY), *self.inline(c["text"])], "bulleted_list_item")
                for label, key in (("能判定", "decide"), ("倾向", "lean")):
                    if d.get(key):
                        out += _line([_t(label + "　", BOLD), *B.runs(d[key])])
        out += self._first()
        for sec in s.get("sections") or []:
            cs = [by_id[i] for i in sec.get("claims") or [] if i in by_id and by_id[i]["status"] == "现行"]
            if not cs and not sec.get("note"):
                continue
            out.append(B.block("heading_2", sec["title"]))
            if sec.get("note"):
                out += _line(B.runs(sec["note"], color="gray"))
            for c in cs:
                out += _line([_t(c["type"] + "　", GRAY), *self.inline(c["text"]), *self._meta(c, c["id"] in dispute)],
                             "bulleted_list_item")
        open_ = sorted((c for c in claims if c["type"] == "预测" and c["status"] == "现行"), key=lambda c: c.get("due") or "9")
        if open_:
            out.append(B.block("heading_2", "待验证的预测"))
            for c in open_:
                out += _line([_t((c.get("due") or "时间含糊") + "　", GRAY), *B.runs(W.plain(c["text"])),
                              _t("　" + self._who(c), GRAY)], "bulleted_list_item")
        done = [c for c in claims if c["status"] != "现行"]
        if done:
            settled = [c for c in done if c["status"] in ("应验", "落空")]
            out.append(B.block("heading_2", "已结算" if len(settled) == len(done) else "已结算 · 已被取代" if settled else "已被取代"))
            for c in settled + [c for c in done if c not in settled]:
                why = self._why(c, by_id)
                out += _line([_t(("改口" if c.get("shift") else c["status"]) + "　", BOLD), *self.inline(c["text"]),
                              _t("　" + " · ".join(x for x in (self._who(c), why) if x), GRAY)], "bulleted_list_item")
        return out

    @staticmethod
    def _meta(c: dict, disputed: bool) -> list[dict]:
        bits = []
        if c.get("certainty"):
            bits.append(c["certainty"] + (f"：{c['why']}" if c["certainty"] != "高" and c.get("why") else ""))
        if c["type"] == "预测" and c.get("due"):
            bits.append(f"到期 {c['due']}")
        if c.get("vague") and "时间含糊" not in (c.get("why") or ""):
            bits.append("时间含糊")
        if c["type"] == "预测" and c.get("condition"):
            bits.append(f"怎么算应验：{c['condition']}")
        if c["type"] == "建议" and c.get("premise"):
            bits.append(f"前提：{c['premise']}")
        if disputed:
            bits.append("见分歧")
        return [_t("　" + " · ".join(bits), GRAY)] if bits else []

    @staticmethod
    def _why(c: dict, by_id: dict) -> str:
        if c.get("marked"):
            return f"你标的：{c['marked']['why']}" if c["marked"].get("why") else "你标的"
        by = by_id.get(c.get("by") or "")
        return f"{'后来改说' if c.get('shift') else '新的说法' if c['status'] == '被取代' else '依据'}：{W.plain(by['text'])}" if by else ""

    def _under(self) -> list[dict]:
        """Under the 来源 table: how the sources lean, how the channels' predictions went, the versions, the foot."""
        out: list[dict] = []
        if self.claimed and (bal := (self.synth.get("balance") or "").strip()):
            out += _line(B.runs(bal))
        names = list(dict.fromkeys(r["channel"] for r in self.rows if r["channel"]))
        rec = W.record(set(names)) if self.claimed else {}
        went = [f"{ch} {rec[ch]['hit']} 应验 · {rec[ch]['miss']} 落空" for ch in names if ch in rec and rec[ch]["hit"] + rec[ch]["miss"]]
        if went:
            out += _line(B.runs("预测战绩　" + "　".join(went), color="gray"))
        if len(self.versions) > 1:
            kids = []
            for x in self.versions[:B.MAX_PIECES]:
                head = " · ".join(b for b in (f"第 {x['n']} 版", _day(x["at"]), f"{x['read']}/{x['total']} 期",
                                               x["certainty"] and f"确定性 {x['certainty']}") if b)
                kids += _line([_t(head), *([_t("　" + "；".join(x["changes"]), GRAY)] if x["changes"] else [])], "bulleted_list_item")
            out.append({"object": "block", "type": "toggle", "toggle": {"rich_text": [_t(f"{len(self.versions)} 个版本")], "children": kids}})
        out.append(B.divider())
        foot = [_t(f"{len(self.read)} 期 · {_day(self.t['synth_at'])}综合", GRAY)]
        if link := notify.link(f"/topics/{self.tid}"):
            foot += [_t(" · ", GRAY), _t("专题页 ↗", GRAY, link)]
        out.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": foot}})
        return out

    def blocks(self) -> tuple[list[dict], list[dict]]:
        """The topic's page: the blocks above the 来源 table, and those under it."""
        if self.claimed:
            top = self._top() + self._claims()
        else:  # an answer put together before 0.4.41: its sections are Markdown
            top = self._top() + self._first()
            for sec in self.synth.get("sections") or []:
                top.append(B.block("heading_2", sec.get("title") or ""))
                top += self._md(sec.get("body") or "")
        return top, self._under()

    # the 来源 table -------------------------------------------------------
    def props(self, r: dict) -> dict:
        """A video's row: what the answer says of it (立场, 利益相关, 转述, 要点), else what its notes did."""
        src = next((x for x in self.synth.get("sources") or [] if x.get("v") == r["no"]), {})
        card = db.unj(r["card"], {}) or {}
        adds = next((x.get("adds") for x in self.synth.get("videos") or [] if x.get("v") == r["no"]), None)
        day = W.day(r["published_at"])
        return {"视频": {"title": _words(r["title"] or r["video_id"])},
                "期": {"number": r["no"]},
                "频道": {"select": {"name": r["channel"].replace(",", "，")[:100]} if r["channel"] else None},
                "发布日期": {"date": {"start": day} if day else None},
                "立场": {"rich_text": _words(src.get("stance"))},
                "要点": {"rich_text": _words(W.plain(adds or r["gist"] or ""))},
                "利益相关": {"rich_text": _words(src.get("interests") or "；".join(card.get("interests") or []))},
                "转述": {"rich_text": _words(src.get("relays") or "；".join(card.get("relays") or []))},
                "状态": {"select": {"name": (f"不算 · {r['excluded_why']}" if r["excluded_why"] else "不算") if r["excluded"]
                                    else "原片下架" if r["gone"] else "算"}},
                "链接": {"url": f"https://www.youtube.com/watch?v={r['video_id']}"}}

    def evidence(self, r: dict) -> list[dict]:
        """A video's row inside: its notes, each citation linking the moment (else what it says in a line)."""
        d = work_dir(self.tid, r["done_rev"] or 1, r["video_id"])
        notes = next(((d / n).read_text() for n in ("notes.md", "notes-1.md") if (d / n).exists()), "")
        if notes.strip():
            return self._md(notes, r["no"])
        return _line(self.inline(r["gist"], r["no"])) if r["gist"] else []


def _clock(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}" if t >= 3600 else f"{t // 60:02}:{t % 60:02}"


def _day(iso: str | None) -> str:
    try:
        d = dt.datetime.fromisoformat(iso).astimezone(TZ)
    except (TypeError, ValueError):
        d = dt.datetime.now(TZ)
    return f"{d.month}月{d.day}日"


# ------------------------------------------------------------------ writing among what others wrote (design §R8-12)
def _said(pieces: list[dict]) -> list:
    text, links = [], []
    for x in pieces:
        text.append(x["plain_text"] if "plain_text" in x else (x.get("text") or {}).get("content", ""))
        url = x.get("href") or ((x.get("text") or {}).get("link") or {}).get("url")
        if url and (not links or links[-1] != url):
            links.append(url)
    return ["".join(text), links]


def _sig(b: dict, kids: list[str]) -> str:
    """What a block says — its kind, words, links and what is in it — the same for a block about to be written as for
    one Notion gives back: the same, it stays."""
    t = b["type"]
    body = b.get(t) or {}
    said = [_said(c) for c in body.get("cells") or []] if t == "table_row" else _said(body.get("rich_text") or [])
    more = [body.get(k) for k in ("checked", "language", "table_width", "has_column_header")]
    return hashlib.sha1(json.dumps([t, said, more, kids], ensure_ascii=False).encode()).hexdigest()


def _shape(b: dict, kids: list[str]) -> str:
    return f"{b['type']}({','.join(kids)})" if kids else b["type"]


def _kids(b: dict) -> list[dict]:
    return (b.get(b["type"]) or {}).get("children") or []


def _new(b: dict, how=_sig) -> str:
    return how(b, [_new(k, how) for k in _kids(b)])


def _count(shapes: list[str]) -> int:
    return sum(len(re.findall(r"[a-z][a-z_0-9]*", x)) for x in shapes)


def _batches(blocks: list[dict]):
    """Blocks in requests Notion takes: 100 at the top, 1000 in all, well under its 500 KB."""
    part: list[dict] = []
    n = size = 0
    for b in blocks:
        bn, bs = _count([_new(b, _shape)]), len(json.dumps(b, ensure_ascii=False).encode())
        if part and (len(part) == B.MAX_PIECES or n + bn > 1000 or size + bs > 400_000):
            yield part
            part, n, size = [], 0, 0
        part.append(b)
        n += bn
        size += bs
    if part:
        yield part


def _lcs(old: list[str | None], new: list[str]) -> list[tuple[int, int]]:
    """The longest run, in order, of Assetizer's blocks (old, None: not its own) the new ones say the same as."""
    a, b = len(old), len(new)
    best = [[0] * (b + 1) for _ in range(a + 1)]
    for i in range(a - 1, -1, -1):
        for j in range(b - 1, -1, -1):
            best[i][j] = best[i + 1][j + 1] + 1 if old[i] is not None and old[i] == new[j] else max(best[i + 1][j], best[i][j + 1])
    out, i, j = [], 0, 0
    while i < a and j < b:
        if old[i] is not None and old[i] == new[j]:
            out.append((i, j))
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def _place(old: list[dict], sig: list[str | None], new: list[dict], pairs: list[tuple[int, int]],
           start: str | None) -> tuple[list[tuple[str | None, list[dict]]], list[str]]:
    """Where the new blocks go, and which old ones go. Between two blocks kept, the new ones take the places of
    Assetizer's old ones, a run of them at a time, so what someone put between two runs stays between them; with none
    of its old ones there, they follow the block kept before them (`start`, else the page's start: after what someone
    put there, or at the end of an empty page)."""
    puts: list[tuple[str | None, list[dict]]] = []
    gone: list[str] = []
    po, pn = -1, -1
    for oi, ni in [*pairs, (len(old), len(new))]:
        runs: list[list[dict]] = []
        run: list[dict] = []
        for i in range(po + 1, oi):
            if sig[i] is not None:
                gone.append(old[i]["id"])
                run.append(old[i])
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        fresh = new[pn + 1:ni]
        if fresh and runs:
            k = 0
            for j, r in enumerate(runs):
                take = len(fresh) - k if j == len(runs) - 1 else min(len(r), len(fresh) - k)
                if take > 0:
                    puts.append((r[-1]["id"], fresh[k:k + take]))
                    k += take
        elif fresh:
            puts.append((old[po]["id"] if po >= 0 else start or (old[oi - 1]["id"] if oi > 0 else None), fresh))
        po, pn = oi, ni
    return puts, gone


class Sync:
    """Writes Assetizer's blocks under a page among everyone else's: its blocks that say the same stay (and the
    comments on them), those that changed or went are taken out — unless someone edited them, wrote in them or
    commented on them: those stay as theirs — and the new ones go where the old ones were. Nothing of anyone else's is
    moved or taken out."""

    def __init__(self, n: Notion, bot: str):
        self.n, self.bot = n, bot
        self.comments = True          # Notion lets the integration read comments (else none can be seen)
        self.kept: set[str] = set()   # Assetizer's blocks kept for someone's comment on them

    def old(self, b: dict, how=_sig) -> str | None:
        """What a block of Assetizer's says, with all in it; None when it is not as Assetizer left it."""
        if ((b.get("created_by") or {}).get("id") != self.bot or (b.get("last_edited_by") or {}).get("id") not in (None, self.bot)
                or b["id"] in self.kept or b["type"] in ("child_page", "child_database")):
            return None
        kids = []
        if b.get("has_children"):
            for k in self.n.children(b["id"]):
                x = self.old(k, how)
                if x is None:
                    return None
                kids.append(x)
        return how(b, kids)

    def said(self, bid: str) -> bool:
        if not self.comments:
            return False
        try:
            return bool(self.n._req("GET", f"/comments?block_id={bid}&page_size=1", idempotent=True).get("results"))
        except NotionError as e:
            if e.status == 403:
                self.comments = False
            elif e.status != 404:
                raise
            return False

    def put(self, parent: str, blocks: list[dict], after: str | None) -> None:
        """After `after` the later requests go in first, each right after it, so they end up in order."""
        parts = list(_batches(blocks))
        for p in reversed(parts) if after else parts:
            self.n.append(parent, p, after=after)

    def write(self, parent: str, new: list[dict], old: list[dict], start: str | None = None) -> None:
        """`old`, the blocks under `parent` now in order, made `new` where they are Assetizer's; `start`: the block just
        before them (the 来源 table, for those under it)."""
        sig = [self.old(b) for b in old]
        pairs = _lcs(sig, [_new(b) for b in new])
        if start is None and pairs and pairs[0][0] == 0 and pairs[0][1] > 0:
            pairs = pairs[1:]  # nothing goes in before a page's first block: it goes, and the new ones in after it
        kept = {i for i, _ in pairs}
        for i, b in enumerate(old):
            if sig[i] is not None and i not in kept and self.said(b["id"]):
                sig[i] = None
                self.kept.add(b["id"])
        puts, gone = _place(old, sig, new, pairs, start)
        for after, bs in puts:
            self.put(parent, bs, after)
        for bid in gone:
            self.n._req("DELETE", f"/blocks/{bid}")

    def check(self, parent: str, want: list[dict], table: str | None = None) -> None:
        """Read back: Assetizer's blocks under `parent` are those written, in order, all of them, folds and lists too."""
        got = []
        for b in self.n.children(parent):
            if table and _norm(b["id"]) == _norm(table):
                got.append(TABLE["type"])
            elif (x := self.old(b, _shape)) is not None:
                got.append(x)
        exp = [_new(b, _shape) for b in want]
        if got != exp:
            raise Problem("OTHER", f"Notion 读回 {_count(got)} 块，应为 {_count(exp)} 块")


# ------------------------------------------------------------------ writing
def _target_alive(n: Notion, target: dict) -> None:
    try:
        o = n._req("GET", f"/databases/{target['id']}") if target["kind"] == "database" else n.page(target["id"])
    except NotionError as e:
        raise _problem(e, target.get("title") or "") from e
    if o.get("archived"):
        raise Problem("GONE", f"「{target.get('title') or '那里'}」在 Notion 里已删除")


def _set(tid: int, **cols) -> None:
    db.exec(f"UPDATE topics SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", *cols.values(), tid)


def _h(x) -> str:
    return hashlib.sha1(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _props(n: Notion, page: dict, last: dict, want: dict) -> dict:
    """Write a page's properties but those someone changed in Notion since Assetizer last wrote them; returns what
    each property written came back as (what to tell a change by next time), the others as they were."""
    cur = page.get("properties") or {}
    edited = {p for p, h in _prop_hashes(cur).items() if p in last and last[p] != h}
    ids = {name: v.get("id") for name, v in cur.items() if isinstance(v, dict)}
    put = {k: v for k, v in want.items() if ids.get(k, k) not in edited}
    if not put:
        return last
    got = n._req("PATCH", f"/pages/{page['id']}", json={"properties": put}).get("properties") or {}
    done = {ids.get(k, k) for k in put} | {v.get("id") for k, v in got.items() if k in put and isinstance(v, dict)}
    return {**last, **{p: h for p, h in _prop_hashes(got).items() if p in done}}


def _fit(props: dict, schema: dict) -> dict:
    """The columns the table still has by name and kind (the title, whatever it is called now)."""
    out = {}
    for name, v in props.items():
        kind = next(iter(v))
        if kind == "title":
            name = next((k for k, p in schema.items() if p.get("type") == "title"), None)
        if name and (schema.get(name) or {}).get("type") == kind:
            out[name] = v
    return out


def _rows(n: Notion, s: Sync, page: Page, table: str) -> None:
    """The 来源 table: a row for each video read, with its notes inside; a column someone changed is theirs from then
    on. A video no longer in the topic is marked 移出: its row stays, and what anyone wrote in it."""
    tid = page.tid
    try:
        schema = n._req("GET", f"/databases/{table}").get("properties") or {}
        have = {_norm(r["id"]): r for r in n.rows(table) if not r.get("archived")}
    except NotionError as e:
        raise _problem(e, "来源表") from e
    rows = db.unj(db.one("SELECT notion_rows FROM topics WHERE id=?", tid)["notion_rows"], {}) or {}
    for r in page.rows:
        rec = rows.get(r["video_id"]) or {}
        want = _fit(page.props(r), schema)
        row = have.get(_norm(rec.get("page") or ""))
        if not row:  # new, or taken out in Notion
            got = n._req("POST", "/pages", json={"parent": {"database_id": table}, "properties": want})
            rec = rows[r["video_id"]] = {"page": got["id"], "want": _h(want), "props": _prop_hashes(got.get("properties"))}
            _set(tid, notion_rows=db.j(rows))
        elif _h(want) != rec.get("want"):
            rec["props"] = _props(n, row, rec.get("props") or {}, want)
            rec["want"] = _h(want)
        blocks = page.evidence(r)
        if rec.get("sig") != _h(blocks):
            s.write(rec["page"], blocks, n.children(rec["page"]) if row else [])
            s.check(rec["page"], blocks)
            rec["sig"] = _h(blocks)
            _set(tid, notion_rows=db.j(rows))
    now_in = {r["video_id"] for r in page.rows}
    for vid, rec in rows.items():
        if vid not in now_in and (row := have.get(_norm(rec.get("page") or ""))):
            want = _fit({"状态": {"select": {"name": "移出"}}}, schema)
            if want and _h(want) != rec.get("want"):
                rec["props"] = _props(n, row, rec.get("props") or {}, want)
                rec["want"] = _h(want)
    _set(tid, notion_rows=db.j(rows))


def write(tid: int, auto: bool = False) -> dict:
    """Write the topic's answer to its target: its page there, made the first time. Written again, Assetizer's blocks
    that changed are replaced and the rest left as they are (Sync), and so are the properties and the 来源 table's
    columns someone changed. `auto`: the worker after the batch's answer (a page deleted in Notion is not made again).
    {state, url?, error?}"""
    t = db.one("SELECT * FROM topics WHERE id=?", tid)
    target = db.unj(t["notion_target"], None) if t else None
    if not t or not target or not t["synth"]:
        if t:
            _set(tid, notion_state=None)
        return {"state": None}
    if not db.connect().execute("""UPDATE topics SET notion_state='RUNNING', notion_error=NULL
                                   WHERE id=? AND COALESCE(notion_state,'') != 'RUNNING'""", (tid,)).rowcount:
        return {"state": "RUNNING"}  # another write of it is under way (the page's, or the batch's answer)
    key = f"notion_props:topic-{tid}"
    try:
        n = _need()
        bot = _bot(n)
        if not bot:
            raise Problem("OTHER", "读不到课代表在 Notion 里的身份，分不出页上哪些是你写的；稍后再试")
        page = Page(tid)
        q, count = t["question"], len(page.read)
        pid = t["notion_page_id"]
        if pid:
            try:
                pg = n.page(pid)
            except NotionError as e:
                if e.status != 404:
                    raise
                pg = None
            if not pg or pg.get("archived"):  # deleted in Notion: back to 写进 Notion
                pid = None
                _set(tid, notion_page_id=None, notion_db=None, notion_rows=None)
                comments.forget(tid)
                if auto:
                    _set(tid, notion_state=None, notion_at=None)
                    return {"state": None, "gone": True}
        want = _topic_props(n, tid, target, q, count) if target["kind"] == "database" else {"title": {"title": [_t(q)]}}
        last = db.setting(key) or {}
        last = last if "want" in last else {"props": last}  # what 0.4.40 kept: the hashes alone
        if pid:
            if _h(want) != last.get("want"):
                db.set_setting(key, {"props": _props(n, pg, last.get("props") or {}, want), "want": _h(want)})
        else:
            _target_alive(n, target)
            if target["kind"] == "database":
                pid = _create(n, target["id"], want, {"type": "emoji", "emoji": ICON})["id"]
            else:
                pid = n.create_page(target["id"], q, ICON)["id"]
            _set(tid, notion_page_id=pid, notion_db=None, notion_rows=None)
            db.set_setting(key, {"props": _prop_hashes(n.page(pid).get("properties")), "want": _h(want)})
        s = Sync(n, bot)
        top, under = page.blocks()
        old = n.children(pid)
        table = t["notion_db"] if t["notion_page_id"] == pid else None
        cut = next((i for i, b in enumerate(old) if table and b["type"] == "child_database" and _norm(b["id"]) == _norm(table)), None)
        if cut is not None:
            s.write(pid, top, old[:cut])
            s.write(pid, under, old[cut + 1:], start=old[cut]["id"])
        else:  # the first time, or the table was taken off the page: the blocks above it, the table at the end, the rest
            s.write(pid, top, old)
            table = n.database(pid, "来源", COLUMNS)["id"]
            _set(tid, notion_db=table, notion_rows=None)
            s.put(pid, under, None)
        _rows(n, s, page, table)
        s.check(pid, [*top, TABLE, *under], table)
        _set(tid, notion_state="DONE", notion_error=None, notion_at=t["synth_at"])
        remember(target)
        try:  # the answer that took the comments in is on the page: what it did with them, in their threads (design §R8-13)
            comments.reply(n, tid)
        except Exception as e:  # noqa: BLE001 — the replies wait for the next write
            db.event("topic", f"专题 {tid} 回复 Notion 批注异常：{e}"[:300], level="warn")
        return {"state": "DONE", "url": page_url(pid)}
    except Exception as e:  # noqa: BLE001 — a write that did not happen is said on the topic's page, never an alert
        p = _problem(e, target.get("title") or "")
        err = {"kind": p.kind, "message": str(p)}
        _set(tid, notion_state="FAILED", notion_error=db.j(err))
        db.event("topic", f"专题 {tid} 没写进 Notion：{p}", level="warn")
        return {"state": "FAILED", "error": err}


def _topic_props(n: Notion, tid: int, target: dict, question: str, count: int) -> dict:
    """The title, and 「专题页」「期数」 where the database has them by that name and type."""
    try:
        schema = n._req("GET", f"/databases/{target['id']}").get("properties") or {}
    except NotionError as e:
        raise _problem(e, target.get("title") or "") from e
    out: dict = {}
    for name, p in schema.items():
        if p.get("type") == "title":
            out[name] = {"title": [{"type": "text", "text": {"content": question[:1900]}}]}
    if (schema.get("期数") or {}).get("type") == "number":
        out["期数"] = {"number": count}
    if (schema.get("专题页") or {}).get("type") == "url" and (link := notify.link(f"/topics/{tid}")):
        out["专题页"] = {"url": link}
    return out


def due() -> list[int]:
    """Topics asked to be written (on the topic's page, or left so by a restart)."""
    return [r["id"] for r in db.q("SELECT id FROM topics WHERE notion_state='QUEUED' ORDER BY id")]


def default() -> dict | None:
    return db.setting("topic_notion_default")


def state(t: dict) -> dict:
    """What the topic's page shows of it."""
    target = db.unj(t.get("notion_target"), None)
    pid = t.get("notion_page_id")
    return {"target": target, "url": page_url(pid) if pid else None, "state": t.get("notion_state"),
            "error": db.unj(t.get("notion_error"), None), "at": t.get("notion_at"),
            "stale": bool(pid and t.get("synth_at") and t.get("notion_at") != t.get("synth_at")), "token": bool(client()),
            "comments": t.get("comments_err") if pid else None}

