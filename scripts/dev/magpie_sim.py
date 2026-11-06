#!/usr/bin/env python3
"""本地体验《课代表》接 magpie 的整条流程：不碰生产、不调模型、不花钱。

起一个假的 magpie（接口形状照 v0.1.2890 实测）、一个真的《课代表》网页和真的 worker，数据放在 var/magpie-sim：

    .venv/bin/python scripts/dev/magpie_sim.py            # 机房 http://127.0.0.1:8799/engine
    .venv/bin/python scripts/dev/magpie_sim.py --reset    # 从头来

控制台 http://127.0.0.1:8798 切场景（连不上、Key 受限、Key 失效、账号掉线）、调额度、改勾选、印一期、
排补录、让 CookieCloud 读不到、造几期失败。
真的：网关读取、分工路由、receipt、额度暂停与恢复、补录派发、登录态的读取与告警、统计、上报。
假的：agy、CookieCloud 只读入口、下载和媒体处理，以及模型的回答。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
DEFAULT_HOME = ROOT / "var" / "magpie-sim"
KEY = "sk-sim-local"
INGEST_FIELDS = {"t", "agent", "provider", "model", "in", "out", "cache_read", "cache_write", "cache_write_1h", "reasoning", "ms", "ttft_ms",
                 "first_text_ms", "flow_ms", "status", "cost", "effort", "session", "err", "rid"}


def utc(**kw) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kw)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def _model(mid: str, name: str, vendor_label: str, context: int, levels: list[str], inputs: list[str], *, owner: str | None = None) -> dict:
    return {"id": mid, "object": "model", "type": "model", "display_name": name, "magpie_label": f"{name} · {vendor_label}",
            "owned_by": owner or mid.split("/")[0], "context_window": context, "context_length": context, "max_input_tokens": context,
            "max_output_tokens": 128000, "modalities": {"input": inputs}, "reasoning": bool(levels), "reasoning_levels": levels,
            "supported_reasoning_levels": [{"effort": x} for x in levels]}


CLAUDE, GPT, GROK = ["low", "medium", "high", "xhigh", "max"], ["low", "medium", "high", "xhigh", "max", "ultra"], ["low", "medium", "high", "xhigh"]
TI, TIP = ["text", "image"], ["text", "image", "pdf"]
MODELS = [
    _model("claude/claude-haiku-5-5", "Claude Haiku 5.5", "Claude Code", 1000000, CLAUDE, TIP),
    _model("claude/claude-sonnet-5-5", "Claude Sonnet 5.5", "Claude Code", 1000000, CLAUDE, TIP),
    _model("claude/claude-opus-5-5", "Claude Opus 5.5", "Claude Code", 1000000, CLAUDE, TIP),
    _model("codex/gpt-6.1-sol", "GPT-6.1-Sol", "Codex", 500000, GPT, TI),
    _model("codex/gpt-6-astra", "GPT-6-Astra", "Codex", 272000, GPT, TI),
    _model("codex/gpt-6.1-terra", "GPT-6.1 Terra", "Codex", 500000, GPT, TI),
    _model("grok/grok-4.7", "Grok 4.7", "Grok", 256000, GROK, TI),
    _model("grok/grok-4.7@500k", "Grok 4.7 @ 500k", "Grok", 500000, GROK, TI),
    _model("grok/grok-4.6", "Grok 4.6", "Grok", 256000, GROK, TI),
    _model("grok/grok-4.6@500k", "Grok 4.6 @ 500k", "Grok", 500000, GROK, TI),
]
GROUP = _model("group/text-fast", "文本快线", "分组", 500000, ["low", "medium", "high"], TI, owner="codex")
SCENARIOS = {"ok": "正常", "down": "连不上 magpie", "held": "Key 受限", "bad_key": "Key 失效", "signed_out": "ChatGPT 账号掉线"}
SUBS = {"codex": "ChatGPT", "claude": "Claude Code", "grok": "Grok", "gemini": "Gemini（agy）"}


class Sim:
    """What the fake magpie holds, and Gemini's quota as the fake agy tells it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.scenario = "ok"
        self.ticked = {m["id"] for m in MODELS}
        self.group = False
        self.drain = 2.0  # points of the week a model call takes
        self.windows = {
            "codex": {"plan": "prolite", "name": "Codex", "w": {"7 days": {"used": 27.0, "resets": utc(days=6)}}},
            "claude": {"plan": "pro", "name": "Claude Code", "w": {"5 hours": {"used": 0.0, "resets": utc(hours=4, minutes=20)},
                                                                   "7 days": {"used": 3.0, "resets": utc(days=1)}}},
            "grok": {"plan": None, "name": "Grok", "w": {"7 days": {"used": 0.0, "resets": utc(days=3)}}},
        }
        self.gemini = {"5h": 0.97, "weekly": 0.85}
        self.cookies = "ok"
        self.port = 8798
        self.history: dict[str, list] = {}
        self.requests: list[dict] = []
        self.reported: dict[str, dict] = {}
        # the week so far, so the cards have a trend and 约够印 N 期 something to go by
        for p, start in (("codex", 88.0), ("claude", 99.0), ("grok", 100.0)):
            end = 100 - self.windows[p]["w"]["7 days"]["used"]
            self.history[p] = [{"at": iso(utc(hours=-6 * (28 - i))), "left": round(start + (end - start) * i / 28 + random.uniform(-0.3, 0.3), 1)} for i in range(29)]

    def note(self, **kw) -> None:
        with self.lock:
            self.requests.insert(0, {"at": datetime.now().strftime("%H:%M:%S"), **kw})
            del self.requests[60:]

    def set_left(self, sub: str, left: float) -> None:
        with self.lock:
            if sub == "gemini":
                self.gemini = {"5h": max(self.gemini["5h"], left / 100) if left > 0 else 0.0, "weekly": left / 100}
                return
            for w in self.windows[sub]["w"].values():
                w["used"] = 100 - left
            self.history[sub].append({"at": iso(utc()), "left": left})

    def spend(self, sub: str) -> None:
        with self.lock:
            for name, w in self.windows[sub]["w"].items():
                w["used"] = min(100.0, w["used"] + self.drain * (3 if name == "5 hours" else 1))
            self.history[sub].append({"at": iso(utc()), "left": round(100 - self.windows[sub]["w"]["7 days"]["used"], 1)})

    def out(self, sub: str) -> bool:
        return any(w["used"] >= 100 for w in self.windows[sub]["w"].values())

    def catalog(self) -> list[dict]:
        return [m for m in MODELS if m["id"] in self.ticked] + ([GROUP] if self.group else [])

    def quotas(self) -> list[dict]:
        out = []
        for p, s in self.windows.items():
            q = {"provider": p, "name": s["name"], "kind": "subscription", "user": f"{p}@example.com", "readAt": iso(utc(minutes=-1)),
                 "windows": [{"name": n, "used": round(w["used"], 1), "remaining": round(100 - w["used"], 1), "resetsAt": iso(w["resets"])}
                             for n, w in s["w"].items()]}
            if s["plan"]:
                q["plan"] = s["plan"]
            if p == "codex" and self.scenario == "signed_out":
                q["error"] = "Codex is signed out; sign in again"
            out.append(q)
        return out


