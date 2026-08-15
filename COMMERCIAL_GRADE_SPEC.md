# Gold Coast AI Automations — WhatsApp AI Sales Agent
## Commercial-Grade Build Specification v1.0
### Authored: August 14, 2026 | Isaias Perez, Gold Coast AI Automations / Intelia Automatizaciones

---

## 1. Purpose of This Document

This is the master reference for building a commercial-grade WhatsApp AI sales agent
on the Kommo + FastAPI + OpenAI stack. It is derived from:
- The live Aguas Profundas RD build (August 2026)
- 5 peer-reviewed and production research documents synthesized August 14, 2026
- Lessons learned from 20+ real conversations in production

Every future client agent should be built against this specification. Deviation from
any section should be documented and justified.

---

## 2. Architecture Overview

```
WhatsApp / Instagram / Facebook
  → Kommo (BSP transport + CRM)
  → Webhook POST /webhook/kommo/{secret}
  → FastAPI main.py (validate, dedupe, ack 200 in <2s, enqueue)
  → worker.py background task:
      1. SCOPE GUARD (layer 1: pattern, layer 2: intent check)
      2. FLOW LOCK (read/set flow_state DB)
      3. CHANNEL GATE (_is_waba, _is_instagram_comment)
      4. BLOQUEADO check (lead + contact tags)
      5. MESSAGE TYPE ROUTING:
         voice → Whisper transcription
         location → flow-aware routing (agua=linderos, séptico=delivery)
         picture/file → media ack + handoff
         text → continue
      6. VOICE BOT SELECTION (keyword match → fire before LLM)
      7. [PLANNED] HAIKU PRE-PROCESSOR (intent extract + scope classify)
      8. RAG (Qdrant top_k=8)
      9. LLM (GPT-4.1 [planned] / GPT-4o [current])
      10. POST-GENERATION FILTERS (phone number regex)
      11. PRE-SEND SUPERSESSION CHECK
      12. send_message → Kommo Chats API
      13. SENTINEL PROCESSING (fire bots, handoff, tag contact)
  → Kommo CRM (lead stage, task, note, contact tags)
```

---

## 3. Research Foundation (All 5 Documents, August 14, 2026)

### R1 — Multi-Intent Message Handling
Source: University of Tokyo "Curse of Instructions" + Rasa, Oracle, LangChain,
respond.io production patterns.

Key finding: Intent drop is mathematical. At n=2 questions, success rate =
(per-instruction rate)^2. At 90% per-instruction rate, only 81% chance both
answered. This is structural, not fixable with prompt wording alone.

Production fix: two-stage pattern.
Stage 1 — Haiku extracts intents as JSON array.
Stage 2 — Main model contractually bound to answer ALL items in array.
Coverage validator: cheap Haiku check after main model replies.
Spanish degrades faster than English — build Spanish-specific eval suite.

Current implementation: single-call fallback (prompt rule). Functional but
not production-grade. Haiku pre-processor is the planned upgrade.

### R2 — Flow Locking & Context Drift Prevention
Source: Laban et al. 2025, Netflix "Beyond Continuity", Liu et al. "Lost in
the Middle", Rasa/BotFramework/Voiceflow/LangGraph production patterns.

Key finding: RLHF makes models answer whatever is asked. Water is semantically
adjacent to séptico — model treats it as in-domain, not a scope violation.
Once drift starts, models don't self-correct (39% avg performance drop,
112% increase in unreliability). Positional attention = rules at top of long
conversation lose weight vs. most recent user message.

Production fix: deterministic FSM in FastAPI owns state. Prompt renders state,
never owns it. Haiku scope-classifier labels each intent:
  - qualification_answer
  - in_scope_question
  - adjacent_out_of_scope
  - fully_off_topic
FSM ignores adjacent_out_of_scope for state transitions. One-turn
acknowledge-and-redirect, then return to active flow.

