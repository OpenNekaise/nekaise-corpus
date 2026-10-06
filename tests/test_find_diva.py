import sys
from types import SimpleNamespace

import pytest

import find_diva
import lint_registry
import registry


def _mods(title, keywords=(), url="https://kth.diva-portal.org/smash/get/diva2:101/FULLTEXT01.pdf",
          note="free", access=None, genre="studentThesis", lang="swe", rid="diva2:101"):
    kw = "".join(f'<subject lang="swe"><topic>{k}</topic></subject>' for k in keywords)
    loc = (f'<location><url displayLabel="fulltext" note="{note}" access="raw object">{url}</url>'
           "</location>") if url else ""
    acc = (f'<accessCondition type="use and reproduction" xlink:href="{access}">{access}'
           "</accessCondition>") if access else ""
    return (f'<record><header><identifier>oai:DiVA.org:x</identifier></header><metadata>'
            f'<mods xmlns="http://www.loc.gov/mods/v3" xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<genre authority="diva" type="publicationTypeCode">{genre}</genre>'
            f"<titleInfo><title>{title}</title></titleInfo>"
            f'<language><languageTerm type="code">{lang}</languageTerm></language>{kw}'
            f"<originInfo><dateIssued>2024</dateIssued></originInfo>{loc}{acc}"
            f"<recordInfo><recordIdentifier>{rid}</recordIdentifier></recordInfo>"
            "</mods></metadata></record>")


def _page(records, token=None):
    tok = f'<resumptionToken completeListSize="9">{token}</resumptionToken>' if token else ""
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f"<ListRecords>{''.join(records)}{tok}</ListRecords></OAI-PMH>").encode()


def _error(code):
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f'<error code="{code}">x</error></OAI-PMH>').encode()


IDA = _mods("Energisimulering av ett flerbostadshus i IDA ICE", rid="diva2:101")
EPLUS = _mods("Calibrated EnergyPlus model of an office building",
              url="https://liu.diva-portal.org/smash/get/diva2:202/FULLTEXT02.pdf", lang="eng",
              access="https://creativecommons.org/licenses/by/4.0/", genre="doctoralThesis",
              rid="diva2:202")
NO_SIM = _mods("Energieffektivisering av flerbostadshus", rid="diva2:303",
               url="https://www.diva-portal.org/smash/get/diva2:303/FULLTEXT01.pdf")
NO_BUILT = _mods("Simulation of patient flow at an emergency department", rid="diva2:404",
                 url="https://www.diva-portal.org/smash/get/diva2:404/FULLTEXT01.pdf")
KEYWORD = _mods("Studie av en förskola", keywords=("Byggnadssimulering", "inomhusklimat"),
                rid="diva2:505", url="https://www.diva-portal.org/smash/get/diva2:505/FULLTEXT01.pdf")
RESTRICTED = _mods("Simulation of a residential building heat pump", note="restricted",
                   rid="diva2:606", url="https://www.diva-portal.org/smash/get/diva2:606/FULLTEXT01.pdf")
NC = _mods("Simulation of ventilation in a school building", rid="diva2:707",
           access="https://creativecommons.org/licenses/by-nc/4.0/",
           url="https://www.diva-portal.org/smash/get/diva2:707/FULLTEXT01.pdf")


def _entries(*records):
    mods, _ = find_diva.parse_page(_page(records))
    return [find_diva.candidate(m, "2026-10-02") for m in mods]


