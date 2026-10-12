import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import build_corpus
import find_ojs
import ojs_sites

BASE = "https://proceedings.open.tudelft.nl/clima2022"
BY = "https://creativecommons.org/licenses/by/4.0/"
SITE = find_ojs.SITES[0]


def _record(n, title, rights=(), relations=None, deleted=False, lang="eng", formats=(),
            subjects=()):
    if deleted:
        return (f'<record><header status="deleted"><identifier>oai:x:article/{n}</identifier>'
                f"</header></record>")
    relations = [f"{BASE}/article/view/{n}/{n + 10}"] if relations is None else relations
    rights_xml = "".join(f"<dc:rights>{r}</dc:rights>" for r in rights)
    rel_xml = "".join(f"<dc:relation>{r}</dc:relation>" for r in relations)
    fmt_xml = "".join(f"<dc:format>{f}</dc:format>" for f in formats)
    subj_xml = "".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
    return (f"<record><header><identifier>oai:x:article/{n}</identifier></header><metadata>"
            f'<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title xml:lang="en-US">{title}</dc:title><dc:date>2022-04-21</dc:date>'
            f"<dc:identifier>{BASE}/article/view/{n}</dc:identifier>"
            f"<dc:identifier>10.34641/clima.2022.{n}</dc:identifier>"
            f"<dc:creator>Raposo, João</dc:creator><dc:creator>Author, Ann</dc:creator>"
            f"<dc:source>Journal of Tests; Vol. 3 No. 1 (2022)</dc:source>"
            f"<dc:language>{lang}</dc:language>{rel_xml}{fmt_xml}{subj_xml}{rights_xml}"
            f"</oai_dc:dc></metadata></record>")


def _page(records, token=""):
    tok = f'<resumptionToken cursor="0" completeListSize="9">{token}</resumptionToken>'
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f"<ListRecords>{''.join(records)}{tok}</ListRecords></OAI-PMH>")


def _error(code):
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f'<error code="{code}">x</error></OAI-PMH>')


PAGE1 = _page([
    _record(1, "Deleted", deleted=True),
    _record(28, "From Diesel to Electric to NZEB in a Hotel",
            ["Copyright (c) 2022 J. Raposo", "https://creativecommons.org/licenses/by/4.0"]),
    _record(29, "Heat Pumps with R290", ["https://creativecommons.org/licenses/by-nc/4.0"]),
    _record(30, "Author Copyright Only", ["Copyright (c) 2022 A. Author"]),
], token="tok-2")
PAGE2 = _page([
    _record(31, "Radiant Ceiling Panels", ["https://creativecommons.org/licenses/by-sa/4.0/"]),
    _record(32, "No Galley Record", ["https://creativecommons.org/licenses/by/4.0"],
            relations=[]),
    _record(33, "Ventilation in Schools", ["https://creativecommons.org/publicdomain/zero/1.0/"],
            lang="nld"),
])


CREATORS = ["Raposo, João", "Author, Ann", "Autor, Álvaro"]
OTHERS = ["Revista de Obras; Vol. 2 Núm. 1 (2021)", "UNAL", "Revista X", "Revista Y", "Revista",
          "Verlag Z", "Jurnal Q", "CLIMA 2022 (14th REHVA HVAC World Congress)"]


@pytest.mark.parametrize(("line", "bare"), [
    ("Copyright (c) 2022 J. Raposo", True),                 # initial + family name
    ("Copyright (c) 2022 João Raposo", True),
    ("Copyright (c) 2022 Joao Raposo, Ann Author", True),   # accents, several authors
    ("© 2019-2021 A. Author and J. Raposo", True),
    ("Derechos de autor 2021 Álvaro Autor", True),
    ("Copyright 2022 Revista X", True),                     # the journal / publisher
    ("Copyright (c) 2021 Revista de Obras", True),
    ("Copyright 2022", True),                               # marker + year alone
    ("Copyright (c) 2017 The Author(s). Published by Verlag Z.", True),   # OJS press default
    ("Copyright (c) 2017 The Authors", True),
    ("Derechos de autor 2021 Los autores", True),
    ("Copyright (c) 2017 The Author(s). Published by Unknown House.", False),
    ("Copyright (c) 2017 The Author(s). Published by Verlag Z. No reuse.", False),
    ("Copyright 2022 Revista X and Verlag Z", False),       # two different `others`
    ("Copyright (c) 2022.", True),
    # review round 4: no wording test — a holder is a name from the record, or nothing
    ("Copyright 2022 A. Author. Reuse requires consent", False),
    ("Copyright 2022 A. Author, contact the publisher first", False),
    ("Copyright 2022 Somebody Else", False),
    ("Copyright 2022 A. Author. Reproduction prohibited", False),
    ("Copyright 2022 Revista X. Prohibida su reproducción", False),
    ("Copyright 2022 Author - personal use", False),
    ("Copyright 2022 A. Author https://example.org/terms", False),
    ("Copyright A. Author", False),                         # no year
    ("A. Author 2022", False),                              # no marker
    ("Free to read", False),
])
def test_copyright_line_is_bare_only_with_the_records_own_holder(line, bare):
    assert find_ojs.bare_copyright_line(line, CREATORS, OTHERS) is bare
    assert find_ojs.license_for([BY, line], CREATORS, OTHERS)[0] == (
        "cc-by" if bare else "unverified")
    nc = "https://creativecommons.org/licenses/by-nc/4.0/"
    assert find_ojs.license_for([nc, line], CREATORS, OTHERS)[0] == (
        "cc-by-nc" if bare else "unverified")


def test_a_holder_is_never_accepted_without_record_metadata():
    assert find_ojs.license_for([BY, "Copyright 2022 A. Author"])[0] == "unverified"
    assert find_ojs.license_for([BY, "Copyright 2022 The Author(s)"])[0] == "cc-by"
    assert find_ojs.license_for([BY, "Copyright 2022"])[0] == "cc-by"
    assert find_ojs.license_for([BY])[0] == "cc-by"


def test_entry_uses_the_records_creators_source_and_the_venue_name():
    def tag(line):
        records, _ = find_ojs.parse_page(_page([_record(5, "Heat pumps in schools", [BY, line])]))
        return find_ojs.entry_for(SITE, records[0], "2026-10-12")["license"]
    assert tag("Copyright (c) 2022 Ann Author, J. Raposo") == "cc-by"
    assert tag("Copyright (c) 2022 Journal of Tests") == "cc-by"            # dc:source
    assert tag("Copyright (c) 2022 CLIMA 2022") == "cc-by"                  # venue name
    assert tag("Copyright (c) 2022 Ann Author. Reuse requires consent") == "unverified"
    assert tag("Copyright (c) 2022 Another Person") == "unverified"


