"""
Thin async wrapper over the Telegram Bot HTTP API.
Why no python-telegram-bot lib here: we want explicit control over what
goes out + minimal startup time on Cloud Run (cold starts matter).
"""

import logging
from typing import Optional

import httpx

from lib import secrets

log = logging.getLogger(__name__)


def _base() -> str:
    return f"https://api.telegram.org/bot{secrets.get('TELEGRAM_BOT_TOKEN')}"


async def send_message(chat_id: int | str, text: str,
                       reply_markup: Optional[dict] = None,
                       parse_mode: Optional[str] = "Markdown",
                       reply_to_message_id: Optional[int] = None) -> dict:
    """
    Send a text message.

    parse_mode defaults to "Markdown" for backwards compatibility with
    existing callers. Pass parse_mode=None when the text contains
    LLM-generated content with potentially unbalanced *…* or _…_ — the
    payload will then skip parse_mode entirely and Telegram treats the
    text as plain.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{_base()}/sendMessage", json=payload)
        if r.status_code >= 400:
            log.error("telegram sendMessage failed: %s", r.text)
        r.raise_for_status()
        return r.json()


async def send_photo(chat_id: int | str, photo_bytes: bytes,
                     caption: Optional[str] = None,
                     reply_markup: Optional[dict] = None,
                     filename: str = "screenshot.jpg",
                     parse_mode: Optional[str] = None) -> dict:
    """
    Send a photo with optional caption.

    parse_mode defaults to None (plain text). Pass "Markdown" or "HTML"
    only when you control the caption text and have verified it's
    well-formed. Captions built from LLM output should stay as plain text
    because LLMs occasionally emit unbalanced *…* or _…_ which Telegram
    rejects with `Bad Request: can't parse entities`.
    """
    files = {"photo": (filename, photo_bytes, "image/jpeg")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1024]  # Telegram limit
        if parse_mode:
            data["parse_mode"] = parse_mode
    if reply_markup:
        import json as _j
        data["reply_markup"] = _j.dumps(reply_markup)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{_base()}/sendPhoto", data=data, files=files)
        if r.status_code >= 400:
            log.error("telegram sendPhoto failed: %s", r.text)
        r.raise_for_status()
        return r.json()


async def get_file_bytes(file_id: str) -> bytes:
    """Two-step download: get_file() returns a path, then GET the path."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{_base()}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        token = secrets.get("TELEGRAM_BOT_TOKEN")
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        r2 = await c.get(download_url)
        r2.raise_for_status()
        return r2.content


async def answer_callback_query(callback_id: str, text: Optional[str] = None) -> None:
    """Acknowledge an inline button tap so the loading spinner disappears."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{_base()}/answerCallbackQuery", json=payload)


async def edit_message_reply_markup(chat_id: int | str, message_id: int,
                                     reply_markup: Optional[dict]) -> None:
    """Remove buttons from a message after they've been used."""
    payload = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{_base()}/editMessageReplyMarkup", json=payload)


def inline_keyboard(rows: list[list[dict]]) -> dict:
    """Convenience helper to build Telegram InlineKeyboardMarkup."""
    return {"inline_keyboard": rows}


def button(text: str, callback_data: str) -> dict:
    """Single inline button. callback_data must be ≤64 bytes."""
    return {"text": text, "callback_data": callback_data[:64]}
