"""
LLM abstraction — provider-agnostic interface for vision classification
and text synthesis. Today: Gemini. Tomorrow: swap to Claude (via Vertex AI)
or OpenAI by switching the implementing class.

Public API:
    await classify_image(image_bytes: bytes, prompt: str) -> dict
    await synthesize_text(prompt: str, system: str = None) -> str
    await reason_about(prompt: str, context: dict) -> dict  # structured output
    await parse_audio(audio_bytes: bytes, prompt: str) -> dict  # transcript + intent

All four calls use a per-role MODEL CHAIN — an ordered list of Gemini
models to try. The first model is the "best" (latest, highest quality);
each subsequent model is a quota-fallback. When the primary model throws
ResourceExhausted (429 quota error), the client transparently retries
with the next model in the chain.

This keeps us on the newest Gemini model whenever quota is available, and
gracefully degrades to older but more generous models when it isn't.
"""

import json
import logging
import os
from typing import Optional

from lib import secrets

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Configuration: ordered fallback chains
# ─────────────────────────────────────────────────────────────────────
# Each list is "best to worst" — we try the first, fall back to the next
# on ResourceExhausted (quota / rate limit).
#
# THE CHAIN IS DATA, NOT CODE. The runtime values come from the
# GEMINI_MODEL_CHAIN env var (loaded from Secret Manager at startup).
# A weekly Cloud Scheduler job (`/cron/refresh-models` → lib/model_refresh.py)
# rebuilds the chain by querying Gemini's list_models() and writes the
# updated chain back to Secret Manager. The next Cloud Run cold start
# picks it up. This means we don't have to manually edit this file each
# time Google ships a new Flash model.
#
# The hardcoded DEFAULTS below are only used:
#   - On first run, before the refresh-models job has populated the secret
#   - As a safety net if the secret is malformed
DEFAULT_CHAIN = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]


def _current_chain() -> list[str]:
    """Read the chain from env, falling back to the hardcoded default.

    Called lazily — at each LLM call site, not at module import — because
    `lib.secrets.load_runtime_secrets()` populates os.environ during app
    startup AFTER lib/llm.py has already been imported.
    """
    raw = os.environ.get("GEMINI_MODEL_CHAIN", "")
    if not raw:
        return DEFAULT_CHAIN
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) and x for x in parsed):
            return parsed
    except json.JSONDecodeError:
        log.warning("GEMINI_MODEL_CHAIN is not valid JSON, using default")
    return DEFAULT_CHAIN


# Module-level shims kept for backwards compatibility with anything that
# imports these names directly. They're computed lazily via _current_chain()
# in each LLM call site, so changes to the env var take effect immediately.
def _models_for_role() -> list[str]:
    return _current_chain()


