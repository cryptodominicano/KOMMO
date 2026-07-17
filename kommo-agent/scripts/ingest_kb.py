#!/usr/bin/env python3
"""Ingest the Aguas Profundas KB into Qdrant.

Matches the existing account convention: 1536-dim vectors, Cosine distance
(OpenAI text-embedding-3-small), same as every other collection on this VPS.

Chunks on markdown H2 headings so each Q/A or objection stays intact.
"""
import os
import sys
import re
import uuid
import httpx
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

QDRANT_URL = os.getenv("QDRANT_URL", "http://172.20.0.10:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "aguas_profundas_kb")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
# The KB lives inside the CLIENT PACK, not at the repo root. This pointed at
# kommo-agent/kb (which has never existed) - a leftover from before the engine
# was made client-agnostic. It would have created an empty collection and left
# the agent answering with no knowledge at all, silently.
CLIENT_ID = os.getenv("CLIENT_ID", "aguas-profundas")
KB_DIR = Path(__file__).parent.parent / "clients" / CLIENT_ID / "kb"


def chunk_markdown(text: str, source: str) -> list[dict]:
    """Split on H2 (##). Keep the H1 title as context on every chunk."""
    title = ""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    parts = re.split(r"\n(?=##\s)", text)
    out = []
    for p in parts:
        body = p.strip()
        if len(body) < 40:
            continue
        heading = ""
        hm = re.match(r"^##\s+(.+)$", body, re.M)
        if hm:
            heading = hm.group(1).strip()
        out.append({
            "text": f"{title}\n\n{body}" if title and not body.startswith("# ") else body,
            "source": source,
            "title": title,
            "heading": heading,
        })
    return out


def embed(texts: list[str]) -> list[list[float]]:
    r = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={"model": EMBED_MODEL, "input": texts},
        timeout=60.0,
    )
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]


def main() -> int:
    if not OPENAI_KEY:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    if not KB_DIR.is_dir():
        print(f"ERROR: KB dir does not exist: {KB_DIR}", file=sys.stderr)
        return 1
    files = sorted(KB_DIR.glob("*.md"))
    if not files:
        print(f"ERROR: no .md files in {KB_DIR}", file=sys.stderr)
        return 1

    chunks: list[dict] = []
    for f in files:
        c = chunk_markdown(f.read_text(encoding="utf-8"), f.name)
        chunks.extend(c)
        print(f"  {f.name}: {len(c)} chunks")

    print(f"total chunks: {len(chunks)}")
    vectors = embed([c["text"] for c in chunks])
    dim = len(vectors[0])
    print(f"embedding dim: {dim}")
    assert dim == 1536, f"expected 1536 to match existing collections, got {dim}"

    client = QdrantClient(url=QDRANT_URL)
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION in existing:
        print(f"recreating collection {COLLECTION}")
        client.delete_collection(COLLECTION)
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    client.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(id=str(uuid.uuid4()), vector=v, payload=c)
            for v, c in zip(vectors, chunks)
        ],
    )
    info = client.get_collection(COLLECTION)
    print(f"OK: {COLLECTION} now has {info.points_count} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
