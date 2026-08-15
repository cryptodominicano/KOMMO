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
from . import haiku as haiku_pre

# Per-talk reply locks: prevents double-reply race condition when two
# messages arrive within the debounce window. Only one reply can be
# in-flight per talk at a time.
_talk_locks: dict[str, asyncio.Lock] = {}
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


# Topic catalog: maps voice bot keys to the sales topics they cover.
# Written to the coverage ledger (covered_topics) when a Salesbot audio fires.
_AUDIO_TOPIC_MAP = {
    "VOZ_AGUA_1":       ["estudio_proceso", "estudio_precio", "perforacion_tipos"],
    "VOZ_AGUA_2":       ["perforacion_precio"],
    "VOZ_AGUA_3":       ["estudio_inicio", "ubicacion_como_enviar"],
    "VOZ_AGUA_4":       ["deposito_agua", "pago_proceso_agua"],
    "VOZ_AGUA_5":       ["precio_objecion_agua"],
    "VOZ_AGUA_6":       ["ubicacion_empresa"],
    "VOZ_AGUA_7":       ["pago_condiciones_agua"],
    "VOZ_AGUA_8":       ["llamada_coordinacion"],
    "[[VOZ_IMHOFF_1]]": ["dos_modulos", "precio_septico", "plastico_vs_cemento"],
    "[[VOZ_IMHOFF_2]]": ["deposito_septico", "entrega_proceso"],
    "[[VOZ_IMHOFF_3]]": ["precio_objecion_septico", "ventajas_plastico"],
    "[[VOZ_IMHOFF_4]]": ["confianza_empresa", "registro_mercantil"],
}

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

    # Customer just messaged -> they are active; cancel any pending nudges.
    # cancel_nudges uses lead_id; fall back to talk_id if entity_id not yet known.
    _cancel_id = str(msg.get("entity_id") or msg.get("element_id") or talk_id)
    state.cancel_nudges(_cancel_id)
    state.cancel_nudges(talk_id)  # also cancel any talk_id-keyed legacy rows

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
            state.advance_stage(talk_id, "greeting")
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
        # Skip the generic agua welcome image when the customer opened with
        # séptico keywords — the SEPTICO_COMPARATIVA image that fires with
        # VOZ_IMHOFF_1 is the correct visual welcome for that context.
        if welcome_bot and entity_id and is_first and not _septico_first:
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
            # Pacing: 1.5s after welcome image before voice fires
            # (BSP/Meta guidance: avoid stacking 3+ media in <2s)
            if is_first:
                await asyncio.sleep(1.5)
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

        # --- Séptico first contact: image → welcome text → audio ---
        # Correct sequence per client approval (2026-08-15):
        #   1. SEPTICO_COMPARATIVA image (fires immediately)
        #   2. Isla welcome text (1s later)
        #   3. VOZ_IMHOFF_1 audio (1.5s after text)
        # The image pair in _VOZ_IMAGE_PAIRS for VOZ_IMHOFF_1 is skipped
        # (comparativa already sent; guard key marked to prevent repeat).
        if (is_first and _has_septico_kw and _septico_first
                and entity_id and _is_waba
                and _imhoff_triggers.get("[[VOZ_IMHOFF_1]]")):
            _vk_i1 = "[[VOZ_IMHOFF_1]]"
            if not state.voice_already_sent(talk_id, _vk_i1):
                try:
                    # Step 1: SEPTICO_COMPARATIVA image first
                    # _sb is already defined above; bots dict not yet built at this point
                    _comp_bot = int((_sb.get("triggers") or {}).get("[[SEPTICO_COMPARATIVA]]") or 0)
                    if _comp_bot:
                        await k.run_bot(_comp_bot, entity_id, _entity_type(msg))
                        # Mark the image pair guard so VOZ_IMAGE_PAIR doesn't re-fire it
                        state.mark_voice_sent(talk_id, _vk_i1 + "_img")
                        log.info("talk=%s septico welcome: fired SEPTICO_COMPARATIVA %s",
                                 talk_id, _comp_bot)
                    # Step 2: Isla welcome text
                    await asyncio.sleep(1.0)
                    await k.send_message(
                        talk_id,
                        "¡Bienvenido! 😊 Con gusto le orientamos sobre "
                        "nuestras plantas sépticas IMHOFF."
                    )
                    log.info("talk=%s septico welcome text sent", talk_id)
                    # Step 3: VOZ_IMHOFF_1 audio
                    await asyncio.sleep(1.5)
                    await k.run_bot(int(_imhoff_triggers[_vk_i1]), entity_id, _entity_type(msg))
                    state.mark_voice_sent(talk_id, _vk_i1)
                    _voz_fired = _vk_i1
                    log.info("talk=%s launched VOZ_IMHOFF_1 %s",
                             talk_id, _imhoff_triggers[_vk_i1])
                except KommoError as e:
                    log.error("talk=%s VOZ_IMHOFF_1 sequence failed: %s", talk_id, e)

        # --- GPS pin OR a pasted Google Maps link: treat both as a location share ---
        # message_type == "location" is a first-class Kommo enum. Customers also
        # very often PASTE a Google Maps URL as text instead of sharing a pin; that
        # is still a location, so route it into the same linderos flow rather than
        # letting the model repeat "send me your location".
        maps_link = mtype == "text" and any(h in text.lower() for h in (
            "maps.app.goo.gl", "goo.gl/maps", "google.com/maps",
            "maps.google.", "/maps/place", "/maps?"))
        if mtype in location_types or maps_link:
            # Brief delay before location processing: if a text message
            # arrived simultaneously (e.g. customer typed location then
            # sent a pin), let the text message process first via the
            # per-talk lock, preventing a double-reply.
            if not is_first:
                await asyncio.sleep(3.0)
                if msg_id and not state.is_latest_inbound(talk_id, msg_id):
                    log.info("talk=%s location superseded — skipping", talk_id)
                    return
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
            # WhatsApp-native linderos flow (self-hosted app removed).
            # Send location_received message: team will send satellite photo,
            # customer marks boundaries with WhatsApp pencil and sends back.
            # Handoff so team knows to send the satellite photo.
            await k.send_message(talk_id, client_pack.msg("location_received"))
            state.mark_handoff(talk_id, "location_received")
            await _signal_handoff(k, msg, talk_id, "location_received")
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
                log.info("talk=%s superseded by a newer message (lock) - skipping",
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
            # Collect ALL matched agua voice bots then fire sequentially.
            # 5s pause between each so customer hears them in order.
            _agua_to_fire = []
            for _vk, _kws in _VOZ_KW:
                if any(kw in _tna for kw in _kws):
                    if not state.voice_already_sent(talk_id, _vk):
                        _bid = _voz_triggers.get(_vk)
                        if _bid:
                            _agua_to_fire.append((_vk, int(_bid)))
            for _idx_a, (_vk, _bid) in enumerate(_agua_to_fire):
                if _idx_a > 0:
                    await asyncio.sleep(5.0)
                try:
                    await k.run_bot(_bid, entity_id, _entity_type(msg))
                    state.mark_voice_sent(talk_id, _vk)
                    _voz_fired = _vk  # last fired = followup text source
                    log.info("talk=%s launched %s bot %s (%d of %d)",
                             talk_id, _vk, _bid, _idx_a + 1, len(_agua_to_fire))
                    # Coverage ledger: log all topics this audio covers
                    _cov_lead = str(entity_id) if entity_id else talk_id
                    for _topic in _AUDIO_TOPIC_MAP.get(_vk, []):
                        state.mark_topic_covered(_cov_lead, _topic,
                                                'audio', source=_vk)
                except KommoError as e:
                    log.error("talk=%s %s failed: %s", talk_id, _vk, e)

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
                    "quiero comprar","como la compro",
                    "que debo hacer","cual es el proceso","como procedo",
                    "quiero adquirir una","que necesito","como hacemos",
                    "quiero ordenar","quiero hacer el pedido","estoy listo",
                    "que sigue","cual es el siguiente paso","como hago el pago",
                    "como se entrega","cuanto tarda","como llega",
                    "hacen envios","la instalan","que incluye",
                    "que tengo que enviar","quiero reservar una",
                ]),
                # Pure location questions → VOZ_AGUA_6 (Jarabacoa, serve whole country).
                # Reuses the agua location audio — content is company-level, not product-specific.
                # No Wellington sequence; this is just a "where are you" answer.
                ("VOZ_AGUA_6", [
                    "donde estan ubicados","donde estan","tienen oficina",
                    "donde puedo visitarlos","cual es la direccion",
                    "puedo pasar","donde queda","en que ciudad estan",
                    "donde los encuentro","donde queda la oficina",
                    "donde trabajan","en que provincia estan",
                    "donde operan","donde puedo ir",
                ]),
                # Trust/credibility questions → VOZ_IMHOFF_4 (registro mercantil,
                # sells from factory, CEO available) + Wellington photo sequence.
                ("[[VOZ_IMHOFF_4]]", [
                    "no me gusta pagar por internet",
                    "no confio en transferir","quiero ver el producto primero",
                    "quiero conocerlos antes","son una empresa real",
                    "tienen oficina fisica","donde puedo ver las plantas",
                    "quiero asegurarme antes de pagar","como se que son confiables",
                    "tienen referencias","tienen redes sociales",
                    "donde puedo ver sus trabajos","quienes son ustedes",
                    "desde hace cuanto trabajan","quien es el ingeniero",
                    "quien es wellington","son confiables",
                    "como verifico","quiero verificar","registro mercantil",
                    "puedo ir a conocerlos","quiero pasar a verlos",
                    "quiero ir personalmente",
                    "empresa verdadera","empresa legitima","empresa legal",
                    "son legitimos","son de fiar","son reales",
                    "como se que son","como saber si","verificar que son",
                    "empresa registrada","tienen registro","estan registrados",
                ]),
            ]
            if _is_septico_flow:
                # Collect ALL matched IMHOFF voice bots then fire sequentially.
                # 5s pause between each. VOZ_IMHOFF_4 sequence fires after its audio.
                _imhoff_to_fire = []
                for _vk_i, _kws_i in _IMHOFF_KW:
                    if any(kw in _tna_i for kw in _kws_i):
                        if not state.voice_already_sent(talk_id, _vk_i):
                            _bid_i = (_voz_triggers.get(_vk_i)
                                      if _vk_i.startswith("VOZ_AGUA")
                                      else _imhoff_triggers.get(_vk_i))
                            if _bid_i:
                                _imhoff_to_fire.append((_vk_i, int(_bid_i)))
                for _idx_i, (_vk_i, _bid_i) in enumerate(_imhoff_to_fire):
                    if _idx_i > 0:
                        await asyncio.sleep(5.0)
                    try:
                        await k.run_bot(_bid_i, entity_id, _entity_type(msg))
                        state.mark_voice_sent(talk_id, _vk_i)
                        _voz_fired = _vk_i  # last fired = followup text source
                        log.info("talk=%s launched %s bot %s (%d of %d)",
                                 talk_id, _vk_i, _bid_i, _idx_i + 1,
                                 len(_imhoff_to_fire))
                        # Coverage ledger: log all topics this audio covers
                        _cov_lead_i = str(entity_id) if entity_id else talk_id
                        for _topic_i in _AUDIO_TOPIC_MAP.get(_vk_i, []):
                            state.mark_topic_covered(_cov_lead_i, _topic_i,
                                                     'audio', source=_vk_i)
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
                            log.info("talk=%s sent Instagram text (VOZ_IMHOFF_4)",
                                     talk_id)
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
                                    log.error("talk=%s Wellington bot failed: %s",
                                              talk_id, e)
                    except KommoError as e:
                        log.error("talk=%s %s failed: %s", talk_id, _vk_i, e)

        # --- RAG + LLM ---
        kb = await rag.retrieve(text)
        history = await _history(k, talk_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]               # current message passed separately
        extra = ""
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
            # Best practice 2026 (Botpress, Infobip): never bypass the LLM
            # for messages containing a question mark — those are genuine
            # queries that deserve a real answer. Also never bypass for
            # transcribed voice notes (mtype in audio_types) — the customer
            # is actively speaking, not giving a closed one-word response.
            _is_genuine_question = (
                "?" in text or
                mtype in audio_types
            )
            _is_short_closed = (
                not _is_genuine_question and
                (
                    len(text) < 30 or
                    any(r in _tna_previo for r in _CLOSED_RESPONSES)
                )
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


        # Cultural opener: warm Dominican register, acknowledges audio landed.
        # Fires after every voice note as part of the followup text.
        # Rotating warm closers — no audio reference, varied so it never reads robotic.
        # Each bot gets its own closer matched to the conversation moment.
        _VOZ_FOLLOWUPS = {
            "VOZ_AGUA_1": "Por favor mándeme la ubicación de donde desea realizar el estudio. 📍",
            "VOZ_AGUA_2": "Con gusto le ayudamos a tomar la mejor decisión. 😊 ¿Le gustaría comenzar con el estudio para tener toda la información? 🙏",
            "VOZ_AGUA_3": "Cuando guste, mándeme la ubicación de su terreno y seguimos el proceso desde ahí. 📍",
            "VOZ_AGUA_4": "A la orden para lo que necesite. 🙏 ¿Tiene alguna pregunta o está listo para que le envíe los datos del depósito?",
            "VOZ_AGUA_5": "Estamos aquí para orientarle. 😊 ¿Le gustaría proceder con el estudio o tiene alguna consulta antes de decidir? 🙏",
            "VOZ_AGUA_6": ("Cualquier consulta que tenga, aquí estamos. 🙏 ¿Desea avanzar?"
                           if _is_septico_flow else
                           "Con mucho gusto le cotizamos. 😊 ¿En qué pueblo o sector desea realizar el estudio? 🙏"),
            "VOZ_AGUA_7": "A la orden. 😊 ¿Está listo para dar el primer paso o tiene alguna consulta adicional? 🙏",
            "VOZ_AGUA_8": "¡Con gusto coordinamos! ¿Qué hora le queda bien para la llamada? 🙏",
            "[[VOZ_IMHOFF_1]]": "¿Cuántos baños tiene su propiedad? Con eso le indico el módulo que necesita. 🙏",
            "[[VOZ_IMHOFF_2]]": "A la orden para ayudarle. 😊 ¿Está listo para proceder con el depósito de RD$10,000 o tiene alguna pregunta? 🙏",
            "[[VOZ_IMHOFF_3]]": "Estamos aquí para lo que necesite. 🙏 ¿Le gustaría proceder con su planta o tiene alguna consulta antes de decidir?",
            # VOZ_IMHOFF_4: no text followup — Instagram text + Wellington photo handle the close.
            "[[VOZ_IMHOFF_4]]": "",
        }
        # Voice → image pairs: after a voice note fires, send its paired image bot
        # with a 4s pause so the voice note lands before the image.
        # Keyed by the voice bot sentinel; value is the image sentinel to look up
        # in the bots dict (same dict used by the main sentinel loop).
        _VOZ_IMAGE_PAIRS = {
            # First séptico contact → comparativa image (IMHOFF vs traditional)
            "[[VOZ_IMHOFF_1]]": "[[SEPTICO_COMPARATIVA]]",
            # How it works / purchase process → funcionamiento brochure
            "[[VOZ_IMHOFF_2]]": "[[SEPTICO_FUNCIONAMIENTO]]",
            # Price objection → ventajas comparison image
            "[[VOZ_IMHOFF_3]]": "[[SEPTICO_VENTAJAS]]",
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
        # Fire paired image bot if this voice note has one.
        # Sentinel stored now; bot_id resolved after `bots` is defined below.
        _voz_image_sentinel = _VOZ_IMAGE_PAIRS.get(_voz_fired) if _voz_fired else None
        # _voz_image_bot_id resolved after bots dict is built (line ~1070)

        if _direct_reply:
            reply = _direct_reply
        else:
            # ── HAIKU PRE-PROCESSOR ──────────────────────────────────────────
            # R1: extracts all intents, builds multi-intent coverage contract
            # R2: detects adjacent_out_of_scope, injects redirect instruction
            _flow_label = "septico" if _is_septico_flow else "agua"
            _current_stage = state.get_stage(talk_id)
            _stage_inj = (
                f"ESTADO ACTUAL: flujo={_flow_label}, etapa={_current_stage}. "
                f"Avanza hacia la siguiente etapa en la conversación."
            )
            extra = (_stage_inj + "\n\n" + extra).strip() if extra else _stage_inj
            # STATE BLOCK: inject coverage ledger so LLM knows what's been covered
            _cov_lead_id = str(entity_id) if entity_id else talk_id
            _coverage_block = state.build_coverage_state_block(_cov_lead_id)
            if _coverage_block:
                extra = (_coverage_block + "\n\n" + extra).strip()
                log.debug("talk=%s coverage block injected (%d topics)",
                          talk_id, len(_coverage_block.splitlines()) - 1)
            _intents = await haiku_pre.classify(text, flow=_flow_label)
            log.info("talk=%s haiku intents: %s", talk_id,
                     [{"scope": i["scope"], "text": i["text"][:40]}
                      for i in _intents])

            # ── HAIKU VOICE-BOT ROUTING (Tier 1 — semantic routing) ──────
            # Research Aug 2026: keyword recall collapses to 11-13% on nuanced
            # intents. Haiku's voz_bot_intents replace keyword lists for all
            # non-unambiguous intents. Fires if confidence >= threshold.
            # Runs AFTER keyword loop — only fires if keyword didn't already
            # fire a bot this turn (_voz_fired is still None here).
            _HAIKU_VOZ_MAP = {
                # agua bots
                "drilling_price":          ("VOZ_AGUA_2", _voz_triggers, 0.70),
                "how_to_start":            ("VOZ_AGUA_3", _voz_triggers, 0.65),
                "payment_agua":            ("VOZ_AGUA_4", _voz_triggers, 0.70),
                "price_objection_agua":    ("VOZ_AGUA_5", _voz_triggers, 0.70),
                "location_agua":           ("VOZ_AGUA_6", _voz_triggers, 0.65),
                "payment_conditions":      ("VOZ_AGUA_7", _voz_triggers, 0.65),
                "call_request":            ("VOZ_AGUA_8", _voz_triggers, 0.70),
                # septico bots
                "purchase_process_septico": ("[[VOZ_IMHOFF_2]]", _imhoff_triggers, 0.70),
                "price_objection_septico":  ("[[VOZ_IMHOFF_3]]", _imhoff_triggers, 0.70),
                "trust_question":           ("[[VOZ_IMHOFF_4]]", _imhoff_triggers, 0.70),
                "location_septico":         ("VOZ_AGUA_6",       _voz_triggers,    0.65),
            }
            if not is_first and _is_waba and entity_id and not _voz_fired:
                _haiku_voz = haiku_pre.get_voz_bot_intents(_intents)
                if _haiku_voz:
                    log.info("talk=%s haiku voz_bot_intents: %s", talk_id, _haiku_voz)
                _haiku_fired = []
                for _hv in _haiku_voz:
                    _hv_intent = _hv["intent"]
                    _hv_conf = _hv["confidence"]
                    if _hv_intent not in _HAIKU_VOZ_MAP:
                        continue
                    _hv_key, _hv_triggers, _hv_threshold = _HAIKU_VOZ_MAP[_hv_intent]
                    if _hv_conf < _hv_threshold:
                        log.info("talk=%s haiku voz SKIP %s conf=%.2f < %.2f",
                                 talk_id, _hv_intent, _hv_conf, _hv_threshold)
                        continue
                    if state.voice_already_sent(talk_id, _hv_key):
                        continue
                    _hv_bid = _hv_triggers.get(_hv_key)
                    if not _hv_bid:
                        continue
                    # Check not already queued by keyword loop
                    # Safe: these lists may not exist if keyword block condition was False
                    _kw_fired_keys = [k for k, _ in
                                      (locals().get('_agua_to_fire', []) if not _is_septico_flow
                                       else locals().get('_imhoff_to_fire', []))]
                    if _hv_key in _kw_fired_keys:
                        continue
                    _haiku_fired.append((_hv_key, int(_hv_bid), _hv_intent, _hv_conf))
                # Fire all matched bots sequentially with 5s pauses
                for _hi, (_hv_key, _hv_bid, _hv_intent, _hv_conf) in enumerate(_haiku_fired):
                    if _hi > 0:
                        await asyncio.sleep(5.0)
                    try:
                        await k.run_bot(_hv_bid, entity_id, _entity_type(msg))
                        state.mark_voice_sent(talk_id, _hv_key)
                        _voz_fired = _hv_key
                        log.info("talk=%s HAIKU_VOZ: fired %s (intent=%s conf=%.2f)",
                                 talk_id, _hv_key, _hv_intent, _hv_conf)
                        # Coverage ledger
                        _cov_lead_h = str(entity_id) if entity_id else talk_id
                        for _topic_h in _AUDIO_TOPIC_MAP.get(_hv_key, []):
                            state.mark_topic_covered(_cov_lead_h, _topic_h,
                                                    'audio', source=_hv_key)
                        # VOZ_IMHOFF_4 Wellington sequence
                        if _hv_key == "[[VOZ_IMHOFF_4]]":
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
                            await asyncio.sleep(1)
                            _wbot = int(_imhoff_triggers.get(
                                "wellington_lider_foto_bot_id", 0) or 0)
                            if _wbot:
                                try:
                                    await k.run_bot(_wbot, entity_id, _entity_type(msg))
                                    log.info("talk=%s HAIKU_VOZ Wellington bot %s",
                                             talk_id, _wbot)
                                except KommoError as e:
                                    log.error("talk=%s Wellington bot failed: %s",
                                              talk_id, e)
                    except KommoError as e:
                        log.error("talk=%s HAIKU_VOZ bot %s failed: %s",
                                  talk_id, _hv_bid, e)

            # Multi-intent: build coverage contract for GPT-4.1
            _multi_prompt = haiku_pre.build_multi_intent_prompt(_intents)
            if _multi_prompt:
                extra = (_multi_prompt + "\n\n" + extra).strip()
                log.info("talk=%s multi-intent coverage injected", talk_id)

            # ── FAREWELL DETECTION (MINITS framework, Research Aug 2026) ──────
            # soft_farewell: latent objection disguised as goodbye.
            # Research: 'Lo voy a pensar' is almost never a true no.
            # One diagnostic probe is warranted. Never two. Never three.
            # hard_no: explicit opt-out — close gracefully, no probe.
            if haiku_pre.is_hard_no(_intents):
                # Explicit rejection — inject graceful close instruction
                extra = (
                    "CIERRE DEFINITIVO: El cliente rechazó explícitamente o "
                    "pidió no ser contactado. Responde con UNA sola despedida "
                    "cálida y breve. Sin preguntas. Sin ofertas. Sin marcadores."
                    + ("\n\n" + extra if extra else "")
                ).strip()
                log.info("talk=%s hard_no detected — graceful close", talk_id)

            elif haiku_pre.is_soft_farewell(_intents):
                # Soft farewell / latent objection — ONE diagnostic probe.
                # MINITS signals already processed by Haiku. Inject probe
                # instruction so GPT-4.1 asks the isolate-the-objection
                # question, then closes if no reply (handled next turn).
                extra = (
                    "OBJECIÓN LATENTE DETECTADA: El cliente se está despidiendo "
                    "de forma vaga. Per MINITS research esto es casi nunca un no "
                    "definitivo. Haz UNA SOLA pregunta diagnóstica cálida para "
                    "aislar la objeción real. Ejemplos: '¿Qué parte necesita "
                    "pensar exactamente? ¿Es el precio, el proceso, o algo que "
                    "no le quedó claro?' o '¿Le gustaría que le escriba en un "
                    "par de días para ver si surgieron dudas?' "
                    "UNA pregunta. Tono cálido y sin presión. "
                    "NO digas que lo entiendes y punto — pregunta algo."
                    + ("\n\n" + extra if extra else "")
                ).strip()
                log.info("talk=%s soft_farewell — MINITS probe injected", talk_id)

            # Adjacent out-of-scope: inject one-turn redirect
            if haiku_pre.has_adjacent_out_of_scope(_intents):
                _adj = "; ".join(i["text"] for i in _intents
                                 if i["scope"] == "adjacent_out_of_scope")
                extra = (
                    f"REDIRECT REQUERIDO: El cliente mencionó un tema adyacente "
                    f"({_adj[:80]}) que NO es el servicio actual ({_flow_label}). "
                    f"Reconoce en UNA línea y vuelve al flujo activo. "
                    f"NO des información sobre el servicio adyacente."
                    + ("\n\n" + extra if extra else "")
                ).strip()
                log.info("talk=%s adjacent redirect injected: %s",
                         talk_id, _adj[:60])

            reply = await agent.generate(text, kb, history, extra)
        if not reply:
            log.warning("talk=%s empty model reply", talk_id)
            return

        # Model signals handoff with a sentinel; the pause is enforced here.
        handoff = marker in reply
        reply = reply.replace(marker, "").strip()

        # IMAGE WORKAROUND: send_message is text-only, but a Salesbot can attach
        # images. The model emits a sentinel; we strip it and launch the bot.
        sb = client_pack.pack().get("salesbot", {})
        bots = sb.get("triggers", {})
        # Resolve paired image bot id now that bots dict is available
        _voz_image_bot_id = int(bots.get(_voz_image_sentinel) or 0) if _voz_image_sentinel else 0
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
        # VOZ → IMAGE PAIR: fire paired image bot 4s after voice+text delivered.
        # Only fires on WhatsApp (waba), only if entity_id is known,
        # only if the paired bot is configured, and only once per conversation
        # (reuses the voice_sent guard with a "_img" suffix key).
        if _voz_image_bot_id and entity_id and _is_waba:
            _img_guard_key = (_voz_fired or "") + "_img"
            if not state.voice_already_sent(talk_id, _img_guard_key):
                try:
                    await asyncio.sleep(4.0)
                    await k.run_bot(_voz_image_bot_id, entity_id, _entity_type(msg))
                    state.mark_voice_sent(talk_id, _img_guard_key)
                    log.info("talk=%s VOZ_IMAGE_PAIR: fired %s (bot %s) after %s",
                             talk_id, _voz_image_sentinel, _voz_image_bot_id, _voz_fired)
                except KommoError as e:
                    log.error("talk=%s VOZ_IMAGE_PAIR bot %s failed: %s",
                              talk_id, _voz_image_bot_id, e)

        # BELT-AND-SUSPENDERS: séptico image marker injection.
        # Sentinel firing measured ~80-90% (context log, 2026-07-17 and proven again
        # 2026-08-15 with the ficha técnica miss). When the model describes sending an
        # image in text WITHOUT emitting the marker, the customer gets a broken promise.
        # Defence: if the reply contains a phrase that IMPLIES a séptico image was meant
        # to accompany it but the marker is absent, inject it deterministically.
        # Only applies when we are in the séptico flow — prevents false positives on
        # agua conversations that happen to mention these words.
        if _is_septico_flow:
            _SEPTICO_FALLBACKS = [
                # (phrase_in_reply, marker_to_inject)
                # Ficha técnica — installation guide
                ("ficha técnica", "[[SEPTICO_FICHA]]"),
                ("ficha tecnica", "[[SEPTICO_FICHA]]"),
                # Funcionamiento — how-it-works brochure
                ("funcionamiento", "[[SEPTICO_FUNCIONAMIENTO]]"),
                ("cómo funciona la planta", "[[SEPTICO_FUNCIONAMIENTO]]"),
                ("como funciona la planta", "[[SEPTICO_FUNCIONAMIENTO]]"),
                # Ventajas — price objection / comparison image
                ("ventajas", "[[SEPTICO_VENTAJAS]]"),
                ("más durable", "[[SEPTICO_VENTAJAS]]"),
                ("mas durable", "[[SEPTICO_VENTAJAS]]"),
                ("no se cuartea", "[[SEPTICO_VENTAJAS]]"),
                ("no contamina", "[[SEPTICO_VENTAJAS]]"),
            ]
            _reply_lower = reply.lower()
            for _phrase, _marker in _SEPTICO_FALLBACKS:
                if _phrase in _reply_lower and _marker not in reply:
                    # Check that the corresponding bot is actually configured
                    if _marker in bots and bots[_marker]:
                        reply = reply.rstrip() + " " + _marker
                        log.warning(
                            "talk=%s SENTINEL_FALLBACK: injected %s (phrase=%r was "
                            "in reply without marker)",
                            talk_id, _marker, _phrase
                        )
                        break  # one injection per turn maximum

        for sentinel, bot_id in bots.items():
            if sentinel in reply:
                reply = reply.replace(sentinel, "").strip()
                if bot_id:
                    fire.append(int(bot_id))
                else:
                    log.warning("sentinel %s has no bot_id configured", sentinel)

        # Post-generation filters (belt-and-suspenders).
        if reply:
            import re as _re
            # Filter 1: Phone numbers — never share in chat.
            # DR-specific phone regex (research: Nacimiento-García et al. 2024)
            # Catches: +1-829-566-7542, (809) 566-7542, 8295667542
            # Negative lookaheads: excludes prices (RD$45,000), times (14:30),
            # dates (08/29/2025), and module numbers (Módulo 8)
            _phone_pattern = _re.compile(
                r'(?<![0-9$])'
                r'(?:\+?1[-\s.]?)?'
                r'\(?(?:8(?:0[09]|[24]9))\)?'
                r'[-\s.]?\d{3}[-\s.]?\d{4}'
                r'(?![\d/\-])'
            )
            _cleaned = _phone_pattern.sub("[número no disponible]", reply)
            if _cleaned != reply:
                log.warning("talk=%s PHONE_NUMBER_STRIPPED from reply", talk_id)
                reply = _cleaned
            # Filter 2: Markdown bold (**text**) — WhatsApp chat is not markdown.
            # GPT-4.1 occasionally uses bold despite prompt instructions.
            _md_bold = _re.compile(r'\*\*([^*]+)\*\*')
            _cleaned2 = _md_bold.sub(r'\1', reply)
            if _cleaned2 != reply:
                log.info("talk=%s MARKDOWN_STRIPPED bold from reply", talk_id)
                reply = _cleaned2

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
        # Stage tracking: deposit bot fired = deposit_requested
        if fire:
            _dep_bot = int((client_pack.pack().get("salesbot") or {}).get("deposit_bot_id") or 0)
            if _dep_bot and _dep_bot in fire:
                _old_stg = state.get_stage(talk_id)
                state.advance_stage(talk_id, "deposit_requested")
                state.log_stage_transition(talk_id, _old_stg, "deposit_requested")
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
            _old_stg3 = state.get_stage(talk_id)
            state.advance_stage(talk_id, "handoff")
            state.log_stage_transition(talk_id, _old_stg3, "handoff")
            state.mark_handoff(talk_id, "agent_requested")
            await _signal_handoff(k, msg, talk_id, "agent_requested")
            log.info("talk=%s handed off by agent (stage: %s → handoff)",
                     talk_id, _old_stg3)

        # One-time "still there?" follow-up: arm only when we answered and are now
        # waiting on the customer. Skip on handoff (a human is handling it), on the
        # deposit moment (they are off making the transfer), on the very first
        # welcome/ad turn (too pushy for a fresh lead), and when our reply was a
        # farewell (the conversation just closed naturally).
        _farewell = any(f in reply.lower() for f in (
            "buen día", "buen dia", "buen día!", "hasta luego", "hasta pronto",
            "excelente día", "excelente dia", "igualmente", "que le vaya"))
        # ── Scenario-specific nudge scheduling ───────────────────────────────
        # Wrapped in try/except: a nudge scheduling crash must NEVER prevent
        # the sentinel processing block below from running. Sentinels fire image
        # bots; a crash here is what caused [[SEPTICO_FICHA]] to not fire.
        _nudge_lead_id = str(entity_id) if entity_id else talk_id
        _nudge_scheduled = False
        try:
            # Scenario: bathrooms — séptico flow only, priority 5, 15 min.
            _BATHROOM_PHRASES = (
                "cuántos baños", "cuantos banos", "cuantos baños", "cuántos banos",
            )
            _reply_lower_fu = reply.lower()
            _bathroom_asked = (
                _is_septico_flow
                and any(p in _reply_lower_fu for p in _BATHROOM_PHRASES)
            )
            if _bathroom_asked and not is_first and not handoff and not state.is_handed_off(talk_id):
                import time as _time
                state.schedule_nudge(
                    lead_id=_nudge_lead_id,
                    talk_id=talk_id,
                    scenario="bathrooms",
                    message="Quedo atento a tu respuesta para entender sus necesidades. 🙏",
                    delay_seconds=15 * 60,
                    priority=5,
                    last_inbound_at=_time.time(),
                )
                _nudge_scheduled = True
                log.info("talk=%s nudge scheduled (scenario=bathrooms, 15 min)", talk_id)

            # Generic fallback nudge
            if (reply and not is_first and not handoff and not send_bank
                    and not _farewell and not _looks_like_closing(text)
                    and not state.is_handed_off(talk_id)
                    and not _nudge_scheduled):
                try:
                    _fu_delay = int(float(client_pack.behavior("followup_delay_minutes")) * 60)
                except Exception:
                    _fu_delay = 0
                if _fu_delay > 0:
                    _default_nudge_msg = (
                        client_pack.pack().get("messages", {}) or {}
                    ).get("followup_nudge") or ""
                    import time as _time
                    state.schedule_nudge(
                        lead_id=_nudge_lead_id,
                        talk_id=talk_id,
                        scenario="generic",
                        message=_default_nudge_msg,
                        delay_seconds=_fu_delay,
                        priority=9,
                        last_inbound_at=_time.time(),
                    )
        except Exception as _nudge_err:
            log.warning("talk=%s nudge scheduling failed (non-fatal): %s",
                        talk_id, _nudge_err)

    except KommoError as e:
        log.error("talk=%s kommo error: %s", talk_id, e)
    except Exception:
        log.exception("talk=%s unhandled error", talk_id)
    finally:
        await k.aclose()
