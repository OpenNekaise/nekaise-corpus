import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import find_worldbank
import lint_registry
import registry

TODAY = datetime.now(timezone.utc).date()


def _doc(n, title, docty="Working Paper", majdocty="Publications & Research", lang="English",
         docdt="2024-05-01T04:00:00Z", pdf=None):
    return {"display_title": title, "docty": docty, "majdocty": majdocty, "lang": lang,
            "docdt": docdt,
            "pdfurl": pdf or f"http://documents.worldbank.org/curated/en/0990{n}/pdf/doc{n}.pdf"}


def _answer(total, docs):
    documents = {f"D{i}": d for i, d in enumerate(docs)}
    documents["facets"] = {}
    return {"total": total, "documents": documents}


WATER = _doc(1, "Urban Water Infrastructure Assessment")
ROADS_FR = _doc(2, "Développement du BTP et le secteur routier", "Brief", lang="French")
ECON = _doc(3, "Kenya - The economy")
PROJECT = _doc(4, "Urban Water Infrastructure Assessment", "Environmental Assessment",
               "Project Documents")
SPEECH = _doc(5, "Remarks on housing and urban development", "President's Speech")


def _setup(monkeypatch, tmp_path, answers, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(answers)

    def get(url, **kwargs):
        assert url == find_worldbank.API
        assert "nekaise-corpus" in kwargs["headers"]["User-Agent"]
        calls.append(kwargs["params"])
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, json=lambda item=item: item)

    monkeypatch.setattr(find_worldbank.requests, "get", get)
    monkeypatch.setattr(find_worldbank.time, "sleep", lambda _s: None)
    monkeypatch.setattr(find_worldbank.dedup, "open_keys",
                        lambda: find_worldbank.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_worldbank.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    for path in files.values():
        path.unlink(missing_ok=True)
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


def test_walk_sends_structured_filters_and_never_a_text_query(monkeypatch, tmp_path):
    monkeypatch.setattr(find_worldbank, "ROWS", 5)
    monkeypatch.setattr(find_worldbank, "OVERLAP", 1)
    calls, appended, files = _setup(
        monkeypatch, tmp_path, [_answer(12, [WATER, ROADS_FR, ECON, PROJECT, SPEECH])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor", "s=1 o=5", "--pages", "1",
                                      "--append"])
    find_worldbank.main()

    params = calls[0]
    assert "qterm" not in params  # no full-text phrase: nothing fuzzy to rank at depth
    assert params["majdocty_exact"] == "Publications & Research"
    assert params["teratopic_exact"] == "Transport"
    assert (params["srt"], params["order"], params["os"], params["rows"]) == ("docdt", "desc", 5, 5)
    assert "majdocty" in params["fl"] and "lang" in params["fl"]
    assert [e["title"] for e in appended] == ["Urban Water Infrastructure Assessment",
                                              "Développement du BTP et le secteur routier"]
    water, roads = appended
    assert water["url"] == "https://documents.worldbank.org/curated/en/09901/pdf/doc1.pdf"
    assert roads["language"] == "fr" and water["published_at"] == "2024-05-01"
    for entry in appended:
        assert entry["source"] == "worldbank_wds"
        assert entry["topic"] in lint_registry.TOPICS
        assert registry.shard_filename(entry["id"]) == "worldbank.yaml"
        assert not lint_registry.entry_errors(entry, "worldbank.yaml")
    # pages overlap by OVERLAP records; the walk's start day is persisted
    assert files["next"].read_text() == f"s=1 o=9 w={TODAY}\n"
    assert not files["exhausted"].exists() and not files["hold"].exists()


def test_wds_results_are_never_labeled_open_or_cc_by(monkeypatch, tmp_path):
    _, appended, _ = _setup(monkeypatch, tmp_path, [_answer(1, [WATER])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--pages", "1", "--append"])
    find_worldbank.main()
    assert len(appended) == 1
    assert appended[0]["license"] == "unverified"
    assert appended[0]["license"] in lint_registry.LICENSES
    assert "non-commercial" in appended[0]["license_evidence"]
    assert "terms-and-conditions" in appended[0]["license_evidence"]
    assert "license_url" not in appended[0]


def test_known_documents_and_foreign_or_missing_pdf_urls_are_skipped(monkeypatch, tmp_path):
    foreign = _doc(6, "Road construction standards", pdf="https://example.org/x.pdf")
    missing = dict(_doc(7, "Bridge maintenance handbook"), pdfurl="")
    known = ({"https://documents.worldbank.org/curated/en/09901/pdf/doc1.pdf"},
             {registry.norm(ROADS_FR["display_title"])}, set())
    _, appended, _ = _setup(monkeypatch, tmp_path,
                            [_answer(4, [WATER, ROADS_FR, foreign, missing])], known)
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--pages", "1", "--append"])
    find_worldbank.main()
    assert appended == []


def test_end_of_slice_moves_on_and_coverage_starts_at_the_walks_start_day(monkeypatch, tmp_path):
    monkeypatch.setattr(find_worldbank, "ROWS", 2)
    _, _, files = _setup(monkeypatch, tmp_path, [_answer(4, [ECON, ECON])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor", "s=0 o=2 w=2026-10-01",
                                      "--pages", "1"])
    find_worldbank.main()
    assert files["next"].read_text() == "s=1 o=0 w=2026-10-01\n"

    last = len(find_worldbank.SLICES) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [_answer(1, [ECON])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor",
                                      f"s={last} o=0 w=2026-10-01"])
    find_worldbank.main()
    # the walk's START day, not the day it ended
    assert files["next"].read_text() == "watch:2026-10-01 w=2026-10-01\n"
    assert not files["exhausted"].exists()


def _day(days_ago):
    return TODAY - timedelta(days=days_ago)


def test_watch_idles_until_the_next_look_is_due(monkeypatch, tmp_path):
    cursor = f"watch:{_day(find_worldbank.WATCH_DAYS - 1)} w={_day(20)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor", cursor])
    find_worldbank.main()
    assert calls == [] and files["next"].read_text() == cursor + "\n"


