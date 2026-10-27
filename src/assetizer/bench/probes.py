"""Cheap probes (no golden media): how large a video the provider takes (C1), how long one answer can be and whether
an enforced schema holds on it (C4, C5). docs/benchmarks.md says which workaround each one stands behind."""
from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path

from ..llm import extract_json
from ..providers.agy import ProviderError, _canary_clip

SIZES_MB = (24, 48, 96)
OUTPUT_ITEMS = 300  # ~45k output tokens


def run_all(p, work: Path) -> dict:
    return {"media_size": media_size(p, work / "media-size"), "output_length": output_length(p, work / "output")}


def media_size(p, work: Path, sizes_mb=SIZES_MB) -> dict:
    """C1: a 10-second clip with a random number drawn in it, encoded CBR to each size. The answer must be the number."""
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for mb in sizes_mb:
        code = str(random.randint(1000, 9999))
        clip = work / f"canary-{mb}mb.mp4"
        try:
            _canary_clip(clip, code, mb * 1024 * 1024)
        except (OSError, subprocess.SubprocessError) as e:
            rows.append({"mb": mb, "ok": False, "error": f"CANARY_UNAVAILABLE: {type(e).__name__}"})
            continue
        row = {"mb": mb, "bytes": clip.stat().st_size}
        try:
            r = p.call(work / f"call-{mb}mb", "只回复画面中央的数字，不要别的文字。", kind="bench-media-size",
                       media=[str(clip)], effort="low", timeout="10m")
            row.update(ok=re.sub(r"\D", "", r.text) == code, wall=r.receipt.get("wall_seconds"),
                       input_tokens=(r.receipt.get("usage") or {}).get("input_tokens"))
            if not row["ok"]:
                row["error"] = "WRONG_ANSWER"
        except ProviderError as e:
            row.update(ok=False, error=f"{e.kind}: {str(e)[:160]}")
        clip.unlink(missing_ok=True)
        rows.append(row)
    ok = [r["mb"] for r in rows if r.get("ok")]
    return {"rows": rows, "largest_ok_mb": max(ok) if ok else None}


def output_length(p, work: Path, items: int = OUTPUT_ITEMS) -> dict:
    """C4/C5: one answer of `items` numbered entries (~150 tokens each), once with the schema enforced by the provider
    and once with the schema only in the prompt, as the large stages do today."""
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"n": {"type": "integer"}, "text": {"type": "string"}}, "required": ["n", "text"]}}},
        "required": ["items"]}
    ask = (f"输出一个 JSON 对象，字段 items 恰好 {items} 条，n 从 1 连续编号到 {items}；每条 text 用约 120 个汉字，"
           "各写一个不同的 K 线形态或交易纪律的学习要点。不要省略、不要合并、不要提前结束。")
    out = {}
    for mode in ("enforced", "embedded"):
        prompt = ask if mode == "enforced" else ask + "\n只输出 JSON，必须符合：\n" + json.dumps(schema, ensure_ascii=False)
        row: dict = {}
        try:
            r = p.call(work / mode, prompt, kind=f"bench-output-{mode}", schema=schema if mode == "enforced" else None,
                       effort="low", timeout="30m")
            u = r.receipt.get("usage") or {}
            row.update(ok=True, wall=r.receipt.get("wall_seconds"), output_tokens=u.get("output_tokens"))
            try:
                got = extract_json(r.text, ["items"]).get("items") or []
                ns = [x.get("n") for x in got if isinstance(x, dict)]
                row.update(items=len(got), complete=ns == list(range(1, items + 1)))
            except Exception as e:  # noqa: BLE001 -- an unparseable answer is the measurement
                row.update(items=0, complete=False, parse_error=f"{type(e).__name__}: {str(e)[:120]}")
        except ProviderError as e:
            row.update(ok=False, complete=False, error=f"{e.kind}: {str(e)[:160]}",
                       output_tokens=((e.receipt or {}).get("usage") or {}).get("output_tokens"))
        out[mode] = row
    return {"items_asked": items, **out}