def _keys(urls=()):
    return find_ojs.dedup.from_sets(set(urls), set(), set())


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No test touches the network, robots.txt or the real resume scratch directory."""
    monkeypatch.setattr(find_ojs.time, "sleep", lambda _s: None)
    monkeypatch.setattr(find_ojs, "RESUME_DIR", tmp_path / "ojs-resume")
    monkeypatch.setattr(find_ojs, "_clock", {})
    monkeypatch.setattr(find_ojs, "_host_delay", {})
    monkeypatch.setattr(find_ojs, "POLICY", {})
    monkeypatch.setattr(find_ojs.robots_policy, "set_policy", lambda policy: None)
    monkeypatch.setattr(find_ojs.robots_policy, "set_pacer", lambda pacer: None)
    monkeypatch.setattr(find_ojs, "site_delay", lambda site, policy=None: 1.0)
    monkeypatch.setattr(find_ojs, "_policy", lambda required: {})
    monkeypatch.setattr(find_ojs, "fetch_page",
                        lambda *_a, **_k: pytest.fail("unexpected network request"))


# ------------------------------------------------------------------------------------ licence
@pytest.mark.parametrize(("rights", "expected"), [
    (["https://creativecommons.org/licenses/by/4.0"], "cc-by"),
    (["http://creativecommons.org/licenses/by-sa/3.0/"], "cc-by-sa"),
    (["https://creativecommons.org/publicdomain/zero/1.0/"], "cc0"),
    (["https://creativecommons.org/publicdomain/mark/1.0/"], "public-domain"),
    (["Copyright (c) 2022 J. Raposo", "https://creativecommons.org/licenses/by/4.0"], "cc-by"),
    (["http://www.creativecommons.org/licenses/by/4.0/legalcode"], "cc-by"),
    (["https://creativecommons.org/licenses/by/3.0/nl/"], "cc-by"),
    ([BY, "https://creativecommons.org/licenses/by-sa/4.0/"], "cc-by-sa"),
    # restricted Creative Commons licences keep their exact tag (collect, classify)
    (["https://creativecommons.org/licenses/by-nc/4.0"], "cc-by-nc"),
    (["https://creativecommons.org/licenses/by-nd/4.0"], "cc-by-nd"),
    (["https://creativecommons.org/licenses/by-nc-sa/4.0"], "cc-by-nc-sa"),
    (["http://creativecommons.org/licenses/by-nc-nd/3.0/es/"], "cc-by-nc-nd"),
    (["Copyright (c) 2022 A", "https://creativecommons.org/licenses/by-nc-nd/4.0/deed.es"],
     "cc-by-nc-nd"),
    # no verifiable single licence: unverified, never open
    (["Copyright (c) 2022 Author"], "unverified"),
    ([], "unverified"),
    (["https://example.org/creativecommons.org/licenses/by/4.0"], "unverified"),
    (["https://creativecommons.org.evil.example/licenses/by/4.0"], "unverified"),
    (["This article is not licensed under https://creativecommons.org/licenses/by/4.0"],
     "unverified"),
    ([BY, "https://creativecommons.org/licenses/by-nc/4.0/"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", BY], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "https://creativecommons.org/licenses/by-nd/4.0/"], "unverified"),
    ([BY, "Non-commercial use only"], "unverified"),
    ([BY, "All rights reserved"], "unverified"),
    ([BY, "Licensed under CC BY-like terms"], "unverified"),
    (["https://creativecommons.org/licenses/by/4.0/ and more"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", "Licensed under CC BY for reuse"],
     "unverified"),
    # a restricted licence URL beside anything but a plain copyright line fails closed
    (["https://creativecommons.org/licenses/by-nc/4.0/", "All rights reserved"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", "No derivatives allowed"],
     "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "Copyright (c) 2022 A. Author. All rights reserved"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc-sa/4.0/", "Todos los derechos reservados"],
     "unverified"),
    (["https://creativecommons.org/licenses/by-nc-nd/4.0/", "Free for any use"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", "Derechos de autor 2021 A. Autor"],
     "cc-by-nc"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", "© 2019-2021 Revista de Obras"],
     "cc-by-nc"),
    # review round 3: a copyright line may carry NOTHING but year and holder
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "Copyright 2026. Reproduction prohibited"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "Copyright (c) 2026 A. Author. Reproduction prohibited"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc-nd/4.0/",
      "Derechos de autor 2021 Revista X. Prohibida su reproducción"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc-sa/4.0/",
      "Direitos autorais 2020 Revista Y. Proibida a reprodução sem autorização"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "Copyright 2022 Verlag Z. Nachdruck verboten"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "Hak cipta 2022 Jurnal Q. Dilarang memperbanyak tanpa izin"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/",
      "Copyright 2022 A. Author. For personal use only"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", "Copyright A. Author"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc/4.0/", "A. Author 2022"], "unverified"),
    # ... and the same holds beside an OPEN licence URL
    ([BY, "Copyright 2026. Reproduction prohibited"], "unverified"),
    ([BY, "Copyright (c) 2026 A. Author. Commercial reuse requires permission"], "unverified"),
    ([BY, "Some statement about this article"], "unverified"),
    (["https://creativecommons.org/licenses/by-nc-sa/4.0/", "Direitos autorais 2020 Revista"],
     "cc-by-nc-sa"),
])
def test_licence_is_classified_per_record_and_fails_closed(rights, expected):
    tag, url = find_ojs.license_for(rights, CREATORS, OTHERS)
    assert tag == expected
    assert (url is None) == (tag == "unverified")
    assert find_ojs.registry.LICENSE_CLASSES[tag] != "open" or tag in (
        "cc-by", "cc-by-sa", "cc0", "public-domain")


def test_restricted_and_unverified_records_are_collected_with_evidence():
    records, _ = find_ojs.parse_page(PAGE1)
    nc = find_ojs.entry_for(SITE, records[1], "2026-10-12")
    bare = find_ojs.entry_for(SITE, records[2], "2026-10-12")
    assert nc["license"] == "cc-by-nc" and nc["license_url"].endswith("/by-nc/4.0")
    assert "verb=GetRecord" in nc["license_evidence"]
    assert bare["license"] == "unverified" and "license_url" not in bare
    assert "no single verifiable Creative Commons licence" in bare["license_evidence"]
    assert "Copyright (c) 2022 A. Author" in bare["license_evidence"]


# ------------------------------------------------------------------------------------ records
def test_galley_view_becomes_direct_download_of_the_same_journal_only():
    assert (find_ojs.galley_download(BASE, [f"{BASE}/article/view/28/38"])
            == f"{BASE}/article/download/28/38")
    assert (find_ojs.galley_download(BASE, [f"http://{BASE[8:]}/article/view/28/38"])
            == f"{BASE}/article/download/28/38")
    assert find_ojs.galley_download(BASE, [f"{BASE}/article/view/28"]) is None
    assert find_ojs.galley_download(BASE, ["https://other.org/j/article/view/1/2"]) is None
    assert find_ojs.galley_download(BASE, [f"https://evil.example/{BASE[8:]}/article/view/1/2"]) is None


def test_pdf_galley_is_chosen_when_formats_pair_with_galleys():
    rel = [f"{BASE}/article/view/5/50", f"{BASE}/article/view/5/51"]
    assert (find_ojs.galley_download(BASE, rel, ["text/html", "application/pdf"])
            == f"{BASE}/article/download/5/51")
    assert find_ojs.galley_download(BASE, rel, ["text/html", "text/xml"]) is None
    # formats that do not pair with the galleys decide nothing: first galley
    assert (find_ojs.galley_download(BASE, rel, ["application/pdf"])
            == f"{BASE}/article/download/5/50")


@pytest.mark.parametrize("title", ["Editorial", "EDITORIAL", "Front Matter", "Table of Contents",
                                   "Índice", "Sumário", "Expediente", "Daftar Isi", "Содержание",
                                   "Editorial Vol. 12 No. 3", "Cover", "Inhaltsverzeichnis"])
def test_front_matter_is_not_an_article(title):
    records, _ = find_ojs.parse_page(_page([_record(7, title, [BY])]))
    assert find_ojs.entry_for(SITE, records[0], "2026-10-12") is None


def test_titles_that_only_start_like_front_matter_are_articles():
    for title in ("Editorial policies for concrete durability research",
                  "Index of thermal comfort in school buildings"):
        records, _ = find_ojs.parse_page(_page([_record(7, title, [BY])]))
        assert find_ojs.entry_for(SITE, records[0], "2026-10-12") is not None


def test_gated_venue_keeps_only_built_environment_records():
    gated = dict(SITE, gate=True)
    records, _ = find_ojs.parse_page(_page([
        _record(1, "Seismic retrofit of masonry school buildings", [BY]),
        _record(2, "A fuzzy controller for a DC motor drive", [BY]),
        _record(3, "Optimización de mezclas", [BY], subjects=["hormigón", "cemento"]),
        _record(4, "Deep learning for speech recognition", [BY], subjects=["neural networks"]),
    ]))
    kept = [r["title"] for r in records if find_ojs.entry_for(gated, r, "2026-10-12")]
    assert kept == ["Seismic retrofit of masonry school buildings", "Optimización de mezclas"]
    assert all(find_ojs.entry_for(SITE, r, "2026-10-12") for r in records)  # ungated: all


@pytest.mark.parametrize("title", [
    "Protein structure prediction with deep networks",
    "Thermal degradation of edible oils",
    "Crystal structure of a zinc complex",
    "Thermal analysis of a lithium battery pack",
    "Software architecture for IoT platforms",
    "Arquitectura de software para sistemas de información",
    "Construction of a fuzzy controller for a DC motor",
    "Stainless steel corrosion in chloride media",
    "Soil microbial communities under maize",
    "Capacity building for rural teachers",
    "Building a model of customer churn",
    "Energy efficiency of induction motors",
    "Network traffic classification with random forests",
    "Laser beam welding of titanium alloys",
    "Distillation column control using MPC",
    "Assessment of ice cream quality sold in Duhok City",
    "Evolución microestructural del acero sometido a tratamiento térmico de temple",
    "El caso concreto de la economía colombiana",
    "Cambios estructurales en series de tiempo",
    "Foundations of quantum computing",
])
def test_gate_rejects_ambiguous_stems_without_built_environment_context(title):
    assert not find_ojs.ojs_gate.relevant(title)


@pytest.mark.parametrize("title", [
    "Structural analysis of a steel frame under wind load",
    "Thermal comfort in naturally ventilated classrooms",
    "Thermal insulation of external walls",
    "Seismic performance of RC shear walls",
    "Soil stabilization with lime and fly ash",
    "Bearing capacity of pile foundations in clay",
    "Delay factors in construction projects in Ghana",
    "Affordable housing policy in Lagos",
    "Urban heat island and street canyon geometry",
    "Contemporary mosque architecture in Baghdad",
    "Traffic flow at signalized intersections",
    "Flood risk mapping of the lower basin",
    "Comportamiento sísmico de pórticos de concreto reforzado",
    "Análisis estructural de un puente atirantado",
    "Estabilización de suelos arcillosos con cal",
    "Desempenho térmico de habitações de interesse social",
    "Resistência à compressão de argamassas com resíduos",
    "Analisis struktur beton bertulang gedung bertingkat",
    "Kinerja simpang bersinyal di Kota Semarang",
    "Nośność konstrukcji żelbetowych budynków mieszkalnych",
    "Прочность железобетонных конструкций зданий",
    "Вогнестійкість залізобетонних конструкцій будівель",
    "Verhalten von Mauerwerk unter Erdbebenlast",
    "Comportement du béton armé sous charge cyclique",
    "Il restauro delle murature storiche",
])
def test_gate_keeps_multilingual_built_environment_titles(title):
    assert find_ojs.ojs_gate.relevant(title)


def test_non_latin_title_gets_an_article_based_id():
    records, _ = find_ojs.parse_page(_page([_record(7, "Архитектура и строительство", [BY],
                                                    lang="rus")]))
    entry = find_ojs.entry_for(SITE, records[0], "2026-10-12")
    assert entry["id"] == "ojs-clima2022-article-7-17" and entry["language"] == "ru"


def test_control_characters_in_abstracts_do_not_fail_the_page():
    page = PAGE2.replace("Radiant Ceiling Panels", "Radiant step-by\x02step Panels")
    records, token = find_ojs.parse_page(page)
    assert records[0]["title"] == "Radiant step-by step Panels" and token == ""


@pytest.mark.parametrize("body", [
    "<html><head><title>Maintenance</title></head><body><p>Back soon</p></body></html>",
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    '<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
    "<Identify/></OAI-PMH>",
    "Service Unavailable",
    _error("badArgument"),
])
def test_non_oai_responses_are_errors_not_empty_complete_lists(body):
    with pytest.raises(find_ojs.UnexpectedResponse):
        find_ojs.parse_page(body)


def test_no_records_match_is_an_empty_complete_list_and_bad_token_is_its_own_error():
    assert find_ojs.parse_page(_error("noRecordsMatch")) == ([], "")
    with pytest.raises(find_ojs.BadToken):
        find_ojs.parse_page(_error("badResumptionToken"))


# ------------------------------------------------------------------------------------ harvest
def _mock_pages(monkeypatch, pages):
    """pages: list of bodies in call order, or {token-or-'': body}. Returns the call log."""
    calls = []

    def fetch(base, token, since=""):
        calls.append((token, since))
        body = pages[token] if isinstance(pages, dict) else pages[len(calls) - 1]
        if isinstance(body, Exception):
            raise body
        return body

    monkeypatch.setattr(find_ojs, "fetch_page", fetch)
    return calls


def test_harvest_pages_classifies_and_dedups(monkeypatch):
    calls = _mock_pages(monkeypatch, [PAGE1, PAGE2])
    keys = _keys({f"{BASE}/article/download/31/41"})  # already registered

    out, scanned, rest = find_ojs.harvest(SITE, keys, maxn=100)

    assert calls == [("", ""), ("tok-2", "")]
    assert rest == "" and scanned == 6  # deleted records are not scanned
    assert [(e["title"], e["license"]) for e in out] == [
        ("From Diesel to Electric to NZEB in a Hotel", "cc-by"),
        ("Heat Pumps with R290", "cc-by-nc"),
        ("Author Copyright Only", "unverified"),
        ("Ventilation in Schools", "cc0")]
    first = out[0]
    assert first["id"] == "ojs-clima2022-from-diesel-to-electric-to-nzeb-in-a-hotel"
    assert first["url"] == f"{BASE}/article/download/28/38" and first["format"] == "pdf"
    assert first["license_url"] == "https://creativecommons.org/licenses/by/4.0"
    assert first["persistent_id"] == "https://doi.org/10.34641/clima.2022.28"
    assert first["language"] == "en" and first["published_at"] == "2022-04-21"
    assert out[3]["language"] == "nl"
    assert find_ojs.registry.shard_filename(first["id"]) == "ojs.yaml"


def test_harvest_dedups_by_doi_identity(monkeypatch):
    _mock_pages(monkeypatch, [PAGE2])
    keys = find_ojs.dedup.from_sets(set(), set(), set(), {"doi:10.34641/clima.2022.33"})
    out, _, _ = find_ojs.harvest(SITE, keys, maxn=100)
    assert [e["title"] for e in out] == ["Radiant Ceiling Panels"]


def test_harvest_reserves_nothing_and_dedups_inside_one_walk(monkeypatch):
    _mock_pages(monkeypatch, [PAGE1, _page([_record(28, "From Diesel to Electric to NZEB in a Hotel",
                                                    [BY])])])
    keys = _keys()
    out, _, _ = find_ojs.harvest(SITE, keys, maxn=100)
    assert len(out) == 3                                    # the repeat inside the walk is dropped
    assert out[0]["url"] not in keys.urls                   # nothing reserved by harvest
    find_ojs.reserve(keys, out)
    assert out[0]["url"] in keys.urls
    assert find_ojs.registry.norm(out[0]["title"]) in keys.titles


def test_token_rejected_after_a_successful_page_loses_no_record(monkeypatch):
    """Review regression: a resumed walk reads a page, then its next token is rejected. The
    retry from the first page must return EVERY record (the failed walk reserved nothing) and
    only then may the venue be complete."""
    resumed = _page([_record(31, "Radiant Ceiling Panels",
                             ["https://creativecommons.org/licenses/by-sa/4.0/"])], token="tok-3")
    find_ojs.save_resume(SITE, "tok-2")
    calls = _mock_pages(monkeypatch, {"tok-2": resumed, "tok-3": _error("badResumptionToken"),
                                      "": _page([_record(28, "Hotel NZEB", [BY])], token="tok-b"),
                                      "tok-b": PAGE2})
    keys = _keys()
    out, scanned, complete = find_ojs.visit(SITE, keys, 100, {})
    assert [t for t, _ in calls] == ["tok-2", "tok-3", "", "tok-b"]
    assert [e["title"] for e in out] == ["Hotel NZEB", "Radiant Ceiling Panels",
                                         "Ventilation in Schools"]
    assert complete and find_ojs.load_resume(SITE) == ""
    assert all(e["url"] in keys.urls for e in out)          # reserved once, after success


def test_failed_walk_leaves_the_dedup_keys_untouched(monkeypatch):
    _mock_pages(monkeypatch, [PAGE1, TimeoutError("offline")])
    keys = _keys()
    with pytest.raises(TimeoutError):
        find_ojs.visit(SITE, keys, 100, {})
    assert f"{BASE}/article/download/28/38" not in keys.urls
    _mock_pages(monkeypatch, [PAGE1, PAGE2])
    out, _, complete = find_ojs.visit(SITE, keys, 100, {})
    assert len(out) == 5 and complete


def test_capped_walk_returns_the_next_token_and_since_is_sent(monkeypatch):
    calls = _mock_pages(monkeypatch, [PAGE1, PAGE2])
    out, _, rest = find_ojs.harvest(SITE, _keys(), maxn=1, since="2026-09-23")
    assert rest == "tok-2" and len(out) == 3 and calls == [("", "2026-09-23")]


def test_repeated_resumption_token_is_a_paging_loop(monkeypatch):
    _mock_pages(monkeypatch, [PAGE1, PAGE1])
    with pytest.raises(find_ojs.UnexpectedResponse, match="repeated resumptionToken"):
        find_ojs.harvest(SITE, _keys(), maxn=100)


def test_only_a_walk_from_the_first_page_proves_completion(monkeypatch):
    calls = _mock_pages(monkeypatch, {"": PAGE1, "tok-2": PAGE2})
    # visit 1: capped on page 1 -> token saved, not complete
    out, _, complete = find_ojs.visit(SITE, _keys(), 1, {})
    assert len(out) == 3 and not complete and find_ojs.load_resume(SITE) == "tok-2"
    # visit 2: resumes from the token, reaches the end -> token cleared, STILL not complete
    out, _, complete = find_ojs.visit(SITE, _keys(), 100, {})
    assert [e["title"] for e in out] == ["Radiant Ceiling Panels", "Ventilation in Schools"]
    assert not complete and find_ojs.load_resume(SITE) == ""
    # visit 3: a full walk from the first page -> complete
    _, _, complete = find_ojs.visit(SITE, _keys(), 100, {})
    assert complete
    assert [t for t, _ in calls] == ["", "tok-2", "", "tok-2"]


def test_expired_token_restarts_the_walk(monkeypatch):
    find_ojs.save_resume(SITE, "old-token")
    calls = _mock_pages(monkeypatch, {"old-token": _error("badResumptionToken"), "": PAGE1,
                                      "tok-2": PAGE2})
    out, _, complete = find_ojs.visit(SITE, _keys(), 100, {})
    assert [t for t, _ in calls] == ["old-token", "", "tok-2"]
    assert len(out) == 5 and complete and find_ojs.load_resume(SITE) == ""


def test_stale_resume_scratch_is_ignored(tmp_path):
    find_ojs.save_resume(SITE, "tok")
    path = find_ojs.RESUME_DIR / f"{SITE['key']}.json"
    rec = json.loads(path.read_text())
    rec["saved_at"] -= find_ojs.RESUME_TTL + 1
    path.write_text(json.dumps(rec))
    assert find_ojs.load_resume(SITE) == ""
    path.write_text("not json")
    assert find_ojs.load_resume(SITE) == ""


def test_watch_visit_never_uses_or_writes_the_resume_token(monkeypatch):
    find_ojs.save_resume(SITE, "backfill-token")
    calls = _mock_pages(monkeypatch, [PAGE2])
    _, _, complete = find_ojs.visit(SITE, _keys(), 100, {}, since="2026-10-01")
    assert calls == [("", "2026-10-01")] and complete
    assert find_ojs.load_resume(SITE) == "backfill-token"


# ------------------------------------------------------------------------------------ access
def _real_access(monkeypatch, decision):
    """Undo the autouse stubs of site_delay / fetch_page; robots answers come from `decision`."""
    monkeypatch.undo()
    monkeypatch.setattr(find_ojs, "_clock", {})
    monkeypatch.setattr(find_ojs, "_host_delay", {})
    monkeypatch.setattr(find_ojs, "POLICY", {})
    monkeypatch.setattr(find_ojs.robots_policy, "decision", decision)
    monkeypatch.setattr(find_ojs.robots_policy, "ai_training_refused", lambda url: None)


def test_site_delay_refuses_suspended_hosts_and_robots_disallow(monkeypatch):
    _real_access(monkeypatch, lambda url: (True, 5.0))
    assert find_ojs.site_delay(SITE, {}) == 5.0
    assert find_ojs.site_delay(dict(SITE, delay=8), {}) == 8.0
    suspended = {"open.tudelft.nl": {"status": "suspended", "reason": "x"}}
    with pytest.raises(find_ojs.Skipped, match="fetch-suspended"):
        find_ojs.site_delay(SITE, suspended)
    monkeypatch.setattr(find_ojs.robots_policy, "decision",
                        lambda url: ("/article/download/" not in url, None))
    with pytest.raises(find_ojs.Skipped, match="disallows"):
        find_ojs.site_delay(SITE, {})
    monkeypatch.setattr(find_ojs, "_host_delay", {})
    monkeypatch.setattr(find_ojs.robots_policy, "decision", lambda url: (True, 60.0))
    with pytest.raises(find_ojs.Skipped, match="Crawl-delay"):
        find_ojs.site_delay(SITE, {})

    def unavailable(url):
        raise find_ojs.robots_policy.RobotsUnavailable("HTTP 403")
    monkeypatch.setattr(find_ojs.robots_policy, "decision", unavailable)
    with pytest.raises(find_ojs.Skipped, match="robots.txt unavailable"):
        find_ojs.site_delay(SITE, {})


ROBOTS_AI = {
    "gptbot": "User-agent: *\nDisallow: /cache/\n\nUser-agent: GPTBot\nDisallow: /\n",
    "claudebot": "User-agent: ClaudeBot\nUser-agent: Claude-Web\nDisallow: /\n",
    "anthropic": "User-agent: anthropic-ai\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
    "ccbot": "User-agent: CCBot\nDisallow: /\n",
    "google-extended": "User-agent: Google-Extended\nDisallow: /\n",
    "perplexity": "User-agent: PerplexityBot\nDisallow: /\n",
    "img2dataset": "User-agent: img2dataset\nDisallow: /\n",
    "signal": "User-agent: *\nContent-Signal: search=yes, ai-train=no\nAllow: /\n",
    "partial": "User-agent: GPTBot\nDisallow: /index.php/\n",
    # merged groups of one agent still refuse; another agent's Allow does not help
    "merged-no": "User-agent: GPTBot\nAllow: /public/\n\nUser-agent: GPTBot\nDisallow: /\n",
    "other-allow": "User-agent: GPTBot\nDisallow: /\n\nUser-agent: CCBot\nAllow: /\n",
}
ROBOTS_OPEN = {
    # several groups for one AI agent are one group: the longer Allow wins for /index.php/
    "merged": ("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /cache/\n\n"
               "User-agent: GPTBot\nAllow: /index.php/\n"),
    "plain": "User-agent: *\nDisallow: /cache/\n",
    "signal-yes": "User-agent: *\nContent-signal: search=yes, ai-train=yes\nAllow: /\n",
    "ai-elsewhere": "User-agent: GPTBot\nDisallow: /private/\n",
    "ai-allowed": "User-agent: GPTBot\nAllow: /\n",
    "comment": "# Content-Signal: ai-train=no is NOT set here\nUser-agent: *\nAllow: /\n",
    "other-bot": "User-agent: AhrefsBot\nDisallow: /\n",
}
DOC = "https://press.example/index.php/j/article/download/5/9"


@pytest.mark.parametrize("name", sorted(ROBOTS_AI))
def test_ai_training_opt_outs_are_recognised(name):
    assert find_ojs.robots_policy.ai_opt_out(ROBOTS_AI[name], DOC)


@pytest.mark.parametrize("name", sorted(ROBOTS_OPEN))
def test_robots_without_an_ai_opt_out_for_the_url_pass(name):
    assert find_ojs.robots_policy.ai_opt_out(ROBOTS_OPEN[name], DOC) is None


@pytest.fixture
def live_robots(monkeypatch, tmp_path):
    """The real robots_policy with a cold cache; robots.txt bodies come from `texts[origin]`."""
    import robots_policy
    monkeypatch.undo()
    monkeypatch.setattr(robots_policy, "CACHE_DIR", tmp_path / "robots-cache")
    monkeypatch.setattr(robots_policy, "POLICY", {})
    monkeypatch.setattr(robots_policy, "_pacer", None)
    monkeypatch.setattr(robots_policy, "_hop_filter", None)
    robots_policy.clear_memory()
    texts = {}
    monkeypatch.setattr(robots_policy, "_get_manual",
                        lambda url: (200, texts[robots_policy.origin(url)].encode()))
    for name in ("_clock", "_host_delay", "POLICY"):
        monkeypatch.setattr(find_ojs, name, {})
    monkeypatch.setattr(find_ojs.time, "sleep", lambda s: None)
    yield texts
    robots_policy.clear_memory()


@pytest.mark.parametrize("name", ["gptbot", "claudebot", "signal"])
def test_finder_skips_a_venue_whose_robots_opts_out_of_ai_training(live_robots, name):
    """Review regression: allowed for `*`, but the host tells the AI crawlers to stay out."""
    live_robots["https://press.example"] = ROBOTS_AI[name]
    site = dict(SITE, base="https://press.example/index.php/j")
    with pytest.raises(find_ojs.Skipped, match="AI-training opt-out"):
        find_ojs.site_delay(site, {})
    live_robots["https://open.example"] = ROBOTS_OPEN["plain"]
    assert find_ojs.site_delay(dict(SITE, base="https://open.example/index.php/j"), {}) == 1.0


def test_finder_refuses_a_redirect_to_a_host_that_opts_out(monkeypatch, live_robots):
    live_robots["https://open.example"] = ROBOTS_OPEN["plain"]
    live_robots["https://cdn.example"] = ROBOTS_AI["ccbot"]
    requested = []
    monkeypatch.setattr(find_ojs.requests, "get", lambda url, **kw: (
        requested.append(url), _Resp(302, location="https://cdn.example/oai"))[1])
    with pytest.raises(find_ojs.Skipped, match="AI-training opt-out"):
        find_ojs.fetch_page("https://open.example/index.php/j", "")
    assert len(requested) == 1                    # the opted-out host was never requested


@pytest.mark.parametrize("name", ["gptbot", "anthropic", "signal"])
def test_loader_defers_ojs_rows_on_an_ai_training_opt_out(monkeypatch, live_robots, name):
    monkeypatch.setattr(build_corpus, "HOST_POLICY", {})
    monkeypatch.setattr(build_corpus, "_robots_down", {})
    live_robots["https://press.example"] = ROBOTS_AI[name]
    live_robots["https://open.example"] = ROBOTS_OPEN["plain"]
    hops = build_corpus.Hops(DOC, False, robots_checked=True, sid="ojs-j-x")
    with pytest.raises(build_corpus.ChallengeRefused, match="AI-training opt-out") as exc:
        build_corpus.check_hop(DOC, hops)
    assert exc.value.requested is False and hops.chain == []
    # the start URL is fine, a redirect hop to an opted-out host is deferred as well
    start = "https://open.example/index.php/j/article/download/5/9"
    hops = build_corpus.Hops(start, False, robots_checked=True, sid="ojs-j-x")
    build_corpus.check_hop(start, hops)
    with pytest.raises(build_corpus.ChallengeRefused, match="AI-training opt-out"):
        build_corpus.check_hop("https://press.example/files/5.pdf", hops)
    assert hops.chain == [start]


def test_vendor_and_programme_rows_do_not_use_the_ai_opt_out_rule(monkeypatch):
    """The manufacturer exception and the compliance programme keep their reviewed rules."""
    import robots_policy
    monkeypatch.setattr(build_corpus, "HOST_POLICY", {})
    monkeypatch.setattr(robots_policy, "ai_training_refused",
                        lambda url: pytest.fail("not consulted"))
    assert not build_corpus.is_robots_checked_row("vnd-carrier-some-manual")
    url = "https://www.vendor.example/manual.pdf"
    build_corpus.check_hop(url, build_corpus.Hops(url, False, sid="vnd-carrier-some-manual"))
    monkeypatch.setattr(robots_policy, "decision", lambda u: (True, None))
    monkeypatch.setattr(build_corpus.compliance_common, "reviewed_host", lambda u: True)
    build_corpus.check_hop(url, build_corpus.Hops(url, False, robots=True, sid="reg-x"))


def test_site_delay_checks_both_the_oai_and_the_download_path(monkeypatch):
    seen = []
    _real_access(monkeypatch, lambda url: (seen.append(url), (True, None))[1])
    assert find_ojs.site_delay(SITE, {}) == find_ojs.DELAY
    assert sorted(seen) == sorted([f"{BASE}/oai?verb=ListRecords&metadataPrefix=oai_dc",
                                   f"{BASE}/article/download/1/1"])


class _Clock:
    """A fake monotonic clock that sleep() advances; records every sleep."""

    def __init__(self, monkeypatch):
        self.now, self.sleeps = 1000.0, []
        monkeypatch.setattr(find_ojs.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(find_ojs.time, "sleep", self.sleep)

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


class _Resp:
    def __init__(self, status=200, text="", location=None):
        self.status_code, self.text = status, text
        self.headers = {"location": location} if location else {}


def test_two_venues_on_one_host_share_the_request_clock(monkeypatch):
    """Review regression: two single-page visits of venues on one host with Crawl-delay 10
    must be 10 s apart, although neither walk has a second page."""
    _real_access(monkeypatch, lambda url: (True, 10.0))
    clock = _Clock(monkeypatch)
    requested = []
    monkeypatch.setattr(find_ojs.requests, "get",
                        lambda url, **kw: (requested.append((url, clock.now)), _Resp(200, PAGE2))[1])
    one = dict(SITE, key="one", base="https://portal.example.org/index.php/one")
    two = dict(SITE, key="two", base="https://portal.example.org/index.php/two")
    other = dict(SITE, key="other", base="https://other.example.org/index.php/x")
    for site in (one, two, other):
        find_ojs.visit(site, _keys(), 100, {})
    starts = [t for _, t in requested]
    assert starts[1] - starts[0] >= 10.0          # same host: paced across venues
    assert starts[2] - starts[1] < 10.0           # another host has its own clock
    assert clock.sleeps == [10.0]


def test_robots_requests_take_a_slot_of_the_same_host_clock(monkeypatch):
    _real_access(monkeypatch, lambda url: (True, 4.0))
    clock = _Clock(monkeypatch)
    with find_ojs._robots_pace("https://portal.example.org/robots.txt"):
        pass
    monkeypatch.setattr(find_ojs.requests, "get", lambda url, **kw: _Resp(200, PAGE2))
    find_ojs.fetch_page("https://portal.example.org/index.php/one", "")
    assert clock.sleeps == [4.0]


def test_redirect_hops_are_checked_and_paced(monkeypatch):
    checked = []
    _real_access(monkeypatch, lambda url: (checked.append(url), ("blocked" not in url, 3.0))[1])
    clock = _Clock(monkeypatch)
    answers = iter([_Resp(301, location="https://portal.example.org/ojs/one/oai?verb=x"),
                    _Resp(200, PAGE2)])
    requested = []
    monkeypatch.setattr(find_ojs.requests, "get",
                        lambda url, **kw: (requested.append((url, kw["allow_redirects"])),
                                           next(answers))[1])
    find_ojs.fetch_page("https://portal.example.org/index.php/one", "")
    assert [u for u, _ in requested] == [
        "https://portal.example.org/index.php/one/oai?verb=ListRecords&metadataPrefix=oai_dc",
        "https://portal.example.org/ojs/one/oai?verb=x"]
    assert not any(follow for _, follow in requested) and checked == [u for u, _ in requested]
    assert clock.sleeps == [3.0]                  # the redirect hop waited for the host clock
    # a redirect to a disallowed path is refused before it is requested
    answers = iter([_Resp(302, location="/blocked/oai")])
    with pytest.raises(find_ojs.Skipped, match="disallows"):
        find_ojs.fetch_page("https://portal.example.org/index.php/one", "")
    # ... and so is a redirect to a fetch-suspended host
    monkeypatch.setattr(find_ojs, "POLICY", {"walled.example": {"status": "suspended"}})
    answers = iter([_Resp(302, location="https://cdn.walled.example/oai")])
    with pytest.raises(find_ojs.Skipped, match="fetch-suspended"):
        find_ojs.fetch_page("https://portal.example.org/index.php/one", "")


# ------------------------------------------------------------------------------------ cursor
N = len(find_ojs.SITES)


def test_cursor_round_trip_and_padding():
    state = find_ojs.parse_cursor("f=2026-09-23 s=2026-10-12 i=0 a=8 d=11111111")
    assert state["d"][:8] == list("11111111") and state["d"][8:] == ["0"] * (N - 8)
    assert find_ojs.parse_cursor(find_ojs.format_cursor(state)) == state
    assert len(find_ojs.format_cursor(state)) < 4096  # finder_protocol.MAX_CURSOR


@pytest.mark.parametrize("cursor", ["", "9", "f=x s=2026-10-12 i=0 a=0 d=1",
                                    "f=2026-13-01 s=2026-10-12 i=0 a=0 d=1",
                                    "f=2026-09-23 s=2026-10-12 i=0 a=0 d=12",
                                    "f=2026-09-23 s=2026-10-12 i=0 a=0 d=" + "1" * (N + 1)])
def test_malformed_cursors_are_rejected(cursor):
    with pytest.raises(ValueError):
        find_ojs.parse_cursor(cursor)


def test_plan_round_robins_the_backfill_and_walks_the_watch_cycle():
    state = find_ojs.parse_cursor("f=2026-09-23 s=2026-10-12 i=6 a=10 d=11111111")
    backfill, watch = find_ojs.plan(state, 3, date(2026, 10, 12))
    assert backfill == [10, 11, 12] and watch == [6, 7, 8]
    state["a"] = N - 1
    backfill, _ = find_ojs.plan(state, 3, date(2026, 10, 12))
    assert backfill == [N - 1, 8, 9]  # wraps to the first pending venue


def test_watch_cycle_idles_then_restarts_with_an_overlapping_from_date():
    done = "1" * N
    state = find_ojs.parse_cursor(f"f=2026-09-23 s=2026-10-12 i={N} a=0 d={done}")
    assert find_ojs.plan(state, 3, date(2026, 10, 18)) == ([], [])
    assert (state["f"], state["s"], state["i"]) == ("2026-09-23", "2026-10-12", N)
    backfill, watch = find_ojs.plan(state, 3, date(2026, 10, 19))
    assert backfill == [] and watch == [0, 1, 2]
    assert (state["f"], state["s"], state["i"]) == ("2026-10-11", "2026-10-19", 0)


# ------------------------------------------------------------------------------------ main
def _run_main(monkeypatch, tmp_path, argv, visits):
    """visits: {site index: result or Exception}; a result is (entries, scanned, complete)."""
    seen = []

    def visit(site, keys, maxn, policy, *, since=""):
        k = find_ojs.SITES.index(site)
        seen.append((k, since))
        result = visits[k]
        if isinstance(result, Exception):
            raise result
        return result

    appended = []
    monkeypatch.setattr(find_ojs, "visit", visit)
    monkeypatch.setattr(find_ojs.dedup, "open_keys", lambda: _keys())
    monkeypatch.setattr(find_ojs.registry, "append_entries",
                        lambda entries: appended.extend(entries) or {})
    files = {name: tmp_path / name for name in ("next", "hold", "exhausted")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setattr(sys, "argv", ["find_ojs.py", *argv])
    find_ojs.main()
    assert not files["hold"].exists() and not files["exhausted"].exists()  # never exhausted
    return seen, appended, find_ojs.parse_cursor(files["next"].read_text().strip())


def _entry(n):
    return {"id": f"ojs-x-{n}", "title": f"Paper {n}", "url": f"https://x.org/{n}",
            "source": "ojs_x", "license": "cc-by", "topic": "urban", "format": "pdf"}


def test_main_backfills_round_robin_and_isolates_a_failing_venue(monkeypatch, tmp_path, capsys):
    cursor = f"f=2026-09-23 s={date.today()} i={N} a=8 d=11111111"
    visits = {8: ([_entry(1)], 10, True), 9: TimeoutError("offline"),
              10: ([_entry(2), _entry(3)], 100, False)}
    seen, appended, state = _run_main(monkeypatch, tmp_path,
                                      ["--cursor", cursor, "--sites", "3", "--append"], visits)
    assert seen == [(8, ""), (9, ""), (10, "")]
    assert [e["id"] for e in appended] == ["ojs-x-1", "ojs-x-2", "ojs-x-3"]
    assert state["d"][8:11] == ["1", "0", "0"] and state["a"] == 11 and state["i"] == N
    out = capsys.readouterr().out
    assert "failed, TimeoutError: offline" in out and "capped, venue not finished" in out


def test_main_watch_cycle_reads_complete_venues_since_f(monkeypatch, tmp_path):
    done = "1" * N
    cursor = f"f=2026-09-23 s=2026-10-01 i={N} a=0 d={done}"
    visits = {0: ([_entry(1)], 3, True), 1: find_ojs.UnexpectedResponse("login page"),
              2: ([], 0, True)}
    seen, appended, state = _run_main(monkeypatch, tmp_path,
                                      ["--cursor", cursor, "--sites", "3", "--append"], visits)
    assert seen == [(0, "2026-09-30"), (1, "2026-09-30"), (2, "2026-09-30")]
    assert len(appended) == 1
    assert (state["f"], state["s"], state["i"]) == ("2026-09-30", str(date.today()), 3)
    assert state["d"][:3] == ["1", "0", "1"]  # the failed venue goes back to the backfill


def test_main_capped_watch_venue_is_read_again_next_run(monkeypatch, tmp_path):
    done = "1" * N
    cursor = f"f=2026-09-23 s={date.today()} i=4 a=0 d={done}"
    visits = {4: ([], 5, True), 5: ([_entry(1)], 100, False)}  # venue 6 is not reached
    seen, _, state = _run_main(monkeypatch, tmp_path,
                               ["--cursor", cursor, "--sites", "3", "--append"], visits)
    assert seen == [(4, "2026-09-23"), (5, "2026-09-23")] and state["i"] == 5


def test_main_idle_cursor_makes_no_request_and_keeps_the_cursor(monkeypatch, tmp_path):
    done = "1" * N
    cursor = f"f=2026-09-23 s={date.today()} i={N} a=0 d={done}"
    seen, appended, state = _run_main(monkeypatch, tmp_path, ["--cursor", cursor, "--append"], {})
    assert seen == [] and appended == [] and find_ojs.format_cursor(state) == cursor


def test_main_dry_run_never_appends(monkeypatch, tmp_path):
    cursor = f"f=2026-09-23 s={date.today()} i={N} a=8 d=11111111"
    visits = {8: ([_entry(1)], 10, True)}
    _, appended, _ = _run_main(monkeypatch, tmp_path, ["--cursor", cursor, "--sites", "1"],
                               visits)
    assert appended == []


def test_main_rejects_a_malformed_cursor(monkeypatch):
    monkeypatch.setattr(find_ojs.dedup, "open_keys", lambda: _keys())
    monkeypatch.setattr(sys, "argv", ["find_ojs.py", "--cursor", "9"])
    with pytest.raises(SystemExit) as exc:
        find_ojs.main()
    assert exc.value.code == 2


# ------------------------------------------------------------------------------------ venues
ROOT = Path(__file__).resolve().parents[1]
TOPICS = {"controls_bas", "equipment_systems", "building_energy", "commissioning_fdd",
          "standards_protocols", "structures_civil", "construction", "materials", "architecture",
          "infrastructure", "urban", "simulation_modeling"}


def test_venue_list_is_append_only_unique_and_well_formed():
    keys = [s["key"] for s in find_ojs.SITES]
    assert keys[:8] == ["clima2022", "jfde", "coastlab24", "jchs", "jcrfr", "jdu",
                        "writingplace", "abe"]  # the cursor's bits index this list
    assert len(keys) == len(set(keys)) and len(keys) >= 100
    bases = [s["base"] for s in find_ojs.SITES]
    assert len(bases) == len(set(bases))
    for site in find_ojs.SITES:
        assert re.fullmatch(r"[a-z0-9]{2,18}", site["key"]), site["key"]
        assert re.fullmatch(r"https?://[^/\s]+(/[^\s?#]*[^/\s?#])?", site["base"]), site["base"]
        assert site["topic"] in TOPICS and site["type"] in (
            "journal-article", "conference-paper", "thesis")
        assert site["source"] and site["name"]
        # the id keeps room for a title slug inside the 63-character budget
        assert len(f"ojs-{site['key']}-") <= 24
    for site in find_ojs.SITES[8:]:
        assert site["source"] == f"ojs_{site['key']}"


def test_no_venue_is_on_a_no_go_host():
    # (a host suspended later in registry/host_policy.json is skipped at run time: site_delay)
    for site in find_ojs.SITES:
        assert not re.search(r"scielo|jstage|ajol\.info", urlsplit(site["base"]).netloc)


def test_loader_treats_reviewed_ojs_hosts_as_polite():
    assert len(ojs_sites.HOSTS) >= 80
    for host in ojs_sites.HOSTS:
        assert host in build_corpus.POLITE_HOSTS
        assert build_corpus.HOST_CONCURRENCY[host] == 1
        assert build_corpus.HOST_DELAY[host] >= max(1.0, ojs_sites.HOST_DELAYS[host])
        assert not build_corpus.HOST_UA[host].startswith("Mozilla")
        assert build_corpus.HOST_RUN_CAP[host] <= build_corpus.OJS_RUN_CAP
    for site in ojs_sites.SITES:
        if site.get("delay"):
            host = urlsplit(site["base"]).netloc.lower()
            assert build_corpus.HOST_DELAY[host] >= site["delay"]


def test_loader_checks_robots_for_every_hop_of_an_ojs_row(monkeypatch):
    """Review regression: the finder's synthetic download check authorizes nothing; the loader
    reads robots.txt for the actual URL and every redirect hop of an ojs- row."""
    import robots_policy
    assert build_corpus.is_robots_checked_row("ojs-jfde-some-paper")
    assert not build_corpus.is_robots_checked_row("osm-some-report")
    monkeypatch.setattr(build_corpus, "HOST_POLICY", {})
    monkeypatch.setattr(build_corpus, "_robots_down", {})
    url = "https://jfde.eu/index.php/jfde/article/download/7/9"
    asked = []

    def decision(u):
        asked.append(u)
        return "/cdn/" not in u, 7.0
    monkeypatch.setattr(robots_policy, "decision", decision)
    monkeypatch.setattr(robots_policy, "ai_training_refused", lambda u: None)
    monkeypatch.setitem(build_corpus.HOST_DELAY, "jfde.eu", 1.0)
    hops = build_corpus.Hops(url, False, robots_checked=True, sid="ojs-jfde-x")
    build_corpus.check_hop(url, hops)
    assert asked == [url] and hops.chain == [url]
    assert build_corpus.HOST_DELAY["jfde.eu"] == 7.0            # dynamic Crawl-delay
    # a redirect hop on ANY host is checked; Disallow defers (transient, never requested)
    with pytest.raises(build_corpus.ChallengeRefused, match="disallows") as exc:
        build_corpus.check_hop("https://files.example.net/cdn/7.pdf", hops)
    assert exc.value.requested is False and len(hops.chain) == 1

    def unavailable(u):
        raise robots_policy.RobotsUnavailable("HTTP 403")
    monkeypatch.setattr(robots_policy, "decision", unavailable)
    with pytest.raises(build_corpus.ChallengeRefused, match="unavailable") as exc:
        build_corpus.check_hop(url, hops)
    assert exc.value.requested is False
    build_corpus._robots_down.clear()  # a new run
    monkeypatch.setattr(robots_policy, "decision", lambda u: (True, 60.0))
    with pytest.raises(build_corpus.ChallengeRefused, match="Crawl-delay") as exc:
        build_corpus.check_hop(url, hops)
    assert exc.value.requested is False
    # rows of other finders are not robots-checked by this path, and the programme's
    # reviewed-host allowlist does not apply to ojs- rows
    monkeypatch.setattr(robots_policy, "decision", lambda u: pytest.fail("not consulted"))
    build_corpus.check_hop(url, build_corpus.Hops(url, False, sid="osm-x"))
    assert not build_corpus.Hops(url, False, robots_checked=True).robots


@pytest.fixture
def loader_robots(monkeypatch, tmp_path):
    """The loader's real robots path with a cold cache and a counted robots.txt transport."""
    import robots_policy
    monkeypatch.setattr(build_corpus, "HOST_POLICY", {})
    monkeypatch.setattr(build_corpus, "_robots_down", {})
    monkeypatch.setattr(build_corpus, "_robots_down_locks", {})
    monkeypatch.setattr(build_corpus, "_host_sems", {})
    monkeypatch.setattr(build_corpus, "_wait_for_host", lambda host: None)
    for table in (build_corpus.HOST_CONCURRENCY, build_corpus.HOST_DELAY):
        monkeypatch.setattr(build_corpus, {id(build_corpus.HOST_CONCURRENCY): "HOST_CONCURRENCY",
                                           id(build_corpus.HOST_DELAY): "HOST_DELAY"}[id(table)],
                            dict(table))
    monkeypatch.setattr(robots_policy, "CACHE_DIR", tmp_path / "robots-cache")
    robots_policy.clear_memory()
    monkeypatch.setattr(robots_policy, "POLICY", {})
    monkeypatch.setattr(robots_policy, "_pacer", build_corpus._robots_pace)
    monkeypatch.setattr(robots_policy, "_hop_filter", build_corpus._robots_hop_allowed)
    calls = []
    yield robots_policy, calls
    robots_policy.clear_memory()


