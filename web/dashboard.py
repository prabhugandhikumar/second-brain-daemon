"""
Web dashboard at briefing.tabp.co.in — login-protected.

Three-tab structure (designed 2026-05-18):

    [Today] [Tomorrow] [All Open]

    - Today    : today's Outlook calendar + agreed meetings not on calendar +
                 commitments due today
    - Tomorrow : tomorrow's Outlook calendar + agreed meetings due in next 3d +
                 commitments due tomorrow (the evening-prep view)
    - All Open : current grouped view (overdue / today / tomorrow / week / later)

The dashboard makes ZERO LLM calls — it's a fast structured view of the raw
data. The synthesised, prioritised version lives in the morning + evening
briefings sent via Telegram.

Routes:
    GET  /            — dashboard (requires login)
    GET  /login       — login form
    POST /login       — verify, set cookie, redirect to /
    GET  /logout      — clear cookie, redirect to /login
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from fastapi import Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from handlers import briefing as briefing_helpers
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

        today = datetime.now(IST).date()
        tomorrow = today + timedelta(days=1)
        today_iso = today.isoformat()
        tomorrow_iso = tomorrow.isoformat()

        # 1) Open commitments (full set, used for all three tabs)
        rows = await notion_writer.get_open_commitments(limit=100)
        commitments = briefing_helpers._summarize_rows(rows, today)

        # 2) Group commitments by due-date bucket (for "All Open" tab)
        grouped = _group_commitments(rows, today)

        # 3) Calendar — today's and tomorrow's Outlook events
        try:
            today_meetings = await briefing_helpers._fetch_today_meetings()
        except Exception as e:
            log.warning("dashboard: today's calendar fetch failed: %s", e)
            today_meetings = []
        try:
            tomorrow_meetings = await briefing_helpers.fetch_meetings_for(tomorrow)
        except Exception as e:
            log.warning("dashboard: tomorrow's calendar fetch failed: %s", e)
            tomorrow_meetings = []

        # 4) Unscheduled meetings (commitments with action_type=Meet)
        unsched_today = briefing_helpers._filter_unscheduled_meetings(
            commitments, window_start=today, window_days=1
        )
        unsched_tomorrow = briefing_helpers._filter_unscheduled_meetings(
            commitments, window_start=tomorrow, window_days=3
        )

        # 5) Due-today and due-tomorrow non-meeting commitments
        due_today = [
            c for c in commitments
            if c.get("due_by") == today_iso and c.get("action_type") != "Meet"
        ]
        due_tomorrow = [
            c for c in commitments
            if c.get("due_by") == tomorrow_iso and c.get("action_type") != "Meet"
        ]

        # 6) Find each commitment's matching Notion URL for the "↗ Notion" buttons
        url_by_id = {r.get("id"): r.get("url", "") for r in rows}

        def _attach_url(items: list[dict]) -> list[dict]:
            out = []
            for item in items:
                copy = dict(item)
                copy["url"] = url_by_id.get(item.get("id") or item.get("page_id"), "")
                out.append(copy)
            return out

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "username": username,
                "today_label": today.strftime("%a %d %b %Y"),
                "tomorrow_label": tomorrow.strftime("%a %d %b %Y"),
                "now": datetime.now(IST).strftime("%I:%M %p IST"),
                "grouped": grouped,
                "today_meetings": today_meetings,
                "tomorrow_meetings": tomorrow_meetings,
                "unsched_today": _attach_url(unsched_today),
                "unsched_tomorrow": _attach_url(unsched_tomorrow),
                "due_today": _attach_url(due_today),
                "due_tomorrow": _attach_url(due_tomorrow),
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
    """Group commitments by urgency for the 'All Open' tab.

    Same grouping as before — overdue / today / tomorrow / this week / later —
    just kept its own pass for the rich rendering (notes, aging label, etc.)
    that the schedule-section views don't need.
    """
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
