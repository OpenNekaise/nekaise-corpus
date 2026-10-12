import copy
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

import pytest
import requests

import build_corpus
import check_contracts
import find_repo
import lint_registry
import registry
import store

REPO = Path(__file__).resolve().parents[1]
TODAY = "2026-10-12"
CFG = {
    "name": "Example Tech Repository", "country": "SE", "languages": ["en", "sv", "de"],
    "source": "repo_example", "platform": "dspace-xoai",
    "oai": "https://repo.example.edu/oai/request", "hosts": ["repo.example.edu"],
    "fulltext_pattern": r"^https://repo\.example\.edu/bitstreams/[0-9a-f-]{36}/download$",
    "sets": ["com_civil", "col_all"], "all_sets": ["com_civil"],
    "relevant_fields": {"dc.contributor.department": "Architecture and Civil Engineering"},
    "open_access": {"field": "dc.rights.access", "pattern": "^openAccess$"},
    "crawl_delay": 4, "topic": "structures_civil", "enabled": True,
    "rights": {"robots": "User-agent: * no Disallow for /oai or /bitstreams",
               "terms_url": "https://repo.example.edu/terms",
               "terms_excerpt": "Metadata and full texts may be harvested.",
               "reviewed_at": "2026-10-12", "decision": "go"},
}
UUID = "0a1b2c3d-0000-4000-8000-{:012d}"


def _fields(path_values):
    """{"dc.title": ["x"], "dc.rights.uri": [...]} -> nested xoai <element> XML."""
    tree: dict = {}
    for path, values in path_values.items():
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})
        node.setdefault("none", {}).setdefault("__values__", []).extend(values)

    def render(name, node):
        inner = "".join(f'<field name="value">{escape(v)}</field>'
                        for v in node.get("__values__", []))
        inner += "".join(render(k, v) for k, v in node.items() if k != "__values__")
        return f'<element name="{name}">{inner}</element>'

    return "".join(render(k, v) for k, v in tree.items())


def record(n, title, *, kind="Master Thesis", pdf=True, deleted=False, bundle="ORIGINAL",
           fmt="application/pdf", access="openAccess", host="repo.example.edu", lang="eng",
           **extra):
    if deleted:
        return (f'<record><header status="deleted"><identifier>oai:x:{n}</identifier>'
                "<datestamp>2026-01-01</datestamp></header></record>")
    fields = {"dc.title": [title], "dc.type": [kind], **({"dc.language.iso": [lang]} if lang else {}),
              "dc.date.issued": ["2024-05-02"],
              "dc.identifier.uri": [f"http://hdl.handle.net/20.500.1/{n}"]}
    if access:
        fields["dc.rights.access"] = [access]
    for key, value in extra.items():
        fields[key.replace("__", ".")] = value if isinstance(value, list) else [value]
    bundles = ""
    if pdf:
        url = f"https://{host}/bitstreams/{UUID.format(n)}/download"
        bundles = ('<element name="bundles"><element name="bundle">'
                   f'<field name="name">{bundle}</field><element name="bitstreams">'
                   '<element name="bitstream"><field name="name">thesis.pdf</field>'
                   f'<field name="format">{fmt}</field><field name="url">{url}</field>'
                   "</element></element></element>"
                   '<element name="bundle"><field name="name">LICENSE</field>'
                   '<element name="bitstreams"><element name="bitstream">'
                   '<field name="format">text/plain</field>'
                   f'<field name="url">https://{host}/bitstreams/{UUID.format(n + 500)}/download'
                   "</field></element></element></element></element>")
    return (f"<record><header><identifier>oai:x:{n}</identifier>"
            "<datestamp>2026-01-01</datestamp></header><metadata>"
            f'<metadata xmlns="http://www.lyncode.com/xoai">{_fields(fields)}{bundles}'
            "</metadata></metadata></record>")


def page(records, token=""):
    resumption = f"<resumptionToken>{token}</resumptionToken>" if token is not None else ""
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f"<ListRecords>{''.join(records)}{resumption}</ListRecords></OAI-PMH>").encode()