def test_catch_up_continues_over_runs_until_the_previous_boundary(monkeypatch, tmp_path):
    """Codex round 1: 201 unseen records must all be reached, whatever the page budget."""
    monkeypatch.setattr(find_worldbank, "SLICES", ({"docty_exact": "A"}, {"docty_exact": "B"}))
    monkeypatch.setattr(find_worldbank, "ROWS", 2)
    monkeypatch.setattr(find_worldbank, "OVERLAP", 0)
    boundary, walk = _day(find_worldbank.WATCH_DAYS), _day(30)
    new = f"{_day(1)}T04:00:00Z"
    old = f"{boundary - timedelta(days=find_worldbank.BOUNDARY_MARGIN_DAYS + 1)}T04:00:00Z"
    unseen = [_doc(10 + i, f"Road construction manual part {i}", docdt=new) for i in range(5)]
    seen = _doc(30, "Bridge inspection handbook", docdt=old)

    # run 1: budget of 2 pages, the slice still has unseen records -> persisted continuation
    calls, appended, files = _setup(monkeypatch, tmp_path, [
        _answer(900, unseen[0:2]), _answer(900, unseen[2:4])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor",
                                      f"watch:{boundary} w={walk}", "--pages", "2", "--append"])
    find_worldbank.main()
    assert [(c["docty_exact"], c["os"]) for c in calls] == [("A", 0), ("A", 2)]
    assert len(appended) == 4
    middle = f"catch:{boundary} n={TODAY} s=0 o=4 w={walk}"
    assert files["next"].read_text() == middle + "\n"

    # run 2: the page that reaches a date before the boundary margin ends slice A; slice B ends
    calls, appended, files = _setup(monkeypatch, tmp_path, [
        _answer(900, [unseen[4], seen]), _answer(1, [WATER])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor", middle, "--pages", "2",
                                      "--append"])
    find_worldbank.main()
    assert [(c["docty_exact"], c["os"]) for c in calls] == [("A", 4), ("B", 0)]
    assert unseen[4]["display_title"] in [e["title"] for e in appended]
    # the next boundary is the day the look STARTED
    assert files["next"].read_text() == f"watch:{TODAY} w={walk}\n"


