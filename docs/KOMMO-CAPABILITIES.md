# Kommo — Capabilities Analysis for Agentic AI Build

**Prepared for:** Aguas Profundas RD (Intelia Automatizaciones / Gold Coast AI Automations)
**Date:** 2026-07-16
**Scope:** What Kommo can and cannot do programmatically, with the goal of building an agentic WhatsApp AI agent on WABA `+1 829-558-3119`.
**Method:** Grounded in official docs at `developers.kommo.com` only. Anything not stated in the docs is flagged as UNVERIFIED rather than guessed.

> Kommo publishes an LLM-friendly docs index at `https://developers.kommo.com/llms.txt` — every page is available as clean markdown by appending `.md` to the URL. Use this for all future research.

---

## 1. Executive summary — read this first

**The headline claim needs correcting.** Kommo does *not* let you create or configure AI agents via API. Verified from the docs:

- **Kommo AI Agent** is configured in the **UI only** (Settings → Kommo AI). The public AI API (APIv2) can only: add knowledge **sources** (URL / file / text), add **policies**, and **sync products** from CRM. There is **no endpoint** to create an agent, set its prompt, or define its behavior.
- **Salesbot** has **no create/update/delete endpoint**. The API can only **list, get, launch, and stop** bots. The bot's JSON script is **not even readable** via the API. Bot authoring is UI-only.

**However — the news is better than Respond.io, not worse.** Kommo exposes something Respond.io did not: a genuine **inbound message webhook** plus an **outbound send API**. That means you can bypass Kommo's built-in AI entirely and run **your own** agent (Claude on the VPS / N8N), owning 100% of the logic in code.

**Recommended architecture (see §5C):**

```
WhatsApp msg → Kommo `add_message` webhook → your VPS/N8N → Claude → 
POST /api/v4/talks/{talk_id}/send_message → reply lands in WhatsApp
```

This is fully agentic, version-controllable, and testable. It is the reason Kommo is the right call.

**The one serious blocker to design around:** `send_message` is **text-only today** — "Support for file uploads will be implemented in an upcoming release." The séptico photos cannot be sent through this endpoint. See §9 (Open questions) and §5D.

---

## 2. Verdict table — what is actually programmable

| Capability | API-creatable? | Notes |
|---|---|---|
| Kommo AI Agent behavior/prompt | ❌ No | UI only (Settings → Kommo AI) |
| Kommo AI knowledge sources | ✅ Yes | URL / file / text, + policies, + product sync |
| Salesbot creation / script edit | ❌ No | UI only. Script not readable via API |
| Salesbot launch / stop | ✅ Yes | `POST /api/v4/bots/{id}/run` and `/stop` |
| Salesbot → call external AI mid-chat | ✅ Yes | `widget_request` handler (requires custom widget, Advanced+) |
| Read inbound messages | ✅ Yes | `add_message` webhook |
| Send outbound message | ✅ Yes | `POST /api/v4/talks/{talk_id}/send_message` (**text only**) |
| Read chat history | ✅ Yes | `GET /api/v4/talks/{talk_id}/messages` |
| Send images/files programmatically | ⚠️ Unclear | Not via `send_message`. See §9 |
| Leads / contacts / pipelines / tags / fields | ✅ Yes | Full CRUD, API v4 |
| WhatsApp templates | ✅ Yes | Add + submit for moderation |

---

## 3. Authentication

### 3.1 Private integration + long-lived token (our path)

Steps (must be account **administrator**):
1. Settings → Integrations → **Create integration** → choose **Private**.
2. Fill: Integration name (3–255 chars), Description (5–65,000 chars), **Allow access** (scopes).
3. **Leave Redirect URL blank** — docs: *"If you are going to use a long-lived token, don't type anything in Redirect URL field."*
4. Save. Go to **Keys and scopes** tab → **Generate long-lived token** → pick expiry → copy.
5. The token is shown **once**. Save it immediately to `/app/data/master.env`.

