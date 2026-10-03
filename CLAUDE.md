# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent. This file lives in the
"Aguas Profundas" Claude project so every session starts oriented on the live system.

Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-09-20 (audit + 4 fixes deployed; then /root synced + clean rebuild - source==image==container all on current main, image ad1f1819; test suite green 55/0. worker.py decomposition still pending.)

---

## 1. The one thing to know

The live agent runs on **Kommo**, not Botpress. It is a self-hosted **FastAPI service**
(`kommo-agent`) on the VPS that owns the AI loop in our own code. Kommo is only the
WhatsApp/Instagram/Facebook transport and CRM.

**As of Oct 1, 2026 the service runs in ESCALATION MODE** (`[escalation].enabled=true`): the entire conversational engine (RAG, LLM, voice bots, septico, pricing, flow-switch) is gated OFF (preserved in code, reversible with `enabled=false` + restart). The ONLY live behaviour is a lead funnel, first contact gives the flyer + one sector question, then the verbiage + a "Hablar con Wellington" button, and on tap the owner's phone gets a WhatsApp template with the customer's name, phone, sector, and wa.me link while the customer gets a 24h confirmation, after which that conversation is permanently silent. Full build in the Oct 1, 2026 CONTEXT-LOG entry.

Status: **deployed and live on Kommo Pro since 2026-07-20.**
Health: `GET https://kommo-agent.goldcoastai.pro/health` → `{"ok":true,"subdomain":"aguasprofundas","provider":"openai"}`

---

## 2. Where the code and docs live

| What | Where |
|---|---|
| Live engine source (source of truth) | GitHub `cryptodominicano/KOMMO` (`kommo-agent/`) |
| Running service | container `kommo-agent`, `https://kommo-agent.goldcoastai.pro` |
| Build log (read first) | `CONTEXT-LOG.md` (this repo root) |
| Audio workflow reference | `kommo-agent/docs/AUDIO_WORKFLOW.md` |
| Agent persona + flows | `kommo-agent/clients/aguas-profundas/prompts/system.md` |
| Client config | `kommo-agent/clients/aguas-profundas/client.toml` |

---

## 3. Architecture

```
WhatsApp / Instagram / Facebook message
  -> Kommo add_message webhook (POST /webhook/kommo/{secret}, hard 2s ack)
  -> app/main.py: validate origin, dedupe, ack 200, enqueue
  -> app/worker.py (background):
       voice/audio -> transcribe.py (OpenAI Whisper + hallucination filter)
       location    -> acknowledge + [[HANDOFF]] (human team handles GPS/linderos)
       picture/file-> acknowledge + handoff
       text        -> Haiku intent classifier -> voice bot if intent matched (WhatsApp only)
                   -> RAG (Qdrant aguas_profundas_kb)
                   -> LLM (gpt-4o) with AUDIO_ENVIADO override if audio fired
                   -> send reply
  -> app/kommo.py POST /talks/{id}/send_message
```

---

## 4. Access and key IDs (verified live 2026-08-22)

| Item | Value |
|---|---|
| Kommo subdomain | `aguasprofundas` |
| Kommo API base | `https://aguasprofundas.kommo.com/api/v4` |
| Kommo account ID | `36745667` |
| Kommo token | `master.env` → `KOMMO_LONG_LIVED_TOKEN` (expires 2030-01-01) |
| Pipeline ID | `14130431` |
| Handoff stage | `Atención humana` / status_id `109168423` |
| Sheyla user_id | `15589135` (handoff owner, 2h SLA) |
| Active webhook ID | `47409015` — `add_message` only |
| Service URL | `https://kommo-agent.goldcoastai.pro` (port 8080, uvicorn) |
| Qdrant collection | `aguas_profundas_kb` (1536-dim Cosine, top_k=8, 48 points) |
| LLM | OpenAI `gpt-4o` (chat), `gpt-4o-mini-transcribe` (voice) |
| Primary WABA | +1 829-558-3119 |
| Legacy number | +1 829-566-7542 (winding down) |
| Instagram | @aguasprofundas_rd |

