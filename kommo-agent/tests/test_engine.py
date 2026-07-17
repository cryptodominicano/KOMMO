"""Engine tests. No network, no Kommo, no API keys required.

Covers the three things that have actually broken in production before:
  1. webhook payload parsing (PHP-style urlencoded)
  2. dedupe (Kommo retries webhooks)
  3. handoff persistence (the pause must survive; prompts could not be trusted)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test")

from app.main import parse_php_form, _as_list  # noqa: E402


def test_parses_incoming_text():
    m = _as_list(parse_php_form({
        "add[0][id]": "m1", "add[0][talk_id]": "172",
        "add[0][text]": "Buenas, cuanto cuesta un pozo?",
        "add[0][message_type]": "text", "add[0][type]": "incoming",
        "add[0][origin]": "waba",
        "add[0][author][id]": "a1", "add[0][author][type]": "external",
    }).get("add"))[0]
    assert m["talk_id"] == "172"
    assert m["type"] == "incoming"
    assert m["origin"] == "waba"          # NOT "whatsapp" - verified live
    assert m["author"]["type"] == "external"


def test_parses_voice_attachment():
    m = _as_list(parse_php_form({
        "add[0][id]": "v1", "add[0][talk_id]": "173",
        "add[0][message_type]": "voice", "add[0][type]": "incoming",
        "add[0][attachment][type]": "voice",
        "add[0][attachment][link]": "https://amojo.kommo.com/x/nota.ogg",
    }).get("add"))[0]
    assert m["message_type"] == "voice"
    assert m["attachment"]["link"].endswith(".ogg")


def test_parses_location():
    m = _as_list(parse_php_form({
        "add[0][id]": "l1", "add[0][talk_id]": "174",
        "add[0][message_type]": "location", "add[0][type]": "incoming",
    }).get("add"))[0]
    assert m["message_type"] == "location"


def test_parses_batch():
    items = _as_list(parse_php_form({
        "add[0][id]": "a", "add[1][id]": "b", "add[2][id]": "c",
    }).get("add"))
    assert [i["id"] for i in items] == ["a", "b", "c"]


def test_outgoing_is_not_top_level_add():
    """Outgoing hooks are wrapped; they must not be mistaken for inbound."""
    p = parse_php_form({"outgoing_message[add][0][id]": "o1"})
    assert "add" not in p


def test_state_dedupe_and_handoff(monkeypatch):
    from app import state
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(state, "_DB", Path(d) / "t.db")
        state.init()
        assert state.already_seen("m1") is False   # first delivery
        assert state.already_seen("m1") is True    # Kommo retry -> ignored
        assert state.is_handed_off("100") is False
        state.mark_handoff("100", "location_shared")
        assert state.is_handed_off("100") is True  # survives; agent goes silent
        state.clear_handoff("100")
        assert state.is_handed_off("100") is False


def test_client_pack_loads():
    from app import client
    p = client.pack("aguas-profundas")
    assert p["kommo"]["origin"] == "waba"
    assert "Recibimos su ubicación" in client.msg("location_received", "aguas-profundas")
    assert client.behavior("handoff_marker", "aguas-profundas") == "[[HANDOFF]]"
    assert "Aguas Profundas" in client.system_prompt("aguas-profundas")


def test_entity_type_pluralised_for_bot_run():
    """Webhook sends 'lead'; POST /bots/{id}/run requires 'leads'."""
    from app.worker import _entity_type
    assert _entity_type({"entity_type": "lead"}) == "leads"
    assert _entity_type({"entity_type": "leads"}) == "leads"
    assert _entity_type({}) == "leads"
    assert _entity_type({"entity_type": "contact"}) == "contacts"


def test_salesbot_trigger_configured():
    """The image workaround: send_message is text-only, so a sentinel fires a
    Salesbot, whose Message step CAN attach images."""
    from app import client
    triggers = client.pack("aguas-profundas").get("salesbot", {}).get("triggers", {})
    assert "[[FOTOS_SEPTICO]]" in triggers
    assert "[[FOTOS_SEPTICO]]" in client.system_prompt("aguas-profundas")


def test_inbound_media_is_configured_not_dropped():
    """A deposit receipt arrives as message_type 'picture' with EMPTY text.

    Regression guard: without a media branch it falls through to the empty-text
    drop and the customer is silently ignored at the moment they send proof of
    payment. Business rule: never confirm a payment - acknowledge and hand off.
    """
    from app import client
    media = client.behavior("media_types", "aguas-profundas")
    assert "picture" in media, "photos (deposit receipts) must be handled"
    assert "file" in media
    ack = client.msg("media_received", "aguas-profundas")
    assert "Recibido" in ack
    assert "verifica" in ack          # promises verification, never confirmation
    for bad in ("confirmado", "confirmamos", "recibimos el pago", "pago confirmado"):
        assert bad.lower() not in ack.lower(), f"must never confirm payment: {bad}"


def test_first_contact_fires_exactly_once(monkeypatch):
    """The welcome infographic is deterministic, not a model decision.

    Guards the double-greeting bug: Kommo retries webhooks and uvicorn runs
    multiple processes, so this must be atomic and must survive.
    """
    from app import state
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(state, "_DB", Path(d) / "t.db")
        state.init()
        assert state.first_contact("500") is True    # first ever message
        assert state.first_contact("500") is False   # retry / 2nd message
        assert state.first_contact("500") is False
        assert state.first_contact("501") is True    # different talk


def test_welcome_bot_is_engine_fired_not_sentinel_fired():
    """welcome_bot_id must NOT be in [salesbot.triggers].

    If it were, the model could emit its sentinel mid-conversation and greet
    someone who has been talking for ten minutes.
    """
    from app import client
    sb = client.pack("aguas-profundas").get("salesbot", {})
    assert int(sb.get("welcome_bot_id", 0)) > 0
    triggers = sb.get("triggers", {})
    assert all(int(v) != int(sb["welcome_bot_id"]) for v in triggers.values())
    assert "FOTO_WELCOME" not in client.system_prompt("aguas-profundas")


def test_agua_photo_sentinel_wired_and_no_longer_hands_off():
    """agua-foto (55348) replaced a handoff.

    The prompt used to say a tecnico would send water-process photos and fire
    [[HANDOFF]]. That handoff existed only because we believed images were
    impossible. Regression guard: it must not come back.
    """
    from app import client
    triggers = client.pack("aguas-profundas").get("salesbot", {}).get("triggers", {})
    assert triggers.get("[[FOTO_AGUA]]") == 55348
    prompt = client.system_prompt("aguas-profundas")
    assert "[[FOTO_AGUA]]" in prompt
    assert "fotos de pozos o del proceso de agua" not in prompt


def test_every_configured_bot_id_is_real():
    """Sentinels with bot_id 0 warn and no-op. Catch a half-finished build."""
    from app import client
    sb = client.pack("aguas-profundas").get("salesbot", {})
    for sentinel, bot_id in sb.get("triggers", {}).items():
        assert int(bot_id) > 0, f"{sentinel} has no bot_id"
        assert sentinel in client.system_prompt("aguas-profundas"), \
            f"{sentinel} configured but the model is never told to emit it"


def test_ingest_script_points_at_the_real_kb():
    """Regression: KB_DIR pointed at kommo-agent/kb, which never existed.

    The KB lives in the client pack. The bug was silent - it would have built an
    empty Qdrant collection and left the agent answering from nothing, with the
    guardrails ("never guarantee water") gone too, since those live in the KB.
    """
    import importlib.util
    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "ingest_kb", root / "scripts" / "ingest_kb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.KB_DIR.is_dir(), f"KB_DIR does not exist: {mod.KB_DIR}"
    files = sorted(mod.KB_DIR.glob("*.md"))
    assert len(files) >= 4, f"expected the 4 KB files, found {len(files)}"

    # The KB must still carry the two non-negotiable business rules.
    corpus = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert "80" in corpus and "90" in corpus, "water success-rate range missing"

    # And chunking must actually produce chunks (H2 split).
    chunks = mod.chunk_markdown(files[0].read_text(encoding="utf-8"), files[0].name)
    assert len(chunks) > 0
    assert all(c["text"].strip() for c in chunks)


def test_agent_loads_the_real_system_prompt():
    """Regression: agent.py hardcoded /srv/prompts/system.md, which never existed.

    generate() raised FileNotFoundError on EVERY message. worker.py catches
    Exception broadly, so the customer was ghosted silently - no reply, no
    visible error. The existing tests missed it because they asserted
    client.system_prompt() works (it does); nothing exercised agent.py's own
    loader. Only running the container caught it.

    This asserts agent.py reads the SAME prompt the client pack serves.
    """
    from app import agent, client
    p = agent.system_prompt()
    assert p == client.system_prompt("aguas-profundas")
    assert "Aguas Profundas" in p
    assert "[[HANDOFF]]" in p


def test_system_prompt_carries_the_guardrails_into_the_llm_call():
    """The non-negotiables must survive assembly, not just exist in a file."""
    from app import agent
    system = agent._system("CONTEXTO_DE_PRUEBA_KB")
    assert "CONTEXTO_DE_PRUEBA_KB" in system          # KB actually injected
    assert "FUENTES DE CONOCIMIENTO" in system
    assert "[[HANDOFF]]" in system                     # handoff still reachable
    assert "datos bancarios" in system                 # never send bank details
    assert "[[FOTOS_SEPTICO]]" in system
    assert "[[FOTO_AGUA]]" in system


def test_retry_recovers_from_429(monkeypatch):
    """A 429 must NOT reach worker.py.

    OpenAI caps this account at 30k TOKENS/min and each reply costs ~6k, so a
    real burst of customers hits 429. worker.py catches Exception broadly, so an
    un-retried 429 = the customer is silently ghosted. Found by running 90 real
    questions through the live agent; no unit test would have surfaced it.
    """
    import asyncio
    import httpx
    from app import retry

    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.request = None
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True}

    class FakeClient:
        async def post(self, url, **kw):
            calls["n"] += 1
            return FakeResp(429 if calls["n"] < 3 else 200)

    async def go():
        return await retry.post_with_retry(
            FakeClient(), "https://x/y", attempts=5, base_delay=0.001)

    r = asyncio.run(go())
    assert r.status_code == 200
    assert calls["n"] == 3          # failed twice, succeeded on the third


def test_retry_gives_up_and_raises(monkeypatch):
    """After the last attempt it must RAISE, not return a bad response.

    Swallowing the error would send the customer an empty reply, which is worse
    than the worker logging a failure we can see.
    """
    import asyncio
    import pytest
    from app import retry

    class FakeResp:
        status_code = 429
        headers = {}
        request = None
        def raise_for_status(self):
            pass

    class AlwaysThrottled:
        async def post(self, url, **kw):
            return FakeResp()

    async def go():
        return await retry.post_with_retry(
            AlwaysThrottled(), "https://x/y", attempts=2, base_delay=0.001)

    try:
        asyncio.run(go())
        assert False, "should have raised"
    except Exception as e:
        assert "429" in str(e)


def test_llm_calls_go_through_retry():
    """Guard against someone reverting to a bare c.post()."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "app" / "agent.py").read_text()
    assert "post_with_retry" in src
    assert "await c.post(" not in src, "bare post found in agent.py - no retry"
    rag_src = (Path(__file__).parent.parent / "app" / "rag.py").read_text()
    assert "post_with_retry" in rag_src


