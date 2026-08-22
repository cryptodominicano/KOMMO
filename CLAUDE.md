# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent. This file lives in the
"Aguas Profundas" Claude project so every session starts oriented on the live system.

Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-21 (session Aug 21 — flow immutability, hallucination filter, province pricing).

---

## 1. The one thing to know

The live agent runs on **Kommo**, not Botpress. It is a self-hosted **FastAPI service**
(`kommo-agent`) on the VPS that owns the AI loop in our own code. Kommo is only the
WhatsApp/Instagram/Facebook transport and CRM. Any doc, repo folder, or memory that describes
a Botpress bot as the current system is historical.

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
       location    -> linderos web-app link or handoff
       picture/file-> acknowledge + handoff
       text        -> keyword match -> voice bot (WhatsApp only)
                   -> RAG (Qdrant aguas_profundas_kb)
                   -> LLM (gpt-4o) with AUDIO_ENVIADO override if audio fired
                   -> send reply
  -> app/kommo.py POST /talks/{id}/send_message
```

---

## 4. Access and key IDs (verified live 2026-08-21)

| Item | Value |
|---|---|
| Kommo subdomain | `aguasprofundas` |
| Kommo API base | `https://aguasprofundas.kommo.com/api/v4` |
| Kommo account ID | `36745667` |
| Kommo amojo_id | `05115415-d76f-43ee-a541-f4cdcad8ba68` |
| Kommo token | `master.env` → `KOMMO_LONG_LIVED_TOKEN` (expires 2030-01-01) |
| Pipeline ID | `14130431` |
| Handoff stage | `Atención humana` / status_id `109168423` |
| Isaias user_id | `15588735` |
| Sheyla user_id | `15589135` (handoff owner, 2h SLA) |
| Active webhook ID | `47409015` — `add_message` only |
| Service URL | `https://kommo-agent.goldcoastai.pro` (port 8080, uvicorn) |
| Qdrant collection | `aguas_profundas_kb` (1536-dim Cosine, top_k=8, 48 points) |
| LLM | OpenAI `gpt-4o` (chat), `gpt-4o-mini-transcribe` (voice) |
| Primary WABA | +1 829-558-3119 |
| Legacy number | +1 829-566-7542 (winding down) |
| Instagram | @aguasprofundas_rd |

---

## 5. Pipeline stages

| Status ID | Stage |
|---|---|
| `109083023` | Incoming leads (unsorted) |
| `109168423` | **Atención humana** ← handoff target |
| `109083027` | Initial contact |
| `109083031` | Discussions |
| `109083035` | Decision making |
| `109083039` | Contract discussion |
| `142` | Closed - won |
| `143` | Closed - lost |

---

## 6. Salesbots (all active, all must have empty Triggers panel)

| ID | Name | Fired by |
|---|---|---|
| `55340` | welcome-bot | NOT fired — welcome images removed 2026-08-21 |
| `55348` | agua-foto | Engine: `[[FOTO_AGUA]]` marker |
| `55956` | banco-foto | Engine: `[[DEPOSITO]]` marker |
| `59058` | Payment-Audio | Engine: `[[AUDIO_PAGO]]` marker |
| `76624` | septico-ficha-tecnica | Engine: `[[SEPTICO_FICHA]]` |
| `76632` | septico-comparativa | Engine: `[[SEPTICO_COMPARATIVA]]` (mid-convo only, NOT on entry) |
| `76634` | septico-funcionamiento | Engine: `[[SEPTICO_FUNCIONAMIENTO]]` / VOZ_IMHOFF_2 pair |
| `76646` | septico-ventajas | Engine: `[[SEPTICO_VENTAJAS]]` / VOZ_IMHOFF_3 pair |
| `85776` | VOZ_AGUA_1 | Engine: first water contact (WhatsApp only) |
| `85778` | VOZ_AGUA_2 | Engine: drilling price keywords |
| `85780` | VOZ_AGUA_3 | Engine: start process keywords |
| `85782` | VOZ_AGUA_4 | Engine: payment/deposit keywords |
| `85784` | VOZ_AGUA_5 | Engine: price objection keywords |
| `85786` | VOZ_AGUA_7 | Engine: payment conditions keywords |
| `85788` | VOZ_AGUA_6 | Engine: office location keywords |
| `85790` | VOZ_AGUA_8 | Engine: call request keywords |
| `85800` | VOZ_IMHOFF_1 | Engine: first séptico contact (WhatsApp only) — NO image pair |
| `85802` | VOZ_IMHOFF_2 | Engine: purchase process keywords + SEPTICO_FUNCIONAMIENTO image |
| `85804` | VOZ_IMHOFF_3 | Engine: séptico price objection + SEPTICO_VENTAJAS image |
| `85806` | VOZ_IMHOFF_4 | Engine: location/trust keywords |
| `85808` | Wellington_Lider_Foto | Engine: after VOZ_IMHOFF_4 sequence |

---

