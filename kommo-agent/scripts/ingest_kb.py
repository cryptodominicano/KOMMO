#!/usr/bin/env python3
"""Ingest a client's KB into Qdrant.

WHY httpx AND NOT qdrant-client: the app dropped qdrant-client because it is
synchronous and blocks the event loop (which threatens Kommo's hard 2-second
webhook ack). This script imported it anyway and died at deploy time on
ModuleNotFoundError. Qdrant's REST API is trivial - use it, add no dependency,
and stay consistent with app/rag.py.

The collection name comes from the CLIENT PACK, exactly like app/rag.py reads
it. It used to come from an env var here, which meant two sources of truth that
happened to agree; the first time they disagreed, ingest would have written to
one collection while the agent read from another, and the agent would have
answered from nothing with no error anywhere.

Vectors: 1536-dim, Cosine (OpenAI text-embedding-3-small) - the account
convention shared by every other collection on this VPS.

Chunks on markdown H2 so each Q/A or objection stays intact.
"""
import os
import sys
import re
import uuid
import httpx
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import client as client_pack  # noqa: E402

QDRANT_URL = os.getenv("QDRANT_URL", "http://172.20.0.10:6333")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
CLIENT_ID = os.getenv("CLIENT_ID", "aguas-profundas")

# The KB lives inside the client pack, not at the repo root.
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

    # Same source of truth the agent reads at query time.
    collection = client_pack.pack(CLIENT_ID)["client"]["qdrant_collection"]
    print(f"client={CLIENT_ID} collection={collection}")

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

    with httpx.Client(timeout=60.0) as c:
        existing = [x["name"] for x in
                    c.get(f"{QDRANT_URL}/collections").json()["result"]["collections"]]
        if collection in existing:
            print(f"recreating collection {collection}")
            c.delete(f"{QDRANT_URL}/collections/{collection}").raise_for_status()
        c.put(
            f"{QDRANT_URL}/collections/{collection}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
        ).raise_for_status()
        c.put(
            f"{QDRANT_URL}/collections/{collection}/points?wait=true",
            json={"points": [
                {"id": str(uuid.uuid4()), "vector": v, "payload": p}
                for v, p in zip(vectors, chunks)
            ]},
        ).raise_for_status()
        info = c.get(f"{QDRANT_URL}/collections/{collection}").json()["result"]

    print(f"OK: {collection} now has {info['points_count']} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
