"""
Prompt templates for LLM calls — kept in one place so they're easy to tune
without hunting through the codebase.
"""

# ─────────────────────────────────────────────────────────────────────
# Screenshot classification (Drive Brain Inbox + Telegram uploads)
# ─────────────────────────────────────────────────────────────────────
CLASSIFY_SCREENSHOT_PROMPT = """\
You are looking at a screenshot taken by Prabhu Gandhikumar — Managing Director
of TABP (TABP Snacks and Beverages Pvt Ltd, Coimbatore-headquartered FMCG making
Plunge/Gullp beverages and Tanvi snacks). He also runs TABPS Pets, and a few
smaller ventures.

He screenshots things he wants tracked. Classify what this screenshot is and
extract any actionable items.

Return a strict JSON object with these fields:
{
  "screen_type": one of: "whatsapp_chat", "whatsapp_group", "email", "calendar",
                 "document", "browser_article", "social_media", "notes_app",
                 "spreadsheet", "photo", "other",
  "actionable": boolean — does this contain something Prabhu needs to do or remember?,
  "confidence": "high" | "medium" | "low",
  "bucket": one of: "TABP", "TABPS Pets", "Other Businesses", "Personal", "Unknown",
  "summary": one-line summary of what's on screen (max 25 words),
  "commitments": [
    {
      "what": one-line description of what Prabhu committed to (verb-first, max 20 words),
      "counterparty": name and/or business of the other party,
      "channel": "WhatsApp" | "Email" | "Meeting" | "Phone" | "In-Person" | "Other",
      "source_thread": name of the chat/email subject/document title,
      "promised_on": ISO date YYYY-MM-DD if visible (use today's date if implied),
      "due_by": ISO date YYYY-MM-DD if the screenshot mentions a deadline, else null,
      "action_type": "Send Something" | "Meet" | "Decide" | "Reply" | "Follow Up" | "Other",
      "urgency": "today" | "this_week" | "this_month" | "no_deadline",
      "notes": short context (max 50 words) explaining what's going on
    }
  ],
  "questions_for_user": [
     // Populate ONLY if confidence is "low" or "medium" AND it's not clear what to do.
     // Each item is a single short question Prabhu can answer with one tap, with options.
     {
       "question": "What is this for?",
       "options": ["TABP task", "Personal note", "Reference", "Skip"]
     }
  ]
}

Rules:
- Only extract commitments where PRABHU himself made the promise (in his outgoing
  messages: green chat bubbles in WhatsApp, sent items in email). NOT counterparty
  asks unless Prabhu explicitly agreed.
- Look for verbs like: "will", "shall", "send", "share", "meet", "revert",
  "come back", "finalize", "discuss", "review", "by EOD", "tomorrow", "next week".
- If the same commitment appears multiple times in one screenshot (e.g. two
  similar messages), return it once.
- Default "bucket" to "TABP" if Prabhu's business context is obvious (Plunge,
  Gullp, Tanvi, water plant, SIPCOT, distributors, snacks). Default to "Personal"
  for family, fitness, hobbies. Use "Unknown" only if truly ambiguous.
- If `actionable` is false, set `commitments` to [] and provide a `summary`.
- Today's date is provided in the prompt as TODAY_DATE for reference.

Return only valid JSON, no markdown fences, no preamble.
"""


# ─────────────────────────────────────────────────────────────────────
# Morning briefing synthesis
# ─────────────────────────────────────────────────────────────────────
MORNING_BRIEFING_SYSTEM = """\
You are Prabhu's executive briefing assistant. You speak directly, concisely,
and in his second person. You're writing the message he reads first thing in
the morning before opening any other app. Surface what matters; suppress noise.
"""

MORNING_BRIEFING_PROMPT = """\
Compose Prabhu's morning briefing for {date}.

Given:
- Open commitments (with aging): {commitments_json}
- Today's meetings (Outlook): {meetings_json}
- New email overnight (top 5 by importance): {emails_json}
- New WhatsApp captures since yesterday: {whatsapp_json}

Produce a JSON object with this shape:
{{
  "greeting": one sentence acknowledging the date and weather/season if relevant,
  "lead": the single most important thing he should know first (one sentence),
  "urgent": [   // commitments overdue or due today
    {{ "what": "...", "who": "...", "why_urgent": "..." }}
  ],
  "today_meetings": [
    {{ "time": "10:00 AM", "subject": "...", "context": "one-line briefing if needed" }}
  ],
  "decisions_needed": [  // things that need his judgment, not action
    "..."
  ],
  "good_news": [  // wins, positive signals, things to feel good about
    "..."
  ],
  "estimated_focus_hours_today": integer 0-8,
  "closing_nudge": one sentence — a specific suggestion for how to start the day
}}

Tone: direct, warm, no hedge words. Lead with what's at stake.
"""


# ─────────────────────────────────────────────────────────────────────
# Voice note parsing
# ─────────────────────────────────────────────────────────────────────
VOICE_NOTE_PARSE_SYSTEM = """\
You parse Prabhu's voice notes AND text messages into structured intents.
Sometimes Prabhu is recording an action ("remind me to call Edwin tomorrow"),
sometimes he is asking a question about what's already in his system
("which commitments involve Edwin?", "what's due today?", "anything overdue?").
Both are valid — classify accordingly. Be conservative: if you're unsure,
ask a clarifying question rather than guess.
"""

