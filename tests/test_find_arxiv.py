import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import find_arxiv
import lint_registry
import registry

NONEXCLUSIVE = "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"
BUILDING = ("We present a model predictive controller for the HVAC system of an office building "
            "and evaluate thermal comfort and energy use in EnergyPlus.")


def _record(aid, title, abstract=BUILDING, licence=NONEXCLUSIVE, stamp="2026-09-01", doi=None,
            versions=1, deleted=False):
    if deleted:
        return (f'<record><header status="deleted"><identifier>oai:arXiv.org:{aid}</identifier>'
                f"<datestamp>{stamp}</datestamp></header></record>")
    vs = "".join(f'<version version="v{n}"><date>Wed, 0{n} Mar 2019 16:33:57 GMT</date>'
                 f"<size>1kb</size></version>" for n in range(1, versions + 1))
    lic = f"<license>{licence}</license>" if licence else ""
    d = f"<doi>{doi}</doi>" if doi else ""
    return (f"<record><header><identifier>oai:arXiv.org:{aid}</identifier>"
            f"<datestamp>{stamp}</datestamp><setSpec>eess:eess:SY</setSpec></header><metadata>"
            f'<arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>{aid}</id>{vs}'
            f"<title>{title}</title><categories>eess.SY cs.SY</categories>{d}{lic}"
            f"<abstract>{abstract}</abstract></arXivRaw></metadata></record>")


def _page(records, token=None):
    tok = (f"<resumptionToken expirationDate='2026-10-13T00:00:00Z'>{token}</resumptionToken>"
           if token else "")
    return (f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>'
            f'{"".join(records)}{tok}</ListRecords></OAI-PMH>').encode()


def _error(code):
    return (f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="{code}">x'
            f"</error></OAI-PMH>").encode()


HVAC = _record("2609.07619", "Data-driven estimation of design heating loads for HVAC equipment "
               "sizing", versions=2, doi="10.1016/j.enbuild.2026.1")
BRIDGE = _record("2608.30939", "Finite Element Model Updating-based Load Rating of Bridges",
                 abstract="Highway bridges age.", licence="http://creativecommons.org/licenses/by/4.0/",
                 stamp="2026-09-02")
NCND = _record("2609.00001", "Seismic retrofit of masonry school buildings",
               licence="http://creativecommons.org/licenses/by-nc-nd/4.0/", stamp="2026-09-03")
NO_LICENCE = _record("2609.00002", "Thermal comfort in naturally ventilated dwellings",
                     licence=None, stamp="2026-09-03")
MICROGRID = _record("2609.00003", "Droop control in DC microgrids",
                    abstract="Microgrids and demand response in smart cities.")
WIFI = _record("2609.00004", "Indoor localization in buildings with WiFi fingerprints")
BLOCKS = _record("2609.00005", "Building blocks of quantum error correction",
                 abstract="Bridging the gap between network architecture and concrete examples.")
DENSE = _record("2609.00006", "A benchmark for learning-based control",
                abstract="District heating networks, heat pumps and thermal comfort in "
                         "residential buildings are simulated.")


def _parsed(*records):
    return find_arxiv.parse_page(_page(records), first=True)[0]