# ─────────────────────────────────────────────────────────────────────
# Gemini client (the active provider for v1)
# ─────────────────────────────────────────────────────────────────────
class _GeminiClient:
    def __init__(self):
        self._configured = False

    def _configure(self):
        if self._configured:
            return
        import google.generativeai as genai
        api_key = secrets.get("GEMINI_API_KEY")
        genai.configure(api_key=api_key)
        self._genai = genai
        self._configured = True

    def _call_with_fallback(self, models: list[str], parts,
                             generation_config: dict,
                             system: Optional[str] = None):
        """
        Try each model in `models` in order. On ResourceExhausted (429),
        log and continue to the next model. Returns (model_name, response).
        Raises the last error if every model in the chain is exhausted.
        """
        # Imported lazily so module import doesn't require the SDK
        from google.api_core.exceptions import ResourceExhausted, TooManyRequests

        self._configure()
        last_err: Optional[Exception] = None
        for idx, model_name in enumerate(models):
            try:
                m = self._genai.GenerativeModel(model_name, system_instruction=system)
                resp = m.generate_content(parts, generation_config=generation_config)
                if idx > 0:
                    log.warning(
                        "LLM call fell back to %s after %d exhausted model(s) "
                        "(primary: %s)", model_name, idx, models[0]
                    )
                else:
                    log.info("LLM call used %s", model_name)
                return model_name, resp
            except (ResourceExhausted, TooManyRequests) as e:
                # Quota / rate limit — try the next model in the chain.
                log.warning(
                    "model %s quota exhausted, trying next in chain: %s",
                    model_name, str(e)[:200]
                )
                last_err = e
                continue
            except Exception as e:
                # Some 429s come back wrapped — sniff the text for quota signals
                msg = str(e)
                if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower():
                    log.warning(
                        "model %s likely quota-exhausted (wrapped error), "
                        "trying next in chain: %s",
                        model_name, msg[:200]
                    )
                    last_err = e
                    continue
                # Non-quota error — re-raise immediately
                raise

        # Every model in the chain failed with quota errors
        assert last_err is not None
        log.error("entire LLM model chain exhausted: %s", [m for m in models])
        raise last_err

    async def classify_image(self, image_bytes: bytes, prompt: str,
                             mime_type: str = "image/jpeg",
                             models: Optional[list[str]] = None) -> dict:
        """Send a screenshot + prompt to a vision model. Returns dict."""
        models = models or _current_chain()
        _, resp = self._call_with_fallback(
            models,
            parts=[{"mime_type": mime_type, "data": image_bytes}, prompt],
            generation_config={
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        )
        text = resp.text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("LLM did not return valid JSON: %r", text)
            raise

    async def parse_audio(self, audio_bytes: bytes, prompt: str,
                           mime_type: str = "audio/ogg",
                           system: Optional[str] = None,
                           models: Optional[list[str]] = None) -> dict:
        """Send audio + prompt to Gemini. Returns dict (transcript + intent)."""
        models = models or _current_chain()
        _, resp = self._call_with_fallback(
            models,
            parts=[{"mime_type": mime_type, "data": audio_bytes}, prompt],
            generation_config={
                "temperature": 0.3,
                "response_mime_type": "application/json",
            },
            system=system,
        )
        text = resp.text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("LLM did not return valid JSON for audio parse: %r", text)
            raise

    async def reason(self, prompt: str, system: Optional[str] = None,
                     models: Optional[list[str]] = None,
                     json_output: bool = True) -> dict:
        """Small-form text reasoning, structured JSON output."""
        models = models or _current_chain()
        _, resp = self._call_with_fallback(
            models,
            parts=prompt,
            generation_config={
                "temperature": 0.3,
                "response_mime_type": "application/json" if json_output else "text/plain",
            },
            system=system,
        )
        text = resp.text.strip()
        if json_output:
            return json.loads(text)
        return {"text": text}

    async def synthesize(self, prompt: str, system: Optional[str] = None,
                         models: Optional[list[str]] = None) -> str:
        """Long-form text generation. Used for morning briefing + NL answers."""
        models = models or _current_chain()
        _, resp = self._call_with_fallback(
            models,
            parts=prompt,
            generation_config={"temperature": 0.5},
            system=system,
        )
        return resp.text.strip()


_client = _GeminiClient()


# ─────────────────────────────────────────────────────────────────────
# Public API — call these from anywhere in the daemon
# ─────────────────────────────────────────────────────────────────────
async def classify_image(image_bytes: bytes, prompt: str,
                          mime_type: str = "image/jpeg") -> dict:
    return await _client.classify_image(image_bytes, prompt, mime_type)


async def parse_audio(audio_bytes: bytes, prompt: str,
                       mime_type: str = "audio/ogg",
                       system: Optional[str] = None) -> dict:
    return await _client.parse_audio(audio_bytes, prompt, mime_type, system)


async def reason_about(prompt: str, system: Optional[str] = None,
                       json_output: bool = True) -> dict:
    return await _client.reason(prompt, system, json_output=json_output)


async def synthesize_text(prompt: str, system: Optional[str] = None) -> str:
    return await _client.synthesize(prompt, system)


# ─────────────────────────────────────────────────────────────────────
# Future: Claude swap
# ─────────────────────────────────────────────────────────────────────
# To swap synthesize_text() to Claude on Vertex AI later:
#   1. Add a _ClaudeVertexClient class with the same async signatures
#   2. In synthesize_text(), route to the new client if a config flag is set
#   3. No changes needed in classify.py / briefing.py / anywhere else
#
# Example:
#     USE_CLAUDE_FOR_SYNTH = secrets.get_optional("USE_CLAUDE_FOR_SYNTH", "0") == "1"
#     async def synthesize_text(...):
#         if USE_CLAUDE_FOR_SYNTH:
#             return await _claude_client.synthesize(...)
#         return await _client.synthesize(...)