SIM = Sim()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code: int, body, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html: str) -> None:
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def _gate(self, *, quota: bool = False) -> bool:
        """What a scenario does to a call on the gateway; True: go on."""
        if SIM.scenario == "down":  # the connection dies: what a stopped magpie, or the line to it, looks like from here
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return False
        if self.headers.get("Authorization") != f"Bearer {KEY}" or SIM.scenario == "bad_key":
            self._json(401, {"error": {"code": "invalid_api_key", "message": "this gateway key is wrong or switched off"}})
            return False
        if quota and SIM.scenario == "held":
            self._json(403, {"error": {"code": "gateway_key_held", "message": "this gateway key is held to a budget, models or accounts"}})
            return False
        return True

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/v3/cookies":  # CookieCloud's read-only ingress
            if SIM.cookies == "ok":
                data = SIGNED_IN.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            return self._json(503, {"code": "upstream_unavailable"})
        if u.path.startswith("/sim/avatar/"):
            hue = sum(u.path.encode()) % 360
            data = (f'<svg xmlns="http://www.w3.org/2000/svg" width="176" height="176"><rect width="176" height="176" fill="hsl({hue},38%,34%)"/>'
                    '<circle cx="88" cy="70" r="30" fill="#f2e6cf"/><path d="M28 176c6-46 30-68 60-68s54 22 60 68z" fill="#f2e6cf"/></svg>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        if u.path == "/api/v3/token/introspect":
            return self._json(200, {"token": {"name": "模拟令牌", "scopes": ["cookies:read"], "allowed_domains": ["youtube.com"]}})
        if not u.path.startswith("/v1/"):
            return self._console(u, q)
        if u.path == "/v1/magpie/me":
            if not self._gate():
                return
            limited = SIM.scenario == "held"
            return self._json(200, {"name": "Home LAN" if not limited else "课代表（限模型）", "version": "v0.1.2890-sim", "readOnly": False,
                                    "limits": [{"kind": "models"}] if limited else [], "models": [],
                                    "usage": {"7d": {"calls": 184, "cost": 5.61}}})
        if u.path == "/v1/models":
            return self._gate() and self._json(200, {"object": "list", "data": SIM.catalog()})
        if u.path == "/v1/magpie/quotas":
            return self._gate(quota=True) and self._json(200, {"object": "list", "data": SIM.quotas()})
        if u.path == "/v1/magpie/quotas/history":
            if not self._gate(quota=True):
                return
            return self._json(200, {"object": "list", "data": [
                {"provider": p, "user": f"{p}@example.com", "lines": [{"name": "7 days", "points": pts[-240:]}]} for p, pts in SIM.history.items()]})
        if u.path == "/v1/magpie/usage/requests":
            return self._gate() and self._json(200, {"object": "list", "data": list(SIM.reported.values())[-50:]})
        return self._json(404, {"error": {"message": f"no such route: {u.path}"}})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path.startswith("/sim/"):
            return self._control(u.path, self._body())
        if u.path == "/v1/magpie/quotas/refresh":
            self._body()
            return self._gate(quota=True) and self._json(200, {"object": "list", "data": SIM.quotas()})
        if u.path == "/v1/magpie/usage/requests":
            records = self._body()
            if not self._gate(quota=True):
                return
            records = records if isinstance(records, list) else [records]
            for i, r in enumerate(records):
                if odd := set(r) - INGEST_FIELDS:
                    return self._json(400, {"error": {"message": f"record {i}: {sorted(odd)[0]}: not a field a reported record takes", "index": i}})
            fresh = [r for r in records if r["rid"] not in SIM.reported]
            for r in fresh:
                SIM.reported[r["rid"]] = {**r, "source": "reported"}
            SIM.note(path="POST /usage/requests", model=f"{len(fresh)} 条 Gemini 调用", status=200, session="", agent=self.headers.get("User-Agent"))
            return self._json(200, {"accepted": len(fresh), "skipped": len(records) - len(fresh), "records": []})
        if u.path == "/v1/responses":
            return self._responses()
        return self._json(404, {"error": {"message": f"no such route: {u.path}"}})

    def _responses(self) -> None:
        body = self._body()
        model = body.get("model", "")
        session, agent = self.headers.get("X-Magpie-Session") or "", self.headers.get("User-Agent")
        if not self._gate():
            return
        member = "codex/gpt-6.1-terra" if model == GROUP["id"] else model
        sub = member.split("/")[0]

        def refuse(code: int, message: str, kind: str | None = None) -> None:
            SIM.note(path="POST /responses", model=model, status=code, session=session, agent=agent)
            self._json(code, {"error": {"code": kind, "message": message}})

        def fails(message: str) -> None:  # Claude's way: a 200, then a failed stream
            SIM.note(path="POST /responses", model=model, status=502, session=session, agent=agent)
            self._stream([{"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error", "message": message}}}], sub, member)

        # a model it does not have, as each of them says it on the real gateway
        name = model.split("/", 1)[-1]
        listed = {m["id"] for m in SIM.catalog()}
        if model not in listed:
            if sub == "codex":
                return refuse(400, f"Codex: The '{name}' model is not supported when using Codex with a ChatGPT account.", "model_not_found")
            if sub == "claude":
                return fails(f"Claude Code: There's an issue with the selected model ({name}). It may not exist or you may not have access to it.")
            if sub == "grok":
                return refuse(404, f"Grok: The model {name} does not exist or your team does not have access to it.")
            return refuse(404, f'magpie knows no model "{model}"')
        if sub == "codex" and SIM.scenario == "signed_out":
            return refuse(401, "Codex is signed out; sign in again")
        if sub in SIM.windows and SIM.out(sub):
            return refuse(429, "The usage limit has been reached, try again at the reset")
        prompt = json.dumps(body.get("input"), ensure_ascii=False)
        if sub == "claude" and prompt.count('"input_image"') >= 20:  # Claude Code ends without an answer: images go in by paste
            return fails("Claude Code: Claude Code ended without an answer (502: you can retry your request)")
        health = "只回复两个字" in prompt
        time.sleep(0.15 if health else random.uniform(0.5, 1.4))
        if not health and sub in SIM.windows:
            SIM.spend(sub)
        SIM.note(path="POST /responses", model=model, status=200, session=session, agent=agent, effort=(body.get("reasoning") or {}).get("effort"))
        text = "在线" if health else "这是模拟的模型回答。" * 30
        events = [{"type": "response.created"}]
        if sub == "codex":
            w = SIM.windows["codex"]["w"]["7 days"]
            events.append({"type": "codex.rate_limits", "plan_type": "prolite", "rate_limits": {"primary": {
                "used_percent": round(w["used"]), "window_minutes": 10080, "reset_at": int(w["resets"].timestamp())}, "secondary": None}})
        events += [{"type": "response.output_text.delta", "delta": text[i:i + 60]} for i in range(0, len(text), 60)]
        tokens = {"input_tokens": random.randint(8000, 60000), "output_tokens": random.randint(2000, 12000),
                  "output_tokens_details": {"reasoning_tokens": random.randint(0, 1500)}}
        events.append({"type": "response.completed", "response": {"status": "completed", "usage": tokens}})
        self._stream(events, sub, member)

    def _stream(self, events: list[dict], sub: str, member: str) -> None:
        data = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Magpie-Provider", sub)
        self.send_header("X-Magpie-Model", member)
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------- the console
    def _control(self, path: str, b: dict) -> None:
        with SIM.lock:
            if path == "/sim/scenario":
                SIM.scenario = b["name"]
            elif path == "/sim/left":
                SIM.set_left(b["sub"], float(b["left"]))
            elif path == "/sim/tick":
                (SIM.ticked.add if b["on"] else SIM.ticked.discard)(b["model"])
            elif path == "/sim/group":
                SIM.group = bool(b["on"])
            elif path == "/sim/drain":
                SIM.drain = float(b["value"])
        if path == "/sim/produce":
            produce(int(b.get("n") or 1))
        elif path == "/sim/backfill":
            backfill(int(b.get("n") or 0))
        elif path == "/sim/fail":
            fail_some()
        elif path == "/sim/cookies":
            cookie_scene(b["name"])
        self._json(200, state_view())

    def _console(self, u, q) -> None:
        if u.path == "/sim/state":
            return self._json(200, state_view())
        where = ""
        if view := (q.get("view") or [""])[0]:  # where 课代表's links lead: magpie's own pages, which this fake does not have
            edit = (q.get("edit") or [""])[0]
            where = f"这里本该是 magpie 的「{ {'providers': '供应商', 'gateway': '网关'}.get(view, view) }」页{f'（{edit}）' if edit else ''}。账号、登录和 Key 都在那里管，《课代表》只读。下面的按钮模拟你在那边的操作。"
        self._html(CONSOLE.replace("{{WHERE}}", where).replace("{{WEB}}", WEB_URL))


