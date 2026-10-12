import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import find_govuk
import lint_registry
import registry


def test_title_relevance_keeps_explicit_built_environment_titles():
    titles = [
        "How to assess and allocate sites for development",
        "Inclusive mobility: guidance on the use of tactile paving surfaces",
        "MA04 Land quality baseline data",
        "Flood risk: Upper Aire management strategy",
        "Energy performance certificates for non-domestic buildings",
        "Approved Document L: conservation of fuel and power",
        "Future Homes Standard: 2025 consultation response",
        "Standard Assessment Procedure for energy rating of dwellings",
        "Digest of UK Energy Statistics 2025",
        "Energy Company Obligation: ECO4 guidance",
        "Fire safety: Approved Document B",
        "National Planning Policy Framework: planning policy for traveller sites",
        "Benchmarking tunnelling costs and production rates in the UK",
        "Reinforced autoclaved aerated concrete (RAAC) in schools",
        "Domestic hot water use ; research on heat pumps in existing homes",
    ]

    assert all(find_govuk.title_relevant(title) for title in titles)


def test_title_relevance_rejects_deep_search_false_positives():
    titles = [
        "Armed forces continuous working patterns survey 2017/18",
        "Crab and lobster stock assessment 2017",
        "Food Information Regulations 2013",
        "PIP breast implants: interim report",
        "The nature of online offending",
        "Child Support Fees Regulations 2012: equality impact assessment",
        "Growing the automotive supply chain: the road forward",
        # an organisation walk meets these department publications
        "Oil and gas: environmental submissions and determinations",
        "Road freight statistics: 2019",
        "School-age childcare guidance for schools",
        "When you have sponsored for 6 months: Homes for Ukraine",
        "Daily domestic transport use by mode",
        "Fire and rescue workforce and pensions statistics",
        "Care homes market study",
        # anchored, but administration or single casework files
        "Section 62A Planning Application: S62A/2025/0110 7 Redcatch Road, Bristol",
        "Memorandum of Understanding between the Regulator of Social Housing and Ofsted",
        "MOD byelaws: Isle of Wight housing estate",
    ]

    assert not any(find_govuk.title_relevant(title) for title in titles)


def _result(link, title, description=""):
    return {"link": link, "title": title, "description": description}


def _page(title, attachments=(), body="", path="/x", kind="publication"):
    return {"base_path": path, "document_type": kind, "title": title,
            "details": {"attachments": list(attachments), "body": body}}


def _pdf(title, name):
    return {"title": title, "content_type": "application/pdf", "attachment_type": "file",
            "url": f"https://assets.publishing.service.gov.uk/media/abc/{name}.pdf"}


def _html(title, path):
    return {"title": title, "attachment_type": "html", "url": path}


TODAY = datetime.now(timezone.utc).date()
AD_L = _result("/government/publications/approved-document-l", "Approved Document L",
               "Building regulation in England for the conservation of fuel and power")
OFF = _result("/government/statistics/road-freight-2019", "Road freight statistics: 2019")
GUIDE = _result("/guidance/heat-networks-overview", "Heat networks: overview")
PAGES = {
    "/government/publications/approved-document-l": _page("Approved Document L", [
        _pdf("Approved Document L, volume 1: dwellings", "adl1"),
        _pdf("Approved Document L", "adl-main"),
        _pdf("Equality impact assessment", "eia"),
        _html("Approved Document L: frequently asked questions",
              "/government/publications/approved-document-l/faq"),
        {"title": "Data tables", "attachment_type": "file",
         "content_type": "application/vnd.oasis.opendocument.spreadsheet",
         "url": "https://assets.publishing.service.gov.uk/media/abc/tables.ods"},
        {"title": "Elsewhere", "content_type": "application/pdf",
         "url": "https://example.org/not-gov.pdf"},
        _html("Off-site", "//evil.example/x"),
    ]),
    "/guidance/heat-networks-overview": _page("Heat networks: overview", body="<p>x</p>" * 1000),
}