def error(code):
    return ('<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f'<error code="{code}">x</error></OAI-PMH>').encode()


def one(xml_record, cfg=CFG, setspec="col_all"):
    records, _ = find_repo.parse_page(page([xml_record]), has_token=False)
    return find_repo.candidate(records[0], "example", cfg, TODAY, setspec) if records else None


# --------------------------------------------------------------------------------------------
# configuration
def test_live_config_is_valid_and_every_enabled_repository_is_vetted():
    data = json.loads((REPO / "registry" / "repositories.json").read_text())
    assert find_repo.validate(data) == []
    repos = data["repositories"]
    enabled = {k: c for k, c in repos.items() if c["enabled"]}
    assert len(enabled) >= 3
    for key, cfg in enabled.items():
        assert cfg["rights"]["decision"] == "go" and cfg["crawl_delay"] >= find_repo.MIN_DELAY
        assert "robots.txt" in cfg["rights"]["robots"]
        assert __import__("coverage").genre_of(cfg["source"]) == "research_papers", key
    assert all(row["status"] in ("later", "rejected") and row["reason"]
               for row in data["backlog"])


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.update(platform="eprints"), "platform"),
    (lambda c: c.update(oai="http://repo.example.edu/oai"), "https"),
    (lambda c: c.update(fulltext_pattern=r"^https://evil\.example/.*$"), "fulltext_pattern"),
    (lambda c: c.update(fulltext_pattern="("), "fulltext_pattern"),
    (lambda c: c.update(crawl_delay=0.5), "crawl_delay"),
    (lambda c: c.update(sets=[]), "sets"),
    (lambda c: c.update(all_sets=["nope"]), "all_sets"),
    (lambda c: c.update(languages=["xx"]), "languages"),
    (lambda c: c.update(source="example"), "source"),
    (lambda c: c.update(relevant_fields={"dc.x": "("}), "relevant_fields"),
    (lambda c: c.update(open_access={"field": "dc.rights"}), "open_access"),
    (lambda c: c["rights"].update(decision="later"), "decision 'go'"),
    (lambda c: c["rights"].pop("terms_excerpt"), "terms_excerpt"),
    (lambda c: c["rights"].update(reviewed_at="yesterday"), "reviewed_at"),
    (lambda c: c.pop("enabled"), "enabled"),
])
def test_validate_rejects_bad_repository_configs(mutate, needle):
    cfg = copy.deepcopy(CFG)
    mutate(cfg)
    errors = find_repo.validate({"repositories": {"example": cfg}})
    assert errors and any(needle in e for e in errors), errors


def test_validate_top_level_keys_sources_and_backlog():
    assert find_repo.validate([]) and find_repo.validate({"repositories": []})
    assert find_repo.validate({"repositories": {"Bad Key": copy.deepcopy(CFG)}})
    twice = {"repositories": {"a": copy.deepcopy(CFG), "b": copy.deepcopy(CFG)}}
    assert any("also used" in e for e in find_repo.validate(twice))
    assert find_repo.validate({"repositories": {}, "backlog": [{"name": "x", "status": "maybe",
                                                                 "reason": "y"}]})
    assert find_repo.validate({"repositories": {"example": copy.deepcopy(CFG)},
                               "backlog": [{"name": "x", "status": "rejected",
                                            "reason": "robots"}]}) == []


def test_contracts_validate_the_pinned_config_and_the_store_pins_it():
    assert "repositories.json" in store.CONFIG_FILES
    good = SimpleNamespace(documents={"repositories.json": {"repositories": {"example": CFG}}})
    backends = {"_readme": "x", "find_repo": {"script": "find_repo.py", "args": []}}
    assert check_contracts.repository_contract_errors(good, backends) == []
    bad = copy.deepcopy(CFG)
    bad["crawl_delay"] = 0
    broken = SimpleNamespace(documents={"repositories.json": {"repositories": {"example": bad}}})
    assert check_contracts.repository_contract_errors(broken, backends)
    missing = SimpleNamespace(documents={})
    assert check_contracts.repository_contract_errors(missing, backends)
    # a checkout that does not configure the vein needs no repository config
    assert check_contracts.repository_contract_errors(missing, {"_readme": "x"}) == []
    assert check_contracts.repository_contract_errors(broken, {"_readme": "x"})


def test_loader_hosts_are_polite_serial_paced_and_capped(monkeypatch):
    disabled = dict(copy.deepcopy(CFG), enabled=False, hosts=["off.example.edu"], crawl_delay=9)
    hosts = find_repo.loader_hosts({"example": CFG, "off": disabled})
    # a disabled repository keeps its protections: its registered rows are still fetched
    assert hosts == {"repo.example.edu": {"delay": 4.0, "cap": 67},
                     "off.example.edu": {"delay": 9.0, "cap": 30}}
    monkeypatch.setattr(build_corpus, "REPOSITORY_HOSTS", set())
    for table in ("HOST_CONCURRENCY", "HOST_DELAY", "HOST_UA", "HOST_RUN_CAP"):
        monkeypatch.setattr(build_corpus, table, dict(getattr(build_corpus, table)))
    monkeypatch.setattr(build_corpus, "POLITE_HOSTS", build_corpus.POLITE_HOSTS)
    build_corpus.HOST_DELAY["repo.example.edu"] = 9.0     # a stricter existing value wins
    build_corpus.apply_repository_hosts(hosts)
    assert build_corpus.HOST_CONCURRENCY["repo.example.edu"] == 1
    assert build_corpus.HOST_DELAY["repo.example.edu"] == 9.0
    assert build_corpus.HOST_UA["repo.example.edu"] == build_corpus.HONEST_UA
    assert build_corpus.HOST_RUN_CAP["repo.example.edu"] == 67
    assert "repo.example.edu" in build_corpus.POLITE_HOSTS
    assert build_corpus.REPOSITORY_HOSTS == {"repo.example.edu", "off.example.edu"}
    assert build_corpus.HOST_CONCURRENCY["off.example.edu"] == 1
    assert "off.example.edu" in build_corpus.POLITE_HOSTS


