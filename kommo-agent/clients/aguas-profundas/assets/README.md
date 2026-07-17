# Client assets — Aguas Profundas

Marketing images, committed here deliberately.

**Why in the repo:** the previous set lived on Botpress's CDN
(`files.bpcontent.cloud`) and died when we left the platform. The set before
that lived on ImgBB, a free host. Both are the same failure: **client assets on
infrastructure the client does not own.** These are versioned with the code and
cannot vanish.

**Permanent URLs** (public repo, no auth):

| Asset | Use |
|---|---|
| `welcome-todo-comienza.jpg` | Welcome — all 3 services + prices |
| `agua-pasos-proceso.jpg` | Water / drilling — the 6-step process |
| `septico-promo-1.jpg` | Séptico intro |
| `septico-promo-2.jpg` | Séptico intro |
| `septico-promo-3.jpg` | Séptico intro |

Raw:
`https://raw.githubusercontent.com/cryptodominicano/KOMMO/main/kommo-agent/clients/aguas-profundas/assets/<file>`

jsDelivr CDN (preferred for messaging — correct content-type, CDN-cached):
`https://cdn.jsdelivr.net/gh/cryptodominicano/KOMMO@main/kommo-agent/clients/aguas-profundas/assets/<file>`

> ⚠️ **The agent cannot send these today.** Kommo's
> `POST /api/v4/talks/{talk_id}/send_message` is **text-only** ("Support for file
> uploads will be implemented in an upcoming release"). These URLs are staged for
> the day that ships. Until then the agent hands off when a customer asks for
> photos, and a técnico sends them from the inbox.