def test_gate_keeps_engineering_sense_and_drops_generic_or_figurative_titles():
    kept = {r["id"] for r in _parsed(HVAC, BRIDGE, NCND, NO_LICENCE, MICROGRID, WIFI, BLOCKS,
                                     DENSE) if find_arxiv.candidate(r, "2026-10-12")}
    assert kept == {"2609.07619", "2608.30939", "2609.00001", "2609.00002", "2609.00006"}
    for title in ("Building a foundation model for time series",
                  "A concrete example of bridging the gap in network architecture",
                  "Structural analysis of Laplacian spectra of social networks",
                  "Model predictive control for microgrid dispatch",
                  "Elemental germanium phase-change memory",
                  "Smart city data platforms and demand response markets"):
        assert not find_arxiv.relevant(title, "Graphs, agents and optimisation."), title
    # mathematical (Tits) buildings are not built environment, even with an empty abstract
    for title in ("Euclidean buildings and their automorphism groups",
                  "Spherical buildings and representation theory",
                  "Affine buildings of type A_n and their boundaries",
                  "Automorphism groups of right-angled buildings",
                  "Buildings and Coxeter groups",
                  "Lattices in Kac-Moody buildings"):
        assert not find_arxiv.relevant(title, ""), title
    for title in ("Evacuation of tall buildings and their occupants",
                  "Energy flexibility of groups of residential buildings",
                  "Fire spread between buildings with timber façades",
                  "Typology of buildings for seismic risk assessment"):
        assert find_arxiv.relevant(title, ""), title
    for title in ("Zero-shot model predictive control of buildings",
                  "Buildability of 3D-printed concrete structures",
                  "Urban microclimate emulation with neural operators",
                  "Water distribution network inspection and maintenance",
                  "Occupancy detection for demand-controlled ventilation in office buildings"):
        assert find_arxiv.relevant(title, ""), title


def test_licence_classes_fields_and_shard():
    entries = {r["id"]: find_arxiv.candidate(r, "2026-10-12")
               for r in _parsed(HVAC, BRIDGE, NCND, NO_LICENCE)}
    hvac, bridge, ncnd, none = (entries[k] for k in ("2609.07619", "2608.30939", "2609.00001",
                                                     "2609.00002"))
    assert hvac["license"] == "arxiv-nonexclusive" and hvac["license_url"] == NONEXCLUSIVE
    assert bridge["license"] == "cc-by" and ncnd["license"] == "cc-by-nc-nd"
    assert none["license"] == "arxiv-nonexclusive" and "no licence element" in none["license_evidence"]
    assert none["license_url"].startswith("https://oaipmh.arxiv.org/oai?verb=GetRecord")
    # robots: Allow /pdf; the URL pins the version whose licence was read
    assert hvac["url"] == "https://arxiv.org/pdf/2609.07619v2"
    assert bridge["url"] == "https://arxiv.org/pdf/2608.30939v1"
    assert hvac["persistent_id"] == "doi:10.48550/arxiv.2609.07619"
    assert hvac["origin_ids"] == "doi:10.1016/j.enbuild.2026.1" and "origin_ids" not in bridge
    assert "(v2, the current version" in hvac["license_evidence"]
    assert hvac["published_at"] == "2019-03-01"
    assert bridge["topic"] == "structures_civil"
    for entry in entries.values():
        assert entry["source"] == "arxiv" and entry["document_type"] == "preprint"
        assert entry["license"] in lint_registry.LICENSES
        assert entry["topic"] in lint_registry.TOPICS
        assert entry["rights_verified_at"] == "2026-10-12"
        assert registry.shard_filename(entry["id"]) == "papers.yaml"
        assert not lint_registry.entry_errors(entry, "papers.yaml")
    odd = _parsed(_record("2609.00009", "HVAC control in buildings", licence="https://example.org/l"))
    assert find_arxiv.candidate(odd[0], "x")["license"] == "unverified"


