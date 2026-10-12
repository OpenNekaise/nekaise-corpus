import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import find_cordis
import lint_registry
import registry

P = "http://data.europa.eu/s66/resource/projects/"
U1 = P + "00000000-0000-0000-0000-000000000001"
U2 = P + "00000000-0000-0000-0000-000000000002"
U3 = P + "00000000-0000-0000-0000-000000000003"
DL = "https://ec.europa.eu/research/participants/documents/downloadPublic?documentIds={}&appId=PPGMS"


def _project(uri, rcn, title, abstract="", acronym="ACRO", grant=None):
    return {"uri": uri, "rcn": str(rcn), "grant": grant or str(700000 + rcn), "title": title,
            "abstract": abstract, "acronym": acronym}


def _deliverable(project, n, title, doc, kind="Documents, reports", host="ec.europa.eu",
                 language="en"):
    return {"project": project, "id": f"{n}_DELIV", "title": title, "language": language,
            "url": f"https://{host}/research/participants/documents/downloadPublic"
                   f"?documentIds={doc}&appId=PPGMS", "type": kind}


def _bindings(rows):
    return {"results": {"bindings": [{k: {"value": v} for k, v in row.items()} for row in rows]}}


RETROFIT = _project(U1, 11, "Deep renovation of residential buildings with prefabricated façades",
                    acronym="RENO")
BY_ABSTRACT = _project(U2, 12, "Circular value chains for a greener Europe",
                       "We recycle construction and demolition waste into low-carbon concrete "
                       "and cement for the building stock.", acronym="CIRC")
OFF = _project(U3, 13, "Tunneling spectroscopy of quantum devices",
               "Concrete actions bridge the gap in system architecture; capacity building.")


def test_project_gate_needs_an_engineering_sense():
    assert find_cordis.project_relevant(RETROFIT)
    assert find_cordis.project_relevant(BY_ABSTRACT)
    assert not find_cordis.project_relevant(OFF)
    for title in ("New generation marine engines and retrofit solutions",
                  "Ferroelectric tunnel junctions in 2D heterostructures",
                  "Building blocks of social cognition in infancy",
                  "A scalable software architecture for exascale computing",
                  "Bridge the gap: capacity building for civil society"):
        assert not find_cordis.project_relevant(_project(U1, 1, title)), title
    for title in ("Seismic retrofit of masonry heritage buildings",
                  "Heat pumps for district heating networks",
                  "Self-sensing asphalt pavements for digital roads",
                  "Wastewater treatment for small municipalities",
                  "BIM-based tools for construction sites"):
        assert find_cordis.project_relevant(_project(U1, 1, title)), title
    # one figurative hit in the objective is not enough; a vetoed title never passes
    assert not find_cordis.project_relevant(
        _project(U1, 1, "Citizen science for all", "Volunteers map housing needs."))
    assert not find_cordis.project_relevant(
        _project(U1, 1, "Protein crystal structure of cement-binding enzymes in buildings"))


def test_deliverable_gate_drops_administration_and_non_reports():
    ok = _deliverable(U1, 1, "Guidelines to foster building with wood", "080166e5dda69231")
    assert find_cordis.deliverable_ok(ok)
    for title in ("Data Management Plan", "Dissemination and Communication plan",
                  "Project website", "Kick-off meeting minutes", "H - Requirement No. 1",
                  "First periodic report", "Exploitation strategy", "Project newsletter 3",
                  "Quality assurance plan", "Final business plan"):
        assert not find_cordis.deliverable_ok(_deliverable(U1, 1, title, "080166e5dda69231")), title
    assert not find_cordis.deliverable_ok(
        _deliverable(U1, 1, "Technical video of the pilot site", "080166e5dda69231",
                     kind="Websites, patent fillings, video"))
    assert not find_cordis.deliverable_ok(
        _deliverable(U1, 1, "Monitoring data of the demo buildings", "080166e5dda69231",
                     kind="Open Research Data Pilot"))
    bad_url = dict(ok, url="https://example.org/research/participants/documents/"
                           "downloadPublic?documentIds=080166e5dda69231&appId=PPGMS")
    assert not find_cordis.deliverable_ok(bad_url)


