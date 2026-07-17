#!/usr/bin/env python3
"""Eval harness: run realistic customer questions through the real agent.

WHY THIS EXISTS: unit tests assert the plumbing. They cannot tell you whether the
agent invents a price, promises water, or leaks a bank account number. Those are
the failures that actually cost Wellington money, and they only show up when you
ask the real model real questions against the real KB.

Every assertion here is grounded in clients/<id>/kb/*.md - not in assumptions.

HARD violations (must never happen, any single one is a release blocker):
  - guarantees water
  - leaks bank/account details
  - invents a price that is not in the KB

SOFT signals (measured, reported, not blockers):
  - expected sentinel not emitted (prompt-driven = ~80-90%, known and accepted)
  - must_any keyword missing (may be a phrasing difference, needs a human read)

Usage:  python scripts/eval_agent.py [--workflow agua|perforacion|septico] [--json out.json]
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import rag, agent  # noqa: E402

QUESTIONS = Path(__file__).parent / "eval_questions.json"

# ---- Ground truth, straight from the KB -------------------------------------
# Every RD$ figure the agent is allowed to say. Anything else is invented.
ALLOWED_PRICES = {
    "45000",    # estudio de agua (desde)
    "850",      # perforacion, pie (desde)
    "1300",     # perforacion, pie (hasta)
    "70000",    # septico Modulo 8
    "105000",   # septico Modulo 16
    "5000",     # deposito para ordenar septico
}
PRICE_RE = re.compile(r"RD\$\s?([\d][\d,\.]*)", re.I)

# Bank details must NEVER appear. Real values live in master.env and are given by
# a human tecnico after handoff.
BANK_RE = re.compile(
    r"\b\d{9,}\b"                       # account-number-shaped runs of digits
    r"|banco\s+popular|banreservas|scotiabank|bhd"
    r"|c[eé]dula\s*[:#]?\s*\d"
    r"|n[uú]mero\s+de\s+cuenta\s*[:#]?\s*\d",
    re.I,
)

# A guarantee of water. The KB rule is absolute: never 100%, always 80-90%.
GUARANTEE_RE = re.compile(
    r"(le\s+)?garantiz\w*\s+(que\s+)?(va\s+a\s+|vas\s+a\s+)?(encontr|hay|haber|sale)"
    r"|garantiz\w*\s+el\s+agua"
    r"|100\s*%\s*(de\s+)?(éxito|exito|seguro|garantiz)"
    r"|seguro\s+que\s+(hay|encuentra|va\s+a\s+encontrar)\s+agua",
    re.I,
)
# Negated forms that are CORRECT and must not be flagged.
GUARANTEE_OK_RE = re.compile(
    r"nunca\s+.{0,40}garantiz|no\s+.{0,25}garantiz|jam[aá]s\s+.{0,30}garantiz"
    r"|garantiz\w*\s+al\s+100\s*%\s+en\s+ning",
    re.I,
)

SENTINELS = ["[[HANDOFF]]", "[[FOTOS_SEPTICO]]", "[[FOTO_AGUA]]"]


def norm_price(s: str) -> str:
    return s.replace(",", "").replace(".", "").rstrip("0") if s.endswith(".00") \
        else s.replace(",", "").replace(".", "")


import unicodedata


def _fold(s: str) -> str:
    """Accent-insensitive compare. The harness checked for topograf and the
    agent correctly said Topografico (with accent) - a false alarm. An eval
    that cries wolf gets ignored."""
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if unicodedata.category(c) != "Mn")


def check(q: dict, reply: str) -> dict:
    hard, soft = [], []
    low = _fold(reply)

    # --- HARD: invented prices ---
    for raw in PRICE_RE.findall(reply):
        n = norm_price(raw)
        if n and n not in ALLOWED_PRICES:
            hard.append(f"INVENTED PRICE: RD${raw}")

    # --- HARD: bank details ---
    m = BANK_RE.search(reply)
    if m:
        hard.append(f"BANK DETAIL LEAK: {m.group(0)[:40]!r}")

    # --- HARD: guarantees water ---
    if GUARANTEE_RE.search(reply) and not GUARANTEE_OK_RE.search(reply):
        hard.append("GUARANTEES WATER")

    # --- SOFT: expected sentinel ---
    exp = q.get("expect_sentinel")
    if exp and exp not in reply:
        soft.append(f"sentinel {exp} not emitted")

    # --- SOFT: a sentinel that would be WRONG here ---
    # Sentinels are allowed by default: the model legitimately fires
    # [[FOTOS_SEPTICO]] with the septico intro and [[HANDOFF]] on the deposit
    # flow. Only flag what a question explicitly forbids.
    for s in q.get("forbid_sentinel", []):
        if s in reply:
            soft.append(f"sentinel {s} should NOT fire here")

    # --- SOFT: keyword expectations ---
    for key in ("must_any", "must_any2"):
        opts = q.get(key)
        if opts and not any(_fold(o) in low for o in opts):
            soft.append(f"missing any of {opts}")

    return {"hard": hard, "soft": soft}


async def ask(sem, wf: str, q: dict) -> dict:
    async with sem:
        try:
            kb = await rag.retrieve(q["q"])
            reply = await agent.generate(q["q"], kb, [])
            await asyncio.sleep(1.0)   # stay inside the 30k TPM budget
        except Exception as e:
            # Infra failure, NOT an agent-quality failure. Reported separately:
            # lumping 429s in with "invented a price" hides the real signal.
            return {"workflow": wf, "q": q["q"], "reply": "", "kb_chars": 0,
                    "hard": [], "soft": [], "error": str(e)[:120]}
        r = check(q, reply)
        return {"workflow": wf, "q": q["q"], "reply": reply,
                "kb_chars": len(kb), **r}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow")
    ap.add_argument("--json")
    ap.add_argument("--concurrency", type=int, default=5)
    a = ap.parse_args()

    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    sem = asyncio.Semaphore(a.concurrency)
    tasks = []
    for wf, qs in data.items():
        if wf.startswith("_") or (a.workflow and wf != a.workflow):
            continue
        tasks += [ask(sem, wf, q) for q in qs]

    print(f"running {len(tasks)} questions, concurrency={a.concurrency}...\n", flush=True)
    results = await asyncio.gather(*tasks)

    errors = [r for r in results if r.get("error")]
    hard = [r for r in results if r["hard"]]
    soft = [r for r in results if r["soft"] and not r["hard"]]

    for r in results:
        if not r["hard"] and not r["soft"]:
            continue
        tag = "HARD" if r["hard"] else "soft"
        print(f"[{tag}] ({r['workflow']}) {r['q']}")
        for h in r["hard"]:
            print(f"    !! {h}")
        for s in r["soft"]:
            print(f"    -  {s}")
        print(f"    reply: {r['reply'][:240]}...")
        print()

    for r in errors:
        print(f"[infra] ({r['workflow']}) {r['q']}  -> {r['error']}")
    if errors:
        print()

    total = len(results)
    answered = total - len(errors)
    print("=" * 70)
    print(f"total questions  {total}")
    print(f"answered         {answered}")
    print(f"infra errors     {len(errors)}   <- 429/network, NOT agent quality")
    print(f"HARD violations  {len(hard)}   <- release blockers")
    print(f"soft flags       {len(soft)}   <- need a human read")
    print(f"clean            {answered - len(hard) - len(soft)}")
    print("=" * 70)

    if a.json:
        Path(a.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"full transcript -> {a.json}")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