def test_rep_rows_are_deferred_without_a_usable_policy_configuration(monkeypatch):
    url = "https://repo.example.edu/bitstreams/x/download"
    monkeypatch.setattr(build_corpus, "REPOSITORY_HOSTS", {"repo.example.edu"})
    monkeypatch.setattr(build_corpus, "REPOSITORY_CONFIG_ERROR", "")
    assert build_corpus.repository_deferral("rep-example-x", url) is None
    assert build_corpus.repository_deferral("ost-x", "https://elsewhere.example/x.pdf") is None
    assert "not a host" in build_corpus.repository_deferral(
        "rep-example-x", "https://elsewhere.example/x.pdf")
    monkeypatch.setattr(build_corpus, "REPOSITORY_CONFIG_ERROR", "ValueError: invalid")
    assert "unavailable" in build_corpus.repository_deferral("rep-example-x", url)

    requested = []
    monkeypatch.setattr(build_corpus, "_get_hops", lambda *a, **k: requested.append(a))
    rec = build_corpus.download_one({"id": "rep-example-x", "url": url, "source": "repo_example",
                                     "license": "unverified", "topic": "materials",
                                     "format": "pdf", "title": "x"})
    assert "unavailable" in rec["_deferred"] and requested == [] and not rec.get("error")


def test_every_configured_live_host_incl_disabled_is_protected():
    repos = find_repo.load_config()
    assert any(not cfg["enabled"] for cfg in repos.values())
    assert not build_corpus.REPOSITORY_CONFIG_ERROR
    for cfg in repos.values():
        for host in cfg["hosts"]:
            assert host in build_corpus.REPOSITORY_HOSTS and host in build_corpus.POLITE_HOSTS
            assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA


def test_every_enabled_live_host_is_a_polite_loader_host():
    for host, limits in find_repo.loader_hosts(find_repo.load_config()).items():
        assert host in build_corpus.POLITE_HOSTS
        assert build_corpus.HOST_CONCURRENCY[host] == 1
        assert build_corpus.HOST_DELAY[host] >= limits["delay"] >= find_repo.MIN_DELAY
        assert not build_corpus.HOST_UA[host].startswith("Mozilla")
        assert build_corpus.HOST_RUN_CAP[host] * limits["delay"] <= find_repo.RUN_SECONDS


