"""Kommo API v4 client.

Only the endpoints we verified in the docs:
  GET  /talks/{id}/messages   - read history (does NOT consume add-on limits)
  POST /talks/{id}/send_message - send text (add-on; TEXT ONLY today)
  GET  /talks                 - find talks
"""
import httpx
from typing import Any
from .config import settings


class KommoError(RuntimeError):
    pass


class KommoClient:
    def __init__(self, token: str | None = None):
        self.token = token or settings.kommo_long_lived_token
        self._client = httpx.AsyncClient(
            base_url=settings.kommo_base,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw) -> Any:
        # Observability (KOMMO_TRACE, default OFF): log every WRITE with its body.
        # This is the exact trace that cracked the Aug-24 lead-mover mystery — a
        # PATCH /leads that carried only {'name': ...} proved the engine was not
        # moving the lead. Reads (GET) are skipped (high-volume, low-value).
        # Never raises: a trace bug must not break a Kommo call.
        if getattr(settings, "kommo_trace", False) and method.upper() in (
                "POST", "PATCH", "DELETE", "PUT"):
            try:
                import logging as _lg
                # Content handling is market-aware (kommo_trace_redact_content,
                # default False). In the DR, bank account numbers + cédulas are
                # routinely shared with customers for transfers, so message bodies
                # are NOT treated as sensitive-in-logs and full bodies aid debugging.
                # For regulated markets (US/EU) set the flag True to redact outbound
                # message/note text (per OTel GenAI content-capture guidance).
                # NOTE: this is separate from the hard rule that bank details never
                # enter the PUBLIC git repo / prompt / KB (prompt-injection + public
                # repo), which is enforced by the client-pack grep test regardless.
                if (getattr(settings, "kommo_trace_redact_content", False)
                        and ("/send_message" in path or "/notes" in path)):
                    _body = "<redacted: message/note text>"
                else:
                    _body = kw.get("json")
                _lg.getLogger("kommo").info(
                    "KOMMO_WRITE %s %s body=%s", method.upper(), path, _body)
            except Exception:
                pass
        r = await self._client.request(method, path, **kw)
        if r.status_code == 204:
            return None
        if r.status_code == 402:
            raise KommoError(f"402 quota/tariff: {r.text}")  # "Over chat API limit"
        if r.status_code == 403:
            raise KommoError(f"403 scope: {r.text}")          # missing chat scopes
        if r.status_code == 422:
            raise KommoError(f"422 talk closed: {r.text}")
        if r.status_code >= 400:
            raise KommoError(f"{r.status_code}: {r.text}")
        return r.json() if r.content else None

    async def send_message(self, talk_id: str | int, text: str) -> dict:
        """POST /talks/{talk_id}/send_message -> 202. TEXT ONLY (per docs)."""
        return await self._req("POST", f"/talks/{talk_id}/send_message", json={"text": text})

    async def get_messages(self, talk_id: str | int, limit: int = 50) -> list[dict]:
        """GET /talks/{talk_id}/messages. Free of add-on quota."""
        data = await self._req("GET", f"/talks/{talk_id}/messages", params={"limit": limit})
        if not data:
            return []
        return data.get("_embedded", {}).get("messages", [])

    async def run_bot(self, bot_id: int | str, entity_id: int | str,
                      entity_type: str = "leads") -> None:
        """POST /bots/{id}/run -> 202 (queued, not sent).

        THE IMAGE WORKAROUND. send_message is text-only, but a Salesbot Message
        step CAN carry images ("Supported file types include: Documents, Images,
        Videos, Audio files"). The payload is built once in the UI; we trigger it
        from code. The agent decides WHEN, Salesbot carries WHAT.

        Gotchas:
          - One bot per entity. A second launch on the same entity silently
            blocks while another bot is running.
          - 202 means queued; there is no synchronous send confirmation.
          - Does NOT go through /talks/{id}/send_message, so it very likely does
            not consume Chats API add-on quota (unverified).
        """
        await self._req("POST", f"/bots/{bot_id}/run",
                        json={"entity_id": int(entity_id), "entity_type": entity_type})

    async def get_lead_contact(self, lead_id: int | str) -> dict:
        """Name + WhatsApp number for a lead's main contact.
        Kommo stores the phone as a contact custom field with field_code PHONE."""
        lead = await self._req("GET", f"/leads/{lead_id}?with=contacts")
        lead_name = (lead or {}).get("name", "")
        contacts = (lead or {}).get("_embedded", {}).get("contacts", [])
        if not contacts:
            return {"name": lead_name, "phone": "", "lead_name": lead_name}
        cid = next((c["id"] for c in contacts if c.get("is_main")), contacts[0]["id"])
        c = await self._req("GET", f"/contacts/{cid}") or {}
        phone = ""
        for f in c.get("custom_fields_values") or []:
            if f.get("field_code") == "PHONE":
                vals = f.get("values") or []
                if vals:
                    phone = vals[0].get("value", "")
                    break
        return {"name": c.get("name") or lead_name, "phone": phone, "lead_name": lead_name}

    async def get_contact_leads(self, contact_id: int) -> list[int]:
        """Return lead IDs linked to a contact, most-recently-updated first.
        Used when entity_id is null (contact messaged without an open lead).
        Best practice: never auto-create a lead; just resolve the existing one."""
        data = await self._req(
            "GET",
            f"/contacts/{contact_id}?with=leads"
        ) or {}
        leads = data.get("_embedded", {}).get("leads", []) or []
        # Sort by id descending (higher id = more recent)
        return [l["id"] for l in sorted(leads, key=lambda x: x.get("id", 0), reverse=True)]

    async def get_contact_tags(self, contact_id: int) -> list[str]:
        """Lowercased tag names on a contact. Used for BLOQUEADO/NO_REACTIVAR
        so a block persists across future leads from the same number."""
        data = await self._req("GET", f"/contacts/{contact_id}") or {}
        tags = data.get("_embedded", {}).get("tags", []) or []
        return [t["name"].lower() for t in tags if t.get("name")]

    async def get_lead_tags(self, lead_id: int | str) -> list[str]:
        """Lowercased tag names on a lead. Used for NO_REACTIVAR (a human
        can permanently silence the agent by tagging the lead)."""
        lead = await self._req("GET", f"/leads/{lead_id}?with=tags") or {}
        tags = lead.get("_embedded", {}).get("tags", []) or []
        return [str(t.get("name", "")).strip().lower() for t in tags]

    async def get_lead_status(self, lead_id: int | str) -> int | None:
        """Current pipeline stage (status_id) of a lead, or None on failure.
        Used to keep the bot silent while a lead sits in the human-handoff
        stage — the stage is the native, board-visible source of truth for
        'a human owns this conversation'. A human moving the lead to any other
        stage naturally reactivates the bot."""
        lead = await self._req("GET", f"/leads/{lead_id}") or {}
        sid = lead.get("status_id")
        try:
            return int(sid) if sid is not None else None
        except (TypeError, ValueError):
            return None

    async def tag_lead_contact(self, lead_id: int | str, tag: str) -> dict | None:
        """Add a tag to the lead's MAIN contact. Geographic/audience data belongs
        to the PERSON (persists across deals, and broadcasts target contacts), not
        the deal. Merges so existing tags are kept. Idempotent."""
        lead = await self._req("GET", f"/leads/{lead_id}?with=contacts") or {}
        contacts = lead.get("_embedded", {}).get("contacts", []) or []
        if not contacts:
            return None
        cid = next((c["id"] for c in contacts if c.get("is_main")), contacts[0]["id"])
        c = await self._req("GET", f"/contacts/{cid}?with=tags") or {}
        names = {str(t.get("name", "")).strip()
                 for t in c.get("_embedded", {}).get("tags", []) or [] if t.get("name")}
        if tag in names:
            return None
        names.add(tag)
        return await self._req("PATCH", f"/contacts/{cid}",
                               json={"_embedded": {"tags": [{"name": n} for n in sorted(names)]}})

    async def add_lead_tag(self, lead_id: int | str, tag: str) -> dict | None:
        """Add ONE tag to a lead without dropping the others. PATCH replaces the
        whole tag set, so we read the existing tags and merge. Idempotent."""
        lead = await self._req("GET", f"/leads/{lead_id}?with=tags") or {}
        existing = lead.get("_embedded", {}).get("tags", []) or []
        names = {str(t.get("name", "")).strip() for t in existing if t.get("name")}
        if tag in names:
            return None
        names.add(tag)
        return await self._req("PATCH", f"/leads/{lead_id}",
                               json={"_embedded": {"tags": [{"name": n} for n in sorted(names)]}})

    async def add_lead_note(self, lead_id: int | str, text: str) -> dict | None:
        """POST /leads/{id}/notes - a common note visible on the lead card."""
        body = [{"note_type": "common", "params": {"text": text}}]
        return await self._req("POST", f"/leads/{lead_id}/notes", json=body)

    async def update_lead(self, lead_id: int | str, **fields) -> dict | None:
        """PATCH /leads/{id}. Used to move a lead to the handoff stage."""
        return await self._req("PATCH", f"/leads/{lead_id}", json=fields)

    async def create_task(self, entity_id: int | str, text: str,
                          due_seconds: int, responsible_user_id: int,
                          entity_type: str = "leads", task_type_id: int = 1) -> dict | None:
        """POST /tasks. A task PINGS the human, unlike an unanswered chat.
        complete_till is a unix timestamp; text + complete_till are required."""
        import time as _t
        body = [{
            "text": text,
            "complete_till": int(_t.time()) + int(due_seconds),
            "entity_id": int(entity_id),
            "entity_type": entity_type,
            "responsible_user_id": int(responsible_user_id),
            "task_type_id": task_type_id,
        }]
        return await self._req("POST", "/tasks", json=body)

    async def get_talk(self, talk_id: str | int) -> dict | None:
        data = await self._req("GET", "/talks", params={"filter[talk_id][]": talk_id})
        if not data:
            return None
        talks = data.get("_embedded", {}).get("talks", [])
        return talks[0] if talks else None
