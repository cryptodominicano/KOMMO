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
