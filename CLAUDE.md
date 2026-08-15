# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent.
Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-14 — **v3.1 deployed and fully tested.**

---

## 1. The one thing to know

FastAPI engine (`kommo-agent`) on VPS. Kommo is only transport + CRM.
Health: `GET https://kommo-agent.goldcoastai.pro/health`
→ `{"ok":true,"subdomain":"aguasprofundas","provider":"openai"}`

---

## 2. Where everything lives

| What | Where |
|---|---|
| Live engine | GitHub `cryptodominicano/KOMMO` (`kommo-agent/`) |
| Build history | `CONTEXT-LOG.md` (repo root) — read first every session |
| Commercial spec | `COMMERCIAL_GRADE_SPEC.md` (repo root) |
| System prompt | `kommo-agent/clients/aguas-profundas/prompts/system.md` |
| Client config | `kommo-agent/clients/aguas-profundas/client.toml` |
| Core eval suite | `/app/data/run_tests3.py` (20/20) |
| Spanish multi-intent | `kommo-agent/scripts/eval_spanish_multi_final.py` (15/15) |
| Transcription | `kommo-agent/app/transcribe.py` |

---

## 3. Architecture (v3.1)

```
WhatsApp / Instagram / Facebook
  → Kommo webhook → FastAPI main.py (ack 200 in <2s)
  → worker.py (per-talk asyncio.Lock — serializes all messages):
       SCOPE GUARD (L1: patterns, L2: 30-char intent check)
       FLOW LOCK (flow_state SQLite — agua/séptico forever)
       CHANNEL GATE (_is_waba, _is_instagram_comment)
       BLOQUEADO check
       MESSAGE TYPE:
         voice → Whisper (DR dialect prompt + VAD + normalization)
         location → 3s delay → flow-aware (agua=linderos, séptico=delivery)
         picture → 30s cooldown → media ack + handoff
         text → continue
       VOICE BOT SELECTION (keyword → fire before LLM)
       HAIKU PRE-PROCESSOR (intent extract + scope classify + DR slang)
       FSM STAGE INJECTION (ESTADO ACTUAL: flujo=X, etapa=Y)
       RAG (Qdrant, top_k=8)
       GPT-4.1 (main model)
       POST-GEN FILTERS:
         phone number (DR regex 809/829/849 + guards)
         markdown bold (**text** → text)
       PRE-SEND SUPERSESSION CHECK
       send_message → Kommo
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
| Main LLM | `gpt-4.1` (forced via model_post_init) |
| Pre-processor | `gpt-4o-mini` via haiku.py |
| Transcription | `gpt-4o-mini-transcribe` + DR dialect prompt |
| Primary WABA | +1 829-558-3119 |
| Latest commit | `91853ef` |

---

## 5. Salesbots (ALL must have empty Triggers panel)

| ID | Name | Fired by |
|---|---|---|
| 55340 | welcome-bot | Engine: first contact |
| 55348 | agua-foto | [[FOTO_AGUA]] |
| 55956 | banco-foto | [[DEPOSITO]] |
| 59058 | Payment-Audio | [[AUDIO_PAGO]] |
| 76624-76646 | septico bots | [[SEPTICO_*]] markers |
| 85776 | VOZ_AGUA_1 | First water contact (1.5s paced) |
| 85778-85790 | VOZ_AGUA_2-8 | Keywords — LLM BYPASSED |
| 85800 | VOZ_IMHOFF_1 | First séptico contact |
| 85802-85804 | VOZ_IMHOFF_2-3 | Keywords — LLM BYPASSED |
| 85806 | VOZ_IMHOFF_4 | Location/trust keywords |
| 85808 | Wellington_Lider_Foto | After VOZ_IMHOFF_4 |

---

## 6. Control markers

[[HANDOFF]] → stage move + Sheyla task + note
[[DEPOSITO]] → banco-foto + bank text
[[AUDIO_PAGO]] → Payment-Audio (ETAPA 1 agua only)
[[SECTOR:Provincia|Pueblo]] → tag contact
[[SEPTICO_COMPARATIVA/FUNCIONAMIENTO/FICHA/VENTAJAS]] → image bots
[[FOTO_AGUA]] → agua-foto
[[LINDEROS_LISTO]] → ETAPA 1 deposit
[[DESC_OFRECIDO]] → log 5% discount

---

## 7. Non-negotiable rules

- Never confirm payment — ask for comprobante + [[HANDOFF]]
- Never guarantee water 100% — "80-90% con el estudio"
- Never give drilling prices in text
- Never repeat audio content in text
- Never share phone numbers (regex filter enforced in code)
- Unknown answers: honest admit + [[HANDOFF]]
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
- End of every session: commit all + update CONTEXT-LOG.md
- BEFORE every prompt commit: run integrity guard:
  docker exec -i kommo-agent python3 < /app/data/prompt_guard.py
  Must return PASS (exit 0). Block commit if any check fails.
  Guard checks 39 research-backed rules across R1-R6 + business rules.

---

## 9. Version status

### v3.1 (live, 2026-08-14) — 46/46 tests passing
All v3.0 features plus:
- Per-talk asyncio.Lock: eliminates double-reply race condition
- Markdown bold post-gen strip: **text** → text before send
- Text+location 3s delay: prevents simultaneous webhook double-reply
- DR transcription: dialect prompt + repetition detection + normalization pass
- Welcome pacing: 1.5s before VOZ_AGUA_1 on first contact
- DR phone regex: 809/829/849 specific + negative lookaheads

### Open items (v3.2)
- Wellington_Lider_Foto (85808): verify image in Kommo UI
- IMHOFF lifespan: ask Wellington → KB → re-ingest
- Facebook/Instagram OAuth: re-authorize if delivery errors persist
- Legacy +1 829-566-7542: wind-down pending
- Voice note audit: check all 12 durations in Kommo UI (Salesbot → message step)
  VOZ_AGUA_1 (~2 min) is priority. Target: 20-40s, hard cap 60s.
- DR WER baseline: run held-out AP voice notes through gpt-4o-mini-transcribe
- Redis debounce: upgrade when volume exceeds 500 concurrent talks/month
- Daily conversation-review automation: not built yet
