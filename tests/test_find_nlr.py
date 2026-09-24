import sys
from types import SimpleNamespace

import pytest

import find_nlr
import lint_registry


def _record(title, number, link=None, deleted=False):
    status = ' status="deleted"' if deleted else ""
    link_xml = f'<dc:identifier type="link">{link}</dc:identifier>' if link else ""
    return f"""<record><header{status}><identifier>oai:x</identifier></header><metadata>
<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
           xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title xml:lang="eng">{title}</dc:title>
<dc:subject xml:lang="eng">{number}</dc:subject>
<dc:identifier>https://research-hub.nlr.gov/en/publications/x</dc:identifier>
{link_xml}
</oai_dc:dc></metadata></record>"""


def _page(records, token=None, size=500):
    token_xml = (f'<resumptionToken cursor="0" completeListSize="{size}">{token}</resumptionToken>'
                 if token else '<resumptionToken cursor="0" completeListSize="0"/>')
    return (
        '<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<ListRecords>{''.join(records)}{token_xml}</ListRecords></OAI-PMH>"
    )


def _error(code):
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f'<error code="{code}">message</error></OAI-PMH>')


BUILDINGS = _record("ResStock Dataset 2024.1 Documentation", "NREL/TP-5500-88109",
                    "https://www.nlr.gov/docs/fy24osti/88109.pdf")
OCCUPANT = _record("Survey of Homes and Offices", "NREL/TP-5500-80001",
                   "https://www.nrel.gov/docs/fy23osti/80001.pdf")
WIND = _record("Aeroacoustics Noise Model of OpenFAST", "NREL/TP-5000-75731",
               "https://www.nlr.gov/docs/fy20osti/75731.pdf")
HVAC_OTHER_DIV = _record("Heat Pump Water Heater Field Study", "NREL/TP-6A20-70000",
                         "https://www.nlr.gov/docs/fy22osti/70000.pdf")
NO_LINK = _record("Residential building retrofits", "NREL/JA-5500-70001")
SLIDES = _record("ResStock webinar", "NREL/PR-5500-70002",
                 "https://www.nlr.gov/docs/fy22osti/70002.pdf")
OFFSITE = _record("Building controls", "NREL/CP-5500-70003", "https://hysafe.info/x.pdf")


def test_parse_and_candidate_mapping():
    records, token = find_nlr.parse_page(_page(
        [BUILDINGS, OCCUPANT, WIND, HVAC_OTHER_DIV, NO_LINK, SLIDES, OFFSITE,
         _record("Deleted", "NREL/TP-5500-1", "https://www.nlr.gov/docs/fy24osti/1.pdf",
                 deleted=True)],
        "tok",
    ))
    assert token == "tok"
    assert len(records) == 7
    kept = [c for c in map(find_nlr.candidate, records) if c]
    assert [c["url"] for c in kept] == [
        "https://docs.nlr.gov/docs/fy24osti/88109.pdf",
        "https://docs.nlr.gov/docs/fy23osti/80001.pdf",  # dead nrel.gov link canonicalised
        "https://docs.nlr.gov/docs/fy22osti/70000.pdf",  # other division, strict gate passes
    ]
    first = kept[0]
    assert first["id"].startswith("nlr-") and first["license"] == "public-domain"
    assert first["persistent_id"] == "NREL/TP-5500-88109"
    assert all(c["topic"] in lint_registry.TOPICS for c in kept)
    assert first["license"] in lint_registry.LICENSES


def test_buildings_center_uses_loose_gate_others_strict():
    occupant_elsewhere = find_nlr.parse_page(_page([_record(
        "Survey of Homes and Offices", "NREL/TP-5000-1", "https://www.nlr.gov/docs/fy23osti/1.pdf"
    )]))[0][0]
    assert find_nlr.candidate(occupant_elsewhere) is None


def test_no_records_match_is_an_empty_finished_set_and_bad_token_is_distinct():
    assert find_nlr.parse_page(_error("noRecordsMatch")) == ([], None)
    with pytest.raises(LookupError):
        find_nlr.parse_page(_error("badResumptionToken"))
    with pytest.raises(RuntimeError):
        find_nlr.parse_page(_error("badArgument"))


def _setup(monkeypatch, tmp_path, pages, known=(set(), set(), set())):
    calls = []
    queue = iter(pages)

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, raise_for_status=lambda: None)

    monkeypatch.setattr(find_nlr.requests, "get", get)
    monkeypatch.setattr(find_nlr.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(find_nlr.registry, "existing_keys", lambda: known)
    appended = []
    monkeypatch.setattr(find_nlr.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    return calls, appended, files


def test_walk_follows_token_then_steps_down_a_year(monkeypatch, tmp_path, capsys):
    known = ({"https://docs.nlr.gov/docs/fy23osti/80001.pdf"}, set(), set())
    pages = [_page([BUILDINGS], "tok-2"), _page([OCCUPANT]), _page([], "tok-2023")]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages, known)
    monkeypatch.setattr(sys, "argv", ["find_nlr.py", "--cursor", "2024:START", "--pages", "3",
                                      "--append"])

    find_nlr.main()

    requests_made = [c for c in calls if c != "sleep"]
    assert requests_made[0]["set"] == "publications:year2024"
    assert requests_made[1] == {"verb": "ListRecords", "resumptionToken": "tok-2"}
    assert requests_made[2]["set"] == "publications:year2023"
    assert calls.count("sleep") == 2  # crawl-delay between requests, none before the first
    assert [e["url"] for e in appended] == ["https://docs.nlr.gov/docs/fy24osti/88109.pdf"]
    assert files["next"].read_text() == "2023:tok-2023\n"


def test_bad_resumption_token_restarts_the_year(monkeypatch, tmp_path):
    pages = [_error("badResumptionToken"), _page([BUILDINGS], "fresh")]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_nlr.py", "--cursor", "2024:stale", "--pages", "2",
                                      "--append"])

    find_nlr.main()

    requests_made = [c for c in calls if c != "sleep"]
    assert requests_made[1]["set"] == "publications:year2024"
    assert len(appended) == 1
    assert files["next"].read_text() == "2024:fresh\n"


def test_floor_reached_reports_end_and_exhaustion(monkeypatch, tmp_path):
    _, _, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch")])
    monkeypatch.setattr(sys, "argv", ["find_nlr.py", "--cursor", f"{find_nlr.FLOOR_YEAR}:START"])

    find_nlr.main()

    assert files["next"].read_text() == "END\n"
    assert "1975" in files["exhausted"].read_text()


def test_end_cursor_makes_no_request(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_nlr.py", "--cursor", "END"])

    find_nlr.main()

    assert calls == []
    assert files["next"].read_text() == "END\n"


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, tmp_path):
    pages = [_page([BUILDINGS], "tok-2"), TimeoutError("offline")]
    _, appended, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_nlr.py", "--cursor", "2024:START", "--pages", "2",
                                      "--append"])

    with pytest.raises(SystemExit) as exc:
        find_nlr.main()

    assert exc.value.code == 1
    assert appended == [] and not files["next"].exists()


def test_pure_article_number_suffix_is_stripped_for_title_dedup():
    record = find_nlr.parse_page(_page([_record(
        "Carbon Intensity of Mass Timber Materials:Article No. 132134", "NREL/JA-5500-87665",
        "https://www.nlr.gov/docs/fy24osti/87665.pdf",
    )]))[0][0]
    assert find_nlr.candidate(record)["title"] == "Carbon Intensity of Mass Timber Materials"
