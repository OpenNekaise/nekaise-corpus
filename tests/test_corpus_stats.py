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
        every = v.scan("manifest", limit=100).rows
    legacy = [r for r in every if registry.restriction_for(r, restrictions) is not None
              and any(f in r for f in registry.CORPUS_FIELDS)]
    assert count == len(legacy) == 2 and first in {r["id"] for r in legacy}


def test_local_unavailable_counts_only_eligible_rows(tmp_path, monkeypatch):
    import store
    from test_store_contract import file_store
    st = file_store(tmp_path / "repo")
    esc = "https://escholarship.org/content/qt1/qt1.pdf"
    write(st, "rows", lambda tx: tx.upsert_manifest([
        mrow("pat-cn1", url=esc, text_path="text/pat-cn1.md"),     # restricted, unavailable
        mrow("ope-1", url=esc.replace("qt1", "qt2"), text_path="text/ope-1.md")]))  # eligible
    monkeypatch.setattr(registry, "load_host_policy",
                        lambda: {"escholarship.org": {"status": "suspended"}})
    import host_policy
    monkeypatch.setattr(host_policy, "suspended", lambda url, policy: "escholarship.org" in url)
    restrictions = {"cn": {"match": {"id_prefix": "pat-cn"}}}
    with st.read() as v:
        assert corpus_stats.local_unavailable(v, st.root, restrictions) == 1