def _setup(monkeypatch, tmp_path, searches, pages=PAGES, known=None):
    """`searches`: one item per Search request — a result list (the organisation's last page),
    a (total, results) pair, an exception, or a ready response object."""
    known = known or (set(), set(), set())
    calls, queue = [], iter(searches)

    def get(url, **kwargs):
        assert "nekaise-corpus" in kwargs["headers"]["User-Agent"]
        if url == find_govuk.SEARCH:
            calls.append(("search", kwargs["params"]))
            item = next(queue)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, SimpleNamespace):
                return item
            total, results = item if isinstance(item, tuple) else (
                kwargs["params"]["start"] + len(item), item)
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"total": total, "results": results})
        path = url.removeprefix("https://www.gov.uk/api/content")
        calls.append(("content", path))
        if path not in pages:
            return SimpleNamespace(status_code=404, json=lambda: {})
        page = pages[path]
        if isinstance(page, SimpleNamespace):
            return page
        return SimpleNamespace(status_code=200, json=lambda page=page: page)

    monkeypatch.setattr(find_govuk.requests, "get", get)
    monkeypatch.setattr(find_govuk.time, "sleep", lambda _s: None)
    monkeypatch.setattr(find_govuk.dedup, "open_keys",
                        lambda: find_govuk.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_govuk.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    for path in files.values():
        path.unlink(missing_ok=True)
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


def test_walk_is_structured_and_registers_pdf_html_and_body_documents(monkeypatch, tmp_path):
    monkeypatch.setattr(find_govuk, "ROWS", 3)
    monkeypatch.setattr(find_govuk, "OVERLAP", 1)
    calls, appended, files = _setup(monkeypatch, tmp_path, [(20, [AD_L, OFF, GUIDE])])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", "o=1 s=6", "--pages", "1",
                                      "--append"])
    find_govuk.main()

    kind, params = calls[0]
    assert kind == "search" and "q" not in params  # no free-text query: nothing fuzzy to rank
    assert params["filter_organisations"] == find_govuk.ORGS[1]
    assert params["order"] == "-public_timestamp" and params["start"] == 6
    assert set(params["filter_content_store_document_type"]) == set(find_govuk.DOC_TYPES)
    assert "filter_public_timestamp" not in params
    # the off-topic statistics page never reaches the Content API
    assert [c[1] for c in calls if c[0] == "content"] == [AD_L["link"], GUIDE["link"]]
    assert [(e["title"], e["format"]) for e in appended] == [
        ("Approved Document L — Approved Document L, volume 1: dwellings", "pdf"),
        ("Approved Document L", "pdf"),
        ("Approved Document L — Approved Document L: frequently asked questions", "html"),
        ("Heat networks: overview", "html"),
    ]
    assert appended[2]["url"] == "https://www.gov.uk/government/publications/approved-document-l/faq"
    assert appended[3]["url"] == "https://www.gov.uk/guidance/heat-networks-overview"
    for entry in appended:
        assert entry["topic"] in lint_registry.TOPICS
        assert registry.shard_filename(entry["id"]).startswith("govuk-")
        assert not lint_registry.entry_errors(entry, registry.shard_filename(entry["id"]))
    # pages overlap by OVERLAP results; the walk's start day is persisted as its watermark
    assert files["next"].read_text() == f"o=1 s=8 w={TODAY}\n"
    assert not files["exhausted"].exists() and not files["hold"].exists()


def test_new_rows_are_unverified_with_the_ogl_statement_as_evidence_only(monkeypatch, tmp_path):
    _, appended, _ = _setup(monkeypatch, tmp_path, [[AD_L]])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--pages", "1", "--append"])
    find_govuk.main()
    assert appended
    for entry in appended:
        assert entry["source"] == "gov_uk" and entry["license"] == "unverified"
        assert entry["license"] in lint_registry.LICENSES and "license_url" not in entry
        assert "Open Government Licence v3.0, except where otherwise stated" in \
            entry["license_evidence"]
        assert "no document-specific check" in entry["license_evidence"]


def test_known_urls_and_titles_are_skipped(monkeypatch, tmp_path):
    known = ({"https://assets.publishing.service.gov.uk/media/abc/adl1.pdf"},
             {registry.norm("Approved Document L")}, set())
    _, appended, _ = _setup(monkeypatch, tmp_path, [[AD_L]], known=known)
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--pages", "1", "--append"])
    find_govuk.main()
    assert [e["format"] for e in appended] == ["html"]


def test_last_page_moves_to_the_next_organisation_and_keeps_the_watermark(monkeypatch, tmp_path):
    _, _, files = _setup(monkeypatch, tmp_path, [[OFF]])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", "o=0 s=50 w=2026-10-01",
                                      "--pages", "1"])
    find_govuk.main()
    assert files["next"].read_text() == "o=1 s=0 w=2026-10-01\n"