def test_candidate_fields_licence_and_shard():
    row = _deliverable(U1, "723916_7", "Report on prefabricated façade modules",
                       "080166e5b1711329", host="www.ec.europa.eu")
    project = dict(RETROFIT, rcn=11)
    entry = find_cordis.candidate(row, project, "2026-10-12")
    assert entry["url"] == DL.format("080166e5b1711329")  # canonical host, loader's URL form
    assert entry["title"] == "RENO (700011): Report on prefabricated façade modules"
    assert entry["license"] == "unverified" and entry["source"] == "cordis_deliverable"
    assert "Grant Agreement" in entry["license_evidence"]
    assert "cordis.europa.eu/about/legal" in entry["license_evidence"]
    assert entry["persistent_id"] == "cordis:723916_7_DELIV" and entry["language"] == "en"
    assert entry["topic"] in lint_registry.TOPICS
    assert entry["license"] in lint_registry.LICENSES
    assert registry.shard_filename(entry["id"]) == "deliverables.yaml"
    assert not lint_registry.entry_errors(entry, "deliverables.yaml")
    assert "downloadPublic" in entry["url"]  # build_corpus routes it to _fetch_ec_deliverable


def _setup(monkeypatch, tmp_path, answers, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(answers)

    def get(url, **kwargs):
        calls.append(kwargs["params"]["query"])
        assert url == find_cordis.ENDPOINT
        assert "nekaise-corpus" in kwargs["headers"]["User-Agent"]
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, json=lambda item=item: _bindings(item))

    monkeypatch.setattr(find_cordis.requests, "get", get)
    monkeypatch.setattr(find_cordis.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(find_cordis.dedup, "open_keys",
                        lambda: find_cordis.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_cordis.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


DELIVS = [
    _deliverable(U1, "700011_1", "Façade module design guide", "080166e5aaaaaaa1"),
    _deliverable(U1, "700011_2", "Data Management Plan", "080166e5aaaaaaa2"),
    _deliverable(U2, "700012_1", "Final report", "080166e5aaaaaaa3"),
    _deliverable(U2, "700012_2", "Low-carbon concrete mix designs", "080166e5aaaaaaa4"),
]


def test_walk_pages_projects_then_lists_kept_projects_only(monkeypatch, tmp_path):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 3)
    known = ({DL.format("080166e5aaaaaaa4")}, set(), set())
    calls, appended, files = _setup(monkeypatch, tmp_path,
                                    [[RETROFIT, BY_ABSTRACT, OFF], DELIVS], known)
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--pages", "1",
                                      "--append"])
    find_cordis.main()

    queries = [c for c in calls if c != "sleep"]
    assert len(queries) == 2 and calls.count("sleep") == 1
    assert "xsd:integer(?rcn) > 10" in queries[0] and "LIMIT 3" in queries[0]
    assert "regex" not in queries[0].lower() and "contains" not in queries[0].lower()
    assert "FILTER EXISTS { ?uri eurio:hasResult ?d . ?d a eurio:ProjectDeliverable }" in queries[0]
    assert f"<{U1}>" in queries[1] and f"<{U2}>" in queries[1] and U3 not in queries[1]
    # the DMP is administration, ...a4 is already known; "Final report" stays distinct by acronym
    assert [e["title"] for e in appended] == ["RENO (700011): Façade module design guide",
                                              "CIRC (700012): Final report"]
    assert len({e["id"] for e in appended}) == 2
    assert files["next"].read_text() == "p=13\n"
    assert not files["exhausted"].exists() and not files["hold"].exists()


