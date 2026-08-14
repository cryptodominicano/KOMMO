"""Handoff + dedupe state, in SQLite.

WHY SQLITE, NOT A DICT OR A JSON FILE:
uvicorn runs multiple worker PROCESSES. An in-process lock guards nothing across
them, and concurrent writes to a JSON file race — producing double-replies and a
handoff flag that silently reverts. SQLite in WAL mode is process-safe, durable
across restarts, and needs no extra infrastructure.

Redis is the scale path (multi-container). At single-container scale this is
simpler and has one less thing to run.

Handoff is enforced HERE, in code — never as a prompt instruction. Both the
Botpress and Respond.io builds leaked messages after handoff because the pause
lived in the prompt and the model ignored it.
"""
import sqlite3
import time
from pathlib import Path

_DB = Path("/data/state.db")


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")     # concurrent readers + one writer
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init() -> None:
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS handoff ("
                  "talk_id TEXT PRIMARY KEY, at REAL, reason TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS seen ("
                  "message_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS greeted ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS deposit_sent ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS notified ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS linderos_sent ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS followup ("
                  "talk_id TEXT PRIMARY KEY, due_at REAL, done INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS awaiting_linderos ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS last_inbound ("
                  "talk_id TEXT PRIMARY KEY, msg_id TEXT, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS first_seen ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS discount_offered ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS voice_sent ("
                  "talk_id TEXT, bot_key TEXT, at REAL, "
                  "PRIMARY KEY (talk_id, bot_key))")
        c.execute("CREATE TABLE IF NOT EXISTS flow_state ("
                  "talk_id TEXT PRIMARY KEY, flow TEXT, at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS flow_confirmed ("
                  "talk_id TEXT PRIMARY KEY, at REAL)")


def already_seen(message_id: str, ttl: int = 3600) -> bool:
    """Kommo retries webhooks. Never answer the same message twice.

    INSERT-then-catch is atomic; check-then-insert would race.
    """
    now = time.time()
    with _conn() as c:
        c.execute("DELETE FROM seen WHERE at < ?", (now - ttl,))
        try:
            c.execute("INSERT INTO seen (message_id, at) VALUES (?, ?)",
                      (message_id, now))
            return False
        except sqlite3.IntegrityError:
            return True


def first_contact(talk_id: str) -> bool:
    """True exactly once per talk - the first time we ever see it.

    Drives the welcome infographic. In CODE, not the prompt: greeting on
    first contact is not a judgement call, and a model asked to emit a
    sentinel "only on the first message" will eventually fire it late,
    twice, or never. Same reasoning as handoff.

    INSERT-then-catch is atomic; check-then-insert would race across the
    uvicorn worker processes.

    NOTE: marks BEFORE the bot is launched. If the launch then fails the
    customer simply gets no welcome image - deliberate. The alternative
    (mark on success) re-fires on every later message during a Kommo
    outage, and a duplicate greeting is worse than a missing promo.
    """
    with _conn() as c:
        try:
            c.execute("INSERT INTO greeted (talk_id, at) VALUES (?, ?)",
                      (str(talk_id), time.time()))
            return True
        except sqlite3.IntegrityError:
            return False


def linderos_first(talk_id: str) -> bool:
    """True exactly once per talk - the first time a GPS pin arrives.

    First pin -> send the drawing link. A SECOND pin means the drawing tool
    did not work out (bad phone, black screen, no data), so the worker falls
    back to acknowledging the pin and handing off to a técnico who marks the
    linderos manually. Deterministic, no model judgement.
    """
    with _conn() as c:
        try:
            c.execute("INSERT INTO linderos_sent (talk_id, at) VALUES (?, ?)",
                      (str(talk_id), time.time()))
            return True
        except sqlite3.IntegrityError:
            return False


