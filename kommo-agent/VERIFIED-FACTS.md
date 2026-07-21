# Verified against the live Kommo account (2026-07-17)

Facts confirmed empirically that Kommo's docs do NOT publish.

| Fact | Value | Notes |
|---|---|---|
| **WhatsApp `origin`** | **`waba`** | Docs only ever show `telegram`. Filtering on `"whatsapp"` silently drops every message. |
| Account ID | 36745667 | subdomain `infoswecinvestmentscom` |
| amojo_id | 05115415-d76f-43ee-a541-f4cdcad8ba68 | |
| Token scopes | `crm`, `files`, `files_delete`, `list_external_messages`, `notifications`, `push_notifications`, `send_external_messages` | `send_external_messages` + `list_external_messages` = the two Chats API add-on scopes |
| Token expiry | 2030-01-01 | long-lived, no refresh needed |
| Currency | DOP (RD$) | country DO |
| Auth host | `https://{subdomain}.kommo.com/api/v4` works | JWT also advertises `api_domain: api-c.kommo.com` (untested) |

## Inbound WhatsApp message — real shape

A single WhatsApp text to the connected number auto-creates a contact, a lead, and a talk:

```
talk_id      = 100
chat_id      = 1b9b13e7-3a7f-43c1-a5db-20d65e28012f
contact_id   = 23932090        (name auto-filled from WhatsApp profile: "Isaias Perez")
entity_id    = 9375716         entity_type = lead
status       = in_work         is_in_work = true    is_read = false
origin       = waba
```

Message object from `GET /api/v4/talks/100/messages`:
```json
{
  "id": "5b215e14-ed1e-4b62-bd61-54d127596378",
  "type": "incoming",
  "message_type": "text",
  "origin": "waba",
  "text": "Hello. Test message",
  "author": {"id": "6b1996fe-...", "type": "external", "name": "Isaias Perez", "avatar_url": ""}
}
```

Contact phone lands in the `PHONE` custom field (e.g. `+16103575363`).

## Still unverified

- `message_type` value for WhatsApp **voice notes** (`voice` vs `audio`) — needs a real voice note.
- Whether the **incoming** webhook payload includes `attachment.link` for voice (docs only show a text sample). Fallback to `GET /talks/{id}/messages` is implemented either way.
- `message_type` for a **location** pin, and whether coordinates are exposed anywhere.
- **Chats API add-on limit reset period** (Trial 100 / Pro 500 — per day? month? lifetime?). Undocumented; ask Kommo support before purchase.


## Session 2026-07-20 additions

### Billing / Chats API
- **402 "Over chat API limit"** = account paid/trial period exhausted. **Chats API
  messages require the Pro plan or higher.** Trial includes only 100 outgoing
  Chats-API messages. Account is now on Pro + a 3,000-message Chats API package.
- A **successful outgoing send returns HTTP 202 Accepted** (not 200).

### Salesbot bot inventory (GET /api/v4/bots → `_embedded.items`)
| id | name | role |
|---|---|---|
| 55340 | welcome-bot | saludo infographic, first contact |
| 55348 | agua-foto | 5-step water process infographic ([[FOTO_AGUA]]) |
| 55306 | septico-fotos | 3 séptico promo photos ([[FOTOS_SEPTICO]]) |
| 55956 | banco-foto | bank details + cédula image (deposit) |
| 59058 | Payment-Audio | Wellington voice note before bank details ([[AUDIO_PAGO]]) |
| 55238 | NPS Bot | inactive |

All customer-facing bots must have an EMPTY Triggers panel (Kommo defaults new
bots to "Any new conversation"; left in place it double-fires).

### Model markers (stripped by the worker before send)
| Marker | Effect |
|---|---|
| `[[HANDOFF]]` | pause agent, hand off to a human, create SLA task |
| `[[FOTO_AGUA]]` | fire agua-foto (55348) |
| `[[FOTOS_SEPTICO]]` | fire septico-fotos (55306) |
| `[[DEPOSITO]]` | fire banco-foto (55956) + send bank text; legacy text phrase "le comparto los datos para el depósito" also still fires it |
| `[[AUDIO_PAGO]]` | fire Payment-Audio (59058) ~2s before the bank details; only on the agua study deposit |