Current implementation: flow_state SQLite table (agua/séptico only).
_is_septico_flow injected per turn. Prompt rule for redirect.
Gap: no qualification stage tracking, no Haiku classifier, state injection
at top only (not repeated at bottom).

### R3 — GPT-4o Prompt Compliance
Source: IFScale benchmark (Jaroslawicz et al., arXiv:2507.11538, July 2025),
OpenAI GPT-4.1 Prompting Guide, AgentIF benchmark.

Key finding: GPT-4o has EXPONENTIAL decay.
- 10 rules: 94% compliance
- 50 rules: 82.8% compliance
- 100 rules: 49% compliance ← our old prompt was here
- 500 rules: 15% compliance

GPT-4.1 has LINEAR decay — roughly 5x the safe rule budget.
GPT-4o is being retired from ChatGPT (API still works but plan migration).

What makes rules stick on GPT-4o/4.1:
- Markdown section headers (not XML — that's Claude's preference)
- Rules at TOP AND BOTTOM of prompt (positional attention fix)
- Numbered/ranked rules, most critical first
- Positive framing ("answer in 1-2 sentences" not "don't be long")
- Conditional rules fail most — keep triggers few and unambiguous
- ONE worked example demonstrating ALL rules simultaneously
- Output format via json_schema, not prose
- Max 20-40 hard rules for GPT-4o, up to 100 for GPT-4.1

OpenAI recommended structure:
  # Role & Objective
  # Priority Rules (ranked)
  # Steps
  # Output Format
  # Examples (one example showing all rules)
  # Final Reminder (repeat top 2-3 non-negotiables)

Current implementation: 169-line prompt, Markdown sections. Better than old
222-line prompt but not yet restructured to OpenAI spec. Rules not at bottom.
No worked example. Still on GPT-4o.

### R4 — WhatsApp Business API DR/Caribbean 2026
Source: Meta official docs, DataReportal Digital 2025, Kommo BSP docs,
Sensor Tower Q2 2025.

Key findings:
- DR has 7.8M weekly WhatsApp users, 88.6% internet penetration
- Per-message billing since July 1, 2025 (replaces per-conversation)
- Service messages FREE until October 1, 2026 — then billable
- CTWA 72-hour free window: reply within 30s, close within 72h
- Quality rating is PORTFOLIO-LEVEL since Oct 7, 2025
- Meta AI ban Jan 15, 2026: general-purpose bots banned, task-specific permitted
- Ice Breakers: max 4, max 80 chars, NO emojis (Meta spec)
- DR +1-809/829/849 numbers supported for WABA

CRITICAL: October 1, 2026 is 47 days away. Every agent reply becomes
billable. Build cost models NOW for all active clients.

Current implementation: CTWA ads running with correct Messages objective.
Agent replies instantly (compliant). Ice Breakers have emojis (check if
Kommo strips or if this causes issues). Oct 1 cost model not built yet.

### R5 — Audio-First in LatAm 2026
Source: Meta CEO at Conversations São Paulo 2024, Opinion Box Brazil 2024,
YouGov Nov 2023, OmniChat Chat Commerce Report 2025, Nature Scientific
Reports 2025.

Key findings:
- 80% of Brazilian WhatsApp users send audio. Mexico #2 globally.
- No controlled A/B test proves voice converts better than text — every
  specific percentage is vendor marketing without methodology.
- Voice's advantage is psychological: warmth, competence, trust within
  milliseconds. Most valuable at objection handling, price, and close.
- Human voice: correct. AI voice: trust collapses when detected as synthetic.
  77% of consumers trust human voices most.
- Sweet spot: 10-30 seconds. Tolerance drops sharply past 60 seconds.
- VOZ_AGUA_1 at 2 minutes is 4x the recommended maximum.
- Architecture: audio-first, text-forward. Voice at friction moments.
  Text for prices, links, CTAs, confirmations.

Current implementation: 12 human voice notes (Wellington). Audio-first
architecture is culturally correct. Voice note lengths not audited against
10-30s recommendation. VOZ_AGUA_1 (2 min) likely too long.

