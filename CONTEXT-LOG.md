# Aguas Profundas RD — Kommo Build Context Log

This file is the persistent memory layer for the Aguas Profundas WhatsApp AI agent build. It is read at the start of every session alongside the capabilities analysis. Each session's findings are **prepended so the most recent entry is always first**. Never delete old entries — the dead ends are the most valuable part, because they stop us re-walking them.

Format for each entry: `## Session: Month DD, YYYY — HH:MM UTC`, followed by what changed, what was verified, and what is still blocked.

---

## Session: July 17, 2026 — 07:30 UTC

### ⚠️ CORRECTION: images ARE sendable. My "no workaround" call was wrong.

The 04:30 and 06:00 entries state that `send_message` is text-only and therefore
the agent cannot send photos, and the prompt was written to apologise and hand
off. **That conclusion was wrong**, and it was wrong because I accepted a
limitation instead of hunting for the seam. Isaias pushed back and asked for a
deep search. The search found it.

**The seam:** `send_message` really is text-only. But a **Salesbot Message step
can attach images** — Kommo's own docs: *"You can also attach files to your
messages... Supported file types include: Documents, **Images**, Videos, Audio
files."* And `POST /api/v4/bots/{id}/run` launches a Salesbot for an entity.

So the payload is authored once in the UI, but **the trigger is fully
programmatic**. The agent decides *when*; Salesbot carries *what*. The
`add_message` webhook already hands us `entity_id`, so no extra lookup.

Implemented:
- `kommo.py` → `run_bot()`
- `worker.py` → sentinel detected → strip → `POST /bots/{id}/run`
- `client.toml` → `[salesbot.triggers]` maps sentinel → bot id
- prompt → emits `[[FOTOS_SEPTICO]]` instead of apologising

**Still to do:** build the `septico-fotos` Salesbot in the UI with the 3 photos,
then set its id in `[salesbot.triggers]` (currently `0`, which logs a warning and
no-ops rather than failing loudly).

### Two more leads worth testing

1. **The `[square bracket]` syntax.** Third-party Kommo docs claim a URL wrapped
   in square brackets inside a Salesbot text box is rendered **as an image, not a
   link**. If Kommo's message pipeline parses that universally rather than only
   inside Salesbot, then `send_message` with `[https://cdn.jsdelivr.net/...jpg]`
   would render as an image and the whole problem disappears. **One request to
   test** once the channel is live. This is why the jsDelivr asset URLs matter.
2. **Probable quota bonus.** Salesbot sends do not go through
   `/talks/{id}/send_message`, so they likely do **not** consume Chats API add-on
   quota (Trial 100 / Pro 500). Unverified, but it would soften the limit risk.

### Audio: re-verified, design confirmed

Scanned the **entire** Kommo API index for `transcri|voice|audio|speech|whisper`
— **zero hits**. Kommo has no transcription of any kind. Whisper is mandatory,
not a preference. The 5E design stands.

Fixed a latent bug while there: `download_audio()` fetched attachments with no
auth. Kommo never documents whether `amojo.kommo.com/attachments/...` is public
or token-gated, and their sample links are expired so it cannot be tested until a
real voice note arrives. Now sends the bearer first and falls back to anonymous.
Guessing wrong would have meant **every voice note silently failing**.

### Assets: off third-party hosts, permanently

The 5 marketing images (welcome, water process, 3× séptico) are now committed to
this repo and served via jsDelivr, verified returning `200 image/jpeg` — the
correct content-type, which GitHub raw does not reliably give and which WhatsApp
requires. Third host these images have lived on: Botpress CDN (died with the
platform) → ImgBB (free host) → this repo. They can no longer vanish.

### Lesson for the template

Twice now the honest-sounding answer ("the platform can't do X") was wrong, and
both times the real answer was a documented feature one layer sideways. **Check
whether an adjacent primitive can do it and whether that primitive is
API-triggerable**, before telling a client no.

---

## Session: July 17, 2026 — 06:00 UTC

### Diagnosis confirmed by Kommo support. Migration staged, one step from done.

Kommo support (Elena Padma) reviewed the account and concluded independently: **"looks like the number is still connected to previous BSP."** That is ManyChat, and it corroborates the credit-line diagnosis from the 04:30 entry exactly. Her follow-up sealed it: *"if it migrated correctly, it shouldn't be like that. You should be able to add your own valid payment method to your WABA Account."*