def test_candidate_gate_canonical_url_and_licence():
    ida, eplus, no_sim, no_built, keyword, restricted, nc = _entries(
        IDA, EPLUS, NO_SIM, NO_BUILT, KEYWORD, RESTRICTED, NC)
    assert no_sim is None and no_built is None and restricted is None
    assert ida["url"] == "https://www.diva-portal.org/smash/get/diva2:101/FULLTEXT01.pdf"
    assert eplus["url"] == "https://www.diva-portal.org/smash/get/diva2:202/FULLTEXT02.pdf"
    assert ida["license"] == "unverified" and "no verifiable" in ida["license_evidence"]
    assert eplus["license"] == "cc-by" and eplus["license_url"].endswith("/by/4.0/")
    assert nc["license"] == "cc-by-nc" and nc["license_url"].endswith("/by-nc/4.0/")
    assert keyword is not None
    assert ida["language"] == "sv" and eplus["language"] == "en"
    assert eplus["document_type"] == "thesis" and eplus["persistent_id"] == "diva2:202"
    for e in (ida, eplus, keyword, nc):
        assert e["topic"] == "simulation_modeling" in lint_registry.TOPICS
        assert registry.shard_filename(e["id"]) == "diva.yaml"
        assert not lint_registry.entry_errors(e, "diva.yaml")


def _with(record, insert):
    return record.replace("<originInfo>", insert + "<originInfo>", 1)


def test_conflicting_licence_text_is_unverified():
    rec = _with(IDA, '<accessCondition xlink:href="https://creativecommons.org/licenses/by/4.0/">'
                     "All rights reserved</accessCondition>")
    entry, = _entries(rec)
    assert entry["license"] == "unverified" and "All rights reserved" in entry["license_evidence"]


def test_nested_restriction_text_is_read():
    rec = _with(IDA, '<accessCondition xlink:href="https://creativecommons.org/licenses/by/4.0/">'
                     "<span>All rights reserved</span></accessCondition>")
    entry, = _entries(rec)
    assert entry["license"] == "unverified"


@pytest.mark.parametrize("url,text", [
    ("https://creativecommons.org/licenses/by-nd/4.0/", "Non-commercial use only"),
    ("https://creativecommons.org/licenses/by-nc/4.0/", "No derivatives"),
])
def test_restricted_url_with_any_other_statement_is_unverified(url, text):
    rec = _mods("Simulation of ventilation in a school building", rid="diva2:909",
                url="https://www.diva-portal.org/smash/get/diva2:909/FULLTEXT01.pdf")
    rec = _with(rec, f'<accessCondition xlink:href="{url}">{text}</accessCondition>')
    entry, = _entries(rec)
    assert entry["license"] == "unverified"


@pytest.mark.parametrize("text", ["No derivatives", "No commercial use", "Free to read"])
def test_open_url_with_any_free_text_is_unverified(text):
    rec = _with(IDA, '<accessCondition xlink:href="https://creativecommons.org/licenses/by/4.0/">'
                     f"{text}</accessCondition>")
    entry, = _entries(rec)
    assert entry["license"] == "unverified"


def test_open_url_repeated_as_text_stays_open():
    url = "https://creativecommons.org/licenses/by/4.0/"
    entry, = _entries(_with(IDA, f'<accessCondition xlink:href="{url}">{url}</accessCondition>'))
    assert entry["license"] == "cc-by"


def test_two_different_restricted_licences_are_unverified():
    rec = _with(NC, '<accessCondition xlink:href="https://creativecommons.org/licenses/by-nd/4.0/"'
                    "/>")
    entry, = _entries(rec)
    assert entry["license"] == "unverified"


def test_subtitle_carries_relevance_and_identity():
    a = _mods("Energy performance of a building", rid="diva2:808",
              url="https://www.diva-portal.org/smash/get/diva2:808/FULLTEXT01.pdf").replace(
        "</title>", "</title><subTitle>A simulation study using IDA ICE</subTitle>", 1)
    entry, = _entries(a)
    assert entry["title"] == "Energy performance of a building: A simulation study using IDA ICE"


def test_modelica_alone_is_not_a_building_anchor():
    pendulum = _mods("Simulation of an inverted pendulum in Modelica", lang="eng")
    hvac = _mods("Modelica simulation of an HVAC system", lang="eng")
    assert _entries(pendulum) == [None]
    assert _entries(hvac)[0] is not None


