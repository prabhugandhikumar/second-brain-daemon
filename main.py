"""
Second Brain daemon — single Cloud Run service entry point.

Routes:
  POST /webhook/telegram        — bot messages, voice notes, photos, button taps
  POST /cron/poll-drive          — Cloud Scheduler every 15 min
  POST /cron/morning-briefing    — Cloud Scheduler at 5 AM IST
  POST /cron/refresh-models      — Cloud Scheduler weekly (Sun 03:00 IST)
  GET  /                         — dashboard (login-protected)
  GET/POST /login                — auth
  GET  /logout                   — clear session
  POST /api/done/{page_id}       — dashboard "✓ Done" button
  POST /api/snooze/{page_id}/{d} — dashboard "⏰ Snooze" button
  GET  /healthz                  — health probe for Cloud Run
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from handlers.telegram import handle_telegram_update
from handlers.drive_poll import run_drive_poll
from handlers.briefing import run_morning_briefing
from lib.secrets import load_runtime_secrets
from lib.model_refresh import refresh_model_chain
from lib import notion_writer
from web.dashboard import register_dashboard_routes
from web.auth import read_session_cookie, SESSION_COOKIE_NAME

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("secondbrain")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("secondbrain daemon starting up")
    try:
        await load_runtime_secrets()
    except Exception as e:
        log.exception("secret load failed (continuing): %s", e)
    yield
    log.info("secondbrain daemon shutting down")


app = FastAPI(
    title="Second Brain Daemon",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


# ─────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────
@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "secondbrain"}


# ─────────────────────────────────────────────────────────────────────
# Telegram webhook
# ─────────────────────────────────────────────────────────────────────
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        payload = await request.json()
        log.info("telegram update id=%s", payload.get("update_id"))
        await handle_telegram_update(payload)
    except Exception as e:
        log.exception("telegram webhook error")
        # Don't return error — Telegram retries on non-200, and we don't want
        # poison messages to retry forever. Always 200.
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────
# Cron: poll Drive every 15 min
# ─────────────────────────────────────────────────────────────────────
@app.post("/cron/poll-drive")
async def cron_poll_drive(request: Request):
    log.info("cron: poll-drive triggered")
    result = await run_drive_poll()
    return result


# ─────────────────────────────────────────────────────────────────────
# Cron: morning briefing at 5 AM IST
# ─────────────────────────────────────────────────────────────────────
@app.post("/cron/morning-briefing")
async def cron_morning_briefing(request: Request):
    log.info("cron: morning-briefing triggered")
    result = await run_morning_briefing()
    return result


# ─────────────────────────────────────────────────────────────────────
# Cron: weekly Gemini model-chain refresh (Sun 03:00 IST)
# ─────────────────────────────────────────────────────────────────────
@app.post("/cron/refresh-models")
async def cron_refresh_models(request: Request):
    log.info("cron: refresh-models triggered")
    result = await refresh_model_chain()
    return result


# ─────────────────────────────────────────────────────────────────────
# Dashboard API endpoints (used by buttons in dashboard.html)
# ─────────────────────────────────────────────────────────────────────
def _require_login_or_redirect(request: Request):
    username = read_session_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if not username:
        return RedirectResponse("/login", status_code=303)
    return None


@app.post("/api/done/{page_id}")
async def api_done(page_id: str, request: Request):
    redirect = _require_login_or_redirect(request)
    if redirect:
        return redirect
    try:
        await notion_writer.mark_commitment_done(page_id)
    except Exception as e:
        log.exception("api/done failed for %s", page_id)
    return RedirectResponse("/", status_code=303)


@app.post("/api/snooze/{page_id}/{days}")
async def api_snooze(page_id: str, days: int, request: Request):
    redirect = _require_login_or_redirect(request)
    if redirect:
        return redirect
    try:
        await notion_writer.snooze_commitment(page_id, days=days)
    except Exception as e:
        log.exception("api/snooze failed for %s", page_id)
    return RedirectResponse("/", status_code=303)


# ─────────────────────────────────────────────────────────────────────
# Dashboard routes — registers GET / + login + logout
# ─────────────────────────────────────────────────────────────────────
register_dashboard_routes(app)


# ─────────────────────────────────────────────────────────────────────
# Generic error handler
# ─────────────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def all_exceptions(request: Request, exc: Exception):
    log.exception("unhandled exception on %s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": str(exc)},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=os.environ.get("ENV") == "development",
    )