def state_view() -> dict:
    from assetizer import db
    runs = db.q("SELECT id, production, stage, wait_reason FROM runs WHERE kind='PRODUCTION' AND id LIKE 'SIM%' ORDER BY created_at DESC LIMIT 6")
    with SIM.lock:
        windows = {k: round(100 - max(w["used"] for n, w in v["w"].items() if n == "7 days"), 1) for k, v in SIM.windows.items()}
        windows["gemini"] = round(SIM.gemini["weekly"] * 100, 1)
        return {"scenario": SIM.scenario, "scenarios": SCENARIOS, "left": windows, "subs": SUBS,
                "models": [{"id": m["id"], "name": m["display_name"], "on": m["id"] in SIM.ticked} for m in MODELS],
                "group": SIM.group, "drain": SIM.drain, "requests": SIM.requests[:14], "reported": len(SIM.reported),
                "paused": db.setting("queue_paused"), "runs": runs, "cookies": SIM.cookies, "cookie_scenes": COOKIES,
                "backfill": db.one("SELECT COUNT(*) n FROM backfill_queue WHERE status='PENDING'")["n"]}


CONSOLE = """<!doctype html><meta charset=utf-8><title>模拟 magpie</title>
<style>
:root{color-scheme:light dark;--ink:light-dark(#1d1b16,#ece8df);--dim:light-dark(#6f6a5e,#a29c8e);--rule:light-dark(#d9d4c7,#3b382f);--paper:light-dark(#faf8f2,#1b1a16);--wash:light-dark(#efece2,#28261f)}
body{margin:0 auto;max-width:880px;padding:28px 20px 60px;font:14px/1.55 -apple-system,"PingFang SC",sans-serif;color:var(--ink);background:var(--paper)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
p{margin:0;color:var(--dim)}a{color:inherit}.where{margin:12px 0 0;padding:10px 12px;background:var(--wash);border-radius:4px;color:var(--ink)}.where:empty{display:none}
button{font:inherit;font-size:13px;height:30px;padding:0 12px;border:1px solid var(--rule);border-radius:4px;background:transparent;color:var(--ink);cursor:pointer}button:hover{border-color:var(--ink)}button.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.subs{display:grid;grid-template-columns:130px 60px 1fr;gap:8px 12px;align-items:center}.subs b{font-weight:600;font-variant-numeric:tabular-nums}
.ticks{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:4px 16px}label{display:flex;gap:6px;align-items:center}
table{width:100%;border-collapse:collapse;font-size:12.5px}td,th{text-align:left;padding:4px 10px 4px 0;border-bottom:1px solid var(--rule);font-weight:400;white-space:nowrap}th{color:var(--dim)}.bad{color:#b4321f}td:last-child{white-space:normal}
</style>
<h1>模拟 magpie</h1><p>本机假网关，不碰生产、不调模型。真正的机房：<a href="{{WEB}}/engine" target="_blank">{{WEB}}/engine</a></p><p class=where>{{WHERE}}</p>
<h2>场景</h2><div class=row id=sc></div>
<h2>额度</h2><div class=subs id=subs></div><div class=row style="margin-top:10px"><span style="color:var(--dim)">每次调用消耗</span><span id=dr class=row></span></div>
<h2>在 magpie 里勾选的模型</h2><div class=ticks id=ticks></div>
<h2>CookieCloud</h2><div class=row id=cc></div>
<h2>补录 <span id=bfn style="font-weight:400;color:var(--dim)"></span></h2><div class=row><button onclick="act('/sim/backfill',{n:172})">排 172 期</button><button onclick="act('/sim/backfill',{n:0})">取消排队</button></div>
<h2>生产</h2><div class=row><button onclick="act('/sim/produce',{n:1})">印一期</button><button onclick="act('/sim/produce',{n:3})">连印 3 期</button><button onclick="act('/sim/produce',{n:8})">连印 8 期</button><button onclick="act('/sim/fail')">造 3 期失败</button><span id=pause></span></div><table id=runs style="margin-top:10px"></table>
<h2>《课代表》发来的请求 <span id=rep style="font-weight:400;color:var(--dim)"></span></h2><table id=reqs></table>
<script>
const $=i=>document.getElementById(i);let cur;
async function act(p,b){cur=await(await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})).json();draw()}
function el(t,a,...k){const e=document.createElement(t);Object.assign(e,a||{});e.append(...k);return e}
function draw(){const s=cur;
$('sc').replaceChildren(...Object.entries(s.scenarios).map(([k,v])=>el('button',{className:s.scenario==k?'on':'',onclick:()=>act('/sim/scenario',{name:k})},v)));
$('subs').replaceChildren(...Object.entries(s.subs).flatMap(([k,v])=>[el('span',{},v),el('b',{},s.left[k]+'%'),el('div',{className:'row'},...[[0,'用完'],[12,'剩 12%'],[50,'剩 50%'],[100,'恢复']].map(([n,t])=>el('button',{onclick:()=>act('/sim/left',{sub:k,left:n})},t)))]));
$('cc').replaceChildren(...Object.entries(s.cookie_scenes).map(([k,v])=>el('button',{className:s.cookies==k?'on':'',onclick:()=>act('/sim/cookies',{name:k})},v)));
$('bfn').textContent=s.backfill?'· 排队 '+s.backfill+' 期':'';
$('dr').replaceChildren(...[[.5,'0.5%'],[2,'2%'],[6,'6%']].map(([n,t])=>el('button',{className:s.drain==n?'on':'',onclick:()=>act('/sim/drain',{value:n})},t)));
$('ticks').replaceChildren(...s.models.map(m=>el('label',{},el('input',{type:'checkbox',checked:m.on,onchange:e=>act('/sim/tick',{model:m.id,on:e.target.checked})}),m.name)),el('label',{},el('input',{type:'checkbox',checked:s.group,onchange:e=>act('/sim/group',{on:e.target.checked})}),'路由分组：文本快线'));
$('pause').textContent=s.paused?'队列已暂停：'+s.paused.reason:'';$('pause').className=s.paused?'bad':'';
$('runs').replaceChildren(...s.runs.map(r=>el('tr',{},el('td',{},r.id),el('td',{},r.production),el('td',{},r.stage||''),el('td',{className:r.wait_reason?'bad':''},r.wait_reason||''))));
$('rep').textContent=s.reported?'· 已收到 Gemini 调用上报 '+s.reported+' 条':'';
$('reqs').replaceChildren(el('tr',{},...['时间','请求','模型','状态','Session','调用方','effort'].map(x=>el('th',{},x))),...s.requests.map(r=>el('tr',{},el('td',{},r.at),el('td',{},r.path),el('td',{},r.model||''),el('td',{className:r.status>=400?'bad':''},String(r.status)),el('td',{},r.session||''),el('td',{},r.agent||''),el('td',{},r.effort||''))))}
async function poll(){try{cur=await(await fetch('/sim/state')).json();if(!document.activeElement||document.activeElement.type!=='checkbox')draw()}catch{}}
poll();setInterval(poll,1500);
</script>"""
WEB_URL = "http://127.0.0.1:8799"


