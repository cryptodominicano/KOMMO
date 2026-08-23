"""Haiku 4.5 pre-processor — intent extraction + scope classification.

Runs before the main GPT-4.1 call. Returns a structured list of intents,
each classified as one of:
  in_scope_agua       — water study, drilling, location, deposit
  in_scope_septico    — IMHOFF plant, modules, installation, delivery
  qualification_answer — customer answering a question we asked (location, banos, etc)
  ready_to_proceed_agua — agua buyer signals intent to proceed/buy (advance to close)
  greeting            — Hola, Buenas, dímelo, ¿qué lo que?, ta to
  adjacent_out_of_scope — water when in séptico flow, or séptico when in agua flow
  soft_farewell       — 'Lo voy a pensar', 'Yo le aviso', latent objection disguised as farewell
  hard_no             — explicit opt-out or annoyance, close gracefully
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
## Señales MINITS para detección de objeción latente (Good et al. 2024)
- Preguntas de compra previas (precio, entrega, depósito) = objeción latente alta
- Conversación larga/profunda antes del farewell = objeción latente alta
- Fecha específica dada ("el viernes le confirmo") = objeción latente media-alta
- Farewell vago sin fecha ("yo le aviso") = objeción latente media
- Molestia explícita o rechazo directo = hard_no, no soft_farewell
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

ready_to_proceed_agua: SOLO flujo AGUA. Cliente que EXPRESA INTENCIÓN DE
  COMPRAR o AVANZAR al siguiente paso — no es una pregunta sobre cómo se paga,
  es la decisión de proceder. Ejemplos: "quiero comprar", "quiero proceder",
  "cuál es el próximo paso", "cómo empezamos", "estoy listo", "vamos a hacerlo",
  "qué necesito para empezar", "cómo procedo". NO confundir con payment_conditions
  (esa es una PREGUNTA sobre formas/condiciones de pago, no la decisión de avanzar).

greeting: saludo sin contenido de servicio — Hola, Buenas, Buenos días,
  dímelo, ¿qué lo que?, ta to, Hello, Hi, Get Started.

adjacent_out_of_scope: menciona el servicio ALTERNATIVO —
  cliente en flujo SÉPTICO que pregunta por agua/pozo/estudio, O
  cliente en flujo AGUA que pregunta por séptico/IMHOFF/planta.

fully_off_topic: nada relacionado con los servicios de Aguas Profundas —
  religión, política, chistes, poemas, clima, noticias, otros negocios,
  solicitudes generales de información. Si hay duda, usa adjacent_out_of_scope.
soft_farewell: cliente posponiendo o despidiéndose de forma vaga, con alta
  probabilidad de objeción latente — "Lo voy a pensar", "Yo le aviso",
  "Déjame consultarlo", "Después le confirmo", "Luego le escribo",
  "Lo voy a hablar con mi esposo/esposa", "Está caro déjame pensar",
  "Mañana le escribo", "Ahorita no puedo". IMPORTANTE: si el mensaje
  contiene además una pregunta o interés activo, clasifica la pregunta
  primero y el farewell también.
  NUNCA uses soft_farewell para solicitudes de información activa:
  "Más información", "Más info", "Quiero saber más", "Cuéntame más",
  "Más detalles", "Explíqueme más" → son in_scope, NO soft_farewell.
  La diferencia: soft_farewell = cliente cerrando/aplazando;
  solicitud de info = cliente queriendo saber MÁS, interés activo.

hard_no: rechazo explícito, molestia o solicitud de no contactar —
  "No me interesa", "No gracias ya decidí que no", "No escriba más",
  "Bórreme", "STOP", "No quiero", "Déjeme tranquilo", "No moleste".
  También: molestia clara ("esto es spam", "¿por qué me sigue escribiendo?").
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
4. Adicionalmente, detecta si el mensaje activa alguna nota de voz de ventas.
5. Responde SOLO en XML con este formato exacto, sin texto adicional.

## Intenciones de nota de voz (voice_bot_intent)
Solo para mensajes subsiguientes (no primer contacto). Detecta si aplica:
REGLA DE FLUJO: Para intenciones con variantes _agua/_septico, usa SIEMPRE
el FLUJO ACTIVO. FLUJO ACTIVO=SEPTICO → septico; FLUJO ACTIVO=AGUA → agua.
NUNCA uses variante del flujo contrario.

REGLA DE PRECIO (crítica): El mensaje incluye PRECIO_YA_DIVULGADO=true/false.
Si PRECIO_YA_DIVULGADO=false → preguntas de precio son price_inquiry_first NUNCA price_objection_agua.
Si PRECIO_YA_DIVULGADO=true → preguntas de precio pueden ser price_objection_agua.
price_inquiry_first: frases interrogativas (¿cuánto cuesta?, a cómo, en cuánto me sale, qué cobran).
price_objection_agua: frases declarativas (ta caro, está muy caro, competencia cobra menos, no tengo presupuesto).
price_inquiry_first NO dispara ningún voice bot — el LLM responde con información del estudio.

AGUA/PERFORACIÓN:
- drilling_price: pregunta por costo de perforar, precio del pozo, cuánto cuesta por pie/metro
- price_inquiry_first: cliente pregunta el precio por primera vez. SOLO si PRECIO_YA_DIVULGADO=false.
  Señal: frase interrogativa. Ejemplo: 'cuánto cuesta el estudio', 'a cómo', 'qué precio tienen'.
  NO dispara voice bot — el LLM da la información.
- price_objection_agua: cliente reacciona a un precio ya conocido. SOLO si FLUJO ACTIVO = AGUA Y PRECIO_YA_DIVULGADO=true.
  Señal: declarativa O interrogativa retórica que cuestiona el precio. Ejemplo: 'ta caro', 'está muy caro', 'competencia cobra menos', 'no tengo presupuesto', '¿por qué tanto dinero?', '¿por qué cuesta tanto?', '¿eso no es mucho?', '¿no es muy caro eso?', 'wow eso está fuerte'.
- location_agua: cliente PREGUNTA dónde está la empresa/oficina, en qué ciudad trabajan.
  SOLO cuando el cliente pregunta POR LA EMPRESA, no cuando da su propio pueblo o terreno.
  NUNCA uses este intent cuando el cliente está RESPONDIENDO dónde está su terreno.
  Ejemplo SÍ: 'dónde están ustedes', 'en qué ciudad trabajan', 'tienen oficina'
  Ejemplo NO: 'mi terreno está en Nagua', 'el terreno queda en Cabrera', 'estoy en Punta Cana'
- payment_conditions: cómo se paga, cuándo se paga, formas de pago, financiamiento.
  SOLO si FLUJO ACTIVO = AGUA. Para séptico, usa purchase_process_septico.
- call_request: quiere llamar, hablar con alguien, que lo llamen, prefiere hablar

SÉPTICO IMHOFF:
- purchase_process_septico: cómo comprar, proceso de pedido, cómo proceder, cuánto tarda entrega.
  También cubre: forma de pago, pago contra entrega, depósito, si se paga antes o después,
  métodos de pago, si aceptan efectivo. SOLO si FLUJO ACTIVO = SEPTICO.
- price_objection_septico: está cara, competencia más barata, fuera de presupuesto. SOLO si FLUJO ACTIVO = SEPTICO.
- trust_question: cómo saber si son legítimos, empresa verdadera/real/legal, confianza,
  registro mercantil, cómo verificar, quién es Wellington, pueden confiar en ellos,
  no quiere pagar por internet, quiere ver el producto primero, empresa registrada
- location_septico: cliente PREGUNTA dónde está la empresa/oficina (séptico context).
  SOLO cuando el cliente pregunta POR LA EMPRESA, no cuando da su propia dirección.

Formato de respuesta:
<razonamiento>análisis breve interno</razonamiento>
<intenciones>
<intencion id="1" categoria="CATEGORIA">texto de la intención</intencion>
<intencion id="2" categoria="CATEGORIA">texto de la intención</intencion>
</intenciones>
<voz_bots>
<voz_bot intent="INTENT" confidence="0.0-1.0"/>
</voz_bots>
Si no aplica ninguna nota de voz, escribe: <voz_bots/>

Ejemplos de voz_bots:
Mensaje: "Como se que ustedes son una empresa verdadera y legitima"
<voz_bots><voz_bot intent="trust_question" confidence="0.95"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA, PRECIO_YA_DIVULGADO=false): "cuánto cuesta el estudio"
<voz_bots/>

Mensaje (FLUJO ACTIVO: AGUA, PRECIO_YA_DIVULGADO=false): "a cómo me sale eso"
<voz_bots/>

Mensaje (FLUJO ACTIVO: AGUA, PRECIO_YA_DIVULGADO=true): "ta muy cara esa vaina"
<voz_bots><voz_bot intent="price_objection_agua" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA, PRECIO_YA_DIVULGADO=false): "ta muy cara esa vaina"
<voz_bots/>

Mensaje (FLUJO ACTIVO: SEPTICO): "ta muy caro eso, la competencia la tiene mas barata"
<voz_bots><voz_bot intent="price_objection_septico" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "ta muy cara esa vaina"
<voz_bots><voz_bot intent="price_objection_agua" confidence="0.90"/></voz_bots>

Mensaje: "cuanto cuesta perforar y donde estan ubicados"
<voz_bots><voz_bot intent="drilling_price" confidence="0.95"/><voz_bot intent="location_agua" confidence="0.85"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "cuánto cuesta perforar"
<voz_bots><voz_bot intent="drilling_price" confidence="0.95"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "cuánto cuesta hacer un pozo"
<voz_bots><voz_bot intent="drilling_price" confidence="0.93"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "cobran por pie de perforación"
<voz_bots><voz_bot intent="drilling_price" confidence="0.92"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "en cuánto me sale el pozo completo"
<voz_bots><voz_bot intent="drilling_price" confidence="0.90"/></voz_bots>

Mensaje: "mi terreno está en Cabrera, Baoba de Pinar a 900 mts de la playa"
<voz_bots/>

Mensaje (FLUJO ACTIVO: AGUA): "¿Y dónde están ustedes ubicados?"
<voz_bots><voz_bot intent="location_agua" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "en qué ciudad trabajan"
<voz_bots><voz_bot intent="location_agua" confidence="0.88"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "tienen oficina, puedo visitarlos"
<voz_bots><voz_bot intent="location_agua" confidence="0.85"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "pueden llamarme"
<voz_bots><voz_bot intent="call_request" confidence="0.92"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "prefiero hablar con alguien"
<voz_bots><voz_bot intent="call_request" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "quiero que me llamen, tengo preguntas"
<voz_bots><voz_bot intent="call_request" confidence="0.88"/></voz_bots>

Mensaje: "el terreno queda en Nagua"
<voz_bots/>

Mensaje (FLUJO ACTIVO: SEPTICO): "dónde queda su oficina"
<voz_bots><voz_bot intent="location_septico" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: SEPTICO): "mi propiedad está en La Romana"
<voz_bots/>

Mensaje: "quiero ordenar, como es el proceso"
<voz_bots><voz_bot intent="purchase_process_septico" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: SEPTICO): "hay forma de pago contra entrega o yo buscarla"
<voz_bots><voz_bot intent="purchase_process_septico" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: SEPTICO): "aceptan efectivo, no hacemos pagos por adelantado"
<voz_bots><voz_bot intent="purchase_process_septico" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "hay forma de pago contra entrega"
<voz_bots><voz_bot intent="payment_conditions" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "cómo funciona el pago"
<voz_bots><voz_bot intent="payment_conditions" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "cómo se hace el pago, cuándo se paga"
<voz_bots><voz_bot intent="payment_conditions" confidence="0.90"/></voz_bots>

Mensaje (FLUJO ACTIVO: AGUA): "aceptan transferencia, pueden cobrar efectivo"
<voz_bots><voz_bot intent="payment_conditions" confidence="0.88"/></voz_bots>

Mensaje: "hola buenos dias"
<voz_bots/>

Mensaje: "Más información"
# NOTA: esto es solicitud de info activa, NO soft_farewell
<voz_bots/>

Mensaje: "8"
<voz_bots/>
"""


