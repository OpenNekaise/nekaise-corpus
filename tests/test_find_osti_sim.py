import sys
from datetime import date
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


TODAY = date(2026, 10, 12)


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
    monkeypatch.setattr(find_osti_sim, "utc_today", lambda: TODAY)
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
    assert not any("entry_date_start" in c or "sort" in c for c in made)  # history: no window
    assert calls.count("sleep") == 1
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["1001", "1002"]  # 1003 known
    # a walk without a start date is stamped with the day it began
    assert files["next"].read_text() == "q=1 p=1 since=2026-10-12\n"


def test_deep_full_pages_keep_walking_the_same_query(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 1)
    _, _, files = _setup(monkeypatch, tmp_path, [[REPORT]])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "q=3 p=40 since=2026-10-02",
                                      "--pages", "1"])
    find_osti_sim.main()
    assert files["next"].read_text() == "q=3 p=41 since=2026-10-02\n"  # the start date is kept
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
    assert files["next"].read_text() == "q=0 p=2 since=2026-10-12\n"


def test_last_query_finished_starts_watching_from_the_walk_start(monkeypatch, tmp_path):
    last = len(find_osti_sim.QUERIES) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [[]])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor",
                                      f"q={last} p=3 since=2026-10-02"])
    find_osti_sim.main()
    # OSTI keeps publishing: never EXHAUSTED; records entered since the walk began are next
    assert files["next"].read_text() == "watch:2026-10-02\n"
    assert not files["exhausted"].exists()


def test_watch_waits_without_a_request_until_the_next_day(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "watch:2026-10-12"])
    find_osti_sim.main()
    assert calls == [] and files["next"].read_text() == "watch:2026-10-12\n"
    assert not files["exhausted"].exists() and not files["hold"].exists()


def test_due_watch_starts_a_pass_over_the_entry_date_window(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 2)
    calls, appended, files = _setup(monkeypatch, tmp_path, [[REPORT, AM], [CONF], []])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "watch:2026-10-02",
                                      "--pages", "3", "--append"])
    find_osti_sim.main()
    made = [c for c in calls if c != "sleep"]
    assert [(c["q"], c["page"]) for c in made] == [
        (find_osti_sim.QUERIES[0], 1), (find_osti_sim.QUERIES[0], 2), (find_osti_sim.QUERIES[1], 1)]
    for c in made:  # the only date form the API filters on is MM/DD/YYYY
        assert (c["entry_date_start"], c["entry_date_end"]) == ("10/02/2026", "10/12/2026")
        assert c["sort"] == "entry_date asc" and c["has_fulltext"] == "true"
    assert len(appended) == 3
    assert files["next"].read_text() == "watch:2026-10-02:2026-10-12 q=2 p=1\n"


def test_watch_pass_resumes_with_its_window_and_ends_at_its_until_date(monkeypatch, tmp_path):
    last = len(find_osti_sim.QUERIES) - 1
    calls, _, files = _setup(monkeypatch, tmp_path, [[REPORT]])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor",
                                      f"watch:2026-10-02:2026-10-10 q={last} p=1"])
    find_osti_sim.main()
    (made,) = [c for c in calls if c != "sleep"]
    assert (made["entry_date_start"], made["entry_date_end"]) == ("10/02/2026", "10/10/2026")
    # the next window starts on the day this one ended (not today): nothing entered between
    # the pass's end date and its last round is skipped
    assert files["next"].read_text() == "watch:2026-10-10\n"
    assert not files["exhausted"].exists()


def test_watch_pass_holds_on_an_unexpected_answer(monkeypatch, tmp_path):
    answer = SimpleNamespace(status_code=500, json=lambda: {})
    _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", "watch:2026-10-02",
                                      "--append"])
    find_osti_sim.main()
    assert appended == [] and "watch:2026-10-02:2026-10-12 q=0 p=1" in files["hold"].read_text()
    assert not files["next"].exists()


def test_requests_ask_for_json(monkeypatch, tmp_path):
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs["headers"])
        return SimpleNamespace(status_code=200, json=lambda: [])

    monkeypatch.setattr(find_osti_sim.requests, "get", get)
    find_osti_sim.fetch_page("x", 1)
    assert seen[0]["Accept"] == "application/json" and "nekaise-corpus" in seen[0]["User-Agent"]


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


@pytest.mark.parametrize("cursor", [
    "", "q=1", "q=a p=1", "q=1 p=0", "p=1 q=1", "q=1 p=1 since=2026-13-01", "q=1 p=1 since=x",
    "watch:", "watch:2026-02-30", "watch:2026-10-02:2026-10-01 q=0 p=1",
    "watch:2026-10-02:2026-10-03", "watch:2026-10-02:2026-10-03 q=0 p=0", "watch:2026-10-02 q=0",
])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        find_osti_sim.parse_position(cursor)


@pytest.mark.parametrize("cursor", [
    "q=4 p=7", "q=4 p=7 since=2026-10-02", "watch:2026-10-02",
    "watch:2026-10-02:2026-10-09 q=3 p=2",
])
def test_cursor_round_trips(cursor):
    assert find_osti_sim.parse_position(cursor).cursor() == cursor


@pytest.mark.parametrize("cursor", ["watch:2026-10-13", "q=0 p=1 since=2026-10-13",
                                    "watch:2026-10-02:2026-10-13 q=0 p=1"])
def test_future_cursor_dates_are_refused(monkeypatch, tmp_path, cursor):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_osti_sim.py", "--cursor", cursor])
    with pytest.raises(SystemExit):
        find_osti_sim.main()
    assert calls == [] and not files["next"].exists()
