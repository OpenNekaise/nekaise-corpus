"""Collect regardless of licence; classify before use (operator directive 2026-09-25).

A licence never stops collection: restricted-use rows are downloaded, extracted and kept with
their provenance, and classified into use views. Only explicit collection-deny rules (and
host/access policy) hold bytes back. The store predicates agree with the Python policy on both
backends."""
from __future__ import annotations

import sys

import pytest

import build_corpus
import pipeline_repo
import registry
import store
from pipeline_repo import manifest_rows, write_repo
from runids import rid
from test_pipeline_store import lentry, serve
from test_store_contract import LICENCES, POLICY, STORES, mrow, write


# --- store predicates == Python policy, on every backend ------------------------------------------

def seeded_rows():
    rows = []
    n = 0
    for sid in ("pat-cn", "pat-us", "jst-", "x-"):
        for source in ("google_patents", "jstage", "other"):
            for lic in LICENCES:
                for corpus_path in (None, "corpus/x.md", "collection/nc/corpus/x.md"):
                    n += 1
                    extra = {"source": source}
                    if lic is None:
                        extra["license"] = None
                    else:
                        extra["license"] = lic
                    if corpus_path is not None:
                        extra["corpus_path"] = corpus_path
                    row = mrow(f"{sid}{n:04d}", **extra)
                    if lic is None:
                        row.pop("license")
                    rows.append(row)
    rows.append(mrow("x-failed", status="failed", corpus_path="corpus/x-failed.md"))
    return rows


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_store_predicates_agree_with_the_python_policy(factory, tmp_path):
    st = factory(tmp_path / "repo")
    if hasattr(st, "drop"):
        import atexit
        atexit.register(st.drop)
    rows = seeded_rows()
    write(st, rid("seed-classes"), lambda tx: tx.upsert_manifest(rows))
    checks = {"collection": (store.collection_where(POLICY), registry.is_collection_eligible),
              "default": (store.default_corpus_where(POLICY),
                          registry.is_default_corpus_eligible)}
    for cls in registry.USE_CLASSES:
        checks[cls] = (store.class_where(cls, POLICY),
                       lambda r, p, c=cls: registry.use_class(r, p) == c)
    for view in registry.VIEWS:
        checks[f"view:{view}"] = (store.corpus_view_where(view, POLICY),
                                  lambda r, p, v=view: registry.is_corpus_view_member(r, v, p))
    with st.read() as v:
        every = {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=store.MAX_PAGE).rows}
        for name, (pred, fn) in checks.items():
            got = {r["id"] for r in v.scan(store.Table.MANIFEST, where=pred, fields=("id",),
                                           limit=store.MAX_PAGE).rows}
            want = {sid for sid, r in every.items() if fn(r, POLICY)}
            assert got == want, name
            agg = sum(g["count"] for g in v.aggregate_manifest(group_by=(), where=pred))
            assert agg == len(want), name
    # every row has exactly one class, and the classes partition the manifest
    assert sum(len({sid for sid, r in every.items() if registry.use_class(r, POLICY) == c})
               for c in registry.USE_CLASSES) == len(every)


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_collection_deny_alone_never_revokes_default_use(factory, tmp_path):
    """Codex review ca1 P2-4: {collection: deny, default_corpus: allow} stops new downloads
    only; an already-held CC-BY row stays in the default view on both backends."""
    import pipeline_repo as pr
    policy = {"stop": pr.restriction({"source": "stopsrc"}, collection="deny",
                                     default_corpus="allow"),
              "hold": pr.restriction({"source": "holdsrc"}, collection="allow",
                                     default_corpus="deny")}
    st = factory(tmp_path / "repo")
    if hasattr(st, "drop"):
        import atexit
        atexit.register(st.drop)
    rows = [mrow("x-stop", source="stopsrc", license="cc-by", corpus_path="corpus/x-stop.md"),
            mrow("x-hold", source="holdsrc", license="cc-by", corpus_path="corpus/x-hold.md")]
    write(st, rid("seed-effects"), lambda tx: tx.upsert_manifest(rows))
    assert registry.use_class(rows[0], policy) == "open"
    assert registry.is_default_corpus_eligible(rows[0], policy)
    assert not registry.is_collection_eligible(rows[0], policy)
    assert registry.use_class(rows[1], policy) == "policy-held"
    with st.read() as v:
        got = {r["id"] for r in v.scan(store.Table.MANIFEST,
                                       where=store.default_corpus_where(policy),
                                       fields=("id",), limit=store.MAX_PAGE).rows}
        held = {r["id"] for r in v.scan(store.Table.MANIFEST,
                                        where=store.class_where("policy-held", policy),
                                        fields=("id",), limit=store.MAX_PAGE).rows}
    assert got == {"x-stop"} and held == {"x-hold"}


