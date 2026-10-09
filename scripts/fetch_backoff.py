"""Reversible host backoff for HTTP 406 downloads.

A negotiation/refusal response is not evidence that a URL is dead. Keep its host quiet for
at least a day (longer Retry-After wins), without changing identity or retrying a transport.
This is local operational state, not an eligibility or rights decision. Losing the scratch
file costs a retry; the existing per-document retry limits still apply.
Discovery keeps queuing candidates: skipping cooling copies there would advance its cursor
past them without retaining their provenance. The loader refuses their network requests.
"""
from __future__ import annotations

import email.utils
import math
import time
from pathlib import Path

import host_policy
from openalex_state import Cooldowns

ROOT = Path(__file__).resolve().parents[1]
MIN_SECONDS = 86400


def _state(root: Path | None = None) -> Cooldowns:
    return Cooldowns((root or ROOT) / "workspace" / "fetch-406-cooldowns.json")


def active(url: str, *, root: Path | None = None) -> float | None:
    """Expiry for the exact response host; parent/sibling hosts are not inferred."""
    return _state(root).active(host_policy.canonical_host(url))


def defer(url: str, retry_after: str | None, *, root: Path | None = None) -> float:
    now = time.time()
    until = now + MIN_SECONDS
    if retry_after:
        try:
            delay = float(retry_after)
            suggested = now + delay if math.isfinite(delay) else until
        except ValueError:
            try:
                suggested = email.utils.parsedate_to_datetime(retry_after).timestamp()
            except (TypeError, ValueError, OverflowError):
                suggested = until
        until = max(until, suggested)
    host = host_policy.canonical_host(url)
    # Cooldowns performs a locked, atomic max-merge, so workers cannot shorten another hold.
    return _state(root).raise_to({host: until})[host]
