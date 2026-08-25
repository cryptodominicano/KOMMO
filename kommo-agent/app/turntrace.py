"""Per-turn decision trace + toggleable outbound-API trace.

Purpose: make one conversation greppable end-to-end and record WHY the engine
did what it did, so a "who moved my lead / why did it say that" question is a
grep, not a multi-hour hunt (see CONTEXT-LOG 2026-08-24). Additive only —
nothing here changes agent behavior, and every function is defensive (a trace
bug must never break a reply).

Two layers:
  1. TURN_TRACE — a per-turn accumulator (contextvars, asyncio-safe). Reset at
     the start of each handled message, appended to at key decision points,
     emitted as ONE summary line at the end. Always on (one low-volume line).
  2. KOMMO_TRACE — verbose outbound-write logging in kommo._req, gated behind an
     env flag (default OFF). When on, every POST/PATCH/DELETE to Kommo logs its
     method, path, and body — the exact trace that cracked the Aug-24 mystery.
"""
import contextvars
import logging

log = logging.getLogger("turntrace")

# asyncio copies the context at task creation, so a ContextVar set inside the
# per-message task is isolated to that task — safe under concurrent messages.
_turn: contextvars.ContextVar = contextvars.ContextVar("turn_trace", default=None)


def reset(talk_id: str) -> None:
    """Start a fresh trace for this turn. Call once at handler entry."""
    try:
        _turn.set({"talk": str(talk_id), "events": []})
    except Exception:
        pass


def add(event: str) -> None:
    """Record one decision (e.g. 'intent=trust_question', 'voz=VOZ_AGUA_5',
    'stage->Discussions', 'handoff=agent_requested'). Never raises."""
    try:
        t = _turn.get()
        if t is not None and event:
            t["events"].append(str(event))
    except Exception:
        pass


def emit() -> None:
    """Emit the one-line turn summary. Call once in the handler finally block."""
    try:
        t = _turn.get()
        if t and t.get("events"):
            log.info("talk=%s TURN_TRACE: %s", t["talk"], " | ".join(t["events"]))
    except Exception:
        pass