# --------------------------------------------------------------------------------------------
# xoai records
def test_candidate_fields_shard_and_lint():
    entry = one(record(1, "Seismic design of reinforced concrete frames &apos;revisited&apos;",
                       dc__rights__uri="https://creativecommons.org/licenses/by/4.0/",
                       dc__subject=["earthquake engineering", "RC frames"]))
    assert entry["url"] == f"https://repo.example.edu/bitstreams/{UUID.format(1)}/download"
    assert entry["title"] == "Seismic design of reinforced concrete frames 'revisited'"
    assert entry["license"] == "cc-by"
    assert entry["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert entry["source"] == "repo_example" and entry["language"] == "en"
    assert entry["document_type"] == "thesis" and entry["published_at"] == "2024-05-02"
    assert entry["persistent_id"] == "hdl:20.500.1/1" and entry["topic"] == "materials"
    assert entry["topic"] in lint_registry.TOPICS and entry["license"] in lint_registry.LICENSES
    assert registry.shard_filename(entry["id"]) == "repositories.yaml"
    assert not lint_registry.entry_errors(entry, "repositories.yaml")


def test_records_without_an_open_original_pdf_are_dropped():
    title = "Timber structures for multi-storey buildings"
    assert one(record(1, title))
    assert one(record(2, title, pdf=False)) is None
    assert one(record(3, title, bundle="TEXT")) is None
    assert one(record(4, title, fmt="application/zip")) is None
    assert one(record(5, title, host="cdn.elsewhere.org")) is None       # foreign host
    assert one(record(6, title, deleted=True)) is None
    assert one(record(7, title, access="restrictedAccess")) is None
    assert one(record(8, title, access="embargoedAccess (2 Years)")) is None
    assert one(record(9, title, access=None)) is None                     # open value required
    assert one(record(10, title, dc__date__embargo="2031-01-01")) is None
    assert one(record(11, title, dc__date__embargo="2020-01-01"))
    assert one(record(12, title, dc__date__embargo="until further notice")) is None
    assert one(record(13, title, dc__rights="info:eu-repo/semantics/closedAccess")) is None
    assert one(record(15, title, dc__rights="http://purl.org/coar/access_right/c_16ec")) is None
    opened = one(record(16, title, dc__rights="http://purl.org/coar/access_right/c_abf2"))
    assert opened and "coar" not in opened["license_evidence"]
    auburn = dict(CFG, open_access={"field": "dc.embargo.status", "pattern": "^NOT_EMBARGOED$"},
                  ignore_fields=["dc.embargo.length"])
    assert one(record(17, title, access=None, dc__embargo__status="NOT_EMBARGOED",
                      dc__embargo__length="MONTHS_WITHHELD:12",
                      dc__embargo__enddate="2026-08-06"), auburn)
    assert one(record(18, title, access=None, dc__embargo__status="EMBARGOED",
                      dc__embargo__enddate="2031-08-05"), auburn) is None
    assert one(record(19, title, access=None, dc__embargo__status="NOT_EMBARGOED",
                      dc__embargo__length="MONTHS_WITHHELD:12")) is None   # field not reviewed
    # an abstract that talks about restricted access is not an access statement
    assert one(record(14, title, dc__description__abstract="We study restricted access roads."))


def test_document_types():
    title = "Timber structures for multi-storey buildings"
    for kind in ("Doctoral Thesis", "Examensarbete för masterexamen", "Report", "masterThesis",
                 "Dissertação (Mestrado)", "info:eu-repo/semantics/doctoralThesis", "Bericht"):
        assert one(record(1, title, kind=kind)), kind
    for kind in ("Dataset", "Image", "Journal Article", "Presentation", "Article", "Other"):
        assert one(record(1, title, kind=kind)) is None, kind
    wide = dict(CFG, types="thesis|article", skip_types="dataset")
    assert one(record(1, title, kind="Article"), wide)["document_type"] == "journal-article"
    dedicated = dict(CFG, types="*")
    assert one(record(1, title, kind="Ph.D."), dedicated)["document_type"] == "thesis"
    assert one(record(1, title, kind="Dataset"), dedicated) is None


@pytest.mark.parametrize("title,lang_ok", [
    ("Geotechnical analysis of embankments", True),
    ("Reinforced concrete bridge decks under fatigue loading", True),
    ("Ground source heat pumps for residential buildings", True),
    ("Geotechnical analysis of slope stability in soft clay", True),
    ("Urban planning and housing policy in Gothenburg", True),
    ("A deep learning approach to protein folding", False),
    ("Quantum error correction on superconducting circuits", False),
    ("Supply chain centralisation in healthcare", False),
    ("Building a compiler for a functional language", False),
    ("Building trust in open source communities", False),
    ("Building energy simulation of a school", True),
    ("Building envelope retrofit strategies", True),
    ("Structural batteries for electric vehicles", False),
    ("Concrete examples of bridge financing in venture capital", False),
])
def test_relevance_gate_in_several_languages(title, lang_ok):
    assert bool(one(record(1, title))) is lang_ok, title


def test_gate_languages_come_from_the_repository_config():
    finnish = "Betonirakenteiden korjausrakentaminen"
    portuguese = "Reabilitação de edifícios de alvenaria"
    english = dict(CFG, languages=["en"])
    assert one(record(1, finnish), english) is None
    assert one(record(1, portuguese), english) is None
    assert one(record(1, finnish, lang="fin"), dict(CFG, languages=["fi"]))
    assert one(record(1, portuguese, lang="por"), dict(CFG, languages=["pt"]))
    assert one(record(1, portuguese, lang=None), dict(CFG, languages=["pt"]))   # unknown: all
    for title, lang in (("Bemessung von Stahlbetonbauteilen unter Brandbeanspruchung", "ger"),
                        ("Tragwerksplanung im Holzbau", "de"),
                        ("Energieffektivisering av flerbostadshus med värmepump", "swe")):
        assert one(record(1, title, lang=lang))["language"] in ("de", "sv")
    # a stated language selects that language's terms only
    assert one(record(1, "Parallel algorithms on multi-core architecture", lang="eng"),
               dict(CFG, languages=["en", "fr"])) is None
    assert one(record(1, "Architecture et urbanisme à Lyon", lang="fra"),
               dict(CFG, languages=["en", "fr"]))


def test_dedicated_sets_departments_and_dewey_classes_pass_without_a_term():
    plain = "Majnabbe"
    assert one(record(1, plain)) is None
    assert one(record(1, plain), setspec="com_civil")
    assert one(record(1, plain, dc__contributor__department=(
        "Chalmers / Department of Architecture and Civil Engineering (ACE)")))
    assert one(record(1, plain, dc__subject__ddc="600 | Technik::624 | Ingenieurbau"))
    assert one(record(1, plain, dc__subject__ddc="ddc:720"))
    assert one(record(1, plain, dc__subject__ddc="620 | Ingenieurwissenschaften")) is None
    assert one(record(1, plain, dc__subject__ddc="004 | Informatik; 1624 items")) is None
    # soils and microbes are civil engineering in a dedicated set, not elsewhere
    soil = "Soil rock interfaces: sealing strategies"
    assert one(record(1, soil), setspec="com_civil") and one(record(1, soil)) is None
    # a subject with no building reading never passes, even in a dedicated set
    assert one(record(1, "Protein crystal structure of a concrete enzyme"),
               setspec="com_civil") is None


@pytest.mark.parametrize("rights,tag", [
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/"}, "cc-by"),
    ({"dc__rights__uri": "http://creativecommons.org/licenses/by-sa/4.0/"}, "cc-by-sa"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights__license": "CC BY 4.0 International"}, "cc-by"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights__license": "CC BY-NC 4.0 International"}, "unverified"),   # conflict
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights": "No reuse without permission"}, "unverified"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by-nc-nd/4.0/"}, "cc-by-nc-nd"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by-nc/4.0/",
      "dc__rights__license": "CC BY-NC 4.0 International"}, "cc-by-nc"),
    ({"dc__rights__uri": ["https://creativecommons.org/licenses/by-nc/4.0/",
                          "https://creativecommons.org/licenses/by/4.0/"]}, "unverified"),
    ({"dc__rights__uri": "http://rightsstatements.org/page/InC-NC/1.0/",
      "dc__rights__license": "In Copyright - Non-Commercial Use Permitted"}, "unverified"),
    ({"dc__rights": "All rights reserved"}, "unverified"),
    ({"dc__rights": "Creative Commons Attribution 4.0"}, "unverified"),      # prose only
    # Codex round 1: negated, contradictory or unrecognised prose beside a canonical URL
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights": "This work is not licensed under Creative Commons"}, "unverified"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights": "Creative Commons Attribution. All rights reserved."}, "unverified"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights": "CC BY applies to the text only; figures are excluded"}, "unverified"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by/4.0/",
      "dc__rights": "Attribution-ShareAlike 4.0 International"}, "unverified"),  # other licence
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
      "dc__rights": "Creative Commons, but all rights reserved"}, "unverified"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
      "dc__rights": "Attribution 4.0 International"}, "unverified"),
    ({"dc__rights__uri": "http://creativecommons.org/licenses/by/3.0/de/",
      "dc__rights__license": ["Attribution 3.0 Germany", "CC BY 3.0 DE"]}, "cc-by"),
    ({"dc__rights__uri": ["https://creativecommons.org/licenses/by/4.0/",
                          "https://creativecommons.org/licenses/by/4.0"]}, "cc-by"),
    ({"dc__rights__uri": "https://creativecommons.org/licenses/by-nc-nd/4.0",
      "dc__rights": "Attribution-NonCommercial-NoDerivatives 4.0 International"},
     "cc-by-nc-nd"),
    ({"dc__rights__uri": "http://creativecommons.org/licenses/by-nc-nd/3.0/us/",
      "dc__rights": "Attribution-NonCommercial-NoDerivs 3.0 United States"}, "cc-by-nc-nd"),
    ({}, "unverified"),
])
def test_licence_classes_fail_closed(rights, tag):
    entry = one(record(1, "Timber structures for multi-storey buildings", **rights))
    assert entry["license"] == tag and entry["license"] in lint_registry.LICENSES
    assert ("license_url" in entry) == (tag != "unverified")
    if rights:
        first = next(iter(rights.values()))
        first = first[0] if isinstance(first, list) else first
        assert first[:12] in entry["license_evidence"]
    else:
        assert "states no rights" in entry["license_evidence"]
    assert "openAccess" not in entry["license_evidence"]


