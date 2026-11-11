"""Write generated Notion blocks among blocks people wrote, edited or commented on."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import blocks as B
from . import mapping as M
from .client import Notion, NotionError


def _norm(i: str) -> str:
    try:
        return M.norm_id(i)
    except ValueError:
        return i


class Problem(RuntimeError):
    """Why a write did not happen, by what the user can do: TOKEN 去设置, NO_ACCESS 重试, GONE 换地方, OTHER 重试."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _said(pieces: list[dict]) -> list:
    out = []
    for x in pieces:
        kind = x.get("type", "text")
        text = x.get("plain_text", (x.get("text") or {}).get("content", (x.get("equation") or {}).get("expression", "")))
        url = x.get("href") or ((x.get("text") or {}).get("link") or {}).get("url")
        annotations = {k: v for k, v in sorted((x.get("annotations") or {}).items()) if v not in (False, None, "default")}
        if out and out[-1][0] == kind and out[-1][2:] == [url, annotations]:
            out[-1][1] += text
        else:
            out.append([kind, text, url, annotations])
    return out


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
    if how is _sig and b.get("_plan", {}).get("media"):
        return b["_plan"]["hash"]
    return how(b, [_new(k, how) for k in _kids(b)])


def plan_hash(item: dict) -> str:
    """Media identity survives a new run's paths and Notion's expiring file URLs; old short hashes never match it."""
    if not item.get("media"):
        return _new(item["block"])
    m = item["media"]
    with Path(m["path"]).open("rb") as f:
        sha = hashlib.file_digest(f, "sha256").hexdigest()
    return "media:" + hashlib.sha256(json.dumps([m["kind"], m["caption"], sha], ensure_ascii=False).encode()).hexdigest()


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
    segments = _segments(old, sig, new, pairs, start)
    return [(after, bs) for after, bs, _ in segments if bs], [bid for _, _, ids in segments for bid in ids]


def _segments(old, sig, new, pairs, start):
    """Each old run and the blocks that replace it; the topic caller still writes all runs before deleting."""
    segments = []
    po, pn = -1, -1
    for oi, ni in [*pairs, (len(old), len(new))]:
        runs: list[list[dict]] = []
        run: list[dict] = []
        for i in range(po + 1, oi):
            if sig[i] is not None:
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
                segments.append((r[-1]["id"], fresh[k:k + take], [b["id"] for b in r]))
                k += take
        elif fresh:
            segments.append((old[po]["id"] if po >= 0 else start or (old[oi - 1]["id"] if oi > 0 else None), fresh, []))
        else:
            segments.extend((r[-1]["id"], [], [b["id"] for b in r]) for r in runs)
        po, pn = oi, ni
    return segments


