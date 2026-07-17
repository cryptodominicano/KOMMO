"""Voice-note transcription with hallucination guards.

Whisper's known failure: on silent/near-silent audio it emits confident filler
("Thank you.", "Gracias por ver el video."). An agent that ACTS on transcripts
must filter these, or it will fabricate customer intent.
"""
import re
import httpx
from .config import settings

# Domain vocabulary — Whisper mangles these in Dominican Spanish without a hint.
PROMPT_HINT = (
    "Aguas Profundas: pozo, perforación, estudio de agua, radioestesia, "
    "geohidrológico, topográfico, aforo, caudal, bomba, séptico, IMHOFF, "
    "planta de tratamiento, módulo, baños, RD$, linderos, terreno."
)

# Common Whisper hallucinations on silence (es/en).
_HALLUCINATIONS = {
    "gracias", "gracias.", "thank you", "thank you.", "thanks", "gracias por ver el video",
    "gracias por ver el video.", "subtítulos realizados por la comunidad de amara.org",
    "subtitulos realizados por la comunidad de amara.org", "you", "amara.org", "¡gracias!",
    "subscribe", "suscríbete",
}


class TranscriptionRejected(Exception):
    """Audio was unusable — ask the client to resend rather than guess."""


def _looks_hallucinated(text: str) -> bool:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    if norm in _HALLUCINATIONS:
        return True
    # A single short filler token with no domain signal.
    if len(norm) < 12 and not re.search(r"\d", norm):
        return norm.strip(" .!¡?¿") in {w.strip(" .") for w in _HALLUCINATIONS}
    return False


async def download_audio(url: str) -> bytes:
    """Fetch a Kommo attachment.

    Kommo never documents whether amojo.kommo.com/attachments/... is public or
    token-gated, and their sample links are expired so it cannot be tested until
    a real voice note arrives. Send the bearer first (harmless if ignored), fall
    back to anonymous. Guessing wrong means every voice note silently fails.
    """
    from .config import settings as _s
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        try:
            r = await c.get(url, headers={"Authorization": f"Bearer {_s.kommo_long_lived_token}"})
            r.raise_for_status()
            return r.content
        except httpx.HTTPStatusError:
            r = await c.get(url)          # some CDNs reject unexpected auth headers
            r.raise_for_status()
            return r.content


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    if len(audio) < settings.min_audio_bytes:
        raise TranscriptionRejected(f"audio too small ({len(audio)}b) — likely empty")

    if settings.transcribe_provider == "groq":
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        key, model = settings.groq_api_key, settings.whisper_model_groq
    else:
        url = "https://api.openai.com/v1/audio/transcriptions"
        key, model = settings.openai_api_key, settings.whisper_model_openai

    if not key:
        raise TranscriptionRejected(f"no API key for provider {settings.transcribe_provider}")

    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio, "application/octet-stream")},
            data={"model": model, "language": "es", "prompt": PROMPT_HINT,
                  "response_format": "json", "temperature": "0"},
        )
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()

    if len(text) < settings.min_transcript_chars:
        raise TranscriptionRejected("empty transcript")
    if _looks_hallucinated(text):
        raise TranscriptionRejected(f"hallucination filter tripped: {text!r}")
    return text