---

## 5. Agua flow (CURRENT — VOZ_AGUA_1 moved post-price 2026-08-24)

```
1. Welcome — TEXT ONLY. Greeting + asks pueblo/sector in the same text.
   (VOZ_AGUA_1 does NOT fire here anymore.)
2. Customer gives location.
3. Confirm province → disclose EXACT price + deposit info:
   "Perfecto, [Pueblo] pertenece a la provincia [Provincia]. El estudio completo
   (topográfico + radiestesia + geohidrológico) tiene un costo de RD$[X]. Para
   iniciar se requiere un depósito de RD$5,000 (estudio topográfico) y luego
   RD$10,000 para la visita presencial — el equipo le coordina todo.
   ¿Tiene alguna pregunta antes de proceder? [[SECTOR:Provincia|Pueblo]]"
   → system fires VOZ_AGUA_1 RIGHT AFTER this price text (reinforces price,
     writes estudio_precio to the ledger = opens the price-objection gate).
     No followup (location already captured; price text already invited questions).
4. Answer questions — Haiku classifies intent → fires voice bot automatically
5. Ask: "¿Está listo para proceder con el análisis de su propiedad? 😊"
6. YES → "¿me puede dar su nombre completo y un número de teléfono de contacto?"
         → once received → "Excelente, [Nombre]. El equipo le contactará en breve.
         [[HANDOFF]]"
         CRITICAL: always collect name+phone — Facebook leads have no phone on file
7. NO  → "Aquí estaremos cuando estés listo. 😊"

Human team handles: GPS pin, satellite photo, linderos, deposits, scheduling.
```

---

## 6. Pipeline stages

Engine-driven pipeline progression (2026-08-24). Kommo Incoming-leads acceptance
routes to the ADJACENT stage by pipeline ORDER, so Initial contact must stay
directly right of Incoming leads. The engine then advances the active funnel:

| Status ID | Stage | Set by |
|---|---|---|
| 109083023 | Incoming leads | (entry) |
| 109083027 | Initial contact | engine on welcome + Kommo acceptance |
| 109083031 | Discussions | engine at price step (also buy signal + name/phone) |
| 109168423 | Atención humana | engine on real handoff only |
| 110761119 | Seguimiento | engine on soft close |
| 110761539 | No interesado | engine on hard close |
| 142 | Closed - won | (deposit paid) |
| 143 | Closed - lost | (manual) |

Decision making + Contract discussion were deleted (not meaningful for the water
flow). Active funnel the engine advances = [Initial contact, Discussions]; terminal
stages are never auto-overridden.


| Status ID | Stage |
|---|---|
| `109083023` | Incoming leads (unsorted) |
| `109168423` | **Atención humana** ← handoff target |
| `109083027` | Initial contact |
| `109083031` | Discussions |
| `110761119` | **Seguimiento** ← soft-close / nurture (warm, not ready) |
| `110761539` | **No interesado** ← hard close (explicit no) |
| `142` | Closed - won |
| `143` | Closed - lost |

---

## 7. Salesbots (all active, all must have empty Triggers panel)

