# Aguas Profundas RD — CLAUDE.md (Project Context)

Single source-of-truth for the Aguas Profundas WhatsApp AI agent.
Owner: Intelia Automatizaciones / Gold Coast AI Automations (Isaias Perez).
Last updated: 2026-08-15 — **v3.5 deployed — Haiku semantic voice-bot routing.**

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
| System prompt | `kommo-agent/clients/aguas-profundas/prompts/system.md` |
| Client config | `kommo-agent/clients/aguas-profundas/client.toml` |
| Audio workflow | `kommo-agent/docs/AUDIO_WORKFLOW.md` |
| Prompt guard | `/app/data/prompt_guard.py` — must return 39/39 before every commit |

---

## 3. Architecture (v3.5)

```
WhatsApp / Instagram / Facebook
  → Kommo webhook → FastAPI main.py (ack 200 in <2s)
  → worker.py (per-talk asyncio.Lock):
       cancel_nudges(lead_id) ← on every inbound
       MESSAGE TYPE branch (voice/location/picture/text)
       SÉPTICO FIRST CONTACT (if is_first + _septico_first + _is_waba):
         1. SEPTICO_COMPARATIVA image (immediately)
         2. 1s → welcome text "¡Bienvenido! 😊 Con gusto le orientamos..."
         3. 1.5s → VOZ_IMHOFF_1 audio
         4. AUDIO_BYPASS → "¿Cuántos baños tiene su propiedad?"
       AGUA FIRST CONTACT (if is_first + agua + _is_waba):
         1. welcome-bot image 55340
         2. VOZ_AGUA_1 audio
         3. AUDIO_BYPASS → "Por favor mándeme la ubicación..."
       THREE-TIER VOICE BOT ROUTING (subsequent messages, _is_waba only):
         TIER 0 — Keywords (unambiguous only): quiero comprar, cuánto cuesta perforar
           → fire immediately, skip Haiku
         TIER 1 — Haiku semantic routing (nuanced intents):
           Haiku outputs <voz_bots> with intent + confidence
           trust_question, price_objection_*, location_*, call_request,
           payment_*, how_to_start, drilling_price, purchase_process_septico
           → fire if confidence ≥ threshold (0.65-0.70 per intent)
         TIER 2 — Text-only fallback (below threshold): no audio, LLM text only
         Multi-intent: all matched bots fire sequentially, 5s between each
         VOZ_IMHOFF_4 always fires: voice → 2s → Instagram text → 1s → Wellington
       VOZ→IMAGE PAIRS (after each voice bot, 4s delay):
         VOZ_IMHOFF_1 → SEPTICO_COMPARATIVA, VOZ_IMHOFF_2 → FUNCIONAMIENTO,
         VOZ_IMHOFF_3 → VENTAJAS
       AUDIO_BYPASS: warm rotating closer per bot (no audio reference)
       STATE BLOCK injection (coverage ledger → LLM context)
       HAIKU PRE-PROCESSOR → GPT-4.1 → BELT-AND-SUSPENDERS sentinel fallback
       POST-GEN FILTERS → send_message → SENTINEL LOOP → schedule_nudge
  → main.py _followup_loop() polls scheduled_nudges every 30s
```

---

## 4. Key IDs (verified 2026-08-15)

| Item | Value |
|---|---|
| Kommo subdomain | `aguasprofundas` |
| Pipeline ID | `14130431` |
| Handoff stage | `Atención humana` / `109168423` |
| Sheyla user_id | `15589135` |
| Qdrant collection | `aguas_profundas_kb` (1536-dim, 48 points) |
| Main LLM | `gpt-4.1` (forced via model_post_init) |
| Pre-processor | `gpt-4o-mini` via haiku.py |
| Transcription | `gpt-4o-mini-transcribe` |

---

## 5. Salesbots (ALL must have empty Triggers panel)