# --- the loader collects every licence class ------------------------------------------------------

RESTRICTED = ("cc-by-nc-nd", "cc-by-nc-sa", "cc-by-nd", "arxiv-nonexclusive", "publisher-oa",
              "unverified", "proprietary", "made-up-tag")


def test_restricted_use_rows_are_downloaded_and_keep_provenance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-collect"))
    entries = [lentry(f"ost-c-{n}", license=lic) for n, lic in enumerate(RESTRICTED)]
    entries += [lentry("ost-c-open", license="cc-by"),
                lentry("ost-c-ptr", license="proprietary-internal"),
                lentry("ost-c-held", license="cc-by", source="heldsrc")]
    rules = {"pointers": pipeline_repo.pointer_rule(),
             "held": pipeline_repo.restriction({"source": "heldsrc"}, collection="allow",
                                               default_corpus="deny")}
    root = write_repo(tmp_path / "repo", entries=sorted(entries, key=lambda e: e["id"]),
                      policy={}, restrictions=rules)
    pipeline_repo.point(monkeypatch, root, policy={})
    serve(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1"])
    build_corpus.main()
    out = capsys.readouterr().out
    rows = manifest_rows(root)
    for n, lic in enumerate(RESTRICTED):
        r = rows[f"ost-c-{n}"]
        assert r["status"] == "ok" and r["license"] == lic
        assert (root / r["raw_path"]).is_file() and (root / r["text_path"]).is_file()
        assert r["sha256"] and r["text_sha256"]
    assert rows["ost-c-held"]["status"] == "ok"        # a default-view hold still collects
    assert rows["ost-c-open"]["status"] == "ok"
    assert "ost-c-ptr" not in rows                     # the explicit collection deny holds
    assert "collection-denied sources: 1 held by eligibility rule 'pointers'" in out
    restrictions = {**rules}
    classes = {sid: registry.use_class(r, restrictions) for sid, r in rows.items()}
    assert classes["ost-c-0"] == "nc-nd" and classes["ost-c-1"] == "nc"
    assert classes["ost-c-3"] == "arxiv-nonexclusive" and classes["ost-c-7"] == "unverified"
    assert classes["ost-c-held"] == "policy-held" and classes["ost-c-open"] == "open"


def test_eligibility_v2_schema():
    ok = {"version": 2, "restrictions": {
        "p": {**pipeline_repo.pointer_rule()},
        "legacy_style": {**pipeline_repo.restriction({"source": "s"}),
                         "effects": {"collection": "deny", "default_corpus": "deny"}}}}
    assert registry.validate_eligibility(ok) == []
    bad = {"version": 2, "restrictions": {"x": {**pipeline_repo.restriction({"license": "cc-by"}),
                                                "effects": {"collection": "allow",
                                                            "default_corpus": "allow"}}}}
    assert any("must deny" in e for e in registry.validate_eligibility(bad))
    v1_license = {"version": 1, "restrictions": {"x": pipeline_repo.restriction({"license": "x"})}}
    assert any("unknown selector" in e for e in registry.validate_eligibility(v1_license))
    # the committed policy validates and keeps J-STAGE's bulk-download collection hold
    import json
    committed = json.loads(store.config_path("eligibility.json").read_text())
    assert registry.validate_eligibility(committed) == []
    jst = {"id": "jst-1", "source": "jstage_aij", "license": "open"}
    assert not registry.is_collection_eligible(jst, committed["restrictions"])
    soep = {"id": "crawl-soep-x", "source": "soep", "license": "open"}
    assert registry.is_collection_eligible(soep, committed["restrictions"])
    assert registry.use_class(soep, committed["restrictions"]) == "policy-held"
    ptr = {"id": "ashrae-x", "source": "ashrae", "license": "proprietary-internal"}
    assert not registry.is_collection_eligible(ptr, committed["restrictions"])


def test_payload_directories_are_never_tracked(tmp_path):
    import check_contracts
    assert check_contracts.payload_tracking_errors() == []
    (tmp_path / ".gitignore").write_text("raw/\ntext/\ncorpus/\n")
    errors = check_contracts.payload_tracking_errors(tmp_path)
    assert any("collection/" in e for e in errors) and any("artifacts/" in e for e in errors)


# --- classified cleaning on the file-authoritative store --------------------------------------------

def _load_and_clean(tmp_path, monkeypatch):
    import clean_corpus
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-classify"))
    entries = [lentry("ost-k-open", license="cc-by"), lentry("ost-k-nc", license="cc-by-nc"),
               lentry("ost-k-arx", license="arxiv-nonexclusive"),
               lentry("ost-k-held", license="cc-by", source="heldsrc")]
    rules = {"held": pipeline_repo.restriction({"source": "heldsrc"}, collection="allow",
                                               default_corpus="deny")}
    root = write_repo(tmp_path / "repo", entries=sorted(entries, key=lambda e: e["id"]),
                      policy={}, restrictions=rules)
    pipeline_repo.point(monkeypatch, root, policy={})
    serve(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1"])
    build_corpus.main()
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1", "--rules", "none"])
    clean_corpus.main()
    return root, rules


def check_ok(monkeypatch, capsys):
    import clean_corpus
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--check"])
    clean_corpus.main()
    assert "OK — every view matches the manifest exactly" in capsys.readouterr().out


def test_every_class_is_cleaned_into_its_view_and_default_consumers_see_only_open(
        tmp_path, monkeypatch, capsys):
    root, _rules = _load_and_clean(tmp_path, monkeypatch)
    rows = manifest_rows(root)
    assert rows["ost-k-open"]["corpus_path"] == "corpus/ost-k-open.md"
    assert rows["ost-k-nc"]["corpus_path"] == "collection/nc/corpus/ost-k-nc.md"
    assert rows["ost-k-arx"]["corpus_path"] == \
        "collection/arxiv-nonexclusive/corpus/ost-k-arx.md"
    assert rows["ost-k-held"]["corpus_path"] == "collection/policy-held/corpus/ost-k-held.md"
    for r in rows.values():
        assert (root / r["corpus_path"]).is_file() and r["corpus_sha256"]
    # a default consumer (a training run over corpus/*) reads the open class only
    assert {p.name for p in (root / "corpus").rglob("*.md")} == {"ost-k-open.md"}
    check_ok(monkeypatch, capsys)


def test_reclassification_moves_the_same_bytes_and_keeps_provenance(tmp_path, monkeypatch,
                                                                    capsys):
    import clean_corpus
    root, _rules = _load_and_clean(tmp_path, monkeypatch)
    before = manifest_rows(root)
    old = root / "corpus" / "ost-k-open.md"
    inode, data = old.stat().st_ino, old.read_bytes()
    st = store.FileStore(root)
    with st.writer() as w:          # the licence audit's kind of change: re-tag with evidence
        with st.transaction(rid("retag"), expected_version=st.version(), writer=w) as tx:
            tx.update_manifest_fields({"ost-k-open": {"license": "cc-by-nc-nd",
                                                      "license_evidence": "test"}})
            e = tx.get_entries(["ost-k-open"])["ost-k-open"]
            tx.upsert_entries([{**e, "license": "cc-by-nc-nd", "license_evidence": "test"}])
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1"])
    clean_corpus.main()
    assert "1 moved between views" in capsys.readouterr().out
    after = manifest_rows(root)
    new = root / "collection" / "nc-nd" / "corpus" / "ost-k-open.md"
    assert not old.exists() and new.read_bytes() == data and new.stat().st_ino == inode
    moved = after["ost-k-open"]
    assert moved["corpus_path"] == "collection/nc-nd/corpus/ost-k-open.md"
    assert moved["corpus_sha256"] == before["ost-k-open"]["corpus_sha256"]
    for key in ("raw_path", "sha256", "text_path", "text_sha256", "status"):
        assert moved[key] == before["ost-k-open"][key]
    assert (root / moved["raw_path"]).is_file() and (root / moved["text_path"]).is_file()
    assert set(after) == set(before)                         # nothing erased
    check_ok(monkeypatch, capsys)
    # and back: reclassified into the default view with the same bytes again
    with st.writer() as w:
        with st.transaction(rid("retag-back"), expected_version=st.version(), writer=w) as tx:
            tx.update_manifest_fields({"ost-k-open": {"license": "cc-by"}})
            e = tx.get_entries(["ost-k-open"])["ost-k-open"]
            tx.upsert_entries([{**e, "license": "cc-by"}])
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1"])
    clean_corpus.main()
    assert old.read_bytes() == data and not new.exists()
    check_ok(monkeypatch, capsys)


def test_check_refuses_a_restricted_row_in_the_default_view(tmp_path, monkeypatch, capsys):
    import clean_corpus
    root, _rules = _load_and_clean(tmp_path, monkeypatch)
    nc = root / "collection" / "nc" / "corpus" / "ost-k-nc.md"
    (root / "corpus" / "ost-k-nc.md").write_bytes(nc.read_bytes())   # leaked into the default
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--check"])
    with pytest.raises(SystemExit):
        clean_corpus.main()
    assert "unprovenanced file in a view: corpus/ost-k-nc.md" in capsys.readouterr().out