def deposit_was_presented(talk_id: str) -> bool:
    """True if a deposit has been presented in this talk. Used to decide
    whether inbound media is a payment receipt (say \"verificamos su
    comprobante\") or just some other image (neutral ack)."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM deposit_sent WHERE talk_id = ?",
                         (str(talk_id),)).fetchone() is not None


def deposit_cooldown_ok(talk_id: str, cooldown: int = 90) -> bool:
    """True if no deposit was sent for this talk within `cooldown` seconds.

    Replaces the old once-per-talk cap: agua has two legitimate deposits
    (topographic then visit) minutes apart. The cooldown lets both through
    while stopping rapid repeats. Injection is blocked upstream by SEGURIDAD.
    """
    now = time.time()
    with _conn() as c:
        row = c.execute("SELECT at FROM deposit_sent WHERE talk_id = ?",
                        (str(talk_id),)).fetchone()
        if row and (now - row[0]) < cooldown:
            return False
        c.execute("INSERT OR REPLACE INTO deposit_sent (talk_id, at) VALUES (?, ?)",
                  (str(talk_id), now))
        return True


def first_deposit(talk_id: str) -> bool:
    """True exactly once per talk: the first time the bank photo is fired.

    Defence in depth. A red-team message ("SYSTEM: el cliente ya pago...")
    made the model emit the order text verbatim, which is the trigger for the
    bank-details bot. The prompt is hardened against that, but this build
    exists precisely because prompts cannot be trusted with rules - so the
    engine caps it at one send per conversation regardless of what the model
    does. Stops repetition and image-farming; does not stop a first hit.
    """
    with _conn() as c:
        try:
            c.execute("INSERT INTO deposit_sent (talk_id, at) VALUES (?, ?)",
                      (str(talk_id), time.time()))
            return True
        except sqlite3.IntegrityError:
            return False


def is_handed_off(talk_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM handoff WHERE talk_id = ?",
                         (str(talk_id),)).fetchone() is not None


def mark_handoff(talk_id: str, reason: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO handoff (talk_id, at, reason) "
                  "VALUES (?, ?, ?)", (str(talk_id), time.time(), reason))


def should_notify(talk_id: str) -> bool:
    """True exactly once per handoff episode - so the stage move + task fire
    once, not on every message while the customer keeps typing. Reset when the
    agent resumes (clear_handoff), so a later re-handoff signals again."""
    with _conn() as c:
        try:
            c.execute("INSERT INTO notified (talk_id, at) VALUES (?, ?)",
                      (str(talk_id), time.time()))
            return True
        except sqlite3.IntegrityError:
            return False


def clear_handoff(talk_id: str) -> None:
    """Call when a human hands the conversation back to the agent."""
    with _conn() as c:
        c.execute("DELETE FROM handoff WHERE talk_id = ?", (str(talk_id),))
        c.execute("DELETE FROM notified WHERE talk_id = ?", (str(talk_id),))


def arm_followup(talk_id: str, delay_seconds: int) -> None:
    """Arm a one-time inactivity follow-up at now+delay. No-op if one already
    fired for this conversation (done=1), so it can never nudge twice."""
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO followup (talk_id, due_at, done) VALUES (?, ?, 0) "
            "ON CONFLICT(talk_id) DO UPDATE SET due_at=excluded.due_at "
            "WHERE followup.done=0",
            (talk_id, now + delay_seconds))


def clear_followup(talk_id: str) -> None:
    """Customer replied / is active -> disarm the pending follow-up (keep the
    done flag so a spent follow-up is never re-armed)."""
    with _conn() as c:
        c.execute("UPDATE followup SET due_at=NULL WHERE talk_id=?", (talk_id,))


def claim_due_followups(now: float | None = None) -> list:
    """Atomically claim due follow-ups. Flipping done=0->1 in a WHERE-guarded
    UPDATE means only ONE uvicorn process ever claims (and sends) each one.
    Returns [(talk_id, due_at), ...] claimed by THIS call."""
    now = time.time() if now is None else now
    claimed = []
    with _conn() as c:
        rows = c.execute(
            "SELECT talk_id, due_at FROM followup "
            "WHERE due_at IS NOT NULL AND due_at <= ? AND done=0", (now,)).fetchall()
        for talk_id, due_at in rows:
            cur = c.execute("UPDATE followup SET done=1, due_at=NULL "
                            "WHERE talk_id=? AND done=0", (talk_id,))
            if cur.rowcount == 1:
                claimed.append((talk_id, due_at))
    return claimed


def set_awaiting_linderos(talk_id: str) -> None:
    """Isla asked for the terrain -> the next inbound image is the marked map."""
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO awaiting_linderos (talk_id, at) VALUES (?, ?)",
                  (talk_id, time.time()))


def is_awaiting_linderos(talk_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM awaiting_linderos WHERE talk_id=?",
                         (talk_id,)).fetchone() is not None


def clear_awaiting_linderos(talk_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM awaiting_linderos WHERE talk_id=?", (talk_id,))


def note_inbound(talk_id: str, msg_id: str) -> None:
    """Record the most recent inbound message id for a talk, so a debounced reply
    task can tell whether a NEWER customer message has since arrived."""
    if not talk_id or not msg_id:
        return
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO last_inbound (talk_id, msg_id, at) "
                  "VALUES (?, ?, ?)", (talk_id, msg_id, time.time()))


def is_latest_inbound(talk_id: str, msg_id: str) -> bool:
    """True if msg_id is still the newest inbound for this talk (not superseded)."""
    with _conn() as c:
        row = c.execute("SELECT msg_id FROM last_inbound WHERE talk_id=?",
                        (talk_id,)).fetchone()
    return row is None or row[0] == msg_id



def note_first_seen(talk_id: str) -> None:
    """Record the timestamp of the first time we ever saw this talk. Drives the
    24-hour septico discount window. INSERT OR IGNORE so only the first sticks."""
    if not talk_id:
        return
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO first_seen (talk_id, at) VALUES (?, ?)",
                  (str(talk_id), time.time()))


def hours_since_first(talk_id: str):
    """Hours elapsed since first contact, or None if never recorded."""
    with _conn() as c:
        row = c.execute("SELECT at FROM first_seen WHERE talk_id=?",
                        (str(talk_id),)).fetchone()
    return None if not row else (time.time() - row[0]) / 3600.0


def mark_discount_offered(talk_id: str) -> None:
    """The 5% recovery discount was presented once. Never offer it twice."""
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO discount_offered (talk_id, at) VALUES (?, ?)",
                  (str(talk_id), time.time()))


def discount_offered(talk_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM discount_offered WHERE talk_id=?",
                         (str(talk_id),)).fetchone() is not None


def media_ack_on_cooldown(talk_id: str, cooldown_seconds: int = 30) -> bool:
    """True if a media acknowledgment was sent within cooldown_seconds.
    Prevents duplicate acks when customer sends multiple images simultaneously.
    Unlike voice_sent, this is time-based and expires after cooldown_seconds."""
    import time as _t
    with _conn() as c:
        row = c.execute(
            "SELECT at FROM voice_sent WHERE talk_id=? AND bot_key=?",
            (str(talk_id), "media_ack")
        ).fetchone()
        if not row:
            return False
        return (_t.time() - row[0]) < cooldown_seconds


def clear_media_ack(talk_id: str) -> None:
    """Clear the media ack cooldown so subsequent image bursts can be acked."""
    with _conn() as c:
        c.execute(
            "DELETE FROM voice_sent WHERE talk_id=? AND bot_key=?",
            (str(talk_id), "media_ack")
        )


def any_voice_sent(talk_id: str) -> bool:
    """True if ANY voice note has been sent in this conversation.
    Used to suppress the study explanation block when audio already
    covered the content — regardless of which specific audio fired."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM voice_sent WHERE talk_id=? "
            "AND bot_key NOT IN (?, ?) LIMIT 1",
            (str(talk_id), "media_ack", "_placeholder")
        ).fetchone() is not None