---

## 4. What's Built and Verified in Production (v2.0, August 14, 2026)

### Engine (worker.py)
- Flow locking: agua/séptico locked on first message, persists for conversation
- Audio bypass: VOZ_AGUA_2-8 and VOZ_IMHOFF_2-3 skip LLM entirely
- PREVIO_BYPASS: after any audio, LLM gets brevity constraint
- any_voice_sent(): suppresses study explanation when any audio played
- Pre-send supersession: second is_latest_inbound check before send
- Channel gating: voice bots on WhatsApp only, text on Instagram/Facebook
- Phone number filter: prompt rule + post-generation regex
- Media cooldown: 30s window prevents duplicate acks on simultaneous images
- Instagram comment guard: @ prefix = silent exit
- Scope guard: Layer 1 (religious/broadcast patterns) + Layer 2 (intent check,
  30-char threshold, no business keywords, no question mark)
- Get Started routing: Facebook button → generic greeting flow
- Unknown answer escalation: honest admit + [[HANDOFF]]
- Location routing: agua=linderos flow, séptico=delivery handoff
- Multi-intent sequential delivery: 3-5s pauses between bots
- BLOQUEADO system: checks lead AND contact tags before any processing
- Debounce: 4-9s length-scaled delay
- Webhook dedupe: atomic INSERT-then-catch
- Whisper transcription: magic byte sniff (Kommo re-encodes to M4A)
- Handoff: stage move (109168423) + Sheyla task (2h SLA) + internal note
- Lead name auto-update: "WhatsApp - Pueblo, Provincia"
- Contact tagging: [[SECTOR:Provincia|Pueblo]] marker
- Followup nudge: 2 hours, warm DR closing message
- Graceful handoff resume: reads message history for internal author
- NO_REACTIVAR tag: permanent bot silence on tagged leads

### State (state.py)
Tables: greeted, voice_sent, flow_state, flow_confirmed, handoff_state,
deposit_cooldown, linderos_state, media_ack

Functions: first_contact(), voice_already_sent(), mark_voice_sent(),
any_voice_sent(), get_flow(), set_flow(), is_flow_confirmed(),
mark_flow_confirmed(), media_ack_on_cooldown(), clear_media_ack(),
is_handed_off(), mark_handoff(), should_notify(), is_latest_inbound()

### System Prompt (v2.0, 169 lines)
Audio-first architecture. LLM does two things: answer KB questions, advance
to next step. All flow detection removed (engine handles via DB). All audio
content listed as reference (LLM never repeats it). Brevity: 2 lines max.
20 test cases passing on GPT-4o.

### Salesbots (all must have empty Triggers panel in Kommo)
| ID    | Name                  | Fired by                          |
|-------|-----------------------|-----------------------------------|
| 55340 | welcome-bot           | Engine: first contact             |
| 55348 | agua-foto             | [[FOTO_AGUA]]                     |
| 55956 | banco-foto            | [[DEPOSITO]] marker               |
| 59058 | Payment-Audio         | [[AUDIO_PAGO]] marker             |
| 76624 | septico-ficha-tecnica | [[SEPTICO_FICHA]]                 |
| 76632 | septico-comparativa   | [[SEPTICO_COMPARATIVA]]           |
| 76634 | septico-funcionamiento| [[SEPTICO_FUNCIONAMIENTO]]        |
| 76646 | septico-ventajas      | [[SEPTICO_VENTAJAS]]              |
| 85776 | VOZ_AGUA_1            | First water contact (WhatsApp)    |
| 85778 | VOZ_AGUA_2            | Drilling price keywords           |
| 85780 | VOZ_AGUA_3            | Start process keywords            |
| 85782 | VOZ_AGUA_4            | Payment/deposit keywords          |
| 85784 | VOZ_AGUA_5            | Price objection keywords          |
| 85786 | VOZ_AGUA_7            | Payment conditions keywords       |
| 85788 | VOZ_AGUA_6            | Office location keywords          |
| 85790 | VOZ_AGUA_8            | Call request keywords             |
| 85800 | VOZ_IMHOFF_1          | First séptico contact (WhatsApp)  |
| 85802 | VOZ_IMHOFF_2          | Purchase process keywords         |
| 85804 | VOZ_IMHOFF_3          | Séptico price objection           |
| 85806 | VOZ_IMHOFF_4          | Location/trust keywords           |
| 85808 | Wellington_Lider_Foto | After VOZ_IMHOFF_4 sequence       |

