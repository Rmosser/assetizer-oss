"""Markdown → Notion blocks (the subset our article writer produces)."""
from __future__ import annotations

import re

MAX_TEXT = 1900
_INLINE = re.compile(r"(\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*]))")


def rich(text: str, **ann) -> list[dict]:
    """Inline markdown → rich_text, splitting long runs under Notion's 2000-char cap."""
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
    return out[:100]


def block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text), **extra}}


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


def markdown(md: str) -> list[dict]:
    lines = md.splitlines()
    out: list[dict] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("```"):
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
            rows = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            w = max(map(len, cells))
            out.append({"object": "block", "type": "table", "table": {
                "table_width": w, "has_column_header": True, "has_row_header": False,
                "children": [{"object": "block", "type": "table_row",
                              "table_row": {"cells": [rich(c) for c in r + [""] * (w - len(r))]}} for r in cells[:100]]}})
            continue
        if m := re.match(r"^(#{1,6})\s+(.+)", line):
            out.append(block(f"heading_{min(3, len(m[1]))}", m[2].strip()))
        elif re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            out.append(divider())
        elif line.startswith(">"):
            out.append(block("quote", line.lstrip("> ").strip()))
        elif m := re.match(r"^[-*+]\s+\[( |x)\]\s+(.+)", line):
            out.append(block("to_do", m[2], checked=m[1] == "x"))
        elif m := re.match(r"^[-*+]\s+(.+)", line):
            out.append(block("bulleted_list_item", m[1]))
        elif m := re.match(r"^\d+[.)]\s+(.+)", line):
            out.append(block("numbered_list_item", m[1]))
        else:
            out.append(block("paragraph", line))
    return out


def plain(b: dict) -> str:
    t = b["type"]
    body = b.get(t, {})
    key = "rich_text" if "rich_text" in body else "caption" if "caption" in body else None
    if not key:
        return ""
    return "".join((x.get("plain_text") if "plain_text" in x else x["text"]["content"]) for x in body[key])


_LANGS = {"python", "json", "javascript", "typescript", "bash", "shell", "markdown", "yaml", "sql", "plain text"}
