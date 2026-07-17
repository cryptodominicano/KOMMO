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

    async def get_talk(self, talk_id: str | int) -> dict | None:
        data = await self._req("GET", "/talks", params={"filter[talk_id][]": talk_id})
        if not data:
            return None
        talks = data.get("_embedded", {}).get("talks", [])
        return talks[0] if talks else None
