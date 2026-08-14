"""Haiku 4.5 pre-processor — intent extraction + scope classification.

Runs before the main GPT-4.1 call. Returns a structured list of intents,
each classified as one of:
  in_scope_agua       — water study, drilling, location, deposit
  in_scope_septico    — IMHOFF plant, modules, installation, delivery
  qualification_answer — customer answering a question we asked (location, banos, etc)
  greeting            — Hola, Buenas, dímelo, ¿qué lo que?, ta to
  adjacent_out_of_scope — water when in séptico flow, or séptico when in agua flow
  fully_off_topic     — religion, politics, general knowledge, poems, weather

Handles Dominican Spanish slang via a cached glossary block.
Temperature 0, XML output, tight max_tokens.
"""

import json
import httpx
from .config import settings
from .retry import post_with_retry

# ── DR Slang Glossary (cached block — static content) ─────────────────────────
_DR_GLOSSARY = """
## Glosario dominicano (para clasificación correcta)
- "ta to" / "tá to" / "ta bien" → greeting o confirmation (NOT complaint)
- "dímelo" / "¿qué lo que?" / "¿qué lo' que?" / "wapa" → greeting
- "¿a cómo?" / "en cuánto sale" / "cuánto es" → precio (price question)
- "esa vaina no sirve" / "no funciona" / "está malo" → complaint
- "un chin" → a little bit
- "jevi" / "chevere" → cool/OK/good
- "dique" → allegedly/supposedly (skeptical framing)
- "vaina" → thing/situation (neutral to negative, context-dependent)
- "por fa" → please
- "tíguere" → street-smart person (tone-dependent: compliment or insult)
- "motoconcho" / "concho" → shared transport (context: location question)
- "la capital" / "el DN" → Santo Domingo
- "la guagua" → bus
"""

# ── Category taxonomy (cached block — static content) ─────────────────────────
_TAXONOMY = """
## Categorías de intención (mutuamente excluyentes):

in_scope_agua: pregunta sobre estudio de agua, pozo, perforación, terreno,
  linderos, ubicación para estudio, precio del estudio RD$45,000-50,000,
  depósito de RD$5,000, proceso del estudio, cuándo vienen al terreno.

in_scope_septico: pregunta sobre planta séptica IMHOFF, módulo 8 o 16,
  precio RD$70,000 o RD$105,000, instalación, entrega, depósito RD$10,000,
  cuántos baños, dimensiones de excavación.

qualification_answer: cliente respondiendo una pregunta que le hicimos —
  su pueblo/sector/provincia, cuántos baños tiene, si quiere avanzar,
  confirmación de que escuchó el audio, número de contacto para llamada.

greeting: saludo sin contenido de servicio — Hola, Buenas, Buenos días,
  dímelo, ¿qué lo que?, ta to, Hello, Hi, Get Started.

adjacent_out_of_scope: menciona el servicio ALTERNATIVO —
  cliente en flujo SÉPTICO que pregunta por agua/pozo/estudio, O
  cliente en flujo AGUA que pregunta por séptico/IMHOFF/planta.

fully_off_topic: nada relacionado con los servicios de Aguas Profundas —
  religión, política, chistes, poemas, clima, noticias, otros negocios,
  solicitudes generales de información. Si hay duda, usa adjacent_out_of_scope.
"""

_SYSTEM = f"""Eres un clasificador de mensajes de WhatsApp para una empresa dominicana
de servicios de agua y sépticos. Tu única tarea es identificar las intenciones
en el mensaje del cliente y clasificar cada una.

{_TAXONOMY}

{_DR_GLOSSARY}

Reglas:
1. Identifica TODAS las intenciones distintas en el mensaje (puede haber 1, 2 o 3).
2. Clasifica cada una con la categoría más específica.
3. Si hay duda entre categorías, elige adjacent_out_of_scope.
4. Responde SOLO en XML con este formato exacto, sin texto adicional.

Formato de respuesta:
<razonamiento>análisis breve interno</razonamiento>
<intenciones>
<intencion id="1" categoria="CATEGORIA">texto de la intención</intencion>
<intencion id="2" categoria="CATEGORIA">texto de la intención</intencion>
</intenciones>
"""


async def classify(text: str, flow: str = "agua") -> list[dict]:
    """Classify a customer message into a list of intents.
    
    Returns: [{"id": 1, "text": "...", "scope": "in_scope_agua"}, ...]
    
    flow: "agua" or "septico" — used to determine adjacent_out_of_scope.
    On failure returns [{"id": 1, "text": text, "scope": "in_scope_agua"}]
    so the main model always gets called (fail-open, not fail-closed).
    """
    if not text or not text.strip():
        return [{"id": 1, "text": text, "scope": "greeting"}]

    user_msg = f"FLUJO ACTIVO: {flow.upper()}\n\nMensaje del cliente: {text}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await post_with_retry(
                c,
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": "gpt-4o-mini",  # cheapest fast model on OpenAI
                    "max_tokens": 300,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                },
            )
        raw = r.json()["choices"][0]["message"]["content"].strip()
        return _parse_xml(raw, text)
    except Exception as e:
        import logging
        logging.getLogger("haiku").warning("classify failed: %s — fail open", e)
        return [{"id": 1, "text": text, "scope": "in_scope_" + flow}]


def _parse_xml(raw: str, fallback_text: str) -> list[dict]:
    """Parse the XML response into a list of intent dicts."""
    import re
    intents = []
    for m in re.finditer(
        r'<intencion\s+id="(\d+)"\s+categoria="([^"]+)">([^<]*)</intencion>',
        raw, re.DOTALL
    ):
        intents.append({
            "id": int(m.group(1)),
            "text": m.group(3).strip(),
            "scope": m.group(2).strip(),
        })
    if not intents:
        intents = [{"id": 1, "text": fallback_text, "scope": "in_scope_agua"}]
    return intents


def is_simple_greeting(intents: list[dict]) -> bool:
    """True if ALL intents are greetings — skip main model, send menu."""
    return all(i["scope"] == "greeting" for i in intents)


def has_adjacent_out_of_scope(intents: list[dict]) -> bool:
    """True if any intent is adjacent_out_of_scope (redirect needed)."""
    return any(i["scope"] == "adjacent_out_of_scope" for i in intents)


def in_scope_intents(intents: list[dict]) -> list[dict]:
    """Return only in-scope intents (filter out adjacent/fully_off_topic)."""
    return [i for i in intents
            if i["scope"] in ("in_scope_agua", "in_scope_septico",
                              "qualification_answer")]


def build_multi_intent_prompt(intents: list[dict]) -> str:
    """Build the 'answer ALL of these' injection for the main model when
    multiple in-scope intents are detected."""
    scoped = in_scope_intents(intents)
    if len(scoped) <= 1:
        return ""
    lines = "\n".join(f"{i+1}. {intent['text']}"
                       for i, intent in enumerate(scoped))
    return (
        f"El cliente hizo {len(scoped)} preguntas distintas. "
        f"Debes responder TODAS antes de enviar tu mensaje:\n{lines}\n"
        f"Verifica internamente que tu respuesta cubre los {len(scoped)} puntos."
    )
