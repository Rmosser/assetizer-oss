"""Weekly benchmark: re-test the outside limits our workarounds exist for, so the pipeline can drop a workaround once
its limit is gone. The registry of workarounds, limits and probes is docs/benchmarks.md.

    python -m assetizer.cli bench                # probes + quality on the golden set, with the box's provider
    python -m assetizer.cli bench --only probes  # the cheap ones

Probes need no media. Quality checks run on the golden set (bench/golden/*.json): videos with checked answers.
Every run writes <home>/bench/reports/<date>.json and .md and compares with the previous report.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config

GOLDEN_DIR = Path(__file__).parent / "golden"


def home() -> Path:
    return config.get().home / "bench"


def golden() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(GOLDEN_DIR.glob("*.json"))]


def run(only: str | None = None, provider=None) -> dict:
    from .. import providers
    from . import probes, quality, report
    p = provider or providers.get()
    stamp = time.strftime("%Y-%m-%d_%H%M")
    work = home() / "runs" / stamp
    result: dict = {"stamp": stamp, "provider": p.version(), "probes": {}, "quality": {}}
    if only in (None, "probes"):
        result["probes"] = probes.run_all(p, work / "probes")
    if only in (None, "quality"):
        result["quality"] = {g["id"]: quality.understanding(p, g, work / "quality" / g["id"]) for g in golden()}
    return report.write(result, home() / "reports")