def test_periodic_full_rewalk_reconciles_backdated_additions(monkeypatch, tmp_path):
    monkeypatch.setattr(find_worldbank, "ROWS", 1)
    monkeypatch.setattr(find_worldbank, "OVERLAP", 0)
    cursor = f"watch:{_day(1)} w={_day(find_worldbank.REWALK_DAYS)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [_answer(700, [ECON])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor", cursor, "--pages", "1"])
    find_worldbank.main()
    assert calls[0]["os"] == 0 and calls[0] | find_worldbank.SLICES[0] == calls[0]
    assert files["next"].read_text() == f"s=0 o=1 w={TODAY}\n"
    assert "full re-walk" in find_worldbank.__doc__


def _ok(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload)


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html"))),
    _ok({"documents": {}}),                                    # no total
    _ok({"total": "lots", "documents": {}}),
    _ok({"total": 3, "documents": []}),
    _ok({"error": "temporarily unavailable"}),
    _ok({"total": 300, "documents": {"D0": "broken", "facets": {}}}),     # malformed value
    _ok({"total": 300, "documents": {"D0": {"display_title": ["x"]}}}),
    _ok({"total": 300, "documents": {"D0": dict(WATER, pdfurl=7)}}),
    _ok({"total": 300, "documents": {"facets": {}}}),          # empty page, records remain
    _ok({"total": 900, "documents": {"D0": WATER, "facets": {}}}),  # short page, records remain
    _ok({"total": 0, "documents": {"facets": {}}}),            # a slice without any record
])
def test_unexpected_answers_hold_and_propose_nothing(monkeypatch, tmp_path, answer):
    _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--append"])
    find_worldbank.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_short_page_after_a_good_page_holds_the_whole_run(monkeypatch, tmp_path):
    """Codex round 2: with total 900 a page of one document is not accepted, and neither is
    the good page read before it in the same run."""
    monkeypatch.setattr(find_worldbank, "ROWS", 2)
    monkeypatch.setattr(find_worldbank, "OVERLAP", 0)
    _, appended, files = _setup(monkeypatch, tmp_path, [
        _answer(900, [WATER, ROADS_FR]), _answer(900, [_doc(8, "Bridge maintenance handbook")])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--pages", "2", "--append"])
    find_worldbank.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_short_page_cannot_end_a_catch_up_slice_early(monkeypatch, tmp_path):
    monkeypatch.setattr(find_worldbank, "ROWS", 2)
    boundary = _day(find_worldbank.WATCH_DAYS)
    old = f"{boundary - timedelta(days=find_worldbank.BOUNDARY_MARGIN_DAYS + 9)}T04:00:00Z"
    _, appended, files = _setup(monkeypatch, tmp_path, [
        _answer(900, [_doc(9, "Road construction manual", docdt=old)])])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor",
                                      f"watch:{boundary} w={_day(30)}", "--append"])
    find_worldbank.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(find_worldbank, "ROWS", 1)
    _, appended, files = _setup(monkeypatch, tmp_path,
                                [_answer(9, [WATER]), requests.Timeout("offline")])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--append", "--pages", "2"])
    with pytest.raises(SystemExit) as exc:
        find_worldbank.main()
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_cursor_forms_and_bounds():
    today, d = date(2026, 10, 12), date
    assert find_worldbank.parse_cursor("s=2 o=300", today) == {
        "mode": "walk", "s": 2, "o": 300, "walk": today}
    assert find_worldbank.parse_cursor("s=2 o=300 w=2026-10-01", today)["walk"] == d(2026, 10, 1)
    assert find_worldbank.parse_cursor("watch:2026-10-10 w=2026-10-01", today) == {
        "mode": "watch", "s": 0, "o": 0, "walk": d(2026, 10, 1), "boundary": d(2026, 10, 10)}
    assert find_worldbank.parse_cursor("catch:2026-10-05 n=2026-10-09 s=3 o=200 w=2026-10-01",
                                       today) == {
        "mode": "catch", "s": 3, "o": 200, "walk": d(2026, 10, 1), "boundary": d(2026, 10, 5),
        "started": d(2026, 10, 9)}
    last = len(find_worldbank.SLICES)
    for bad in ("", "8000", "END", "s=1", "watch:soon", "s=-1 o=0", "s=0 o=-1",
                f"s={last} o=0", "s=9999 o=0", f"s=0 o={find_worldbank.MAX_OFFSET + 1}",
                "watch:2026-10-13", "watch:2026-02-30", "s=0 o=0 w=2026-10-13",
                "watch:2026-10-01 w=2026-10-05",
                f"catch:2026-10-05 n=2026-10-09 s={last} o=0 w=2026-10-01",
                "catch:2026-10-05 n=2026-10-13 s=0 o=0 w=2026-10-01",
                "catch:2026-10-09 n=2026-10-05 s=0 o=0 w=2026-10-01"):
        with pytest.raises(ValueError):
            find_worldbank.parse_cursor(bad, today)