def test_the_watch_starts_at_the_walks_start_day_not_its_completion_day(monkeypatch, tmp_path):
    last = len(find_govuk.ORGS) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [[OFF]])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", f"o={last} s=0 w=2026-10-01"])
    find_govuk.main()
    assert files["next"].read_text() == "watch:2026-10-01 w=2026-10-01\n"
    assert not files["exhausted"].exists()


def test_the_last_page_of_the_search_window_is_read(monkeypatch, tmp_path, capsys):
    """Codex round 3: a 45-result stride would stop at 9945 and leave 9995-9999 unread."""
    near = find_govuk.MAX_START - 30
    page = [OFF] * find_govuk.ROWS
    calls, _, files = _setup(monkeypatch, tmp_path, [(20000, page), (20000, page)])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", f"o=2 s={near}",
                                      "--pages", "1"])
    find_govuk.main()
    assert files["next"].read_text() == f"o=2 s={find_govuk.MAX_START} w={TODAY}\n"
    assert "COVERAGE EXCLUSION" not in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor",
                                      f"o=2 s={find_govuk.MAX_START}", "--pages", "1"])
    find_govuk.main()
    assert calls[-1][1]["start"] == find_govuk.MAX_START
    assert find_govuk.MAX_START + find_govuk.ROWS == 10000
    assert files["next"].read_text() == f"o=3 s=0 w={TODAY}\n"
    assert "COVERAGE EXCLUSION (search window)" in capsys.readouterr().out


def test_search_window_limit_is_a_reported_coverage_exclusion(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(find_govuk, "ROWS", 1)
    monkeypatch.setattr(find_govuk, "OVERLAP", 0)
    _, _, files = _setup(monkeypatch, tmp_path, [(20000, [OFF])])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor",
                                      f"o=2 s={find_govuk.MAX_START}", "--pages", "1"])
    find_govuk.main()
    assert files["next"].read_text() == f"o=3 s=0 w={TODAY}\n"
    assert "COVERAGE EXCLUSION (search window)" in capsys.readouterr().out


def _day(days_ago):
    return TODAY - timedelta(days=days_ago)


def test_watch_idles_until_the_next_look_is_due(monkeypatch, tmp_path):
    cursor = f"watch:{_day(find_govuk.WATCH_DAYS - 1)} w={_day(20)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", cursor])
    find_govuk.main()
    assert calls == [] and files["next"].read_text() == cursor + "\n"


def test_catch_up_honours_the_budgets_and_continues_in_the_next_run(monkeypatch, tmp_path):
    """Codex round 2: a backlog after downtime is read over several runs, never in one."""
    monkeypatch.setattr(find_govuk, "ORGS", ("org-a", "org-b"))
    monkeypatch.setattr(find_govuk, "ROWS", 1)
    monkeypatch.setattr(find_govuk, "OVERLAP", 0)
    boundary, walk = _day(find_govuk.WATCH_DAYS), _day(30)

    # run 1: --pages 2 stops inside org-a
    calls, appended, files = _setup(monkeypatch, tmp_path, [(3, [GUIDE]), (3, [OFF])])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", f"watch:{boundary} w={walk}",
                                      "--pages", "2", "--append"])
    find_govuk.main()
    searches = [c[1] for c in calls if c[0] == "search"]
    assert [(q["filter_organisations"], q["start"]) for q in searches] == [
        ("org-a", 0), ("org-a", 1)]
    assert {q["filter_public_timestamp"] for q in searches} == {
        f"from:{boundary - timedelta(days=1)}"}
    assert len(appended) == 1
    middle = f"catch:{boundary} n={TODAY} o=0 s=2 w={walk}"
    assert files["next"].read_text() == middle + "\n"

    # run 2: --max 1 stops after the complete page that reached it; the filter date is unchanged
    calls, appended, files = _setup(monkeypatch, tmp_path, [[AD_L]])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", middle, "--max", "1",
                                      "--append"])
    find_govuk.main()
    assert [c[1]["start"] for c in calls if c[0] == "search"] == [2]
    assert calls[0][1]["filter_public_timestamp"] == f"from:{boundary - timedelta(days=1)}"
    assert len(appended) == 3  # the whole page is kept, then the budget stops the run
    after = f"catch:{boundary} n={TODAY} o=1 s=0 w={walk}"
    assert files["next"].read_text() == after + "\n"

    # run 3: org-b has nothing new (an empty FILTERED listing is normal) -> next boundary is
    # the day the look STARTED
    started = _day(1)
    resumed = f"catch:{boundary} n={started} o=1 s=0 w={walk}"
    _, _, files = _setup(monkeypatch, tmp_path, [[]])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", resumed])
    find_govuk.main()
    assert files["next"].read_text() == f"watch:{started} w={walk}\n"


