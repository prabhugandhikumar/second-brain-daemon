"""
Screenshot classification — sends an image to the LLM with the canonical
prompt and returns a structured classification dict.

Usage:
    result = await classify_screenshot(image_bytes, source_hint="Drive: Brain Inbox")
    if result["actionable"]:
        for c in result["commitments"]:
            await notion_writer.add_commitment(c, source_file_id=file_id)
"""

import logging
from datetime import datetime
from typing import Optional

import pytz

from lib import llm
from lib.prompts import CLASSIFY_SCREENSHOT_PROMPT

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


async def classify_screenshot(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    source_hint: Optional[str] = None,
) -> dict:
    """
    Pass a screenshot through Gemini vision with the canonical prompt.
    Returns the parsed JSON dict (see prompts.py for shape).

    Always sets two derived fields:
      - "needs_user_input": True iff confidence is low OR questions_for_user is non-empty
      - "processed_at": ISO timestamp in IST
    """
    today_ist = datetime.now(IST).date().isoformat()
    prompt = CLASSIFY_SCREENSHOT_PROMPT
    if source_hint:
        prompt += f"\n\nSOURCE_HINT: This screenshot came from: {source_hint}\n"
    prompt += f"\nTODAY_DATE: {today_ist}\n"

    try:
        result = await llm.classify_image(image_bytes, prompt, mime_type=mime_type)
    except Exception as e:
        log.exception("classify_screenshot failed")
        # Return a safe fallback that surfaces the failure to the user
        return {
            "screen_type": "other",
            "actionable": False,
            "confidence": "low",
            "bucket": "Unknown",
            "summary": f"(classification error: {e})",
            "commitments": [],
            "questions_for_user": [
                {
                    "question": "The classifier couldn't read this screenshot. What is it?",
                    "options": ["TABP commitment", "Personal note", "Reference", "Skip"],
                }
            ],
            "needs_user_input": True,
            "processed_at": datetime.now(IST).isoformat(),
            "_error": str(e),
        }

    # Derived fields
    result["needs_user_input"] = bool(
        result.get("confidence") in ("low", "medium")
        and result.get("questions_for_user")
    )
    result["processed_at"] = datetime.now(IST).isoformat()

    # Defensive shape — ensure required keys exist
    result.setdefault("commitments", [])
    result.setdefault("questions_for_user", [])
    result.setdefault("summary", "")

    log.info(
        "classified: type=%s actionable=%s bucket=%s confidence=%s commitments=%d",
        result.get("screen_type"),
        result.get("actionable"),
        result.get("bucket"),
        result.get("confidence"),
        len(result["commitments"]),
    )
    return result


def is_noise(classification: dict) -> bool:
    """
    Heuristic: is this screenshot worth keeping vs silently archiving?
    Per Prabhu's rule (2026-05-11): "I almost never screenshot without utility,
    so ask on noise too." So this returns False unless the model is HIGHLY
    confident it's spam/non-actionable (e.g., ads, random web articles he
    captured for nothing in particular).

    Conservative: when in doubt, treat as user-action-needed.
    """
    if not classification.get("actionable", False):
        # Even non-actionable things might be worth keeping as references.
        # Only mark as noise if explicitly low confidence + non-actionable + no commitments.
        if (
            classification.get("confidence") == "high"
            and classification.get("screen_type") in ("social_media", "browser_article")
            and not classification.get("commitments")
        ):
            # Likely a meme, ad, or random web read
            return True
    return False
