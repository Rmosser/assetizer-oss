"""Small shared helpers: JSON IO, hashing, timecodes."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def read_json(p: Path) -> Any:
    return json.loads(Path(p).read_text())


def write_json(p: Path, obj: Any) -> None:
    """Atomic write so a crash never leaves a half-written checkpoint."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def write_text(p: Path, text: str) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def sha256_file(p: Path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


_TC = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def parse_tc(v: Any) -> float:
    """Accept seconds (int/float/numeric str) or [H:]MM:SS(.fff)."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        raise ValueError(f"bad timecode {v!r}")
    s = v.strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = _TC.match(s)
    if not m:
        raise ValueError(f"bad timecode {v!r}")
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + float(se)


def fmt_tc(sec: float, millis: bool = False) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    if millis:
        return f"{h:02}:{m:02}:{s:06.3f}"
    return f"{h:02}:{m:02}:{int(s):02}"


def slug(s: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^\w\-]+", "-", s.strip().lower(), flags=re.U).strip("-")
    return s[:maxlen] or "item"


def disk_usage_ratio(path: Path) -> float:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return 1 - free / total if total else 0.0


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """Sortable 26-char id (ms timestamp + 80 random bits)."""
    import secrets
    import time as _t

    n = (int(_t.time() * 1000) << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return "".join(reversed(out))
