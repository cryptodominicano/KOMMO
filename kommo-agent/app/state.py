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


def is_handed_off(talk_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM handoff WHERE talk_id = ?",
                         (str(talk_id),)).fetchone() is not None


def mark_handoff(talk_id: str, reason: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO handoff (talk_id, at, reason) "
                  "VALUES (?, ?, ?)", (str(talk_id), time.time(), reason))


def clear_handoff(talk_id: str) -> None:
    """Call when a human hands the conversation back to the agent."""
    with _conn() as c:
        c.execute("DELETE FROM handoff WHERE talk_id = ?", (str(talk_id),))
