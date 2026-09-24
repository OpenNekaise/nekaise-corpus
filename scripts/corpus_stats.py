#!/usr/bin/env python3
"""corpus_stats.py — the corpus's headline statistics from any store view (ADR 0001, stage 3).

One definition for everything that reports the corpus size: README stats, run_round's round
summary and commit message, and check_contracts. It uses store aggregates only, so it runs the
same against FileStore and PostgreSQL, and it is manifest-based: identical on every machine
whatever payloads the machine holds (local availability is reported elsewhere).

Semantics (unchanged from the pre-store code, pinned by tests/test_corpus_stats.py):
* documents = successful ("ok") training-eligible manifest rows; excluded = successful rows a
  restriction or pointer-only license keeps out;
* text_chars = sum of their text_chars; corpus_chars = sum of corpus_chars, falling back to
  text_chars for rows the cleaner has not visited;
* tokens ≈ chars // 4;
* topics / licenses: counts, ordered by count descending, ties by name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import registry
import store
from store import And, Eq, Exists, Not, Or


@dataclass(frozen=True)
class CorpusStats:
    documents: int
    excluded: int
    text_chars: int
    corpus_chars: int
    topics: list = field(default_factory=list)     # [(topic, count)]
    licenses: dict = field(default_factory=dict)   # {license: count}

    @property
    def tokens(self) -> int:
        return self.text_chars // 4

    @property
    def corpus_tokens(self) -> int:
        return self.corpus_chars // 4


def _ordered(counts: dict) -> list:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def compute(view, restrictions: dict | None = None) -> CorpusStats:
    """Statistics of one consistent store view. `restrictions` defaults to the view's pinned
    eligibility policy."""
    if restrictions is None:
        restrictions = view.config_get().eligibility.get("restrictions", {})
    eligible_rule = store.eligibility_where(restrictions)
    ok = Eq("status", "ok")
    eligible = And(ok, eligible_rule)
    topics, text_chars, corpus_chars, documents = {}, 0, 0, 0
    for g in view.aggregate_manifest(group_by=("topic",), where=eligible,
                                     sums=("text_chars", "corpus_chars")):
        topics[g["topic"]] = g["count"]
        documents += g["count"]
        text_chars += g["sum_text_chars"]
        corpus_chars += g["sum_corpus_chars"]
    # rows the cleaner has not visited yet count with their extracted size
    for g in view.aggregate_manifest(group_by=(), where=And(eligible, Not(Exists("corpus_chars"))),
                                     sums=("text_chars",)):
        corpus_chars += g["sum_text_chars"]
    licenses = {g["license"]: g["count"]
                for g in view.aggregate_manifest(group_by=("license",), where=eligible)}
    excluded = sum(g["count"] for g in view.aggregate_manifest(
        group_by=(), where=And(ok, Not(eligible_rule))))
    return CorpusStats(documents, excluded, int(text_chars), int(corpus_chars),
                       _ordered(topics), licenses)


def restricted_with_corpus_data(view, restrictions: dict) -> tuple[int, str | None]:
    """Policy-restricted manifest rows that still claim corpus data: (count, first id)."""
    where = And(store.restriction_where(restrictions),
                Or(*(Exists(f) for f in registry.CORPUS_FIELDS)))
    count = sum(g["count"] for g in view.aggregate_manifest(group_by=(), where=where))
    first = view.scan(store.Table.MANIFEST, where=where, fields=("id",), limit=1).rows
    return count, (first[0]["id"] if first else None)


def local_unavailable(view, root: Path, restrictions: dict) -> int:
    """ELIGIBLE successful rows on a fetch-suspended host with no payload on this machine (a
    local report, never an input to committed statistics)."""
    policy = registry.load_host_policy()
    if not policy:
        return 0
    eligible = And(Eq("status", "ok"), store.eligibility_where(restrictions))
    n, cursor = 0, None
    while True:
        page = view.scan(store.Table.MANIFEST, where=eligible,
                         fields=("status", "url", "text_path"), cursor=cursor,
                         limit=store.MAX_PAGE)
        n += len(registry.locally_unavailable_rows(page.rows, policy, root))
        if page.next_cursor is None:
            return n
        cursor = page.next_cursor