**The key realization: the first "migration" never happened.** Connecting the number in Kommo produced a *partner assignment* on the existing WABA, not a *BSP transfer*. That is why inbound worked (Meta forwards to assigned partners) while outbound was refused (BSP ownership and billing stayed with ManyChat). Receiving messages successfully was misread as proof of migration — it is not. **Inbound working proves nothing about BSP ownership.**

### ⚠️ Correction to the 04:30 entry

The 04:30 fix sequence said to attach a payment method to WABA `1064953052052555`. **That is wrong and would have been wasted effort.** Migration moves the number into a **brand-new WABA**; the old one is abandoned. The payment method belongs on the **new** WABA.

Consequence worth noting: **no ManyChat ticket is needed after all.** Meta performs the automatic migration and disconnects the previous provider itself. The ManyChat credit line is simply left behind on the dead WABA. The earlier plan to fight for its removal was unnecessary.

### Current state — staged, awaiting OTP

| Item | Status |
|---|---|
| **New WABA** | **"Aguas Profundas Kommo"** — ID `1472215754667167`, owned by Aguas Profundas MC ✅ created |
| 3119 in new WABA | ✅ present, status **Unverified** (staged, awaiting OTP) |
| Two-step verification (criterion 4) | ✅ disabled |
| Meta Business verified (criterion 1) | ✅ |
| WABA Approved status (criterion 2) | ✅ |
| Valid payment method on **new** WABA (criterion 3) | ⬜ to do |
| OTP capability | ✅ resolved — new SIM in a new phone |

The migration flow was walked to the phone-verification step and stopped there only because the handset was at another location. Nothing failed.

### Tomorrow's checklist

1. Attach a valid payment method to the **new** WABA `1472215754667167` (do this first — criterion 3, prevents an error at the finish line).
2. With the 3119 phone in hand: Kommo → Settings → Marketplace integrations → WhatsApp Business → Settings → Connect new account → Connect a new number → **select the new WABA** → Add a new number → 3119 → Next → enter OTP.
3. **Watch for the yellow warning banner.** Support flagged this specifically: it is the signal Meta is performing an automatic *migration* rather than a fresh registration. If it does not appear, stop and escalate — that means it is going down the wrong path again.
4. Verify status flips **Unverified → Connected**.
5. Test a send (manual from the inbox, or via `POST /api/v4/talks/{talk_id}/send_message`).
6. Re-verify `origin` is still `waba` on the new WABA, and capture the new `talk_id`. The old talk (`100`) belongs to the dead WABA and will be orphaned.
7. **Do not delete the old WABA** until sending is confirmed. Then contact Kommo support — they asked to assist with it.

### Notes

- Three WABAs now exist. `1472215754667167` "Aguas Profundas **Kommo**" is the live target; `1064953052052555` "Aguas Profundas" is the ManyChat-billed one being abandoned; `1343110684623231` "Aguas Profundas" is older still and likely holds 566-7542. The distinct "Kommo" suffix on the new one is deliberate — keep it.
- Automatic migration carries over display name, quality rating, messaging limits, OBA status, and approved high-quality templates. Manual migration does not (Kommo re-submits templates for Meta review). Automatic is materially better — protect it.
- **Kommo trial clock is live.** Their requirement is a paid account *or within 14 days of an active trial*. Trial started 2026-07-17.
- `kommo-agent` needs no config change from the WABA swap — it keys off subdomain, `talk_id` and `origin`, none of which are WABA-scoped.

---

## Session: July 17, 2026 — 04:30 UTC

### Current status: BLOCKED at Meta, not at Kommo, not at our code

Outbound WhatsApp is dead in the water for a reason that has nothing to do with the platform choice or the build. Root cause found and confirmed from Meta's own UI. Everything else on the critical path is verified working.

### BLOCKER — ManyChat holds the credit line on the WABA

WABA `1064953052052555` ("Aguas Profundas", owned by Aguas Profundas MC) has its **Payment method set to a Credit line from ManyChat Inc**. Meta's permission dialog states, three separate times, that **"This permission can only be assigned to the partner who's responsible for billing."**

Consequence: because ManyChat holds the credit line, ManyChat is the billing partner. Kommo therefore **cannot be granted the `Messages` permission** ("Send and respond to messages as the WhatsApp account"). Meta blocks it at the platform level. Kommo shows as an assigned partner but with **Partial access** and every meaningful toggle greyed out.

