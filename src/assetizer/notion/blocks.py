"""Markdown → Notion blocks (the subset our article writer produces)."""
from __future__ import annotations

import re

MAX_TEXT = 1900
MAX_PIECES = 100   # pieces of rich_text in a block, rows in a table, blocks in a request: Notion's caps
_INLINE = re.compile(r"(\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*]))")


def rich(text: str, **ann) -> list[dict]:
    """Inline markdown → rich_text, splitting long runs under Notion's 2000-char cap; at most the 100 pieces a block
    takes (``runs`` keeps them all, for a caller that spreads a text over blocks)."""
    return runs(text, **ann)[:MAX_PIECES]


def runs(text: str, **ann) -> list[dict]:
    """Inline markdown → every piece of rich_text it takes."""
    out: list[dict] = []

    def push(t: str, link: str | None = None, **a):
        for i in range(0, len(t), MAX_TEXT):
            item: dict = {"type": "text", "text": {"content": t[i:i + MAX_TEXT]}}
            if link:
                item["text"]["link"] = {"url": link}
            merged = {**ann, **a}
            if merged:
                item["annotations"] = merged
            out.append(item)

    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            push(text[pos:m.start()])
        if m.group(2) is not None:
            push(m.group(2), bold=True)
        elif m.group(3) is not None:
            push(m.group(3), code=True)
        elif m.group(4) is not None:
            push(m.group(4), link=m.group(5))
        elif m.group(6) is not None:
            push(m.group(6), italic=True)
        pos = m.end()
    if pos < len(text):
        push(text[pos:])
    if not out:
        out = [{"type": "text", "text": {"content": ""}}]
    return out


def block(kind: str, text: str, inline=rich, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": inline(text), **extra}}


def spread(kind: str, pieces: list[dict], **extra) -> list[dict]:
    """A block of `kind` for every 100 pieces of rich_text: a long text goes on in the next block, nothing is cut."""
    pieces = pieces or [{"type": "text", "text": {"content": ""}}]
    return [{"object": "block", "type": kind, kind: {"rich_text": pieces[i:i + MAX_PIECES], **extra}}
            for i in range(0, len(pieces), MAX_PIECES)]


def callout(text: str, emoji: str = "🧭", color: str = "gray_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rich(text), "icon": {"type": "emoji", "emoji": emoji}, "color": color}}


def toggle(title: str, children: list[dict]) -> dict:
    return {"object": "block", "type": "toggle", "toggle": {"rich_text": rich(title), "children": children[:100]}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def media(kind: str, upload_id: str, caption: str) -> dict:
    return {"object": "block", "type": kind,
            kind: {"type": "file_upload", "file_upload": {"id": upload_id}, "caption": rich(caption)}}


_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def markdown(md: str, inline=rich, whole: bool = False) -> list[dict]:
    """`inline`: text → rich_text (rich, or one that also turns the text's citations into links). `whole` (专题):
    nothing is cut — a list item goes under the one it is indented under (three levels: Notion takes two levels of
    nesting in a request, deeper ones join the third), a text past 100 pieces goes on in the next block, a table past
    100 rows in the next table under the same header."""
    lines = md.splitlines()
    out: list[dict] = []
    stack: list[tuple[int, dict]] = []  # whole: the list items above, by indent, that an indented line goes under

    def make(kind: str, text: str, **extra) -> list[dict]:
        return spread(kind, inline(text), **extra) if whole else [block(kind, text, inline, **extra)]

    def under(indent: int) -> list[dict]:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        del stack[2:]
        if not stack:
            return out
        item = stack[-1][1]
        return item[item["type"]].setdefault("children", [])

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line or line.startswith("<!--"):
            continue
        flat = raw.expandtabs(4)
        indent = len(flat) - len(flat.lstrip())
        if line.startswith("```"):
            stack.clear()
            lang = line[3:].strip() or "plain text"
            body = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            out.append({"object": "block", "type": "code",
                        "code": {"rich_text": rich("\n".join(body)), "language": "plain text" if lang not in _LANGS else lang}})
            continue
        if line.startswith("|") and i < len(lines) and _TABLE_SEP.match(lines[i].strip()):
            stack.clear()
            rows = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            w = max(map(len, cells))
            cells = [r + [""] * (w - len(r)) for r in cells]
            parts = ([[cells[0], *cells[1:][k:k + MAX_PIECES - 1]] for k in range(0, max(len(cells) - 1, 1), MAX_PIECES - 1)]
                     if whole else [cells[:MAX_PIECES]])
            for part in parts:
                out.append({"object": "block", "type": "table", "table": {
                    "table_width": w, "has_column_header": True, "has_row_header": False,
                    "children": [{"object": "block", "type": "table_row", "table_row": {"cells": [inline(c) for c in r]}}
                                 for r in part]}})
            continue
        item = None
        if m := re.match(r"^(#{1,6})\s+(.+)", line):
            stack.clear()
            out += make(f"heading_{min(3, len(m[1]))}", m[2].strip())
        elif re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            stack.clear()
            out.append(divider())
        elif line.startswith(">"):
            stack.clear()
            out += make("quote", line.lstrip("> ").strip())
        elif m := re.match(r"^[-*+]\s+\[( |x)\]\s+(.+)", line):
            item = make("to_do", m[2], checked=m[1] == "x")
        elif m := re.match(r"^[-*+]\s+(.+)", line):
            item = make("bulleted_list_item", m[1])
        elif m := re.match(r"^\d+[.)]\s+(.+)", line):
            item = make("numbered_list_item", m[1])
        elif whole and indent and stack:  # a list item's next line or paragraph
            under(indent).extend(make("paragraph", line))
        else:
            stack.clear()
            out += make("paragraph", line)
        if item and whole:
            under(indent).extend(item)
            stack.append((indent, item[-1]))
        elif item:
            out += item
    return out


def plain(b: dict) -> str:
    t = b["type"]
    body = b.get(t, {})
    key = "rich_text" if "rich_text" in body else "caption" if "caption" in body else None
    if not key:
        return ""
    return "".join((x.get("plain_text") if "plain_text" in x else x["text"]["content"]) for x in body[key])


_LANGS = {"python", "json", "javascript", "typescript", "bash", "shell", "markdown", "yaml", "sql", "plain text"}
