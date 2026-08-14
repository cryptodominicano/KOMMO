"""Background processing. Runs AFTER the webhook has already been acked.

Client-agnostic: every Spanish string and channel value comes from the client
pack (clients/<id>/client.toml). Onboarding a client is a new directory.
"""
import asyncio
import logging
import random
import re
import unicodedata
import time
from . import rag, agent, state, client as client_pack
from .kommo import KommoClient, KommoError
from . import dr_geo
from .transcribe import download_audio, transcribe, TranscriptionRejected
from . import linderos
from .config import settings

log = logging.getLogger("worker")


def _deaccent(x: str) -> str:
    """Lowercased, accent-stripped, for robust phrase matching against WhatsApp
    text where Dominican customers routinely omit accents."""
    x = unicodedata.normalize("NFD", (x or "").lower())
    return "".join(c for c in x if unicodedata.category(c) != "Mn")


# Deferral / "I'll think about it and get back to you" stalls, plus price stalls.
# Sales best practice: a timing/soft-no stall is the moment to add urgency, which
# here is the 23-hour 5% recovery discount. Accent-insensitive, lowercased.
# Kept to multi-word phrases to avoid firing on engaged, still-qualifying buyers.
_HES_PHRASES = (
    # think about it
    "lo voy a pensar", "voy a pensar", "voy a pensarlo", "pensarlo", "lo pienso",
    "me lo pienso", "tengo que pensar", "dejame pensar", "hay que pensar",
    "lo pienso y", "pensarlo bien", "pensarlo mejor",
    # let me see / check with calm
    "dejame ver", "voy a ver", "vamos a ver", "dejame verlo", "lo veo con calma",
    "con calma lo veo", "tengo que verlo", "dejame chequear", "lo chequeo",
    "voy a chequear", "dejame revisar", "lo reviso",
    # consult / talk to someone
    "dejame consultar", "lo consulto", "tengo que consultar", "voy a consultar",
    "consultarlo", "lo hablo con", "hablar con mi", "hablarlo con", "tengo que hablar",
    "hablarlo", "consultarlo con",
    # i'll let you know / get back to you
    "le aviso", "yo aviso", "te aviso", "ya le aviso", "cualquier cosa le aviso",
    "cualquier cosa aviso", "le avisamos",
    "le escribo luego", "despues le escribo", "le escribo mas tarde", "luego le escribo",
    "cualquier cosa le escribo", "despues te escribo", "le escribo despues",
    "le confirmo", "le confirmo luego", "despues le confirmo", "luego le confirmo",
    "ya le confirmo", "le digo luego", "despues le digo", "ahi le digo", "luego le digo",
    "le digo algo", "despues te digo", "me comunico luego", "despues me comunico",
    "luego me comunico", "cualquier cosa le digo",
    # analyze / evaluate / quote-compare
    "dejame analizar", "lo analizo", "analizarlo", "dejame evaluar", "evaluarlo",
    "lo evaluo", "lo voy a considerar", "dejame considerar", "considerarlo",
    "lo considero", "voy a cotizar", "dejame cotizar", "estoy cotizando",
    "voy a comparar", "dejame comparar", "comparando", "cotizar primero",
    # not now / later / not a priority
    "mas adelante", "mas alante", "mas pa lante", "mas pa'lante", "en otro momento",
    "por ahora no", "ahora no", "ahorita no", "no es prioridad", "para despues",
    "lo dejo para", "lo dejamos para", "cuando pueda", "cuando decida",
    "cuando este listo", "apenas pueda", "todavia no",
    # not sure yet
    "todavia no estoy seguro", "no estoy seguro", "no estoy segura", "no se todavia",
    "aun no se", "aun no estoy",
    # price stall / competing quote
    "esta caro", "muy caro", "carito", "esta carito", "esta fuerte el precio",
    "me cotizaron", "cotizaron", "mas barato", "mas economico", "mas economica",
    "consegui mas barato", "otro me da", "me sale mas barato", "mas barata",
    # english
    "think about it", "let me think", "get back to you", "i'll let you know",
    "ill let you know", "i will let you know", "i'll check", "not right now",
    "maybe later", "talk to my wife", "talk to my husband", "i'll consider",
)

_ASK_PHRASES = (
    "descuento", "descuentito", "rebaja", "rebajita", "oferta", "promocion",
    "promo", "mejor precio", "precio especial", "me rebaja", "me deja en",
    "algo de descuento", "un descuento", "discount",
)


def _entity_type(msg: dict) -> str:
    """Webhook says "lead" (singular); POST /bots/{id}/run wants "leads" (plural)."""
    t = str(msg.get("entity_type") or "lead").lower()
    return t if t.endswith("s") else t + "s"


async def _signal_handoff(k: KommoClient, msg: dict, talk_id: str, reason: str) -> None:
    """Make a handoff VISIBLE to humans in Kommo, once per episode.

    An unanswered chat is easy to miss. Best practice (per Kommo docs) is to
    also move the lead to a dedicated stage (board visibility) AND drop a task
    (which actively pings the responsible user). Both are best-effort: if they
    fail, the customer was still acknowledged and the chat is still unanswered,
    so we log and move on rather than break the reply path.
    """
    if not state.should_notify(talk_id):
        return                                   # already signalled this episode
    entity_id = msg.get("entity_id") or msg.get("element_id")
    if not entity_id:
        log.warning("talk=%s cannot signal handoff: no entity_id", talk_id)
        return
    pack = client_pack.pack()
    status_id = pack.get("kommo", {}).get("handoff_status_id")
    try:
        if status_id:
            await k.update_lead(int(entity_id), status_id=int(status_id))
        await k.create_task(
            entity_id=int(entity_id),
            text=client_pack.msg("handoff_task_text"),
            due_seconds=int(float(client_pack.behavior("handoff_task_due_hours")) * 3600),
            responsible_user_id=int(client_pack.behavior("handoff_task_user_id")),
        )
        # Internal note on the lead card — human sees context without reading full chat.
        _origin = (msg.get("origin") or "?").upper()
        _contact_id = msg.get("contact_id", "?")
        _note_text = (
            f"\U0001f916 Isla \u2192 Handoff\n"
            f"Canal: {_origin}\n"
            f"Motivo: {reason}\n"
            f"Talk: {talk_id} | Contacto ID: {_contact_id}\n"
            "Acci\u00f3n: revisar historial y dar seguimiento al cliente."
        )
        try:
            await k.add_lead_note(int(entity_id), _note_text)
        except Exception as _ne:
            log.warning("talk=%s handoff note failed (non-critical): %s", talk_id, _ne)
        log.info("talk=%s handoff signalled (stage+task+note), reason=%s", talk_id, reason)
    except KommoError as e:
        log.error("talk=%s handoff signal failed: %s", talk_id, e)


async def _human_last_active_min(k: KommoClient, talk_id: str) -> float | None:
    """Minutes since a HUMAN agent last replied, or None if none ever has.

    Kommo message authors: external = the customer, bot = our automation,
    internal = a real Kommo user (the técnico). Verified live. This is the
    only reliable takeover signal, because Kommo does not webhook outgoing
    messages. get_messages is free of the Chats API add-on quota.
    """
    try:
        msgs = await k.get_messages(talk_id, limit=20)
    except KommoError:
        return None
    latest = 0
    for m in msgs:
        a = m.get("author") or {}
        if m.get("type") == "outgoing" and a.get("type") == "internal":
            ts = int(m.get("created_at") or 0)
            if ts > latest:
                latest = ts
    if not latest:
        return None
    return (time.time() - latest) / 60.0


async def _history(k: KommoClient, talk_id: str, limit: int = 20) -> list[dict]:
    """Claude/OpenAI-shaped history from Kommo chat history (free of add-on quota)."""
    msgs = await k.get_messages(talk_id, limit=limit)
    out = []
    for m in reversed(msgs):                     # oldest first
        text = (m.get("text") or "").strip()
        if not text:
            continue
        out.append({"role": "user" if m.get("type") == "incoming" else "assistant",
                    "content": text})
    return out[-limit:]


_CLOSING_WORDS = (
    "gracias", "hasta luego", "hasta pronto", "nos vemos", "adios", "adiós",
    "bendiciones", "amen", "amén", "igualmente", "saludos", "feliz día", "feliz dia",
    "que esté bien", "que este bien", "buen día", "buen dia", "dios te bendiga",
    "excelente día", "excelente dia",
)
_CLOSING_EXACT = {
    "ok", "okay", "oki", "okey", "okok", "bien", "listo", "dale", "va", "perfecto",
    "correcto", "entiendo", "esta bien", "está bien", "ya", "de acuerdo", "vale",
    "👍", "👍🏽", "🙏", "ok gracias", "muchas gracias", "gracias", "gracias igual",
}