This explains every symptom exactly:

| Symptom | Explanation |
|---|---|
| Inbound messages arrive fine | Meta forwards incoming to subscribers regardless of send rights |
| Every outbound fails | Kommo has no authority to send as this WhatsApp account |
| Manual sends from the Kommo UI fail identically | It's Kommo being refused, not our API |
| Error is an *access* error, not payment/registration | Because that is literally what it is |

Error string seen on every outgoing message: *"We couldn't access your WhatsApp account. Please reconnect your phone number to the integration and try again."* This exact string is **not** in Kommo's published error reference (which documents 3117, 3120, 3123, 3136, 3137), but it sits in their "Access and account errors" family.

**Secondary blocker on the fix:** attempting to add our own payment method is refused with *"You can't add a payment method because you're using a shared credit line to pay for ads."* So the ManyChat credit line must be removed **before** a card can be attached.

**Fix sequence:** remove ManyChat's shared credit line (via the `...` menu on the payment method, or the Credit lines section, or ManyChat support if they hold it) -> attach Aguas Profundas MC's own payment method -> ManyChat ceases to be billing partner -> the `Messages` toggle unlocks for Kommo -> grant it -> sends work.

**Ruled out with evidence:** payment (the WABA is funded, "Available funds"), number registration (3119 status = **Connected**), display name (set, visible to customers, not pending), the 24-hour window (client messaged minutes prior; error is not 3108), encoding (plain ASCII failed identically), and our integration (returns `202 Accepted` + message id, message lands in thread with `delivery_status: error`).

### Pattern worth naming

**Both of Isaias's WhatsApp numbers are entangled with former providers at the Meta level.** 566-7542 is blocked by the #3441061 Auto-labels binding, which no amount of work inside Respond.io could clear. 558-3119 is blocked by ManyChat's credit line, which no amount of work inside Kommo can clear. In both cases the new platform is innocent and the residue is upstream at Meta. Future number onboarding should start by auditing the WABA's billing partner and permission assignments **before** any platform work.

### Verified live against account 36745667

| Fact | Value |
|---|---|
| **WhatsApp `origin`** | **`waba`** — NOT `whatsapp`. Kommo's docs only ever show `telegram`. Filtering on the wrong value silently drops every message with no error. |
| Subdomain / Account ID | `infoswecinvestmentscom` / `36745667` |
| Integration ID | `2ab81db4-0ed3-4947-8112-f6a1cdec6ae0` |
| amojo_id | `05115415-d76f-43ee-a541-f4cdcad8ba68` |
| Token scopes | `crm`, `files`, `files_delete`, `list_external_messages`, `notifications`, `push_notifications`, `send_external_messages` |
| Token expiry | 2030-01-01 (long-lived, no refresh) |
| Plan | **Trial** (100 Chats API requests; payment planned) |
| Country / Currency | DO / DOP (RD$) |

Credentials stored in `/app/data/master.env` under `KOMMO_*` with a timestamped backup.

**Live inbound test succeeded.** One WhatsApp text to 3119 auto-created contact `23932090` ("Isaias Perez", phone `+16103575363`), lead `9375716`, and talk `100` (status `in_work`, origin `waba`). Message arrived as `type=incoming`, `message_type=text`, `author.type=external`. Our API sends return `202` and appear in-thread with `author.type=bot`, name "WhatsApp Business" — useful later, since a human agent would be `internal`, giving the handoff logic a clean signal.

Consumed 2 of 100 trial Chats API requests on send tests. Reads (`GET /talks/{id}/messages`) do not consume quota.

### Architecture decision: run our own agent, not Kommo's

Kommo, like every platform before it, **does not allow creating AI agents via API**. Its AI Agent is UI-configured; the public AI API only adds knowledge sources, policies, and product sync. Salesbot has no create/update endpoint and its script isn't even readable via API.

Kommo won anyway, because it is the only one of the four that exposes **both** halves of the loop: an inbound webhook (`add_message`) and an outbound send (`POST /api/v4/talks/{talk_id}/send_message`). That lets us skip Kommo's AI entirely and run Claude from our own service, with prompts and flows in git.

Audio and location settled the choice. Kommo's internal AI documents **no** support for either. The webhook path handles both: `message_type` includes `voice`/`audio` with a downloadable `attachment.link`, and `location` is a **first-class typed value** — a real upgrade over Respond.io, where a GPS pin arrived as the opaque string `[Unsupported message]` and had to be pattern-matched.

