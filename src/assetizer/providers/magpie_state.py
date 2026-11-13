"""What magpie, the subscription gateway, says about itself: its subscriptions and how much of each is left, the models
ticked there, whose Key this is. Nothing about them is configured here: accounts, keys and routing groups live in
magpie, and the 机房 only reads them (setting ``magpie_state``, design §R6-8). The reads ask no vendor and spend no
quota (``/quotas?kept=1`` answers from what magpie last read); only 刷新 has magpie ask its vendors again.

A failed read keeps the last numbers and says why (``error``):
  NO_KEY       no secrets/magpie-api-key
  UNREACHABLE  magpie does not answer, or something else answers in its place
  BAD_KEY      401: the key is wrong or switched off
  HELD         403 gateway_key_held: a key held to a budget, models or accounts may not read the quotas

Calls that do not go through magpie (agy's) are reported to its Usage page, so its numbers cover all of production.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import config, db, knobs
from ..util import now
from . import is_model, resolve, routes
from .magpie import DEFAULT_URL, USER_AGENT, quota_view

READ_EVERY = 300        # seconds between two reads by the worker
STALE_AFTER = 60        # seconds: the page asks for a new read when what it shows is older than this
TIMEOUT = 15
TREND_DAYS = 8
TREND_POINTS = 120
CREDIT_GAP = 240        # seconds: credits read nearer than this to the last reading replace it
CREDIT_POINTS = 3000
REPORT_BATCH = 500
REPORT_DAYS = 30        # the first report goes this far back: as far as the 机房's own statistics
WINDOWS = {"5 hours": ("5h", "5 小时"), "7 days": ("weekly", "周额度")}
SIGNED_OUT = re.compile(r"sign(ed)?[ -]?(in|out)|log[ -]?in|auth|expired|credential", re.I)
NOT_READ = "not read yet"  # magpie holds its reads until asked: no reading yet, nothing the matter
LEVELLED = re.compile(r"(.+)-(minimal|low|medium|high|xhigh)")  # agy's model ids say the level; magpie prices the model, not the level
CREDITS = re.compile(r"([\d,]+(?:\.\d+)?)\s*credits?", re.I)  # a ChatGPT account's balance as magpie tells it: "72785.81 credits"


class Refused(Exception):
    def __init__(self, error: str, detail: str = ""):
        super().__init__(error)
        self.error, self.detail = error, detail


def base_url() -> str:
    return (db.setting("magpie_url") or DEFAULT_URL).rstrip("/")


def _key() -> str:
    key = config.get().secret("magpie-api-key")
    if not key:
        raise Refused("NO_KEY")
    return key


def _ask(path: str, *, data: bytes | None = None, method: str = "GET", timeout: float = TIMEOUT):
    req = urllib.request.Request(base_url() + path, data=data, method=method, headers={
        "Authorization": f"Bearer {_key()}", "User-Agent": USER_AGENT, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # magpie is on the same box
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        if e.code == 401:
            raise Refused("BAD_KEY", body) from None
        if e.code == 403:
            raise Refused("HELD" if "gateway_key" in body else "BAD_KEY", body) from None
        if e.code == 400:  # magpie answered and said what is wrong: a post it will refuse again
            raise Refused("REFUSED", body) from None
        raise Refused("UNREACHABLE", f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError: a page that is not magpie's JSON
        raise Refused("UNREACHABLE", type(e).__name__) from None


# ---------------------------------------------------------------- the shapes the 机房 reads
def _when(iso: str | None) -> str | None:
    """Any of magpie's times (with nanoseconds, a zone or Z) as an ISO time in UTC."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def _window(w: dict) -> dict | None:
    if w.get("text") or w.get("unlimited") or not isinstance(w.get("used"), (int, float)):
        return None  # told as words, or no limit: no share to show
    key, label = WINDOWS.get(w.get("name"), (w.get("name") or "?", w.get("name") or "?"))
    left = w.get("remaining") if isinstance(w.get("remaining"), (int, float)) else 100 - w["used"]
    return {"window": key, "label": label, "remaining": max(0.0, min(1.0, left / 100)), "reset_at": _when(w.get("resetsAt")),
            "disabled": False}


