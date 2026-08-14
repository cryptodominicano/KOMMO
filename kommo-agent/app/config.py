"""Configuration loaded from environment (master.env convention)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Client pack (clients/<id>/) ---
    client_id: str = "aguas-profundas"
    # Kommo general webhooks are unsigned; a path secret is the available defence.
    webhook_secret: str = ""

    # --- Kommo ---
    kommo_subdomain: str = "infoswecinvestmentscom"
    kommo_long_lived_token: str = ""
    # VERIFIED 2026-07-17 against live account: WhatsApp Business API origin is "waba".
    # (Kommo docs only ever show "telegram" — the WhatsApp value is undocumented.)
    kommo_whatsapp_origin: str = "waba"

    # --- LLM ---
    # provider: "openai" (current) or "anthropic". Prompt is model-agnostic;
    # switching is a one-line change.
    llm_provider: str = "openai"
    openai_model: str = "gpt-4.1"
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-5"
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.3

    # --- Transcription ---
    # provider: "openai" (default - uses existing OPENAI_API_KEY) or "groq"
    # Groq is faster/cheaper but needs a new credential; not worth it at this volume.
    transcribe_provider: str = "openai"
    groq_api_key: str = ""
    openai_api_key: str = ""
    whisper_model_groq: str = "whisper-large-v3"
    # whisper-1 = Whisper large-v2. gpt-4o-transcribe / gpt-4o-mini-transcribe are newer
    # and more accurate for accented Spanish + domain jargon. A/B test on a real voice note.
    whisper_model_openai: str = "gpt-4o-mini-transcribe"
    # Hallucination guards
    min_audio_bytes: int = 2000        # reject near-empty audio
    min_transcript_chars: int = 2

    # --- Qdrant ---
    qdrant_url: str = "http://172.20.0.10:6333"
    qdrant_collection: str = "aguas_profundas_kb"
    embed_model: str = "text-embedding-3-small"  # 1536-dim, matches existing collections
    rag_top_k: int = 8   # generous: KB is small, effectively returns everything relevant

    # --- Behavior ---
    human_agent_name: str = "un técnico"
    dedupe_ttl_seconds: int = 3600

    # --- Linderos (property-boundary capture) ---
    # Public HTTPS base the customer opens the drawing link on. Must match the
    # agent's own public host so /linderos and /api/linderos are reachable.
    public_base_url: str = "https://kommo-agent.goldcoastai.pro"
    resend_api_key: str = ""

    # Bank details sent in TEXT alongside the account image (client asked for
    # this to avoid read errors). Sourced from the SECRET store (master.env),
    # NOT the public repo and NOT the prompt - so the account number is never
    # in git history and the LLM never sees it (injection cannot extract it).
    # Contains bank/type/number/holder only. The cédula stays image-only.
    bank_details_text: str = ""

    def model_post_init(self, __context):
        # Force model upgrade regardless of env var
        if self.openai_model == "gpt-4o":
            object.__setattr__(self, "openai_model", "gpt-4.1")

    @property
    def kommo_base(self) -> str:
        return f"https://{self.kommo_subdomain}.kommo.com/api/v4"


settings = Settings()