class Sync:
    """Writes Assetizer's blocks under a page among everyone else's: its blocks that say the same stay (and the
    comments on them), those that changed or went are taken out — unless someone edited them, wrote in them or
    commented on them: those stay as theirs — and the new ones go where the old ones were. Nothing of anyone else's is
    moved or taken out."""

    def __init__(self, n: Notion, bot: str, *, hashes: dict[str, str] | None = None, checkpoint=None, forget=None,
                 authors: set[str] | None = None, keys: dict[str, str] | None = None):
        """Channel plans opt in with hashes by block id and durable checkpoint(items, returned_blocks)/forget(id).
        Plain block callers retain the topic writer's batching, signatures and comment behavior."""
        self.n, self.bot = n, bot
        self.hashes, self.checkpoint, self.forget = hashes, checkpoint, forget
        self.authors, self.keys = {bot} | (authors or set()), keys or {}
        self._children: dict[str, list[dict]] = {}
        self._ids: dict[str, set[str]] = {}
        self.written = self.retained = self.deleted = self.uploads = 0
        self.comments = True          # Notion lets the integration read comments (else none can be seen)
        self.kept: set[str] = set()   # Assetizer's blocks kept for someone's comment on them

    def children(self, parent: str) -> list[dict]:
        if parent not in self._children:
            self._children[parent] = self.n.children(parent)
        return self._children[parent]

    def old(self, b: dict, how=_sig) -> str | None:
        """What a block of Assetizer's says, with all in it; None when it is not as Assetizer left it."""
        if ((b.get("created_by") or {}).get("id") not in self.authors
                or (b.get("last_edited_by") or {}).get("id") not in self.authors | ({None} if self.hashes is None else set())
                or b["id"] in self.kept or b["type"] in ("child_page", "child_database")):
            return None
        kids = []
        if b.get("has_children"):
            for k in self.children(b["id"]):
                x = self.old(k, how)
                if x is None:
                    return None
                kids.append(x)
        if how is _sig and self.hashes is not None and b["type"] in ("image", "video"):
            h = self.hashes.get(b["id"], "")
            return h if h.startswith("media:") else f"unknown-media:{b['id']}"
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

    def put(self, parent: str, blocks: list[dict], after: str | None, *, at_start: bool = False) -> str | None:
        """After `after` the later requests go in first, each right after it, so they end up in order."""
        if self.checkpoint is not None:
            # At most 40 blocks, ending at a video; images may share the batch with other blocks. Upload immediately
            # before append, then advance the anchor after checkpointing the acknowledged batch in page order.
            for part in _batches(blocks):
                pending = []
                for b in part:
                    pending.append(b)
                    if len(pending) == 40 or b["_plan"].get("media", {}).get("kind") == "video":
                        after = self._put_plan(parent, pending, after, at_start=at_start)
                        at_start = False
                        pending = []
                if pending:
                    after = self._put_plan(parent, pending, after, at_start=at_start)
                    at_start = False
            return after
        parts = list(_batches(blocks))
        if at_start:
            for p in parts:
                kw = {"position": {"type": "start"}} if at_start else {"after": after}
                got = self.n.append(parent, p, **kw)["results"]
                self._children.pop(parent, None)
                after, at_start = got[len(p) - 1]["id"], False
            return after
        for p in reversed(parts) if after else parts:
            self.n.append(parent, p, after=after)
            self._children.pop(parent, None)
        return after

    def _put_plan(self, parent: str, blocks: list[dict], after: str | None, *, at_start: bool = False) -> str:
        items, body = [b["_plan"] for b in blocks], []
        for b, it in zip(blocks, items):
            if m := it.get("media"):
                up = self.n.upload(Path(m["path"]), "video/mp4" if m["kind"] == "video" else "image/jpeg")
                self.uploads += 1
                body.append(B.media(m["kind"], up["id"], m["caption"]))
            else:
                body.append({k: v for k, v in b.items() if k != "_plan"})
        kw = {"position": {"type": "start"}} if at_start else {"after": after}
        got = self.n.append(parent, body, **kw)["results"][:len(body)]
        self._children.pop(parent, None)
        ids = [b.get("id") for b in got]
        if (len(got) != len(body) or len(set(ids)) != len(body) or not all(ids)
                or set(ids) & self._ids[parent] or [b.get("type") for b in got] != [b["type"] for b in body]):
            raise RuntimeError("READBACK_MISMATCH append 回答的新块不符")
        self.checkpoint(items, got)  # before another request, including uploads, deletions or readback
        self._ids[parent].update(ids)
        self.hashes.update({b["id"]: it["hash"] for it, b in zip(items, got)})
        self.written += len(got)
        return got[-1]["id"]

    def commented(self, b: dict) -> bool:
        """A channel fold must survive a comment on any descendant, too. Topic behavior stays as before."""
        return self.said(b["id"]) or (self.hashes is not None and b.get("has_children")
                                      and any(self.commented(k) for k in self.children(b["id"])))

    def write_plan(self, parent: str, plan: list[dict], old: list[dict]) -> list[dict]:
        """Adapt keyed, lazily uploaded channel items to the same placement algorithm as ordinary topic blocks."""
        new = [{**(it.get("block") or {"type": it["media"]["kind"]}), "_plan": it} for it in plan]
        self.write(parent, new, old)
        return new

    def write(self, parent: str, new: list[dict], old: list[dict], start: str | None = None) -> None:
        """`old`, the blocks under `parent` now in order, made `new` where they are Assetizer's; `start`: the block just
        before them (the 来源 table, for those under it)."""
        if self.checkpoint is not None:  # topic `old` can be only the part above/below its 来源 table
            self._children[parent] = old
        else:
            self._children.pop(parent, None)
        self._ids[parent] = {b["id"] for b in old}
        sig = [self.old(b) for b in old]
        pairs = _lcs(sig, [_new(b) for b in new])
        kept = {i for i, _ in pairs}
        if self.checkpoint is not None:
            self.checkpoint([new[j]["_plan"] for _, j in pairs], [old[i] for i, _ in pairs])
        for i, b in enumerate(old):
            if sig[i] is not None and i not in kept and self.commented(b):
                sig[i] = None
                self.kept.add(b["id"])
        if self.checkpoint is not None:
            self._write_channel(parent, old, sig, new, pairs, start)
            return
        puts, gone = _place(old, sig, new, pairs, start)
        for after, bs in puts:
            self.put(parent, bs, after, at_start=after is None and bool(old))
        for bid in gone:
            self._delete(parent, bid)

    def _write_channel(self, parent, old, sig, new, pairs, start):
        # Protected, keyed blocks are placement boundaries too: their own correction goes immediately after them.
        by_key = {b["_plan"]["key"]: j for j, b in enumerate(new)}
        boundaries, protected = list(pairs), set()
        for i, b in enumerate(old):
            if sig[i] is not None or (b.get("created_by") or {}).get("id") not in self.authors:
                continue
            j = by_key.get(self.keys.get(b["id"]))
            if j is not None and all((i < oi and j < nj) or (i > oi and j > nj) for oi, nj in boundaries):
                boundaries.append((i, j))
                protected.add(i)
        boundaries.sort()
        segments, po, pn = [], -1, -1
        for oi, ni in [*boundaries, (len(old), len(new))]:
            # Use the ordinary placement rules inside each bounded interval, retaining user block positions.
            local_old, local_new = old[po + 1:oi], new[pn + 1:ni]
            anchor = old[po]["id"] if po >= 0 else start
            segments.extend(_segments(local_old, sig[po + 1:oi], local_new, [], anchor))
            if oi in protected:
                segments.append((old[oi]["id"], [new[ni]], []))
            po, pn = oi, ni
        gone = {bid for _, _, ids in segments for bid in ids}
        self.retained += sum((b.get("created_by") or {}).get("id") in self.authors
                             and b["type"] not in ("child_page", "child_database") and b["id"] not in gone for b in old)
        corrected = {}
        for after, bs, ids in segments:
            if bs:
                last = self.put(parent, bs, corrected.get(after, after), at_start=after is None and bool(old))
                if after in {old[i]["id"] for i in protected}:
                    corrected[after] = last
            for bid in ids:
                self._delete(parent, bid)

    def _delete(self, parent: str, bid: str) -> None:
        self.n._req("DELETE", f"/blocks/{bid}")
        self._children.pop(parent, None)
        self._ids[parent].discard(bid)
        if self.forget is not None:
            self.forget(bid)
        self.deleted += 1

    def check(self, parent: str, want: list[dict], table: str | None = None) -> None:
        """Read back: Assetizer's blocks under `parent` are those written, in order, all of them, folds and lists too."""
        got = []
        for b in self.children(parent):
            if table and _norm(b["id"]) == _norm(table):
                got.append("child_database")
            elif (x := self.old(b, _shape)) is not None:
                got.append(x)
                if self.hashes is not None and b["type"] in ("image", "video") and b[b["type"]].get("type") != "file":
                    raise RuntimeError("READBACK_MEDIA")
        exp = [_new(b, _shape) for b in want]
        if got != exp:
            raise Problem("OTHER", f"Notion 读回 {_count(got)} 块，应为 {_count(exp)} 块")