@pytest.mark.parametrize("body", [
    _page(["<record><header><identifier>x</identifier></header><metadata/></record>"]),
    _page([]),
])
def test_malformed_payloads_are_unexpected(body):
    with pytest.raises(find_diva.Unexpected):
        find_diva.parse_page(body)


def test_deleted_records_and_no_records_match_are_valid():
    deleted = ('<record><header status="deleted"><identifier>x</identifier></header></record>')
    assert find_diva.parse_page(_page([deleted], "t")) == ([], "t")
    assert find_diva.parse_page(_error("noRecordsMatch")) == ([], None)


def _diva_token(offset, size=2, stamp="1791327753133", setname="doctoralThesis"):
    return (f"{stamp}/diva/swepub_mods/{offset}/{size}/{setname}/"
            "2009-01-01T00:00:00Z/2009-12-31T23:59:59Z/")


def _counted_page(records, token, total, cursor):
    return _page(records, token).replace(
        b'completeListSize="9"', f'completeListSize="{total}" cursor="{cursor}"'.encode())


def test_full_final_page_closes_without_requesting_spurious_empty_page(monkeypatch, tmp_path):
    # DiVA emits a continuation even when a full page reaches completeListSize.
    # Its token and cursor both name the NEXT offset, unlike generic OAI cursors.
    pages = [_counted_page([IDA, EPLUS], _diva_token(2), 4, 2),
             _counted_page([KEYWORD, NC], _diva_token(4), 4, 4), _page([])]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", "1:2009:START",
                                      "--pages", "2", "--append"])
    find_diva.main()
    assert len([c for c in calls if c != "sleep"]) == 2
    assert [e["persistent_id"] for e in appended] == [
        "diva2:101", "diva2:202", "diva2:505", "diva2:707"]
    assert files["next"].read_text() == "1:2008:START\n"
    assert not files["hold"].exists() and not files["exhausted"].exists()


@pytest.mark.parametrize("requested_token", [None, _diva_token(0, stamp="1791299095935")])
def test_first_full_page_can_also_be_terminal(requested_token):
    body = _counted_page([IDA, EPLUS], _diva_token(2), 2, 2)
    mods, token = find_diva.parse_page(body, request_token=requested_token)
    assert len(mods) == 2 and token is None


def test_deleted_records_count_toward_proven_terminal_page():
    deleted = '<record><header status="deleted"><identifier>x</identifier></header></record>'
    body = _counted_page([IDA, deleted], _diva_token(4), 4, 4)
    mods, token = find_diva.parse_page(body, request_token=_diva_token(2))
    assert len(mods) == 1 and token is None


@pytest.mark.parametrize("requested_token,token,total,cursor,records", [
    (_diva_token(2), _diva_token(4), 6, 4, [IDA, EPLUS]),  # more records promised
    (_diva_token(1), _diva_token(3), 3, 3, [IDA, EPLUS]),  # non-page boundary
    (_diva_token(2), _diva_token(4), 4, 4, [IDA]),  # truncated payload
    (_diva_token(2), _diva_token(4), 4, 2, [IDA, EPLUS]),  # different cursor convention
    (_diva_token(2), _diva_token(4), "unknown", 4, [IDA, EPLUS]),
    (_diva_token(2), _diva_token(4, setname="report"), 4, 4, [IDA, EPLUS]),
    (_diva_token(2, size=1), _diva_token(4), 4, 4, [IDA, EPLUS]),
    ("opaque", _diva_token(4), 4, 4, [IDA, EPLUS]),
    (_diva_token(2), "opaque", 4, 4, [IDA, EPLUS]),
    (_diva_token(2), _diva_token(6), 6, 6, [IDA, EPLUS]),  # skipped records
])
def test_uncorroborated_count_cannot_close_window(requested_token, token, total, cursor, records):
    body = _counted_page(records, token, total, cursor)
    _, next_token = find_diva.parse_page(body, request_token=requested_token)
    assert next_token == token


