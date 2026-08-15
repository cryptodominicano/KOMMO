# Aguas Profundas RD — Audio Workflow

Session date: 2026-08-10.
Full reference for the audio-first conversation flow built on top of the Kommo FastAPI agent.

---

## Architecture

```
Incoming WhatsApp message
  │
  ├── is_first + not _septico_first + _is_waba
  │     → welcome-bot image (55340)
  │     → AI text greeting (asks for pueblo/sector)
  │     → VOZ_AGUA_1 (85776)
  │
  ├── is_first + _septico_first + _is_waba
  │     → welcome-bot image (55340)
  │     → AI text + [[SEPTICO_COMPARATIVA]] image (76632)
  │     → VOZ_IMHOFF_1 (85800)
  │
  └── subsequent messages + _is_waba
        → keyword match → fire voice bot (one per turn, never repeated)
        → set _voz_fired → inject AUDIO_ENVIADO into extra_system
        → LLM outputs exact follow-up one-liner only
        │
        └── no keyword match
              → LLM full text answer from KB, no audio

Instagram / Facebook: full KB text, no audio (Meta API restriction)
```

---

## Rules

- One audio per turn maximum
- No audio repeats in same conversation (voice_sent SQLite table)
- Audio fires BEFORE LLM call — LLM receives AUDIO_ENVIADO override
- LLM never repeats audio content in text
- LLM never gives drilling prices (audio handles this)
- All bots must have empty Triggers panel in Kommo UI

---

## AGUA flow

### VOZ_AGUA_1 — Bot 85776 — Welcome and intro

Fires on first water/generic contact on WhatsApp.

Transcript: Greets client, explains 3-part study process (topographic evaluation,
radiesthesia, full area survey), 80-90% success rate, RD$45,000-50,000 cost
(includes all three studies), difference between exploratory and conventional
drilling. Closes asking client to send their location.

Follow-up text: "Para comenzar, por favor mándeme la ubicación de donde desea
realizar el estudio. 📍"

LLM must NOT: repeat study explanation, give prices, explain drilling types.

---

### VOZ_AGUA_2 — Bot 85778 — Drilling price

Keywords: cuánto cuesta perforar, qué cuesta un pozo, cuánto vale hacer un pozo,
cuál es el precio, en cuánto sale, cuánto cobran, cuánto cuesta hacer un hoyo,
cuánto cuesta el pozo, cuánto cuesta sacar agua, cuál es el costo, qué precio
tiene, qué vale, cuánto cuesta encontrar agua, cuánto vale una perforación,
cobran por pie, cuánto cuesta por metro, cuánto cuesta por pie, cómo cobran.

Transcript: Without the study it is impossible to give exact drilling prices.
Depth and saturation are unknown. Highly saturated areas may not be worth
drilling at all. Company works with data, not guessing. All info available
after the study.

Follow-up text: "¿Le gustaría comenzar con el estudio para poder darle toda la
información que necesita? 🙏"

LLM must NOT: give any drilling prices in text.

---

### VOZ_AGUA_3 — Bot 85780 — How to start the study

Keywords: quiero hacer el estudio, vamos a hacerlo, quiero proceder, qué
necesito, cuál es el siguiente paso, cómo funciona, cómo se hace, qué debo
enviar, qué necesitan de mí, cómo empezamos, quiero contratar el estudio,
estoy listo, quiero iniciar, cómo es el procedimiento, explíqueme el proceso,
qué sigue, qué hago ahora, quiero coordinar.

Transcript: Send your location. Team will send satellite photo for client to
mark land boundaries using the WhatsApp pencil tool. Process continues from there.

Follow-up text: "Por favor mándeme la ubicación de su terreno y seguimos el
proceso desde ahí. 📍"

---

### VOZ_AGUA_4 — Bot 85782 — Deposit and payment process

Keywords: quiero pagar, dónde deposito, envíeme la cuenta, voy a pagar, cómo
hago el pago, a qué cuenta, envíeme los datos, dónde transfiero, listo para
pagar, quiero reservar, procedamos, ya tengo todo, aquí está mi ubicación,
ya envié la ubicación, quiero agendar.

