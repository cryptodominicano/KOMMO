"""Voice-note transcription with hallucination guards.

Whisper's known failure: on silent/near-silent audio it emits confident filler
("Thank you.", "Gracias por ver el video."). An agent that ACTS on transcripts
must filter these, or it will fabricate customer intent.

v2.0 improvements (2026-08-14) based on MDPI 2024 peer-reviewed research:
- DR dialect + slang vocabulary in end-weighted prompt (Nacimiento-García et al.)
- VAD gating: length-vs-duration ratio check
- Repetition detection hallucination guard
- GPT normalization pass for DR contractions/slang before classification
"""
import re
import httpx
from .config import settings

# Domain vocabulary — end-weighted per OpenAI prompting cookbook.
# Research: put highest-value terms at END of prompt (last 224 tokens weighted most).
# Structure: dialect sentence first (style biasing), then slang, then domain vocab.
# Short DOMAIN-ONLY glossary. Research (OpenAI cookbook; whisper.cpp #2286;
# AGH ICASSP 2025 non-speech-hallucination study) is clear that a LONG prompt —
# especially a stylistic dialect sentence — is exactly what Whisper echoes back
# verbatim on silent/low-energy audio, causing false rejects. We keep ONLY the
# rare technical nouns the model can't guess, and no style sentence, so the
# echo surface is minimal. Kept well under Whisper's 224-token limit.
PROMPT_HINT = (
    "pozo, perforación, estudio de agua, radiestesia, geohidrológico, "
    "topográfico, aforo, caudal, séptico, IMHOFF, planta de tratamiento, "
    "módulo, linderos, Jarabacoa."
)

# Common Whisper hallucinations on silence (es/en).
_HALLUCINATIONS = {
    "gracias", "gracias.", "thank you", "thank you.", "thanks",
    "gracias por ver el video", "gracias por ver el video.",
    "subtítulos realizados por la comunidad de amara.org",
    "subtitulos realizados por la comunidad de amara.org",
    "you", "amara.org", "¡gracias!", "subscribe", "suscríbete",
    # Additional known gpt-4o-transcribe leakage patterns
    "aguas profundas.", "aguas profundas",  # prompt leakage on silence
    "vaina, tiguere,",  # prompt leakage fragments
}

# Prompt-dump detection: if 5+ domain hint words appear, Whisper echoed our prompt.
_PROMPT_DUMP_WORDS = [
    "vaina", "tiguere", "motoconcho", "diache", "colmado", "guagua",
    "jevi", "cuartos", "concho", "dique",
    "perforacion", "radiestesia", "geohidrologico", "topografico",
    "aforo", "caudal", "bauche", "jarabacoa", "linderos",
]


