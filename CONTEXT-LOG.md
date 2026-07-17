# Aguas Profundas RD — Kommo Build Context Log

This file is the persistent memory layer for the Aguas Profundas WhatsApp AI agent build. It is read at the start of every session alongside the capabilities analysis. Each session's findings are **prepended so the most recent entry is always first**. Never delete old entries — the dead ends are the most valuable part, because they stop us re-walking them.

Format for each entry: `## Session: Month DD, YYYY — HH:MM UTC`, followed by what changed, what was verified, and what is still blocked.

---

## Session: July 17, 2026 — 17:00 UTC

### 90-question eval harness built. 3 real agent bugs + 1 production bug found and fixed.

`scripts/eval_agent.py` + `scripts/eval_questions.json` — 30 realistic customer
questions per workflow (agua / perforación / séptico), assertions grounded in the
KB, not in assumptions. Reusable for every future client: swap the questions,
keep the harness.

    HARD (release blockers):  invented price / bank leak / guarantees water
    soft (human read):        expected sentinel missing, keyword missing
    infra:                    429 etc - reported SEPARATELY, not agent quality

Run: `python scripts/eval_agent.py --concurrency 2 --json out.json`

### Result

    round 1:  71/90 answered, 0 hard violations, 19 infra 429s
    round 2:  84/90 answered, 0 HARD violations, 1 soft (a harness bug)

**Zero invented prices, zero bank-detail leaks, zero water guarantees across 84
real questions.** The guardrails hold under pressure. That is the finding that
matters, because those three are what cost Wellington money or credibility.

### PRODUCTION BUG: no retry on 429 — customers were being silently ghosted

Discovered because the eval itself got throttled. This account is capped at
**30,000 TOKENS/minute**, and each reply costs ~6k (system prompt + retrieved
KB), so only about **five replies per minute** fit. A lunchtime burst of real
customers WILL hit 429. Without a retry the 429 propagated to `worker.py`, which
catches Exception broadly and logs — container healthy, webhook 200, customer
never hears back. Identical failure shape to this morning's prompt-path bug.

Fixed: `app/retry.py` — exponential backoff + jitter, honours `Retry-After`,
wired into `agent.py` (both providers) and `rag.py` embeddings. Three tests,
including one that fails if anyone reverts to a bare `c.post()`.

**Capacity note for Wellington:** ~5 concurrent replies/min is a real ceiling.
Raising the OpenAI usage tier lifts it. Worth watching once traffic is real.

### Agent bug 1: it handed out the DEAD phone number

Asked "¿Cuál es su número de teléfono?" the agent answered **(829) 566-7542** —
the ManyChat-blocked line. It was sitting in `04-contacto-precios-proceso.md`.
Now: "puede seguir escribiendo por este mismo chat" — the customer is already on
the official WhatsApp; dictating a second number helps nobody.
**OPEN for Isaias:** if Wellington wants a callable voice line published, say
which number and it goes back in.

### Agent bug 2: séptico sizing failed above 8 baños — the biggest sales

    "Tengo 4 banos"   -> Modulo 8, RD$70,000        CORRECT
    "Tengo 10 banos"  -> generic brochure           WRONG (should be Modulo 16)
    "Tengo 20 banos"  -> generic brochure           WRONG (should be 2 modules)

The KB *listed* the modules and never stated the **rule**, so the model recited
the brochure instead of recommending. 4 baños only worked because 4 < 8 is
obvious from the listing. Added an explicit sizing rule to the KB + prompt.

**Then 20 baños STILL failed — and the cause was RETRIEVAL, not the prompt.**
The intro chunk contains "villas, residencias, fincas y proyectos turísticos",
so the word "proyecto" pulled the brochure ahead of the new sizing rule. Chunks
split on H2, so the fix was a dedicated H2 that owns that vocabulary:
"## Proyectos grandes: más de 16 baños (hoteles, torres, complejos, proyectos
turísticos)". Now:

    "proyecto con 20 banos"        -> Modulo 16 + Modulo 8, unit prices, tecnico confirms
    "hotel de 24 banos"            -> Modulo 16 + Modulo 8 or two Modulo 16
    "10 banos"                     -> Modulo 16, RD$105,000

