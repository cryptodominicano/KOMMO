"""Background processing. Runs AFTER the webhook has already been acked.

Client-agnostic: every Spanish string and channel value comes from the client
pack (clients/<id>/client.toml). Onboarding a client is a new directory.
"""
import logging
import time
from . import rag, agent, state, client as client_pack
from .kommo import KommoClient, KommoError
from .transcribe import download_audio, transcribe, TranscriptionRejected
from . import linderos
from .config import settings

log = logging.getLogger("worker")


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


async def handle_message(msg: dict) -> None:
    talk_id = str(msg.get("talk_id") or "")
    msg_id = str(msg.get("id") or "")
    mtype = (msg.get("message_type") or "text").lower()
    text = (msg.get("text") or "").strip()

    if not talk_id:
        log.warning("no talk_id, skipping msg=%s", msg_id)
        return

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
        if welcome_bot and entity_id and state.first_contact(talk_id):
            try:
                await k.run_bot(int(welcome_bot), entity_id, _entity_type(msg))
                log.info("talk=%s launched welcome bot %s", talk_id, welcome_bot)
            except KommoError as e:
                log.error("talk=%s welcome bot launch failed: %s", talk_id, e)

        # --- GPS pin: recognize, send verbatim text, hand off, stop ---
        # message_type == "location" is a first-class Kommo enum. On the previous
        # platform this arrived as the opaque string "[Unsupported message]" and
        # had to be pattern-matched. Deterministic here - no model judgment.
        if mtype in location_types:
            log.info("talk=%s location pin received", talk_id)
            entity_id = msg.get("entity_id") or msg.get("element_id")
            first = state.linderos_first(talk_id) if entity_id else False
            if entity_id and settings.public_base_url and first:
                # First pin: send the drawing link.
                link = linderos.build_link(entity_id, talk_id, settings.client_id)
                await k.send_message(
                    talk_id, client_pack.msg("linderos_invite") + "\n\n" + link)
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
            # Only call it a "comprobante" if a deposit was actually presented;
            # otherwise it is some other image (terrain photo, etc.) - neutral ack.
            is_receipt = state.deposit_was_presented(talk_id)
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

        # --- RAG + LLM ---
        kb = await rag.retrieve(text)
        history = await _history(k, talk_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]               # current message passed separately
        reply = await agent.generate(text, kb, history)
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
        if deposit_requested:
            if not deposit_bot:
                log.error("talk=%s DEPOSIT MESSAGE SENT BUT deposit_bot_id IS 0 - "
                          "customer promised bank details and will get none", talk_id)
            elif not state.deposit_cooldown_ok(talk_id):
                log.warning("talk=%s deposit within cooldown - suppressed", talk_id)
            else:
                fire.append(int(deposit_bot))
                send_bank = True
                log.info("talk=%s deposit message sent - firing bank text + photo %s",
                         talk_id, deposit_bot)
        for sentinel, bot_id in bots.items():
            if sentinel in reply:
                reply = reply.replace(sentinel, "").strip()
                if bot_id:
                    fire.append(int(bot_id))
                else:
                    log.warning("sentinel %s has no bot_id configured", sentinel)

        if reply:
            await k.send_message(talk_id, reply)

        # Bank details in text (from the secret store), between the reply and
        # the account image, so the customer sees both. Never from the prompt.
        if send_bank and settings.bank_details_text:
            try:
                await k.send_message(talk_id, settings.bank_details_text)
            except KommoError as e:
                log.error("talk=%s bank-text send failed: %s", talk_id, e)

        entity_id = msg.get("entity_id") or msg.get("element_id")
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

    except KommoError as e:
        log.error("talk=%s kommo error: %s", talk_id, e)
    except Exception:
        log.exception("talk=%s unhandled error", talk_id)
    finally:
        await k.aclose()