def test_parse_page_outcomes():
    recs, token = find_repo.parse_page(page([record(1, "a"), record(2, "b", deleted=True)],
                                            "xoai///set/100"), has_token=False)
    assert len(recs) == 1 and token == "xoai///set/100"
    assert find_repo.parse_page(error("noRecordsMatch"), has_token=False) == ([], "")
    with pytest.raises(find_repo.Unexpected):
        find_repo.parse_page(error("noRecordsMatch"), has_token=True)
    with pytest.raises(LookupError):
        find_repo.parse_page(error("badResumptionToken"), has_token=True)
    for body in (b"<html>Just a moment...</html>", b"not xml", page([]),
                 b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"></OAI-PMH>',
                 error("badArgument")):
        with pytest.raises(find_repo.Unexpected):
            find_repo.parse_page(body, has_token=False)


def test_licence_names_are_recognised_only_as_whole_statements():
    kind = find_repo.licence_name_kind
    assert kind("CC BY 4.0 International") == "by" and kind("cc-by") == "by"
    assert kind("Creative Commons Attribution 4.0 International License") == "by"
    assert kind("Attribution-NonCommercial-ShareAlike 4.0 International") == "by-nc-sa"
    assert kind("CC BY-NC-ND 4.0 International") == "by-nc-nd"
    assert kind("CC0 1.0 Universal") == "zero"
    for text in ("This work is not licensed under Creative Commons", "All rights reserved",
                 "Creative Commons Attribution. All rights reserved.", "Creative Commons",
                 "CC BY except where noted", "Attribution required", "CC BY 4.0 Mars",
                 "In Copyright - Non-Commercial Use Permitted"):
        assert kind(text) is None, text


def test_another_metadata_format_is_unexpected_not_an_empty_page():
    dc = ('<record><header><identifier>oai:x:1</identifier></header><metadata>'
          '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
          'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Concrete bridges</dc:title>'
          "</oai_dc:dc></metadata></record>")
    with pytest.raises(find_repo.Unexpected, match="not xoai"):
        find_repo.parse_page(page([dc]), has_token=False)


# --------------------------------------------------------------------------------------------
# rotation
class Calls(list):
    robots: list


