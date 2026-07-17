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