def test_unavailable_robots_is_asked_once_per_origin_and_run(monkeypatch, loader_robots):
    """Review regression (transport level): robots.txt answering 403 is requested ONCE; every
    later ojs- row of that origin defers without another request, and the document is never
    requested."""
    robots_policy, calls = loader_robots

    def get_manual(url):
        calls.append(url)
        return 403, b""
    monkeypatch.setattr(robots_policy, "_get_manual", get_manual)
    monkeypatch.setattr(build_corpus.requests, "get",
                        lambda *a, **k: pytest.fail("the document must not be requested"))
    monkeypatch.setattr(build_corpus, "_fetch_with_fallback",
                        lambda url, fmt, rec, *a: build_corpus._get_hops(url, fmt).content)
    recs = [build_corpus.download_one(
        {"id": f"ojs-x-paper-{n}", "url": f"https://press.example/j/article/download/{n}/1",
         "source": "ojs_x", "format": "pdf", "license": "cc-by", "topic": "urban",
         "title": f"Paper {n}"}) for n in range(3)]
    assert calls == ["https://press.example/robots.txt"]
    for rec in recs:
        assert "robots.txt unavailable" in rec["error"] and rec["transient"]
        assert rec["_not_requested"] is True
    # another origin is still asked
    build_corpus.download_one(
        {"id": "ojs-y-paper", "url": "https://other.example/j/article/download/1/1",
         "source": "ojs_y", "format": "pdf", "license": "cc-by", "topic": "urban", "title": "P"})
    assert calls[-1] == "https://other.example/robots.txt" and len(calls) == 2