def test_out_of_range_cursor_is_refused_without_discovery(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_worldbank.py", "--cursor", "s=9999 o=0"])
    with pytest.raises(SystemExit) as exc:
        find_worldbank.main()
    assert exc.value.code == 2 and calls == [] and not files["next"].exists()


def test_loader_treats_world_bank_delivery_hosts_as_polite_honest_and_paced():
    for host in ("documents.worldbank.org", "documents1.worldbank.org"):
        assert host in build_corpus.POLITE_HOSTS  # challenge circuit, no curl fallback
        assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA
        assert build_corpus.HOST_CONCURRENCY[host] == 1
        assert build_corpus.HOST_DELAY[host] >= 1.0
        # a PDF is 2 requests on documents1 (redirect + file)
        assert build_corpus.HOST_RUN_CAP[host] * 2 * build_corpus.HOST_DELAY[host] <= 240


def test_relevance_requires_allowed_major_type_and_aec_title():
    assert find_worldbank.relevant(
        "India - Indian road construction industry: capacity issues",
        "Policy Note",
        "Economic & Sector Work",
    )
    assert find_worldbank.relevant(
        "Développement du BTP et le secteur routier",
        "Brief",
        "Publications & Research",
    )
    assert find_worldbank.relevant(
        "城市规划与建筑节能",
        "Working Paper",
        "Publications; Publications & Research",
    )

    assert not find_worldbank.relevant(
        "Urban Water Infrastructure Assessment",
        "Environmental Assessment",
        "Project Documents",
    )
    assert not find_worldbank.relevant(
        "Kenya - The economy",
        "Pre-2003 Economic or Sector Report",
        "Economic & Sector Work",
    )
    assert not find_worldbank.relevant(
        "Announcement of a construction industry loan",
        "Announcement",
        "Publications & Research",
    )
    assert not find_worldbank.relevant(
        "Building energy efficiency",
        "Working Paper",
        "",
    )


@pytest.mark.parametrize("title", [
    "Rwanda petroleum exploration capacity building project",
    "Sudan - The road toward sustainable and broad-based growth",
    "Bosnia and Herzegovina - The road to Europe (Vol. 4 of 6) : annex 3",
    "Roadmap for a disaster risk financing strategy : Belize",
    "Building human capital in fragile states",
    "Institution building for the financial sector",
])
def test_figurative_building_and_road_titles_are_not_built_environment(title):
    assert not find_worldbank.relevant(title, "Working Paper", "Publications & Research")


@pytest.mark.parametrize("title", [
    "Building energy efficiency codes : capacity building for enforcement",
    "Kosovo - Inventory of local roads in Kosovo",
    "Green buildings : a finance and policy blueprint for emerging markets",
    "Road construction and maintenance : the road to better asset management",
])
def test_real_building_and_road_titles_survive_the_figurative_filter(title):
    assert find_worldbank.relevant(title, "Working Paper", "Publications & Research")


@pytest.mark.parametrize("document_type", [
    "Memorandum & Recommendation of the President",
    "Staff Appraisal Report",
    "Project Information Document",
    "Implementation Status and Results Report",
    "Environmental and Social Review Summary",
    "Stakeholder Engagement Plan",
    "Announcement",
    "Newsletter",
    "Credit Agreement",
    "Project Completion Report",
    "President's Speech",
    "Procedure and Checklist",
    "Annual Report",
    "Access to Information Summary of Request",
])
def test_operational_document_types_are_recognized_as_junk(document_type):
    assert find_worldbank.JUNK_DOCTY.search(document_type)


def test_slices_are_structured_classification_filters():
    assert len(find_worldbank.SLICES) == 24
    assert find_worldbank.SLICES[0] == {"majdocty_exact": "Publications & Research",
                                        "teratopic_exact": "Urban Development"}
    assert all(key.endswith("_exact") for s in find_worldbank.SLICES for key in s)
    assert {"docty_exact": "ESMAP Paper"} in find_worldbank.SLICES
