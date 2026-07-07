#!/usr/bin/env python3
"""blocklist.py — persistent set of URLs the pruner has dropped (pruned_urls.txt, committed).

Why: prune removes a dropped doc from BOTH the manifest and sources.yaml, so without this file the
discovery scripts (find_sources / find_github / find_osti / find_books / find_archive) re-find,
re-append, re-fetch, and re-prune the same URLs every round (~200 wasted fetches/round). Every
finder dedups against blocklist.load(); prune_corpus --apply calls blocklist.add() for what it drops.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
PATH = ROOT / "pruned_urls.txt"


def _norm(u: str) -> str:
    return (u or "").strip().rstrip("/")


def load() -> set[str]:
    if not PATH.exists():
        return set()
    return {_norm(l) for l in PATH.read_text().splitlines() if l.strip()}


def add(urls) -> int:
    """Append new URLs (deduped, sorted) to the blocklist file. Returns how many were new."""
    cur = load()
    new = {_norm(u) for u in urls if u and _norm(u)} - cur
    if new:
        with PATH.open("a") as f:
            for u in sorted(new):
                f.write(u + "\n")
    return len(new)