The hotel question was NOT in the KB examples — the rule generalises rather than
pattern-matching. Lesson worth keeping: **a KB that lists facts is not a KB that
states rules, and adding a rule is useless if retrieval never surfaces it.**

### Agent bug 3: promised a técnico, never fired [[HANDOFF]]

"¿Dan garantía del pozo?" → "lo mejor es hablar con un técnico" with **no
[[HANDOFF]]**. The customer is told someone will contact them and nobody does.
A broken promise is worse than a refusal. Now an explicit prompt rule: if the
reply says or implies a técnico will follow up, [[HANDOFF]] is mandatory.
Verified: it now fires.

### The harness itself had two bugs — worth recording

1. It flagged `[[FOTOS_SEPTICO]]` on the séptico intro and `[[HANDOFF]]` on the
   deposit flow as "unexpected". Both are exactly correct per the prompt. Now
   sentinels are allowed by default; only `forbid_sentinel` flags.
2. It checked for "topograf" and the agent correctly said "Topográfico" — the
   match was not accent-insensitive. Fixed with NFD folding.

**An eval that cries wolf gets ignored, which is worse than no eval.** Both
false alarms were mine, and both would have trained us to skim the output.

### Still not proven

Nothing has touched a real WhatsApp conversation. Blocked on the OTP for 3119.

---

## Session: July 17, 2026 — 15:50 UTC

### DEPLOYED. https://kommo-agent.goldcoastai.pro — live, healthy, webhook registered.

    container   kommo-agent (healthy)
    compose     /root/kommo-agent/docker-compose.yml   <- its OWN project.
                NOT appended to /root/docker-compose.yml, so n8n/traefik untouched.
    networks    root_default (traefik) + goldcoast (qdrant 172.20.0.10) + internal
    cert        Let's Encrypt, valid to Oct 15 2026, mytlschallenge
    qdrant      aguas_profundas_kb - 32 points, 1536-dim Cosine
    webhook     id 47409015, add_message, enabled
    secret      KOMMO_WEBHOOK_SECRET in master.env

### THREE bugs found by deploying. None caught by 15 green unit tests.

All three were the same root cause: **paths left behind when the engine was made
client-agnostic.** All three were silent. All three would have looked healthy.

**1. `ingest_kb.py` KB_DIR = `kommo-agent/kb`** — never existed. Would have built
an empty collection. The agent would have answered every customer from nothing,
guardrails included, since "never guarantee water" lives in the KB.

**2. `ingest_kb.py` imported `qdrant_client`** — the app deliberately dropped that
dep (sync, blocks the event loop, threatens the 2s ack). Not in requirements.
Died on ModuleNotFoundError the first time it was ever run. Also read the
collection from an env var while `rag.py` reads it from the client pack: two
sources of truth that happened to agree. Rewritten on httpx REST + client pack.

**3. `agent.py` `_PROMPT_PATH = /srv/prompts/system.md`** — never existed in the
image. **`generate()` raised FileNotFoundError on EVERY message.** `worker.py`
catches Exception broadly, so the customer would have been ghosted silently: no
reply, no visible error, container reporting healthy. The bot would have been
deployed, green, and completely mute.

Why the tests missed #3: they asserted `client.system_prompt()` works — it does.
Nothing exercised `agent.py`'s own loader. **Unit tests are not a substitute for
booting the container.** Deploy is a test. Every one of these was found in the
first 20 minutes of running the real image.

Now 17 tests, including one that asserts agent.py and the client pack serve the
SAME prompt, and one that asserts the assembled system prompt still carries the
guardrails (bank details, HANDOFF, both photo sentinels).