def test_same_generic_title_in_two_projects_is_kept_twice(monkeypatch, tmp_path):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 2)
    rows = [_deliverable(U1, "700011_9", "Final report", "080166e5bbbbbbb1"),
            _deliverable(U2, "700012_9", "Final report", "080166e5bbbbbbb2")]
    _, appended, _ = _setup(monkeypatch, tmp_path, [[RETROFIT, BY_ABSTRACT], rows])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--pages", "1", "--append"])
    find_cordis.main()
    assert [e["title"] for e in appended] == ["RENO (700011): Final report",
                                              "CIRC (700012): Final report"]


def test_same_acronym_in_two_grants_keeps_both_deliverables(monkeypatch, tmp_path):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 2)
    twin = dict(BY_ABSTRACT, acronym="RENO")
    rows = [_deliverable(U1, "700011_9", "Final report", "080166e5bbbbbbb1"),
            _deliverable(U2, "700012_9", "Final report", "080166e5bbbbbbb2")]
    _, appended, _ = _setup(monkeypatch, tmp_path, [[RETROFIT, twin], rows])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--pages", "1", "--append"])
    find_cordis.main()
    assert [e["title"] for e in appended] == ["RENO (700011): Final report",
                                              "RENO (700012): Final report"]
    assert len({e["id"] for e in appended}) == 2


@pytest.mark.parametrize("missing", ["title", "url", "id"])
def test_a_malformed_deliverable_binding_holds_the_whole_page(monkeypatch, tmp_path, missing):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 1)
    good = _deliverable(U1, "700011_1", "Façade module design guide", "080166e5aaaaaaa1")
    bad = _deliverable(U1, "700011_2", "Roof module design guide", "080166e5aaaaaaa2")
    del bad[missing]
    _, appended, files = _setup(monkeypatch, tmp_path, [[RETROFIT], [good, bad]])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--append"])
    find_cordis.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


@pytest.mark.parametrize("missing", ["title", "grant"])
def test_a_malformed_project_binding_holds(monkeypatch, tmp_path, missing):
    bad = dict(BY_ABSTRACT)
    del bad[missing]
    _, appended, files = _setup(monkeypatch, tmp_path, [[RETROFIT, bad]])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--append"])
    find_cordis.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_only_an_empty_page_ends_the_walk_with_a_watch_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 5)
    _, appended, files = _setup(monkeypatch, tmp_path, [[OFF], []])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=12", "--append"])
    find_cordis.main()
    today = datetime.now(timezone.utc).date().isoformat()
    assert appended == [] and files["next"].read_text() == f"watch:{today}\n"
    assert not files["exhausted"].exists()


def test_a_short_page_does_not_end_the_walk(monkeypatch, tmp_path):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 5)
    _, _, files = _setup(monkeypatch, tmp_path, [[OFF]])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=12", "--pages", "1"])
    find_cordis.main()
    assert files["next"].read_text() == "p=13\n"


def test_repeated_bindings_of_one_project_do_not_end_the_walk(monkeypatch, tmp_path):
    # two acronyms repeat the project row: a FULL answer with one distinct project
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 2)
    twice = [dict(OFF, acronym="ONE"), dict(OFF, acronym="TWO")]
    calls, _, files = _setup(monkeypatch, tmp_path, [twice])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--pages", "1"])
    find_cordis.main()
    assert files["next"].read_text() == "p=13\n" and len(calls) == 1


