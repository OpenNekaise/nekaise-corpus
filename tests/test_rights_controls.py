"""Rights-decision plumbing added 2026-09-24: selective re-extraction, crawl_docs refusal and
exact-licence provenance, manual-tool restrictions in the contracts, committed decisions."""

import sys

import pytest

import build_corpus
import check_contracts
import crawl_docs
import registry


def _row(sid, source, fmt="html", raw=None):
    return {"id": sid, "title": sid, "url": f"https://e/{sid}", "source": source,
            "license": "open", "topic": "building_energy", "format": fmt, "status": "ok",
            "raw_path": raw, "text_chars": 0}


def test_reextract_selector_parses_filters(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("# sphinx pages\ncrawl-a\n\ncrawl-b  # trailing comment\n")
    assert build_corpus.reextract_selector("x, y", "html", str(ids)) == {
        "source": {"x", "y"}, "format": {"html"}, "id": {"crawl-a", "crawl-b"}}
    assert build_corpus.reextract_selector() == {}
    (tmp_path / "empty.txt").write_text("# nothing\n")
    with pytest.raises(SystemExit):
        build_corpus.reextract_selector(ids_from=str(tmp_path / "empty.txt"))


def test_reextract_touches_only_selected_eligible_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "TEXT", tmp_path / "text")
    page = b'<div role="main"><p>See <a class="reference internal">Running</a> now.</p></div>'
    for name in ("a", "b", "c", "d"):
        (tmp_path / f"{name}.html").write_bytes(page)
    manifest = {
        "a": _row("a", "resstock_docs", raw="a.html"),
        "b": _row("b", "boptest_docs", raw="b.html"),
        "c": _row("c", "resstock_docs", fmt="md", raw="c.html"),
        "d": _row("d", "soep", raw="d.html"),                     # restricted
    }
    restrictions = {"soep": {"match": {"source": "soep"}}}

    done, _ = build_corpus.reextract(
        manifest, restrictions, {"source": {"resstock_docs", "soep"}, "format": {"html"}})

    assert done == 1
    assert (tmp_path / "text" / "a.md").read_text().endswith("See\nRunning\nnow.")
    assert not (tmp_path / "text" / "b.md").exists()   # other source
    assert not (tmp_path / "text" / "c.md").exists()   # other format
    assert not (tmp_path / "text" / "d.md").exists()   # policy-restricted
    assert manifest["a"]["extractor_version"] == build_corpus.EXTRACTOR_VERSION
    assert "extractor_version" not in manifest["b"]


def test_selection_flags_require_reextract(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--source", "x"])
    with pytest.raises(SystemExit):
        build_corpus.main()


def test_crawl_docs_refuses_restricted_source_before_any_request(monkeypatch):
    monkeypatch.setattr(crawl_docs.registry, "load_eligibility", lambda: {
        "soep": {"match": {"source": "soep"}, "decided_at": "2026-09-24", "reason": "ARR"}})
    monkeypatch.setattr(crawl_docs, "crawl", lambda *_a: pytest.fail("must not crawl"))
    monkeypatch.setattr(sys, "argv", ["crawl_docs.py", "--seed", "https://x/", "--source",
                                      "soep", "--topic", "building_energy"])
    with pytest.raises(SystemExit, match="restricted by eligibility rule 'soep'"):
        crawl_docs.main()


def test_crawl_docs_records_exact_license_evidence(monkeypatch, capsys):
    monkeypatch.setattr(crawl_docs.registry, "load_eligibility", lambda: {})
    monkeypatch.setattr(crawl_docs.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(crawl_docs, "crawl", lambda *_a: ["https://m.io/en/latest/api.html"])
    appended = []
    monkeypatch.setattr(crawl_docs.registry, "append_entries", appended.extend)
    monkeypatch.setattr(sys, "argv", [
        "crawl_docs.py", "--seed", "https://m.io/en/latest/", "--prefix", "/en/latest/",
        "--source", "mosaik_docs", "--topic", "controls_bas", "--license", "open",
        "--license-url", "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html",
        "--license-evidence", "SPDX LGPL-2.1", "--append"])

    crawl_docs.main()

    (row,) = appended
    assert row["license"] == "open"
    assert row["license_url"].endswith("lgpl-2.1.html")
    assert row["license_evidence"] == "SPDX LGPL-2.1"
    assert row["rights_verified_at"]


def test_manual_tool_restrictions_need_no_backend_entry():
    restrictions = {"soep": {"match": {"source": "soep"}, "backends": ["crawl_docs"]}}
    assert check_contracts.eligibility_contract_errors([], {}, restrictions) == []
    rows = [{"id": "crawl-soep-x", "source": "soep", "corpus_path": "corpus/crawl-soep-x.md"}]
    errors = check_contracts.eligibility_contract_errors(rows, {}, restrictions)
    assert any("still claim corpus data" in e for e in errors)


def test_committed_rights_decisions_cover_only_ungranted_sources():
    restrictions = registry.load_eligibility()
    for source in ("soep", "openstudio-docs"):
        assert registry.restriction_for({"id": "crawl-x", "source": source}, restrictions)
    # OpenStudio material with a real BSD-3 grant stays eligible
    for source in ("gh_openstudio", "gh_openstudio-standards", "gh_openstudio-hpxml"):
        assert registry.restriction_for({"id": "gh-x", "source": source}, restrictions) is None
