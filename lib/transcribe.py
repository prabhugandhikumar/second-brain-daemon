"""
DEPRECATED 2026-05-16.

This module previously used OpenAI Whisper to transcribe Telegram voice
notes. It's no longer used — voice notes now go directly to Gemini Flash
via `lib.llm.parse_audio()`, which transcribes AND classifies intent in a
single call. See handlers/telegram.py:_handle_voice and lib/prompts.py:
VOICE_AUDIO_PARSE_PROMPT.

Kept as a stub so any stale import surfaces loudly instead of silently
loading dead code.
"""

raise ImportError(
    "lib.transcribe was retired on 2026-05-16. Use lib.llm.parse_audio() "
    "with VOICE_AUDIO_PARSE_PROMPT instead — see handlers/telegram.py."
)
