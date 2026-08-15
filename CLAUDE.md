# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent.
Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-15 — **v3.3 deployed and fully tested.**

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
| Prompt integrity guard | `/app/data/prompt_guard.py` — must return 39/39 before every commit |

---

## 3. Architecture (v3.3)

```
WhatsApp / Instagram / Facebook
  → Kommo webhook → FastAPI main.py (ack 200 in <2s)
  → worker.py (per-talk asyncio.Lock — serializes all messages):
       SCOPE GUARD (L1: patterns, L2: 30-char intent check)
       FLOW LOCK (flow_state SQLite — agua/séptico forever)
       CHANNEL GATE (_is_waba, _is_instagram_comment)
       BLOQUEADO check
       cancel_nudges(lead_id) ← cancels pending nudges on any inbound
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
       BELT-AND-SUSPENDERS: séptico image sentinel fallback
       POST-GEN FILTERS:
         phone number (DR regex 809/829/849 + guards)
         markdown bold (**text** → text)
       PRE-SEND SUPERSESSION CHECK
       send_message → Kommo
       SENTINEL PROCESSING (strip + fire image bots)
       schedule_nudge() if scenario detected or generic fallback
  → main.py _followup_loop() polls scheduled_nudges every 30s
       claim_due_nudges() → 24h window guard → send or expire
```

---

## 4. Key IDs (verified 2026-08-15)

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
| Latest commit | `2ae7abf` (worker.py, 2026-08-15) |

---

## 5. Salesbots (ALL must have empty Triggers panel)

| ID | Name | Fired by |
|---|---|---|
| 55340 | welcome-bot | Engine: first contact |
| 55348 | agua-foto | [[FOTO_AGUA]] |
| 55956 | banco-foto | [[DEPOSITO]] |
| 59058 | Payment-Audio | [[AUDIO_PAGO]] |
| 76624 | septico-ficha-tecnica | [[SEPTICO_FICHA]] |
| 76632 | septico-comparativa | [[SEPTICO_COMPARATIVA]] |
| 76634 | septico-funcionamiento | [[SEPTICO_FUNCIONAMIENTO]] |
| 76646 | septico-ventajas | [[SEPTICO_VENTAJAS]] |
| 85776 | VOZ_AGUA_1 | First water contact (1.5s paced) |
| 85778-85790 | VOZ_AGUA_2-8 | Keywords — LLM BYPASSED |
| 85800 | VOZ_IMHOFF_1 | First séptico contact |
| 85802-85804 | VOZ_IMHOFF_2-3 | Keywords — LLM BYPASSED |
| 85806 | VOZ_IMHOFF_4 | Location/trust keywords |
| 85808 | Wellington_Lider_Foto | After VOZ_IMHOFF_4 |

---

## 6. Control markers

```
[[HANDOFF]]                  → stage move + Sheyla task + note
[[DEPOSITO]]                 → banco-foto + bank text
[[AUDIO_PAGO]]               → Payment-Audio (ETAPA 1 agua only)
[[SECTOR:Provincia|Pueblo]]  → tag contact
[[SEPTICO_COMPARATIVA]]      → image bot 76632
[[SEPTICO_FUNCIONAMIENTO]]   → image bot 76634
[[SEPTICO_FICHA]]            → image bot 76624 (ficha técnica for plumber)
[[SEPTICO_VENTAJAS]]         → image bot 76646 (price objection image)
[[FOTO_AGUA]]                → agua-foto 55348
[[LINDEROS_LISTO]]           → ETAPA 1 deposit
```

**Belt-and-suspenders sentinel fallback (worker.py):** if the model emits prose
describing a séptico image without the marker, engine detects the phrase and injects
the marker before processing. Logged as `SENTINEL_FALLBACK`. One injection per turn.

---

## 7. Nudge system (`scheduled_nudges` table)

Scenario-specific, priority-queued outbox. One active nudge per lead at a time
(enforced by partial unique index). Poller runs every 30s in `main.py`.

**Priority scale:** lower = more important.

| Scenario | Priority | Delay | Message |
|---|---|---|---|
| `bathrooms` | 5 | 15 min | "Quedo atento a tu respuesta para entender sus necesidades. 🙏" |
| `generic` | 9 | 120 min (config) | `followup_nudge` from client.toml |