**Lifetime:** selectable, **1 day to 5 years**.
**Rights:** runs with **administrator rights** (the creator's).
**No refresh needed:** *"Long-lived tokens do not have a refresh_token."*
**Security:** docs explicitly call it *"less safe"* — if leaked, the account is exposed. Treat like a root credential.
**Revoke:** Authorization tab → Revoke access.

Usage:
```http
Authorization: Bearer <LONG_LIVED_TOKEN>
```
Base URL: `https://{subdomain}.kommo.com/api/v4/...`

### 3.2 OAuth 2.0 (only if we ever go public/multi-tenant)
- Access token: **24 hours**. Refresh token: **3 months**, rotating.
- Authorization code expires in **20 minutes**.
- `POST /oauth2/access_token` to exchange.

### 3.3 Scopes
The docs **do not enumerate scope strings**. Only four groups are named: account data (per user rights), Notification Center, Files, Users. Two chat scopes are named in the add-on docs and are required:
- **`Sending to external chats`** — required to send
- **`External chat history`** — required to read history

Missing scope → `403 {"Invalid scope"}`.

> `X-Context-User-ID` header lets an admin token execute as another user.

---

## 4. Limits (memorize these)

| Limit | Value |
|---|---|
| **Rate limit** | **7 requests/second**. Exceed → `429`. Repeat → IP blocked, `403` |
| Entities returned per request | ≤ 250 |
| Entities added/updated per request | ≤ 250 (recommended ≤ 50) |
| Webhooks per account | 100 |
| Pipelines per account | 50 (100 stages each) |
| Lists per account | 10 |
| Sources per integration | 100 |
| Kommo AI sources | 100 paid / 10 trial (per functionality type) |
| Salesbot script JSON | **≤ 64 KB** |
| Webhook response window | **2 seconds**, success = HTTP 100–299 |
| Webhook auto-disable | >100 invalid responses in 2 hours |
| TLS | 1.2 recommended; HTTPS only; must hit `subdomain.kommo.com` |

**Chats API add-on request limits** (consumed by `send_message`):
- Trial: **100** · Pro/Enterprise: **500** · Technical: **10,000**
- Additional limits purchasable in Billing. Exceed → `402 "Over chat API limit"`.
- ⚠️ **Reset period is NOT documented.** Must verify (see §9).
- `GET /talks/{id}/messages` does **not** consume limits.

**Plan gating:**
- Webhooks via API: **Advanced, Pro, or Enterprise**
- Chats API add-on: **Trial, Pro, Enterprise, or Technical**
- Custom widgets (needed for `widget_request`): **Advanced+**

---

## 5. The three possible architectures

### 5A. Kommo native AI Agent — UI-configured
**What it is:** Kommo's built-in agent. Answers product/price/availability questions from the CRM product list, tracks Shopify orders, answers from uploaded policies, handles greetings.

**Programmable surface (APIv2):**
- Add source type URL — `POST` (public URLs only, no auth-walled pages, images not read)
- Add source type file — PDF/DOC/DOCX, **≤ 45 MB**, images not read
- Add source type text — **≤ 5,000 characters**
- Add policies (sources with special properties)
- Launch product import from CRM to AI

Auth: OAuth2 or long-lived token, `Authorization: Bearer`. Header `X-Language: {en|es|pt|ru}`.

Errors: `402 Disabled for account`, `402 Limit reached`, `403 Service unavailable`.

**Verdict:** ❌ Not agentic. The agent's actual behavior/prompt is UI-only. Useful only as a fallback or for simple FAQ deflection. **Not our path.**

---

### 5B. Salesbot + `widget_request` — hybrid
**Concept:** Build a Salesbot in the UI, but insert a **Widget step** whose `widget_request` handler calls *our* endpoint mid-conversation. Claude generates the reply; we post it back.

**Flow:**
1. Salesbot hits the Widget step.
2. Kommo POSTs to our URL:
```json
{
  "token": "JWT_TOKEN",
  "data": { "contact": "Contact name", "from": "widget" },
  "return_url": "https://subdomain.kommo.com/api/v4/salesbot/321/continue/123"
}
```
   JWT is signed with the integration secret and carries `account_id`, `subdomain`, `entity_type`, `entity_id`, `client_uid`.
3. **We must ack with HTTP 200 within 2 seconds.** (Bare ack — inference happens after.)
4. We call Claude, then POST to `return_url`:
```json
{
  "data": { "message": "..." },
  "execute_handlers": [
    { "handler": "show", "params": { "type": "text", "value": "Your text" } },
    { "handler": "goto", "params": { "type": "question", "step": 5 } }
  ]
}
```
5. Anything in `data` is later readable as `{{json.KEY}}`.

**Hard constraints:**
- `widget_request` is **Widget-step only** → requires an uploaded custom widget → **Advanced plan minimum**.
- `/continue/` endpoint is **administrator-only**.
- **Max 10 handlers** per `execute_handlers`.
- **`show` value ≤ 80 characters** via `/continue/` — a hard validation limit. LLM output must be chunked: ~10 × 80 chars ≈ **800 chars per resume**. This is brutal for our long séptico intro.
- **The bot blocks** until the continue call arrives: *"The current bot will not continue its operation until it receives the request."*
- **One bot per entity** — a second bot on the same entity silently blocks the continue.
- No documented TTL on the continue record; `404 "There is no continue record with id"` if stale.

**Verdict:** ⚠️ Workable but constrained. The 80-char cap and 2s ack make it awkward for long AI replies. **Keep as fallback.**

---

### 5C. ⭐ RECOMMENDED — Webhook + Chats API add-on (fully agentic)

**This is the build.** We own the entire AI loop; Kommo is just the WhatsApp transport + CRM.

```
1. Customer sends WhatsApp msg to 829-558-3119
2. Kommo fires `add_message` webhook → our VPS/N8N endpoint
3. We ack 200 within 2 seconds, enqueue
4. Our service loads context (Qdrant KB + chat history) → calls Claude
5. POST /api/v4/talks/{talk_id}/send_message → reply lands in WhatsApp
6. Optionally update lead/tags/pipeline stage via CRM API
```

**Step 2 — the inbound webhook.** Register:
```http
POST /api/v4/webhooks
{
  "destination": "https://<your-host>/webhook/kommo-inbound",
  "settings": ["add_message"],
  "sort": 10
}
```
Administrator only. Format is `x-www-form-urlencoded`.

`add_message` payload (note: **unwrapped**, array at top level):
```json
{"add":[{
  "id":"9402b05b-...","chat_id":"dfa7f0e5-...","talk_id":"172",
  "contact_id":"46855094","text":"Hi!","created_at":"1782389132",
  "message_type":"text","element_type":"2","entity_type":"lead",
  "element_id":"50296276","entity_id":"50296276",
  "type":"incoming",
  "author":{"id":"9729f051-...","type":"external"},
  "origin":"telegram"
}]}
```
Filter on `type == "incoming"`. `origin` identifies the channel (doc example shows `telegram`; **WhatsApp's exact `origin` value is not documented** — verify empirically, §9).

**Webhook delivery rules (critical):**
- Respond **within 2 seconds** with 2xx or it counts as failed.
- Retries: attempt 2 at +5 min, 3 at +15 min (codes 0–99, ≥300); attempt 4 at +15 min, 5 at +1 hr (499, 500–599).
- **Auto-disabled** after >100 invalid responses in 2 hours.
- → **Pattern: verify, enqueue, return 200 immediately. Never run Claude inline.**

**Step 5 — sending the reply.**
```http
POST /api/v4/talks/{talk_id}/send_message
Authorization: Bearer <token>
{ "text": "Message text" }
```
Returns `202 {"id":"ec1cadc7-..."}`.

Errors: `400` validation · `402 "Over chat API limit"` / `"Endpoint not available for current account tariff"` · `403 "Invalid scope"` · `404 "Requested entity not found"` · `422 "Talk is closed"`.

> ⚠️ **"The endpoint currently supports sending text messages only. Support for file uploads will be implemented in an upcoming release."**

**Reading history:**
```http
GET /api/v4/talks/{talk_id}/messages?limit=250&filter[created_at][from]=...
```
Returns `_embedded.messages[]` with `type` (incoming/outgoing), `message_type` (text|contact|file|video|picture|voice|audio|sticker|location), `author{type: internal|external|bot}`, `text`, `delivery_status`, `attachment{type,link,file_name}`. **Does not consume add-on limits.**

**Finding the talk_id:**
```http
GET /api/v4/talks?filter[contact_id][]=...&filter[only_in_work]=1
```
Returns `talk_id`, `chat_id`, `contact_id`, `entity_id`, `status` (`in_work|closed|nps_scheduled|nps_in_progress|with_error`), `origin`. `204` when empty.

**Why this wins:** the AI lives in our stack (Claude + Qdrant + N8N on the VPS). Prompts are in git. No 80-char caps, no UI-only config, no vendor lock on the agent logic. Kommo handles WhatsApp delivery, inbox, and CRM.

---

### 5D. The image problem (again)

`send_message` is **text-only**. Options for the séptico photos, in order of confidence:

1. **Salesbot `show` handler** — send images via a Salesbot triggered from our webhook (`POST /api/v4/bots/{id}/run`). ⚠️ The documented `show` types are `text`, `buttons`, `buttons_url` — **media is not documented**. Must verify.
2. **WhatsApp templates** — the Templates API supports attached files (delete-template docs reference "the file attached to the template"). Templates can be added and submitted for moderation via API. Likely the most reliable media path.
3. **Manual/UI send** by a human agent after handoff.
4. **Wait** for file support on `send_message`.

**This must be resolved before committing to the design.** See §9.

---

## 5E. Audio (voice notes) and GPS location — DECISION: webhook wins

Two hard requirements from the Aguas Profundas build: clients send **WhatsApp voice notes**, and clients share **GPS location pins** which the agent must recognize before human handoff. These were re-checked against the docs because they could have forced the internal-AI path. **They do not. They argue against it.**

### Does Kommo's internal AI handle audio or location?

**No evidence it handles either.** The AI Agent docs enumerate its capabilities exhaustively:
- Advising on product details and availability (from the CRM product list)
- Tracking/updating orders (Shopify) — *"can't cancel, change, or perform other direct interactions"*
- Providing business information from uploaded policies
- Answering low-context queries ("hello", "goodbye")

**Audio transcription is never mentioned. Location handling is never mentioned.** Knowledge sources explicitly state *"Images aren't read."* It is a text FAQ agent. Choosing it would mean betting our two hardest requirements on undocumented behavior.

### How the webhook path handles audio

`message_type` is a documented enum including **`voice`** and **`audio`** (also `text|contact|file|video|picture|sticker|location`).

Kommo carries media as an `attachment` object. Proof from the **outgoing** message webhook sample:
```json
"message_type": "picture",
"attachment": {
  "type": "picture",
  "link": "https://amojo.kommo.com/attachments/db6a2cea-.../kommo.gif",
  "file_name": "kommo.gif"
}
```
And `GET /api/v4/talks/{talk_id}/messages` returns `attachment{type, link, file_name}` for **any** message, and **does not consume add-on limits**.

> ⚠️ **Documented gap:** the *incoming* `add_message` webhook sample only shows a `text` message and includes **no** `attachment` field. Whether `attachment` is present on incoming voice messages is **not documented**. Design defensively: if the webhook lacks the link, call `GET /api/v4/talks/{talk_id}/messages?limit=1` to retrieve it. That call is free of add-on quota, so this costs nothing but a round trip.

**Audio pipeline:**
```
add_message webhook (message_type = voice|audio)
  → ack 200 in <2s, enqueue
  → GET /api/v4/talks/{talk_id}/messages → attachment.link
  → download .ogg from amojo.kommo.com
  → Whisper (self-hosted or Groq) → Spanish transcript
  → Claude (+ Qdrant KB) → reply
  → POST /api/v4/talks/{talk_id}/send_message  (text reply — fine, we only RECEIVE audio)
```
Note the text-only send limitation (§5D) is **irrelevant here** — we receive audio and reply in text.

**Best practices (from research, and these matter):**
1. **Filter Whisper hallucinations.** Whisper has a well-known failure: on silent or near-silent audio it emits confident filler like *"Thank you."* An agent that **acts** on transcripts will produce false positives. Guard with a minimum audio duration and a no-speech/confidence threshold; discard below it and ask the client to resend.
2. **Confirm before acting.** Echo the interpretation back before any consequential step: *"Entendí que necesita un estudio de agua en Santo Domingo Este, ¿es correcto?"* Never trigger the RD$5,000 deposit message or a handoff off an unconfirmed transcript.
3. **Acknowledge while processing.** Transcription adds latency; the 2s webhook window already forces async, so send a brief "Un momento 🙏" if processing runs long.
4. **Never act on audio alone for money.** Payment/deposit steps stay human-confirmed, consistent with the existing banking boundary.
5. Whisper handles Spanish well, but **Dominican dialect + well-drilling jargon** should be spot-checked. Consider a domain prompt/vocabulary hint ("pozo", "IMHOFF", "radioestesia", "aforo").

### How the webhook path handles GPS location

`message_type: "location"` is a **documented typed value**. This is a materially better position than Respond.io, where a location pin arrived as the opaque string `[Unsupported message]` and we had to pattern-match a magic string. Here it's a first-class enum:

```
if message_type == "location":
    → send the verbatim "Recibimos su ubicación" message
    → assign to human / set tag / move pipeline stage
    → AI goes silent (PAUSA TOTAL rule)
```

That fully satisfies the requirement: **recognize the location before human handoff.**

> ⚠️ **Documented gap:** **latitude/longitude are not documented anywhere.** The `attachment` object is only `{type, link, file_name}` — no coordinate fields. So we can reliably *detect* that a pin was shared, but extracting the raw coordinates programmatically is **unverified**. This is acceptable: the requirement is recognition + handoff, and the human sees the pin natively in the Kommo inbox. If we later want coordinates in a custom field, verify empirically (§9).

### Verdict

| Requirement | Internal AI Agent | Webhook + own Claude |
|---|---|---|
| Voice note transcription | ❌ Not documented | ✅ Full control (Whisper, our tuning) |
| Recognize GPS location | ❌ Not documented | ✅ Typed `message_type: "location"` |
| Dominican Spanish tuning | ❌ No control | ✅ Our prompt, our model |
| Deterministic handoff | ❌ No control | ✅ Our code |
| Prompts in git | ❌ UI only | ✅ Yes |

**We do NOT have to use the internal AI, and we should not.** The audio and location requirements are precisely the ones the internal AI has no documented answer for, and precisely the ones the webhook path handles natively.

---

## 6. Chats API (custom channel) — the *other* Chats API

Do not confuse these two. Different hosts, different auth.

| | Chats API (custom channel) | Chats API **add-on** |
|---|---|---|
| Host | `https://amojo.kommo.com` | `https://{subdomain}.kommo.com` |
| Auth | `Date` + `Content-MD5` + `X-Signature` (HMAC-SHA1) | OAuth2 / long-lived token |
| Purpose | You **own** the channel | Send into channels **others** own (e.g. WhatsApp Business) |

Since our WhatsApp is connected through Kommo's own WhatsApp integration, **we use the add-on (§5C), not this.** Documented here for completeness.

**Channel registration is NOT an API call** — you email Kommo support with service name, webhook URL (`https://domain.com/location/:scope_id`), account ID, 14×14 SVG icon, integration ID/code. Turnaround 1–3 business days. You receive channel `id`, `code`, `secret_key`, bot ids.

**Signing scheme** (every request to `amojo.kommo.com`):
```
X-Signature = lowercase HMAC-SHA1(secret, 
  UPPERCASE(METHOD) \n
  Content-MD5       \n
  Content-Type      \n
  Date              \n
  path
)
```
```python
check_sum = hashlib.md5(request_body.encode('utf-8')).hexdigest()
str_to_sign = "\n".join([method.upper(), check_sum, content_type, date, path])
signature = hmac.new(secret.encode(), str_to_sign.encode(), hashlib.sha1).hexdigest()
```
- `Date`: RFC2822. Signature valid **15 minutes**.
- `Content-MD5`: lowercase md5 of the **raw body bytes** (compute even for GET — md5 of empty string).
- `path` excludes protocol/domain **and GET params**.
- The exact same body bytes must be used for MD5, signature, and transmission.

Key endpoints: `POST /v2/origin/custom/{channel_id}/connect` (must reconnect after every integration install — the channel auto-disables when the integration is disabled), `POST /v2/origin/custom/{scope_id}/chats`, `POST /v2/origin/custom/{scope_id}` (send/import).

---

## 7. Salesbot JSON reference (for the fallback path)

Top level is an **array of step objects** (0-indexed). Each has `question`, `answer`, or `finish`:
- `question` — actions when a message is sent to the user
- `answer` — actions when the user responds
- `finish` — actions when the bot completes
- `error` — fires when a message can't be delivered (e.g. client blocked the channel)

**Handlers:** `show`, `buttons`, `action`, `meta`, `condition`, `validations`, `preset`, `goto`, `wait_answer`, `find`, `filter`, `send_internal`, `stop`. Documented in body but absent from the handler table: `send_external_message`, `widget_request`.

- **`show`** — `type`: `text` | `buttons` | `buttons_url`. Messenger truncates values over 80 chars.
- **`goto`** — `{type: "question"|"answer"|"finish", step: int}` (steps start at 0).
- **`condition`** — `{term1, term2, operation, result}`; ops `=`, `!=`, `in`, `not_in`, `in_range`.
- **`validations`** — types `simple`, `email`, `phone`, `regex`, `range_numbers`; result in `{{last_validation_result}}`.
- **`action`** names: `unsorted`, `change_status`, `set_tag`, `unset_tag`, `set_custom_fields`, `subscribe`, `unsubscribe`, `add_lead_contact`, `set_budget`, `add_linked_company`, `add_note`, `link`, `change_responsible_user`, `link_to_unsorted`.
- **`stop`** — `action`: `talk-close` | `salesbot-start` (+ `bot: <id>`).

Entity type ints: **1 = contact, 2 = lead, 3 = company, 11 = list element.**

**Placeholders:** `{{contact.name}}`, `{{lead.id}}`, `{{message_text}}`, `{{message_text.email}}`, `{{message_text.phone}}`, `{{lead.cf.ID}}`, `{{contact.cf.ID}}`, `{{origin}}`, `{{lead.price}}`, `{{current_date}}`, `{{rand}}`, `{{short_rand_num}}`, `{{regexp./(...)/}}`, `{{lead|contact|company.responsible.id|name|email}}`.

**Endpoints:**
| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v4/bots` | List (max 250; `filter[type_functionality][]`) |
| `GET` | `/api/v4/bots/{id}` | Get one (204 if not found) |
| `POST` | `/api/v4/bots/{id}/run` | Launch — `{"entity_id":int,"entity_type":"leads"\|"contacts"}` → 202 |
| `POST` | `/api/v4/bots/{id}/stop` | Stop — `entity_type` **`leads` only** → 202 |
| `POST` | `/api/v4/salesbot/{bot_id}/continue/{continue_id}` | Resume widget_request. **Admin only** |

GET returns **metadata only** (`id`, `name`, `is_visual_editor`, `type_functionality`, `settings.active`) — **never the script**.

> **Doc inconsistency:** salesbot-dp.md documents `condition` (singular) but the SDK/Private-Chatbot examples emit `conditions` (plural) plus an undocumented `exits` handler. Verify against a live account.

---

## 8. Core CRM API (v4)

Base: `https://{subdomain}.kommo.com/api/v4/`

**Leads:** `GET /leads`, `GET /leads/{id}`, `POST /leads`, `PATCH /leads`, `PATCH /leads/{id}`, `POST /leads/complex` (lead + contact + company with duplicate control)
**Contacts:** `GET /contacts`, `GET /contacts/{id}`, `POST /contacts`, `PATCH /contacts`
**Companies:** `GET|POST|PATCH /companies`
**Pipelines:** `GET|POST|PATCH|DELETE /leads/pipelines`, stages under `/leads/pipelines/{id}/statuses`
**Custom fields:** `/{entity}/custom_fields` — full CRUD, plus field groups
**Tags:** `/{entity}/tags` — list, create, assign
**Notes:** `/{entity}/notes` — CRUD, pin/unpin
**Tasks:** `/tasks` — CRUD
**Events:** `/events` — list, by ID, types, filtration
**Users & roles:** `/users`, `/roles` — incl. activate/deactivate
**Links:** `/{entity}/link`, `/unlink`
**Templates:** `/templates` — CRUD + `submit a WhatsApp template for moderation` + edit status
**Files:** separate file-service host; session-based chunked upload; attach/detach to entities
**Webhooks:** `GET|POST|DELETE /webhooks`
**Account:** `GET /account?with=amojo_id,amojo_rights`
**Conversations:** `GET /talks`, `GET /talks/{id}`, `POST /talks/{id}/close`

---

## 9. ⚠️ Open questions — verify before building

1. **Can we send images programmatically at all?** `send_message` is text-only. Test whether a Salesbot `show` can carry media, or whether WhatsApp templates with attachments are the answer. **This is the #1 blocker** — it's the exact issue that bit us on Respond.io.
2. **What is the `origin` value for WhatsApp** in the `add_message` payload? Docs only show `telegram`. Verify empirically.
3. **Chats API add-on limit reset period.** Trial 100 / Pro 500 — per what? Day? Month? Not documented. If it's 500/month on Pro, that is *far* too low for a lead-gen number and changes the whole plan. **Verify this before committing.**
4. **Which plan is the account on?** Webhooks need Advanced/Pro/Enterprise; the add-on needs Trial/Pro/Enterprise/Technical. Confirm both are satisfied.
5. **`add_outgoing_message`** appears only in the changelog, not the events table. Verify empirically if needed.
6. Does the add-on cover **Kommo-native** WhatsApp, or only "external chat channels created by other integrations"? Docs name WhatsApp Business as the example and state no exclusion, but confirm with our actual channel.
7. **Does the incoming `add_message` webhook include `attachment` for voice notes?** Docs only show a text sample. If not, fall back to `GET /talks/{talk_id}/messages` (free of quota). Verify with a real voice note.
8. **Are GPS coordinates exposed anywhere** for `message_type: "location"`? Not documented. Detection works regardless; coordinates are a nice-to-have.
9. What is the exact `message_type` value WhatsApp voice notes produce — `voice` or `audio`? Both are valid enum values. Verify empirically.

---

## 10. Recommended next steps

1. Create the private integration; generate a **5-year long-lived token**; store as `KOMMO_LONG_LIVED_TOKEN` in `/app/data/master.env` (per env-naming-convention). Also store `KOMMO_SUBDOMAIN`, `KOMMO_ACCOUNT_ID`.
2. Grant scopes: **Sending to external chats** + **External chat history**.
3. Confirm the plan supports webhooks + the add-on.
4. Send a WhatsApp message to 3119, then `GET /api/v4/talks` to confirm the talk appears and capture the real `origin` value.
5. Register the `add_message` webhook to an N8N endpoint. Verify the payload shape live.
6. Resolve the image question (§9.1) **before** porting the séptico flow.
7. Build the loop: N8N webhook (ack fast) → Qdrant KB lookup → Claude → `send_message`.
8. Port the Aguas Profundas persona/flows from `aguas-profundas` repo knowledge files into the new agent prompt.

---

## 11. Sources

All from `developers.kommo.com` (append `.md` for markdown):
`/llms.txt` · `/reference/ai-features` · `/reference/ai-api-methods` · `/reference/salesbot` · `/docs/salesbot-dp` · `/docs/salesbot-sdk` · `/docs/private-chatbot-integration` · `/reference/salesbots-list` · `/reference/get-salesbot-by-id` · `/reference/launch-a-salesbot` · `/reference/stop-salesbot` · `/reference/salesbot-widget-block-execution-confirmation` · `/reference/send-message-guide` · `/reference/chats-api-authorization-and-headers` · `/reference/chat-api-accountid` · `/reference/register-channel` · `/reference/connect-channel` · `/reference/create-chat` · `/reference/send-import-messages` · `/reference/receiving-chat-webhooks` · `/reference/chats-api-add-on` · `/reference/send-message-to-conversation` · `/reference/get-conversation-messages` · `/reference/get-talks` · `/recipes/calculate-headers-for-chats-api-requests` · `/docs/oauth-20` · `/docs/long-lived-token` · `/docs/private-integration` · `/docs/permissions` · `/docs/limitations` · `/docs/webhooks-general` · `/reference/webhook-events` · `/reference/add-webhooks` · `/reference/account-parameters` · `/changelog/conversations-list-and-outgoing-message-webhook`
