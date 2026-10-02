import sys
from types import SimpleNamespace

import pytest

import find_osti_sim
import lint_registry
import registry


def _rec(oid, title, kind="Technical Report", fulltext=True, **extra):
    links = [{"rel": "citation", "href": f"https://www.osti.gov/biblio/{oid}"}]
    if fulltext:
        links.append({"rel": "fulltext", "href": f"https://www.osti.gov/servlets/purl/{oid}"})
    return {"osti_id": str(oid), "title": title, "product_type": kind, "links": links,
            "language": "English", "publication_date": "2024-05-01T00:00:00Z", **extra}


REPORT = _rec(1001, "EnergyPlus Simulation of Commercial Prototype Buildings")
AM = _rec(1002, "Calibrating urban building energy models with measured data",
          "Journal Article", article_type="Accepted Manuscript", journal_name="Energy and Buildings",
          doi="10.1016/j.enbuild.2024.1")
CONF = _rec(1003, "Co-simulation of HVAC controls with Modelica", "Conference")
TOOL_ONLY = _rec(1004, "DOE-2 sample run book: Version 2.1E")
OFF_TOPIC = _rec(1005, "Kinetic precursor drift model for molten salt reactors")
SOFTWARE = _rec("code-1006", "EnergyPlus View Factor Calculation", "Software")
NO_FULLTEXT = _rec(1007, "Residential building energy modeling", fulltext=False)
SLIDES = _rec(1008, "Building energy modeling webinar", "Program Document")
VETOED_TOOL = _rec(1009, "CONTAM transport of protein aerosols in mice")


def test_candidate_gate_types_and_licence_classes():
    kept = {r["osti_id"]: find_osti_sim.candidate(r, "2026-10-02") for r in
            (REPORT, AM, CONF, TOOL_ONLY, OFF_TOPIC, SOFTWARE, NO_FULLTEXT, SLIDES, VETOED_TOOL)}
    assert {k for k, v in kept.items() if v} == {"1001", "1002", "1003", "1004"}
    report, am, conf, tool = kept["1001"], kept["1002"], kept["1003"], kept["1004"]
    assert report["license"] == "unverified" and report["document_type"] == "technical-report"
    assert "osti.gov/disclaim" in report["license_evidence"]
    assert am["license"] == "publisher-oa" and "Energy and Buildings" in am["license_evidence"]
    assert am["persistent_id"] == "doi:10.1016/j.enbuild.2024.1"
    assert conf["license"] == "unverified" and conf["document_type"] == "conference-paper"
    assert tool["url"] == "https://www.osti.gov/servlets/purl/1004"
    for entry in (report, am, conf, tool):
        assert entry["topic"] == "simulation_modeling" in lint_registry.TOPICS
        assert entry["license"] in lint_registry.LICENSES
        assert entry["source"] == "osti_sim" and entry["language"] == "en"
        assert entry["rights_verified_at"] == "2026-10-02"
        assert registry.shard_filename(entry["id"]) == "ostisim.yaml"
        assert not lint_registry.entry_errors(entry, "ostisim.yaml")


def test_titles_lose_markup_entities():
    rec = _rec(1010, "CO<sub>2</sub> &amp; ventilation in office buildings")
    assert find_osti_sim.candidate(rec, "x")["title"] == "CO2 & ventilation in office buildings"


def _setup(monkeypatch, tmp_path, pages, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(pages)

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, json=lambda item=item: item)

    monkeypatch.setattr(find_osti_sim.requests, "get", get)
    monkeypatch.setattr(find_osti_sim.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(find_osti_sim.dedup, "open_keys",
                        lambda: find_osti_sim.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_osti_sim.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


def test_full_page_advances_page_short_page_advances_query(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 2)
    known = ({"https://www.osti.gov/servlets/purl/1003"}, set(), set())
    pages = [[REPORT, CONF], [AM]]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages, known)
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "q=0 p=1", "--pages", "2",
                                      "--append"])

    find_osti_sim.main()

    made = [c for c in calls if c != "sleep"]
    assert [(c["q"], c["page"]) for c in made] == [(find_osti_sim.QUERIES[0], 1),
                                                   (find_osti_sim.QUERIES[0], 2)]
    assert all(c["has_fulltext"] == "true" for c in made)
    assert calls.count("sleep") == 1
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["1001", "1002"]  # 1003 known
    assert files["next"].read_text() == "q=1 p=1\n"


