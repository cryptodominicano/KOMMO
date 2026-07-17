"""LLM call. The system prompt is the persona + flows; the KB arrives as
retrieved context. Provider-agnostic: the same prompt drives OpenAI or Claude."""
import httpx
from .config import settings
from . import client as client_pack
from .retry import post_with_retry

# The prompt lives in the CLIENT PACK (clients/<id>/prompts/system.md).
# This module used to hardcode /srv/prompts/system.md - a leftover from before
# the engine was made client-agnostic. That path has never existed in the
# image, so generate() raised FileNotFoundError on EVERY message. worker.py
# catches Exception broadly, so the customer would simply have been ghosted,
# silently, forever. Unit tests missed it because they asserted
# client.system_prompt() works - which it does; nothing exercised THIS path.
# Caught only by running the real container. Deploy IS a test.


def system_prompt() -> str:
    return client_pack.system_prompt()


def _system(kb_context: str) -> str:
    return (
        system_prompt()
        + "\n\n# FUENTES DE CONOCIMIENTO (usa SOLO esta información)\n"
        + kb_context
    )


async def _openai(system: str, msgs: list[dict]) -> str:
    # Retry matters here: this account is capped at 30k TOKENS/min and each
    # reply costs ~6k (system prompt + retrieved KB), so ~5 replies/min fit.
    # A real lunchtime burst hits 429, and without a retry the customer is
    # silently ghosted by worker.py broad except.
    async with httpx.AsyncClient(timeout=90.0) as c:
        r = await post_with_retry(
            c,
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "max_tokens": settings.llm_max_tokens,
                "temperature": settings.llm_temperature,
                "messages": [{"role": "system", "content": system}] + msgs,
            },
        )
        return r.json()["choices"][0]["message"]["content"].strip()


async def _anthropic(system: str, msgs: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=90.0) as c:
        r = await post_with_retry(
            c,
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.anthropic_api_key,
                     "anthropic-version": "2023-06-01"},
            json={
                "model": settings.claude_model,
                "max_tokens": settings.llm_max_tokens,
                "system": system,
                "messages": msgs,
            },
        )
        return "".join(b["text"] for b in r.json()["content"]
                       if b["type"] == "text").strip()


async def generate(user_text: str, kb_context: str, history: list[dict]) -> str:
    """history: [{"role": "user"|"assistant", "content": str}] oldest-first."""
    system = _system(kb_context)
    msgs = history + [{"role": "user", "content": user_text}]
    if settings.llm_provider == "anthropic":
        return await _anthropic(system, msgs)
    return await _openai(system, msgs)