# Words that signal the customer still wants something (a question or intent).
# If ANY of these appear, the message is NOT a close, even if it also says "gracias".
_INTENT_HINTS = (
    "?", "cuant", "precio", "costo", "como", "cómo", "donde", "dónde", "cuando",
    "cuándo", "quiero", "avanz", "interes", "modulo", "módulo", "pozo", "estudio",
    "septic", "séptic", "baño", "ubicaci", "cuenta", "deposit", "depósit", "cotiz",
    "informaci", "foto", "tama", "profund", "cuánto", "qué ", "necesito",
)


def _looks_like_closing(text: str) -> bool:
    """Best practice: a re-engagement nudge should stand down when the conversation
    has naturally closed - but ONLY on a PURE thank-you / goodbye / acknowledgement.
    A "gracias" that comes with a question or a request ("gracias, ¿y cuánto tarda?")
    still means the customer is engaged, so we keep nudging enabled for those."""
    t = (text or "").strip().lower().rstrip("!. ")
    if not t:
        return False
    if any(h in t for h in _INTENT_HINTS):
        return False                       # a question / request is never a close
    if t in _CLOSING_EXACT:
        return True
    return len(t) <= 45 and any(word in t for word in _CLOSING_WORDS)


async def handle_message(msg: dict) -> None:
    talk_id = str(msg.get("talk_id") or "")
    msg_id = str(msg.get("id") or "")
    mtype = (msg.get("message_type") or "text").lower()
    text = (msg.get("text") or "").strip()
    # Channel origin — voice bots only work reliably on WhatsApp (waba).
    # Instagram and Facebook Messenger do not support proactive audio
    # delivery via Kommo Salesbot (Meta API restriction). Best practice:
    # gate all voice bot calls behind is_waba and fall back to text.
    _origin = (msg.get("origin") or "").lower()
    _is_waba = (_origin == "waba")

    # Instagram COMMENT detection: comments start with @mention or the text
    # contains a public post reply pattern. Kommo cannot send DMs in response
    # to public comments — the send_message call returns 202 but Instagram
    # rejects delivery. Best practice: skip all replies to comments.
    _raw_first = (msg.get("text") or "").strip()
    _is_instagram_comment = (
        _origin == "instagram_business"
        and _raw_first.startswith("@")
    )
    if _is_instagram_comment:
        log.info("talk=%s msg=%s instagram comment — skipping reply "
                 "(cannot DM in response to public comments)", talk_id, msg_id)
        return

    if not talk_id:
        log.warning("no talk_id, skipping msg=%s", msg_id)
        return

    # Scope guard: reject messages that are clearly off-topic before any
    # SCOPE GUARD — two-layer filter. Fail fast before any state writes.
    # Best practice: pattern layer catches known spam categories;
    # intent layer catches first-contact messages with zero business signal.
    _raw_text = (msg.get("text") or "").strip()
    _rtl = _raw_text.lower()

    # Layer 1: Known broadcast/spam content patterns
    _SPAM_PATTERNS = [
        # Biblical books (with and without space after — catches "mateo24" and "mateo ")
        "mateo", "marcos", "lucas", "juan", "hechos", "romanos",
        "corintios", "galatas", "efesios", "filipenses", "colosenses",
        "tesalonicenses", "timoteo", "tito", "filemon", "hebreos",
        "santiago", "pedro", "judas", "apocalipsis", "genesis",
        "exodo", "levitico", "numeros", "deuteronomio", "josue",
        "jueces", "samuel", "reyes", "cronicas", "esdras", "nehemias",
        "esther", "salmos", "salmo", "proverbios", "eclesiastes",
        "isaias", "jeremias", "ezequiel", "daniel", "oseas", "joel",
        "amos", "abdias", "jonas", "miqueas", "nahum", "habacuc",
        "sofonias", "hageo", "zacarias", "malaquias",
        # Common broadcast phrases
        "jesucristo", "dios te bendiga", "bendiciones", "amén", "amen",
        "el senor", "el señor", "cristo", "jesus regresa", "dios es",
        "buenos dias que dios", "que dios te", "dios les bendiga",
        "forward this", "comparte este", "reenvía esto", "reenvia esto",
        "cadena de oracion", "oración del dia", "oracion del dia",
    ]
    if any(p in _rtl for p in _SPAM_PATTERNS):
        log.info("talk=%s msg=%s scope-rejected (layer1: religious/broadcast spam)",
                 talk_id, msg_id)
        return

    # Layer 2: First-contact intent check.
    # Only applies to the very first message in a NEW talk (not in state yet).
    # If the message has ZERO business-intent signals AND looks like broadcast
    # content (long + no question + no business keyword), drop it silently.
    # This catches daily devotionals, political forwards, chain messages.
    # Does NOT apply to mid-conversation messages — those always get through.
    _is_new_talk = not state.is_handed_off(talk_id)
    _already_greeted = not state.first_contact.__doc__ or False  # check below
    try:
        from .state import _conn as _sc
        with _sc() as _cc:
            _already_greeted = _cc.execute(
                "SELECT 1 FROM greeted WHERE talk_id=?", (str(talk_id),)
            ).fetchone() is not None
    except Exception:
        _already_greeted = False

    # Facebook "Get Started" button: treat as a generic first contact.
    # The customer clicked the Messenger Get Started button — they want
    # to start a conversation. Route as a generic greeting so they get
    # the welcome image + service selection menu. Do NOT drop silently.
    _META_SYSTEM_MSGS = ["get started", "send message", "send_message"]
    if _raw_text.lower().strip() in _META_SYSTEM_MSGS:
        log.info("talk=%s msg=%s Meta system button — treating as generic greeting",
                 talk_id, msg_id)
        msg = dict(msg)  # make mutable
        msg["text"] = "Hola"  # treat as generic greeting
        msg["message_type"] = "text"

    if not _already_greeted and _is_new_talk and len(_raw_text) > 30:
        # Business-intent signals — ANY of these means process normally
        _BUSINESS_KEYWORDS = [
            "agua", "pozo", "estudio", "perfor", "septico", "séptico",
            "imhoff", "planta", "terreno", "finca", "precio", "costo",
            "cuanto", "cuánto", "informacion", "información", "servicio",
            "ayuda", "interesa", "quiero", "necesito", "busco",
            "cotiz", "trabajo", "llegán", "llegan", "provincia",
            "hola", "buenas", "buen dia", "buenos dias", "buenas tardes",
            "buenas noches", "info", "quisiera", "pueden", "tienen",
        ]
        _has_business_signal = any(k in _rtl for k in _BUSINESS_KEYWORDS)
        _has_question = "?" in _raw_text

        if not _has_business_signal and not _has_question:
            log.info(
                "talk=%s msg=%s scope-rejected (layer2: no business intent, "
                "len=%d, first contact)",
                talk_id, msg_id, len(_raw_text)
            )
            return

    # --- BLOCK CHECK: NO_REACTIVAR / BLOQUEADO ----------------------------
    # Checked before ANY processing. Works even when the lead is not handed off.
    # Tag the lead OR the contact with NO_REACTIVAR or BLOQUEADO in Kommo UI
    # to permanently silence the agent for that number.
    _block_entity = msg.get("entity_id") or msg.get("element_id")
    _block_contact = msg.get("contact_id")
    if _block_entity or _block_contact:
        try:
            _block_tags = set()
            _bk = KommoClient()
            if _block_entity:
                _lt = await _bk.get_lead_tags(int(_block_entity))
                _block_tags.update(t.lower() for t in _lt)
            if _block_contact:
                _ct = await _bk.get_contact_tags(int(_block_contact))
                _block_tags.update(t.lower() for t in _ct)
            if "no_reactivar" in _block_tags or "bloqueado" in _block_tags:
                log.info("talk=%s BLOCKED (no_reactivar/bloqueado tag) - silent",
                         talk_id)
                return
        except Exception as _be:
            log.warning("talk=%s block-check failed (non-critical): %s",
                        talk_id, _be)

    # Customer just messaged -> they are active; disarm any pending inactivity
    # follow-up. It is re-armed after we reply if we end up waiting on them.
    state.clear_followup(talk_id)

    location_types = set(client_pack.behavior("location_types"))
    audio_types = set(client_pack.behavior("audio_types"))
    media_types = set(client_pack.behavior("media_types"))
    marker = client_pack.behavior("handoff_marker")
    grace = int(client_pack.behavior("handoff_grace_minutes"))

    k = KommoClient()
    try:
        # --- Graceful handoff (enforced in CODE) ---
        # Old behaviour: handoff = permanent silence. New: the agent is silent
        # only while a HUMAN agent is actively engaged (author_type=internal,
        # replied within `grace` minutes). If no human has spoken, or the last
        # human reply is older than the window, the agent resumes - so a
        # customer with more questions is never stranded by a slow técnico.
        # Kommo does not webhook outgoing messages, so we read history to tell
        # a human reply apart from our own bot sends (free of add-on quota).
        if state.is_handed_off(talk_id):
            # NO_REACTIVAR tag = a human permanently silenced the agent.
            entity_id_h = msg.get("entity_id") or msg.get("element_id")
            if entity_id_h:
                try:
                    if "no_reactivar" in await k.get_lead_tags(int(entity_id_h)):
                        log.info("talk=%s NO_REACTIVAR tag - staying silent", talk_id)
                        return
                except KommoError:
                    pass
            human_min = await _human_last_active_min(k, talk_id)
            if human_min is not None and human_min < grace:
                log.info("talk=%s handoff, human active %.1fm ago - silent",
                         talk_id, human_min)
                return
            log.info("talk=%s handoff grace elapsed (human=%s) - resuming",
                     talk_id, human_min)
            state.clear_handoff(talk_id)

        # --- First contact: fire the welcome infographic, once, in code ---
        # The image is the saludo made visual: the same three services the
        # greeting text offers (agua / perforacion / septico). It reinforces
        # the menu rather than competing with it.
        # Ordering caveat: send_message and /bots/{id}/run are separate calls
        # and the bot run returns 202 (queued), so image-vs-text arrival order
        # is NOT guaranteed. Acceptable here - they reinforce each other.
        welcome_bot = client_pack.pack().get("salesbot", {}).get("welcome_bot_id", 0)
        entity_id = msg.get("entity_id") or msg.get("element_id")
        # entity_id is null when a contact messages without an open lead.
        # Best practice: look up the most recent lead for this contact so
        # Salesbot bot.run() (which requires a lead entity) can still fire.
        # We do NOT create a lead automatically — that is a human decision.
        if not entity_id:
            try:
                _contact_id = (msg.get("contact_id")
                               or talk_id)  # fallback: talk maps to contact
                _leads_r = await k.get_contact_leads(int(_contact_id))
                if _leads_r:
                    entity_id = str(_leads_r[0])  # most recent lead
                    log.info("talk=%s resolved entity_id=%s from contact",
                             talk_id, entity_id)
            except Exception as _e:
                log.warning("talk=%s entity_id lookup failed: %s", talk_id, _e)
        is_first = state.first_contact(talk_id)   # marks first contact; reused to exempt the greeting from the typing delay

        # --- FLOW LOCKING -------------------------------------------------------
        # Best practice: lock the conversation flow on first contact and use it
        # for all subsequent routing. Re-detecting from message content every turn
        # causes context drift (e.g. "Bani" in a séptico chat triggering agua).
        _locked_flow = state.get_flow(talk_id)
        if _locked_flow is None:
            # Flow detection: check if any séptico keyword appears anywhere
            # in the first message — covers pre-filled ad messages and organic.
            # Rule: any mention of septico/IMHOFF/planta/modulo in the first
            # message locks to septico. No exact-match config needed.
            _SEPTICO_FIRST_WORDS = [
                "septic", "séptic", "imhoff", "planta de trat", "planta septic",
                "modulo", "módulo", "tanque septic", "tratamiento de agua",
                "aguas residual", "aguas negra", "aguas gris",
            ]
            _tna_first = _deaccent(text)
            _is_septico_first_msg = any(w in _tna_first for w in _SEPTICO_FIRST_WORDS)
            _detected_flow = "septico" if _is_septico_first_msg else "agua"
            state.set_flow(talk_id, _detected_flow)
            _locked_flow = _detected_flow
            log.info("talk=%s flow locked: %s (keyword_first=%s)",
                     talk_id, _locked_flow, _is_septico_first_msg)
        _is_septico_flow = (_locked_flow == "septico")
        # Water-ad Click-to-WhatsApp: a known pre-filled first message routes
        # straight into the agua flow (no 3-option menu / welcome infographic).
        # The prompt has the matching reply rule; here we suppress the menu image.
        try:
            _ad_text = (client_pack.behavior("ad_direct_entry_text") or "").strip().lower()
        except Exception:
            _ad_text = ""
        from_water_ad = bool(_ad_text) and is_first and text.lower() == _ad_text
        if from_water_ad:
            log.info("talk=%s water-ad direct entry - skipping welcome menu", talk_id)
        # Septico first-contact gets its OWN welcome visual (the comparison image,
        # sent via [[SEPTICO_COMPARATIVA]] in the reply), so skip the generic
        # agua/menu infographic to avoid two competing welcome images.
        _septico_first = is_first and any(w in text.lower() for w in (
            "septic", "séptic", "imhoff", "planta de trat"))
        if _septico_first:
            log.info("talk=%s septico first-contact detected - welcome image will fire", talk_id)

        # Detect generic greeting: no agua, no séptico, no water-ad keywords.
        # Best practice (Infobip design guidelines): show a service selection
        # menu so the customer picks their path. Audio fires only after they
        # explicitly choose a service (flow confirmed on second message).
        _AGUA_KEYWORDS = [
            "agua", "pozo", "perfor", "estudio", "terreno", "finca",
            "vena", "hidrolog", "topograf", "radiestesia",
        ]
        _tna_first_check = _deaccent(text)
        _has_agua_kw = any(w in _tna_first_check for w in _AGUA_KEYWORDS)
        _has_septico_kw = _septico_first or _is_septico_flow
        _is_generic_greeting = (
            is_first and not from_water_ad
            and not _has_agua_kw and not _has_septico_kw
        )
        if _is_generic_greeting:
            log.info("talk=%s generic greeting — service selection menu will show",
                     talk_id)
        if welcome_bot and entity_id and is_first:
            try:
                await k.run_bot(int(welcome_bot), entity_id, _entity_type(msg))
                log.info("talk=%s launched welcome bot %s", talk_id, welcome_bot)
            except KommoError as e:
                log.error("talk=%s welcome bot launch failed: %s", talk_id, e)

        # Tracks which voice note fired this turn so the LLM text follows up correctly.
        _voz_fired = None

        # --- VOZ_AGUA_1: welcome voice note, first contact, water flow only -------
        _sb = client_pack.pack().get("salesbot", {})
        _voz_triggers = _sb.get("voz_agua_triggers", {})
        _imhoff_triggers = _sb.get("voz_imhoff_triggers", {})
        # VOZ_AGUA_1: only fire when agua flow is explicitly confirmed.
        # Generic greeting (no keywords) → hold audio, show menu instead.
        _agua_flow_confirmed = (
            is_first and not _septico_first and not from_water_ad
            and (_has_agua_kw or state.is_flow_confirmed(talk_id))
        )
        if (_agua_flow_confirmed
                and entity_id and _is_waba
                and _voz_triggers.get("VOZ_AGUA_1")):
            _vk1 = "VOZ_AGUA_1"
            if not state.voice_already_sent(talk_id, _vk1):
                try:
                    await asyncio.sleep(1)
                    await k.run_bot(int(_voz_triggers[_vk1]), entity_id, _entity_type(msg))
                    state.mark_voice_sent(talk_id, _vk1)
                    _voz_fired = _vk1
                    log.info("talk=%s launched VOZ_AGUA_1 %s", talk_id, _voz_triggers[_vk1])
                except KommoError as e:
                    log.error("talk=%s VOZ_AGUA_1 failed: %s", talk_id, e)

        # --- VOZ_IMHOFF_1: welcome voice note, first contact, séptico flow only ---
        # VOZ_IMHOFF_1: only fire when séptico is explicitly in first message.
        if (is_first and _has_septico_kw and _septico_first
                and entity_id and _is_waba
                and _imhoff_triggers.get("[[VOZ_IMHOFF_1]]")):
            _vk_i1 = "[[VOZ_IMHOFF_1]]"
            if not state.voice_already_sent(talk_id, _vk_i1):
                try:
                    await asyncio.sleep(1)
                    await k.run_bot(int(_imhoff_triggers[_vk_i1]), entity_id, _entity_type(msg))
                    state.mark_voice_sent(talk_id, _vk_i1)
                    _voz_fired = _vk_i1
                    log.info("talk=%s launched VOZ_IMHOFF_1 %s",
                             talk_id, _imhoff_triggers[_vk_i1])
                except KommoError as e:
                    log.error("talk=%s VOZ_IMHOFF_1 failed: %s", talk_id, e)

        # --- GPS pin OR a pasted Google Maps link: treat both as a location share ---
        # message_type == "location" is a first-class Kommo enum. Customers also
        # very often PASTE a Google Maps URL as text instead of sharing a pin; that
        # is still a location, so route it into the same linderos flow rather than
        # letting the model repeat "send me your location".
        maps_link = mtype == "text" and any(h in text.lower() for h in (
            "maps.app.goo.gl", "goo.gl/maps", "google.com/maps",
            "maps.google.", "/maps/place", "/maps?"))
        if mtype in location_types or maps_link:
            log.info("talk=%s location received (%s)", talk_id,
                     "maps-link" if maps_link else "pin")
            # Linderos flow is agua-only. In séptico conversations a location
            # pin means delivery address — acknowledge and hand off to the team.
            if _is_septico_flow:
                log.info("talk=%s location in séptico flow — ack + handoff",
                         talk_id)
                await k.send_message(
                    talk_id,
                    "¡Gracias! 🙏 Recibimos tu ubicación. Un representante "
                    "se comunicará contigo para coordinar la entrega.")
                state.mark_handoff(talk_id, "location_septico")
                await _signal_handoff(k, msg, talk_id, "location_septico")
                return
            entity_id = msg.get("entity_id") or msg.get("element_id")
            first = state.linderos_first(talk_id) if entity_id else False
            if entity_id and settings.public_base_url and first:
                # First pin: send the drawing link.
                link = linderos.build_link(entity_id, talk_id, settings.client_id)
                await k.send_message(
                    talk_id, client_pack.msg("linderos_invite") + "\n\n" + link)
                state.set_awaiting_linderos(talk_id)
            else:
                # BACKUP PATH: a second pin (the drawing tool did not work) or no
                # link possible. Acknowledge the pin and hand off - a técnico marks
                # the linderos manually and already has the customer's GPS location.
                await k.send_message(talk_id, client_pack.msg("linderos_fallback"))
                state.mark_handoff(talk_id, "linderos_fallback")
                await _signal_handoff(k, msg, talk_id, "linderos_fallback")
            return

        # --- Voice note: download -> transcribe -> treat as text ---
        if mtype in audio_types:
            link = (msg.get("attachment") or {}).get("link")
            if not link:
                # Kommo's docs never show `attachment` on the INCOMING webhook
                # (only a text sample), so fall back to the history endpoint,
                # which does not consume add-on quota.
                for m in await k.get_messages(talk_id, limit=5):
                    if str(m.get("id")) == msg_id:
                        link = (m.get("attachment") or {}).get("link")
                        break
            if not link:
                log.warning("talk=%s voice note without attachment link", talk_id)
                await k.send_message(talk_id, client_pack.msg("audio_unclear"))
                return
            try:
                text = await transcribe(await download_audio(link))
                log.info("talk=%s transcript=%r", talk_id, text)
            except TranscriptionRejected as e:
                # Whisper invents filler on silence. Never guess intent - ask again.
                log.info("talk=%s transcription rejected (%s)", talk_id, e)
                await k.send_message(talk_id, client_pack.msg("audio_unclear"))
                return

        # --- Inbound media (usually a deposit receipt): acknowledge + hand off ---
        # A photo arrives with EMPTY text, so without this branch it falls
        # through to the empty-text drop below and the customer is ghosted at
        # the exact moment they send proof of payment. Deterministic, in code:
        # the business rule is NEVER confirm a payment.
        if mtype in media_types:
            is_receipt = state.deposit_was_presented(talk_id)
            # Linderos map: if we asked for the terrain and no deposit has been
            # presented yet, an inbound image IS the customer's marked map. Do NOT
            # hand off - route straight into the RD$5,000 deposit flow. The prompt
            # answers the "[[LINDEROS_LISTO]]" signal with the ETAPA 1 deposit
            # message, which fires the voice note + bank details automatically.
            if not is_receipt and state.is_awaiting_linderos(talk_id):
                state.clear_awaiting_linderos(talk_id)
                log.info("talk=%s linderos map received - continuing to deposit "
                         "(no handoff)", talk_id)
                text = "[[LINDEROS_LISTO]]"
            else:
                key = "media_received" if is_receipt else "media_received_generic"
                log.info("talk=%s inbound media (%s) receipt=%s - ack + handoff",
                         talk_id, mtype, is_receipt)
                # Media cooldown: when a customer sends multiple images
                # simultaneously, each triggers a separate webhook. Without a
                # cooldown the same ack message fires once per image.
                # Best practice: one acknowledgment per burst, 30s cooldown.
                if state.media_ack_on_cooldown(talk_id, cooldown_seconds=30):
                    log.info("talk=%s media ack cooldown (30s) — skipping "
                             "duplicate ack for %s", talk_id, mtype)
                    return
                # Record ack time for cooldown, then clear after 30s so
                # future image bursts in the same conversation still get acked.
                state.mark_voice_sent(talk_id, "media_ack")
                await k.send_message(talk_id, client_pack.msg(key))
                state.mark_handoff(talk_id, f"media_received:{mtype}")
                await _signal_handoff(k, msg, talk_id, f"media_received:{mtype}")
                # Schedule clear of media_ack cooldown after 30s
                asyncio.get_event_loop().call_later(
                    30, state.clear_media_ack, talk_id)
                return

        if not text:
            log.info("talk=%s nothing to answer (type=%s)", talk_id, mtype)
            return

        # DEBOUNCE: consecutive messages from the same customer are answered ONCE.
        # Wait a short window (doubles as the human-like pause); if a NEWER message
        # arrives while we wait, abort and let that newer task reply - by then all
        # the messages are in history, so the single reply addresses them together.
        # The first greeting is exempt (instant), as is the deterministic linderos
        # map signal. This runs in the background task, so it never delays the ack.
        if not is_first and text != "[[LINDEROS_LISTO]]":
            try:
                lo = float(client_pack.behavior("reply_delay_min_seconds"))
                hi = float(client_pack.behavior("reply_delay_max_seconds"))
            except Exception:
                lo, hi = 0.0, 0.0
            if hi > 0:
                # Scale to message length: short ~3s, long ~9s.
                _dl_lo = max(3.0, min(lo, hi))
                _dl_hi = max(_dl_lo, max(lo, hi))
                _char_ratio = min(1.0, len(text) / 200.0)
                _scaled = _dl_lo + _char_ratio * (_dl_hi - _dl_lo)
                _jitter = random.uniform(-0.5, 0.5)
                await asyncio.sleep(max(_dl_lo, _scaled + _jitter))
            if msg_id and not state.is_latest_inbound(talk_id, msg_id):
                log.info("talk=%s superseded by a newer message - skipping reply",
                         talk_id)
                return

        # --- VOZ_AGUA_2-8: keyword-triggered, audio-first, no-repeat ─────────────
        # Use locked flow state — deterministic, never drifts mid-conversation.
        _tna = _deaccent(text)
        # --- SECOND MESSAGE AFTER GENERIC GREETING: service confirmation --------
        # If first contact was a generic greeting (flow unconfirmed), the
        # second message is the customer picking their service. Detect it,
        # mark flow confirmed, and fire the correct welcome audio.
        # No image fires — the welcome image already went out on first contact.
        _SEPTICO_CONFIRM_WORDS = [
            "septic", "imhoff", "planta", "modulo", "bano", "fosa", "tanque",
            "aguas negra", "aguas residual", "aguas gris",
        ]
        _AGUA_CONFIRM_WORDS = [
            "agua", "pozo", "perfor", "estudio", "terreno", "finca",
            "vena", "hoyo", "pozo", "cisterna",
        ]
        _flow_was_generic = (not state.is_flow_confirmed(talk_id)
                             and not is_first
                             and _locked_flow == "agua")
        if _flow_was_generic and entity_id and _is_waba:
            _tna_confirm = _deaccent(text)
            _confirms_septico = any(w in _tna_confirm for w in _SEPTICO_CONFIRM_WORDS)
            _confirms_agua = any(w in _tna_confirm for w in _AGUA_CONFIRM_WORDS)
            if _confirms_septico:
                # Customer chose séptico — re-lock flow and fire IMHOFF_1 audio
                state.set_flow(talk_id + "_override", "septico")  # note for log
                state.mark_flow_confirmed(talk_id)
                _is_septico_flow = True
                _vk_confirm = "[[VOZ_IMHOFF_1]]"
                if not state.voice_already_sent(talk_id, _vk_confirm):
                    _bid_confirm = _imhoff_triggers.get(_vk_confirm)
                    if _bid_confirm:
                        try:
                            await asyncio.sleep(1)
                            await k.run_bot(int(_bid_confirm), entity_id,
                                           _entity_type(msg))
                            state.mark_voice_sent(talk_id, _vk_confirm)
                            _voz_fired = _vk_confirm
                            log.info("talk=%s confirmed SEPTICO — fired VOZ_IMHOFF_1",
                                     talk_id)
                        except KommoError as e:
                            log.error("talk=%s VOZ_IMHOFF_1 confirm failed: %s",
                                      talk_id, e)
            elif _confirms_agua:
                # Customer chose agua — confirm flow and fire AGUA_1 audio
                state.mark_flow_confirmed(talk_id)
                _vk_confirm_a = "VOZ_AGUA_1"
                if not state.voice_already_sent(talk_id, _vk_confirm_a):
                    _bid_confirm_a = _voz_triggers.get(_vk_confirm_a)
                    if _bid_confirm_a:
                        try:
                            await asyncio.sleep(1)
                            await k.run_bot(int(_bid_confirm_a), entity_id,
                                           _entity_type(msg))
                            state.mark_voice_sent(talk_id, _vk_confirm_a)
                            _voz_fired = _vk_confirm_a
                            log.info("talk=%s confirmed AGUA — fired VOZ_AGUA_1",
                                     talk_id)
                        except KommoError as e:
                            log.error("talk=%s VOZ_AGUA_1 confirm failed: %s",
                                      talk_id, e)

        if entity_id and _voz_triggers and not is_first and _is_waba and not _is_septico_flow:
            _VOZ_KW = [
                ("VOZ_AGUA_5", ["esta muy caro","muy costoso","es mucho dinero",
                    "pense que era menos","no tengo ese presupuesto","muy alto",
                    "muy elevado","no puedo pagar eso","fuera de mi presupuesto",
                    "demasiado caro","hacen descuento","pueden bajar",
                    "ese es el mejor precio","no hay oferta","por que cuesta tanto",
                    "esta fuerte ese precio","lo voy a pensar","dejame ver",
                    "esta dificil","muy costoso para mi"]),
                ("VOZ_AGUA_4", ["quiero pagar","donde deposito","enviame la cuenta",
                    "voy a pagar","como hago el pago","a que cuenta",
                    "enviame los datos","donde transfiero","listo para pagar",
                    "quiero reservar","procedamos","ya tengo todo",
                    "aqui esta mi ubicacion","ya envie la ubicacion"]),
                ("VOZ_AGUA_3", ["quiero hacer el estudio","vamos a hacerlo",
                    "quiero proceder","que necesito","cual es el siguiente paso",
                    "como funciona","como se hace","que debo enviar",
                    "que necesitan de mi","como empezamos","quiero contratar el estudio",
                    "estoy listo","quiero iniciar","como es el procedimiento",
                    "expliqueme el proceso","que sigue","que hago ahora",
                    "quiero coordinar"]),
                ("VOZ_AGUA_2", ["cuanto cuesta perforar","que cuesta un pozo",
                    "cuanto vale hacer un pozo","cual es el precio","en cuanto sale",
                    "cuanto cobran","cuanto cuesta hacer un hoyo",
                    "cuanto cuesta el pozo","cuanto cuesta sacar agua",
                    "cual es el costo","que precio tiene","que vale",
                    "cuanto cuesta encontrar agua","cuanto vale una perforacion",
                    "cobran por pie","cuanto cuesta por metro",
                    "cuanto cuesta por pie","como cobran"]),
                ("VOZ_AGUA_6", ["donde estan ubicados","donde estan",
                    "donde queda la oficina","tienen oficina","en que ciudad estan",
                    "donde los encuentro","donde puedo visitarlos",
                    "puedo pasar por la oficina","donde trabajan",
                    "en que provincia estan","donde operan","cual es su direccion"]),
                ("VOZ_AGUA_7", ["como se paga","cuando se paga","se paga antes",
                    "se paga despues","cuanto hay que adelantar","hay deposito",
                    "aceptan transferencia","aceptan efectivo","aceptan tarjeta",
                    "como funcionan los pagos","cuales son las condiciones",
                    "cual es la forma de pago","que metodos aceptan",
                    "se paga completo","hay financiamiento",
                    "puedo pagar en dos partes"]),
                ("VOZ_AGUA_8", ["puedo llamarlo","lo puedo llamar",
                    "quiero hablar con usted","quiero hablar con un asesor",
                    "tiene un numero","me puede llamar","llameme",
                    "quiero hacerle unas preguntas","prefiero hablar",
                    "podemos hablar","esta disponible","podemos conversar",
                    "puede atenderme","tiene unos minutos",
                    "necesito hablar con alguien","quiero comunicarme directamente",
                    "le puedo hacer una llamada"]),
            ]
            for _vk, _kws in _VOZ_KW:
                if any(kw in _tna for kw in _kws):
                    if not state.voice_already_sent(talk_id, _vk):
                        _bid = _voz_triggers.get(_vk)
                        if _bid:
                            try:
                                await k.run_bot(int(_bid), entity_id, _entity_type(msg))
                                state.mark_voice_sent(talk_id, _vk)
                                _voz_fired = _vk
                                log.info("talk=%s launched %s bot %s",
                                         talk_id, _vk, _bid)
                            except KommoError as e:
                                log.error("talk=%s %s failed: %s", talk_id, _vk, e)
                    break  # one voice note per turn

        # --- VOZ_IMHOFF_2-4: séptico keyword-triggered, no-repeat per convo -------
        # VOZ_IMHOFF_4 fires a 3-step sequence: voice → Instagram text → Wellington image.
        if entity_id and _imhoff_triggers and not is_first and _is_waba:
            _tna_i = _deaccent(text)
            # Flow is already locked — _is_septico_flow is the authoritative signal.
            # No need to re-scan message content; that causes context drift.
            _IMHOFF_KW = [
                ("[[VOZ_IMHOFF_3]]", [
                    "esta muy cara","muy costosa","es mucho dinero",
                    "pense que costaba menos","fuera de mi presupuesto",
                    "muy elevado","no tengo ese presupuesto","hacen descuento",
                    "ese es el mejor precio","no pueden bajar el precio",
                    "hay alguna oferta","esta fuerte ese precio",
                    "la competencia la tiene mas barata","vi otra mas economica",
                    "por que cuesta tanto","que tiene de diferente",
                    "vale la pena","lo voy a pensar","esta dificil",
                    "no puedo pagar eso ahora",
                ]),
                ("[[VOZ_IMHOFF_2]]", [
                    "quiero comprar","como la compro","como funciona",
                    "que debo hacer","cual es el proceso","como procedo",
                    "quiero adquirir una","que necesito","como hacemos",
                    "quiero ordenar","quiero hacer el pedido","estoy listo",
                    "que sigue","cual es el siguiente paso","como hago el pago",
                    "como se entrega","cuanto tarda","como llega",
                    "hacen envios","la instalan","que incluye",
                    "que tengo que enviar","quiero reservar una",
                ]),
                ("[[VOZ_IMHOFF_4]]", [
                    "donde estan ubicados","donde estan","tienen oficina",
                    "donde puedo visitarlos","cual es la direccion",
                    "puedo pasar","donde queda","en que ciudad estan",
                    "donde los encuentro","quiero ir personalmente",
                    "quiero pasar a verlos","no me gusta pagar por internet",
                    "no confio en transferir","quiero ver el producto primero",
                    "quiero conocerlos antes","son una empresa real",
                    "tienen oficina fisica","donde puedo ver las plantas",
                    "quiero asegurarme antes de pagar","como se que son confiables",
                    "tienen referencias","tienen redes sociales",
                    "donde puedo ver sus trabajos","quienes son ustedes",
                    "desde hace cuanto trabajan","quien es el ingeniero",
                    "quien es wellington","quiero hablar con alguien",
                    "puedo ir a conocerlos",
                ]),
            ]
            if _is_septico_flow:
                for _vk_i, _kws_i in _IMHOFF_KW:
                    if any(kw in _tna_i for kw in _kws_i):
                        if not state.voice_already_sent(talk_id, _vk_i):
                            _bid_i = _imhoff_triggers.get(_vk_i)
                            if _bid_i:
                                try:
                                    await k.run_bot(int(_bid_i), entity_id,
                                                    _entity_type(msg))
                                    state.mark_voice_sent(talk_id, _vk_i)
                                    _voz_fired = _vk_i
                                    log.info("talk=%s launched %s bot %s",
                                             talk_id, _vk_i, _bid_i)
                                    if _vk_i == "[[VOZ_IMHOFF_4]]":
                                        await asyncio.sleep(2)
                                        _ig_text = (
                                            "📍 También puedes conocer más sobre nuestra "
                                            "empresa, nuestros proyectos y el trabajo que "
                                            "realizamos visitando nuestro Instagram oficial. "
                                            "Allí encontrarás fotografías, videos de "
                                            "instalaciones reales, testimonios de clientes "
                                            "y mucho más.\n\n"
                                            "👉 Instagram: @aguasprofundas_rd\n\n"
                                            "Será un gusto recibirte y ayudarte con "
                                            "cualquier duda."
                                        )
                                        await k.send_message(talk_id, _ig_text)
                                        log.info("talk=%s sent Instagram text "
                                                 "(VOZ_IMHOFF_4)", talk_id)
                                        await asyncio.sleep(1)
                                        _wbot = int(_imhoff_triggers.get(
                                            "wellington_lider_foto_bot_id", 0) or 0)
                                        if _wbot:
                                            try:
                                                await k.run_bot(_wbot, entity_id,
                                                                _entity_type(msg))
                                                log.info("talk=%s launched Wellington "
                                                         "image bot %s", talk_id, _wbot)
                                            except KommoError as e:
                                                log.error("talk=%s Wellington bot "
                                                          "failed: %s", talk_id, e)
                                except KommoError as e:
                                    log.error("talk=%s %s failed: %s",
                                              talk_id, _vk_i, e)
                        break  # one voice note per turn

        # --- RAG + LLM ---
        kb = await rag.retrieve(text)
        history = await _history(k, talk_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]               # current message passed separately
        # Septico 5% recovery discount window (24h from first contact, once).
        # The engine owns the HARD gates (route + window + not-yet-offered) and
        # tells the model DISPONIBLE / NO_DISPONIBLE; the model owns the JUDGEMENT
        # (only on real hesitation). Sheyla requires an agent to authorize the
        # actual discount, so the model only OFFERS - acceptance hands off.
        extra = ""
        offer_discount = False
        offer_is_ask = False
        try:
            state.note_first_seen(talk_id)
            _tl = text.lower()
            _hb = " ".join(m.get("content", "") for m in history[-8:]).lower()
            _sep = any(w in _tl or w in _hb for w in (
                "septic", "séptic", "imhoff", "planta de trat", "modulo",
                "módulo", "bano", "baño"))
            if _sep:
                _hrs = state.hours_since_first(talk_id)
                _avail = (_hrs is not None and _hrs < 24.0) and not state.discount_offered(talk_id)
                # Deciding WHEN to offer is too flaky for the model (it once fired
                # the lock marker without an offer, burning the discount silently).
                # So the ENGINE detects the hesitation/deferral or a direct discount
                # ask in the customer's message and tells the model to offer NOW.
                _tl_na = _deaccent(_tl)
                _hes = any(p in _tl_na for p in _HES_PHRASES)
                _ask = any(p in _tl_na for p in _ASK_PHRASES)
                if _avail and (_hes or _ask):
                    # The model is unreliable at obeying an "offer now" directive on
                    # soft goodbyes ("yo le aviso"), so the ENGINE appends the offer
                    # deterministically after the reply. Tell the model to stay out of it.
                    offer_discount = True
                    offer_is_ask = _ask
                    # Say NOTHING to the model about the discount: any instruction that
                    # references it gets parroted to the customer or causes a double-offer.
                    # The KB has no discount content, so the model won't volunteer one;
                    # the engine appends the fixed 5% offer below, deterministically.
                    extra = ""
                elif _avail:
                    extra = ("DESCUENTO_5: disponible, pero NO lo menciones salvo que el cliente "
                             "pregunte directamente por un descuento.")
                else:
                    extra = "DESCUENTO_5: NO disponible. No menciones ningún descuento."
        except Exception as e:
            log.warning("talk=%s discount-window calc failed: %s", talk_id, e)
        # Channel-aware price guard for non-WhatsApp channels.
        # On Instagram/Facebook no audio fires so the LLM must still answer,
        # but it should frame prices correctly: study-first, then quote.
        # Best practice: never volunteer drilling prices without study context.
        if not _is_waba:
            extra = (extra + " " if extra else "") + (
                "CANAL_NO_WABA: Esta conversación viene de Instagram o Facebook. "
                "No se envían notas de voz en este canal. "
                "Para preguntas sobre precio de perforación, explica que el precio "
                "exacto se define DESPUÉS del estudio — nunca des precios exactos de "
                "perforación en texto. Para precio del estudio sí puedes dar el rango "
                "RD$45,000-50,000. Mantén respuestas breves, máximo 3 líneas."
            )

        # If VOZ_AGUA_1 was sent in a PREVIOUS turn (not this one), inject
        # a signal so the LLM skips the study explanation and greeting blocks.
        # This prevents the full study pitch repeating when the client sends a
        # second 'Hola' or when the location is captured after the welcome audio.
        # Check both flows — if either welcome audio was already sent,
        # apply PREVIO_BYPASS. Fixes double menu on séptico conversations
        # where VOZ_IMHOFF_1 fired instead of VOZ_AGUA_1.
        # any_voice_sent: True if ANY voice note fired this conversation.
        # Broader than checking VOZ_AGUA_1/IMHOFF_1 only — covers cases
        # where a different audio fired first (VOZ_AGUA_3, VOZ_AGUA_6 etc)
        # and the study explanation should still be suppressed.
        _welcome_audio_sent = state.any_voice_sent(talk_id)
        if not _voz_fired and _welcome_audio_sent:
            _tna_previo = _deaccent(text)
            # Short/closed responses after VOZ_AGUA_1: bypass LLM entirely.
            # These are messages where the LLM would otherwise repeat the study.
            _CLOSED_RESPONSES = [
                "no", "asi no", "de saber", "gracia", "esta bien", "ok",
                "okay", "bueno", "entendido", "claro", "perfecto", "bien",
                "no gracias", "no me interesa", "lo voy a pensar", "despues"
            ]
            _is_short_closed = (
                len(text) < 30 or
                any(r in _tna_previo for r in _CLOSED_RESPONSES)
            )
            if _is_short_closed:
                # Determine appropriate short reply based on sentiment
                _neg_signals = ["no", "asi no", "no me interesa", "no gracias",
                                "no quiero", "lo voy a pensar", "despues"]
                if any(s in _tna_previo for s in _neg_signals):
                    _direct_reply = ("Entiendo, no hay problema. 😊 "
                                     "Si en algún momento desea más información o "
                                     "avanzar, aquí estamos para ayudarle.")
                else:
                    _direct_reply = ("¡De nada! 😊 Cuando guste, por favor mándeme "
                                     "la ubicación de su terreno para comenzar. 📍")
                log.info("talk=%s PREVIO_BYPASS: short/closed response, skipping LLM",
                         talk_id)
            else:
                # Longer new question — call LLM but with tight constraint
                extra = (extra + ' ' if extra else '') + (
                    'AUDIO_ENVIADO_PREVIO: VOZ_AGUA_1 ya fue enviada. '
                    'NO repitas el saludo ni la explicación del estudio. '
                    'NO des precios de perforación. '
                    'Responde SOLO la pregunta específica del cliente en máximo 2 líneas '
                    'y cierra con una pregunta que avance el proceso.'
                )

        # Inject voice-note follow-up into extra so the LLM knows exactly
        # what one-liner to send after the audio — no repetition, no prices.
        _VOZ_FOLLOWUPS = {
            # Audio already covers study process, 80-90% success, RD$45-50K pricing,
            # exploratory vs conventional. Closes asking for location. Text echoes that.
            "VOZ_AGUA_1": "Para comenzar, por favor mándeme la ubicación de donde desea realizar el estudio. 📍",
            # Audio: can't price drilling without study — we work with data not guessing.
            # Text nudges back to study as the logical next step.
            "VOZ_AGUA_2": "¿Le gustaría comenzar con el estudio para poder darle toda la información que necesita? 🙏",
            # Audio: send location, I'll send satellite photo, mark boundaries with WhatsApp pencil.
            # Text prompts them to send location now.
            "VOZ_AGUA_3": "Por favor mándeme la ubicación de su terreno y seguimos el proceso desde ahí. 📍",
            # Audio: RD$5K deposit starts topographic study, 2-3 days, visit land,
            # 3-4 more days, pay remainder, get report, send voucher.
            # Text moves toward sending bank details.
            "VOZ_AGUA_4": "¿Tiene alguna pregunta sobre el proceso o está listo para que le envíe los datos de depósito? 🙏",
            # Audio: 3-part study vs competitors' 1-part, 80-90% vs 25% success, quality justification.
            # Text soft-closes toward committing.
            "VOZ_AGUA_5": "¿Le gustaría proceder con el estudio o tiene alguna otra consulta antes de decidir? 🙏",
            # Audio: located in Arabacoa, serve all country, need their location to quote.
            # Text asks for location to move forward.
            "VOZ_AGUA_6": "¿En qué pueblo o sector desea realizar el estudio? Con eso le cotizo de inmediato. 🙏",
            # Audio: RD$5K deposit, visit land, 24-48h study, contact for remainder, deliver report.
            # Text asks if ready to start.
            "VOZ_AGUA_7": "¿Está listo para dar el primer paso o tiene alguna consulta adicional antes de comenzar? 🙏",
            # Audio: yes to call but need to schedule — asks for a good time.
            # Text asks for their available time.
            "VOZ_AGUA_8": "¿Qué hora le queda bien para coordinar la llamada? 🙏",
            # Audio: full product intro — plastic vs cement, 2 modules (RD$70K/8 baths,
            # RD$105K/16 baths), modular system. Closes: "si le gustaría comprar no deja saber."
            # Text qualifies which module they need.
            "[[VOZ_IMHOFF_1]]": "¿Cuántos baños tiene su propiedad? Con eso le indico el módulo que necesita. 🙏",
            # Audio: RD$10K deposit, 1 week delivery, pay remainder on delivery.
            # Text asks if ready to place deposit.
            "[[VOZ_IMHOFF_2]]": "¿Está listo para proceder con el depósito de RD$10,000 o tiene alguna pregunta adicional? 🙏",
            # Audio: plastic vs cement comparison, more durable, won't crack or poison soil/water.
            # Text soft-closes toward decision.
            "[[VOZ_IMHOFF_3]]": "¿Le gustaría proceder con su planta o tiene alguna otra consulta antes de decidir? 🙏",
            # Audio: trust/location — sells from factory, can send registro mercantil, always available.
            # Followed by Instagram text + Wellington image — no extra text override needed.
            "[[VOZ_IMHOFF_4]]": "",
        }
        # If a voice bot fired AND has a prescribed follow-up line, skip the LLM
        # entirely and send the hardcoded line. This is the only reliable way to
        # prevent the LLM from contradicting the audio content — extra_system
        # injections are too low-priority and the KB context overrides them.
        _direct_reply = None
        if _voz_fired and _voz_fired in _VOZ_FOLLOWUPS:
            _followup = _VOZ_FOLLOWUPS[_voz_fired]
            if _followup:
                # Hard bypass: send the followup directly, no LLM involved.
                _direct_reply = _followup
                log.info("talk=%s AUDIO_BYPASS: skipping LLM, sending direct followup "
                         "for %s", talk_id, _voz_fired)
            # VOZ_AGUA_1 / VOZ_IMHOFF_1 / VOZ_IMHOFF_4: no hardcoded line, normal LLM flow
        if _direct_reply:
            reply = _direct_reply
        else:
            reply = await agent.generate(text, kb, history, extra)
        if not reply:
            log.warning("talk=%s empty model reply", talk_id)
            return

        # Model signals handoff with a sentinel; the pause is enforced here.
        handoff = marker in reply
        reply = reply.replace(marker, "").strip()

        # Septico 5% discount was presented -> record it so it is never offered
        # again in this conversation. Strip the hidden marker before sending.
        if "[[DESC_OFRECIDO]]" in reply:
            reply = reply.replace("[[DESC_OFRECIDO]]", "").strip()
            state.mark_discount_offered(talk_id)
            log.info("talk=%s septico 5%% discount offered - locked", talk_id)

        # IMAGE WORKAROUND: send_message is text-only, but a Salesbot can attach
        # images. The model emits a sentinel; we strip it and launch the bot.
        sb = client_pack.pack().get("salesbot", {})
        bots = sb.get("triggers", {})
        fire: list[int] = []

        # --- DEPOSIT / BANK DETAILS: fired by the TEXT, not by a sentinel ---
        # The model decides whether to send the septico order message - that is
        # judgement. The bank photo riding along with it is a RULE, so it fires
        # from code. Sentinel firing measured ~80-90%; a miss here would tell the
        # customer "le comparto los datos" and send no photo, at the exact moment
        # they are trying to pay. Same class of broken promise as the [[HANDOFF]]
        # bug on garantia.
        #
        # The account number and cedula exist ONLY inside the Salesbot image in
        # Kommo. They never touch this repo, the prompt, the KB, or a log line.
        trigger_text = sb.get("deposit_trigger_text") or ""
        deposit_bot = sb.get("deposit_bot_id", 0)
        send_bank = False
        # Hidden [[DEPOSITO]] sentinel decouples the bank-photo trigger from the
        # client-facing wording, so client-approved verbatim deposit lines ship
        # intact. Strip it BEFORE the reply is sent. The legacy text phrase is
        # kept as a fallback so older deposit messages still fire.
        deposit_requested = "[[DEPOSITO]]" in reply
        if deposit_requested:
            reply = reply.replace("[[DEPOSITO]]", "").strip()
        if trigger_text and trigger_text in reply:
            deposit_requested = True
        # Optional payment voice note, scoped to the agua study deposit via
        # [[AUDIO_PAGO]]; plays right before the bank details. Strip it always.
        audio_bot = int(sb.get("payment_audio_bot_id", 0) or 0)
        audio_requested = "[[AUDIO_PAGO]]" in reply
        if audio_requested:
            reply = reply.replace("[[AUDIO_PAGO]]", "").strip()
        # Zone/sector tag for per-zone lists. Isla appends [[SECTOR:Town]] when she
        # captures the pueblo; strip it here, apply the tag once entity_id is known.
        _sector = ""
        _sm = re.search(r"\[\[SECTOR:([^\]]+)\]\]", reply)
        if _sm:
            _sector = _sm.group(1).strip()
            reply = re.sub(r"\s*\[\[SECTOR:[^\]]+\]\]\s*", " ", reply).strip()
        if deposit_requested:
            if not deposit_bot:
                log.error("talk=%s DEPOSIT MESSAGE SENT BUT deposit_bot_id IS 0 - "
                          "customer promised bank details and will get none", talk_id)
            elif not state.deposit_cooldown_ok(talk_id):
                log.warning("talk=%s deposit within cooldown - suppressed", talk_id)
            else:
                fire.append(int(deposit_bot))
                send_bank = True
                state.clear_awaiting_linderos(talk_id)
                log.info("talk=%s deposit message sent - firing bank text + photo %s",
                         talk_id, deposit_bot)
        for sentinel, bot_id in bots.items():
            if sentinel in reply:
                reply = reply.replace(sentinel, "").strip()
                if bot_id:
                    fire.append(int(bot_id))
                else:
                    log.warning("sentinel %s has no bot_id configured", sentinel)

        # Deterministic 5% recovery offer, engine-owned (the model is too flaky on
        # soft goodbyes and, on a direct "hay descuento?", tends to invent a wrong
        # "no discount" answer). Fired once per conversation.
        _OFFER_ASK = ("¡Claro! Como usted nos escribió por nuestra campaña, si reserva y hace el "
                      "depósito dentro de las próximas 23 horas le puedo aplicar un 5% de descuento "
                      "sobre el precio del módulo. Después de ese tiempo vuelve a su precio normal. "
                      "Si prefiere, también podemos coordinar una llamada. 🙂")
        _OFFER_TAIL = ("\n\nUna cosa: como usted nos escribió por nuestra campaña, si reserva y "
                       "hace el depósito dentro de las próximas 23 horas le puedo aplicar un 5% de "
                       "descuento sobre el precio del módulo. Después de ese tiempo vuelve a su "
                       "precio normal. Si prefiere, también podemos coordinar una llamada. 🙂")
        if offer_discount and not state.discount_offered(talk_id):
            if offer_is_ask:
                # Direct discount question -> answer it cleanly, ignore the model's take.
                reply = _OFFER_ASK
                state.mark_discount_offered(talk_id)
                log.info("talk=%s septico 5%% offer sent (direct ask) + locked", talk_id)
            elif reply and not handoff and "descuento" not in reply.lower():
                # Hesitation / soft goodbye -> keep the model's natural line, add the offer.
                reply = reply.rstrip() + _OFFER_TAIL
                state.mark_discount_offered(talk_id)
                log.info("talk=%s septico 5%% offer appended (hesitation) + locked", talk_id)

        # Post-generation phone number filter (belt-and-suspenders).
        # Best practice 2026 (Meta AI incident, Infobip guidelines): AI agents
        # must never share phone numbers in chat. Strip any pattern that looks
        # like a phone number from the outgoing reply before sending.
        # Covers Dominican (829/849/809), US (+1), and generic international.
        if reply:
            import re as _re
            _phone_pattern = _re.compile(
                r'(?:\+?1[-\s.]?)?(?:\(?\d{3}\)?[-\s.]?)?\d{3}[-\s.]?\d{4}\b'
            )
            _cleaned = _phone_pattern.sub("[número no disponible]", reply)
            if _cleaned != reply:
                log.warning("talk=%s PHONE_NUMBER_STRIPPED from reply — "
                            "LLM attempted to share contact info", talk_id)
                reply = _cleaned

        # Final supersession check before sending — catches cases where
        # the customer sent another message AFTER the debounce sleep completed
        # but BEFORE the LLM finished generating. Best practice: check at
        # every major boundary, not just after sleep.
        if msg_id and not state.is_latest_inbound(talk_id, msg_id):
            log.info("talk=%s superseded before send — reply discarded", talk_id)
            return
        if reply:
            await k.send_message(talk_id, reply)
            # Non-WhatsApp delivery warning: Kommo returns 202 Accepted but
            # Instagram/Facebook may silently fail (expired OAuth token, comment
            # vs DM mismatch, 24h window). Per Kommo docs: if delivery errors
            # persist, re-authorize the integration in Settings → Integrations.
            if not _is_waba and is_first:
                log.info(
                    "talk=%s non-WhatsApp first contact (%s) — "
                    "verify Kommo integration auth if delivery errors appear",
                    talk_id, _origin
                )

        # We just asked for the terrain location/linderos -> the next inbound
        # image is the customer's marked map, so arm the linderos-map path.
        if reply and "necesito la ubicación de su terreno" in reply.lower():
            state.set_awaiting_linderos(talk_id)

        entity_id = msg.get("entity_id") or msg.get("element_id")

        # Payment voice note fires BEFORE the bank details, only on a real deposit.
        if send_bank and audio_requested and audio_bot and entity_id:
            try:
                await k.run_bot(audio_bot, entity_id, _entity_type(msg))
                log.info("talk=%s launched payment-audio bot %s", talk_id, audio_bot)
                await asyncio.sleep(2)   # let the voice note land before the bank details
            except KommoError as e:
                log.error("talk=%s payment-audio launch failed: %s", talk_id, e)

        # Bank details in text (from the secret store), between the reply and
        # the account image, so the customer sees both. Never from the prompt.
        if send_bank and settings.bank_details_text:
            try:
                await k.send_message(talk_id, settings.bank_details_text)
            except KommoError as e:
                log.error("talk=%s bank-text send failed: %s", talk_id, e)

        # Auto-tag the lead by zone so the team can build per-sector lists.
        if _sector and entity_id:
            _parts = [p.strip() for p in _sector.split("|") if p.strip()]
            _tags = []
            if _parts:
                # Marker is [[SECTOR:Provincia|Pueblo]]. The town is the reliable
                # part (model extraction); the province is looked up deterministically
                # from dr_geo so the tag/price tier never ride on a geography guess.
                _town = _parts[1] if len(_parts) > 1 else _parts[0]
                _prov = dr_geo.province_for(_town) or (_parts[0] if len(_parts) > 1 else "")
                if _prov and dr_geo.province_for(_town) and len(_parts) > 1 and _prov != _parts[0]:
                    log.info("talk=%s province corrected %r -> %r for town %r",
                             talk_id, _parts[0], _prov, _town)
                if _prov:
                    _tags.append("Provincia: " + _prov)
                if len(_parts) > 1:
                    _tags.append("Pueblo: " + _town)
            # Update the lead name to include location so the pipeline
            # board is self-describing without opening the chat.
            if _tags and entity_id:
                _town_label = (_parts[1] if len(_parts) > 1 else _parts[0]) if _parts else ""
                _prov_label = _prov if _prov else ""
                if _town_label:
                    _lead_name = f"WhatsApp - {_town_label}, {_prov_label}".strip(", ")
                    try:
                        await k.update_lead(int(entity_id), name=_lead_name)
                        log.info("talk=%s lead name updated: %s", talk_id, _lead_name)
                    except Exception as _ln_e:
                        log.warning("talk=%s lead name update failed: %s", talk_id, _ln_e)
            for _tg in _tags:
                try:
                    await k.tag_lead_contact(entity_id, _tg)
                    log.info("talk=%s tagged contact %r", talk_id, _tg)
                except KommoError as e:
                    log.error("talk=%s contact tag failed: %s", talk_id, e)

        # Sequential multi-intent delivery: if multiple bots queued, fire each
        # with a human-like pause between them (3-5s). This addresses the case
        # where a customer asks two things at once (e.g. "mándeme el brochure y
        # dónde están ubicados") — both get answered in order, not skipped.
        # Best practice: independent intents delivered sequentially with pauses.
        #
        # If a voice bot already fired this turn (_voz_fired is set) AND there
        # are also image/sentinel bots queued, add an inter-system pause so the
        # voice note lands before the image arrives.
        if fire and _voz_fired:
            _vs_delay = random.uniform(3.0, 4.0)
            log.info("talk=%s voice+image multi-intent: %.1fs pause before "
                     "sentinel bots", talk_id, _vs_delay)
            await asyncio.sleep(_vs_delay)
        for _fire_idx, bot_id in enumerate(fire):
            if not entity_id:
                log.warning("talk=%s cannot launch bot %s: no entity_id",
                            talk_id, bot_id)
                continue
            # Add a pause between bots so each message lands before the next.
            # First bot fires immediately (text reply already sent above);
            # subsequent bots wait 3-5s so the customer reads/hears each one.
            if _fire_idx > 0:
                _inter_delay = random.uniform(3.0, 5.0)
                log.info("talk=%s multi-intent pause %.1fs before bot %s "
                         "(%d of %d)", talk_id, _inter_delay, bot_id,
                         _fire_idx + 1, len(fire))
                await asyncio.sleep(_inter_delay)
            try:
                await k.run_bot(bot_id, entity_id, _entity_type(msg))
                log.info("talk=%s launched salesbot %s (%d of %d)",
                         talk_id, bot_id, _fire_idx + 1, len(fire))
            except KommoError as e:
                log.error("talk=%s salesbot %s launch failed: %s",
                          talk_id, bot_id, e)

        if handoff:
            state.mark_handoff(talk_id, "agent_requested")
            await _signal_handoff(k, msg, talk_id, "agent_requested")
            log.info("talk=%s handed off by agent", talk_id)

        # One-time "still there?" follow-up: arm only when we answered and are now
        # waiting on the customer. Skip on handoff (a human is handling it), on the
        # deposit moment (they are off making the transfer), on the very first
        # welcome/ad turn (too pushy for a fresh lead), and when our reply was a
        # farewell (the conversation just closed naturally).
        _farewell = any(f in reply.lower() for f in (
            "buen día", "buen dia", "buen día!", "hasta luego", "hasta pronto",
            "excelente día", "excelente dia", "igualmente", "que le vaya"))
        if (reply and not is_first and not handoff and not send_bank
                and not _farewell and not _looks_like_closing(text)
                and not state.is_handed_off(talk_id)):
            try:
                _fu_delay = int(float(client_pack.behavior("followup_delay_minutes")) * 60)
            except Exception:
                _fu_delay = 0
            if _fu_delay > 0:
                state.arm_followup(talk_id, _fu_delay)

    except KommoError as e:
        log.error("talk=%s kommo error: %s", talk_id, e)
    except Exception:
        log.exception("talk=%s unhandled error", talk_id)
    finally:
        await k.aclose()
