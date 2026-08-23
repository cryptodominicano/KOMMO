# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent. This file lives in the
"Aguas Profundas" Claude project so every session starts oriented on the live system.

Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-23 (scope-derived audio routing, stage machine + sector memory, state-gated price objection, buy-signal routing, stage-based handoff silence, spam-filter name fix, pre-deploy UBA guard. VOZ_AGUA_4 removed.)

---

## 1. The one thing to know

The live agent runs on **Kommo**, not Botpress. It is a self-hosted **FastAPI service**
(`kommo-agent`) on the VPS that owns the AI loop in our own code. Kommo is only the
WhatsApp/Instagram/Facebook transport and CRM.

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

## 5. Agua flow (CURRENT — simplified 2026-08-22)

```
1. Welcome text + VOZ_AGUA_1 (process, 80-90% success, range RD$45k-50k)
2. Ask: pueblo/sector of the terrain
3. Confirm province → disclose EXACT price + deposit info:
   "Perfecto, [Pueblo] pertenece a la provincia [Provincia]. El estudio completo
   (topográfico + radiestesia + geohidrológico) tiene un costo de RD$[X]. Para
   iniciar se requiere un depósito de RD$5,000 (estudio topográfico) y luego
   RD$10,000 para la visita presencial — el equipo le coordina todo.
   ¿Tiene alguna pregunta antes de proceder? [[SECTOR:Provincia|Pueblo]]"
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

| Status ID | Stage |
|---|---|
| `109083023` | Incoming leads (unsorted) |
| `109168423` | **Atención humana** ← handoff target |
| `109083027` | Initial contact |
| `109083031` | Discussions |
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

- `docker restart` reloads from committed image — always `docker commit` first
- KB changes require re-ingestion: `docker exec -w /srv kommo-agent python3 scripts/ingest_kb.py`
- Every Salesbot must have empty Triggers panel in Kommo UI
- Never push to Vercel manually — push to GitHub
- infra-mcp drops under load — `docker restart infra-mcp` resolves
- **Deploy cycle: syntax check → `scripts/prompt_guard_uba.py` (UBA guard, blocks on exit 1) →
  import smoke test → `docker commit` → restart → health → push + update CONTEXT-LOG**
- `docker restart` does NOT reload env_file — use `docker compose up -d` for env changes

---

## 14. Open items

1. Agua flow validated end-to-end live (talk 906, 2026-08-23): welcome → sector capture →
   location audio → price objection (gate open) → buy signal → name+phone → handoff → silence.
   Remaining agua scenarios to live-test: GPS pin, banco-foto/deposit path (human-handled now,
   but confirm the acknowledgement text), and a returning next-day customer reusing an open talk.
2. Séptico flow: price-objection gate now flow-aware (`precio_septico`), but the VOZ_IMHOFF_1
   ledger-write + septico objection gate were not yet live-tested — run a séptico objection.
3. Consider keying `sector`/`stage` by lead_id (not talk_id) so flow-state survives a talk CLOSE,
   not just an open talk. Not urgent (Kommo reuses the talk_id), but it's the durability gap.
4. Pre-existing hardening from the playbook still open: silence the webhook-secret access log,
   drop customer transcripts to DEBUG (Business Solution Data at rest).
2. Complete end-to-end live test: name+phone capture → [[HANDOFF]] confirmed
3. VOZ_AGUA_1: 2:01 duration, re-recording pending (target 30-40s)
4. Daily conversation-review automation: not built
5. Legacy number +1 829-566-7542: wind-down pending
6. Wellington_Lider_Foto (85808): verify image loaded in Kommo UI