def _setup(monkeypatch, tmp_path, answers, repos=None, known=None, robots=None, policy=None,
           ai_opt_out=None):
    known = known or (set(), set(), set())
    calls, queue = Calls(), iter(answers)

    def get(url, **kwargs):
        base, _, query = url.partition("?")
        calls.append((base, dict(parse_qsl(query))))
        assert "nekaise-corpus" in kwargs["headers"]["User-Agent"]
        assert kwargs["allow_redirects"] is False
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, content=item, headers={})

    monkeypatch.setattr(find_repo.requests, "get", get)
    sleeps = []
    monkeypatch.setattr(find_repo.time, "sleep", sleeps.append)
    find_repo._last.clear()
    monkeypatch.setattr(find_repo, "load_policy", lambda: policy or {})
    robots_calls = []

    def decision(url):
        robots_calls.append(url)
        answer = robots(url) if robots else (True, None)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(find_repo.robots_policy, "decision", decision)
    monkeypatch.setattr(find_repo.robots_policy, "ai_training_refused",
                        lambda url: ai_opt_out(url) if ai_opt_out else None)
    calls.robots = robots_calls
    monkeypatch.setattr(find_repo, "load_config", lambda: repos or {"example": CFG})
    monkeypatch.setattr(find_repo.dedup, "open_keys",
                        lambda: find_repo.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_repo.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, sleeps, appended, files


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["find_repo.py", *argv])
    find_repo.main()


def _next(files):
    return json.loads(files["next"].read_text())


def test_walk_pages_sets_then_a_watch_state(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).date()
    pages = [page([record(1, "Majnabbe"), record(2, "Concrete slab bridges")], "tok1"),
             page([record(3, "Timber frames")], ""),
             page([record(4, "Quantum circuits"), record(5, "Steel bridges in fatigue")], "")]
    known = ({f"https://repo.example.edu/bitstreams/{UUID.format(3)}/download"}, set(), set())
    calls, sleeps, appended, files = _setup(monkeypatch, tmp_path, pages, known=known)
    _run(monkeypatch, "--cursor", "START", "--pages", "2", "--append")
    assert [c[1] for c in calls] == [
        {"verb": "ListRecords", "metadataPrefix": "xoai", "set": "com_civil"},
        {"verb": "ListRecords", "resumptionToken": "tok1"}]
    assert len(sleeps) == 1 and 3.9 < sleeps[0] <= 4.0       # the repository's crawl delay
    assert len(calls.robots) == 2 and "resumptionToken=tok1" in calls.robots[1]
    assert [e["title"] for e in appended] == ["Majnabbe", "Concrete slab bridges"]  # 3 is known
    state = _next(files)["s"]["example"]
    assert state == {"i": 1, "d": today.isoformat()}

    calls, _, appended, files = _setup(monkeypatch, tmp_path, pages[2:])
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": state}}), "--append")
    assert calls[0][1] == {"verb": "ListRecords", "metadataPrefix": "xoai", "set": "col_all"}
    assert [e["title"] for e in appended] == ["Steel bridges in fatigue"]    # gate in col_all
    done = _next(files)["s"]["example"]
    assert done == {"i": 0, "w": today.isoformat(),
                    "p": (today - timedelta(days=1)).isoformat()}
    assert not files["exhausted"].exists() and not files["hold"].exists()


def test_watch_waits_then_harvests_from_the_previous_pass_date(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).date()
    recent = {"i": 0, "w": (today - timedelta(days=find_repo.WATCH_DAYS - 1)).isoformat(),
              "p": "2026-09-30"}
    calls, _, _, files = _setup(monkeypatch, tmp_path, [])
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": recent}}))
    assert calls == [] and _next(files)["s"]["example"] == recent

    due = dict(recent, w=(today - timedelta(days=find_repo.WATCH_DAYS)).isoformat())
    calls, _, appended, files = _setup(
        monkeypatch, tmp_path, [error("noRecordsMatch"), page([record(9, "Masonry arches")])])
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": due}}), "--append")
    assert [c[1] for c in calls] == [
        {"verb": "ListRecords", "metadataPrefix": "xoai", "set": "com_civil",
         "from": "2026-09-30"},
        {"verb": "ListRecords", "metadataPrefix": "xoai", "set": "col_all",
         "from": "2026-09-30"}]
    assert [e["title"] for e in appended] == ["Masonry arches"]
    assert _next(files)["s"]["example"]["w"] == today.isoformat()