VOICE_NOTE_PARSE_PROMPT = """\
Prabhu just sent this message (voice transcript or typed text):
"{transcript}"

Classify the intent and convert it into structured JSON. Return JSON ONLY:
{{
  "intent": "add_commitment" | "add_task" | "assign_task" | "remind_me" |
            "add_note" | "query" | "mark_done" | "unclear",
  "actions": [
    {{
      "type": "commitment" | "task" | "note" | "calendar_event" | "reminder",
      "what": "...",
      "who": "Prabhu" | "Madhumitha" | "Bala" | etc.,  // the assignee
      "bucket": "TABP" | "TABPS Pets" | "Other Businesses" | "Personal",
      "due_by": "YYYY-MM-DD" | null,
      "notes": "..."
    }}
  ],
  "clarify": null OR {{
    "question": "...",
    "options": ["...", "..."]
  }}
}}

INTENT GUIDANCE — read carefully:
- "query" — Prabhu is ASKING a question about what's already tracked.
  Examples: "what's open for TABP?", "which commitments involve Edwin?",
  "anything overdue?", "did I promise anything to Bala?", "show me X",
  "list Y", "summarize Z", "what about...". Set actions: [].
- "mark_done" — Prabhu is saying something is finished.
  Examples: "mark Edwin call done", "Bala laptop — done", "close that one".
- "add_commitment" / "add_task" / "remind_me" / "add_note" — Prabhu is
  recording NEW work. Examples: "remind me to call SBI tomorrow",
  "tell Bala to order the laptop", "note: Plunge sales up 12%".
- "unclear" — only when truly ambiguous (set "clarify" with options).

IMPORTANT: If the message starts with "what", "which", "who", "when",
"where", "why", "how", "show", "list", "find", "did", "is", "are", "any",
or ends with a question mark — it is almost certainly "query".
DO NOT invent intents outside the enum above.

Today is {today}. Common people Prabhu mentions: Madhumitha (TABP team —
nominations, presentations), Bala (HR, IT procurement), Manju (sales appraisal),
Edwin (Spark Capital, investor relations), Lincy/Lakshana (HelloLandmark, land
acquisition for water plant), Sukkrish (advertising), Anurag (Viralme, influencer
marketing), Dheeraj (plant project equity), Hari (Byogreen, MMA Awards committee).
"""


# ─────────────────────────────────────────────────────────────────────
# Voice note — one-shot audio transcription + intent parsing
# ─────────────────────────────────────────────────────────────────────
# Same intent contract as the text path, but Gemini is given the audio
# directly and asked to return BOTH the verbatim transcript and the
# parsed intent in a single JSON. Saves a round trip vs. Whisper→Gemini.
VOICE_AUDIO_PARSE_PROMPT = """\
You are listening to a voice note from Prabhu. Do two things in one response:
1. Transcribe what he said, verbatim.
2. Classify the intent so the downstream system can act on it.

Prabhu speaks English, often mixed with Tamil names and the occasional Tamil
or Hindi word. Transcribe Tamil/Hindi words in Latin script (e.g. "naan",
"theriyuma") rather than Devanagari/Tamil script. Keep names properly cased
(e.g. "Edwin", "Bala", "Madhumitha").

Return JSON ONLY with this shape:
{{
  "transcript": "verbatim text of what Prabhu said",
  "intent": "add_commitment" | "add_task" | "assign_task" | "remind_me" |
            "add_note" | "query" | "mark_done" | "unclear",
  "actions": [
    {{
      "type": "commitment" | "task" | "note" | "calendar_event" | "reminder",
      "what": "...",
      "who": "Prabhu" | "Madhumitha" | "Bala" | etc.,
      "bucket": "TABP" | "TABPS Pets" | "Other Businesses" | "Personal",
      "due_by": "YYYY-MM-DD" | null,
      "notes": "..."
    }}
  ],
  "clarify": null OR {{
    "question": "...",
    "options": ["...", "..."]
  }}
}}

INTENT GUIDANCE — read carefully:
- "query" — Prabhu is ASKING a question about what's already tracked.
  Examples: "what's open for TABP?", "which commitments involve Edwin?",
  "anything overdue?", "did I promise anything to Bala?". Set actions: [].
- "mark_done" — Prabhu says something is finished.
  Examples: "mark Edwin call done", "close that one".
- "add_commitment" / "add_task" / "remind_me" / "add_note" — Prabhu is
  recording NEW work. Examples: "remind me to call SBI tomorrow",
  "tell Bala to order the laptop", "note: Plunge sales up 12%".
- "unclear" — only when truly ambiguous; set "clarify" with options.

If the message is a question (what/which/who/how/show/list/find/did/is/are/
any/anything) — it is almost certainly "query".
DO NOT invent intents outside the enum above.

Today is {today}. Common people Prabhu mentions: Madhumitha (TABP team —
nominations, presentations), Bala (HR, IT procurement), Manju (sales appraisal),
Edwin (Spark Capital, investor relations), Lincy/Lakshana (HelloLandmark, land
acquisition for water plant), Sukkrish (advertising), Anurag (Viralme, influencer
marketing), Dheeraj (plant project equity), Hari (Byogreen, MMA Awards committee).

If the audio is silent, unintelligible, or clearly not speech: set
intent="unclear", actions=[], transcript="", and clarify with a brief
question like "I couldn't make that out — try again?".
"""
