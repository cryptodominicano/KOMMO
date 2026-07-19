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
async def _render_marked_image(geojson: dict) -> bytes | None:
    """Render the parcel with its boundary drawn, SERVER-SIDE, from the coords.

    The browser html2canvas capture drops Leaflet's SVG polygon overlay (tiles
    but no lines). The Esri /export endpoint is disabled on the tiled service
    (500). So we stitch the Esri World Imagery raster TILES that cover the parcel
    (standard Web Mercator math), then draw the polygon with Pillow. Device-
    agnostic; frames tightly on the terrain.
    """
    import io
    import math
    try:
        ring = geojson["coordinates"][0]
        lngs = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        minlng, maxlng = min(lngs), max(lngs)
        minlat, maxlat = min(lats), max(lats)
        padx = (maxlng - minlng) * 0.35 or 0.0008
        pady = (maxlat - minlat) * 0.35 or 0.0008
        minlng -= padx; maxlng += padx; minlat -= pady; maxlat += pady

        def to_px(lat, lng, z):
            n = 2 ** z
            x = (lng + 180.0) / 360.0 * n * 256
            s_ = math.sin(math.radians(lat))
            y = (0.5 - math.log((1 + s_) / (1 - s_)) / (4 * math.pi)) * n * 256
            return x, y

        # pick the deepest zoom (<=19) where the parcel bbox stays under ~1100px wide
        z = 19
        for zz in range(19, 0, -1):
            x0, _ = to_px(maxlat, minlng, zz)
            x1, _ = to_px(maxlat, maxlng, zz)
            if (x1 - x0) <= 1100:
                z = zz
                break

        px_min, py_min = to_px(maxlat, minlng, z)   # top-left pixel
        px_max, py_max = to_px(minlat, maxlng, z)   # bottom-right pixel
        tx0, tx1 = int(px_min // 256), int(px_max // 256)
        ty0, ty1 = int(py_min // 256), int(py_max // 256)

        from PIL import Image, ImageDraw
        canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (20, 33, 61))
        base = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile"
        async with httpx.AsyncClient(timeout=25.0) as c:
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    try:
                        r = await c.get(f"{base}/{z}/{ty}/{tx}")
                        if r.status_code == 200:
                            t = Image.open(io.BytesIO(r.content)).convert("RGB")
                            canvas.paste(t, ((tx - tx0) * 256, (ty - ty0) * 256))
                    except Exception:
                        pass

        draw = ImageDraw.Draw(canvas, "RGBA")

        def px(lng, lat):
            x, y = to_px(lat, lng, z)
            return (x - tx0 * 256, y - ty0 * 256)

        pts = [px(p[0], p[1]) for p in ring]
        draw.polygon(pts, fill=(42, 157, 143, 85))
        draw.line(pts + [pts[0]], fill=(255, 211, 78, 255), width=4)
        for x, y in pts:
            draw.ellipse([x - 5, y - 5, x + 5, y + 5],
                         fill=(255, 211, 78, 255), outline=(18, 32, 58, 255))

        # crop to the parcel bbox + margin
        cx0 = max(0, int(px_min - tx0 * 256) - 45)
        cy0 = max(0, int(py_min - ty0 * 256) - 45)
        cx1 = min(canvas.width, int(px_max - tx0 * 256) + 45)
        cy1 = min(canvas.height, int(py_max - ty0 * 256) + 45)
        canvas = canvas.crop((cx0, cy0, cx1, cy1))
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=86)
        return out.getvalue()
    except Exception:
        log.exception("linderos: server-side render failed")
        return None


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
    img_url, img_b64 = "", ""
    raw = await _render_marked_image(geojson)          # primary: server-side, device-agnostic
    if not raw:
        image = body.get("image") or ""                # fallback: browser capture (may lack lines)
        if image.startswith("data:image"):
            try:
                raw = base64.b64decode(image.split(",", 1)[1])
            except Exception:
                raw = None
    if raw:
        try:
            _IMG_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.jpg"
            (_IMG_DIR / name).write_bytes(raw)
            img_url = f"{settings.public_base_url}/linderos/img/{name}"
            img_b64 = base64.b64encode(raw).decode()
        except Exception:
            log.exception("linderos: failed to store image")

    # Deliver everywhere, best-effort (never fail the customer's submit).
    await _deliver(client_id, lead_id, talk_id, area_m2, tareas, img_url, img_b64, geojson)
    return {"ok": True}


def _centroid(geojson: dict):
    """Average of the polygon ring vertices -> a single (lat, lng) to navigate to.
    Good enough for a small parcel; the técnico just needs a point on the map."""
    try:
        ring = geojson["coordinates"][0]
        pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        lng = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        return round(lat, 6), round(lng, 6)
    except Exception:
        return None, None