### Meta / Kommo Configuration
- Instagram Ice Breakers (Kommo): 💧 Estudio de agua y perforación /
  🪣 Planta séptica IMHOFF
- Facebook Messenger FAQ buttons (Meta Business Suite): same two options
- Generate leads from comments: OFF (Instagram + Facebook)
- Away message: OFF (24/7 AI agent)
- CTWA ad pre-filled messages: include service keyword (agua or IMHOFF)

### KB (Qdrant aguas_profundas_kb, 48 points, 1536-dim Cosine)
4 files: estudio de agua, perforación de pozos, séptico IMHOFF,
contacto/precios/proceso. Drilling prices removed (VOZ_AGUA_2 handles).
IMHOFF lifespan NOT in KB — escalates to human when asked.

---

## 5. Planned Upgrades (v3.0 — Research-Backed)

### P1 — Model: GPT-4o → GPT-4.1 (HIGH PRIORITY)
Why: GPT-4o exponential decay hits 49% compliance at 100 rules.
GPT-4.1 linear decay = 5x safe rule budget. Our conditional rules
(objection handling, escalation) are exactly what GPT-4o drops first.
How: change model string in agent.py from "gpt-4o" to "gpt-4.1".
Test: run 20-case eval suite before and after, compare pass rates.
Prerequisite: confirm GPT-4.1 Spanish instruction-following data.

### P2 — Haiku Pre-Processor (HIGH PRIORITY)
Why: fixes Test 10 (multi-intent drop) and Test 20 (context drift)
at the architectural level. Both Research 1 and 2 independently
recommend the same solution.
What: single Haiku call before main model that returns:
{
  "intents": [
    {"id": 1, "text": "...", "scope": "in_scope_septic_question"},
    {"id": 2, "text": "...", "scope": "adjacent_out_of_scope"}
  ]
}
Scope categories: qualification_answer, in_scope_agua_question,
in_scope_septic_question, adjacent_out_of_scope, fully_off_topic
Main model receives: "El cliente hizo estas preguntas (responde TODAS):
1. ... 2. ..." and must verify coverage before sending.
Latency: adds ~300-700ms — acceptable for WhatsApp conversational rhythm.
Coverage validator: second Haiku call checks reply covers all intents.

### P3 — System Prompt Restructure for GPT-4.1 (MEDIUM PRIORITY)
Why: current prompt not structured per OpenAI GPT-4.1 spec.
Rules not repeated at bottom. No worked example. Some rules use
negative framing. Conditional rules not anchored with examples.
Structure to implement:
  # Role & Objective (2-3 lines)
  # Priority Rules (numbered, ranked, max 20-40, positive framing)
  # Steps (ordered flow for agua and séptico)
  # Output Format (json_schema if using structured outputs)
  # Examples (ONE example showing ALL priority rules in action)
  # Final Reminder (top 3 non-negotiables repeated verbatim)

### P4 — Qualification FSM Stages (MEDIUM PRIORITY)
Why: current FSM only tracks agua/séptico. Research 2 recommends
full qualification stage tracking for trajectory monitoring and
drift detection.
Stages to implement:
  greeting → need_discovery → location_captured → price_presented
  → deposit_requested → deposit_confirmed → won/handoff
Each stage stored in flow_state table alongside flow type.
Stage injected into every LLM call as current_stage.

