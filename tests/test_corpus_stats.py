"""corpus_stats reproduces the pre-store statistics exactly (both backends via the contract
fixture) — README numbers must not move when callers switch to the store."""
from collections import Counter

import pytest

import corpus_stats
import registry
from test_store_contract import STORES, mrow, seed, write


def legacy(rows, restrictions):
    ok, excluded = registry.partition_manifest_ok_rows(rows, restrictions)
    return (len(ok), len(excluded), sum(r.get("text_chars", 0) for r in ok),
            sum(r.get("corpus_chars", r.get("text_chars", 0)) for r in ok),
            Counter(r["topic"] for r in ok), Counter(r["license"] for r in ok))


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_matches_legacy_partition_and_sums(factory, tmp_path):
    st = factory(tmp_path / "repo")
    if hasattr(st, "drop"):
        import atexit
        atexit.register(st.drop)
    rows = [mrow("a-1", topic="hvac", text_chars=100, corpus_chars=90),
            mrow("a-2", topic="hvac", text_chars=50),                 # not cleaned yet
            mrow("pat-cn9", topic="structures", text_chars=70),        # restricted below
            mrow("b-1", topic="structures", text_chars=30, license="cc-by"),
            mrow("x-f", status="failed", text_chars=None)]
    write(st, "seed", seed)
    write(st, "rows", lambda tx: tx.upsert_manifest(rows))
    restrictions = {"cn": {"status": "restricted", "match": {"id_prefix": "pat-cn"}}}
    with st.read() as v:
        got = corpus_stats.compute(v, restrictions)
        every = v.scan("manifest", limit=1000).rows
    docs, excluded, text, corpus, topics, licenses = legacy(every, restrictions)
    assert (got.documents, got.excluded, got.text_chars, got.corpus_chars) == \
        (docs, excluded, text, corpus)
    assert dict(got.topics) == dict(topics) and got.licenses == dict(licenses)
    assert got.topics == sorted(topics.items(), key=lambda kv: (-kv[1], kv[0]))
    assert got.tokens == text // 4 and got.corpus_tokens == corpus // 4


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_restricted_rows_still_claiming_corpus_data(factory, tmp_path):
    st = factory(tmp_path / "repo")
    if hasattr(st, "drop"):
        import atexit
        atexit.register(st.drop)
    write(st, "rows", lambda tx: tx.upsert_manifest([
        mrow("pat-cn1", corpus_path="corpus/pat-cn1.md"),     # restricted + corpus data
        mrow("pat-cn2"),                                      # restricted, clean
        mrow("crawl-s", source="soep", corpus_chars=5),       # restricted by source
        mrow("ok-1", corpus_path="corpus/ok-1.md")]))         # eligible
    restrictions = {"cn": {"match": {"id_prefix": "pat-cn"}},
                    "soep": {"match": {"source": "soep"}}}
    with st.read() as v:
        count, first = corpus_stats.restricted_with_corpus_data(v, restrictions)
        misplaced, first_misplaced = corpus_stats.misplaced_view_claims(v, restrictions)
        every = v.scan("manifest", limit=100).rows
    # only a claim on the DEFAULT view is wrong for a held row (crawl-s claims no path at all)
    wrong = [r for r in every if not registry.is_default_corpus_eligible(r, restrictions)
             and str(r.get("corpus_path", "")).startswith("corpus/")]
    assert count == len(wrong) == 1 and first == "pat-cn1"
    assert (misplaced, first_misplaced) == (1, "pat-cn1")


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_empty_cleaned_paths_are_not_view_claims(factory, tmp_path, request):
    """The loader's failed record has an explicit null, unlike an absent JSON field.

    A transient download failure must remain reportable without aborting the round's
    contracts gate; successful rows awaiting cleaning also make no path claim yet.
    """
    import build_corpus

    st = factory(tmp_path / "repo")
    if hasattr(st, "drop"):
        request.addfinalizer(st.drop)
    rows = []
    restrictions = {"held": {"match": {"source": "held"}}}
    for license, source in [("open", "test"), ("cc-by-nc", "test"), ("open", "held")]:
        for status in ("ok", "failed"):
            for state in ("missing", "null", "empty"):
                row, _ = build_corpus._new_record(mrow(f"doc-{len(rows)}", license=license,
                                                       source=source))
                row["status"] = status
                if state == "missing":
                    row.pop("corpus_path")
                elif state == "empty":
                    row["corpus_path"] = ""
                rows.append(row)
    write(st, "rows", lambda tx: tx.upsert_manifest(rows))
    with st.read() as v:
        assert corpus_stats.misplaced_view_claims(v, restrictions) == (0, None)


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_nonempty_wrong_view_claims_fail_even_on_failed_rows(factory, tmp_path, request):
    st = factory(tmp_path / "repo")
    if hasattr(st, "drop"):
        request.addfinalizer(st.drop)
    restrictions = {"held": {"match": {"source": "held"}}}
    rows, wrong = [], set()
    for license, source in [("open", "test"), ("cc-by-nc", "test"), ("open", "held")]:
        for status in ("ok", "failed"):
            row = mrow(f"doc-{len(rows)}", license=license, source=source, status=status)
            row["corpus_path"] = registry.corpus_path_for(row, restrictions)
            rows.append(row)
            bad = dict(row, id=f"doc-{len(rows)}")
            bad["corpus_path"] = ("collection/nc/corpus/wrong.md" if license == "open"
                                  and source == "test" else "corpus/wrong.md")
            rows.append(bad)
            wrong.add(bad["id"])
    write(st, "rows", lambda tx: tx.upsert_manifest(rows))
    with st.read() as v:
        count, first = corpus_stats.misplaced_view_claims(v, restrictions)
    assert count == len(wrong) == 6
    assert first in wrong


def test_local_unavailable_counts_only_eligible_rows(tmp_path, monkeypatch):
    import store
    from test_store_contract import file_store
    st = file_store(tmp_path / "repo")
    esc = "https://escholarship.org/content/qt1/qt1.pdf"
    write(st, "rows", lambda tx: tx.upsert_manifest([
        mrow("pat-cn1", url=esc, text_path="text/pat-cn1.md"),     # restricted, unavailable
        mrow("ope-1", url=esc.replace("qt1", "qt2"), text_path="text/ope-1.md")]))  # eligible
    policy = {"escholarship.org": {"status": "suspended"}}
    import host_policy
    monkeypatch.setattr(host_policy, "suspended", lambda url, policy: "escholarship.org" in url)
    restrictions = {"cn": {"match": {"id_prefix": "pat-cn"}}}
    with st.read() as v:
        assert corpus_stats.local_unavailable(v, st.root, restrictions, policy) == 1
