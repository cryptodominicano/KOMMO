"""Retry with exponential backoff + jitter for upstream API calls.

WHY THIS EXISTS: OpenAI enforces a TOKEN-per-minute limit (30k TPM on this
account), not just a request limit. Each reply sends the system prompt plus the
retrieved KB - roughly 6k tokens - so only about five replies per minute fit in
the budget. A lunchtime burst of real customers WILL hit 429.

Without this, a 429 propagated up to worker.py, which catches Exception broadly
and logs. The customer got silence. Same silent-ghost failure as the prompt-path
bug: the container stays healthy, the webhook still returns 200, and the person
on WhatsApp just never hears back. Discovered by running 90 real questions
through the live agent - never by a unit test.

Respects Retry-After when the server sends it. Jitter prevents a thundering herd
when several conversations are throttled at the same moment.
"""
import asyncio
import logging
import random

import httpx

log = logging.getLogger("retry")

RETRY_STATUS = {429, 500, 502, 503, 504}


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    attempts: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    **kwargs,
) -> httpx.Response:
    """POST, retrying transient upstream failures. Raises on final failure."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = await client.post(url, **kwargs)
            if r.status_code not in RETRY_STATUS:
                r.raise_for_status()
                return r
            # Honour the server's own advice when present.
            retry_after = r.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() \
                else min(base_delay * (2 ** i), max_delay)
            last = httpx.HTTPStatusError(
                f"{r.status_code} from {url}", request=r.request, response=r)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            delay = min(base_delay * (2 ** i), max_delay)

        if i == attempts - 1:
            break
        delay += random.uniform(0, delay * 0.25)      # jitter
        log.warning("upstream %s failed (attempt %d/%d), retrying in %.1fs",
                    url.rsplit("/", 1)[-1], i + 1, attempts, delay)
        await asyncio.sleep(delay)

    assert last is not None
    raise last