def test_round_robin_reads_other_repositories_next(monkeypatch, tmp_path):
    repos = _two(("aaa", "bbb", "ccc"))
    answers = [page([record(1, "Report A", host="ccc.example.edu")], "t"),
               page([record(2, "Report B", host="aaa.example.edu")], "t")]
    calls, _, appended, files = _setup(monkeypatch, tmp_path, answers, repos=repos)
    _run(monkeypatch, "--cursor", json.dumps({"r": 2, "s": {}}), "--repos", "2", "--pages", "1",
         "--append")
    assert [c[0] for c in calls] == ["https://ccc.example.edu/oai/request",
                                     "https://aaa.example.edu/oai/request"]
    cursor = _next(files)
    assert cursor["r"] == 1 and set(cursor["s"]) == {"aaa", "ccc"}
    assert {e["source"] for e in appended} == {"repo_ccc", "repo_aaa"}


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, content=b"", headers={}),
    b"<html><title>Just a moment...</title></html>",
    page([]),
    requests.ConnectionError("down"),
])
def test_an_unexpected_answer_keeps_that_repositorys_state(monkeypatch, tmp_path, answer):
    state = {"i": 1, "t": "tok9", "d": "2026-10-01"}
    cursor = json.dumps({"r": 0, "s": {"example": state}})
    _, _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    _run(monkeypatch, "--cursor", cursor, "--append")
    assert appended == [] and not files["hold"].exists()
    assert _next(files) == {"r": 0, "s": {"example": state}}   # state kept, retried next turn


def test_one_failing_repository_does_not_block_the_other(monkeypatch, tmp_path):
    repos = _two()
    answers = [SimpleNamespace(status_code=500, content=b"", headers={}),
               page([record(2, "Report B", host="bbb.example.edu")], "t")]
    _, _, appended, files = _setup(monkeypatch, tmp_path, answers, repos=repos)
    _run(monkeypatch, "--cursor", "START", "--repos", "2", "--pages", "1", "--append")
    assert [e["title"] for e in appended] == ["Report B"]
    cursor = _next(files)
    assert set(cursor["s"]) == {"bbb"} and not files["hold"].exists()


def test_a_rejected_token_restarts_the_set_once(monkeypatch, tmp_path):
    state = {"i": 0, "t": "stale", "d": "2026-10-01"}
    answers = [error("badResumptionToken"), page([record(1, "Bridge report")], "fresh")]
    calls, _, appended, files = _setup(monkeypatch, tmp_path, answers)
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": state}}), "--pages", "2",
         "--append")
    assert calls[1][1] == {"verb": "ListRecords", "metadataPrefix": "xoai", "set": "com_civil"}
    assert len(appended) == 1 and _next(files)["s"]["example"]["t"] == "fresh"

    _, _, appended, files = _setup(monkeypatch, tmp_path, [error("badResumptionToken")] * 2)
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": state}}), "--pages", "3",
         "--append")
    assert appended == [] and _next(files)["s"]["example"] == state


def _two(keys=("aaa", "bbb")):
    return {key: dict(copy.deepcopy(CFG), source=f"repo_{key}", sets=["s"], all_sets=["s"],
                      hosts=[f"{key}.example.edu"],
                      fulltext_pattern=rf"^https://{key}\.example\.edu/bitstreams/"
                                       r"[0-9a-f-]{36}/download$",
                      oai=f"https://{key}.example.edu/oai/request") for key in keys}


def test_a_failed_repository_reserves_nothing_for_the_next_one(monkeypatch, tmp_path):
    # aaa lists "Shared thesis" and then fails on its second page; bbb holds the same work
    # (same title) and is complete after one page: bbb must propose it (Codex round 1)
    answers = [page([record(1, "Shared thesis", host="aaa.example.edu")], "t2"),
               SimpleNamespace(status_code=500, content=b"", headers={}),
               page([record(2, "Shared thesis", host="bbb.example.edu")], "")]
    _, _, appended, files = _setup(monkeypatch, tmp_path, answers, repos=_two())
    _run(monkeypatch, "--cursor", "START", "--repos", "2", "--pages", "2", "--append")
    assert [(e["source"], e["title"]) for e in appended] == [("repo_bbb", "Shared thesis")]
    cursor = _next(files)["s"]
    assert "aaa" not in cursor and cursor["bbb"]["w"]

    # an ACCEPTED repository's entries do count for the next one
    answers = [page([record(1, "Shared thesis", host="aaa.example.edu")], ""),
               page([record(2, "Shared thesis", host="bbb.example.edu")], "")]
    _, _, appended, _ = _setup(monkeypatch, tmp_path, answers, repos=_two())
    _run(monkeypatch, "--cursor", "START", "--repos", "2", "--append")
    assert [e["source"] for e in appended] == ["repo_aaa"]


def test_an_oai_dc_answer_keeps_the_state_and_never_completes_history(monkeypatch, tmp_path):
    dc = ('<record><header><identifier>oai:x:1</identifier></header><metadata>'
          '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"/>'
          "</metadata></record>")
    state = {"i": 1, "d": "2026-10-01"}
    _, _, appended, files = _setup(monkeypatch, tmp_path, [page([dc], "")])
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": state}}), "--append")
    assert appended == [] and _next(files)["s"]["example"] == state