| ID | Name | Fired by |
|---|---|---|
| `55340` | welcome-bot | NOT fired — welcome images removed 2026-08-21 |
| `55348` | agua-foto | Engine: `[[FOTO_AGUA]]` marker |
| `55956` | banco-foto | Engine: `[[DEPOSITO]]` marker (séptico only now) |
| `59058` | Payment-Audio | Engine: `[[AUDIO_PAGO]]` marker (reserved, not fired) |
| `76624` | septico-ficha-tecnica | Engine: `[[SEPTICO_FICHA]]` |
| `76632` | septico-comparativa | Engine: `[[SEPTICO_COMPARATIVA]]` (mid-convo only) |
| `76634` | septico-funcionamiento | Engine: `[[SEPTICO_FUNCIONAMIENTO]]` / VOZ_IMHOFF_2 pair |
| `76646` | septico-ventajas | Engine: `[[SEPTICO_VENTAJAS]]` / VOZ_IMHOFF_3 pair |
| `85776` | VOZ_AGUA_1 | Engine: first water contact (WhatsApp only) |
| `85778` | VOZ_AGUA_2 | Haiku: `drilling_price` intent |
| `85784` | VOZ_AGUA_5 | Haiku: `price_objection_agua` intent (declarative + interrogative) |
| `85786` | VOZ_AGUA_7 | Haiku: `payment_conditions` intent |
| `85788` | VOZ_AGUA_6 | Haiku: `location_agua` intent |
| `85790` | VOZ_AGUA_8 | Haiku: `call_request` intent |
| `85800` | VOZ_IMHOFF_1 | Engine: first séptico contact (WhatsApp only) — NO image pair |
| `85802` | VOZ_IMHOFF_2 | Haiku/keyword: purchase process + SEPTICO_FUNCIONAMIENTO image |
| `85804` | VOZ_IMHOFF_3 | Haiku/keyword: séptico price objection + SEPTICO_VENTAJAS image |
| `85806` | VOZ_IMHOFF_4 | Haiku/keyword: location/trust keywords |
| `85808` | Wellington_Lider_Foto | Engine: after VOZ_IMHOFF_4 sequence |

**REMOVED:** VOZ_AGUA_3 (85780) GPS/linderos explanation, and VOZ_AGUA_4 (85782) payment/deposit process — both obsolete (deposits/GPS are human-handled). Bots still exist in the Kommo UI but the engine never calls them.

---

## 8. Voice bot routing — derived from the classifier SCOPE field (2026-08-23)

Audio routing is driven by the Haiku (gpt-4o-mini) **scope** classification, in
code (`get_voz_bot_intents` maps scope->bot). The old parallel `<voz_bots>` XML
block was unreliable (the model dropped it even when scope was correct, so audio
silently stopped firing) — scope is now the single source of truth. A thin
`correct_scope()` layer fixes ONLY two measured slang misreads (drill-cost read as
price objection; oblique location read as greeting), when the correcting evidence
is present. The prompt's few-shot examples stay (they sharpen scope accuracy) but
no longer drive routing.

**Architecture principle:** classify once (scope), act in code. If a bot is
missing, check the scope the classifier returned in the logs first; only add a
`correct_scope()` rule for a *systematic* misread, never a broad keyword list.

Scope → bot mappings:
- `drilling_price` → VOZ_AGUA_2 (never give drilling prices in text)
- `price_objection_agua` → VOZ_AGUA_5 — **gated on `price_disclosed`** (only fires
  after the price was disclosed; the welcome audio VOZ_AGUA_1 records `estudio_precio`)
- `location_agua` → VOZ_AGUA_6 (state-aware LLM followup, not hardcoded)
- `payment_conditions` → VOZ_AGUA_7 (state-aware LLM followup)
- `call_request` → VOZ_AGUA_8
- `ready_to_proceed_agua` → **no audio** — routes to name+phone collection + advance to handoff
- payment/deposit "how" question → answered from KB text (VOZ_AGUA_4 removed)

Flow state: `flow_state` carries `stage` (greeting → price_presented → handoff) and
`sector`; both are injected into the LLM prompt every turn so the model never
re-asks a captured location. Price gate is flow-aware (agua `estudio_precio`,
septico `precio_septico`).

---

## 9. Province pricing (agua flow)

**RD$45,000** (16 provinces): Puerto Plata, Espaillat, Santiago, La Vega, Monseñor Nouel,
Sánchez Ramírez (Hermanas Mirabal / Salcedo), Duarte, María Trinidad Sánchez, Samaná,
Monte Plata, Santo Domingo, Distrito Nacional, San Cristóbal, Peravia, San José de Ocoa, Azua.

**RD$50,000** (15 provinces): Monte Cristi, Dajabón, Santiago Rodríguez, Valverde (Mao),
Elías Piña, San Juan, Bahoruco, Independencia, Barahona, Pedernales, Hato Mayor, El Seibo,
San Pedro de Macorís, La Romana, La Altagracia.

