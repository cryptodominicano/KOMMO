"""Client pack loader.

The engine (app/) is client-agnostic. Everything specific to a business —
prompt, KB, verbatim messages, Kommo subdomain, channel origin — lives in
clients/<id>/. Onboarding a new client is a new directory, not a code change.
"""
try:
    import tomllib                    # stdlib from Python 3.11 (container is 3.12)
except ModuleNotFoundError:          # older interpreter, e.g. a dev box
    import tomli as tomllib
from functools import lru_cache
from pathlib import Path
from .config import settings

ROOT = Path(__file__).parent.parent / "clients"


@lru_cache(maxsize=8)
def pack(client_id: str | None = None) -> dict:
    cid = client_id or settings.client_id
    path = ROOT / cid / "client.toml"
    if not path.exists():
        raise FileNotFoundError(f"no client pack at {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    data["_dir"] = str(path.parent)
    return data


def system_prompt(client_id: str | None = None) -> str:
    p = Path(pack(client_id)["_dir"]) / "prompts" / "system.md"
    return p.read_text(encoding="utf-8")


def msg(key: str, client_id: str | None = None) -> str:
    return pack(client_id)["messages"][key]


def behavior(key: str, client_id: str | None = None):
    return pack(client_id)["behavior"][key]