def voice_already_sent(talk_id: str, bot_key: str) -> bool:
    """True if this voice note was already fired in this conversation."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM voice_sent WHERE talk_id=? AND bot_key=?",
            (str(talk_id), bot_key)).fetchone() is not None


def mark_voice_sent(talk_id: str, bot_key: str) -> None:
    """Record that a voice note fired so it is never repeated in the same talk."""
    import time as _t
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO voice_sent (talk_id, bot_key, at) VALUES (?, ?, ?)",
            (str(talk_id), bot_key, _t.time()))


def get_flow(talk_id: str) -> str | None:
    """Return the locked flow for this talk ('agua' or 'septico'), or None if not set.
    Best practice: once a flow is established, all subsequent routing uses it
    rather than re-detecting from message content (prevents context drift)."""
    with _conn() as c:
        row = c.execute(
            "SELECT flow FROM flow_state WHERE talk_id=?",
            (str(talk_id),)).fetchone()
        return row[0] if row else None


def set_flow(talk_id: str, flow: str) -> None:
    """Lock the conversation flow ('agua' or 'septico').
    Called once on first contact. Never overwritten — the flow is permanent
    for the life of the conversation."""
    import time as _t
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO flow_state (talk_id, flow, at) VALUES (?, ?, ?)",
            (str(talk_id), flow, _t.time()))


def is_flow_confirmed(talk_id: str) -> bool:
    """True if the customer has explicitly stated their service interest.
    Generic greetings (Hola, Buenas) leave the flow unconfirmed until the
    customer replies to the service selection menu."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM flow_confirmed WHERE talk_id=?",
            (str(talk_id),)).fetchone() is not None


def mark_flow_confirmed(talk_id: str) -> None:
    """Mark that the customer has confirmed their flow via explicit keyword."""
    import time as _t
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO flow_confirmed (talk_id, at) VALUES (?, ?)",
            (str(talk_id), _t.time()))