def test_periodic_full_rewalk_reconciles_removals_larger_than_the_overlap(monkeypatch, tmp_path):
    """Codex round 2: read 0-49, save offset 45, six results before it are withdrawn -> the
    result that moves from 50 to 44 is skipped by this walk. Its old timestamp keeps it out of
    every catch-up; the next full walk reads it."""
    monkeypatch.setattr(find_govuk, "ORGS", ("org-a",))
    listing = [_result(f"/guidance/heat-networks-part-{i}", f"Heat networks: part {i}")
               for i in range(60)]
    pages = {r["link"]: _page(r["title"], body="<p>x</p>" * 1000) for r in listing}
    seen = (set(), set(), set())

    def run(cursor, answers):
        calls, appended, files = _setup(monkeypatch, tmp_path, answers, pages=pages, known=seen)
        monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", cursor, "--pages", "1",
                                          "--max", "999", "--append"])
        find_govuk.main()
        seen[0].update(e["url"] for e in appended)
        seen[1].update(registry.norm(e["title"]) for e in appended)
        return [e["title"] for e in appended], files["next"].read_text().strip()

    walk = _day(find_govuk.REWALK_DAYS)
    first, cursor = run(f"o=0 s=0 w={walk}", [(60, listing[:50])])
    assert len(first) == 50 and cursor == f"o=0 s=45 w={walk}"
    shifted = listing[6:]                      # results 0-5 are withdrawn
    second, cursor = run(cursor, [(54, shifted[45:])])
    assert cursor == f"watch:{walk} w={walk}"
    assert "Heat networks: part 50" not in first + second   # lost to the shift

    # the full walk is due REWALK_DAYS after the last one started, whatever the watch boundary
    third, cursor = run(f"watch:{TODAY} w={walk}", [(54, shifted[:50])])
    assert third == ["Heat networks: part 50"] and cursor == f"o=0 s=45 w={TODAY}"
    assert "full re-walk" in find_govuk.__doc__


def test_short_page_after_a_good_page_holds_the_whole_run(monkeypatch, tmp_path):
    monkeypatch.setattr(find_govuk, "ROWS", 2)
    monkeypatch.setattr(find_govuk, "OVERLAP", 0)
    _, appended, files = _setup(monkeypatch, tmp_path, [(9, [AD_L, GUIDE]), (9, [GUIDE])])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--pages", "2", "--append"])
    find_govuk.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_an_organisation_without_any_publication_holds(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [(0, [])])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", "o=4 s=0", "--append"])
    find_govuk.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def _ok(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload)


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html"))),
    _ok({"error": "temporarily unavailable"}),
    _ok({"results": [AD_L]}),                              # no total
    _ok({"total": "many", "results": [AD_L]}),
    _ok({"total": 1, "results": ["x"]}),
    _ok({"total": 1, "results": [{"title": "Approved Document L"}]}),       # no link
    _ok({"total": 1, "results": [{"link": "/x", "title": None}]}),
    _ok({"total": 120, "results": []}),                    # empty page while results remain
    _ok({"total": 120, "results": [OFF]}),                 # short page while results remain
])
def test_unexpected_search_answers_hold(monkeypatch, tmp_path, answer):
    _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--append"])
    find_govuk.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


@pytest.mark.parametrize("broken", [
    SimpleNamespace(status_code=500, json=lambda: {}),
    _ok({"error": "temporarily unavailable"}),             # HTTP 200 error object
    _ok(["not", "an", "object"]),
    _ok({"base_path": "/x", "document_type": "guidance", "title": "Heat networks"}),
    _ok({"base_path": "/x", "document_type": "guidance", "title": "Heat networks",
         "details": {"attachments": "none"}}),
    _ok({"base_path": "/x", "document_type": "guidance", "title": "Heat networks",
         "details": {"attachments": [{"title": "no url"}]}}),
])
def test_unexpected_content_answers_hold_the_whole_run(monkeypatch, tmp_path, broken):
    pages = dict(PAGES)
    pages[GUIDE["link"]] = broken
    _, appended, files = _setup(monkeypatch, tmp_path, [[AD_L, GUIDE]], pages=pages)
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--pages", "1", "--append"])
    find_govuk.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_gone_pages_are_skipped_without_a_hold(monkeypatch, tmp_path):
    for gone in ({AD_L["link"]: PAGES[AD_L["link"]]},  # the guide answers 404
                 {**PAGES, GUIDE["link"]: {"base_path": GUIDE["link"], "document_type": "gone"}}):
        _, appended, files = _setup(monkeypatch, tmp_path, [[AD_L, GUIDE]], pages=gone)
        monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--pages", "1", "--append"])
        find_govuk.main()
        assert len(appended) == 3 and files["next"].read_text() == f"o=1 s=0 w={TODAY}\n"