| ID | Name | Fired by |
|---|---|---|
| 55340 | welcome-bot | Engine: agua first contact only (not séptico-first) |
| 55348 | agua-foto | [[FOTO_AGUA]] |
| 55956 | banco-foto | [[DEPOSITO]] |
| 59058 | Payment-Audio | [[AUDIO_PAGO]] |
| 76624 | septico-ficha-tecnica | [[SEPTICO_FICHA]] |
| 76632 | septico-comparativa | Séptico first-contact sequence + VOZ_IMHOFF_1 pair |
| 76634 | septico-funcionamiento | [[SEPTICO_FUNCIONAMIENTO]] + VOZ_IMHOFF_2 pair |
| 76646 | septico-ventajas | [[SEPTICO_VENTAJAS]] + VOZ_IMHOFF_3 pair |
| 85776 | VOZ_AGUA_1 | Engine: agua first contact |
| 85778 | VOZ_AGUA_2 | Drilling price keywords |
| 85780 | VOZ_AGUA_3 | Start process keywords |
| 85782 | VOZ_AGUA_4 | Payment/deposit keywords |
| 85784 | VOZ_AGUA_5 | Price objection keywords (agua) |
| 85786 | VOZ_AGUA_7 | Payment conditions keywords |
| 85788 | VOZ_AGUA_6 | Office location keywords (agua + séptico) |
| 85790 | VOZ_AGUA_8 | Call request keywords |
| 85800 | VOZ_IMHOFF_1 | Engine: séptico first contact |
| 85802 | VOZ_IMHOFF_2 | Purchase process keywords |
| 85804 | VOZ_IMHOFF_4 | Trust/credibility keywords (verified 2026-08-15) |
| 85806 | VOZ_IMHOFF_3 | Price objection keywords (verified 2026-08-15) |
| 85808 | Wellington_Lider_Foto | After VOZ_IMHOFF_4 sequence |

⚠️ NOTE: Bot IDs 85804 and 85806 were swapped on 2026-08-15 after live audio verification. 85804 = trust audio, 85806 = price objection audio. client.toml reflects the correct mapping.

---

## 6. Audio keyword routing (séptico flow)

| Bot sentinel | Keywords (sample) | Paired image (4s after) |
|---|---|---|
| VOZ_IMHOFF_2 | quiero comprar, cuál es el proceso, cómo procedo, cuánto tarda | SEPTICO_FUNCIONAMIENTO |
| VOZ_IMHOFF_3 | está muy cara, la competencia, fuera de mi presupuesto, lo voy a pensar | SEPTICO_VENTAJAS |
| VOZ_AGUA_6 | dónde están ubicados, tienen oficina, dónde queda | None |
| VOZ_IMHOFF_4 | cómo sé que son confiables, no confío en transferir, son una empresa real, registro mercantil | Wellington photo (sequence) |

Multi-intent: all matched bots fire sequentially with 5s pauses. Last fired drives followup text.

---

## 7. Audio followup text pattern

```
VOZ_AGUA_1:    "Por favor mándeme la ubicación..." (no opener — first contact)
VOZ_IMHOFF_1:  "¿Cuántos baños tiene su propiedad?" (no opener — first contact)
VOZ_IMHOFF_4:  "" (Instagram text + Wellington photo handle the close)
All others:    "Luego de escuchar la nota de voz, con gusto le atiendo. 😊 " + qualifying question
```

---

## 8. Séptico image sentinel fallback (belt-and-suspenders)

If the LLM describes sending a séptico image in text WITHOUT emitting the marker, the engine detects the phrase and injects the marker before the sentinel loop. Logged as `SENTINEL_FALLBACK`.

Covered phrases: "ficha técnica", "funcionamiento", "ventajas", "no se cuartea", "no contamina".

---

## 9. Nudge system (`scheduled_nudges` table)

| Scenario | Priority | Delay | Message |
|---|---|---|---|
| bathrooms | 5 | 15 min | "Quedo atento a tu respuesta para entender sus necesidades. 🙏" |
| generic | 9 | 120 min | `followup_nudge` from client.toml |

One active nudge per lead (partial unique index). 24h window guard at fire time. October 1, 2026: service messages become paid.

---

## 10. Identity and AI disclosure

Never volunteer name or AI status. Respond as the Aguas Profundas team.

**Disclosure triggers** (direct AI/human questions only):
"eres un bot", "eres IA", "eres humano", "eres una persona", "con quién hablo", "estoy hablando con una máquina", "cómo sé que son confiables" (re: AI), "son confiables", "me responde una máquina", "es esto automático", "eres real", "hay una persona real ahí", "quién me está respondiendo"

**NOT triggers** (just checking responsiveness):
"estás ahí", "hay alguien ahí", "hola", follow-ups after silence.

Response: "Soy Isla, asistente virtual de Aguas Profundas. 😊 El equipo humano también está disponible — ¿le conecto con alguien? [[HANDOFF]]"

---

## 11. Non-negotiable rules

- Never confirm payment — receipt → acknowledge + [[HANDOFF]]
- Never guarantee water 100% — "80-90% con el estudio"
- Never give drilling prices in text (VOZ_AGUA_2 handles)
- Never repeat audio content in text reply
- Never share phone numbers
- Never offer or mention any discount — removed entirely
- Never volunteer name or AI status
- Max 2 lines per reply, one question, no lists or bold

