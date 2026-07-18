"""Diagnose the live 400 on voice transcription, and prove the fix.

Pulls the most recent incoming voice note from Kommo, downloads it (following
Kommo's .ogg -> drive-g .m4a redirect), sniffs the real format from magic bytes,
and calls OpenAI both the OLD way (voice.ogg / octet-stream) and the NEW way
(correct extension) so we see exactly what changed.
"""
import asyncio
import os
import httpx

TOKEN = os.environ["KOMMO_LONG_LIVED_TOKEN"]
BASE = f"https://{os.environ['KOMMO_SUBDOMAIN']}.kommo.com/api/v4"
OAI = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("WHISPER_MODEL_OPENAI", "gpt-4o-mini-transcribe")


def sniff(b: bytes) -> str:
    if b[:4] == b"OggS":
        return "ogg"
    if b[4:8] == b"ftyp":
        return "m4a"
    if b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return "mp3"
    if b[:4] == b"RIFF":
        return "wav"
    if b[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return "unknown"


async def main():
    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as c:
        # find the newest incoming voice across recent talks
        link = None
        for tid in (101, 102, 100):
            r = await c.get(f"{BASE}/talks/{tid}/messages?limit=30",
                            headers={"Authorization": f"Bearer {TOKEN}"})
            for m in r.json().get("_embedded", {}).get("messages", []):
                if m.get("type") == "incoming" and m.get("message_type") in ("voice", "audio"):
                    att = m.get("attachment") or {}
                    if att.get("link"):
                        link = att["link"]
                        print(f"found voice in talk {tid}: {link}")
                        break
            if link:
                break
        if not link:
            print("NO incoming voice note found in talks 100/101/102")
            return

        dl = await c.get(link, headers={"Authorization": f"Bearer {TOKEN}"})
        audio = dl.content
        fmt = sniff(audio)
        print(f"downloaded {len(audio)} bytes | final_url={str(dl.url)[:80]} | sniffed={fmt}")
        print(f"first 12 bytes: {audio[:12]!r}")

        async def try_send(label, filename, ctype):
            files = {"file": (filename, audio, ctype)} if ctype else {"file": (filename, audio)}
            rr = await c.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OAI}"},
                files=files,
                data={"model": MODEL, "language": "es", "response_format": "json",
                      "temperature": "0"},
            )
            body = rr.text[:400]
            print(f"\n[{label}] filename={filename} ctype={ctype}")
            print(f"  HTTP {rr.status_code}: {body}")

        await try_send("OLD (bug)", "voice.ogg", "application/octet-stream")
        await try_send("NEW (fix)", f"voice.{fmt if fmt!='unknown' else 'm4a'}", None)


asyncio.run(main())