@pytest.mark.parametrize("body", [
    _page([]),
    _counted_page([], _diva_token(4), 6, 4),
    _counted_page([], _diva_token(4), 4, 4),
    _error("noRecordsMatch"),
])
def test_empty_continuation_never_proves_completion(body):
    with pytest.raises(find_diva.Unexpected):
        find_diva.parse_page(body, request_token=_diva_token(4))


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
        return SimpleNamespace(status_code=200, content=item)

    monkeypatch.setattr(find_diva.requests, "get", get)
    monkeypatch.setattr(find_diva.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(find_diva.dedup, "open_keys", lambda: find_diva.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_diva.registry, "append_entries", appended.extend)
    files = {n: tmp_path / n for n in ("next", "exhausted", "hold")}
    for env, name in (("NEKAISE_ROTATION_NEXT_FILE", "next"),
                      ("NEKAISE_BACKEND_EXHAUSTED_FILE", "exhausted"),
                      ("NEKAISE_ROTATION_HOLD_FILE", "hold")):
        monkeypatch.setenv(env, str(files[name]))
    return calls, appended, files


def test_walk_follows_token_then_steps_down_a_year(monkeypatch, tmp_path):
    known = ({"https://www.diva-portal.org/smash/get/diva2:505/FULLTEXT01.pdf"}, set(), set())
    pages = [_page([IDA, KEYWORD], "tok:2"), _page([EPLUS]), _page([NO_SIM], "tok-2023")]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages, known)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", "0:2024:START", "--pages", "3",
                                      "--append"])
    find_diva.main()
    made = [c for c in calls if c != "sleep"]
    assert made[0]["set"] == "Technology" and made[0]["from"].startswith("2024-01-01")
    assert made[1] == {"verb": "ListRecords", "resumptionToken": "tok:2"}
    assert made[2]["from"].startswith("2023-01-01")
    assert calls.count("sleep") == 2
    assert [e["persistent_id"] for e in appended] == ["diva2:101", "diva2:202"]
    assert files["next"].read_text() == "0:2023:tok-2023\n"


def test_floor_year_moves_to_next_set_and_last_set_exhausts(monkeypatch, tmp_path):
    last = len(find_diva.SETS) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor",
                                      f"{last}:{find_diva.FLOOR_YEAR}:START", "--pages", "1"])
    find_diva.main()
    assert files["next"].read_text() == "END\n" and files["exhausted"].read_text().strip()


def test_floor_year_of_a_middle_set_starts_the_next_set(monkeypatch, tmp_path):
    _, _, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor",
                                      f"0:{find_diva.FLOOR_YEAR}:START", "--pages", "1"])
    find_diva.main()
    assert files["next"].read_text().startswith("1:")
    assert files["next"].read_text().endswith(":START\n")


def test_bad_token_restarts_the_year_once(monkeypatch, tmp_path):
    pages = [_error("badResumptionToken"), _page([IDA], "fresh")]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", "0:2024:stale", "--pages", "2",
                                      "--append"])
    find_diva.main()
    made = [c for c in calls if c != "sleep"]
    assert made[1]["set"] == "Technology" and len(appended) == 1
    assert files["next"].read_text() == "0:2024:fresh\n"


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, content=b""),
    b"<html>maintenance</html>",
    b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>x</body></html>',
    _error("badArgument"),
])
def test_unexpected_answer_holds_and_proposes_nothing(monkeypatch, tmp_path, answer):
    _, appended, files = _setup(monkeypatch, tmp_path, [_page([IDA], "t"), answer])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", "0:2024:START", "--pages", "3",
                                      "--append"])
    find_diva.main()
    assert appended == [] and files["hold"].read_text().strip()
    assert not files["next"].exists()


def test_network_failure_exits_nonzero(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [_page([IDA], "t"), TimeoutError("x")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", "0:2024:START", "--pages", "3",
                                      "--append"])
    with pytest.raises(SystemExit) as exc:
        find_diva.main()
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()


@pytest.mark.parametrize("cursor", ["", "0:2024", "a:2024:START", "0:x:START", "0:2024:"])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        find_diva.parse_cursor(cursor)