**Key behaviors:**
- `cancel_nudges(lead_id)` fires on every inbound message — no nudge ever sends after the customer replies.
- `claim_due_nudges()` applies a 24h window guard at fire time — marks `expired` instead of sending if the customer's service window has closed. Critical: service messages become **paid per-message from October 1, 2026**.
- Higher-priority scenario supersedes a pending lower-priority nudge automatically.
- Legacy `followup` table still drained for backward compatibility.

**Adding a new scenario:** one `schedule_nudge()` call in worker.py with scenario name, message, delay, and priority. Architecture handles everything else.

**state.py API:**
- `schedule_nudge(lead_id, talk_id, scenario, message, delay_seconds, priority, last_inbound_at, context_json)`
- `cancel_nudges(lead_id)`
- `claim_due_nudges(now) → [(talk_id, message, scenario)]`

---

## 8. Non-negotiable rules

- Never confirm payment — ask for comprobante + [[HANDOFF]]
- Never guarantee water 100% — "80-90% con el estudio"
- Never give drilling prices in text
- Never repeat audio content in text reply
- Never share phone numbers (regex filter enforced in code)
- Never offer or mention any discount — feature removed entirely
- Unknown answers: honest admit + [[HANDOFF]]
- Always Spanish regardless of customer language
- Max 2 lines per reply, one question, no lists or bold
- Never promise to send an image in text — use the marker, let the engine send it

---

## 9. Infrastructure rules

- `docker restart` does NOT reload env_file → `docker compose up -d`
- `docker commit kommo-agent kommo-agent:latest` before any restart
- infra-mcp drops under load → `docker restart infra-mcp`
- Never push to Vercel manually → GitHub only
- Every Salesbot: empty Triggers panel
- Patches: write to /app/data/ → `docker exec -i kommo-agent python3 < /app/data/patch.py`
- End of every session: commit all + update CONTEXT-LOG.md + update CLAUDE.md
- BEFORE every prompt commit: run integrity guard:
  `docker exec -i kommo-agent python3 < /app/data/prompt_guard.py`
  Must return 39/39 PASS. Block commit if any check fails.

---

## 10. Version status

### v3.3 (live, 2026-08-15)

**Ficha técnica fix (sentinel reliability):**
- system.md step 5 rewritten with verbatim output template + `NUNCA digas que enviarás la ficha sin incluir [[SEPTICO_FICHA]]` rule
- worker.py `_SEPTICO_FALLBACKS` belt-and-suspenders: phrase detection → marker injection before sentinel loop
- Covers: ficha técnica, funcionamiento, ventajas, no se cuartea, no contamina

**Discount removed:**
- All discount logic deleted from worker.py: `_HES_PHRASES`, `_ASK_PHRASES`, discount window calc, `_OFFER_ASK`/`_OFFER_TAIL`, `[[DESC_OFRECIDO]]` stripping
- `[[DESC_OFRECIDO]]` removed from system.md markers
- state.py discount functions left as harmless dead code

**Nudge system re-architected:**
- New `scheduled_nudges` table with priority, scenario, status, 24h window guard
- Partial unique index enforces one-active-nudge-per-lead at DB level
- `schedule_nudge()` / `cancel_nudges()` / `claim_due_nudges()` API
- Legacy `followup` shims kept for backward compatibility
- Bathroom scenario (priority 5, 15 min) migrated to new system
- Poller interval 60s → 30s

### v3.2 (2026-08-14)
- MINITS farewell detection (soft_farewell + hard_no in haiku.py)
- Prompt integrity guard: 39/39

### v3.1 (2026-08-14)
- Per-talk asyncio.Lock, markdown bold strip, DR transcription improvements

---

## 11. Open items

1. Wellington_Lider_Foto (85808): verify image loaded in Kommo UI
2. Voice note duration audit: all 12 bots, target 20-40s, hard cap 60s — VOZ_AGUA_1 (~2 min) priority
3. IMHOFF lifespan: ask Wellington → add to KB → re-ingest Qdrant
4. October 1, 2026: service messages become paid — instrument nudge reply rates before that date
5. Stage 2 re-engagement: 3 templates for Wellington → Meta HSM approval
6. Stage 3: conversation state persistence to Kommo custom fields + opt-in capture at soft_farewell
7. Daily conversation-review automation: not built yet
8. Legacy number +1 829-566-7542: wind-down pending
9. KOMMO repo README: still says "Claude LLM, not deployed" — fix when convenient
