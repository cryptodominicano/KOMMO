# kommo-agent

Agentic WhatsApp AI agent on **Kommo**. The agent runs in *our* code — Kommo is
the WhatsApp transport and CRM, nothing more.

Built as a **reusable template**. The engine (`app/`) contains no client-specific
content. Onboarding a client is a new directory under `clients/`, not a code change.

## Why this shape

Every platform evaluated (Botpress, Chatwoot, Respond.io, Kommo) is **UI-only for
AI agent authoring** — none let you create or configure an agent via API. Kommo won
because it is the only one exposing *both* halves of the loop:

- `add_message` — a real inbound webhook
- `POST /api/v4/talks/{talk_id}/send_message` — a real outbound send

So we skip Kommo's built-in AI entirely and own the agent in code, in git.

## Flow

```
WhatsApp message
  → Kommo `add_message` webhook  (hard 2s ack budget)
  → ack immediately, enqueue
  → branch on message_type:
       voice/audio → download attachment → Whisper → transcript
       location    → verbatim ubicación message → handoff → stop
       text        → passthrough
  → Qdrant retrieval (1536-dim Cosine)
  → LLM (OpenAI or Claude — one config line)
  → POST /talks/{id}/send_message
```

## Design decisions, each earned the hard way

| Decision | Why |
|---|---|
| **Handoff enforced in code** (`state.py`), never in the prompt | Botpress and Respond.io both leaked messages after handoff. A prompt rule is a suggestion; the model ignored it. The worker now returns before ever calling the LLM. |
| **Location handled deterministically** | `message_type == "location"` is a first-class Kommo enum. On Respond.io a GPS pin arrived as the opaque string `[Unsupported message]` and had to be pattern-matched. No model judgment here. |
| **Whisper hallucination filter** | Whisper invents confident filler on silence ("Gracias.", "Thank you.", the Amara.org artifact). An agent that *acts* on transcripts will fabricate customer intent. Filtered explicitly + minimum-audio gate. |
| **SQLite state, not a dict or JSON file** | uvicorn runs multiple worker processes; an in-process lock guards nothing and JSON writes race. WAL mode is process-safe and durable. Redis is the scale path. |
| **Pure-async Qdrant over httpx** | `qdrant-client` is synchronous; calling it from async code blocks the event loop and threatens the 2s ack. |
| **Webhook secret in the path** | Kommo's general webhooks carry **no signature** (only Chats API custom-channel hooks do). Without a secret, anyone who learns the URL can drive the agent. |
| **Generous `rag_top_k`** | These KBs are ~4k tokens. A high k effectively returns everything relevant, removing the retrieval-miss mode that forced a "search these keywords" hack on the previous platform. |
| **Webhook dedupe** | Kommo retries. Never answer twice. `INSERT`-then-catch is atomic; check-then-insert races. |
| **Client packs** | Prompt, KB, verbatim messages and channel config live in `clients/<id>/`. The engine never mentions a client. |

## Layout

```
app/                  engine — client-agnostic
  main.py             webhook: parse, auth, ack fast, enqueue
  worker.py           branch, transcribe, retrieve, generate, send
  state.py            handoff + dedupe (SQLite/WAL)
  rag.py              Qdrant retrieval (async httpx)
  agent.py            LLM (OpenAI | Anthropic)
  transcribe.py       Whisper + hallucination guards
  kommo.py            Kommo API v4 client
  client.py           client pack loader
clients/<id>/
  client.toml         messages, channel origin, behavior
  prompts/system.md   persona + flows
  kb/*.md             knowledge base
scripts/ingest_kb.py  KB → Qdrant
tests/                engine tests (no network, no keys)
```

## Onboard a new client

1. `cp -r clients/aguas-profundas clients/<new>`
2. Edit `client.toml` (subdomain, origin, verbatim messages), `prompts/system.md`, `kb/`.
3. `QDRANT_COLLECTION=<new>_kb python scripts/ingest_kb.py`
4. Set `CLIENT_ID=<new>` and deploy.

No engine code changes.

## Verified facts (live, 2026-07-17)

- **WhatsApp `origin` is `waba`** — not `whatsapp`. Kommo's docs only ever show
  `telegram`. The wrong value silently drops every message with no error.
- `send_message` is **text-only** today ("file uploads in an upcoming release"),
  so the agent must hand off rather than promise photos.
- `GET /talks/{id}/messages` does **not** consume Chats API add-on quota; sends do.
- Kommo rate limit: **7 req/sec**. Webhook ack budget: **2 seconds**, and >100
  invalid responses in 2 hours auto-disables the hook.

## Test

```bash
pytest tests/ -q          # no network, no keys
```

## Known limits / scale path

- `BackgroundTasks` runs in-process: a restart mid-task drops that reply. Move to
  Redis + a worker (arq/celery) before high volume.
- Single uvicorn worker keeps per-conversation ordering deterministic. Scale out
  with Redis-backed state + replicas.
- No client-side rate limiting against Kommo's 7 req/sec yet.
