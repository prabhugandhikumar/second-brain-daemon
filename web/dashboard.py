"""
Web dashboard at briefing.tabp.co.in — login-protected.

Routes:
    GET  /            — dashboard (requires login)
    GET  /login       — login form
    POST /login       — verify, set cookie, redirect to /
    GET  /logout      — clear cookie, redirect to /login
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz
from fastapi import Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lib import notion_writer
from web import auth

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _require_login(request: Request) -> Optional[str]:
    cookie = request.cookies.get(auth.SESSION_COOKIE_NAME)
    return auth.read_session_cookie(cookie)


def register_dashboard_routes(app):
    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        username = _require_login(request)
        if not username:
            return RedirectResponse("/login")

        # Fetch open commitments and group by urgency
        rows = await notion_writer.get_open_commitments(limit=50)
        today = datetime.now(IST).date()
        grouped = _group_commitments(rows, today)

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "username": username,
                "today": today.strftime("%a %d %b %Y"),
                "grouped": grouped,
                "now": datetime.now(IST).strftime("%I:%M %p IST"),
            },
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, error: Optional[str] = None):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": error},
        )

    @app.post("/login")
    async def login_submit(username: str = Form(...), password: str = Form(...)):
        if auth.check_password(username, password):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                auth.SESSION_COOKIE_NAME,
                auth.make_session_cookie(username),
                max_age=auth.SESSION_MAX_AGE_SEC,
                httponly=True,
                secure=True,
                samesite="lax",
            )
            return response
        return RedirectResponse("/login?error=invalid", status_code=303)

    @app.get("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE_NAME)
        return response


def _group_commitments(rows: list, today) -> dict:
    """Group commitments by urgency for the dashboard sections."""
    today_iso = today.isoformat()
    groups = {"overdue": [], "today": [], "tomorrow": [], "week": [], "later": []}

    for r in rows:
        props = r.get("properties", {})
        title = " ".join(b.get("plain_text", "") for b in props.get("Commitment", {}).get("title", []))
        counterparty = " ".join(b.get("plain_text", "") for b in props.get("Counterparty", {}).get("rich_text", []))
        company = (props.get("Company", {}).get("select") or {}).get("name", "—")
        channel = (props.get("Counterparty Channel", {}).get("select") or {}).get("name", "—")
        notes = " ".join(b.get("plain_text", "") for b in props.get("Notes", {}).get("rich_text", []))
        due = (props.get("Due By", {}).get("date") or {}).get("start", "")
        promised = (props.get("Promised On", {}).get("date") or {}).get("start", "")
        url = r.get("url", "")
        page_id = r.get("id", "")

        aging_label = ""
        aging_class = ""
        if due:
            try:
                d = datetime.fromisoformat(due).date()
                days = (d - today).days
                if days < 0:
                    aging_label = f"{abs(days)}d overdue"
                    aging_class = "crit"
                elif days == 0:
                    aging_label = "due today"
                    aging_class = "warn"
                elif days == 1:
                    aging_label = "due tomorrow"
                    aging_class = "warn"
                else:
                    aging_label = f"due in {days}d"
                    aging_class = "cool"
            except Exception:
                pass

        commit_view = {
            "who": counterparty or "—",
            "what": title,
            "company": company,
            "channel": channel,
            "notes": notes[:160] + ("…" if len(notes) > 160 else ""),
            "due": due,
            "aging_label": aging_label,
            "aging_class": aging_class,
            "url": url,
            "id": page_id,
        }

        if not due:
            groups["later"].append(commit_view)
        elif due < today_iso:
            groups["overdue"].append(commit_view)
        elif due == today_iso:
            groups["today"].append(commit_view)
        else:
            d = datetime.fromisoformat(due).date()
            days_out = (d - today).days
            if days_out == 1:
                groups["tomorrow"].append(commit_view)
            elif days_out <= 7:
                groups["week"].append(commit_view)
            else:
                groups["later"].append(commit_view)

    return groups
