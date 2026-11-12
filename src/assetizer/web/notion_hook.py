"""Webhook ingress and authenticated setup, kept separate from Notion delivery."""
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..notion import hook

router = APIRouter()


@router.post(hook.PATH)
async def webhook(request: Request):
    if retry := hook.rate_limit():
        return JSONResponse({}, status_code=429, headers={"Retry-After": str(retry)})
    raw = bytearray()
    async for part in request.stream():
        if len(raw) + len(part) > hook.LIMIT:
            return JSONResponse({}, status_code=413)
        raw.extend(part)
    try:
        code = await run_in_threadpool(hook.receive, bytes(raw), request.headers.get("x-notion-signature"))
    except Exception:  # no body, token, signature or exception text in a web error or a log
        return JSONResponse({}, status_code=503)
    return JSONResponse({}, status_code=code)


@router.get("/api/notion/hook")
def hook_status():
    return JSONResponse(hook.status(), headers={"Cache-Control": "no-store"})


@router.get("/api/notion/hook/token")
def hook_token():
    value = hook.token()
    return JSONResponse({"token": value} if value else {}, status_code=200 if value else 404,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/notion/hook/rearm")
def hook_rearm():
    return JSONResponse(hook.rearm(), headers={"Cache-Control": "no-store"})


@router.post("/api/notion/hook/tunnel")
async def hook_tunnel(request: Request):
    """Place or clear the tunnel token: {token?, clear?: ["token"]}. Write-only; the answer is the hook's status."""
    from .. import db
    from .app import _secret_body
    body = await _secret_body(request, {"token"})
    if isinstance(body, JSONResponse):
        return body
    token = "".join((body.get("token") or "").split())
    if not token and "token" not in (body.get("clear") or []):
        return JSONResponse({"detail": "没有要修改的内容"}, status_code=400)
    if token and not hook.tunnel_token_ok(token):
        return JSONResponse({"detail": "这不像 Cloudflare 隧道 token", "field": "token"}, status_code=400)
    await run_in_threadpool(hook.put_tunnel_token, token or None)
    db.event("config", "批注推送的隧道 token：" + ("已更新" if token else "已清除"))
    return JSONResponse(await run_in_threadpool(hook.status), headers={"Cache-Control": "no-store"})