def _credits(q: dict) -> float | None:
    """The credits a ChatGPT account holds: with any left, a used-up window does not stop it — the vendor spends them
    instead (magpie's default; it does not tell an account set not to). None: none, or not a ChatGPT account."""
    m = q.get("provider") == "codex" and CREDITS.fullmatch((q.get("balance") or "").strip())
    n = float(m[1].replace(",", "")) if m else 0
    return n if n > 0 else None


def sub_view(q: dict) -> dict:
    windows = [v for w in q.get("windows") or [] if (v := _window(w))]
    windows.sort(key=lambda b: {"5h": 0, "weekly": 1}.get(b["window"], 2))
    error = q.get("error") or None
    if error and error.startswith(NOT_READ):
        error = None
    return {"provider": q.get("provider"), "name": q.get("name") or q.get("provider"), "plan": q.get("plan"), "user": q.get("user"),
            "kind": q.get("kind"), "windows": windows, "balance": q.get("balance"), "credits": _credits(q), "error": error,
            "signed_out": bool(error and SIGNED_OUT.search(error)),
            "read_at": _when(q.get("readAt") or q.get("asOf")), "last_served_at": _when(q.get("lastServedAt"))}


def model_view(m: dict) -> dict:
    mod = m.get("modalities") or {}
    return {"id": m.get("id"), "name": m.get("display_name") or m.get("id"), "label": m.get("magpie_label") or m.get("display_name"),
            "vendor": m.get("owned_by") or (m.get("id") or "").split("/")[0], "group": (m.get("id") or "").startswith("group/"),
            "context": m.get("context_window"), "inputs": mod.get("input") or ["text"], "levels": m.get("reasoning_levels") or [],
            "watches": knobs.sees(m.get("id") or "", mod.get("input") or ["text"])}


