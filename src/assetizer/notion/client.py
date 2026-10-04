"""Minimal Notion REST client: pages, blocks, file uploads (single + multipart)."""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import httpx

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
SINGLE_PART_MAX = 20 * 1024 * 1024
PART_SIZE = 10 * 1024 * 1024


class NotionError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Notion {status}: {body[:400]}")
        self.status = status
        self.body = body


class Notion:
    def __init__(self, token: str, timeout: float = 120):
        self.c = httpx.Client(base_url=API, timeout=timeout, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": VERSION})

    def _req(self, method: str, path: str, idempotent: bool | None = None, **kw) -> Any:
        """Retry 429 always; retry 5xx/transport errors only for idempotent requests.
        A timed-out page create or block append has usually been applied — the next
        delivery reconciles instead of blindly duplicating it."""
        if idempotent is None:
            idempotent = method in ("GET", "DELETE") or "/send" in path or path.endswith("/complete")
        for attempt in range(6):
            try:
                r = self.c.request(method, path, **kw)
            except httpx.TransportError:
                if attempt == 5 or not idempotent:
                    raise
                time.sleep(2 ** attempt)
                continue
            if (r.status_code == 429 or (r.status_code in (500, 502, 503, 504) and idempotent)) and attempt < 5:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                raise NotionError(r.status_code, r.text)
            return r.json()
        raise NotionError(599, "retries exhausted")

    # pages / blocks ---------------------------------------------------------
    def me(self) -> dict:
        return self._req("GET", "/users/me")

    def page(self, page_id: str) -> dict:
        return self._req("GET", f"/pages/{page_id}")

    def create_page(self, parent_page_id: str, title: str, icon: str | None = None,
                    children: list | None = None) -> dict:
        body: dict = {"parent": {"page_id": parent_page_id},
                      "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:1900]}}]}}}
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        if children:
            body["children"] = children
        return self._req("POST", "/pages", json=body)

    def archive_page(self, page_id: str) -> dict:
        return self._req("PATCH", f"/pages/{page_id}", idempotent=True, json={"archived": True})

    def append(self, block_id: str, children: list) -> dict:
        return self._req("PATCH", f"/blocks/{block_id}/children", json={"children": children})

    def update_block(self, block_id: str, body: dict) -> dict:
        return self._req("PATCH", f"/blocks/{block_id}", json=body)

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            q = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            z = self._req("GET", f"/blocks/{block_id}/children{q}")
            out += z["results"]
            if not z.get("has_more"):
                return out
            cursor = z["next_cursor"]

    # uploads ----------------------------------------------------------------
    def upload(self, path: Path, content_type: str) -> dict:
        size = path.stat().st_size
        if size <= SINGLE_PART_MAX:
            fu = self._req("POST", "/file_uploads", json={"filename": path.name, "content_type": content_type})
            with path.open("rb") as f:
                self._req("POST", f"/file_uploads/{fu['id']}/send",
                          files={"file": (path.name, f, content_type)})
        else:
            parts = math.ceil(size / PART_SIZE)
            fu = self._req("POST", "/file_uploads", json={
                "filename": path.name, "content_type": content_type,
                "mode": "multi_part", "number_of_parts": parts})
            with path.open("rb") as f:
                for n in range(1, parts + 1):
                    chunk = f.read(PART_SIZE)
                    self._req("POST", f"/file_uploads/{fu['id']}/send",
                              files={"file": (path.name, chunk, content_type)},
                              data={"part_number": str(n)})
            self._req("POST", f"/file_uploads/{fu['id']}/complete")
        done = self._req("GET", f"/file_uploads/{fu['id']}")
        if done.get("status") != "uploaded":
            raise NotionError(0, f"upload not completed: {done.get('status')}")
        return done