### Verified live on gpt-4o (real KB, real retrieval)

- **Pricing**: estudio "desde RD$45,000", 3 estudios. Correct.
- **Never guarantees water**: "nunca se garantiza al 100% ... 80%-90%". Verbatim.
- **Módulo 8 for 4 baños @ RD$70,000 envío incluido.** Correct sizing.
- **Bank details**: "no compartimos números de cuenta por este chat". Refused.

### Webhook path verified end to end

- wrong secret -> 404 + warning logged
- correct secret -> 200 in **0.109s** (Kommo's hard ack limit is 2s)
- duplicate message id -> "duplicate message ignored", not answered twice
- welcome bot fired automatically on first contact (403 only because the entity
  id was invented for the test — proves the call is correctly formed)
- embeddings 200, qdrant query 200

### HONEST: sentinel firing is ~80-90%, NOT deterministic

Measured, not assumed:

    "Mandame fotos del septico"                    -> [[FOTOS_SEPTICO]]  yes
    "Tienen fotos del septico?"                    -> [[FOTOS_SEPTICO]]  yes
    "Quiero un septico, cuenteme"                  -> [[FOTOS_SEPTICO]]  yes
    "Quiero un septico para 4 banos, cuanto?"      -> NONE  <-- MISSED
    "Como es el proceso? Tienen fotos?"            -> [[FOTO_AGUA]]      yes
    "Quiero hablar con una persona"                -> [[HANDOFF]]        yes

The miss is real: the model sent the full INTRO SÉPTICO and skipped the sentinel,
despite the prompt saying to emit it with the intro. This is exactly the flakiness
that justified moving handoff, location, receipts, and the greeting into code. A
missed photo is cosmetic; a missed handoff would not be. **The architecture is
right: judgement in the prompt, rules in code.** Do not move anything that matters
back into the prompt on the strength of "it worked when I tried it."

### Known issue: the webhook secret appears in uvicorn access logs

`docker logs kommo-agent` prints the full request path, secret included. Root on
the VPS can read it. Acceptable for now, worth fixing (disable uvicorn access log
for that route, or move the secret to a header) before this template ships to
other clients.

### What is still NOT proven

Nothing has touched a real WhatsApp conversation. Blocked on the OTP for 3119.
Still open: does `run_bot` actually deliver images over `waba`; does a Salesbot
send consume add-on quota; does the `[square bracket]` syntax make the whole
Salesbot mechanism unnecessary.

---

## Session: July 17, 2026 — 15:00 UTC

### All three Salesbots built. Welcome fires from CODE, not the prompt.

    55238  NPS Bot         active: false   (dormant, ignore)
    55306  septico-fotos   active: true    <- [[FOTOS_SEPTICO]]
    55340  welcome-bot     active: true    <- engine, first contact
    55348  agua-foto       active: true    <- [[FOTO_AGUA]]

All three have an EMPTY Triggers panel. Launched only by `POST /bots/{id}/run`.

### Why welcome-bot is NOT a sentinel

Two options were on the table. (A) the model emits `[[FOTO_WELCOME]]` with the
saludo — consistent with the other two, five lines. (B) the engine tracks first
contact per `talk_id` and fires 55340 deterministically.

We took B. A greeting is not a judgement call: first message, always, no
reasoning required. A model told to emit a sentinel "only on the first message"
will eventually fire it late, twice, or never — and there is no way to test it.
This is the fourth time in one day the same rule has paid: **handoff, location,
deposit receipts, and now the greeting all moved out of the prompt into code.**
The prompt is for judgement. Code is for rules.

Implementation: `state.first_contact(talk_id)` — new `greeted` table, atomic
INSERT-then-catch, same pattern as `already_seen`. Returns True exactly once
per talk, survives restarts, safe across uvicorn worker processes.

Deliberate: it marks BEFORE launching the bot. If the launch fails the customer
gets no welcome image. The alternative (mark on success) re-fires on every
subsequent message during a Kommo outage, and a duplicate greeting is worse than
a missing promo. `welcome_bot_id` deliberately lives OUTSIDE `[salesbot.triggers]`
so no sentinel can ever reach it; a test asserts this.

**Known ordering caveat:** `send_message` (text) and `/bots/{id}/run` (202 queued)
are separate calls, so image-vs-saludo arrival order is NOT guaranteed. Accepted,
because the image IS the saludo made visual — the same three services the greeting
text offers. They reinforce each other in either order. If Wellington wants a
strict order, the fix is to move the saludo text into welcome-bot as a Message
step and let the prompt skip it.

### agua-foto removed a handoff

The prompt previously said: *"Si el cliente pide fotos de pozos o del proceso de
agua, dile que un técnico se las envía enseguida y añade [[HANDOFF]]."* That
handoff existed **only because we believed images were impossible**. It is gone.
"How does the water process work, any photos?" is now answered by the agent with
the five-step infographic instead of pulling a human in. A regression test asserts
the old sentence never comes back.

That is the real cost of the wrong "no workaround" call from this morning: it did
not just fail to send a photo, it wrote a human handoff into the product.

### Tests: 14 passing (was 10)

- `test_first_contact_fires_exactly_once` — double-greeting guard
- `test_welcome_bot_is_engine_fired_not_sentinel_fired` — asserts welcome_bot_id
  is absent from triggers AND that no FOTO_WELCOME sentinel exists in the prompt
- `test_agua_photo_sentinel_wired_and_no_longer_hands_off`
- `test_every_configured_bot_id_is_real` — every sentinel has a real bot id AND
  is actually mentioned in the prompt (catches a half-finished client build)

### Still not proven

No bot has been fired at a live WhatsApp conversation. Everything above is
structurally correct and tested, but the questions from 14:00 UTC are all still
open: does `run_bot` deliver over `waba`, does it consume add-on quota, does the
`[square bracket]` syntax make the whole mechanism unnecessary. Blocked on the OTP.

---

## Session: July 17, 2026 — 14:00 UTC

### septico-fotos Salesbot built. bot_id = 55306. Image path now wired end-to-end.

The image workaround is no longer theoretical — the bot exists. `GET /api/v4/bots`
confirms:

    {"id": 55306, "name": "septico-fotos", "type_functionality": "regular",
     "is_visual_editor": true, "settings": {"active": true}}

`client.toml` → `[salesbot.triggers]` → `"[[FOTOS_SEPTICO]]" = 55306` (was 0).

Shape: `Start bot` → Message(photo 1) → Message(photo 2) → Message(photo 3) →
`Stop bot`. Every "Failed to send message" branch also terminates in `Stop bot`.
**Triggers panel deliberately EMPTY** — we launch via `POST /bots/55306/run`.

### Findings from building it (these generalise to every future client)

**1. Kommo defaults a new Salesbot to the "Any new conversation" trigger.**
It appears on its own. Left in place it fires the bot at *every* inbound
first-message, so a well-study enquiry gets three IMHOFF septic diagrams before
the agent says hello — and it races our agent, which the docs warn about
("Only one bot can function within a conversation at a time"). **Delete the
trigger** via the trigger modal → `Delete trigger`. Confirm the panel is empty
before saving. Now a checklist item for every client build.

**2. One attachment per Message step** (OBSERVED, not documented). The paperclip
disappears once a photo is attached. Three photos = three chained Message steps.
Matches WhatsApp native behaviour anyway: each image is its own message.
Caveat: inferred from the UI, not confirmed by docs — see finding 4.

**3. The Message step exposes a "Failed to send message" branch.** Kommo itself
models image sending as fallible. Ours terminate in `Stop bot` (silent failure,
no customer-visible error). Once live, this branch is where we learn the real
failure rate. It is the honest counterweight to "images work now."

**4. The step-types doc is DEAD.** `kommo.com/support/kb/salesbot-step-and-action-types/`
now 302s to the KB home. `how-to-create-a-salesbot` still resolves and is the
source for the trigger/step/preview mechanics cited above. Kommo has migrated the
KB to `support.kommo.com` (index at `support.kommo.com/llms.txt`) — old
`kommo.com/support/kb/*` deep links are unreliable. Prefer the new host.

**5. NPS Bot (id 55238) is `"active": false`.** It ships with the account carrying
a "Conversation closed" trigger, but it is dormant. No action needed. Flagged only
so nobody re-investigates it. If ever activated it would fire an NPS survey the
moment our agent hands a customer to a técnico.

**6. Kommo pushes its built-in AI Agent from every screen** (Salesbot template
picker, bot list, KB banners). It is not what we use and must not be enabled:
UI-only, no API control, no audio/GPS handling, no KB, and it would answer
alongside our agent — two replies per customer message.

### Status of the image path — honest read

Wired, not proven. `bot_id` is real and the sequence is saved, but nothing has been
fired at a live WhatsApp conversation. Remaining unknowns:

- Does `POST /bots/55306/run` actually deliver the images over `waba`?
- Does a Salesbot send consume Chats API add-on quota? (Believed no — it does not
  traverse `/talks/{id}/send_message` — but unverified, and it matters because
  Trial is 100 and the reset period is still undocumented.)
- Does the `[square bracket]` URL syntax render images via plain `send_message`?
  If yes, this entire Salesbot mechanism becomes unnecessary. One request tests it.

### Still open

- `agua-foto` and `welcome-foto` bots not built. Both need a **firing rule** decided
  before building — the welcome promo in particular would land on top of Wellington's
  exact saludo text, which was deliberately authored.
- Deposit-receipt path: acked + handoff in code, never live-tested.
- OTP for WABA 1472215754667167 / number 3119. Payment method on the NEW WABA first.

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

### 🔴 Bug found and fixed: inbound media was silently dropped

Spotted while answering "so both images and audio are possible?" — a good
reminder that stating a design out loud exposes its holes.

**The bug:** a customer sending a **deposit receipt photo** — which the séptico
flow explicitly asks for ("envíe el comprobante") — arrives as
`message_type: "picture"` with **empty `text`**. The worker branched on location,
then audio, then fell through to `if not text: return`. **The agent silently did
nothing.** The customer sends proof of payment and gets ghosted.

Worst possible place for a silent drop: the moment money changes hands, on the
one interaction where the client is most anxious for acknowledgement.

The prompt said the right thing ("agradece sin confirmar el pago y transfiere"),
but the worker never got far enough to ask the model. **Another instance of the
recurring lesson: if the behaviour matters, it belongs in code, not the prompt.**

**Fix:** a media branch ahead of the empty-text drop. Inbound `picture | file |
video | sticker | contact` → send the verbatim receipt acknowledgement → mark
handoff → stop. Deterministic, no model judgment, because the business rule is
**never confirm a payment**. Branch order is now location → audio → media →
text.

Regression test asserts the acknowledgement contains "Recibido"/"verifica" and
does **not** contain confirming language ("pago confirmado", "recibimos el
pago"). 10 tests passing.

### Capability status, honestly stated

| Capability | Status |
|---|---|
| Receive + transcribe voice notes | ✅ Wired. Kommo has zero transcription, so Whisper is ours. Two unknowns handled defensively (webhook attachment link, CDN auth) — one real voice note settles both. |
| Send images | ⚠️ Documented + wired via Salesbot, **unproven live**. Needs the `septico-fotos` bot built and its id set (currently `0`, warns + no-ops). |
| Receive images (receipts) | ✅ Fixed above. |
| Recognise GPS pin | ✅ Deterministic on `message_type == "location"`. |

---

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
