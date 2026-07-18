# KOMMO — Aguas Profundas RD WhatsApp AI Agent

Agentic WhatsApp AI agent for **Aguas Profundas RD** (underground water studies, well drilling, IMHOFF septic systems) built on **Kommo** as the WhatsApp transport + CRM layer.

Primary WABA: `+1 829-558-3119` (lead generation). The legacy number is being wound down separately to preserve its chat history.

## Why Kommo

Previous platforms (Botpress, Chatwoot, Respond.io) all failed the same test: the AI agent could only be authored in a UI, so prompts and flows could not live in version control, and the platform's own retrieval kept mis-firing.

Kommo **also** does not allow creating AI agents via API — see the analysis for proof. But it exposes two things the others did not:

- `add_message` — a real **inbound** message webhook
- `POST /api/v4/talks/{talk_id}/send_message` — a real **outbound** send endpoint

That combination lets us skip Kommo's built-in AI entirely and run **our own Claude agent**, with the prompt, the flows, and the handoff logic all in code, in this repo.

## The agent

Code lives in [`kommo-agent/`](kommo-agent/) — a **reusable template**. The engine
(`app/`) has no client-specific content; a client is a directory under
`clients/<id>/` holding its prompt, KB, verbatim messages and channel config.
Onboarding a client is a new directory, not a code change.

See [`kommo-agent/README.md`](kommo-agent/README.md) for the design decisions and
why each one exists.

## Architecture

```
WhatsApp message
  -> Kommo `add_message` webhook
  -> FastAPI service (ack < 2s, enqueue)
  -> branch on message_type:
       voice/audio -> download attachment -> Whisper -> transcript
       location    -> recognize pin -> ubicacion message -> human handoff
       text        -> passthrough
  -> Qdrant retrieval (aguas_profundas_kb, 1536-dim Cosine)
  -> Claude
  -> POST /api/v4/talks/{talk_id}/send_message
```

The **handoff pause** is enforced in code, not as a prompt instruction the model can ignore. This was a recurring failure on the previous builds.

## Documentation

- [`CONTEXT-LOG.md`](CONTEXT-LOG.md) — **running build log. Read this first.** Reverse-chronological, newest entry on top. Records what changed, what's verified, and what's blocked,each session. Never delete entries.
- [`docs/AGENT-CONTENT-ARCHIVE.md`](docs/AGENT-CONTENT-ARCHIVE.md) — **the crown jewels.** Every KB file, verbatim customer message, business rule and hard-won gotcha, preserved from the Botpress and Respond.io builds. Platform-independent. If a fifth platform ever happens, start here.
- [`docs/KOMMO-CAPABILITIES.md`](docs/KOMMO-CAPABILITIES.md) — full doc-grounded capabilities analysis: what is and is not API-creatable, auth, Chats API vs Chats API add-on, Salesbot JSON reference, webhooks, rate limits, the audio/location decision, and open questions to verify.

## Key constraints (verified against Kommo docs)

| Constraint | Value |
|---|---|
| API rate limit | 7 req/sec |
| Webhook response window | 2 seconds, no retry on success codes outside 100-299 |
| Webhook auto-disable | >100 invalid responses in 2 hours |
| Salesbot script | Not creatable or readable via API |
| Kommo AI Agent | UI-configured only; API adds knowledge sources only |
| `send_message` | **Text only** — file support not yet released |
| Chats API add-on | Plan-gated, request-limited |

## Open questions

Tracked in the analysis doc, section 9. The two that could still change the architecture:

1. The **reset period** for Chats API add-on request limits is undocumented. If Pro is 500/month, this design does not survive contact with a lead-gen number.
2. `send_message` is text-only, so **sending images** needs a verified path (Salesbot `show`, or WhatsApp templates with attachments).

## Status

Service built, not deployed. **Blocked at Meta**: ManyChat holds the credit line on the WABA, which makes them the billing partner, which prevents Kommo from being granted the `Messages` permission. See [`CONTEXT-LOG.md`](CONTEXT-LOG.md) for the diagnosis and fix sequence.

---

Maintained by Intelia Automatizaciones / Gold Coast AI Automations.


## Building the next client

Start here: [kommo-agent/docs/AGENT-BUILD-PLAYBOOK.md](kommo-agent/docs/AGENT-BUILD-PLAYBOOK.md) — the reusable master reference. Every rule in it was paid for by a real failure on the first build.