### Built this session (not yet deployed)

`kommo-agent` — FastAPI service, Docker + Traefik, mirroring the infra-mcp pattern. All modules compile; the PHP-style urlencoded webhook parser is unit-tested against realistic text, voice, location, batched, and outgoing payloads.

Notable decisions, each one a scar from a previous build:

- **Handoff is enforced in code** (`state.py`), not as a prompt instruction. Both Botpress and Respond.io leaked messages after handoff because the pause lived in the prompt and the model ignored it. The worker now returns before ever calling Claude.
- **Location handling is deterministic** — `message_type == "location"` fires the verbatim ubicación message and the handoff. No model judgment, no magic strings.
- **Whisper hallucination filter** — silence produces confident filler ("Gracias.", "Thank you.", the Amara.org subtitle artifact). Filtered explicitly, plus a minimum-audio-size gate, because an agent that *acts* on transcripts will otherwise fabricate customer intent.
- **Webhook dedupe** — Kommo retries; we must never answer twice.
- **Domain vocabulary hint** for Whisper (pozo, IMHOFF, radioestesia, aforo) so Dominican Spanish jargon survives transcription.
- **Image constraint made honest** — `send_message` is text-only today, so the prompt tells the agent it cannot send photos and to hand off if asked, instead of silently failing.

KB ported verbatim (all 4 files). Ingestion script asserts 1536 dims so it cannot drift from the existing Qdrant convention.

### Open items

1. **Remove the ManyChat credit line.** Everything else is downstream of this.
2. **Chats API add-on limit reset period** — Trial 100 / Pro 500, but Kommo's docs never say per day, month, or lifetime. **Ask before paying.** If Pro is 500/month, a lead-gen number burns it in days and the architecture needs rethinking.
3. Confirm whether ManyChat still has 3119 connected on their side — disconnecting there may release the credit line automatically.
4. Unverified: `message_type` for voice notes (`voice` vs `audio`), whether the inbound webhook carries `attachment.link` (fallback to the free history endpoint is implemented), and whether GPS coordinates are exposed anywhere.
5. Two WABAs share the name "Aguas Profundas" (`1343110684623231` and `1064953052052555`). Confusing; rename or retire the old one once 3119 is live. **Do not delete** — the old one likely holds 566-7542 and its history.
6. Rotate the GitHub PAT and the Kommo integration secret (the secret was pasted in chat and is unused by our architecture).

---

## Background: Platform History (July 11–16, 2026)

Four platforms in six days. Recording why each failed so we don't relitigate.

| Platform | Outcome | Why it died |
|---|---|---|
| **Botpress** | Abandoned | Agent authoring UI-bound; flows couldn't live in git. |
| **Chatwoot** | Built, then orphaned | Self-hosted v4.15.1 deployed on the VPS with the audio fix and multi-tenant provisioning script, all verified working. Superseded by the Respond.io decision before it carried traffic. Still running; decommission undecided. |
| **Respond.io** | Abandoned | AI Agent config is **UI-only** — no API, no MCP (28 MCP tools, none for agent config). Retrieval couldn't pick its own knowledge source, forcing a "search these keywords" hack into the prompt. `send_message` equivalent couldn't attach the séptico photos. GPS pins arrived as `[Unsupported message]`. |
| **Kommo** | **Current** | Also UI-only for agent authoring — but the only one exposing both an inbound webhook and an outbound send API, which lets us own the agent in code. |

**The #3441061 saga (566-7542).** Meta blocked coexistence onboarding with "Your phone number is already linked to Automatic Events. Turn off Auto labels in Business Tools > Labels." The client's WhatsApp Business app has **no Labels menu** — because WhatsApp is retiring/replacing Labels with "Lists" (per WhatsApp Business's own announcement). So the in-app control Meta points to no longer exists, and only Meta can clear the binding server-side. Never resolved. Directly caused the pivot to a second number (3119).

**Client assets carried across every platform unchanged:** four KB files (estudio de agua, perforación, séptico IMHOFF, contacto/precios) containing Wellington's real sales copy and objection handling, plus the persona, the verbatim ubicación message, the séptico intro, and the RD$5,000 deposit flow. These have survived four platforms and are the actual product. The platform is just plumbing.