def _email_html(brand, footer, name, phone, area_m2, tareas, img_url, lead_id,
                lat=None, lng=None) -> str:
    """Branded, email-client-safe HTML (inline styles, table layout)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    phone_cell = (f'<a href="https://wa.me/{digits}" style="color:#1b6e87;text-decoration:none;font-weight:600">{phone}</a>'
                  if phone else '<span style="color:#9aa4b2">no disponible</span>')
    name_cell = name if name else '<span style="color:#9aa4b2">no disponible</span>'

    def row(label, val):
        return (f'<tr><td style="padding:10px 0;border-bottom:1px solid #eef1f6;font-size:13px;'
                f'color:#6b7280;width:130px;vertical-align:top">{label}</td>'
                f'<td style="padding:10px 0;border-bottom:1px solid #eef1f6;font-size:15px;'
                f'color:#12203a;font-weight:600">{val}</td></tr>')

    img = ("" if not img_url else
           f'<tr><td style="padding:4px 28px 8px">'
           f'<img src="{img_url}" width="100%" alt="Linderos del terreno" '
           f'style="display:block;width:100%;border-radius:10px;border:1px solid #e3e9f2"/>'
           f'<div style="font-size:11px;color:#9aa4b2;margin-top:6px;text-align:center">'
           f'Mapa satelital con los límites marcados por el cliente. Imagen adjunta también.</div></td></tr>')

    if lat is not None and lng is not None:
        maps = f"https://www.google.com/maps?q={lat},{lng}"
        loc_cell = (f'<a href="{maps}" style="color:#1b6e87;text-decoration:none;font-weight:600">'
                    f'{lat}, {lng}</a> <span style="color:#9aa4b2;font-weight:400">(abrir en Maps)</span>')
        loc_row = row("Ubicación", loc_cell)
    else:
        loc_row = ""

    return f"""<!doctype html>
<html><body style="margin:0;background:#eef1f6;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f6;padding:22px 0">
<tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         style="max-width:600px;width:100%;background:#fff;border-radius:14px;overflow:hidden;
                box-shadow:0 2px 10px rgba(20,33,61,.08)">
    <tr><td style="background:#0e213d;padding:20px 28px">
      <div style="color:#35c1b6;font-size:12px;letter-spacing:2px;font-weight:700">{brand.upper()}</div>
      <div style="color:#fff;font-size:20px;font-weight:700;margin-top:2px">Nuevos linderos recibidos</div>
    </td></tr>
    <tr><td style="padding:22px 28px 6px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {row("Cliente", name_cell)}
        {row("WhatsApp", phone_cell)}
        {loc_row}
        {row("Área", f"{area_m2:,} m² &nbsp;·&nbsp; ~{tareas} tareas")}
        {row("Lead", f"#{lead_id}")}
      </table>
    </td></tr>
    {img}
    <tr><td style="padding:14px 28px 22px">
      <div style="font-size:13px;color:#6b7280;line-height:1.5">
        El cliente marcó los límites de su terreno. Un técnico debe revisar el mapa,
        confirmar el mejor punto de perforación y continuar con el cliente por WhatsApp.
      </div>
    </td></tr>
    <tr><td style="background:#f3f6fb;padding:16px 28px;text-align:center">
      <div style="font-size:13px;color:#12203a;font-weight:700">{footer}</div>
      <div style="font-size:11px;color:#9aa4b2;margin-top:2px">goldcoastai.pro · Automatización con IA</div>
    </td></tr>
  </table>
</td></tr></table></body></html>"""


async def _deliver(client_id, lead_id, talk_id, area_m2, tareas, img_url, img_b64, geojson):
    pack = client_pack.pack(client_id)
    lin = pack.get("linderos", {})
    k = KommoClient()
    lat, lng = _centroid(geojson)
    coords = f"{lat}, {lng}" if lat is not None else "n/d"
    summary = (f"Linderos recibidos. Área aproximada: {area_m2:,} m² (~{tareas} tareas). "
               f"Centro del terreno: {coords}.")
    name, phone = "", ""
    try:
        try:
            info = await k.get_lead_contact(int(lead_id))
            name, phone = info.get("name", ""), info.get("phone", "")
        except KommoError as e:
            log.error("linderos: contact lookup failed: %s", e)
        # 1. Note on the lead (técnico sees it on the card)
        note = summary + (f"\nMapa: https://www.google.com/maps?q={lat},{lng}" if lat is not None else "")
        note += (f"\nImagen: {img_url}" if img_url else "")
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
    recipients = owner if isinstance(owner, list) else ([owner] if owner else [])
    recipients = [e for e in recipients if e]
    if recipients and settings.resend_api_key:
        html = _email_html(
            brand=lin.get("email_brand", "Aguas Profundas"),
            footer=lin.get("email_footer", "Gold Coast AI Automations"),
            name=name, phone=phone, area_m2=area_m2, tareas=tareas,
            img_url=img_url, lead_id=lead_id, lat=lat, lng=lng)
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post("https://api.resend.com/emails",
                                 headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                                 json={"from": lin.get("from_email", "Aguas Profundas <aguas@goldcoastai.pro>"),
                                       "to": recipients,
                                       "subject": f"Linderos recibidos — lead {lead_id} ({area_m2:,} m²)",
                                       "html": html,
                                       # the marked map as a real .jpg file, so it shows even when
                                       # the mail client blocks remote (inline) images
                                       "attachments": ([{"filename": "linderos.jpg", "content": img_b64}]
                                                       if img_b64 else [])})
                if r.status_code >= 300:
                    log.error("linderos: resend %s %s", r.status_code, r.text[:200])
        except Exception:
            log.exception("linderos: email failed")

    log.info("linderos delivered: lead=%s area=%sm2 img=%s", lead_id, area_m2, bool(img_url))
