"""assetizer CLI."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from . import config, db


def _print(o) -> None:
    print(json.dumps(o, ensure_ascii=False, indent=2, default=str))


def cmd_init(a) -> None:
    from . import seed
    config.get().ensure_dirs()
    _print(seed.run())


def cmd_import(a) -> None:
    from .importer import run14
    _print({"run_id": run14(a.path)})


def cmd_submit(a) -> None:
    from .pipeline.runner import submit
    _print(submit(a.url, priority=a.priority))


def cmd_worker(a) -> None:
    from .pipeline.runner import Worker
    Worker().loop()


def cmd_run(a) -> None:
    from .pipeline.runner import execute
    _print({"state": execute(a.run_id)})


def cmd_web(a) -> None:
    import uvicorn
    s = config.get()
    uvicorn.run("assetizer.web.app:app", host=a.host or s.web_host, port=a.port or s.web_port, log_level="info")


def cmd_deliver(a) -> None:
    from .delivery import deliver
    _print(deliver(a.source_id))


def cmd_notion_target(a) -> None:
    """Point a channel at a Notion database and fill the mapping from its preset (names → property ids)."""
    from .delivery import client
    from .notion import mapping as M
    n = client()
    if not n:
        sys.exit("no Notion token")
    if a.clear:
        db.exec("DELETE FROM settings WHERE key=?", M.key(a.channel))
        _print({"cleared": a.channel})
        return
    dbid = M.norm_id(a.database)
    dbo = n._req("GET", f"/databases/{dbid}")
    schema = M.schema_of(dbo)
    cfg = M.preset(a.channel, dbid, schema, a.parent)
    cfg["database_title"] = "".join(t.get("plain_text", "") for t in dbo.get("title", []))
    M.save(a.channel, cfg)
    _print({"channel": a.channel, **cfg, "problems": M.problems(cfg, schema)})


def cmd_cookies(a) -> None:
    """Read YouTube cookies from CookieCloud once and report what came back (never the values)."""
    from . import cookies as C
    _print({"configured": C.configured(), "origin": C.origin(), "domains": C.domains(), **C.probe()})


def cmd_set(a) -> None:
    v = a.value
    try:
        v = json.loads(v)
    except ValueError:
        pass
    db.set_setting(a.key, v)
    _print({a.key: db.setting(a.key)})


def cmd_status(a) -> None:
    _print({
        "paused": db.setting("queue_paused"),
        "runs": db.q("SELECT id, production, stage, wait_reason, error_class FROM runs ORDER BY created_at DESC LIMIT 10"),
        "alerts": db.q("SELECT key, title FROM alerts WHERE resolved_at IS NULL"),
    })


def cmd_doctor(a) -> None:
    s = config.get()
    out = {"home": str(s.home), "db": str(s.db_path)}
    for tool in ("ffmpeg", "ffprobe", s.agy_bin, s.ytdlp_bin):
        out[tool] = shutil.which(tool) or "MISSING"
    out["disk_free_gb"] = round(shutil.disk_usage(s.home).free / 1e9, 1)
    out["notion_token"] = bool(s.secret("notion-token"))
    out["browser_profile"] = str(s.browser_profile) if s.browser_profile and s.browser_profile.exists() else "MISSING"
    if a.agy:
        from .providers.agy import AgyProvider
        out["agy"] = AgyProvider().health()
    _print(out)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="assetizer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    x = sub.add_parser("import-run14"); x.add_argument("path"); x.set_defaults(fn=cmd_import)
    x = sub.add_parser("submit"); x.add_argument("url"); x.add_argument("--priority", type=int, default=0); x.set_defaults(fn=cmd_submit)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    x = sub.add_parser("run"); x.add_argument("run_id"); x.set_defaults(fn=cmd_run)
    x = sub.add_parser("web"); x.add_argument("--host"); x.add_argument("--port", type=int); x.set_defaults(fn=cmd_web)
    x = sub.add_parser("deliver"); x.add_argument("source_id"); x.set_defaults(fn=cmd_deliver)
    x = sub.add_parser("notion-target"); x.add_argument("channel"); x.add_argument("database", nargs="?", default="")
    x.add_argument("--parent", help="上级页面（关联属性的固定值）"); x.add_argument("--clear", action="store_true")
    x.set_defaults(fn=cmd_notion_target)
    sub.add_parser("cookies").set_defaults(fn=cmd_cookies)
    x = sub.add_parser("set"); x.add_argument("key"); x.add_argument("value"); x.set_defaults(fn=cmd_set)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    x = sub.add_parser("doctor"); x.add_argument("--agy", action="store_true"); x.set_defaults(fn=cmd_doctor)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
