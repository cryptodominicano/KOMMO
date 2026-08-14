# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent. This file lives in the
"Aguas Profundas" Claude project so every session starts oriented on the live system.

Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-14 (v2.0 deployed, v3.0 planned).

---

## 1. The one thing to know

The live agent runs on **Kommo**. It is a self-hosted **FastAPI service** (`kommo-agent`)
on the VPS that owns the AI loop in our own code. Kommo is only the transport and CRM.

Status: **deployed and live on Kommo Pro since 2026-07-20. v2.0 deployed 2026-08-14.**
Health: `GET https://kommo-agent.goldcoastai.pro/health` → `{"ok":true,"subdomain":"aguasprofundas","provider":"openai"}`

---

## 2. Where the code and docs live

| What | Where |
|---|---|
| Live engine source | GitHub `cryptodominicano/KOMMO` (`kommo-agent/`) |
| Running service | container `kommo-agent`, `https://kommo-agent.goldcoastai.pro` |
| Build log (read first) | `CONTEXT-LOG.md` (repo root) |
| Commercial-grade spec | `COMMERCIAL_GRADE_SPEC.md` (repo root) |
| Audio workflow | `kommo-agent/docs/AUDIO_WORKFLOW.md` |
| System prompt | `kommo-agent/clients/aguas-profundas/prompts/system.md` |
| Client config | `kommo-agent/clients/aguas-profundas/client.toml` |
| 20-test eval suite | `/app/data/run_tests3.py` (on VPS) |

---

## 3. Architecture

```
WhatsApp / Instagram / Facebook
  → Kommo webhook → FastAPI main.py (ack 200 in <2s, enqueue)
  → worker.py background:
       SCOPE GUARD (L1: patterns, L2: intent check 30-char)
       FLOW LOCK (flow_state SQLite, locked on first message)
       CHANNEL GATE (_is_waba, _is_instagram_comment)
       BLOQUEADO check (lead + contact tags)
       MESSAGE TYPE ROUTING (voice/location/picture/text)
       VOICE BOT SELECTION (keyword → fire before LLM)
       [PLANNED v3.0] HAIKU PRE-PROCESSOR
       RAG (Qdrant, top_k=8)
       LLM (gpt-4o / gpt-4.1 planned)
       POST-GEN PHONE NUMBER FILTER
       PRE-SEND SUPERSESSION CHECK
       send_message → Kommo Chats API
       SENTINEL PROCESSING
```

---

## 4. Key IDs (verified 2026-08-14)

| Item | Value |
|---|---|
| Kommo subdomain | `aguasprofundas` |
| Pipeline ID | `14130431` |
| Handoff stage | `Atención humana` / `109168423` |
| Isaias user_id | `15588735` |
| Sheyla user_id | `15589135` |
| Webhook ID | `47409015` |
| Qdrant collection | `aguas_profundas_kb` (1536-dim, 48 points) |
| LLM | `gpt-4o` → `gpt-4.1` planned |
| Primary WABA | +1 829-558-3119 |
| GitHub | `cryptodominicano/KOMMO`, branch `main` |

---

## 5. Salesbots (ALL must have empty Triggers panel)

| ID | Name | Fired by |
|---|---|---|
| 55340 | welcome-bot | Engine: first contact |
| 55348 | agua-foto | [[FOTO_AGUA]] |
| 55956 | banco-foto | [[DEPOSITO]] |
| 59058 | Payment-Audio | [[AUDIO_PAGO]] |
| 76624 | septico-ficha | [[SEPTICO_FICHA]] |
| 76632 | septico-comparativa | [[SEPTICO_COMPARATIVA]] |
| 76634 | septico-funcionamiento | [[SEPTICO_FUNCIONAMIENTO]] |
| 76646 | septico-ventajas | [[SEPTICO_VENTAJAS]] |
| 85776 | VOZ_AGUA_1 | First water contact |
| 85778 | VOZ_AGUA_2 | Drilling price — LLM BYPASSED |
| 85780 | VOZ_AGUA_3 | Start process — LLM BYPASSED |
| 85782 | VOZ_AGUA_4 | Payment/deposit — LLM BYPASSED |
| 85784 | VOZ_AGUA_5 | Price objection — LLM BYPASSED |
| 85786 | VOZ_AGUA_7 | Payment conditions — LLM BYPASSED |
| 85788 | VOZ_AGUA_6 | Office location — LLM BYPASSED |
| 85790 | VOZ_AGUA_8 | Call request — LLM BYPASSED |
| 85800 | VOZ_IMHOFF_1 | First séptico contact |
| 85802 | VOZ_IMHOFF_2 | Purchase process — LLM BYPASSED |
| 85804 | VOZ_IMHOFF_3 | Price objection — LLM BYPASSED |
| 85806 | VOZ_IMHOFF_4 | Location/trust |
| 85808 | Wellington_Lider_Foto | After VOZ_IMHOFF_4 |

---

## 6. Control markers

[[HANDOFF]] → stage move + task + note
[[DEPOSITO]] → banco-foto + AGUAS_BANK_TEXT
[[AUDIO_PAGO]] → Payment-Audio (ETAPA 1 only)
[[SECTOR:Provincia|Pueblo]] → tag contact
[[SEPTICO_COMPARATIVA/FUNCIONAMIENTO/FICHA/VENTAJAS]] → image bots
[[FOTO_AGUA]] → agua-foto bot
[[LINDEROS_LISTO]] → ETAPA 1 deposit, no handoff
[[DESC_OFRECIDO]] → log 5% discount

---

## 7. Non-negotiable rules

- Never confirm a payment — always ask for comprobante + [[HANDOFF]]
- Never guarantee water 100% — "80-90% con el estudio"
- Never give drilling prices in text
- Never repeat audio content in text
- Never share phone numbers
- Unknown answers: admit honestly + [[HANDOFF]]
- Always Spanish regardless of customer language
- Max 2 lines per reply, one question

---

## 8. Infrastructure rules

- `docker restart` does NOT reload env_file → use `docker compose up -d`
- `docker commit kommo-agent kommo-agent:latest` before any restart
- infra-mcp drops under load → `docker restart infra-mcp`
- Never push to Vercel manually → GitHub only
- Every Salesbot: empty Triggers panel
- Patches: write to /app/data/ → `docker exec -i kommo-agent python3 < /app/data/patch.py`
- End of every session: commit all files + update CONTEXT-LOG.md

---

## 9. Version status

### v2.0 (live, 2026-08-14) — 20/20 tests passing
All engine fixes deployed. 169-line audio-first system prompt.
Full list of fixes in CONTEXT-LOG.md Sessions Aug 10 and Aug 14.

### v3.0 (planned — full spec in COMMERCIAL_GRADE_SPEC.md)
P1 — gpt-4o → gpt-4.1 (URGENT: 5x rule budget)
P2 — Haiku pre-processor: intent extract + scope classify
P3 — System prompt per OpenAI GPT-4.1 spec
P4 — Qualification FSM stages
P5 — Oct 1 cost model (service messages billable in 47 days)
P6 — Voice note length audit (VOZ_AGUA_1 = 2min, too long)
P7 — Spanish multi-intent eval suite

---

## 10. Open items

- Wellington_Lider_Foto (85808): verify image in Kommo UI
- IMHOFF lifespan: ask Wellington → add to KB → re-ingest
- Facebook/Instagram OAuth: re-authorize if delivery errors persist
- Legacy +1 829-566-7542: wind-down pending
- Voice note audit: check all 12 durations
- Oct 1 cost model: build before September 15
- GPT-4.1 Spanish compliance: research before upgrade
