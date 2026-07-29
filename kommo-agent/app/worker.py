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
        log.info("talk=%s handoff signalled (stage+task), reason=%s", talk_id, reason)
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

    if not talk_id:
        log.warning("no talk_id, skipping msg=%s", msg_id)
        return

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
        is_first = state.first_contact(talk_id)   # marks first contact; reused to exempt the greeting from the typing delay
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
            log.info("talk=%s septico first-contact - skipping generic welcome image", talk_id)
        if welcome_bot and entity_id and is_first and not _septico_first:
            try:
                await k.run_bot(int(welcome_bot), entity_id, _entity_type(msg))
                log.info("talk=%s launched welcome bot %s", talk_id, welcome_bot)
            except KommoError as e:
                log.error("talk=%s welcome bot launch failed: %s", talk_id, e)

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
                await k.send_message(talk_id, client_pack.msg(key))
                state.mark_handoff(talk_id, f"media_received:{mtype}")
                await _signal_handoff(k, msg, talk_id, f"media_received:{mtype}")
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
                await asyncio.sleep(random.uniform(min(lo, hi), max(lo, hi)))
            if msg_id and not state.is_latest_inbound(talk_id, msg_id):
                log.info("talk=%s superseded by a newer message - skipping reply",
                         talk_id)
                return

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

        if reply:
            await k.send_message(talk_id, reply)

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
            for _tg in _tags:
                try:
                    await k.tag_lead_contact(entity_id, _tg)
                    log.info("talk=%s tagged contact %r", talk_id, _tg)
                except KommoError as e:
                    log.error("talk=%s contact tag failed: %s", talk_id, e)

        for bot_id in fire:
            if not entity_id:
                log.warning("talk=%s cannot launch bot %s: no entity_id", talk_id, bot_id)
                continue
            try:
                await k.run_bot(bot_id, entity_id, _entity_type(msg))   # one bot per entity
                log.info("talk=%s launched salesbot %s for entity %s",
                         talk_id, bot_id, entity_id)
            except KommoError as e:
                log.error("talk=%s salesbot %s launch failed: %s", talk_id, bot_id, e)

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