Transcript: RD$5,000 deposit starts topographic study (2-3 days, reveals water
veins). Team visits land, takes measurements. 3-4 additional days to complete
full study. Client contacted for remaining deposit. Full report delivered
immediately on final payment. Client must send deposit voucher (bauche).

Follow-up text: "¿Tiene alguna pregunta sobre el proceso o está listo para que
le envíe los datos de depósito? 🙏"

Next engine step: LLM outputs deposit message → engine fires banco-foto (55956)
+ sends AGUAS_BANK_TEXT.

---

### VOZ_AGUA_5 — Bot 85784 — Price objection

Keywords: está muy caro, muy costoso, es mucho dinero, pensé que era menos,
no tengo ese presupuesto, muy alto, muy elevado, no puedo pagar eso, fuera de
mi presupuesto, demasiado caro, hacen descuento, pueden bajar, ese es el mejor
precio, no hay oferta, por qué cuesta tanto, está fuerte ese precio, lo voy a
pensar, déjame ver, está difícil, muy costoso para mí.

Transcript: Company does 3-part study vs competitors' 1-part. 80-90% success
vs 25% for competitors. Many clients arrive with incomplete competitor studies
and realize they were cheated. Making the right investment gives correct
information for correct decisions.

Follow-up text: "¿Le gustaría proceder con el estudio o tiene alguna otra
consulta antes de decidir? 🙏"

---

### VOZ_AGUA_6 — Bot 85788 — Office location

Keywords: dónde están ubicados, dónde están, dónde queda la oficina, tienen
oficina, en qué ciudad están, dónde los encuentro, dónde puedo visitarlos,
puedo pasar por la oficina, dónde trabajan, en qué provincia están, dónde
operan, cuál es su dirección.

Transcript: Located in Arabacoa, serve the entire country. Needs client's
location to quote and schedule appropriately.

Follow-up text: "¿En qué pueblo o sector desea realizar el estudio? Con eso
le cotizo de inmediato. 🙏"

---

### VOZ_AGUA_7 — Bot 85786 — Payment conditions

Keywords: cómo se paga, cuándo se paga, se paga antes, se paga después, cuánto
hay que adelantar, hay depósito, aceptan transferencia, aceptan efectivo,
aceptan tarjeta, cómo funcionan los pagos, cuáles son las condiciones, cómo
trabajan, cuál es la forma de pago, qué métodos aceptan, se paga completo,
hay financiamiento, puedo pagar en dos partes.

Transcript: RD$5,000 deposit to start. Visit land, take info. 24-48 hours to
elaborate study. Contact client for remaining deposit when ready. Deliver
report immediately on final payment and explain results.

Follow-up text: "¿Está listo para dar el primer paso o tiene alguna consulta
adicional antes de comenzar? 🙏"

---

### VOZ_AGUA_8 — Bot 85790 — Call request

Keywords: puedo llamarlo, lo puedo llamar, quiero hablar con usted, quiero
hablar con un asesor, tiene un número, me puede llamar, llámeme, quiero hacerle
unas preguntas, prefiero hablar, podemos hablar, está disponible, podemos
conversar, puede atenderme, tiene unos minutos, necesito hablar con alguien,
quiero comunicarme directamente, le puedo hacer una llamada.

Transcript: Yes to a call, but need to schedule because CEO is always in the
field doing studies and client calls. Ask client for a good time to coordinate.

Follow-up text: "¿Qué hora le queda bien para coordinar la llamada? 🙏"

---

## IMHOFF / Séptico flow

Séptico context detected when: séptico, imhoff, planta de trat, módulo, baño
appears in current message or recent history (last 10 messages).

Priority order: price objection (3) → purchase process (2) → location/trust (4).

---

### VOZ_IMHOFF_1 — Bot 85800 — Welcome and product intro

Fires on first contact when séptico/IMHOFF/planta keywords present in first message.

Detection keywords: séptico, séptic, imhoff, planta de tratamiento.