def _setup(monkeypatch, tmp_path, answers, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(answers)

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        assert url == find_arxiv.OAI and "nekaise-corpus" in kwargs["headers"]["User-Agent"]
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, content=item)

    monkeypatch.setattr(find_arxiv.requests, "get", get)
    monkeypatch.setattr(find_arxiv.time, "sleep", lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(find_arxiv.dedup, "open_keys",
                        lambda: find_arxiv.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_arxiv.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    for f in files.values():
        f.unlink(missing_ok=True)
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


def _argv(monkeypatch, cursor, *extra):
    monkeypatch.setattr(sys, "argv", ["find_arxiv.py", "--cursor", cursor, "--append", *extra])


FRESH = "s=0 t=START f=-,-,-,-,-,- w=-"


def test_first_page_uses_the_set_then_the_token_one_request_per_three_seconds(monkeypatch,
                                                                             tmp_path):
    calls, appended, files = _setup(monkeypatch, tmp_path,
                                    [_page([HVAC, MICROGRID], "TOK%3D1"), _page([BRIDGE], "TOK%3D2")])
    _argv(monkeypatch, FRESH, "--pages", "2")
    find_arxiv.main()
    requests_made = [c for c in calls if isinstance(c, dict)]
    assert requests_made[0] == {"verb": "ListRecords", "metadataPrefix": "arXivRaw",
                                "set": "eess:eess:SY"}
    assert requests_made[1] == {"verb": "ListRecords", "resumptionToken": "TOK%3D1"}
    assert [c for c in calls if isinstance(c, tuple)] == [("sleep", find_arxiv.OAI_DELAY)]
    assert find_arxiv.OAI_DELAY >= 3.0
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["2609.07619v2", "2608.30939v1"]
    assert files["next"].read_text() == "s=0 t=TOK%3D2 f=2026-09-02,-,-,-,-,- w=-\n"
    assert not files["exhausted"].exists()


def test_dedup_by_url_forms_title_and_doi(monkeypatch, tmp_path):
    known = ({"https://arxiv.org/pdf/2608.30939"},      # held unversioned (older rows)
             {registry.norm("Seismic retrofit of masonry school buildings")}, set(),
             {"doi:10.1016/j.enbuild.2026.1"})
    _, appended, _ = _setup(monkeypatch, tmp_path, [_page([HVAC, BRIDGE, NCND, NO_LICENCE], "T")],
                            known)
    _argv(monkeypatch, FRESH, "--pages", "1")
    find_arxiv.main()
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["2609.00002v1"]


def test_a_replacement_after_discovery_cannot_change_the_bytes_under_the_recorded_rights(
        monkeypatch, tmp_path):
    """v1 is CC BY at discovery; the author then replaces it with v2 under another licence."""
    cc_by = "http://creativecommons.org/licenses/by/4.0/"
    title = "Seismic retrofit of masonry school buildings"
    v1 = _record("2609.00001", title, licence=cc_by, versions=1)
    v2 = _record("2609.00001", title, licence=NONEXCLUSIVE, versions=2, stamp="2026-10-01")
    _, appended, _ = _setup(monkeypatch, tmp_path, [_page([v1], "T")])
    _argv(monkeypatch, FRESH, "--pages", "1")
    find_arxiv.main()
    (held,) = appended
    assert held["url"] == "https://arxiv.org/pdf/2609.00001v1" and held["license"] == "cc-by"
    assert "(v1, the current version" in held["license_evidence"]

    # the loader requests exactly the pinned version, never the unversioned (now v2) URL
    served = {held["url"]: b"%PDF-1.7 version one", "https://arxiv.org/pdf/2609.00001":
              b"%PDF-1.7 version two", "https://arxiv.org/pdf/2609.00001v2": b"%PDF-1.7 version two"}
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        return SimpleNamespace(status_code=200, content=served[url], headers={}, url=url,
                               raise_for_status=lambda: None)

    monkeypatch.setattr(build_corpus.requests, "get", get)
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("a polite host must never reach curl"))
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    row = build_corpus.download_one(dict(held))
    assert requested == [held["url"]]
    assert row["sha256"] == build_corpus.sha256_bytes(b"%PDF-1.7 version one")

    # the re-harvested record (v2, other licence) is the same paper: not proposed again, and the
    # held row keeps its v1 URL and v1 licence
    for known in (({held["url"]}, set(), set()),
                  (set(), set(), set(), {held["persistent_id"]})):
        _, again, _ = _setup(monkeypatch, tmp_path, [_page([v2], "T")], known)
        _argv(monkeypatch, FRESH, "--pages", "1")
        find_arxiv.main()
        assert again == []
    # a paper not held at all is proposed at its current version with that version's licence
    _, fresh, _ = _setup(monkeypatch, tmp_path, [_page([v2], "T")])
    _argv(monkeypatch, FRESH, "--pages", "1")
    find_arxiv.main()
    assert fresh[0]["url"].endswith("2609.00001v2") and fresh[0]["license"] == "arxiv-nonexclusive"


@pytest.mark.parametrize("status,body", [
    (403, b"<html><title>Access Denied</title></html>"), (429, b"Rate exceeded."), (503, b"")])
def test_an_arxiv_refusal_opens_the_circuit_and_never_reaches_curl(monkeypatch, tmp_path,
                                                                    status, body):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]["User-Agent"]))
        return SimpleNamespace(
            status_code=status, content=body, headers={}, url=url,
            raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError(f"HTTP {status}")))

    monkeypatch.setattr(build_corpus.requests, "get", get)
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("a polite host must never reach curl"))
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})

    def row(aid):
        return {"id": f"arx-{aid}", "title": f"HVAC control {aid}", "source": "arxiv",
                "license": "arxiv-nonexclusive", "topic": "controls_bas", "format": "pdf",
                "url": f"https://arxiv.org/pdf/{aid}v1"}

    first = build_corpus.download_one(row("2609.00001"))
    second = build_corpus.download_one(row("2609.00002"))
    assert first["transient"] is True and "challenge" in first["error"]
    assert second["transient"] is True and "circuit open" in second["error"]
    assert calls == [("https://arxiv.org/pdf/2609.00001v1", build_corpus.HONEST_UA)]


