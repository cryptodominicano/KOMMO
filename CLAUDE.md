# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent. This file lives in the
"Aguas Profundas" Claude project so every session starts oriented on the live system.

Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-13 (verified from live Kommo API + conversation review).

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
       BLOCK CHECK (NO_REACTIVAR/BLOQUEADO tag on lead OR contact) → exit
       SPAM GUARD (off-topic first messages) → exit
       voice/audio -> transcribe.py (OpenAI Whisper)
       location    -> linderos web-app link or handoff
       picture/file-> acknowledge + handoff
       text        -> keyword match -> voice bot (WhatsApp only)
                      if voice bot fired + has followup → BYPASS LLM, send direct
                      else → RAG + LLM (gpt-4o)
                      PRE-SEND supersession check → discard if superseded
  -> app/kommo.py POST /talks/{id}/send_message
```

---

## 4. Access and key IDs (verified live 2026-08-13)

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
| `55340` | welcome-bot | Engine: first contact (all channels) |
| `55348` | agua-foto | Engine: `[[FOTO_AGUA]]` marker |
| `55956` | banco-foto | Engine: `[[DEPOSITO]]` marker |
| `59058` | Payment-Audio | Engine: `[[AUDIO_PAGO]]` marker |
| `76624` | septico-ficha-tecnica | Engine: `[[SEPTICO_FICHA]]` |
| `76632` | septico-comparativa | Engine: `[[SEPTICO_COMPARATIVA]]` |
| `76634` | septico-funcionamiento | Engine: `[[SEPTICO_FUNCIONAMIENTO]]` |
| `76646` | septico-ventajas | Engine: `[[SEPTICO_VENTAJAS]]` |
| `85776` | VOZ_AGUA_1 | Engine: first water contact (WhatsApp only) |
| `85778` | VOZ_AGUA_2 | Engine: drilling price keywords |
| `85780` | VOZ_AGUA_3 | Engine: start process keywords |
| `85782` | VOZ_AGUA_4 | Engine: payment/deposit keywords |
| `85784` | VOZ_AGUA_5 | Engine: price objection keywords |
| `85786` | VOZ_AGUA_7 | Engine: payment conditions keywords |
| `85788` | VOZ_AGUA_6 | Engine: office location keywords |
| `85790` | VOZ_AGUA_8 | Engine: call request keywords |
| `85800` | VOZ_IMHOFF_1 | Engine: first séptico contact (WhatsApp only) |
| `85802` | VOZ_IMHOFF_2 | Engine: purchase process keywords |
| `85804` | VOZ_IMHOFF_3 | Engine: séptico price objection |
| `85806` | VOZ_IMHOFF_4 | Engine: location/trust keywords |
| `85808` | Wellington_Lider_Foto | Engine: after VOZ_IMHOFF_4 sequence |

---

## 7. Audio flow summary

Voice bots only fire on WhatsApp (`origin=waba`). Instagram/Facebook get text-only.
One audio per turn, never repeated in same conversation.

When a voice bot fires (VOZ_AGUA_2-8, VOZ_IMHOFF_2-3): LLM is BYPASSED entirely.
The hardcoded follow-up line from `_VOZ_FOLLOWUPS` is sent directly. Logged as
`AUDIO_BYPASS: skipping LLM`.

VOZ_AGUA_1, VOZ_IMHOFF_1, VOZ_IMHOFF_4: no bypass — LLM handles follow-up.

Full keyword lists, transcripts, follow-up text: `kommo-agent/docs/AUDIO_WORKFLOW.md`

---

## 8. Control markers (model emits → engine strips + acts)

```
[[HANDOFF]]               → move to Atención humana, create Sheyla task + internal note
[[FOTO_AGUA]]             → fire bot 55348
[[SEPTICO_COMPARATIVA]]   → fire bot 76632
[[SEPTICO_FUNCIONAMIENTO]]→ fire bot 76634
[[SEPTICO_FICHA]]         → fire bot 76624
[[SEPTICO_VENTAJAS]]      → fire bot 76646
[[DEPOSITO]]              → fire bot 55956 + send AGUAS_BANK_TEXT
[[AUDIO_PAGO]]            → fire bot 59058 (ETAPA 1 water only)
[[LINDEROS_LISTO]]        → send ETAPA 1 deposit info, no handoff
[[SECTOR:Provincia|Pueblo]]→ tag contact + update lead name to "WhatsApp - Pueblo, Provincia"
[[DESC_OFRECIDO]]         → log 5% séptico discount offered
```

---

## 9. Key rules (never break)

- Never confirm a payment — receipt → acknowledge + `[[HANDOFF]]`
- Never guarantee water 100% — always "80-90% con el estudio"
- Never show ETAPA 2 price until client confirms ETAPA 1 study received
- Never send `[[DEPOSITO]]` off unconfirmed voice transcription
- Perforación price/deposit is always a human quote → `[[HANDOFF]]`
- Never give drilling prices in text (VOZ_AGUA_2 audio handles this; prices removed from KB)
- Never repeat audio content in text reply
- One voice note per turn, never repeated in same conversation

---

## 10. Spam / block system

**To block a contact permanently:**
1. Open lead → `#ADD TAGS` → type `BLOQUEADO` → close as lost
2. Open contact → `#ADD TAGS` → type `BLOQUEADO`
3. Rename contact "Spam User" for identification

**Two block tags:**
- `NO_REACTIVAR` — silence after human closes conversation
- `BLOQUEADO` — outright ban, checked on lead AND contact before any processing

**Known blocked contact:** #41676244 "Spam User" +1 829-804-7618

**Note:** WhatsApp Cloud API has NO business-side block list at the Meta level.
Block must be handled at the application layer (our BLOQUEADO tag system).

---

## 11. Inbox / pipeline visibility

- Use **Pipeline board view** (not inbox) for status at a glance
- "Atención humana" column = needs human now
- Lead name auto-updates to "WhatsApp - Pueblo, Provincia" when location captured
- Handoff creates internal note on lead card with context summary
- Enable Kommo AI sentiment labels: Settings → Kommo AI

---

## 12. Infrastructure rules

- `docker restart` does NOT reload `env_file` — use `docker compose up -d`
- `docker commit kommo-agent kommo-agent:latest` before any restart
- infra-mcp drops under load — `docker restart infra-mcp` from VPS SSH
- Never push to Vercel manually — push to GitHub, let git integration handle it
- Every Salesbot must have an empty Triggers panel in Kommo UI
- Patches: write to `/app/data/patch_X.py`, run via `docker exec -i kommo-agent python3 < /app/data/patch_X.py`

---

## 13. Open items

1. Wellington_Lider_Foto (85808): verify image is loaded in Kommo UI
2. septico-fotos (55306): legacy bot — audit before use
3. KOMMO repo README: still says "Claude LLM, not deployed"
4. Daily conversation-review automation: not built yet
5. Legacy number +1 829-566-7542: wind-down pending
6. Monitor VOZ_IMHOFF_1 false positives (generic "Hola" with no séptico keyword)
