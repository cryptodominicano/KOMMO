# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent.
Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-14 — **v3.0 deployed and fully tested.**

---

## 1. The one thing to know

The live agent runs on **Kommo** transport + **FastAPI** engine (`kommo-agent`).
Kommo is only the WhatsApp/Instagram/Facebook transport and CRM.
The AI loop lives entirely in our own code.

Health check: `GET https://kommo-agent.goldcoastai.pro/health`
→ `{"ok":true,"subdomain":"aguasprofundas","provider":"openai"}`

---

## 2. Where everything lives

| What | Where |
|---|---|
| Live engine | GitHub `cryptodominicano/KOMMO` (`kommo-agent/`) |
| Running service | container `kommo-agent` on srv1175204.hstgr.cloud |
| Build history | `CONTEXT-LOG.md` (repo root) — read first every session |
| Commercial spec | `COMMERCIAL_GRADE_SPEC.md` (repo root) — future clients |
| Audio workflow | `kommo-agent/docs/AUDIO_WORKFLOW.md` |
| System prompt | `kommo-agent/clients/aguas-profundas/prompts/system.md` |
| Client config | `kommo-agent/clients/aguas-profundas/client.toml` |
| Core eval suite | `/app/data/run_tests3.py` (on VPS, 20/20) |
| Spanish multi-intent | `kommo-agent/scripts/eval_spanish_multi_final.py` (15/15) |

---

## 3. Architecture (v3.0)

```
WhatsApp / Instagram / Facebook
  → Kommo webhook → FastAPI main.py (ack 200 in <2s, enqueue)
  → worker.py background:
       SCOPE GUARD (L1: patterns, L2: intent 30-char threshold)
       FLOW LOCK (flow_state SQLite — agua/séptico, locked on first msg)
       CHANNEL GATE (_is_waba, _is_instagram_comment)
       BLOQUEADO check (lead + contact tags)
       MESSAGE TYPE (voice→Whisper, location→flow-aware, picture→cooldown)
       VOICE BOT SELECTION (keyword → fire before LLM)
       HAIKU PRE-PROCESSOR (gpt-4o-mini: intent extract + scope classify)
         → multi-intent: "Debes responder TODAS" contract injected
         → adjacent scope: REDIRECT REQUERIDO injected
       FSM STAGE INJECTION (ESTADO ACTUAL: flujo=X, etapa=Y)
       RAG (Qdrant aguas_profundas_kb, top_k=8)
       GPT-4.1 (main model — 5x rule budget vs gpt-4o)
       POST-GEN PHONE NUMBER FILTER (regex strip)
       PRE-SEND SUPERSESSION CHECK
       send_message → Kommo Chats API
       SENTINEL PROCESSING (bots, handoff, tag, rename, stage advance)
```

---

## 4. Key IDs (verified 2026-08-14)

| Item | Value |
|---|---|
| Kommo subdomain | `aguasprofundas` |
| Pipeline ID | `14130431` |
| Handoff stage | `Atención humana` / `109168423` |
| Isaias user_id | `15588735` |
| Sheyla user_id | `15589135` (handoff owner, 2h SLA) |
| Webhook ID | `47409015` (add_message only) |
| Qdrant collection | `aguas_profundas_kb` (1536-dim, 48 points) |
| Main LLM | `gpt-4.1` (forced via model_post_init in config.py) |
| Pre-processor | `gpt-4o-mini` via haiku.py |
| Transcription | `gpt-4o-mini-transcribe` |
| Primary WABA | +1 829-558-3119 |
| GitHub | `cryptodominicano/KOMMO`, branch `main` |
| Latest commit | `c6e200f` |

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
| 85776 | VOZ_AGUA_1 | Engine: first water contact |
| 85778-85790 | VOZ_AGUA_2-8 | Keywords — LLM BYPASSED |
| 85800 | VOZ_IMHOFF_1 | Engine: first séptico contact |
| 85802-85804 | VOZ_IMHOFF_2-3 | Keywords — LLM BYPASSED |
| 85806 | VOZ_IMHOFF_4 | Location/trust keywords |
| 85808 | Wellington_Lider_Foto | After VOZ_IMHOFF_4 |

---

## 6. Control markers

[[HANDOFF]] → stage move 109168423 + Sheyla task + internal note
[[DEPOSITO]] → banco-foto bot + AGUAS_BANK_TEXT
[[AUDIO_PAGO]] → Payment-Audio (ETAPA 1 agua only)
[[SECTOR:Provincia|Pueblo]] → tag contact by area
[[SEPTICO_COMPARATIVA/FUNCIONAMIENTO/FICHA/VENTAJAS]] → image bots
[[FOTO_AGUA]] → agua-foto bot
[[LINDEROS_LISTO]] → ETAPA 1 deposit (no handoff)
[[DESC_OFRECIDO]] → log 5% discount offered

---

## 7. Non-negotiable rules (enforced in code + prompt)

- Never confirm a payment — always ask for comprobante + [[HANDOFF]]
- Never guarantee water 100% — "80-90% con el estudio"
- Never give drilling prices in text — VOZ_AGUA_2 handles
- Never repeat audio content in text reply
- Never share phone numbers — enforced by post-gen regex filter
- Unknown answers: honest admit + [[HANDOFF]] — never invent
- Always Spanish regardless of customer language
- Max 2 lines per reply, one question, no lists or bold

---

## 8. Infrastructure rules

- `docker restart` does NOT reload env_file → `docker compose up -d`
- `docker commit kommo-agent kommo-agent:latest` before any restart
- infra-mcp drops under load → `docker restart infra-mcp`
- Never push to Vercel manually → GitHub only
- Every Salesbot: empty Triggers panel
- Patches: write to /app/data/ → `docker exec -i kommo-agent python3 < /app/data/patch.py`
- End of every session: commit all files + update CONTEXT-LOG.md

---

## 9. Version status

### v3.0 (live, 2026-08-14) — 46/46 tests passing
- GPT-4.1 (linear decay, 5x rule budget vs GPT-4o)
- Haiku pre-processor (gpt-4o-mini): intent extract + scope classify
- System prompt: 144 lines, GPT-4.1 OpenAI spec, written in Spanish
- Qualification FSM: greeting→need→location→price→deposit→won/handoff
- Oct 1 cost model: $3.89/mo at current volume (low risk)
- Eval suites: 20/20 core + 15/15 Spanish multi-intent + 11/11 Haiku

### Open items (v3.1)
- Wellington_Lider_Foto (85808): verify image in Kommo UI
- IMHOFF lifespan: ask Wellington → KB → re-ingest
- Facebook/Instagram OAuth: re-authorize if delivery errors persist
- Legacy +1 829-566-7542: wind-down pending
- Voice note audit: check all 12 durations manually (VOZ_AGUA_1 = priority)
- Daily conversation-review automation: not built yet