def test_deposit_bot_fires_from_text_not_a_sentinel():
    """The bank photo must ride on the order message deterministically.

    Sentinel firing measured ~80-90%. A miss here tells the customer "le comparto
    los datos" and sends no photo, at the exact moment they are trying to pay -
    the same broken promise as the [[HANDOFF]]-on-garantia bug. So the engine
    fires it on the order TEXT, which the model either sent or did not.
    """
    from app import client
    sb = client.pack("aguas-profundas").get("salesbot", {})
    trigger = sb.get("deposit_trigger_text")
    assert trigger, "deposit_trigger_text missing"

    # The trigger must actually appear in the order message the model is told to
    # send, or the bank photo never fires.
    prompt = client.system_prompt("aguas-profundas")
    assert trigger in prompt, (
        f"deposit_trigger_text {trigger!r} is not in the order message - "
        "config and prompt have drifted, the bank photo would never fire")

    # It must NOT be a model-fired sentinel.
    assert "[[FOTO_BANCO]]" not in prompt
    assert all("BANCO" not in k for k in sb.get("triggers", {}))


def test_septico_order_does_not_hand_off_before_payment():
    """Handoff used to fire at 'quiero ordenarlo', silencing the agent BEFORE the
    deposit - which made the receipt-handling code dead in the septico path.

    The flow is now: order message + bank photo -> customer pays -> receipt
    arrives -> code acks and hands off. So the prompt must NOT tell the model to
    hand off right after the order message.
    """
    from app import client
    prompt = client.system_prompt("aguas-profundas")
    assert "NO añadas [[HANDOFF]] después de este mensaje" in prompt
    assert "Ya enviaste el mensaje de orden del séptico" not in prompt


def test_no_bank_details_anywhere_in_the_client_pack():
    """The account number and cedula live ONLY in the Kommo Salesbot image.

    This repo is public. A real account number or cedula in the pack, the prompt,
    or the KB is a leak that git history keeps forever.
    """
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent / "clients" / "aguas-profundas"
    bad = re.compile(r"\b\d{9,}\b|banco\s+popular|banreservas|c[eé]dula\s*[:#]?\s*\d",
                     re.I)
    for f in list(root.rglob("*.md")) + list(root.rglob("*.toml")):
        m = bad.search(f.read_text(encoding="utf-8"))
        assert not m, f"possible bank detail in {f.name}: {m.group(0)!r}"