# ---------------------------------------------------------------- the stages, with what is real and what is not
CALLS = {
    "UNDERSTAND": (["understand-CH01", "understand-CH02"], True),
    "MERGE": (["merge"], False),
    "KNOWLEDGE": (["knowledge-skeleton", "knowledge-detail-01", "knowledge-detail-02"], False),
    "ARTICLE": (["article-outline", "article-sections-01"], False),
    "SELECT": (["selection"], False),
    "BOUNDARY": (["boundary-U01", "boundary-U02"], True),
    "REVIEW_CHECK": (["review-check-01"], False),
}
TITLES = ["美联储议息前夜：流动性还撑得住吗", "财报季中段复盘：谁在定价谁在讲故事", "债市的信号与股市的分歧", "AI 资本开支的第二阶段", "周线级别的三个风险点",
          "黄金、美元与实际利率", "中概反弹的成色", "科技巨头的自由现金流", "期权市场在押注什么", "小盘股的轮动与信用利差"]
_seq = [0]


def produce(n: int) -> None:
    from assetizer import db
    from assetizer.pipeline import runner
    from assetizer.util import now
    for _ in range(n):
        _seq[0] += 1
        vid = f"SIM{int(time.time()) % 100000:05d}{_seq[0]:03d}"[:11]
        runner.submit(f"https://www.youtube.com/watch?v={vid}", origin="manual")
        channel = (db.one("SELECT id FROM channels LIMIT 1") or {}).get("id")
        db.exec("UPDATE sources SET title=?, headline=?, channel_id=?, duration=?, updated_at=? WHERE id=?",
                random.choice(TITLES), None, channel, random.randint(3000, 9000), now(), vid)


