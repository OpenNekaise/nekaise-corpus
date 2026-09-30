#!/usr/bin/env python3
"""corpus_stats.py — the corpus's headline statistics from any store view (ADR 0001, stage 3).

One definition for everything that reports the corpus size: README stats, run_round's round
summary and commit message, and check_contracts. It uses store aggregates only, so it runs the
same against FileStore and PostgreSQL, and it is manifest-based: identical on every machine
whatever payloads the machine holds (local availability is reported elsewhere).

Semantics (unchanged from the pre-store code, pinned by tests/test_corpus_stats.py):
* documents = successful ("ok") DEFAULT-VIEW manifest rows (registry.is_default_corpus_eligible,
  the open use class); excluded = successful rows outside the default view (restricted-use
  classes and policy holds) — they are still COLLECTED, see compute_collection;
* text_chars = sum of their text_chars; corpus_chars = sum of corpus_chars, falling back to
  text_chars for rows the cleaner has not visited;
* tokens ≈ chars // 4;
* topics / licenses: counts, ordered by count descending, ties by name.

Collection statistics (compute_collection) are reported ALONGSIDE, never mixed into the default
numbers: held raw originals per use class (a verified raw claim counts even when extraction
failed), successful extractions, and cleaned payloads per view.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import registry
import store
from store import And, Eq, Exists, Not, Or, Prefix


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
    eligibility policy (validated, failing closed)."""
    if restrictions is None:
        restrictions, _ = store.pinned_policy(view)
    eligible_rule = store.default_corpus_where(restrictions)
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


@dataclass(frozen=True)
class CollectionStats:
    held: dict          # {use class: rows holding a verified raw original (sha256 + raw_path)}
    extracted: dict     # {use class: successful rows with extracted text}
    held_chars: dict    # {use class: sum of text_chars over the extracted rows}
    cleaned: dict       # {view: rows whose cleaned payload is a member of that view}

    @property
    def total_held(self) -> int:
        return sum(self.held.values())

    @property
    def extraction_failed(self) -> int:
        return self.total_held - sum(self.extracted.values())


def compute_collection(view, restrictions: dict | None = None) -> CollectionStats:
    """Everything collected, by use class — reported alongside the default statistics. A few
    aggregates grouped by licence (mapped to classes here, exactly as registry.use_class) plus
    the policy-held rows, so it costs a handful of scans on any store."""
    if restrictions is None:
        restrictions, _ = store.pinned_policy(view)
    held_rule = And(Exists("sha256"), Not(Eq("sha256", None)), Exists("raw_path"))
    extracted_rule = And(Eq("status", "ok"), Exists("text_path"))
    cleaned_rule = And(Eq("status", "ok"), Exists("corpus_path"), Not(Eq("corpus_path", "")),
                       Not(Eq("corpus_path", None)))
    policy_held = store.class_where("policy-held", restrictions)
    held, extracted, chars, cleaned = Counter(), Counter(), Counter(), Counter()

    def by_class(where, sums=()):
        out = []
        for g in view.aggregate_manifest(group_by=("license",), where=And(where, Not(policy_held)),
                                         sums=sums):
            lic = g["license"]
            out.append((registry.LICENSE_CLASSES.get(lic, "unverified")
                        if isinstance(lic, str) else "unverified", g))
        for g in view.aggregate_manifest(group_by=(), where=And(where, policy_held), sums=sums):
            out.append(("policy-held", g))
        return out

    for cls, g in by_class(held_rule):
        held[cls] += g["count"]
    for cls, g in by_class(extracted_rule, ("text_chars",)):
        extracted[cls] += g["count"]
        chars[cls] += int(g["sum_text_chars"])
    for cls, g in by_class(cleaned_rule):
        cleaned[registry.DEFAULT_VIEW if cls == "open" else cls] += g["count"]
    drop = lambda c: {k: v for k, v in sorted(c.items()) if v}  # noqa: E731
    return CollectionStats(drop(held), drop(extracted), drop(chars), drop(cleaned))


def misplaced_view_claims(view, restrictions: dict) -> tuple[int, str | None]:
    """Manifest rows whose cleaned-payload claim is not in their own view — above all a
    restricted-use or policy-held row claiming the default corpus/ view: (count, first id)."""
    # Exists tests JSON key presence, including the loader's corpus_path: null on a
    # failed/unprocessed record. Only a nonempty path claims a cleaned payload. Do not
    # filter by status: a failed row with a real misplaced path is still a violation.
    claimed = And(Exists("corpus_path"), Not(Eq("corpus_path", None)),
                  Not(Eq("corpus_path", "")))
    wrong = []
    for v in registry.VIEWS:
        root = registry.view_root(v) + "/"
        wrong.append(And(store.view_where(v, restrictions), claimed,
                         Not(Prefix("corpus_path", root))))
    where = Or(*wrong)
    count = sum(g["count"] for g in view.aggregate_manifest(group_by=(), where=where))
    first = view.scan(store.Table.MANIFEST, where=where, fields=("id",), limit=1).rows
    return count, (first[0]["id"] if first else None)


def restricted_with_corpus_data(view, restrictions: dict) -> tuple[int, str | None]:
    """DEPRECATED name: rows outside the default view that still claim the default corpus/
    view. Restricted-use rows legitimately claim a cleaned payload in their classified view."""
    where = And(Not(store.default_corpus_where(restrictions)), Prefix("corpus_path", "corpus/"))
    count = sum(g["count"] for g in view.aggregate_manifest(group_by=(), where=where))
    first = view.scan(store.Table.MANIFEST, where=where, fields=("id",), limit=1).rows
    return count, (first[0]["id"] if first else None)


def local_unavailable(view, root: Path, restrictions: dict, policy: dict) -> int:
    """ELIGIBLE successful rows on a fetch-suspended host with no payload on this machine (a
    local report, never an input to committed statistics). `restrictions` and `policy` come
    from the same view (store.pinned_policy(view))."""
    if not policy:
        return 0
    eligible = And(Eq("status", "ok"), store.default_corpus_where(restrictions))
    n, cursor = 0, None
    while True:
        page = view.scan(store.Table.MANIFEST, where=eligible,
                         fields=("status", "url", "text_path"), cursor=cursor,
                         limit=store.MAX_PAGE)
        n += len(registry.locally_unavailable_rows(page.rows, policy, root))
        if page.next_cursor is None:
            return n
        cursor = page.next_cursor


def iter_eligible(view, restrictions: dict, fields: tuple[str, ...] | None = None):
    """Successful default-view manifest rows in registry.load_manifest_rows' order (so
    first-seen ties and seeded samples match the pre-store tools), projected to `fields`."""
    where = And(Eq("status", "ok"), store.default_corpus_where(restrictions))
    cursor = None
    while True:
        page = view.scan(store.Table.MANIFEST, where=where, fields=fields, cursor=cursor,
                         limit=store.MAX_PAGE, order="legacy")
        yield from page.rows
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def iter_manifest(view, where=None, fields: tuple[str, ...] | None = None):
    """Every manifest row in registry.load_manifest_rows' order, page by page."""
    cursor = None
    while True:
        page = view.scan(store.Table.MANIFEST, where=where, fields=fields, cursor=cursor,
                         limit=store.MAX_PAGE, order="legacy")
        yield from page.rows
        if page.next_cursor is None:
            return
        cursor = page.next_cursor
