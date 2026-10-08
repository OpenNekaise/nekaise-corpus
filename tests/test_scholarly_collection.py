"""Scholarly discovery collects by access and classifies use before materialization."""
import sys

import pytest

import build_corpus
import clean_corpus
import find_sources
import lint_registry
import oa_resolution as oar
import openalex_families as fam
import pipeline_repo
import registry
import store
from runids import rid
from test_openalex_sim import POLICY, fake_transport, loc, work
from test_openalex_review_fixes import loader  # isolated download fixture
from test_pipeline_store import Resp


@pytest.mark.parametrize("value,tag", [
    ("cc-by-nc", "cc-by-nc"), ("CC-BY-NC-SA-4.0", "cc-by-nc-sa"),
    ("CC-BY-ND-4.0", "cc-by-nd"),
    ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "cc-by-nc-nd"),
    (None, "unverified"), ("other-oa", "unverified"),
    ("https://example.org/licenses/by-nc/4.0/", "unverified"),
    ("not licensed under CC-BY-4.0", "unverified"),
])
def test_restricted_copy_has_a_use_class_and_copy_evidence(value, tag):
    w = work(locations=[loc("https://zenodo.org/records/1/files/a.pdf", value)])
    res = oar.select_copy(w, POLICY)
    assert res.status == "resolved" and res.rights.tag == tag
    for source in ("openalex_sim", "openalex_ai"):
        e = fam.build_entry(w, res, topic="simulation_modeling", source=source,
                           family="test", today="2026-10-07")
        assert not lint_registry.entry_errors(e, "test")
        assert registry.is_collection_eligible(e, {})
        assert not registry.is_default_corpus_eligible(e, {})
        assert e["url"] in e["license_evidence"]
        assert (value or "no licence statement") in e["license_evidence"]


def test_conflicting_rights_keep_all_evidence_without_truncation():
    url = "https://zenodo.org/records/1/files/a.pdf"
    statement = "Unrecognized terms " + "x" * 700
    w = work(locations=[loc(url, "cc-by"), loc(url, "cc-by-nc"), loc(url, statement)])
    res = oar.select_copy(w, POLICY)
    assert res.rights.status == "conflict" and res.rights.tag == "unverified"
    e = find_sources.openalex_entry(w, res, "building_energy", "2026-10-07")
    assert statement in e["license_evidence"] and "cc-by-nc" in e["license_evidence"]
    assert "rights conflict" in e["license_evidence"] and "license_url" not in e


@pytest.mark.parametrize("changed", ["url", "selected_version"])
def test_unverified_family_evidence_must_bind_the_selected_copy(changed):
    w = work(locations=[loc("https://zenodo.org/records/1/files/a.pdf", None)])
    e = fam.build_entry(w, oar.select_copy(w, POLICY), topic="simulation_modeling",
                       source="openalex_sim", family="test", today="2026-10-07")
    assert lint_registry.entry_errors(e, "test") == []
    e[changed] += "-different"
    assert any("requires an evidenced" in err for err in lint_registry.entry_errors(e, "test"))


@pytest.mark.parametrize("license", ["cc-by-nc", None])
@pytest.mark.parametrize("missing", ["license_evidence", "rights_verified_at", "persistent_id"])
def test_restricted_family_still_requires_provenance(license, missing):
    w = work(locations=[loc("https://zenodo.org/records/1/files/a.pdf", license)])
    e = fam.build_entry(w, oar.select_copy(w, POLICY), topic="simulation_modeling",
                       source="openalex_sim", family="test", today="2026-10-07")
    del e[missing]
    assert any(f"lacks {missing}" in err for err in lint_registry.entry_errors(e, "test"))


def test_closed_copy_cannot_be_reopened_by_another_providers_oa_metadata():
    url = "https://zenodo.org/records/1/files/a.pdf"
    w = work(locations=[loc(url, "cc-by-nc", is_oa=False)])
    res = oar.select_copy(w, POLICY, unpaywall={"oa_locations": [
        {"url_for_pdf": url, "license": "cc-by-nc", "version": "publishedVersion"}]})
    assert res.status == "unresolved" and res.reasons == ["access_closed:zenodo.org"]


@pytest.mark.parametrize("license", [None, "cc-by-nc", "cc-by"])
def test_closed_location_is_never_a_fallback(license):
    w = work(locations=[loc("https://zenodo.org/records/1/files/a.pdf", license, is_oa=False)])
    res = oar.select_copy(w, POLICY)
    assert res.status == "unresolved" and res.reasons == ["access_closed:zenodo.org"]


def test_work_oa_does_not_make_every_location_public():
    w = work(locations=[{"pdf_url": "https://zenodo.org/records/1/files/a.pdf"}])
    res = oar.select_copy(w, POLICY)
    assert res.status == "unresolved" and res.reasons == ["access_unverified:zenodo.org"]