def test_end_of_a_set_moves_to_the_next_and_the_last_set_starts_the_watch(monkeypatch, tmp_path):
    _, _, files = _setup(monkeypatch, tmp_path, [_page([HVAC]), _page([BRIDGE], "T2")])
    _argv(monkeypatch, FRESH, "--pages", "2")
    find_arxiv.main()
    assert files["next"].read_text() == "s=1 t=T2 f=2026-09-01,2026-09-02,-,-,-,- w=-\n"

    last = find_arxiv.ALL
    calls, _, files = _setup(monkeypatch, tmp_path, [_page([NCND])])
    _argv(monkeypatch, f"s={last} t=LAST f=2026-08-01,-,-,-,-,2026-08-30 w=-")
    find_arxiv.main()
    today = datetime.now(timezone.utc).date().isoformat()
    assert files["next"].read_text() == \
        f"s={last} t=START f=2026-08-01,-,-,-,-,2026-09-03 w={today}\n"
    assert not files["exhausted"].exists()


def test_watch_waits_then_harvests_all_of_arxiv_from_the_stored_datestamp(monkeypatch, tmp_path):
    last = find_arxiv.ALL
    today = datetime.now(timezone.utc).date()
    recent = f"s={last} t=START f=-,-,-,-,-,2026-09-03 w={today - timedelta(days=1)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    _argv(monkeypatch, recent)
    find_arxiv.main()
    assert calls == [] and files["next"].read_text() == recent + "\n"

    due = f"s={last} t=START f=-,-,-,-,-,2026-09-03 w={today - timedelta(days=find_arxiv.WATCH_DAYS)}"
    calls, appended, files = _setup(monkeypatch, tmp_path, [_page([DENSE])])
    _argv(monkeypatch, due)
    find_arxiv.main()
    assert calls[0] == {"verb": "ListRecords", "metadataPrefix": "arXivRaw", "from": "2026-09-03"}
    assert len(appended) == 1 and files["next"].read_text().endswith(f"w={today}\n")