async def classify(text: str, flow: str = "agua", price_disclosed: bool = False) -> list[dict]:
    """Classify a customer message into a list of intents.
    
    Returns: [{"id": 1, "text": "...", "scope": "in_scope_agua"}, ...]
    
    flow: "agua" or "septico" — used to determine adjacent_out_of_scope.
    On failure returns [{"id": 1, "text": text, "scope": "in_scope_agua"}]
    so the main model always gets called (fail-open, not fail-closed).
    """
    if not text or not text.strip():
        return [{"id": 1, "text": text, "scope": "greeting"}]

    _price_flag = "true" if price_disclosed else "false"
    user_msg = (
        f"FLUJO ACTIVO: {flow.upper()}\n"
        f"PRECIO_YA_DIVULGADO: {_price_flag}\n\n"
        f"Mensaje del cliente: {text}"
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await post_with_retry(
                c,
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": "gpt-4o-mini",  # cheapest fast model on OpenAI
                    "max_tokens": 500,
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
    """Parse the XML response into a list of intent dicts.
    Also extracts voice-bot intents from the <voz_bots> block.
    """
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
    # Parse voice-bot intents from <voz_bots> block
    voz_bots = []
    voz_block = re.search(r'<voz_bots>(.*?)</voz_bots>', raw, re.DOTALL)
    if voz_block:
        for vm in re.finditer(
            r'<voz_bot\s+intent="([^"]+)"\s+confidence="([^"]+)"/?>',
            voz_block.group(1)
        ):
            try:
                conf = float(vm.group(2))
            except ValueError:
                conf = 0.0
            voz_bots.append({"intent": vm.group(1), "confidence": conf})
    # Attach voice-bot intents to the first intent dict for easy access
    if intents:
        intents[0]["voz_bots"] = voz_bots
    return intents


import unicodedata as _ud


def _deaccent(s: str) -> str:
    return "".join(
        c for c in _ud.normalize("NFD", s.lower())
        if _ud.category(c) != "Mn"
    )


# Deterministic corrections for the specific slang confusions gpt-4o-mini
# makes systematically. Measured Aug 2026: drilling-cost questions phrased
# colloquially ("perforar a como ta", "el hoyo cuanto sale", "que me cuesta
# abrir el pozo") get misread as study-price objections, and oblique
# location asks ("darme una vuelta por alla") get read as greetings.
# Haiku's NLU is trusted for everything else — this only overrides the two
# documented misreads, and only when the correcting evidence is present.
_DRILL_TERMS = (
    "perforar", "perforacion", "abrir el pozo", "abrir pozo",
    "hacer el pozo", "hacer un pozo", "el hoyo", "un hoyo", "ese hoyo",
    "por pie", "por metro", "pozo cuanto", "el pozo cuanto",
)
_COST_SIGNALS = (
    "cuesta", "cuanto", "a como", "como ta", "como esta", "precio",
    "sale", "vale", "cobran", "cobra", "presupuesto", "me sale",
)
_LOC_VISIT = (
    "darme una vuelta", "dar una vuelta", "pasar por alla", "pasar por alli",
    "ir a verlos", "ir a conocerlos", "visitarlos", "pasar a verlos",
    "puedo pasar", "puedo ir",
)


def correct_scope(intents: list[dict], text: str, flow: str) -> list[dict]:
    """Apply deterministic corrections to Haiku's scope classification for
    the documented slang confusions. Mutates and returns the intent list."""
    t = _deaccent(text)
    has_drill = any(term in t for term in _DRILL_TERMS)
    has_cost = any(s in t for s in _COST_SIGNALS)
    has_loc_visit = any(p in t for p in _LOC_VISIT)
    loc_scope = "location_agua" if flow == "agua" else "location_septico"
    for i in intents:
        sc = i.get("scope")
        # Drilling cost question misread as study-price objection or generic
        if has_drill and has_cost and sc in (
            "price_objection_agua", "in_scope_agua", "greeting"
        ):
            i["scope"] = "drilling_price"
        # Oblique visit/location ask misread as greeting or generic
        elif has_loc_visit and sc in ("greeting", "in_scope_agua",
                                      "in_scope_septico"):
            i["scope"] = loc_scope
    return intents


# Scope values that map directly to a voice bot. The scope field of the
# Haiku classification is the single source of truth for audio routing —
# it is far more reliable than a redundant parallel <voz_bots> block, which
# gpt-4o-mini drops inconsistently. The intent name handed to worker.py's
# _HAIKU_VOZ_MAP is the scope string itself (they share one vocabulary).
_VOZ_SCOPES = {
    "location_agua", "drilling_price", "payment_conditions",
    "call_request", "price_objection_agua",
    "location_septico", "purchase_process_septico",
    "price_objection_septico", "trust_question",
}


def get_voz_bot_intents(intents: list[dict]) -> list[dict]:
    """Derive voice-bot intents from the reliable scope classification.
    Every intent whose scope is audio-triggering yields one voice bot.
    Multi-intent messages (e.g. drilling_price + location_agua) naturally
    produce multiple bots. Confidence is a constant 0.9 — the real gating
    (price_disclosed, flow-awareness) happens in worker.py, and scope is a
    categorical decision, not a scored one.
    """
    out = []
    seen = set()
    for i in intents:
        scope = i.get("scope")
        if scope in _VOZ_SCOPES and scope not in seen:
            seen.add(scope)
            out.append({"intent": scope, "confidence": 0.9})
    return out


def is_soft_farewell(intents: list[dict]) -> bool:
    """True if the primary intent is a soft farewell (latent objection).
    Research: MINITS framework — one diagnostic probe is warranted.
    Never returns True if there is also an in-scope question present.
    """
    scopes = [i["scope"] for i in intents]
    has_farewell = "soft_farewell" in scopes
    has_active_question = any(s in ("in_scope_agua", "in_scope_septico",
                                    "qualification_answer")
                              for s in scopes)
    return has_farewell and not has_active_question


def is_hard_no(intents: list[dict]) -> bool:
    """True if the customer explicitly opted out or expressed annoyance.
    Research: never probe after a hard_no — close gracefully only.
    """
    return any(i["scope"] == "hard_no" for i in intents)


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
