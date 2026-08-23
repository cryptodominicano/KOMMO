import httpx, asyncio, os

SYSTEM_PROMPT = open("/srv/clients/aguas-profundas/prompts/system.md").read()
API_KEY = os.environ["OPENAI_API_KEY"]

TESTS = [
    {"id": "M1", "label": "2Q: price + location",
     "history": [], "context": "FLOW: agua. No hay AUDIO_ENVIADO en este contexto.",
     "msg": "¿Cuánto cuesta el estudio y también dónde están ubicados?",
     "must_contain": ["RD$45,000", "Jarabacoa"], "must_not": []},

    {"id": "M2", "label": "2Q: brochure + module recommendation",
     "history": [{"role":"user","content":"Quiero una planta IMHOFF"},
                 {"role":"assistant","content":"¿Cuántos baños tiene su propiedad?"}],
     "context": "FLOW: septico",
     "msg": "Mándeme el brochure y también para 6 baños cuál me recomienda",
     "must_contain": ["[[SEPTICO_FUNCIONAMIENTO]]", "Módulo 8"], "must_not": []},

    {"id": "M3", "label": "2Q: installation + delivery time",
     "history": [], "context": "FLOW: septico. No hay AUDIO_ENVIADO.",
     "msg": "¿Ustedes instalan la planta y cuánto tiempo tarda la entrega?",
     "must_contain": ["plomero", "semana"], "must_not": []},

    {"id": "M4", "label": "2Q: price objection + how it works",
     "history": [{"role":"assistant","content":"El Módulo 8 es RD$70,000 con envío incluido."}],
     "context": "FLOW: septico",
     "msg": "Está muy caro, ¿y cómo funciona exactamente el sistema?",
     "must_contain": ["[[SEPTICO_VENTAJAS]]"], "must_not": []},

    {"id": "M5", "label": "2Q: call request + price",
     "history": [], "context": "FLOW: agua. No hay AUDIO_ENVIADO.",
     "msg": "¿Me pueden llamar y cuánto sale el estudio completo?",
     "must_contain": ["llamamos", "RD$45,000"], "must_not": []},

    {"id": "M6", "label": "3Q: price + location + time",
     "history": [], "context": "FLOW: agua. No hay AUDIO_ENVIADO.",
     "msg": "¿Cuánto cuesta el estudio, dónde están y cuánto tiempo toma?",
     "must_contain": ["RD$45,000", "Jarabacoa"], "must_not": []},

    {"id": "M7", "label": "3Q: modules + price + delivery septico",
     "history": [], "context": "FLOW: septico. No hay AUDIO_ENVIADO.",
     "msg": "¿Qué módulos tienen, cuánto cuestan y cómo se entrega?",
     "must_contain": ["Módulo 8", "RD$70,000"], "must_not": []},

    {"id": "M8", "label": "3Q: process + price + guarantee",
     "history": [], "context": "FLOW: agua. No hay AUDIO_ENVIADO.",
     "msg": "¿Cómo es el proceso del estudio, cuánto cuesta y tienen garantía?",
     "must_contain": ["RD$45,000", "[[HANDOFF]]"], "must_not": []},

    {"id": "M9", "label": "3Q: installation + dimensions + ficha",
     "history": [], "context": "FLOW: septico. No hay AUDIO_ENVIADO.",
     "msg": "¿Ustedes instalan, cuáles son las dimensiones de excavación y tienen ficha técnica?",
     "must_contain": ["[[SEPTICO_FICHA]]"], "must_not": []},

    {"id": "M10", "label": "3Q: success rate + price + when can they come",
     "history": [], "context": "FLOW: agua. No hay AUDIO_ENVIADO.",
     "msg": "¿Cuál es el porcentaje de éxito, cuánto cuesta y cuándo pueden venir?",
     "must_contain": ["RD$45,000"], "must_not": []},

    {"id": "S1", "label": "DR slang: ta to dímelo greeting",
     "history": [], "context": "FLOW: agua",
     "msg": "Ta to, dímelo",
     "must_contain": ["Aguas Profundas"], "must_not": []},

    {"id": "S2", "label": "DR slang: ¿a cómo? price question",
     "history": [], "context": "FLOW: agua. No hay AUDIO_ENVIADO.",
     "msg": "¿A cómo sale ese estudio de agua?",
     "must_contain": ["RD$45,000"], "must_not": []},

    {"id": "S3", "label": "DR slang: esa vaina",
     "history": [{"role":"assistant","content":"¿Cuántos baños tiene su propiedad?"}],
     "context": "FLOW: septico",
     "msg": "Esa vaina la quiero para 4 baños",
     "must_contain": ["Módulo 8"], "must_not": []},

    {"id": "S4", "label": "DR slang: adjacent scope un chin",
     "history": [{"role":"user","content":"Quiero una planta IMHOFF"},
                 {"role":"assistant","content":"¿Cuántos baños?"}],
     "context": "FLOW: septico. REDIRECT si menciona agua.",
     "msg": "Un chin, ¿y también hacen lo del agua en mi finca?",
     "must_contain": ["séptico"], "must_not": ["RD$45,000"]},

    {"id": "S5", "label": "DR slang: jevi = positive confirmation",
     "history": [{"role":"assistant","content":"El Módulo 8 es RD$70,000 con envío incluido. ¿Le gustaría proceder?"}],
     "context": "FLOW: septico",
     "msg": "Jevi, vamos con eso",
     "must_contain": ["nombre"], "must_not": []},
]

async def run_test(client, test):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in test.get("history", []):
        messages.append({"role": h["role"], "content": h["content"]})
    ctx = test.get("context", "")
    user_content = f"[ESTADO DEL SISTEMA: {ctx}]\n\n{test['msg']}" if ctx else test["msg"]
    messages.append({"role": "user", "content": user_content})
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": "gpt-4.1", "max_tokens": 300, "temperature": 0.3, "messages": messages},
        timeout=30,
    )
    return resp.json()["choices"][0]["message"]["content"].strip()

async def main():
    passed = failed = 0
    async with httpx.AsyncClient() as client:
        for test in TESTS:
            try:
                reply = await run_test(client, test)
                errs = []
                for must in test.get("must_contain", []):
                    if must not in reply:
                        errs.append(f"MISSING: {must}")
                for must_not in test.get("must_not", []):
                    if must_not in reply:
                        errs.append(f"PRESENT (should not be): {must_not}")
                ok = len(errs) == 0
                passed += ok
                failed += not ok
                print(f'{"OK" if ok else "FAIL"} {test["id"]} [{test["label"]}]')
                if not ok:
                    for e in errs: print(f"     {e}")
                    print(f"     REPLY: {reply[:110]}")
            except Exception as e:
                failed += 1
                print(f"ERR {test['id']}: {e}")
    print(f"\nResult: {passed}/{len(TESTS)}")

asyncio.run(main())
