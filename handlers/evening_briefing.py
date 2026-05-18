"""
Evening briefing — runs at 9 PM IST via Cloud Scheduler.

Forward-looking digest for the next day. Sent before bed so Prabhu can
prep tonight instead of discovering tomorrow's load in the morning.

Sections (per design conversation 2026-05-17):
  1. Tomorrow's Outlook calendar (the spine of tomorrow)
  2. Meetings he agreed to that aren't on the calendar yet
  3. Commitments due tomorrow (non-meeting action items)
  4. Prep-tonight suggestions — 1-3 things doable in <15 min that smooth tomorrow
  5. Closing thought — calm framing for sleep

Re-uses fetch helpers from handlers/briefing.py so the data shape stays
identical across both briefings.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import pytz

from handlers import briefing as morning  # shared helpers
from lib import llm
from lib import notion_writer
from lib import telegram_client as tg
from lib.prompts import EVENING_BRIEFING_PROMPT, EVENING_BRIEFING_SYSTEM

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


async def run_evening_briefing() -> dict:
    """Main entry. Called by /cron/evening-briefing."""
    today = datetime.now(IST).date()
    tomorrow = today + timedelta(days=1)
    today_iso = today.isoformat()
    tomorrow_iso = tomorrow.isoformat()
    log.info("evening briefing for tomorrow=%s starting", tomorrow_iso)

    # 1. Open commitments (full set; LLM uses for context and "due tomorrow")
    raw_rows = await notion_writer.get_open_commitments(limit=100)
    commitments = morning._summarize_rows(raw_rows, today)

    # 2. Tomorrow's Outlook calendar
    meetings = await morning.fetch_meetings_for(tomorrow)

    # 3. Unscheduled meetings — agreed in chat/email, due tomorrow or within 3 days
    unscheduled_meetings = morning._filter_unscheduled_meetings(
        commitments, window_start=tomorrow, window_days=3
    )

    # 4. Commitments due tomorrow that are NOT meetings (those are unscheduled_meetings)
    due_tomorrow = [
        c for c in commitments
        if c.get("due_by") == tomorrow_iso and c.get("action_type") != "Meet"
    ]

    # 5. Synthesize
    prompt = EVENING_BRIEFING_PROMPT.format(
        tomorrow_date=tomorrow_iso,
        today_date=today_iso,
        meetings_json=json.dumps(meetings, default=str),
        unscheduled_meetings_json=json.dumps(unscheduled_meetings, default=str),
        due_tomorrow_json=json.dumps(due_tomorrow, default=str),
        commitments_json=json.dumps(commitments[:50], default=str),
    )
    briefing = await llm.reason_about(prompt, system=EVENING_BRIEFING_SYSTEM)

    # 6. Deliver to Telegram
    await _deliver_evening(briefing, tomorrow)

    log.info(
        "evening briefing delivered: schedule=%d unscheduled=%d due_tomorrow=%d",
        len(briefing.get("schedule", [])),
        len(briefing.get("unscheduled_meetings", [])),
        len(briefing.get("due_tomorrow", [])),
    )
    return {
        "ok": True,
        "tomorrow": tomorrow_iso,
        "schedule_count": len(briefing.get("schedule", [])),
    }


async def _deliver_evening(briefing: dict, tomorrow) -> None:
    owner_id = int(os.environ.get("TELEGRAM_OWNER_CHAT_ID", "0"))
    if owner_id == 0:
        log.warning("TELEGRAM_OWNER_CHAT_ID not set — skipping evening DM")
        return
    msg = _render_evening(briefing, tomorrow)
    try:
        await tg.send_message(owner_id, msg)
    except Exception as e:
        log.warning("evening briefing send failed: %s", e)


def _render_evening(briefing: dict, tomorrow) -> str:
    lines = [
        f"*🌙 Tomorrow — {tomorrow.strftime('%a %d %b')}*",
        "",
        briefing.get("greeting", ""),
        "",
    ]
    lead = briefing.get("lead")
    if lead:
        lines += [f"*Lead:* {lead}", ""]

    # Schedule
    schedule = briefing.get("schedule", [])
    if schedule:
        meeting_hours = briefing.get("estimated_meeting_hours_tomorrow", 0)
        header = f"*📅 Schedule* ({len(schedule)} meetings"
        if meeting_hours:
            header += f", ~{meeting_hours} hrs blocked"
        header += ")"
        lines.append(header)
        for s in schedule:
            time = s.get("time", "")
            subject = s.get("subject", "")
            with_who = s.get("with", "")
            company = s.get("company", "")
            tag = f" · _{company}_" if company and company != "Personal" else ""
            who_tag = f" — {with_who}" if with_who else ""
            lines.append(f"• *{time}* {subject}{who_tag}{tag}")
            prep = s.get("prep_notes", "")
            if prep:
                lines.append(f"  _{prep}_")
        lines.append("")

    # Unscheduled meetings — need to land on calendar
    unsched = briefing.get("unscheduled_meetings", [])
    if unsched:
        lines.append("*🤝 Meetings to put on the calendar*")
        for m in unsched[:8]:
            who = m.get("with", "")
            what = m.get("what", "")
            promised = m.get("promised", "")
            suggestion = m.get("suggestion", "")
            lines.append(f"• *{who}* — {what}" + (f" _(promised {promised})_" if promised else ""))
            if suggestion:
                lines.append(f"  → {suggestion}")
        lines.append("")

    # Due tomorrow
    due = briefing.get("due_tomorrow", [])
    if due:
        lines.append("*📌 Due tomorrow*")
        for d in due[:8]:
            who = d.get("who", "")
            what = d.get("what", "")
            company = d.get("company", "")
            tag = f" · _{company}_" if company and company != "Personal" else ""
            lines.append(f"• *{who}*: {what}{tag}")
        lines.append("")

    # Prep tonight
    prep = briefing.get("prep_tonight", [])
    if prep:
        lines.append("*🛏️ Knock out tonight*")
        for p in prep[:5]:
            lines.append(f"• {p}")
        lines.append("")

    # Closing thought
    thought = briefing.get("closing_thought")
    if thought:
        lines += ["—", f"_{thought}_"]

    return "\n".join(lines)