Transcript: IMHOFF plants treat black and grey water. Made from durable
polyethylene plastic — doesn't degrade or crack. Explains why cement plants
fail in DR (tectonic movement causes cracking, poisons soil and water). Two
modules: Módulo 8 (up to 8 continuous bathrooms, RD$70,000) and Módulo 16
(up to 16 continuous bathrooms, RD$105,000). System is modular. Closes asking
client to let them know if they want to buy.

Follow-up text: "¿Cuántos baños tiene su propiedad? Con eso le indico el módulo
que necesita. 🙏"

LLM must NOT: repeat product explanation or prices.

---

### VOZ_IMHOFF_2 — Bot 85802 — Purchase process

Keywords (séptico context required): quiero comprar, cómo la compro, cómo
funciona, qué debo hacer, cuál es el proceso, cómo procedo, quiero adquirir
una, qué necesito, cómo hacemos, quiero ordenar, quiero hacer el pedido, estoy
listo, qué sigue, cuál es el siguiente paso, cómo hago el pago, cómo se
entrega, cuánto tarda, cómo llega, hacen envíos, la instalan, qué incluye,
qué tengo que enviar, quiero reservar una.

Transcript: RD$10,000 deposit. In one week team calls to arrange delivery.
Client pays remaining balance to driver on delivery. Payment on delivery.

Follow-up text: "¿Está listo para proceder con el depósito de RD$10,000 o
tiene alguna pregunta adicional? 🙏"

---

### VOZ_IMHOFF_3 — Bot 85806 — Price objection

⚠️ NOTE: Bot IDs were swapped 2026-08-15 after live audio verification.
85806 contains the price objection audio; 85804 contains the trust/credibility audio.

Keywords (séptico context required): está muy cara, muy costosa, es mucho
dinero, pensé que costaba menos, fuera de mi presupuesto, muy elevado, no
tengo ese presupuesto, hacen descuento, ese es el mejor precio, no pueden
bajar el precio, hay alguna oferta, está fuerte ese precio, la competencia la
tiene más barata, vi otra más económica, por qué cuesta tanto, qué tiene de
diferente, vale la pena, lo voy a pensar, está difícil, no puedo pagar eso ahora.

Transcript (verified live): Compares plastic vs cement/block. Plastic is more
efficient, more durable, won't crack, won't poison soil or water, easier to
install. When comparing full lifetime cost the IMHOFF plant is actually more
economical.

Follow-up text: "¿Le gustaría proceder con su planta o tiene alguna otra
consulta antes de decidir? 🙏"

Paired image after 4s: [[SEPTICO_VENTAJAS]] (bot 76646)

---

### VOZ_IMHOFF_4 — Bot 85804 — Location, trust, credibility

⚠️ NOTE: Bot IDs were swapped 2026-08-15 after live audio verification.
85804 contains the trust/credibility audio; 85806 contains the price objection audio.

Keywords (séptico context required): dónde están ubicados, dónde están, tienen
oficina, dónde puedo visitarlos, cuál es la dirección, puedo pasar, dónde
queda, en qué ciudad están, dónde los encuentro, quiero ir personalmente, no
me gusta pagar por internet, no confío en transferir, quiero ver el producto
primero, quiero conocerlos antes, son una empresa real, tienen oficina física,
dónde puedo ver las plantas, cómo sé que son confiables, tienen referencias,
quién es Wellington, puedo ir a conocerlos.

Transcript (verified live): Sells direct from factory — special pricing, includes
shipping. Understands distrust of online transfers — can send registro mercantil
to verify the company. CEO always available to call or contact.

3-step sequence:
1. VOZ_IMHOFF_4 voice note (85806)
2. 2 second wait
3. Text: "📍 También puedes conocer más sobre nuestra empresa..." + @aguasprofundas_rd
4. 1 second wait
5. Wellington_Lider_Foto image bot (85808)

Follow-up text: none — sequence handles the full response.

---

## Channel behavior

| Channel | Welcome image | Voice notes | Text answers |
|---|---|---|---|
| WhatsApp (waba) | ✅ | ✅ | ✅ |
| Instagram | ✅ | ❌ | ✅ |
| Facebook | ✅ | ❌ | ✅ |
