"""Background processing. Runs AFTER the webhook has already been acked.

Client-agnostic: every Spanish string and channel value comes from the client
pack (clients/<id>/client.toml). Onboarding a client is a new directory.
"""
import logging
from . import rag, agent, state, client as client_pack
from .kommo import KommoClient, KommoError
from .transcribe import download_audio, transcribe, TranscriptionRejected
from .config import settings

log = logging.getLogger("worker")


def _entity_type(msg: dict) -> str:
    """Webhook says "lead" (singular); POST /bots/{id}/run wants "leads" (plural)."""
    t = str(msg.get("entity_type") or "lead").lower()
    return t if t.endswith("s") else t + "s"


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

    # Handoff is enforced in CODE. Once a human owns the talk, we are silent.
    if state.is_handed_off(talk_id):
        log.info("talk=%s handed off - staying silent", talk_id)
        return

    location_types = set(client_pack.behavior("location_types"))
    audio_types = set(client_pack.behavior("audio_types"))
    media_types = set(client_pack.behavior("media_types"))
    marker = client_pack.behavior("handoff_marker")

    k = KommoClient()
    try:
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
            await k.send_message(talk_id, client_pack.msg("location_received"))
            state.mark_handoff(talk_id, "location_shared")
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
            log.info("talk=%s inbound media (%s) - ack + handoff", talk_id, mtype)
            await k.send_message(talk_id, client_pack.msg("media_received"))
            state.mark_handoff(talk_id, f"media_received:{mtype}")
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
        if trigger_text and trigger_text in reply:
            if deposit_bot and not state.first_deposit(talk_id):
                # Already sent for this talk. Repeats are either a model loop
                # or someone farming the image; either way, once is enough.
                log.warning("talk=%s deposit bot already fired - suppressed", talk_id)
            elif deposit_bot:
                fire.append(int(deposit_bot))
                log.info("talk=%s order message sent - firing deposit bot %s",
                         talk_id, deposit_bot)
            else:
                # Loud: the customer was just promised bank details we cannot send.
                log.error("talk=%s ORDER MESSAGE SENT BUT deposit_bot_id IS 0 - "
                          "customer promised bank details and will get none",
                          talk_id)
        for sentinel, bot_id in bots.items():
            if sentinel in reply:
                reply = reply.replace(sentinel, "").strip()
                if bot_id:
                    fire.append(int(bot_id))
                else:
                    log.warning("sentinel %s has no bot_id configured", sentinel)

        if reply:
            await k.send_message(talk_id, reply)

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
            log.info("talk=%s handed off by agent", talk_id)

    except KommoError as e:
        log.error("talk=%s kommo error: %s", talk_id, e)
    except Exception:
        log.exception("talk=%s unhandled error", talk_id)
    finally:
        await k.aclose()
