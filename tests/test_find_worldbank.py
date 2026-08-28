import sys

import pytest

import find_worldbank


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_worldbank.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_worldbank,
        "fetch_page",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_worldbank.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_worldbank.py", "--append", "--pages", "1"],
    )

    with pytest.raises(SystemExit) as exc:
        find_worldbank.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_page_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_worldbank, "fetch_page", lambda *_args: (2060, []))
    monkeypatch.setattr(
        find_worldbank.registry,
        "append_entries",
        lambda _entries: pytest.fail("an empty result must not append"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_worldbank.py", "--append", "--os", "5000", "--pages", "1"],
    )

    find_worldbank.main()

    assert "0 NEW World Bank docs" in capsys.readouterr().out


def test_wds_results_are_not_blanket_labeled_cc_by(monkeypatch):
    _empty_registry(monkeypatch)
    captured = []
    monkeypatch.setattr(
        find_worldbank,
        "fetch_page",
        lambda *_args: (1, [{
            "title": "Urban Water Infrastructure Assessment",
            "pdf_url": "https://documents.worldbank.org/example.pdf",
            "docty": "Working Paper",
            "majdocty": "Publications & Research",
            "lang": "English",
        }]),
    )
    monkeypatch.setattr(find_worldbank.registry, "append_entries", captured.extend)
    monkeypatch.setattr(find_worldbank.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_worldbank.py", "--append", "--pages", "1"],
    )

    find_worldbank.main()

    assert len(captured) == 1
    assert captured[0]["source"] == "worldbank_wds"
    assert captured[0]["license"] == "open"


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
])
def test_operational_document_types_are_recognized_as_junk(document_type):
    assert find_worldbank.JUNK_DOCTY.search(document_type)


def test_fetch_page_drops_project_document_even_with_aec_title(monkeypatch):
    request = {}
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"total": 1, "documents": {"D1": {
            "display_title": "Urban Water Infrastructure Assessment",
            "pdfurl": "https://documents.worldbank.org/project.pdf",
            "docty": "Environmental Assessment",
            "majdocty": "Project Documents",
            "lang": "English",
        }}},
    })()

    def fake_get(_url, **kwargs):
        request.update(kwargs)
        return response

    monkeypatch.setattr(find_worldbank.requests, "get", fake_get)

    total, docs = find_worldbank.fetch_page("water infrastructure", 50, 0)

    assert total == 1
    assert docs == []
    assert "majdocty" in request["params"]["fl"]
    assert "lang" in request["params"]["fl"]