### Human-like reply delay
- Randomized 4-9s before conversational replies; first greeting exempt. Config in
  client.toml [behavior] reply_delay_min_seconds / reply_delay_max_seconds.
- Runs inside the FastAPI BackgroundTask (webhook returns 200 first), so it never
  causes Kommo webhook retries.

### Salesbot voice notes (from Kommo docs, for future audio steps)
- Max 16MB. Convertible formats: WAV, MP3, OGG, M4A, AAC, FLAC, OPUS.
- "Convert to voice" required; step must have NO text and NO buttons or it sends as
  a downloadable file. iOS can still deliver .ogg as an attachment — test both OSes.


## Session 2026-07-21 additions

### Channel origins (allow-list) — all verified live
| Channel | origin string |
|---|---|
| WhatsApp | `waba` |
| Instagram | `instagram_business` |
| Facebook Messenger | `facebook` |

Config: `[kommo].origins`. Single-value equality would drop the other channels.

### Talks lookup (for reviews / lead->conversation mapping)
- `GET /api/v4/talks?limit=100` returns `_embedded.talks`, newest talk_id first,
  each with `talk_id`, `entity_id` (the lead), `entity_type`, `origin`, timestamps.
- `GET /api/v4/talks/{talk_id}/messages` returns the transcript (free of add-on
  quota). Message: `type` (incoming/outgoing), `message_type`, `text`,
  `author.type` (external=customer, bot=Isla, internal=human agent), `created_at`.
- The `updated_at` filter on `GET /leads` is unreliable for "today"; use the talks
  list + timestamps instead.

### Meta-side auto-reply gotcha
- A phantom "En breve le responderemos" on Instagram came from Meta's **Instant
  reply** automation (Business Suite -> Inbox -> Automations: Auto reply / FAQ /
  Away message), NOT from our agent or any Kommo bot. Disable per channel or it
  double-replies against the agent.

### New model markers / state
- `[[LINDEROS_LISTO]]` — worker emits it when a marked map arrives while awaiting
  linderos; prompt answers with the ETAPA 1 deposit (no handoff).
- New SQLite tables: `followup` (inactivity nudge), `awaiting_linderos`,
  `last_inbound` (debounce supersede).

### Behavior config knobs
- `reply_delay_min/max_seconds` (4/9): the debounce window + human pause.
- `followup_delay_minutes` (15) + `[messages].followup_nudge`.
- `ad_direct_entry_text` = "Hola! Quiero Agua en Mi Tierra." routes to the agua flow.


## Session 2026-07-21 (pm) additions

### Lead segmentation tags (on the CONTACT)
- Marker: `[[SECTOR:Provincia|Pueblo]]` (province first, town second, pipe-separated).
  Worker strips it and tags the lead's MAIN contact with `Provincia: X` + `Pueblo: Y`.
- Tags live on the CONTACT (person), not the lead: persists across deals, survives
  lead closure, and broadcasts/audiences target contacts. Build a per-area list by
  filtering Contacts by the tag (Lists -> Contacts -> Tags filter).
- Kommo tag PATCH REPLACES the whole tag set -> always read existing tags and merge
  (`tag_lead_contact` / `add_lead_tag` do this). Contact-tag endpoint:
  `PATCH /contacts/{id}` with `_embedded.tags`.
- `?with=tags` and `?with=contacts` on GET leads/contacts return the linked data.

### Follow-up close-detection
- Nudge stands down if the customer's last message is a PURE close (_looks_like_closing:
  bare gracias/ok/bien/hasta luego/👍). A "gracias" + question/intent word ("?", cuanto,
  precio, quiero, ubicación, módulo, ...) is NOT a close and still nudges.

### Kommo object model
- One inbound from a new number auto-creates Contact + Lead + Talk (linked). Repeat
  phone reuses the same Contact. Lead/contact tags are separate namespaces.