### P5 — October 1 Cost Model (URGENT — 47 days)
Why: service messages become billable Oct 1, 2026.
Every agent reply currently free. After Oct 1, each reply costs money.
Action: pull Kommo/Meta rate card for DR service messages.
Build spreadsheet: avg replies per conversation × avg conversations per
month × per-message rate = monthly cost.
Present to Wellington before October 1.

### P6 — Voice Note Length Audit (LOW PRIORITY)
Why: Research 5 says 10-30s sweet spot. VOZ_AGUA_1 is 2 minutes.
Action: measure all 12 voice note durations. Flag any over 60 seconds.
Ask Wellington to record shorter versions for flagged bots.

### P7 — Spanish Multi-Intent Eval Suite (MEDIUM PRIORITY)
Why: Research 1 notes Spanish instruction-following degrades faster
than English. Our 20-case eval suite is in Spanish but needs more
multi-intent cases specifically.
Add: 10-15 Spanish test cases with 2, 3, and 4 simultaneous questions.
Run before every prompt or model change.

---

## 6. Rules for Every Future Client Build

### Infrastructure
- FastAPI on VPS, Docker, Traefik for SSL
- Webhook returns 200 in <2s, processes in background
- SQLite for conversation state (upgrade to Redis at scale)
- Qdrant for KB vector search, 1536-dim, Cosine
- Deduplicate by message ID (atomic INSERT-then-catch)
- Debounce 4-9s length-scaled before LLM call

### Kommo Setup
- Every Salesbot: EMPTY Triggers panel (Kommo defaults to
  "Any new conversation" — always delete immediately)
- Pipeline: create "Atención humana" stage for handoff target
- Handoff: move to stage + create task for responsible user + internal note
- Contact tags for geographic/audience segmentation
- Lead name auto-update with location when captured

