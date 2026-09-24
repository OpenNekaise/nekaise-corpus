#!/usr/bin/env python3
"""blocklist.py — the store's set of URLs the pruner has dropped (FileStore: pruned_urls.txt).

Why: prune removes a dropped doc from BOTH the manifest and the registry, so without it the
discovery scripts would re-find, re-append, re-fetch, and re-prune the same URLs every round
(~200 wasted fetches/round). Finders dedup against it through scripts/dedup.py (store known());
prune_corpus --apply adds what it drops in its own store transaction (blocklist_add), and add()
below records URLs for every other caller. Nothing here touches a file (ADR 0001 stage 3, step 7).
"""
from __future__ import annotations

from pathlib import Path

import store
from state_codec import normalize_url as normalize  # noqa: F401 — the shared URL normal form

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)


def load() -> set[str]:
    """The whole blocklist, read UNFENCED (store.FileStore.peek: no lock, may observe a round in
    flight) — for display and diagnostics; mutations decide inside their transaction."""
    return store.open(root=ROOT).peek("blocklist")


def add(urls) -> int:
    """Record new URLs (deduped, sorted) in the blocklist through the store: through the broker of
    the round or maintenance window this runs in, otherwise in one standalone store transaction
    (store_broker.run_batch). The file store appends them to pruned_urls.txt exactly as the
    legacy writer did. Returns how many were new; nothing is recorded (no transaction) when none
    are."""
    import store_broker  # imports store only; kept lazy like registry's adapters

    cands = sorted({normalize(u) for u in urls if u and normalize(u)})
    if not cands:
        return 0

    def body(view, batch):
        known = set()
        for i in range(0, len(cands), store.MAX_KNOWN):
            chunk = cands[i:i + store.MAX_KNOWN]
            page = view.scan(store.Table.BLOCKLIST, where=store.In("url", chunk),
                             limit=store.MAX_PAGE)
            known.update(r["url"] for r in page.rows)
        new = [u for u in cands if u not in known]
        if new:
            batch.blocklist_add(new)
        return len(new)

    added, _ = store_broker.run_batch(store.open(root=ROOT), "blocklist", body)
    return added