def test_network_failure_exits_nonzero_without_append(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [requests.ConnectionError("down")])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--append"])
    with pytest.raises(SystemExit) as exc:
        find_govuk.main()
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()


def test_attachment_cap_is_a_reported_coverage_exclusion(monkeypatch, tmp_path, capsys):
    many = {AD_L["link"]: _page("Approved Document L", [
        _pdf(f"Approved Document L map book {i}", f"m{i}") for i in range(40)])}
    _, appended, _ = _setup(monkeypatch, tmp_path, [[AD_L]], pages=many)
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--pages", "1", "--append"])
    find_govuk.main()
    assert len(appended) == find_govuk.ATTACHMENTS_PER_PAGE
    out = capsys.readouterr().out
    assert (f"COVERAGE EXCLUSION (attachment cap) {AD_L['link']}: first "
            f"{find_govuk.ATTACHMENTS_PER_PAGE} of 40 documents") in out
    assert "NOT collected" in find_govuk.__doc__


def test_cursor_forms_and_bounds():
    today, d = date(2026, 10, 12), date
    assert find_govuk.parse_cursor("o=3 s=150", today) == {
        "mode": "walk", "o": 3, "s": 150, "walk": today}
    assert find_govuk.parse_cursor("o=3 s=150 w=2026-10-01", today)["walk"] == d(2026, 10, 1)
    assert find_govuk.parse_cursor("watch:2026-10-12", today) == {
        "mode": "watch", "o": 0, "s": 0, "walk": today, "boundary": today}
    assert find_govuk.parse_cursor("watch:2026-10-10 w=2026-10-01", today)["walk"] == \
        d(2026, 10, 1)
    assert find_govuk.parse_cursor("catch:2026-10-05 n=2026-10-09 o=3 s=100 w=2026-10-01",
                                   today) == {
        "mode": "catch", "o": 3, "s": 100, "walk": d(2026, 10, 1), "boundary": d(2026, 10, 5),
        "started": d(2026, 10, 9)}
    last = len(find_govuk.ORGS)
    for bad in ("", "905", "END", "o=1", "watch:soon", "o=-1 s=0", "o=1 s=-5",
                f"o={last} s=0", "o=9999 s=0",
                f"o=0 s={find_govuk.MAX_START + 1}", "watch:2026-10-13", "watch:2026-13-40",
                "o=0 s=0 w=2026-10-13", "o=0 s=0 w=2026-02-30", "watch:2026-10-01 w=2026-10-05",
                f"catch:2026-10-05 n=2026-10-09 o={last} s=0 w=2026-10-01",
                "catch:2026-10-05 n=2026-10-13 o=0 s=0 w=2026-10-01",
                "catch:2026-10-09 n=2026-10-05 o=0 s=0 w=2026-10-01"):
        with pytest.raises(ValueError):
            find_govuk.parse_cursor(bad, today)


def test_out_of_range_cursor_is_refused_without_discovery(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_govuk.py", "--cursor", "o=9999 s=0"])
    with pytest.raises(SystemExit) as exc:
        find_govuk.main()
    assert exc.value.code == 2 and calls == [] and not files["next"].exists()


def test_loader_treats_govuk_delivery_hosts_as_polite_honest_and_paced():
    for host in ("www.gov.uk", "assets.publishing.service.gov.uk"):
        assert host in build_corpus.POLITE_HOSTS  # challenge circuit, no curl fallback
        assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA
        assert build_corpus.HOST_CONCURRENCY[host] == 1
        assert build_corpus.HOST_DELAY[host] >= 1.0
        assert build_corpus.HOST_RUN_CAP[host] * build_corpus.HOST_DELAY[host] <= 240