def test_deep_full_pages_keep_walking_the_same_query(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 1)
    _, _, files = _setup(monkeypatch, tmp_path, [[REPORT]])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "q=3 p=40", "--pages", "1"])
    find_osti_sim.main()
    assert files["next"].read_text() == "q=3 p=41\n"
    assert not files["exhausted"].exists()


@pytest.mark.parametrize("rights,tag", [
    ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
    ("https://creativecommons.org/licenses/by-sa/4.0/legalcode", "cc-by-sa"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("Creative Commons Attribution 4.0 International", "unverified"),  # prose: not verifiable
    ("Creative Commons Attribution - NonCommercial - NoDerivatives 4.0 International",
     "unverified"),
    ("Creative Commons Attribution License (http://creativecommons.org/licenses/by-nc-nd/4.0/)",
     "unverified"),
    ("CC BY\u2013NC\u2013ND 4.0", "unverified"),
    ("Lawrence Livermore National Security, LLC", "unverified"),
])
def test_explicit_rights_field_wins_over_product_type(rights, tag):
    rec = _rec(1011, "Building energy simulation report", "Journal Article", rights=rights,
               article_type="Accepted Manuscript")
    entry = find_osti_sim.candidate(rec, "x")
    assert entry["license"] == tag and rights[:20] in entry["license_evidence"]
    assert entry["license"] not in ("public-domain", "publisher-oa")
    assert ("license_url" in entry) == (tag != "unverified")


def test_fire_dynamics_simulator_title_is_relevant_but_vetoes_still_apply():
    assert find_osti_sim.relevant("Fire Dynamics Simulator Technical Reference Guide")
    assert not find_osti_sim.relevant("Fire Dynamics Simulator study of protein aerosols")


def test_doi_identity_and_title_dedup(monkeypatch, tmp_path):
    known = (set(), {registry.norm(REPORT["title"])}, set(), {"doi:10.1016/j.enbuild.2024.1"})
    _, appended, _ = _setup(monkeypatch, tmp_path, [[REPORT, AM, CONF]], known)
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--pages", "1", "--append"])
    find_osti_sim.main()
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["1003"]


def test_max_stops_after_the_page_that_reaches_it(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 2)
    calls, appended, files = _setup(monkeypatch, tmp_path, [[REPORT, AM], [CONF, TOOL_ONLY]])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--pages", "5", "--max", "1",
                                      "--append"])
    find_osti_sim.main()
    assert len([c for c in calls if c != "sleep"]) == 1
    assert len(appended) == 2
    assert files["next"].read_text() == "q=0 p=2\n"


def test_last_query_finished_reports_exhausted(monkeypatch, tmp_path):
    last = len(find_osti_sim.QUERIES) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [[]])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", f"q={last} p=3"])
    find_osti_sim.main()
    assert files["next"].read_text() == "END\n"
    assert "queries walked" in files["exhausted"].read_text()


def test_end_cursor_makes_no_request(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "END"])
    find_osti_sim.main()
    assert calls == [] and files["next"].read_text() == "END\n"


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, json=lambda: []),
    SimpleNamespace(status_code=200, json=lambda: {"error": "maintenance"}),
    SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html"))),
])
def test_unexpected_answer_holds_and_proposes_nothing(monkeypatch, tmp_path, answer):
    monkeypatch.setattr(find_osti_sim, "ROWS", 1)
    _, appended, files = _setup(monkeypatch, tmp_path, [[REPORT], answer])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--pages", "3", "--append"])
    find_osti_sim.main()
    assert appended == [] and files["hold"].read_text().strip()
    assert not files["next"].exists()


def test_network_failure_exits_nonzero_without_append(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 1)
    _, appended, files = _setup(monkeypatch, tmp_path, [[REPORT], TimeoutError("offline")])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--pages", "3", "--append"])
    with pytest.raises(SystemExit) as exc:
        find_osti_sim.main()
    assert exc.value.code == 1
    assert appended == [] and not files["next"].exists()


@pytest.mark.parametrize("cursor", ["", "q=1", "q=a p=1", "q=1 p=0", "p=1 q=1"])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        find_osti_sim.parse_cursor(cursor)