def test_no_records_since_the_last_harvest_is_a_finished_set(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch"), _page([HVAC], "T")])
    _argv(monkeypatch, "s=1 t=START f=2026-09-01,2026-10-10,-,-,-,- w=-", "--pages", "2")
    find_arxiv.main()
    assert files["next"].read_text().startswith("s=2 t=T f=2026-09-01,2026-10-10,2026-09-01,")


def test_rejected_token_restarts_the_set_from_its_datestamp(monkeypatch, tmp_path):
    calls, appended, files = _setup(monkeypatch, tmp_path,
                                    [_error("badResumptionToken"), _page([BRIDGE], "NEW")])
    _argv(monkeypatch, "s=0 t=OLD f=2026-08-20,-,-,-,-,- w=-", "--pages", "2")
    find_arxiv.main()
    requests_made = [c for c in calls if isinstance(c, dict)]
    assert requests_made[1] == {"verb": "ListRecords", "metadataPrefix": "arXivRaw",
                                "set": "eess:eess:SY", "from": "2026-08-20"}
    assert len(appended) == 1 and files["next"].read_text().startswith("s=0 t=NEW f=2026-09-02,")


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=429, content=b"Rate exceeded."),
    SimpleNamespace(status_code=503, content=b""),
    b"<html>maintenance</html>",
    _error("badArgument"),
    _page([]),
    _error("badResumptionToken"),          # a fresh request cannot have a bad token
])
def test_unexpected_answers_hold_and_propose_nothing(monkeypatch, tmp_path, answer):
    _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    _argv(monkeypatch, FRESH)
    find_arxiv.main()
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_failure_after_a_complete_page_keeps_that_pages_progress(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path,
                                [_page([HVAC], "T1"), SimpleNamespace(status_code=429, content=b"")])
    _argv(monkeypatch, FRESH, "--pages", "3")
    find_arxiv.main()
    assert len(appended) == 1
    assert files["next"].read_text() == "s=0 t=T1 f=2026-09-01,-,-,-,-,- w=-\n"


def test_network_failure_on_the_first_page_exits_nonzero(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [requests.ConnectionError("down")])
    _argv(monkeypatch, FRESH)
    with pytest.raises(SystemExit) as exc:
        find_arxiv.main()
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()


def _broken(kind):
    good = _record("2609.07777", "HVAC control in office buildings", versions=2)
    return {
        "no id": good.replace("<id>2609.07777</id>", ""),
        "id mismatch": good.replace("<id>2609.07777</id>", "<id>2609.08888</id>"),
        "bad id": _record("not-an-id", "HVAC control in office buildings"),
        "no title": good.replace("<title>HVAC control in office buildings</title>", "<title/>"),
        "no versions": _record("2609.07777", "HVAC control in office buildings", versions=0),
        "bad latest version": good.replace('version="v2"', 'version="vX"'),
        "unnumbered version": good.replace('version="v2"', ""),
        "repeated version": good.replace('version="v2"', 'version="v1"'),
        "no metadata": good.replace("<metadata>", "<about>").replace("</metadata>", "</about>"),
        "impossible datestamp": good.replace("2026-09-01", "2026-02-30"),
        "datestamp with junk": good.replace("2026-09-01", "2026-09-01zz"),
        "no datestamp": good.replace("<datestamp>2026-09-01</datestamp>", ""),
    }[kind]


KINDS = ("no id", "id mismatch", "bad id", "no title", "no versions", "bad latest version",
         "unnumbered version", "repeated version", "no metadata", "impossible datestamp",
         "datestamp with junk", "no datestamp")


@pytest.mark.parametrize("kind", KINDS)
def test_one_malformed_record_rejects_the_whole_page(monkeypatch, tmp_path, kind):
    # first page: HOLD, the valid neighbours are not proposed, no cursor is emitted
    for records in ([HVAC, _broken(kind), BRIDGE], [_broken(kind)]):
        _, appended, files = _setup(monkeypatch, tmp_path, [_page(records, "T")])
        _argv(monkeypatch, FRESH, "--pages", "1")
        find_arxiv.main()
        assert appended == [] and files["hold"].exists() and not files["next"].exists(), kind
    # later page: progress only through the earlier complete page
    _, appended, files = _setup(monkeypatch, tmp_path,
                                [_page([HVAC], "T1"), _page([BRIDGE, _broken(kind)], "T2")])
    _argv(monkeypatch, FRESH, "--pages", "2")
    find_arxiv.main()
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["2609.07619v2"]
    assert files["next"].read_text() == "s=0 t=T1 f=2026-09-01,-,-,-,-,- w=-\n"
    find_arxiv.parse_cursor(files["next"].read_text())