**RD$5,000 surcharge**: difficult terrain access, with prior client approval.
All 32 DR provinces covered. Foreign/unrecognizable → `[[HANDOFF]]` only.

---

## 10. Control markers (model emits → engine strips + acts)

```
[[HANDOFF]]               → move to Atención humana (109168423), create Sheyla task
[[FOTO_AGUA]]             → fire bot 55348
[[SEPTICO_COMPARATIVA]]   → fire bot 76632 (mid-conversation only)
[[SEPTICO_FUNCIONAMIENTO]]→ fire bot 76634
[[SEPTICO_FICHA]]         → fire bot 76624
[[SEPTICO_VENTAJAS]]      → fire bot 76646
[[DEPOSITO]]              → fire bot 55956 + send AGUAS_BANK_TEXT (séptico only)
[[AUDIO_PAGO]]            → reserved; not fired by bot (deposit human-coordinated)
[[LINDEROS_LISTO]]        → reserved for human use; bot no longer emits
[[SECTOR:Provincia|Pueblo]]→ tag contact by area
[[DESC_OFRECIDO]]         → log 5% séptico discount offered
```

---

## 11. Key rules (never break)

- Never confirm a payment — receipt → acknowledge + `[[HANDOFF]]`
- Never guarantee water 100% — always "80-90% con el estudio"
- Never give drilling prices in text (VOZ_AGUA_2 handles this)
- Never mention comprobante fiscal unless customer asks directly (rule 4)
- Never repeat audio content in text reply
- **FLOW IMMUTABILITY: confirmed agua flow can never re-lock to séptico**
- **ALWAYS collect name + phone before [[HANDOFF]]** — Facebook leads have no phone
- All 32 DR provinces covered — never handoff on province alone
- Audio routing derives from classifier SCOPE (code), not a keyword list or a separate block
- Price-objection audio (VOZ_AGUA_5) only fires AFTER price disclosed (`price_disclosed` gate)
- Handoff silence = pipeline STAGE: lead in Atención humana (109168423) → bot fully silent;
  a human moving the lead to another stage reactivates it (grace timer + NO_REACTIVAR are fallbacks)

---

## 12. Whisper hallucination filter

`transcribe.py` rejects via `_looks_hallucinated()`:
- Known silence fillers, Amara.org artifacts, repetition loops
- **Prompt-dump detection**: ≥5 domain hint words in transcript = Whisper echoed prompt
  → `TranscriptionRejected` → `audio_unclear` message to customer

---

## 13. Infrastructure rules

- **DEPLOY FROM SOURCE: `docker compose build && docker compose up -d` on the host. NEVER `docker commit`** — the Dockerfile COPYs app/clients/scripts; the repo is the source of truth. `docker commit` writes to tag `kommo-agent:latest` but compose builds/runs `kommo-agent-kommo-agent:latest` (a DIFFERENT tag), so commits are silently discarded on the next `compose up` (live incident, Aug 2026)
- KB changes require re-ingestion: `docker exec -w /srv kommo-agent python3 scripts/ingest_kb.py`
- Every Salesbot must have empty Triggers panel in Kommo UI
- Never push to Vercel manually — push to GitHub
- infra-mcp drops under load — `docker restart infra-mcp` resolves
- **Deploy cycle: syntax check → `scripts/prompt_guard_uba.py` (UBA guard, blocks on exit 1) → import smoke test → git commit + push → on host: sync source into /root/kommo-agent build context → `docker compose build` → `docker compose up -d` → health → update CONTEXT-LOG**
- `/root/kommo-agent` is the host build context, NOT a git checkout — sync repo source into it before building. compose build/up CAN be driven through infra-mcp: a docker:cli helper with the docker socket + the host context mounted at its real path, then `docker compose -p kommo-agent build|up -d` (exact commands in CONTEXT-LOG 2026-09-20 11:20). SSH is not required.
- `.env` changes ALSO require `docker compose up -d` (plain `docker restart` does NOT reload env_file). `docker restart` is only a fast in-container hotfix you must immediately also push + rebuild

