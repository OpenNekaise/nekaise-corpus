#!/usr/bin/env python3
"""blocklist.py — persistent set of URLs the pruner has dropped (pruned_urls.txt, committed).

Why: prune removes a dropped doc from BOTH the manifest and the registry, so without this file the
discovery scripts (find_sources / find_github / find_osti / find_books / find_archive) re-find,
re-append, re-fetch, and re-prune the same URLs every round (~200 wasted fetches/round). Every
finder dedups against blocklist.load(); prune_corpus --apply calls blocklist.add() for what it drops.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
PATH = ROOT / "pruned_urls.txt"


def normalize(u: str) -> str:
    return (u or "").strip().rstrip("/")


def load() -> set[str]:
    if not PATH.exists():
        return set()
    return {normalize(l) for l in PATH.read_text().splitlines() if l.strip()}


def add(urls) -> int:
    """Record new URLs (deduped, sorted) in the blocklist through the store (ADR 0001 stage 3):
    through the broker of the round or maintenance window this runs in, otherwise in one
    standalone store transaction (store_broker.run_batch). The file store appends them to
    pruned_urls.txt exactly as the legacy writer did. Returns how many were new; nothing is
    written (no transaction) when none are."""
    import store  # store imports this module
    import store_broker

    new = sorted({normalize(u) for u in urls if u and normalize(u)} - load())
    if not new:
        return 0
    _, (added,) = store_broker.run_batch(store.open(root=PATH.parent), "blocklist",
                                         lambda view, batch: batch.blocklist_add(new))
    return added