def backfill(n: int) -> None:
    """Line up the back catalogue of the first locked channel (0: take the line away)."""
    from assetizer import db, ingest
    ch = db.one("SELECT id FROM channels WHERE stage='LOCKED' ORDER BY created_at LIMIT 1")
    if not ch:
        return
    if not n:
        ingest.cancel_backfill(ch["id"])
        return
    _seq[0] += 1
    ingest.enqueue_backfill(ch["id"], [{"id": f"B{_seq[0]:03d}{i:07d}"[:11], "title": random.choice(TITLES), "duration": random.randint(1800, 7200)} for i in range(n)])


def fail_some() -> None:
    """Three issues whose production failed, for the 首页: what the failure was is a line under each."""
    from assetizer import db
    from assetizer.pipeline.runner import STAGE_NAMES
    from assetizer.util import now
    channel = (db.one("SELECT id FROM channels LIMIT 1") or {}).get("id")
    for i, (title, cls, text) in enumerate([
            ("美联储议息前夜：流动性还撑得住吗，以及为什么这一次的缩表和上一次不一样", "PROVIDER_FAILURE", "stream ended: blocked by content safety filters"),
            ("财报季中段复盘：谁在定价谁在讲故事", "MEDIA_READ_FAILED", "agy could not read the video"),
            ("债市的信号与股市的分歧", "PROVIDER_OUTPUT_LIMIT", "output truncated at the limit")]):
        _seq[0] += 1
        vid = f"F{_seq[0]:03d}{i:07d}"[:11]
        db.exec("""INSERT INTO sources(id, url, channel_id, title, duration, created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
                vid, f"https://www.youtube.com/watch?v={vid}", channel, title, 3600, now(), now())
        db.exec("""INSERT INTO runs(id, source_id, kind, production, error_class, error, attempts, created_at, started_at, finished_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", f"{vid}-r1", vid, "PRODUCTION", "FAILED", cls, text, 1, now(), now(), now())
        for n, name in enumerate(STAGE_NAMES):
            db.exec("INSERT INTO stages(run_id, name, ord, status) VALUES (?,?,?,?)", f"{vid}-r1", name, n, "DONE" if n < 2 else "FAILED" if n == 2 else "PENDING")


COOKIES = {"ok": "读得到", "kept": "读不到 · 用上一份", "kept_long": "读不到超过 6 小时", "lost": "读不到 · 没有上一份", "stale": "读不到 · 上一份也失效"}
SIGNED_IN = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsim\n.youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tsim\n"


def cookie_scene(name: str) -> None:
    """Put CookieCloud in this state and read it once, through the code a download goes through."""
    from assetizer import config, cookies, db
    from assetizer.pipeline.runner import ALERT_TEXT
    SIM.cookies = name
    kept = config.get().secrets_dir / cookies.KEPT_LOGIN
    db.resolve_alert("YOUTUBE_AUTH")
    if name == "lost":
        kept.unlink(missing_ok=True)
    if name != "ok" and name != "lost" and not kept.exists():
        kept.write_text(SIGNED_IN)
        os.chmod(kept, 0o600)
        past = time.time() - 3 * 3600
        os.utime(kept, (past, past))
    if name == "kept_long":
        s = db.setting("youtube_cookies_state") or {}
        db.set_setting("youtube_cookies_state", {**s, "ok": False, "source": "cookiecloud", "since": iso(utc(hours=-7)).replace("Z", "+00:00")})
    with cookies.ytdlp_login():
        pass
    if name == "stale":
        db.raise_alert("YOUTUBE_AUTH", "critical", *ALERT_TEXT["YOUTUBE_AUTH"])


def install(home: Path) -> None:
    """Everything that would reach past this machine, or take hours, answers here instead."""
    from assetizer import ingest, llm, playback, youtube
    from assetizer.pipeline import finish, media_stages, runner
    from assetizer.providers import agy, magpie, magpie_state

    clip = home / "sim-clip.mp4"
    clip.write_bytes(b"sim")

    def stage(name: str):
        def run(ctx):
            calls, video = CALLS.get(name, ([], False))
            if not calls:
                time.sleep(random.uniform(0.2, 0.5))
            llm_ = ctx.llm()
            for c in calls:
                llm_.text(ctx.calls, c, f"模拟提示词：{c}", min_chars=1, media=[str(clip)] if video else None)
            return {"sim": True}
        return run
    runner.STAGES[:] = [(n, stage(n), b) for n, _, b in runner.STAGES]
    media_stages.transcribe = stage("TRANSCRIBE")

    def fake_frames(video, out):
        out.mkdir(parents=True, exist_ok=True)
        f = out / "a.jpg"
        f.write_bytes(b"\xff\xd8\xff\xd9")
        return [(float(i * 15), f) for i in range(4)]
    magpie.frames = fake_frames
    magpie.transcript = lambda video, out: [{"start": 1.0, "end": 4.0, "text": "模拟的语音转写"}]
    magpie.MagpieProvider.media_health = lambda self: {"ok": True, "version": self.version(), "checked_at": _now()}

    def agy_call(self, call_dir, prompt, *, kind, schema=None, media=None, effort=None, timeout=None):
        call_dir = Path(call_dir)
        call_dir.mkdir(parents=True, exist_ok=True)
        with SIM.lock:
            if min(SIM.gemini.values()) <= 0:
                raise agy.ProviderError("PROVIDER_QUOTA", "Gemini quota used up (simulated)", {"provider": "agy"})
            SIM.gemini["weekly"] = max(0.0, SIM.gemini["weekly"] - SIM.drain / 100 * 0.2)
            SIM.gemini["5h"] = max(0.0, SIM.gemini["5h"] - SIM.drain / 100 * 0.5)
        health = "只回复两个字" in prompt
        time.sleep(0.1 if health else random.uniform(0.5, 1.2))
        text = "在线" if health else "这是模拟的 Gemini 回答。" * 20
        receipt = {"provider": "agy", "kind": kind, "status": "SUCCESS", "actual_model": "gemini-3.8-flash-high", "agy_version": "agy-sim",
                   "wall_seconds": random.uniform(20, 90), "view_file_count": len(media or []),
                   "usage": {"input_tokens": random.randint(20000, 90000), "output_tokens": random.randint(2000, 9000), "thinking_tokens": random.randint(500, 4000)}}
        return agy.CallResult(text, receipt)

    def agy_usage(self):
        with SIM.lock:
            left = dict(SIM.gemini)
        return {"ok": True, "checked_at": _now(), "version": "agy-sim", "groups": [{"name": "Gemini Models", "models": "Models within this group: Gemini Flash", "buckets": [
            {"window": "5h", "remaining": left["5h"], "reset_at": iso(utc(hours=3, minutes=20)), "disabled": False, "note": None},
            {"window": "weekly", "remaining": left["weekly"], "reset_at": iso(utc(days=5, hours=2)), "disabled": False, "note": None}]}]}

    def agy_health(self):
        ok = min(SIM.gemini.values()) > 0
        return {"ok": ok, "error": None if ok else "PROVIDER_QUOTA", "model": "agy-sim", "checked_at": _now()}

    agy.AgyProvider.call = agy_call
    agy.AgyProvider.usage = agy_usage
    agy.AgyProvider.health = agy_health
    agy.AgyProvider.media_health = lambda self: {"ok": True, "version": "agy-sim", "checked_at": _now()}
    agy.AgyProvider.version = lambda self: "agy-sim"

    youtube.channel_avatar = lambda cid: f"http://127.0.0.1:{SIM.port}/sim/avatar/{cid}.svg"  # nor its avatar
    ingest.poll = lambda: None                 # no channel is looked up on YouTube
    playback.tick = lambda: None
    finish.sweep_agy_state = lambda: 0         # nothing of this machine's own agy is touched
    runner.kill_orphans = lambda: 0
    runner.retention = lambda: None
    runner.memory_alert = lambda: None
    llm.time = types.SimpleNamespace(sleep=lambda s: time.sleep(min(s, 1.0)), monotonic=time.monotonic)  # the wait between retries
    runner.QUOTA_EVERY = 20
    runner.MAGPIE_EVERY = 10
    magpie_state.STALE_AFTER = 5


def _now() -> str:
    from assetizer.util import now
    return now()


def run_worker() -> None:
    from assetizer import db
    from assetizer.pipeline.runner import Worker
    w = Worker(poll=1.0)
    w.recover()
    while True:
        # what waits on a timer in production comes round every few seconds here: a paused queue is probed, a wait for the network retried
        w.last["health"] = w.last["retry"] = 0.0
        try:
            busy = w.tick()
        except Exception as e:  # noqa: BLE001
            db.event("worker", f"Worker 异常：{e}", level="error")
            busy = False
        time.sleep(0.3 if busy else 2.0)


# ---------------------------------------------------------------- what has already happened on this box
def seed(home: Path) -> None:
    """The last 30 days as production would have left them: the statistics have something to show from the first look."""
    from assetizer import config, db, seed as seeds
    from assetizer.pipeline.runner import STAGE_NAMES
    from assetizer.util import now
    config.get().ensure_dirs()
    seeds.run()
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "secrets" / "magpie-api-key").write_text(KEY)
    db.set_setting("llm_provider", "agy")
    db.set_setting("stage_providers", {"knowledge": "magpie", "selection": "magpie"})  # as on prodbox today
    db.set_setting("agy_media_version", "agy-sim")
    db.set_setting("agy_media_health", {"ok": True, "version": "agy-sim", "checked_at": now()})
    channel = (db.one("SELECT id FROM channels LIMIT 1") or {}).get("id")
    rng = random.Random(7)
    calls = [("understand-CH01", "gemini-3.8-flash-high", 900000), ("merge", "gemini-3.8-flash-high", 30000), ("knowledge-skeleton", "codex/gpt-6.1-terra", 90000),
             ("knowledge-detail-01", "codex/gpt-6.1-terra", 180000), ("article-sections-01", "gemini-3.8-flash-high", 500000), ("selection", "codex/gpt-6.1-terra", 30000),
             ("boundary-U01", "gemini-3.8-flash-high", 3600000), ("review-check-01", "gemini-3.8-flash-high", 450000)]
    log = []
    for i in range(36):
        at = utc(days=-(30 * (36 - i) / 36), minutes=rng.randint(0, 200))
        vid = f"HIST{i:07d}"[:11]
        title = rng.choice(TITLES)
        db.exec("""INSERT INTO sources(id, url, channel_id, title, headline, duration, media_state, delivery, created_at, updated_at, delivered_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""", vid, f"https://www.youtube.com/watch?v={vid}", channel, title, title, rng.randint(3000, 9000), "PURGED", "DELIVERED",
                (at - timedelta(hours=3)).isoformat(timespec="seconds"), at.isoformat(timespec="seconds"), at.isoformat(timespec="seconds"))
        rid = f"{vid}-r1"
        db.exec("""INSERT INTO runs(id, source_id, kind, production, attempts, created_at, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?)""",
                rid, vid, "PRODUCTION", "COMPLETE", 1, (at - timedelta(hours=2)).isoformat(timespec="seconds"), (at - timedelta(hours=2)).isoformat(timespec="seconds"), at.isoformat(timespec="seconds"))
        db.exec("UPDATE sources SET current_run_id=? WHERE id=?", rid, vid)
        for n, name in enumerate(STAGE_NAMES):
            db.exec("INSERT INTO stages(run_id, name, ord, status, wall_seconds) VALUES (?,?,?,?,?)", rid, name, n, "DONE", rng.uniform(30, 900))
        for stage_, model, size in calls:
            db.exec("""INSERT INTO model_calls(run_id, stage, attempt, model, agy_version, status, wall_seconds, input_tokens, output_tokens, thinking_tokens, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rid, stage_, "attempt-1", model, f"magpie:{model}" if "/" in model else "agy-sim", "SUCCESS", rng.uniform(30, 300),
                    int(size * rng.uniform(.7, 1.1)), int(size * .08), int(size * .03), at.isoformat(timespec="seconds"))
        log.append([at.isoformat(timespec="seconds"), 0.9, None])
        for j, (name, level) in enumerate([*[(f"{n} 完成", "info") for n in ("ACQUIRE", "UNDERSTAND", "KNOWLEDGE", "ARTICLE", "PACKAGE")], ("生产完成（COMPLETE）", "info")]):
            db.exec("INSERT INTO events(ts, run_id, level, kind, message) VALUES (?,?,?,?,?)",
                    (at - timedelta(minutes=(6 - j) * 18)).isoformat(timespec="seconds"), rid, level, "stage" if j < 5 else "run", name)
    week = [x for x in log if x[0] >= iso(utc(days=-7)).replace("Z", "+00:00")]
    for j, x in enumerate(week):
        x[2] = round(0.98 - (0.98 - SIM.gemini["weekly"]) * j / max(1, len(week) - 1), 3)
    db.set_setting("agy_quota_log", week)
    db.set_setting("magpie_report_cursor", db.one("SELECT MAX(id) m FROM model_calls")["m"])  # nothing old to report: the sim shows new calls going


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8799, help="《课代表》网页")
    ap.add_argument("--magpie-port", type=int, default=8798, help="假 magpie 与控制台")
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME)
    ap.add_argument("--reset", action="store_true", help="清掉模拟数据，从头来")
    a = ap.parse_args()
    home = a.home.resolve()
    if a.reset and home.exists():
        if "magpie-sim" not in home.name:
            sys.exit(f"--reset 只清名字里带 magpie-sim 的目录，不动 {home}")
        shutil.rmtree(home)
    fresh = not (home / "assetizer.db").exists() and not (home / "data" / "assetizer.db").exists()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["ASSETIZER_HOME"] = str(home)
    for name in ("ASSETIZER_NOTION_TOKEN", "ASSETIZER_MAGPIE_API_KEY", "ASSETIZER_TELEGRAM_BOT_TOKEN"):
        os.environ.pop(name, None)  # nothing of this machine's own reaches the simulation
    global WEB_URL
    WEB_URL = f"http://127.0.0.1:{a.port}"

    from assetizer import config, db
    if fresh:
        seed(home)
    db.set_setting("magpie_url", f"http://127.0.0.1:{a.magpie_port}")  # this box's magpie is the fake, whatever else was set
    db.set_setting("cookiecloud_origin", f"http://127.0.0.1:{a.magpie_port}")  # and its CookieCloud
    db.exec("UPDATE channels SET stage='LOCKED' WHERE stage='TUNING' AND id=(SELECT id FROM channels ORDER BY created_at LIMIT 1)")
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "secrets" / "cookiecloud-token").write_text("ccat_" + "sim" * 14)
    db.exec("UPDATE runs SET production='CANCELLED', error_class='STOPPED' WHERE production IN ('RUNNING','QUEUED','WAITING') AND id LIKE 'SIM%'")
    db.set_setting("queue_paused", None)
    assert str(config.get().home) == str(home)
    SIM.port = a.magpie_port
    install(home)

    httpd = ThreadingHTTPServer(("127.0.0.1", a.magpie_port), Handler)
    threading.Thread(target=httpd.serve_forever, name="fake-magpie", daemon=True).start()
    threading.Thread(target=run_worker, name="sim-worker", daemon=True).start()
    cookie_scene("ok")
    print(f"\n  机房    {WEB_URL}/engine\n  控制台  http://127.0.0.1:{a.magpie_port}\n", flush=True)

    import uvicorn
    uvicorn.run("assetizer.web.app:app", host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