---

- Image is built from the host repo `/root/kommo-agent` (compose `build: .`); only `/data` is mounted, so prompt + client.toml + app code are baked into the image. No `.git` on the VPS working copy; GitHub is synced via the Contents API.
- Prompt/code deploy recipe: edit the host repo (`docker run --rm -v /root/kommo-agent:/work ...`), `docker cp` into the container, `docker commit kommo-agent kommo-agent-kommo-agent:latest`, `docker restart kommo-agent`, then push to GitHub via the Contents API. `system.md` is read fresh per request (no restart); app code (`worker.py` etc.) needs a restart.

## 14. Open items

### LIVE: owner-escalation flow (Oct 1, 2026, the current production behaviour; see CONTEXT-LOG Oct 1)
- Escalation mode is LIVE and in production (`[escalation].enabled=true`); conversational engine gated OFF (preserved, reversible). Flow: first contact -> ONE line "Hola, le saluda Wellington de Aguas Profundas. Para empezar, en que pueblo o sector esta ubicado?" (no flyer yet) -> customer answers by VOICE or text (voice is transcribed; unintelligible audio asks to repeat/type) -> dr_geo normalizes the town (phrase-aware) and prices the verbiage RD$45k or RD$50k by province -> FLYER (bot 97800) + verbiage tail ("Le orientare...") + "Hablar con Wellington" button -> tap -> owner alert (name, phone, sector, wa.me link) + customer 24h confirmation -> permanent silence on that talk.
- Bots (empty triggers, fired by the engine via run_bot): 100104 Boton-Hablar-Wellington (customer button), 100050 Wellington messenger (template sender), 97800 welcome-bot 55340 (flyer). Lead fields: Cliente Telefono 2104942, Cliente Link WhatsApp 2104940, Cliente Sector 2105112.
- Template LIVE: cliente_listo_wellington_v2 (UTILITY, 4 vars incl. Ubicacion [Cliente Sector 2105112]) is approved and the sender bot 100050 points at it, so alerts include the location line. (v1 "Wellingtons CX Messing Flow" id 84458 is the retired 3-var version.)
- Owner recipient: PRODUCTION LIVE on Wellington 829-566-7542 (`owner_contact_id = 39939531`, established). The 849 test contact (26049644) is retired. (Any owner number must have messaged the business once or the alert delivers nowhere.)
- Nudge: if the sector question goes unanswered, a one-time reminder fires after `nudge_minutes` (120) via the scheduled_nudges outbox and auto-cancels on any reply. PROPOSED (not built, pending Wellington's OK): a 2nd end-of-day (~5-6pm) nudge that bypasses the sector question and sends flyer + verbiage defaulted to RD$45,000 + button for customers who were busy that day. See CONTEXT-LOG.
- Billing: the sending WABA 1032952952881055 (829-837-9566) must have a payment method or template sends fail with error 3107. Card is on it (Visa 3450).
- Button match gotcha: WhatsApp truncates quick-reply titles to 20 chars ("Hablar con Wellington" -> "Hablar con Wellingto"); engine matches the prefix "hablar con welling".

### Earlier pre-build notes (historical; the flow is now LIVE per the block above). Still-standing items (WABA number, spam contacts, flyer number) remain valid:
- Decommission the conversational agent -> flyer + Wellington study message + a "talk to Wellington" button that escalates the lead to the owner's phone. Engine not built; conversational engine to be gated off behind a reversible mode flag (not deleted).
- Utility template "Wellingtons CX Messing Flow" submitted to Meta (UTILITY, Spanish, WABA 1032952952881055). Waiting on approval.
- Handoff lead custom fields: "Cliente Link WhatsApp" (id 2104940), "Cliente Telefono" (id 2104942).
- UNVERIFIED: how to fire an approved WABA template to a chosen number with field merge (likely a Salesbot Send-Message step + run_bot on a staged alert lead). Prove before wiring.
- After approval: build the customer in-window reply button, the button-tap handoff (staged alert lead -> fire template), test to 829-204-6993 first, then point at Wellington (+1 829-566-7542, confirmed personal WhatsApp).
- WABA number discrepancy: UI shows live connected +1 829-837-9566 ("Aguas Profundas KOMMO 2", WABA ID 1032952952881055); section 4 lists +1 829-558-3119. Confirm the live inbound number.
- Spam contacts burning paid outbound + nudges: talk 1024 (YouTube spammer), talk 907 (daily Bible-verse) -> tag NO_REACTIVAR, pending go-ahead.
- Welcome flyer image still shows the legacy 566-7542 number (baked in; UI swap, deferred).
- Kommo "Any new conversation" auto-trigger on a welcome Salesbot is the de-facto welcome-image source (violates empty-triggers rule).
- LIVE now (deployed + pushed Sept 21): generic "hola" fires the Wellington welcome (`_is_generic_greeting`); stage-gated agua<->septico flow switch (`state.switch_flow`); double welcome image fixed (code no longer fires welcome_bot 55340).

### Standing

**Done 2026-09-20 (audit + fixes, see CONTEXT-LOG top entry):** webhook-secret
access-log redaction (this item was open below); state.prune() TTL sweep so state.db
no longer grows unbounded; haiku.py and config.py doc/naming accuracy; public linderos
web app and its routes removed (app/linderos.py, app/linderos.html, the router mount in
main.py). All deployed and verified live.

**Deploy state (RESOLVED 2026-09-20):** /root/kommo-agent was synced to current main and
a clean `docker compose build` + `up -d` was run from infra-mcp via a docker:cli host-mount
helper (no SSH). Source == image (ad1f1819) == running container, all on main; public
/health through Traefik ok. The stale-context footgun is closed. Rollback snapshot: image
kommo-agent:latest (885e4f9e).

**Still open:**
1. worker.py DECOMPOSITION. handle_message is one ~1400-line function (mid-function
   imports, locals().get() state passing, a dead `_already_greeted` line). The audit's
   #1 risk. Tier 2 dead code rides with it: internal linderos machinery
   (awaiting_linderos, linderos_first, the [[LINDEROS_LISTO]] media branch, the
   [[AUDIO_PAGO]] strip, the [linderos] block in client.toml).
2. (RESOLVED 2026-09-20) Uptime Kuma monitor "Kommo Agent" (id 10) watches /health,
   status UP, and alerts by email to Isaias on failure. Optional enhancement: add a
   second channel (SMS/Telegram/push) so an outage is caught even if email is missed.
3. Drop customer voice transcripts to DEBUG (Business Solution Data at rest in logs).
4. (RESOLVED 2026-09-20) /root master.env + .env token sync - verified: KOMMO_LONG_LIVED_TOKEN
   is byte-identical (compared by hash, no drift) across the container env, /app/data/master.env,
   /root/master.env, and /root/kommo-agent/.env. /root/.env does NOT carry the key (it holds other
   services' creds incl. live Chakra) and was deliberately left untouched. If ever needed, /root is
   reachable from infra-mcp via the docker socket host-mount, no SSH.
5. Live-test remaining agua scenarios: GPS pin, banco-foto/deposit acknowledgement text
   (human-handled now), and a returning next-day customer reusing an open talk. (Agua
   happy path validated live, talk 906, 2026-08-23.)
6. Live-test séptico objection: VOZ_IMHOFF_1 ledger-write + the flow-aware
   `precio_septico` objection gate were not yet exercised.
7. Consider keying sector/stage by lead_id, not talk_id, so flow-state survives a talk
   CLOSE. Not urgent (Kommo reuses the talk_id), but it is the durability gap.
8. VOZ_AGUA_1: 2:01 duration, re-record to 30-40s.
9. Daily conversation-review automation: not built.
10. Legacy number +1 829-566-7542: wind-down pending.
11. Wellington_Lider_Foto (85808): verify image loaded in Kommo UI.
12. BUSINESS (not code): Meta re-engagement template for weekend leads outside the 24h
    window; accepted bank-details residual risk (Wellington's chosen design).
