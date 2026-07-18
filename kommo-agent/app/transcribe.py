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


# OpenAI/Whisper picks the decoder from the FILENAME EXTENSION, not the bytes.
# LIVE BUG, verified 2026-07-18: Kommo serves WhatsApp voice notes re-encoded as
# M4A (magic bytes "....ftypM4A "), but the attachment URL still ends in .ogg.
# Sending those M4A bytes as "voice.ogg" got a hard 400 "Audio file might be
# corrupted or unsupported". The real note transcribed cleanly once labelled
# .m4a. So: sniff the container from the bytes and never trust the URL.
def sniff_ext(b: bytes) -> str:
    if b[:4] == b"OggS":
        return "ogg"
    if b[4:8] == b"ftyp":                       # ISO-BMFF: m4a / mp4 / 3gp
        return "m4a"
    if b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return "mp3"
    if b[:4] == b"RIFF":
        return "wav"
    if b[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return "m4a"                                # Kommo's default re-encode


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


async def transcribe(audio: bytes, filename: str | None = None) -> str:
    if len(audio) < settings.min_audio_bytes:
        raise TranscriptionRejected(f"audio too small ({len(audio)}b) — likely empty")

    # Name the file by its ACTUAL container, or Whisper 400s (see sniff_ext).
    if not filename:
        filename = f"voice.{sniff_ext(audio)}"

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
            # No explicit content-type: forcing octet-stream is part of what
            # produced the 400. Let the filename extension drive the decoder.
            files={"file": (filename, audio)},
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
