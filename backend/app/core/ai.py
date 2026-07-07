"""The ONE place the Anthropic key is used.

Every LLM call in the platform routes through ``call_claude`` /
``call_claude_json`` so each is logged and gatable, and so the key never
leaves the backend. When ``ANTHROPIC_API_KEY`` is unset, calls raise
``AIUnavailableError`` and agents fall back to a degraded, human-review-only
recommendation — the demo still walks end-to-end without a key.

Treat all model input derived from intake (email bodies, uploads,
transcripts) as UNTRUSTED. Prompt injection is real; the true control is the
human-approval gate downstream, never anything asserted here.
"""

from __future__ import annotations

import json
import logging
import re

from app.core.config import settings

logger = logging.getLogger("aegis.ai")

_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class AIUnavailableError(RuntimeError):
    """No API key configured, or the provider call failed. Callers degrade."""


class AIResponseFormatError(RuntimeError):
    """Model returned something that was not the requested JSON object."""


def ai_available() -> bool:
    return bool(settings.anthropic_api_key)


def friendly_ai_error(exc: Exception) -> str:
    if isinstance(exc, AIUnavailableError):
        return "AI is temporarily unavailable — surfaced a template draft for review."
    return f"AI call failed: {type(exc).__name__}."


async def call_claude(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    """Return Claude's text completion for ``prompt``.

    Raises ``AIUnavailableError`` when no key is configured or the provider
    call fails, so agents route through their degraded fallback.
    """
    if not settings.anthropic_api_key:
        raise AIUnavailableError("ANTHROPIC_API_KEY is not configured.")

    try:
        # Imported lazily so the process starts without the SDK's network
        # client when AI is unused.
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        message = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are a careful legal-operations assistant.",
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()
    except AIUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalize every provider error
        logger.warning("call_claude failed: %s", exc)
        raise AIUnavailableError(str(exc)) from exc


async def call_claude_json(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> dict:
    """Call Claude and parse a single JSON object from the response."""
    raw = await call_claude(
        prompt, system=system, max_tokens=max_tokens, temperature=temperature
    )
    cleaned = _CODE_FENCE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Salvage the first {...} block if the model added prose around it.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        raise AIResponseFormatError("Claude did not return valid JSON.")