def test_cold_robots_lookup_keeps_unconfigured_hosts_serial(monkeypatch, loader_robots):
    """Review regression: the robots.txt lookup creates the host semaphore; the serial defaults
    must already be installed, also for a robots.txt redirect host."""
    robots_policy, calls = loader_robots
    limits = {}

    class Resp:
        def __init__(self, status, location=None):
            self.status_code, self.headers = status, ({"location": location} if location else {})

        def close(self):
            pass

    def get(url, **kw):
        host = build_corpus.host_policy.canonical_host(url)
        limits[host] = build_corpus.HOST_CONCURRENCY.get(host, build_corpus.PER_HOST)
        if host == "fresh.example":
            return Resp(301, "https://www.fresh.example/robots.txt")
        return Resp(404)
    monkeypatch.setattr(robots_policy.requests, "get", get)
    url = "https://fresh.example/j/article/download/1/1"
    hops = build_corpus.Hops(url, False, robots_checked=True, sid="ojs-x-1")
    monkeypatch.setattr(build_corpus._hops, "value", hops, raising=False)
    build_corpus.check_hop(url, hops)
    assert limits == {"fresh.example": 1, "www.fresh.example": 1}
    for host in ("fresh.example", "www.fresh.example"):
        sem = build_corpus._host_sems[host]
        assert sem.acquire(blocking=False) and not sem.acquire(blocking=False)
        sem.release()
        assert build_corpus.HOST_DELAY[host] >= build_corpus.PACED_DELAY