@pytest.mark.parametrize("robots,needle", [
    (lambda url: (False, None), "robots.txt disallows"),
    (lambda url: find_repo.robots_policy.RobotsUnavailable("HTTP 503"), "unavailable"),
    (lambda url: (True, 120.0), "Crawl-delay"),
])
def test_robots_is_checked_before_the_request(monkeypatch, tmp_path, capsys, robots, needle):
    state = {"i": 0, "t": "tok", "d": "2026-10-01"}
    calls, _, appended, files = _setup(monkeypatch, tmp_path, [], robots=robots)
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": state}}), "--append")
    assert calls == [] and appended == []                       # nothing was requested
    assert calls.robots[0].startswith("https://repo.example.edu/oai/request?verb=ListRecords")
    assert _next(files)["s"]["example"] == state and needle in capsys.readouterr().err


def test_an_ai_training_opt_out_added_later_skips_the_repository(monkeypatch, tmp_path,
                                                                 capsys):
    state = {"i": 0, "t": "tok", "d": "2026-10-01"}
    calls, _, appended, files = _setup(
        monkeypatch, tmp_path, [], ai_opt_out=lambda url: "Disallow for AI crawler claudebot")
    _run(monkeypatch, "--cursor", json.dumps({"r": 0, "s": {"example": state}}), "--append")
    assert calls == [] and appended == [] and _next(files)["s"]["example"] == state
    assert "opts out of AI training" in capsys.readouterr().err


def test_rep_rows_take_the_loaders_robots_checked_path():
    assert build_corpus.is_robots_checked_row("rep-chalmers-odr-x")
    assert not build_corpus.is_programme_row("rep-chalmers-odr-x")


def test_a_robots_crawl_delay_above_the_configured_one_paces_the_requests(monkeypatch,
                                                                          tmp_path):
    answers = [page([record(1, "Timber frames")], "t"), page([record(2, "Steel frames")], "t")]
    _, sleeps, _, _ = _setup(monkeypatch, tmp_path, answers, robots=lambda url: (True, 11))
    _run(monkeypatch, "--cursor", "START", "--pages", "2")
    assert len(sleeps) == 1 and 10.9 < sleeps[0] <= 11.0


def test_redirects_are_followed_by_hand_and_checked_per_hop(monkeypatch, tmp_path, capsys):
    def redirect(to):
        return SimpleNamespace(status_code=302, content=b"", headers={"location": to})

    # a same-host redirect is followed after its own robots check and pacing
    answers = [redirect("/server/oai/request?verb=ListRecords&metadataPrefix=xoai&set=com_civil"),
               page([record(1, "Timber frames")], "t")]
    calls, sleeps, appended, _ = _setup(monkeypatch, tmp_path, answers)
    _run(monkeypatch, "--cursor", "START", "--pages", "1", "--append")
    assert [c[0] for c in calls] == ["https://repo.example.edu/oai/request",
                                     "https://repo.example.edu/server/oai/request"]
    assert len(calls.robots) == 2 and "/server/oai/request" in calls.robots[1]
    assert len(sleeps) == 1 and len(appended) == 1

    # a redirect the robots file disallows, to another host, or to http is never requested
    for target, robots in (
            ("/login?next=oai", lambda url: ("/login" not in url, None)),
            ("https://sso.elsewhere.org/oai", None),
            ("http://repo.example.edu/oai/request", None)):
        calls, _, appended, files = _setup(monkeypatch, tmp_path, [redirect(target)],
                                           robots=robots)
        _run(monkeypatch, "--cursor", "START", "--append")
        assert len(calls) == 1 and appended == [] and _next(files)["s"] == {}, target
    assert "unreviewed destination" in capsys.readouterr().err


def test_a_suspended_host_or_an_unavailable_policy_makes_no_request(monkeypatch, tmp_path):
    rule = {"repo.example.edu": {"status": "suspended", "reason": "x",
                                 "decided_at": "2026-10-12"}}
    calls, _, appended, files = _setup(monkeypatch, tmp_path, [], policy=rule)
    _run(monkeypatch, "--cursor", "START", "--append")
    assert calls == [] and calls.robots == [] and appended == [] and _next(files)["s"] == {}

    files["next"].unlink()
    calls, _, appended, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(find_repo, "load_policy",
                        lambda: (_ for _ in ()).throw(RuntimeError("no pinned policy")))
    _run(monkeypatch, "--cursor", "START", "--append")
    assert calls == [] and appended == []
    assert files["hold"].exists() and not files["next"].exists()


def test_cursor_forms_and_removed_repositories(monkeypatch, tmp_path):
    assert find_repo.parse_cursor("START") == (0, {})
    for bad in ("END", "{}", '{"r": -1, "s": {}}', '{"r": 0, "s": {"x": {"i": "0"}}}',
                '{"r": 0, "s": {"x": {"i": 0, "w": "soon"}}}', '{"r": true, "s": {}}'):
        with pytest.raises(ValueError):
            find_repo.parse_cursor(bad)
    state = {"i": 0, "w": date.today().isoformat(), "p": "2026-10-01"}
    _, _, _, files = _setup(monkeypatch, tmp_path, [])
    _run(monkeypatch, "--cursor", json.dumps({"r": 5, "s": {"example": state, "gone": state}}))
    assert set(_next(files)["s"]) == {"example"}     # a removed repository's state is dropped
