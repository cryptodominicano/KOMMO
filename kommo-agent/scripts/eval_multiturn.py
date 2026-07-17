"""Multi-turn test: does the septico INTRO fire ONCE, then get out of the way?

The single-turn eval asks every question with empty history, so every septico
question looks like "first mention" and always triggers the INTRO. That is an
artifact of the harness, not agent behaviour - the prompt deliberately sends the
INTRO once, before answering the first specific question. This checks the thing
that actually matters: turn 2 onwards must answer DIRECTLY.
"""
import asyncio
from app import rag, agent

CONVOS = {
    "septico: intro then specifics": [
        "Que es el septico IMHOFF?",
        "Incluye el envio?",
        "Incluye la instalacion?",
        "Tengo 10 banos, cual me recomienda?",
    ],
    "septico: opens with a specific question": [
        "Incluye el envio del septico?",
        "Y la instalacion?",
        "Cuanto cuesta el mas pequeno?",
    ],
    "agua: multi-turn": [
        "Buenas, quiero saber sobre estudios de agua",
        "Cuanto cuesta?",
        "Ustedes garantizan el agua?",
        "Ok, quiero avanzar",
    ],
}


async def run(name, turns):
    print("#" * 72)
    print("#", name)
    print("#" * 72)
    history = []
    for i, t in enumerate(turns, 1):
        kb = await rag.retrieve(t)
        reply = await agent.generate(t, kb, history)
        intro = "Gracias por comunicarte con Aguas Profundas" in reply
        tag = "  <-- INTRO SENT" if intro else ""
        print(f"\n--- turn {i} ---")
        print(f"CLIENTE: {t}")
        print(f"AGENTE ({len(reply)} chars){tag}:")
        print(reply[:340])
        history.append({"role": "user", "content": t})
        history.append({"role": "assistant", "content": reply})
        await asyncio.sleep(12)
    print()


async def main():
    for name, turns in CONVOS.items():
        await run(name, turns)


asyncio.run(main())
