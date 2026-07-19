"""Property-boundary ("linderos") drawing capture.

Fills the one manual gap in the agua/perforación flow: instead of a técnico
sending a satellite screenshot for the customer to scribble on with the WhatsApp
pencil, the agent sends a link. The customer draws their parcel on a satellite
map, and the marked result flows to the lead card, the WhatsApp chat, and the
owner's email - so the conversation reaches the deposit/payment step the same way
séptico already does.

Self-contained: one HTML page (MapLibre + Terra Draw + Turf, CDN) + these routes.
No separate deploy. Reusable per client.

PROTOTYPE NOTE: satellite imagery uses Esri World Imagery's keyless tiles, which
are fine for a demo but licensed for NON-commercial use. Before this serves
paying customers, swap to a licensed MapTiler/Mapbox satellite key (one line in
the HTML tile URL). Flagged loudly so it is not forgotten.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from .config import settings
from . import client as client_pack
from .kommo import KommoClient, KommoError

log = logging.getLogger("linderos")
router = APIRouter()

_HTML = (Path(__file__).parent / "linderos.html").read_text(encoding="utf-8")
_IMG_DIR = Path("/data/linderos")
try:
    _IMG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass   # test env has no /data; created lazily on first write


# ---------------------------------------------------------------- token (HMAC)
def _secret() -> bytes:
    # Reuse the webhook secret; the link is only a router, not a credential.
    return (settings.webhook_secret or "linderos").encode()


def sign_token(lead_id, talk_id, client_id: str, ttl: int = 86400) -> str:
    payload = {"l": str(lead_id), "t": str(talk_id), "c": client_id,
               "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.new(_secret(), raw, hashlib.sha256).digest()).rstrip(b"=")
    return (raw + b"." + sig).decode()


def verify_token(token: str) -> dict | None:
    try:
        raw, sig = token.encode().split(b".")
        expected = base64.urlsafe_b64encode(hmac.new(_secret(), raw, hashlib.sha256).digest()).rstrip(b"=")
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def build_link(lead_id, talk_id, client_id: str) -> str:
    return f"{settings.public_base_url}/linderos?t={sign_token(lead_id, talk_id, client_id)}"


# ---------------------------------------------------------------- page
_EXPIRED = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<body style='font-family:sans-serif;text-align:center;padding:40px;color:#12203a'>"
            "<h2>Enlace expirado</h2><p>Por favor solicite un nuevo enlace escribiéndonos por WhatsApp.</p>")


@router.get("/linderos", response_class=HTMLResponse)
async def linderos_page(t: str = ""):
    if not verify_token(t):
        return HTMLResponse(_EXPIRED, status_code=410)
    html = (_HTML.replace("__TOKEN__", t).replace("__LAT__", "0").replace("__LNG__", "0"))
    return HTMLResponse(html)


@router.get("/linderos/img/{name}")
async def linderos_img(name: str):
    # basename only; no path traversal
    p = _IMG_DIR / Path(name).name
    if not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="image/jpeg")


# ---------------------------------------------------------------- submit
@router.post("/api/linderos")
async def linderos_submit(request: Request):
    body = await request.json()
    data = verify_token(body.get("token", ""))
    if not data:
        return JSONResponse({"error": "invalid token"}, status_code=403)

    lead_id, talk_id, client_id = data["l"], data["t"], data["c"]
    area_m2 = int(body.get("area_m2") or 0)
    tareas = round(area_m2 / 628.8, 1)
    geojson = body.get("geojson") or {}

    # Persist the marked image if the browser sent one (canvas capture).
    img_url = ""
    image = body.get("image") or ""
    if image.startswith("data:image"):
        try:
            raw = base64.b64decode(image.split(",", 1)[1])
            _IMG_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.jpg"
            (_IMG_DIR / name).write_bytes(raw)
            img_url = f"{settings.public_base_url}/linderos/img/{name}"
        except Exception:
            log.exception("linderos: failed to store image")

    # Deliver everywhere, best-effort (never fail the customer's submit).
    await _deliver(client_id, lead_id, talk_id, area_m2, tareas, img_url, geojson)
    return {"ok": True}


async def _deliver(client_id, lead_id, talk_id, area_m2, tareas, img_url, geojson):
    pack = client_pack.pack(client_id)
    lin = pack.get("linderos", {})
    k = KommoClient()
    summary = (f"Linderos recibidos. Área aproximada: {area_m2:,} m² (~{tareas} tareas).")
    try:
        # 1. Note on the lead (técnico sees it on the card)
        note = summary + (f"\nImagen: {img_url}" if img_url else "")
        try:
            await k.add_lead_note(int(lead_id), note)
        except KommoError as e:
            log.error("linderos: note failed: %s", e)

        # 2. Move to Atención humana + task (visible in the inbox)
        status_id = pack.get("kommo", {}).get("handoff_status_id")
        try:
            if status_id:
                await k.update_lead(int(lead_id), status_id=int(status_id))
            await k.create_task(
                entity_id=int(lead_id),
                text=f"Linderos recibidos ({area_m2:,} m²). Recomendar el punto de perforación.",
                due_seconds=int(float(client_pack.behavior("handoff_task_due_hours", client_id)) * 3600),
                responsible_user_id=int(client_pack.behavior("handoff_task_user_id", client_id)),
            )
        except KommoError as e:
            log.error("linderos: stage/task failed: %s", e)

        # 3. Confirm in the WhatsApp chat (text-only send)
        try:
            await k.send_message(talk_id, lin.get(
                "received_message",
                "¡Recibimos los límites de su terreno! ✅ Un técnico los revisa y le "
                "confirma el mejor punto de perforación en breve."))
        except KommoError as e:
            log.error("linderos: chat confirm failed: %s", e)
    finally:
        await k.aclose()

    # 4. Email the owner via Resend
    owner = lin.get("owner_email")
    if owner and settings.resend_api_key:
        html = (f"<h2>Linderos recibidos</h2>"
                f"<p>Área aproximada: <b>{area_m2:,} m²</b> (~{tareas} tareas).</p>"
                + (f'<p><img src="{img_url}" style="max-width:100%;border-radius:8px"></p>' if img_url else "")
                + f'<pre style="font-size:11px;color:#555">{json.dumps(geojson)[:1500]}</pre>')
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post("https://api.resend.com/emails",
                                 headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                                 json={"from": lin.get("from_email", "Aguas Profundas <aguas@goldcoastai.pro>"),
                                       "to": [owner],
                                       "subject": f"Linderos recibidos — lead {lead_id} ({area_m2:,} m²)",
                                       "html": html})
                if r.status_code >= 300:
                    log.error("linderos: resend %s %s", r.status_code, r.text[:200])
        except Exception:
            log.exception("linderos: email failed")

    log.info("linderos delivered: lead=%s area=%sm2 img=%s", lead_id, area_m2, bool(img_url))
