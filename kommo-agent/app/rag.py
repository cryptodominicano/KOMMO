"""Qdrant retrieval over plain async HTTP.

WHY NOT qdrant-client: it is synchronous. Calling it from async code blocks the
event loop, which under concurrency also threatens Kommo's hard 2-second webhook
ack. Qdrant's REST API is trivial, so we use httpx and stay fully async.

Collections follow the account convention: 1536-dim, Cosine (text-embedding-3-small).
"""
import httpx
from .config import settings
from . import client as client_pack


async def embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.embed_model, "input": text},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


async def retrieve(query: str, top_k: int | None = None) -> str:
    """Concatenated KB context.

    top_k is deliberately generous. These KBs are small (~4k tokens), so a high k
    effectively returns everything relevant, removing the retrieval-miss failure
    mode that forced a 'search these keywords' hack on the previous platform.
    """
    k = top_k or settings.rag_top_k
    coll = client_pack.pack()["client"]["qdrant_collection"]
    try:
        vec = await embed(query)
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{settings.qdrant_url}/collections/{coll}/points/query",
                json={"query": vec, "limit": k, "with_payload": True},
            )
            r.raise_for_status()
            points = r.json()["result"]["points"]
    except Exception as e:
        # RAG failure must never take down the reply path.
        return f"[KB unavailable: {e}]"

    chunks = [
        f"### {p.get('payload', {}).get('source', 'kb')}\n"
        f"{p.get('payload', {}).get('text', '')}"
        for p in points if p.get("payload", {}).get("text")
    ]
    return "\n\n".join(chunks) if chunks else "[KB empty]"