def test_robots_requests_of_an_ojs_row_may_reach_any_unsuspended_host(monkeypatch):
    url = "https://small-press.example/robots.txt"
    monkeypatch.setattr(build_corpus._hops, "value", None, raising=False)
    assert not build_corpus._robots_hop_allowed(url)            # programme rule: reviewed hosts
    monkeypatch.setattr(build_corpus._hops, "value",
                        build_corpus.Hops(url, False, robots_checked=True), raising=False)
    assert build_corpus._robots_hop_allowed(url)


def test_rotation_and_backend_configuration_match_the_finder():
    rotation = json.loads((ROOT / "registry" / "rotation.json").read_text())["find_ojs"]
    backend = json.loads((ROOT / "registry" / "backends.json").read_text())["find_ojs"]
    # LIVE configuration: structure and bounds only. Rounds move the cursor (and run this
    # test afterwards); operators may pause or re-tune the backend.
    assert rotation["flag"] == "--cursor" and rotation["dynamic"] is True
    state = find_ojs.parse_cursor(rotation["next"])       # well-formed, bits within SITES
    assert len(state["d"]) == len(find_ojs.SITES) and state["a"] >= 0 and state["i"] >= 0
    assert "--site" not in backend["args"] and backend["script"] == "find_ojs.py"


def test_initial_cursor_backfills_every_venue_with_no_bit_set():
    """Every venue starts with bit 0, the first 8 too: the old finder kept CC BY/BY-SA/CC0
    records only, so they are backfilled again under the classify-all licence policy."""
    state = find_ojs.parse_cursor(find_ojs.initial_cursor(date(2026, 10, 12)))
    assert "1" not in state["d"] and state["a"] == 0 and state["i"] == len(find_ojs.SITES)
    assert (state["f"], state["s"]) == ("2026-10-12", "2026-10-12")
    assert find_ojs.plan(state, 3, date(2026, 10, 12)) == ([0, 1, 2], [])
