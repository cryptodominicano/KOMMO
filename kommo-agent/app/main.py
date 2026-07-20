"""FastAPI entrypoint.

Kommo gives us a HARD 2-second window to respond to a webhook, and disables the
hook after >100 invalid responses in 2 hours. So: parse, ack, and do all real
work (Whisper, Claude, Qdrant) in the background. Never inline.
"""
import logging
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks, Response, HTTPException

from .config import settings
from . import state, client as client_pack
from .worker import handle_message
from . import linderos

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern FastAPI startup/shutdown (on_event is deprecated)."""
    state.init()
    pack = client_pack.pack()
    log.info(
        "client=%s origin=%s collection=%s llm=%s/%s transcribe=%s",
        pack["client"]["id"], pack["kommo"]["origin"],
        pack["client"]["qdrant_collection"],
        settings.llm_provider,
        settings.openai_model if settings.llm_provider == "openai" else settings.claude_model,
        settings.transcribe_provider,
    )
    if not settings.webhook_secret:
        log.warning("WEBHOOK_SECRET is empty - the webhook endpoint will reject everything")
    yield


app = FastAPI(title="kommo-agent", version="1.0.0", lifespan=lifespan)
app.include_router(linderos.router)   # /linderos, /api/linderos, /linderos/img/*

_KEY_RE = re.compile(r"(\w+)|\[(\w*)\]")


def parse_php_form(form: dict) -> dict:
    """Kommo posts webhooks as x-www-form-urlencoded with PHP-style nested keys:
        add[0][id]=...&add[0][text]=...
    Rebuild that into nested dicts/lists."""
    root: dict = {}
    for raw_key, value in form.items():
        parts = [m.group(1) or m.group(2) for m in _KEY_RE.finditer(raw_key)]
        if not parts:
            continue
        node = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, {})
    return root


def _as_list(node) -> list:
    """PHP-style numeric-keyed dicts -> list."""
    if isinstance(node, dict):
        if all(k.isdigit() for k in node.keys()) and node:
            return [node[k] for k in sorted(node, key=int)]
        return [node]
    if isinstance(node, list):
        return node
    return []


@app.get("/health")
async def health():
    return {"ok": True, "subdomain": settings.kommo_subdomain,
            "provider": settings.transcribe_provider}


@app.post("/webhook/kommo/{secret}")
async def kommo_webhook(secret: str, request: Request, background: BackgroundTasks):
    """Kommo `add_message`. ACK FAST — everything else is background.

    SECURITY: Kommo's general webhooks carry NO signature (only the Chats API
    custom-channel hooks do), so we cannot verify authenticity. A secret in the
    path is the available defence: without it, anyone who learns this URL could
    POST fake customer messages and drive the agent.
    """
    if not settings.webhook_secret or secret != settings.webhook_secret:
        log.warning("rejected webhook with bad secret")
        raise HTTPException(status_code=404)
    try:
        form = dict(await request.form())
        payload = parse_php_form(form)
    except Exception:
        log.exception("failed to parse webhook body")
        return Response(status_code=200)   # never make Kommo retry a parse bug

    # `add_message` arrives UNWRAPPED: {"add":[{...}]}
    items = _as_list(payload.get("add") or payload.get("message", {}).get("add"))

    queued = 0
    for msg in items:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "incoming":
            continue                                     # ignore our own/outgoing
        kcfg = client_pack.pack()["kommo"]
        allowed = [o.lower() for o in (kcfg.get("origins")
                    or ([kcfg["origin"]] if kcfg.get("origin") else []))]
        origin = (msg.get("origin") or "").lower()
        if allowed and origin and origin not in allowed:
            log.info("skipping origin=%s (not in allow-list)", origin)
            continue
        mid = str(msg.get("id") or "")
        if mid and state.already_seen(mid, settings.dedupe_ttl_seconds):
            log.info("duplicate message %s ignored", mid)
            continue
        background.add_task(handle_message, msg)
        queued += 1

    log.info("acked webhook, queued=%d", queued)
    return {"ok": True, "queued": queued}