## 7. Audio flow summary

Voice bots only fire on WhatsApp (`origin=waba`). Instagram and Facebook get
text-only responses. One audio per turn, never repeated in same conversation.

After each audio fires, the engine injects `AUDIO_ENVIADO` into the LLM's
`extra_system` with the exact follow-up one-liner — LLM outputs only that line.

Full keyword lists, transcripts, and follow-up text: `kommo-agent/docs/AUDIO_WORKFLOW.md`

---

## 8. Control markers (model emits → engine strips + acts)

```
[[HANDOFF]]               → move to Atención humana (109168423), create Sheyla task
[[FOTO_AGUA]]             → fire bot 55348
[[SEPTICO_COMPARATIVA]]   → fire bot 76632 (mid-conversation only, NOT on entry)
[[SEPTICO_FUNCIONAMIENTO]]→ fire bot 76634
[[SEPTICO_FICHA]]         → fire bot 76624
[[SEPTICO_VENTAJAS]]      → fire bot 76646
[[DEPOSITO]]              → fire bot 55956 + send AGUAS_BANK_TEXT
[[AUDIO_PAGO]]            → fire bot 59058 (ETAPA 1 water only)
[[LINDEROS_LISTO]]        → send ETAPA 1 deposit info, no handoff
[[SECTOR:Provincia|Pueblo]]→ tag contact by area
[[DESC_OFRECIDO]]         → log 5% séptico discount offered
```

---

## 9. Key rules (never break)

- Never confirm a payment — receipt → acknowledge + `[[HANDOFF]]`
- Never guarantee water 100% — always "80-90% con el estudio"
- Never show ETAPA 2 price until client confirms ETAPA 1 study received
- Never send `[[DEPOSITO]]` off unconfirmed voice transcription
- Perforación price/deposit is always a human quote → `[[HANDOFF]]`
- One question per turn, short WhatsApp messages
- Never give drilling prices in text (VOZ_AGUA_2 audio handles this)
- Never repeat audio content in text reply
- Never mention comprobante fiscal unless customer asks directly (rule 4)
- **FLOW IMMUTABILITY: once agua flow is confirmed, it can never re-lock to séptico**
- All 32 DR provinces are covered — never handoff on province alone

---

## 10. Province pricing (agua flow)

Isla discloses the price in the SAME message that confirms the province. Never advance
without price disclosure.

**RD$45,000** (16 provinces): Puerto Plata, Espaillat, Santiago, La Vega, Monseñor Nouel,
Sánchez Ramírez (Hermanas Mirabal / Salcedo), Duarte, María Trinidad Sánchez, Samaná,
Monte Plata, Santo Domingo, Distrito Nacional, San Cristóbal, Peravia, San José de Ocoa, Azua.

**RD$50,000** (15 provinces): Monte Cristi, Dajabón, Santiago Rodríguez, Valverde (Mao),
Elías Piña, San Juan, Bahoruco, Independencia, Barahona, Pedernales, Hato Mayor, El Seibo,
San Pedro de Macorís, La Romana, La Altagracia.

**RD$5,000 surcharge**: difficult terrain access, with prior client approval.

All 32 provinces covered. Foreign/unrecognizable location → `[[HANDOFF]]` only.

---

## 11. Infrastructure rules

- `docker restart` does NOT reload `env_file` — use `docker compose up -d`
- `docker commit kommo-agent kommo-agent:latest` before any restart
- infra-mcp drops under load — `docker restart infra-mcp` from VPS SSH
- Never push to Vercel manually — push to GitHub, let git integration handle it
- Every Salesbot must have an empty Triggers panel in Kommo UI
- KB changes require re-ingestion: `docker exec -w /srv kommo-agent python3 scripts/ingest_kb.py`

---

## 12. Whisper hallucination filter

`transcribe.py` rejects transcripts via `_looks_hallucinated()`:
- Known silence fillers (Gracias, Amara.org, etc.)
- Repetition loops
- **Prompt-dump detection**: if transcript contains ≥5 of our PROMPT_HINT domain words
  (motoconcho, radiestesia, geohidrológico, bauche, jarabacoa, etc.), Whisper echoed
  our hint — reject. Fires `TranscriptionRejected` → `audio_unclear` message sent.
  Threshold of 5 prevents false positives on real messages with 1-2 domain words.

---

## 13. Open items

1. Callback-capture + service-ID flows: seamless combined delivery when customer gives
   phone number before identifying service (partially fixed in prompt; deeper worker fix needed)
2. VOZ_AGUA_1: 2:01 duration, re-recording pending (target 30-40s)
3. Agua flow validation: payment/deposit → banco-foto, GPS pin → linderos still need E2E testing
4. Daily conversation-review automation: not built
5. Legacy number +1 829-566-7542: wind-down pending
6. KOMMO repo README: still says "Claude LLM, not deployed" — fix when convenient
7. Wellington_Lider_Foto (85808): verify image is loaded in Kommo UI
