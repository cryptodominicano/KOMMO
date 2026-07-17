"""LLM call. The system prompt is the persona + flows; the KB arrives as
retrieved context. Provider-agnostic: the same prompt drives OpenAI or Claude."""
import httpx
from pathlib import Path
from .config import settings

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system.md"


def system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _system(kb_context: str) -> str:
    return (
        system_prompt()
        + "\n\n# FUENTES DE CONOCIMIENTO (usa SOLO esta información)\n"
        + kb_context
    )


async def _openai(system: str, msgs: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=90.0) as c:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "max_tokens": settings.llm_max_tokens,
                "temperature": settings.llm_temperature,
                "messages": [{"role": "system", "content": system}] + msgs,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def _anthropic(system: str, msgs: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=90.0) as c:
        r = await c.post(
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
        r.raise_for_status()
        return "".join(b["text"] for b in r.json()["content"]
                       if b["type"] == "text").strip()


async def generate(user_text: str, kb_context: str, history: list[dict]) -> str:
    """history: [{"role": "user"|"assistant", "content": str}] oldest-first."""
    system = _system(kb_context)
    msgs = history + [{"role": "user", "content": user_text}]
    if settings.llm_provider == "anthropic":
        return await _anthropic(system, msgs)
    return await _openai(system, msgs)