def test_a_malformed_latest_version_never_yields_an_older_versions_url():
    cc_by = "http://creativecommons.org/licenses/by/4.0/"
    broken = _record("2609.07777", "HVAC control in office buildings", licence=cc_by,
                     versions=2).replace('version="v2"', 'version="vX"')
    with pytest.raises(find_arxiv.Unexpected):
        find_arxiv.parse_page(_page([broken]), first=True)


@pytest.mark.parametrize("answers,cursor", [
    ([_page([HVAC, MICROGRID], "TOK%3D1")], FRESH),
    ([_page([HVAC])], FRESH),
    ([_error("noRecordsMatch")], "s=1 t=START f=2026-09-01,2026-10-10,-,-,-,- w=-"),
    ([_page([NCND])], f"s={find_arxiv.ALL} t=LAST f=2026-08-01,-,-,-,-,2026-08-30 w=-"),
    ([_error("badResumptionToken"), _page([BRIDGE], "NEW")], "s=0 t=OLD f=2026-08-20,-,-,-,-,- w=-"),
    ([_page([_record("2609.9", "x", stamp="2026-09-09", deleted=True)], "T")], FRESH),
    ([], f"s={find_arxiv.ALL} t=START f=-,-,-,-,-,2026-09-03 w=2099-01-01"),
])
def test_every_emitted_cursor_parses_and_round_trips(monkeypatch, tmp_path, answers, cursor):
    _, _, files = _setup(monkeypatch, tmp_path, answers)
    _argv(monkeypatch, cursor, "--pages", str(max(1, len(answers))))
    find_arxiv.main()
    emitted =files["next"].read_text().strip()
    assert find_arxiv.format_cursor(*find_arxiv.parse_cursor(emitted)) == emitted


def test_deleted_records_count_for_the_datestamp_only():
    records, token, last = find_arxiv.parse_page(
        _page([HVAC, _record("2609.9", "x", stamp="2026-09-09", deleted=True)], "T"), first=True)
    assert [r["id"] for r in records] == ["2609.07619"] and token == "T" and last == "2026-09-09"


def test_cursor_round_trip_and_set_list_growth():
    text = "s=5 t=verb%3DListRecords%26skip%3D88 f=2026-09-01,-,2026-01-01,-,-,2026-10-01 w=2026-10-12"
    assert find_arxiv.format_cursor(*find_arxiv.parse_cursor(text)) == text
    assert find_arxiv.SETS[find_arxiv.ALL] is None and find_arxiv.SETS[0] == "eess:eess:SY"
    # a cursor written before a set was appended still parses (missing datestamps = beginning)
    s, token, froms, watched = find_arxiv.parse_cursor("s=0 t=START f=2026-09-01 w=-")
    assert froms == ["2026-09-01"] + [None] * (len(find_arxiv.SETS) - 1) and watched is None
    for bad in ("", "END", "8", "s=9 t=START f=- w=-", "s=0 t=START f=yesterday w=-",
                "s=0 f=- w=-", "s=0 t=START f=2026-99-99 w=-", "s=0 t=START f=2026-02-30 w=-",
                "s=0 t=START f=- w=2026-13-01", "s=0 t=START f=20260101 w=-",
                "s=0 t=START f=2026-W01-1 w=-", "s=0 t=START f=2026-1-1 w=-"):
        with pytest.raises(ValueError):
            find_arxiv.parse_cursor(bad)


def test_loader_honours_arxiv_robots_crawl_delay():
    assert "arxiv.org" in build_corpus.POLITE_HOSTS
    assert build_corpus.HOST_CONCURRENCY["arxiv.org"] == 1
    assert build_corpus.HOST_DELAY["arxiv.org"] >= 15.0      # robots.txt Crawl-delay: 15
    assert build_corpus.HOST_UA["arxiv.org"] == build_corpus.HONEST_UA
    assert build_corpus.HOST_RUN_CAP["arxiv.org"] * build_corpus.HOST_DELAY["arxiv.org"] <= 300