def _is_prompt_dump(text):
    """True only when the transcript is the PROMPT HINT echoed back (Whisper's
    silence failure), NOT when a real customer merely uses domain words.
    Signal: the transcript reproduces the hint's comma-separated glossary
    structure — many of the hint's own tokens, in a list, with little else."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    hint_tokens = {t.strip() for t in re.split(r"[,\.]", PROMPT_HINT.lower()) if t.strip()}
    # How many of the hint's exact glossary phrases appear in the transcript
    phrase_hits = sum(1 for t in hint_tokens if t and t in norm)
    # Echo shape: lots of commas (a list, not a sentence) AND the transcript is
    # mostly made of hint phrases.
    comma_count = norm.count(",")
    words = norm.split()
    # Reject only when it looks like the glossary itself: >=6 hint phrases echoed
    # AND it reads as a comma list (>=4 commas), i.e. not a normal spoken reply.
    if phrase_hits >= 6 and comma_count >= 4:
        return True
    # Or: the transcript is almost entirely hint phrases (a short echo fragment
    # like "radiestesia, geohidrológico, topográfico, aforo, caudal").
    if phrase_hits >= 4 and comma_count >= 3 and len(words) < 20:
        return True
    return False

# Repetition detection — hallucinated loops
_REPETITION_RE = re.compile(r'(.{10,}?)\1{2,}', re.DOTALL)


def sniff_ext(b: bytes) -> str:
    if b[:4] == b"OggS":
        return "ogg"
    if b[4:8] == b"ftyp":
        return "m4a"
    if b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return "mp3"
    if b[:4] == b"RIFF":
        return "wav"
    if b[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return "m4a"


class TranscriptionRejected(Exception):
    """Audio was unusable — ask the client to resend rather than guess."""


def _looks_hallucinated(text: str) -> bool:
    if _is_prompt_dump(text):
        return True
    norm = re.sub(r"\s+", " ", text.strip().lower())
    if norm in _HALLUCINATIONS:
        return True
    # Short filler with no domain signal
    if len(norm) < 12 and not re.search(r"\d", norm):
        if norm.strip(" .!¡?¿") in {w.strip(" .") for w in _HALLUCINATIONS}:
            return True
    # Repetition loop detection (hallucinated "Thank you. Thank you. Thank you.")
    if _REPETITION_RE.search(norm):
        return True
    # Length-vs-duration sanity: if text is suspiciously short for the audio
    # This is a soft signal — we log but don't always reject
    return False


def _suspicious_transcript(text: str, audio_bytes: int) -> bool:
    """True if the transcript looks suspicious given audio size.
    Very short transcripts from large audio files suggest hallucination."""
    words = len(text.split())
    # Rough estimate: ~180 words/min, audio at ~16KB/s M4A
    est_duration_s = audio_bytes / 16000
    est_words_expected = max(3, est_duration_s * 3)  # 3 words/sec conservative
    # If we got < 10% of expected words from a long clip, suspicious
    if est_duration_s > 10 and words < est_words_expected * 0.1:
        return True
    return False


async def _normalize_transcript(text: str) -> str:
    """Optional GPT normalization pass for DR contractions and slang.
    Expands contractions, normalizes slang to standard spelling,
    fixes common DR aspiration patterns before intent classification.
    Uses gpt-4o-mini for speed and cost. Skip if text is already clean."""
    # Only normalize if DR-specific patterns are detected
    dr_patterns = ["ta'", "lo'", "vamo'", "pa'", "to'", "po'", "na'"]
    if not any(p in text.lower() for p in dr_patterns):
        return text
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "max_tokens": 200,
                    "temperature": 0,
                    "messages": [{
                        "role": "system",
                        "content": (
                            "Normaliza este texto en español dominicano: "
                            "expande contracciones (ta' to = está todo bien, "
                            "lo' = los, vamo' = vamos, pa' = para), "
                            "mantén el significado original. "
                            "Devuelve SOLO el texto normalizado, sin explicaciones."
                        )
                    }, {
                        "role": "user",
                        "content": text
                    }],
                },
            )
            normalized = r.json()["choices"][0]["message"]["content"].strip()
            return normalized if normalized else text
    except Exception:
        return text  # fail-open: return original if normalization fails


async def download_audio(url: str) -> bytes:
    from .config import settings as _s
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        try:
            r = await c.get(url, headers={"Authorization": f"Bearer {_s.kommo_long_lived_token}"})
            r.raise_for_status()
            return r.content
        except httpx.HTTPStatusError:
            r = await c.get(url)
            r.raise_for_status()
            return r.content


async def transcribe(audio: bytes, filename: str | None = None,
                     normalize: bool = True) -> str:
    if len(audio) < settings.min_audio_bytes:
        raise TranscriptionRejected(f"audio too small ({len(audio)}b) — likely empty")

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
            files={"file": (filename, audio)},
            # verbose_json exposes segment no_speech_prob / avg_logprob /
            # compression_ratio — the strongest hosted-API signals for "this was
            # not real speech." Whisper-family models (whisper-1, whisper-large-v3)
            # support it; the gpt-4o-*-transcribe models do NOT and additionally
            # echo the prompt on Spanish silence, so we avoid them for this client.
            data={"model": model, "language": "es", "prompt": PROMPT_HINT,
                  "response_format": "verbose_json", "temperature": "0"},
        )
        r.raise_for_status()
        _resp = r.json()
        text = (_resp.get("text") or "").strip()

    if len(text) < settings.min_transcript_chars:
        raise TranscriptionRejected("empty transcript")
    # API confidence signals (verbose_json). Zero-hallucination policy: when the
    # model itself signals no-speech / very-low-confidence / looping, reject and
    # let the escalation ask the customer to repeat — never pass a guess downstream.
    _seg = _resp.get("segments") or []
    if _seg:
        _ns = [s.get("no_speech_prob", 0.0) for s in _seg]
        _lp = [s.get("avg_logprob", 0.0) for s in _seg]
        _cr = [s.get("compression_ratio", 0.0) for s in _seg]
        _max_ns = max(_ns) if _ns else 0.0
        _min_lp = min(_lp) if _lp else 0.0
        _max_cr = max(_cr) if _cr else 0.0
        # OpenAI-documented heuristics: no-speech when no_speech_prob high AND
        # avg_logprob < -1; compression_ratio > 2.4 = repetition/looping.
        if _max_ns > 0.6 and _min_lp < -1.0:
            raise TranscriptionRejected(
                f"no-speech signal (no_speech_prob={_max_ns:.2f}, "
                f"avg_logprob={_min_lp:.2f})")
        if _min_lp < -1.2:
            raise TranscriptionRejected(f"very low confidence (avg_logprob={_min_lp:.2f})")
        if _max_cr > 2.4:
            raise TranscriptionRejected(f"repetition/loop (compression_ratio={_max_cr:.2f})")
    if _looks_hallucinated(text):
        raise TranscriptionRejected(f"hallucination filter tripped: {text!r}")
    if _suspicious_transcript(text, len(audio)):
        import logging
        logging.getLogger("transcribe").warning(
            "suspicious transcript (short text from long audio): %r", text)
        # Don't reject — log and continue, as it may be a genuine short answer

    # GPT normalization pass for DR contractions (optional, fail-open)
    if normalize and text:
        text = await _normalize_transcript(text)

    return text
