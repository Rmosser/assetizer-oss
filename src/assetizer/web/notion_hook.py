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