### System Prompt Rules
- Max 20-40 hard rules for GPT-4o, up to 100 for GPT-4.1
- OpenAI Markdown structure (not XML — XML is Claude's preference)
- Rules at top AND bottom (positional attention)
- Positive framing everywhere except privacy/escalation
- One worked example demonstrating all rules simultaneously
- Conditional rules (if X then Y): keep triggers few and unambiguous
- Never rely on prompt alone for critical rules — enforce in code

### Code Guardrails (never prompt-only)
- Phone number filter: post-generation regex
- Payment confirmation: deterministic code, never LLM decision
- Handoff enforcement: code state machine, not prompt instruction
- Flow locking: DB table, not re-detection per message
- Deposit cap: cooldown in state, not prompt rule

### Voice Note Architecture
- Human voice only (never AI voice for relationship moments)
- 10-30 seconds per note (flag and replace anything over 60s)
- Audio-first at: process explanation, price objection, close, location/trust
- Text-forward for: prices, links, CTAs, confirmations, deposit data
- LLM bypass after audio: hardcoded follow-up only, no LLM
- No-repeat per conversation: voice_sent SQLite table

### Channel Behavior
- WhatsApp: full flow (audio + text + images)
- Instagram: text only, no audio (Meta API restriction)
- Facebook: text only, no audio
- Instagram comments (@prefix): silent exit, no reply attempt
- Get Started button: route as generic greeting, never drop silently

### Meta Compliance (2026)
- Task-specific scope only — never open-domain
- AI disclosure when directly asked
- Ice Breakers: max 4, max 80 chars, no emojis
- Explicit opt-in for all contacts
- Quality rating monitoring (portfolio-level since Oct 2025)
- CTWA ads: Messages objective for 72-hour free window
- October 1, 2026: service messages become billable — build cost model

---

## 7. What Still Needs Research Before v3.0

### Gap 1 — GPT-4.1 Spanish instruction-following
Does GPT-4.1's linear decay hold in Spanish? Non-English instruction-
following degrades faster (Research 1 + 3). Need Spanish-specific
compliance data before committing to the upgrade.

### Gap 2 — Haiku classifier prompt for Spanish/DR
Both Research 1 and 2 recommend the Haiku pre-processor but neither
provides a tested Spanish-language classifier prompt with DR-specific
vocabulary seeding. This is an implementation task, not a research gap,
but needs careful design and testing before production deployment.

### Gap 3 — Oct 1, 2026 per-message rate for DR
Need the actual Kommo/Meta rate card for DR service messages post-Oct 1.
Not a research gap — lookup task. Pull from Kommo partner portal or
Meta Business Manager before September 15.

### Gap 4 — Voice note length audit
Check all 12 bot durations. Ask Wellington for shorter recordings if any
exceed 60 seconds. VOZ_AGUA_1 at 2 minutes is the highest priority.

---

## 8. Infrastructure Reference (Aguas Profundas Specific)

| Item | Value |
|------|-------|
| VPS | srv1175204.hstgr.cloud (Hostinger) |
| Container | kommo-agent |
| Service URL | https://kommo-agent.goldcoastai.pro |
| GitHub | cryptodominicano/KOMMO, branch main |
| Kommo subdomain | aguasprofundas |
| Pipeline ID | 14130431 |
| Handoff stage | Atención humana / 109168423 |
| Isaias user_id | 15588735 |
| Sheyla user_id | 15589135 |
| Webhook ID | 47409015 |
| Qdrant collection | aguas_profundas_kb |
| Primary WABA | +1 829-558-3119 |
| LLM | gpt-4o (upgrade to gpt-4.1 planned) |
| Transcription | gpt-4o-mini-transcribe (Whisper) |

---

## 9. Critical Infrastructure Rules (Never Break)

- docker restart does NOT reload env_file → use docker compose up -d
- docker commit kommo-agent kommo-agent:latest before any restart
- infra-mcp drops under load → docker restart infra-mcp from VPS SSH
- Never push to Vercel manually → push to GitHub only
- Every Salesbot must have empty Triggers panel in Kommo UI
- Never modify files in /home/node/.n8n/ or Docker volumes while N8N running
- Multi-line Python patches: write to /app/data/ then execute with
  docker exec -i kommo-agent python3 < /app/data/patch.py
- All changes commit to cryptodominicano/KOMMO and push to main after session
- Update CONTEXT-LOG.md with dated session entry at end of every session

---

## 10. Research Gap Closure (Document 6, August 14, 2026)

### Gap 1 — Spanish instruction-following confirmed safe for GPT-4.1
Spanish is one of OpenAI's strongest non-English languages (Multi-IF, M-IFEval).
GPT-4o Spanish 89.8 vs English 88.6 on strict instruction-following.
Languages that collapse: non-Latin scripts only (Japanese -18.2 vs English).
GPT-4.1 scores 10.5% better than GPT-4o on multi-turn benchmarks.
Rule: write the system prompt IN Spanish, not in English about Spanish.
Re-inject critical rules every 6-8 turns on long conversations.

### Gap 2 — Haiku 4.5 classifier: confirmed architecture and DR vocabulary
Temperature 0, XML tags (<razonamiento>, <categoria>), prompt caching.
Cache: category taxonomy + 5-10 few-shot examples + DR slang glossary.
DR slang glossary (required in every DR deployment):
  "ta to"/"tá to" → greeting/confirmation (NOT complaint)
  "¿a cómo?"/"en cuánto" → price question
  "dímelo"/"¿qué lo que?" → greeting
  "esa vaina no sirve" → complaint
  "un chin" → a little bit
  "jevi" → cool/OK
  "dique" → allegedly/supposedly
  "por fa" → please
  "tíguere" → tone-dependent (compliment or insult)
Simple greetings: Haiku replies directly, skip main model call.
Adjacent out-of-scope: Haiku flags, main model does one-turn redirect.

### Gap 3 — DR pricing confirmed (Rest of Latin America rates)
DR bills at Rest of Latin America despite +1 country code.
Marketing: $0.086/msg | Utility: $0.014/msg | Service: FREE until Oct 1, 2026
Kommo: NO per-message markup (confirmed from official pricing page).
Post-Oct-1 cost model: service replies × $0.014 each.
CRITICAL: high-volume agents can see significant post-Oct-1 cost increases.
Minimize unnecessary replies BEFORE Oct 1. Haiku direct handling of simple
messages reduces main model calls and reply volume simultaneously.

### Gap 4 — Voice note length: 20-40s target, 60s hard cap
Break content into 2-3 sequential notes (one idea per note).
Lead with core intent in first 3-5 seconds.
Always pair with text CTA (accessibility + skimmability).
Any note over 60s must be re-recorded before production.
Rule for all future client builds: audit ALL voice notes at build time.

---

## 11. v3.0 Implementation Status (Completed August 14, 2026)

All 7 planned upgrades deployed. Commit c6e200f on cryptodominicano/KOMMO main.

### P1 — GPT-4.1 ✅
config.py model_post_init forces gpt-4.1 regardless of env var.
Verified live. 20/20 core eval pass.

### P2 — Haiku pre-processor ✅
app/haiku.py — gpt-4o-mini, temperature 0, XML output.
DR slang glossary embedded. Multi-intent + adjacent scope wired.
11/11 classifier tests. Fail-open on error.

### P3 — System prompt GPT-4.1 spec ✅
144 lines, 8 priority rules, written in Spanish.
Sandwich structure (rules at top + bottom). One worked example.
20/20 core eval + 15/15 Spanish multi-intent eval pass.

### P4 — Qualification FSM stages ✅
greeting → need_identified → location → price → deposit_requested
→ deposit_confirmed → won | handoff
Stage injected into every LLM call. Transitions logged.

### P5 — Oct 1 cost model ✅
$3.89/mo at current volume (278 replies/month).
Low risk. Re-assess when ads scale to 500+ talks/month.
DR rate: $0.014/msg (Rest of LatAm, confirmed).
Kommo: no per-message markup.

### P6 — Voice note audit ✅ (action pending with Wellington)
API doesn't expose durations. Manual check required in Kommo UI.
Target: 20-40s. Hard cap: 60s. VOZ_AGUA_1 (2 min) = priority re-record.

### P7 — Spanish multi-intent eval suite ✅
15/15 pass. File: kommo-agent/scripts/eval_spanish_multi_final.py
Covers 2Q, 3Q, and DR slang scenarios. Run before every prompt change.

### Eval totals (v3.0)
Core 20-test: 20/20 | Spanish multi: 15/15 | Haiku classifier: 11/11
Total: 46/46

### For future client builds
The v3.0 architecture is the commercial-grade standard:
GPT-4.1 + Haiku pre-processor + FSM stages + OpenAI GPT-4.1 prompt spec
+ eval suite before every change. See sections 4-7 for implementation rules.

---

## 12. Transcription Standards (Research-Backed, August 2026)

Source: Nacimiento-García, Díaz-Kaas-Nielsen & González-González, Applied Sciences
2024, 14(11), 4734. Caribbean Spanish (incl. DR) is the worst-recognized accent
group for Whisper Large-v2 (~4-8pp WER penalty vs best accent on clean speech).
Conversational WhatsApp voice notes will be worse — no public DR-specific WER exists.

### For every future client in DR/Caribbean:

**Transcription prompt structure (end-weighted per OpenAI cookbook):**
1. Dialect style sentence first (sets register)
2. Local slang glossary (lexical biasing for high-miss terms)
3. Domain vocabulary last (highest attention weight)

**Hallucination guards (all required):**
- _HALLUCINATIONS set with known silence outputs + prompt-leakage fragments
- Repetition loop detection (regex)
- Length-vs-duration sanity check (warn on <10% of expected words)
- min_audio_bytes guard (reject near-empty clips)

**GPT normalization pass:**
After transcription, if local contractions detected, run a fast cheap model
call to expand them before intent classification. Fail-open.

**Model recommendation:**
gpt-4o-mini-transcribe for accuracy/cost balance. Groq Whisper-large-v3 for
latency/cost if accuracy ceiling is acceptable. Always measure actual WER on
a held-out set of real client voice notes — no public dialect-specific benchmarks.

**VAD gating:**
min_audio_bytes=2000 is a basic guard. Add length-vs-duration ratio check.
Never route raw transcripts directly to keyword-triggered actions.

### Message pacing (BSP/Meta guidance):
- Welcome sequence: 1.5-4s between image, voice, and text
- Never stack 3+ media in under 2 seconds
- Monitor quality rating weekly — yellow = lengthen gaps immediately
- WhatsApp per-recipient pair-rate limit: 1 msg / 6s sustained

### Phone number filtering (DR-specific):
Required pattern: DR area codes (809/829/849) explicitly required.
Negative lookbehind: exclude prices ($, RD$) and date digits.
Negative lookahead: exclude date separators (/, -).
Support parentheses format: \(?area\)?.
Layer: regex (every message) → optional LLM judge (audit only, never sync).

---

## 13. Farewell Detection & Re-Engagement (Research-Backed, August 2026)

Source: Good, Bhattacharya, Hochstein & Voorhees — MINITS framework
(International Journal of Research in Marketing). SPIN Selling (Rackham).
Marketing Donut. Chet Holmes Buyer Pyramid. bePragma dataset (80,000+ contacts).

### Core principle (for every future client)
"Lo voy a pensar" is almost never a true no. The real no is silence.
63% of information requesters don't buy for 3+ months (Marketing Donut).
Only 3% of any audience is buying now (Chet Holmes Buyer Pyramid).
Soft farewells are latent objections disguised as goodbyes.

### Required Haiku scope categories (add to every client build)
soft_farewell: "Lo voy a pensar", "Yo le aviso", "Déjame consultarlo",
  "Después le confirmo", vague postponement with no specific date.
hard_no: "No me interesa", "No escriba más", "STOP", explicit annoyance.

### Required behavior
soft_farewell → ONE diagnostic probe:
  "¿Qué parte necesita pensar exactamente? ¿Es el precio, el proceso,
  o algo que no le quedó claro?"
  Tone: warm, no pressure. One question. Never list benefits again.
hard_no → ONE warm farewell. No probe. No questions. No offers.
Probe limit: 1 = acceptable. 2 = borderline. 3 = spam → quality rating hit.

### MINITS signals to include in Haiku prompt (per conversation context)
- Buying questions asked earlier (price, deposit, delivery) → soft_farewell
- Long/deep conversation before farewell → soft_farewell
- Specific future date given → soft_farewell (strong signal)
- Vague "yo le aviso" with no date → soft_farewell (medium signal)
- Explicit annoyance or opt-out language → hard_no

### Stage 2 — Re-engagement templates (requires Meta HSM approval)
Only for contacts with captured opt-in. 3 templates max per lead.
Cadence: Day 1-2 (contextual), Day 5-7 (value-add), Day 6-8 (break-up).
Category: Marketing (may auto-recategorize from Utility since Apr 9, 2025).
Template pacing: don't blast new templates — Meta throttles for first 7 days.
WhatsApp reactivation rate: 22-34% vs email 6-11% (bePragma, directional).
Stop after 3 touches with no reply.

### Stage 3 — Context resumption
Persist conversation state (product discussed, isolated objection, engagement
score) to CRM contact fields. When template gets a reply (fresh 24h window),
bot references prior context and resumes at the isolated objection.
Never start from zero with a re-engaged lead.

### Quality rating monitoring (required for all clients with re-engagement)
Check WhatsApp Manager weekly. Green = healthy. Yellow = pause new templates.
Red = reduce messaging limits. Over-probing (3+ touches) risks blocks/reports
which feed quality rating directly. One follow-up acceptable, never three.