def test_watch_cursor_waits_then_restarts_from_the_first_project(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).date()
    recent = f"watch:{today - timedelta(days=find_cordis.WATCH_DAYS - 1)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", recent])
    find_cordis.main()
    assert calls == [] and files["next"].read_text() == recent + "\n"

    due = f"watch:{today - timedelta(days=find_cordis.WATCH_DAYS)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [[OFF]])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", due, "--pages", "1"])
    find_cordis.main()
    assert "xsd:integer(?rcn) > 0" in calls[0]
    assert files["next"].read_text() == "p=13\n"


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html"))),
    SimpleNamespace(status_code=200, json=lambda: {"results": {}}),
    SimpleNamespace(status_code=200, json=lambda: _bindings([{"uri": U1, "rcn": "x",
                                                             "grant": "1", "title": "t"}])),
    SimpleNamespace(status_code=200, json=lambda: _bindings([dict(RETROFIT, rcn="5")])),
])
def test_unexpected_answers_hold_and_propose_nothing(monkeypatch, tmp_path, answer):
    _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--append"])
    find_cordis.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_unexpected_deliverable_answer_holds_the_whole_page(monkeypatch, tmp_path):
    monkeypatch.setattr(find_cordis, "PROJECT_PAGE", 1)
    foreign = [_deliverable(U3, "1_1", "Façade report", "080166e5ccccccc1")]
    _, appended, files = _setup(monkeypatch, tmp_path, [[RETROFIT], foreign])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--append"])
    find_cordis.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_network_failure_exits_nonzero_without_append(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [requests.ConnectionError("down")])
    monkeypatch.setattr(sys, "argv", ["find_cordis.py", "--cursor", "p=10", "--append"])
    with pytest.raises(SystemExit) as exc:
        find_cordis.main()
    assert exc.value.code == 1 and appended == []
    assert not files["next"].exists() and not files["hold"].exists()


def test_cursor_forms():
    assert find_cordis.parse_cursor("p=0") == (0, None)
    assert find_cordis.parse_cursor("watch:2026-10-12") == (None, date(2026, 10, 12))
    for bad in ("", "END", "p=-1", "watch:soon", "q=1 p=2"):
        with pytest.raises(ValueError):
            find_cordis.parse_cursor(bad)


def test_deliverables_query_only_accepts_project_uris():
    with pytest.raises(ValueError):
        find_cordis.deliverables(["http://example.org/> } DROP"])
    with pytest.raises(ValueError):
        find_cordis.deliverables([])


def test_loader_follows_the_download_modules_own_hop_only():
    token = ("https://www.ec.europa.eu/research/participants/documents/downloadPublic/"
             "bWxPL3FZ=/attachment/VFEyQTQ4=")
    meta = f'<div>\n <meta http-equiv="refresh" content="0;url={token}" />\n</div>'
    assert build_corpus.EC_NEXT_HOP.search(meta).group(1) == token
    old = ("<script>window.location='https://ec.europa.eu/research/participants/documents/"
           "downloadPublic/abc/attachment/def';</script>")
    assert build_corpus.EC_NEXT_HOP.search(old).group(1).endswith("/attachment/def")
    for foreign in ('<meta http-equiv="refresh" content="0;url=https://evil.example/x.pdf" />',
                    '<meta http-equiv="refresh" content="0;url=https://ec.europa.eu/other/x" />',
                    "window.location='https://ec.europa.eu.evil.example/research/participants/"
                    "documents/downloadPublic/x'"):
        assert not build_corpus.EC_NEXT_HOP.search(foreign)
    for host in ("ec.europa.eu", "www.ec.europa.eu"):
        assert host in build_corpus.POLITE_HOSTS
        assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA
        assert build_corpus.HOST_DELAY[host] >= 1.0
    assert build_corpus.HOST_RUN_CAP["ec.europa.eu"] <= 200


def test_ec_download_challenge_never_reaches_the_curl_fallback(monkeypatch):
    url = DL.format("080166e5aaaaaaaa")
    body = b"<html><title>Access denied</title>awswaf captcha</html>"
    assert build_corpus.is_challenge(403, body, "pdf")
    interstitial = (b"<html><head><title>Documents Download Module</title></head><body>the "
                    b"requested document is being prepared for download</body></html>")
    assert not build_corpus.is_challenge(200, interstitial, "pdf")
    called = []
    monkeypatch.setattr(build_corpus, "_curl_follow", lambda *a, **k: called.append(a) or b"%PDF-x")
    hops = build_corpus.Hops(url, False)
    hops.chain.append(url)
    monkeypatch.setattr(build_corpus, "_current_hops", lambda: hops)
    response = SimpleNamespace(status_code=403, content=body, headers={}, url=url, text="")
    with pytest.raises(Exception):
        build_corpus._fetch_with_fallback(url, "pdf", {}, response)
    assert called == []