def _trends() -> dict[str, list]:
    """Each subscription's weekly window over the last days, as (time, left): the trend and 约够印 N 期 on its card."""
    since = datetime.now(timezone.utc) - timedelta(days=TREND_DAYS)
    out: dict[str, list] = {}
    for h in _ask(f"/v1/magpie/quotas/history?days={TREND_DAYS}").get("data") or []:
        line = next((x for x in h.get("lines") or [] if x.get("name") == "7 days"), None)
        pts = [[_when(p.get("at")), p["left"] / 100] for p in (line or {}).get("points") or []
               if isinstance(p.get("left"), (int, float)) and _when(p.get("at"))]
        pts = [p for p in pts if p[0] >= since.isoformat(timespec="seconds")]
        if pts:
            step = max(1, len(pts) // TREND_POINTS)
            out[sub_id(h)] = pts[::step] + ([pts[-1]] if len(pts) % step != 1 and step > 1 else [])
    return out


def sub_id(q: dict) -> str:
    return f"{q.get('provider')}|{(q.get('user') or '').lower()}"


def log_credits(subs: list[dict], at: str | None = None) -> None:
    """Each ChatGPT account's credits as read, by sub_id, as [time, credits] oldest first (setting
    `magpie_credit_logs`): magpie keeps no such history, and 约够印 N 期 on credits is reckoned from it. A run of
    the same amount keeps its first and last reading; a reading nearer than CREDIT_GAP to the one before the last moves the last, so readings stay that far apart."""
    logs = dict(db.setting("magpie_credit_logs") or {})
    t = datetime.fromisoformat(at or now())
    cut = (t - timedelta(days=TREND_DAYS)).isoformat(timespec="seconds")
    for s in subs:
        if s.get("credits") is None:
            continue
        pts = [p for p in logs.get(sub_id(s)) or [] if p[0] >= cut]
        point = [t.isoformat(timespec="seconds"), s["credits"]]
        if len(pts) >= 2 and (pts[-1][1] == pts[-2][1] == s["credits"]
                              or t - datetime.fromisoformat(pts[-2][0]) < timedelta(seconds=CREDIT_GAP)):
            pts[-1] = point
        else:
            pts.append(point)
        logs[sub_id(s)] = pts[-CREDIT_POINTS:]
    db.set_setting("magpie_credit_logs", logs)


# ---------------------------------------------------------------- reading
_lock = threading.Lock()


def read(*, vendors: bool = False) -> dict:
    """Read what the gateway says into `magpie_state`. `vendors`: have magpie ask its vendors again first (刷新)."""
    with _lock:
        state = dict(db.setting("magpie_state") or {})
        state.update(checked_at=now(), ok=False, error=None, detail=None)
        try:
            who = _ask("/v1/magpie/me")
            state["key"] = {"name": who.get("name"), "limited": bool(who.get("limits")), "read_only": bool(who.get("readOnly"))}
            state["version"] = who.get("version")
            state["usage"] = (who.get("usage") or {}).get("7d")
            state["models"] = [model_view(m) for m in _ask("/v1/models").get("data") or [] if m.get("id")]
            state["models_at"] = now()
            if vendors:
                try:
                    _ask("/v1/magpie/quotas/refresh", method="POST", data=b"{}", timeout=45)
                except Refused as e:
                    if e.error not in ("UNREACHABLE", "REFUSED"):  # a vendor slow to answer: what magpie has kept still reads
                        raise
            state["subs"] = [sub_view(q) for q in _ask("/v1/magpie/quotas?kept=1").get("data") or []
                             if q.get("kind") in (None, "subscription") or q.get("windows")]
            state["read_at"] = now()
            log_credits(state["subs"])
            try:
                trends = _trends()
                for s in state["subs"]:
                    s["trend"] = trends.get(sub_id(s), [])
            except Refused:
                pass  # the bars are what counts; a trend can wait for the next read
            state["ok"] = True
        except Refused as e:
            state.update(error=e.error, detail=e.detail)
        db.set_setting("magpie_state", state)
        return state


_reading = threading.Lock()


def _in_background(work) -> None:
    """One at a time, and never in the way of what asked: magpie not answering costs a read its timeout."""
    if not config.get().secret("magpie-api-key") or not _reading.acquire(blocking=False):
        return

    def go():
        try:
            work()
        except Exception as e:  # noqa: BLE001
            db.event("worker", f"读 magpie 异常：{e}", level="warn")
        finally:
            _reading.release()
    threading.Thread(target=go, name="magpie-read", daemon=True).start()


def read_if_stale() -> None:
    """A new read when what the page is about to show is old, without making the page wait for it."""
    try:
        at = datetime.fromisoformat((db.setting("magpie_state") or {}).get("checked_at") or "")
        if datetime.now(timezone.utc) - at < timedelta(seconds=STALE_AFTER):
            return
    except ValueError:
        pass
    _in_background(read)


def tick() -> None:
    """The worker's regular read, and the calls to report since the last one."""
    def work():
        read()
        report()
    _in_background(work)


# ---------------------------------------------------------------- what is in use
def vendor_of(route: str, models: list[dict] | None = None) -> str | None:
    """The subscription a route draws on: a model's vendor; a routing group's first member's (the catalog names that one)."""
    if not is_model(route):
        return None
    m = next((x for x in models or [] if x["id"] == route), None)
    return m["vendor"] if m else None if route.startswith("group/") else route.split("/")[0]


def view() -> dict:
    """The 机房's account of magpie: the read state, which subscriptions the routes draw on, which routes are off the list."""
    st = db.setting("magpie_state") or {}
    if not config.get().secret("magpie-api-key"):
        st = {**st, "ok": False, "error": "NO_KEY"}
    models = st.get("models") or []
    stages = {k: resolve(v) for k, v in knobs.get("stage_providers").items()}
    used = routes()
    listed = {m["id"] for m in models}
    missing = [r for r in used if is_model(r) and models and r not in listed]
    from .agy import missing_routes
    return {"url": base_url(), "state": st, "routes": {"default": resolve(knobs.get("llm_provider")), "stages": stages}, "missing": missing + missing_routes(),
            "vendors": [v for v in dict.fromkeys(vendor_of(r, models) for r in used) if v],
            "fallback": quota_view(db.setting("magpie_quota")), "report": db.setting("magpie_report")}


def windows_in_use() -> list[dict]:
    """The windows of the subscriptions the routes draw on, as of a read within two hours: what 补录 and a quota alert go by."""
    st = db.setting("magpie_state") or {}
    try:
        fresh = st.get("ok") and datetime.fromisoformat(st["read_at"]) > datetime.now(timezone.utc) - timedelta(hours=2)
    except (KeyError, TypeError, ValueError):
        fresh = False
    if not fresh:
        return []
    used = {vendor_of(r, st.get("models")) for r in routes()}
    return [w for s in st.get("subs") or [] if s.get("provider") in used for w in s.get("windows") or []]


def signed_out_in_use() -> list[str]:
    """The subscriptions in use whose account has to sign in again, by name: why a call through magpie may be refused."""
    st = db.setting("magpie_state") or {}
    used = {vendor_of(r, st.get("models")) for r in routes()}
    return [s["name"] for s in st.get("subs") or [] if s.get("signed_out") and s.get("provider") in used]


# ---------------------------------------------------------------- reporting agy's calls
def report() -> int:
    """Tell magpie's Usage page of the calls that did not go through it. magpie skips a `rid` it has, so a batch sent
    twice does no harm; the rows count in its totals, never against a limit. Returns how many went."""
    cursor = db.setting("magpie_report_cursor")
    if cursor is None:
        cut = (datetime.now(timezone.utc) - timedelta(days=REPORT_DAYS)).isoformat(timespec="seconds")
        first = db.one("SELECT MIN(id) m FROM model_calls WHERE created_at >= ?", cut)["m"]
        cursor = (first - 1) if first else (db.one("SELECT MAX(id) m FROM model_calls")["m"] or 0)
    last = db.one("SELECT MAX(id) m FROM model_calls WHERE id > ?", cursor)["m"]  # first: a call made from here on waits for the next round
    rows = db.q("""SELECT * FROM model_calls WHERE id > ? AND id <= ? AND model IS NOT NULL AND (agy_version IS NULL OR agy_version NOT LIKE 'magpie:%')
                   ORDER BY id LIMIT ?""", cursor, last or cursor, REPORT_BATCH)
    if not rows:
        if last:
            db.set_setting("magpie_report_cursor", last)
        return 0
    records = [_record(r) for r in rows]
    prev = db.setting("magpie_report") or {}
    try:
        _ask("/v1/magpie/usage/requests", method="POST", data=json.dumps(records).encode(), timeout=30)
    except Refused as e:
        db.set_setting("magpie_report", {**prev, "ok": False, "error": e.error, "at": now()})
        return 0
    # the rows below the batch's end that were not to be told (through magpie, no model) need no second look
    done = rows[-1]["id"] if len(rows) == REPORT_BATCH else last
    db.set_setting("magpie_report_cursor", done)
    db.set_setting("magpie_report", {"ok": True, "error": None, "at": now(), "total": (prev.get("total") or 0) + len(rows)})
    return len(rows)


def _record(r: dict) -> dict:
    t = datetime.fromisoformat(r["created_at"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    level = LEVELLED.fullmatch(r["model"])
    out = {"t": t, "agent": USER_AGENT, "provider": "agy", "model": level[1] if level else r["model"], "rid": f"assetizer-{r['id']}",
           "status": 200 if r["status"] == "SUCCESS" else 500, **({"effort": level[2]} if level else {})}
    for field, key in (("in", "input_tokens"), ("out", "output_tokens"), ("reasoning", "thinking_tokens")):
        if isinstance(r.get(key), int):
            out[field] = r[key]
    if isinstance(r.get("wall_seconds"), (int, float)):
        out["ms"] = int(r["wall_seconds"] * 1000)
    if r.get("run_id"):
        out["session"] = r["run_id"]
    if r.get("error_class"):
        out["err"] = r["error_class"]
    return out