@pytest.mark.parametrize("url", [
    "https://escholarship.org/content/qt1/qt1.pdf", "https://www.mdpi.com/x.pdf",
    "https://papers.ssrn.com/x.pdf", "https://www.jstage.jst.go.jp/x.pdf",
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf/a.pdf", "https://doi.org/10.1234/x",
])
def test_host_access_exclusion_precedes_rights(url, monkeypatch):
    monkeypatch.setattr(oar, "combine", lambda *_: pytest.fail("classified a blocked copy"))
    res = oar.select_copy(work(locations=[loc(url, "cc-by-nc")]), POLICY)
    assert res.status in ("unresolved", "excluded") and res.copy is None


@pytest.mark.parametrize("body", [
    b"<html><title>Sign in to purchase this article</title></html>",
    b"<html><title>Just a moment...</title><script>cf-chl</script></html>",
])
def test_restricted_metadata_does_not_turn_paywall_or_challenge_html_into_pdf(
        monkeypatch, loader, body):
    url = "https://zenodo.org/records/1/files/a.pdf"
    w = work(locations=[loc(url, "cc-by-nc")])
    e = find_sources.openalex_entry(w, oar.select_copy(w, POLICY), "building_energy", "2026-10-07")
    requested = fake_transport(monkeypatch, {url: (200, {"Content-Type": "text/html"}, body)})
    rec = build_corpus.download_one(e)
    assert requested == [url] and rec["status"] == "failed" and not rec.get("raw_path")


def test_discovered_classes_reach_only_their_training_views(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("scholarly-classes"))
    entries = []
    classes = {}
    for n, (license, other, cls) in enumerate([
        ("cc-by", None, "open"), ("CC0-1.0", None, "open"),
        ("cc-by-nc", None, "nc"),
        ("cc-by-nd", None, "nd"), ("cc-by-nc-nd", None, "nc-nd"),
        (None, None, "unverified"), ("cc-by", "cc-by-nc", "unverified"),
    ]):
        url = f"https://zenodo.org/records/{n + 1}/files/{n}.pdf"
        locations = [loc(url, license)] + ([loc(url, other)] if other else [])
        w = work(title=f"Building HVAC simulation {n}", doi=f"10.1234/class.{n}", locations=locations)
        e = fam.build_entry(w, oar.select_copy(w, POLICY), topic="simulation_modeling",
                           source="openalex_sim", family="simulation", today="2026-10-07")
        assert not lint_registry.entry_errors(e, "test")
        entries.append(e)
        classes[e["id"]] = cls
    root = pipeline_repo.write_repo(tmp_path / "repo", entries=entries, policy={})
    pipeline_repo.point(monkeypatch, root, policy={})
    # Network and PDF parsing are canned; the actual loader, store, cleaner and check run.
    monkeypatch.setattr(build_corpus.requests, "get", lambda url, **_: Resp(
        200, ("%PDF-1.7 " + url + "\n" + "Building HVAC energy control equations.\n" * 40).encode()))
    monkeypatch.setattr(build_corpus, "extract_pdf", lambda data: data.decode())
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1"])
    build_corpus.main()
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1", "--rules", "none"])
    clean_corpus.main()
    rows = pipeline_repo.manifest_rows(root)
    assert set(rows) == set(classes)
    for sid, row in rows.items():
        cls = classes[sid]
        prefix = "corpus" if cls == "open" else f"collection/{cls}/corpus"
        assert row["status"] == "ok" and row["corpus_path"] == f"{prefix}/{sid}.md"
        for key in ("raw_path", "text_path", "corpus_path"):
            assert (root / row[key]).is_file()
    open_ids = {sid for sid, cls in classes.items() if cls == "open"}
    assert {p.stem for p in (root / "corpus").glob("*.md")} == open_ids
    with store.open(root=root).read() as v:
        visible = v.scan(store.Table.MANIFEST, where=store.default_corpus_where({})).rows
        assert {r["id"] for r in visible} == open_ids
        assert sum(r["corpus_chars"] for r in visible) == sum(
            rows[sid]["corpus_chars"] for sid in open_ids)
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--check"])
    clean_corpus.main()
    assert "OK — every view matches the manifest exactly" in capsys.readouterr().out


@pytest.mark.parametrize("fallback_license", [None, "cc-by"])
def test_family_keeps_bounded_open_copy_search_before_collecting_restricted(fallback_license):
    from test_openalex_sim import FakeHttp, make_run
    original = "https://zenodo.org/records/1/files/nc.pdf"
    alternative = "https://zenodo.org/records/2/files/open.pdf"
    w = work(locations=[loc(original, "cc-by-nc")])
    http = FakeHttp(singles={"https://api.unpaywall.org/v2/10.1234/abc.1": {
        "doi": "10.1234/abc.1",
        "oa_locations": ([{"url_for_pdf": alternative, "license": fallback_license,
                           "version": "acceptedVersion"}] if fallback_license else [])}})
    run = make_run(http=http)
    run.consider(w, "simulation_modeling", "sim")
    (entry,) = run.out
    assert entry["url"] == (alternative if fallback_license else original)
    assert entry["license"] == (fallback_license or "cc-by-nc")
    assert len(http.calls) == 1  # existing supplementary budget; no extra search