---

## 12. Infrastructure rules

- `docker restart` does NOT reload env_file → `docker compose up -d`
- `docker commit kommo-agent kommo-agent:latest` before any restart
- infra-mcp drops under load → `docker restart infra-mcp`
- Patches: write to /app/data/ → `docker exec -i kommo-agent python3 < /app/data/patch.py`
- After ANY patch: commit container → restart → verify logs → push to GitHub
- Never push to Vercel manually
- Every Salesbot: empty Triggers panel (Kommo defaults to "Any new conversation" — always delete)
- Prompt guard: `docker exec -i kommo-agent python3 < /app/data/prompt_guard.py` → must be 39/39

---

## 13. Version history

### v3.5 (live, 2026-08-15)
- Haiku semantic voice-bot routing — replaces keyword lists for all nuanced intents
- Three-tier hybrid: keywords (unambiguous) → Haiku semantic (nuanced) → text-only fallback
- 11 voice-bot intent labels: trust_question, price_objection_*, location_*, payment_*,
  drilling_price, how_to_start, purchase_process_septico, call_request
- Multi-intent semantic: compound messages fire all matching bots sequentially
- Coverage ledger writes on Haiku-routed audios (same as keyword-routed)
- Warm rotating closers per bot (removed "Luego de escuchar la nota de voz" opener)
- Anti-repetition coverage ledger (covered_topics SQLite + STATE BLOCK injection)
- Ambiguous "Sí" service selection handled with contextual descriptions
- Generic greeting improved with emoji service options
- VOZ_IMHOFF_4 trust keywords expanded
- API validation: 25/27 test cases correct including DR slang ("ta muy cara esa vaina")

### v3.4 (live, 2026-08-15)
- Séptico workflow validated end-to-end: all 7 scenarios passing
- Bot IDs 85804↔85806 swapped after live audio verification
- Séptico first-contact sequence: SEPTICO_COMPARATIVA → welcome text → VOZ_IMHOFF_1 → bathroom Q
- Agua welcome image skipped for séptico-first contacts
- VOZ→IMAGE pairs: IMHOFF_2→funcionamiento, IMHOFF_3→ventajas (4s delay each)
- Multi-audio: both keyword loops collect all matches, fire sequentially with 5s pauses
- VOZ_IMHOFF_4 keywords split: location → VOZ_AGUA_6, trust → VOZ_IMHOFF_4
- Cultural opener "Luego de escuchar la nota de voz..." on all non-first-contact followups
- AI disclosure expanded with explicit trigger/non-trigger lists (Meta Jan 2026 compliance)
- Identity: never volunteer name or AI; respond as the team
- Discount feature removed entirely
- scheduled_nudges outbox architecture with priority queue and 24h window guard
- advance_stage Connection.rowcount → cursor.rowcount bug fixed
- client_pack.salesbot() → pack().get("salesbot") bug fixed
- Nudge scheduling block wrapped in try/except (never kills sentinel loop)
- Belt-and-suspenders séptico image fallback for ficha/funcionamiento/ventajas phrases

### v3.3 (2026-08-15 morning)
- Ficha técnica image fix (sentinel + belt-and-suspenders)
- Discount removed
- Bathroom nudge (15 min, scenario-specific)

### v3.2 (2026-08-14)
- MINITS farewell detection, prompt integrity guard 39/39

---

## 14. Open items

**Must fix before production traffic:**
1. SEPTICO_VENTAJAS image (bot 76646): has legacy number 829-566-7542 — replace in Kommo Salesbot UI

**Next session:**
2. Agua flow end-to-end test (séptico fully validated; agua not yet)
3. Live test Haiku semantic routing with real WhatsApp conversations
4. Weekly threshold tuning: sample 100 conversations, measure false-audio rate
5. Coverage ledger Stage 2: add `mark_topic_covered` for text-delivered topics
6. Facebook ad CTWA prefill: configure per campaign in Meta Business Suite
7. Voice note duration audit: all 12 bots, target 20-40s — VOZ_AGUA_1 at 1:38 is over
8. IMHOFF lifespan: ask Wellington → KB → re-ingest Qdrant
9. October 1, 2026 (47 days): service messages become paid — instrument nudge reply rates
10. Daily conversation-review automation: not built
11. Legacy number +1 829-566-7542: wind-down pending
12. Edge case: "puedo ir a verlos personalmente" returns NONE — add to few-shot examples
