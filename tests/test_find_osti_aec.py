import json
import sys
import zlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import find_osti_aec
import find_osti_sim
import lint_registry
import registry

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 12)


def _rec(oid, title, kind="Technical Report", fulltext=True, **extra):
    links = [{"rel": "citation", "href": f"https://www.osti.gov/biblio/{oid}"}]
    if fulltext:
        links.append({"rel": "fulltext", "href": f"https://www.osti.gov/servlets/purl/{oid}"})
    return {"osti_id": str(oid), "title": title, "product_type": kind, "links": links,
            "language": "English", "publication_date": "2024-05-01T00:00:00Z", **extra}


HEAT_PUMP = _rec(2001, "Cold Climate Heat Pump Field Study in Multifamily Housing")
BRIDGE = _rec(2002, "Durability of reinforced concrete bridge decks", "Journal Article",
              article_type="Accepted Manuscript", journal_name="Cement and Concrete Research",
              doi="10.1016/j.cemconres.2024.1")
SEISMIC = _rec(2003, "Seismic design of steel moment frames", "Conference")
WASTE = _rec(2004, "Concrete durability in saltstone waste forms")
PHYSICS = _rec(2005, "Structural phase transitions in perovskite crystals")
DATASET = _rec(2006, "Residential building stock dataset", "Dataset")


@pytest.mark.parametrize("title", [
    # building science (bes_relevance strict gate)
    "Cold Climate Heat Pump Field Study in Multifamily Housing",
    "Commercial Building Energy Codes: Compliance Evaluation",
    "Weatherization Assistance Program: indoor air quality in retrofitted homes",
    # unambiguous civil / structural / construction-material concepts
    "Durability of reinforced concrete bridge decks",
    "Seismic design of steel moment frames",
    "Soil-structure interaction of deeply embedded structures",
    "Geotechnical investigation for a foundation on soft clay",
    "Asphalt pavement recycling with fly ash",
    "Mass timber and cross-laminated timber in mid-rise construction",
    "Wastewater treatment plant energy audit",
    "Construction cost estimating for modular construction",
    "Fire protection of structural steel",
    "Urban planning and land use for transit corridors",
    # ambiguous civil word plus an engineering context
    "Compressive strength of concrete with recycled aggregate",
    "Earthquake hazard and the safety of dams",
    "Corrosion of steel in cracked concrete structures",
    # "fusion" is not "diffusion" (Codex review round 1)
    "Chloride diffusion in reinforced concrete",
    "Moisture diffusion through building envelopes",
    # contextual vetoes: a building anchor in the title overrides them
    "Fire protection using smoke detectors in residential buildings",
    "Oil-fired residential furnaces",
    "Laser scanning of building facades for retrofit design",
    "Cybersecurity of building automation systems",
    "Electric vehicle charging infrastructure in multifamily buildings",
    "Biomass boiler for a district heating network",
])
def test_gate_keeps_built_environment_titles(title):
    assert find_osti_aec.relevant(title)


@pytest.mark.parametrize("title", [
    "",
    # an ambiguous civil word without an engineering context
    "A concrete example of gauge symmetry breaking",
    "Bridge functions in the theory of liquids",
    "Foundations of statistical mechanics",
    "Column chromatography of rare earths",
    "Urban legends in the history of physics",
    # off-domain subjects, even beside a civil concept
    "Concrete durability in saltstone waste forms",
    "Reinforced concrete response to underground nuclear test loading",
    "Seismic survey design for reservoir characterization",
    "Seismic monitoring of nuclear explosions",
    "Structural design of the tokamak vacuum vessel",
    "Cement integrity in wellbore drilling for geologic sequestration",
    "Structural phase transitions in perovskite crystals",
    "Reactor core seismic design margins",
    "Construction of the Yucca Mountain repository tunnels",
    "Fire safety of lithium battery cells",
    "Soil microbial communities under drought",
    "Hydrogen infrastructure for fuel cell vehicles",
    # unconditional vetoes hold beside a civil or building word
    "Structural design of a nuclear fusion reactor building",
    "Seismic qualification of particle detectors in the collider hall building",
    "Petroleum exploration: construction of drilling structures",
    "Oil exploration and the design of offshore structures",
    "Gaseous diffusion plant building ventilation",
    # contextual vetoes hold without a building anchor
    "Structural design of silicon strip detectors",
    "Foundations and structures for oil storage",
    "Structural analysis of wind turbine blades",
    "Laser welding of structural steel",
    "Cement leaching in a geologic repository",
])
def test_gate_drops_off_domain_and_ambiguous_titles(title):
    assert not find_osti_aec.relevant(title)


@pytest.mark.parametrize("word", [
    "diffusion", "infusion", "confusion", "communition", "bleaching", "isotropic", "anisotropic",
    "accelerated", "accelerator admixture", "wellbeing", "well-insulated", "foiled", "soiled",
    "turmoil", "marsh", "generation", "generic", "indigenous", "forestall", "fishing line",
    "selfish", "unclear", "lasers" + "cape", "crystalline"[:4], "atomizer", "biomassive",
    "electrolaser", "detectorist", "plasmatic"[:5] + "ter",
])
def test_vetoes_do_not_match_inside_ordinary_words(word):
    """Each word contains a veto stem as a substring only; the building title must pass."""
    title = f"Residential building energy retrofit: {word} study"
    assert not find_osti_aec.OFF_DOMAIN.search(title), find_osti_aec.OFF_DOMAIN.search(title)
    assert find_osti_aec.relevant(title)


def test_candidate_carries_family_identity_topic_and_licence_classes():
    family = find_osti_aec.family()
    q = next(i for i, (_query, topic) in enumerate(find_osti_aec.QUERIES)
             if topic == "structures_civil")
    kept = {r["osti_id"]: find_osti_sim.candidate(r, "2026-10-12", family, q)
            for r in (HEAT_PUMP, BRIDGE, SEISMIC, WASTE, PHYSICS, DATASET)}
    assert {k for k, v in kept.items() if v} == {"2001", "2002", "2003"}
    report, article, conf = kept["2001"], kept["2002"], kept["2003"]
    assert report["license"] == "unverified" and "osti.gov/disclaim" in report["license_evidence"]
    assert article["license"] == "publisher-oa"
    assert article["persistent_id"] == "doi:10.1016/j.cemconres.2024.1"
    assert conf["document_type"] == "conference-paper"
    for entry in kept.values():
        if not entry:
            continue
        assert entry["topic"] == "structures_civil"  # the query's topic
        assert entry["license"] in lint_registry.LICENSES
        assert entry["license"] != "public-domain"  # never without evidence for the copy
        assert entry["source"] == "osti_aec" and entry["id"].startswith("osa-")
        shard = registry.shard_filename(entry["id"])
        bucket = zlib.crc32(entry["id"].encode()) % registry.HASH_BUCKETS["ostiaec"]
        assert shard == f"ostiaec-{bucket}.yaml"
        assert registry.manifest_shard(entry["id"]) == f"ostiaec-{bucket}"
        assert not lint_registry.entry_errors(entry, shard)


def test_explicit_cc_rights_field_is_the_licence():
    rec = _rec(2010, "Heat pump water heater field test",
               rights="https://creativecommons.org/licenses/by/4.0/")
    entry = find_osti_sim.candidate(rec, "x", find_osti_aec.family(), 0)
    assert entry["license"] == "cc-by" and entry["license_url"]


def test_queries_are_wellformed_and_topics_are_in_the_vocabulary():
    queries = [query for query, _topic in find_osti_aec.QUERIES]
    assert len(set(queries)) == len(queries) >= 50
    for query, topic in find_osti_aec.QUERIES:
        assert topic in lint_registry.TOPICS and topic != "simulation_modeling"
        assert query.count('"') % 2 == 0 and query.count("(") == query.count(")")
        # OR and AND never share one parenthesis level (precedence is not documented)
        depth0 = ""
        level = 0
        for token in query.replace("(", " ( ").replace(")", " ) ").split():
            level += (token == "(") - (token == ")")
            if level == 0 and token in ("AND", "OR"):
                depth0 += token[0]
        assert len(set(depth0)) <= 1, query
    # the simulation family keeps its own queries: no query is walked by both backends
    assert not set(queries) & set(find_osti_sim.QUERIES)


def _setup(monkeypatch, tmp_path, pages, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(pages)

    def get(url, **kwargs):
        calls.append({**kwargs["params"], "_ua": kwargs["headers"]["User-Agent"]})
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, json=lambda item=item: item)

    monkeypatch.setattr(find_osti_sim.requests, "get", get)
    monkeypatch.setattr(find_osti_sim.time, "sleep", lambda _s: None)
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


def test_history_walk_uses_the_family_queries_and_dedups(monkeypatch, tmp_path):
    monkeypatch.setattr(find_osti_sim, "ROWS", 3)
    known = (set(), set(), set(), {"doi:10.1016/j.cemconres.2024.1"})
    calls, appended, files = _setup(monkeypatch, tmp_path,
                                    [[HEAT_PUMP, BRIDGE, WASTE], [SEISMIC]], known)
    monkeypatch.setattr(sys, "argv", ["find_osti_aec.py", "--cursor", "q=0 p=1", "--pages", "2",
                                      "--append"])
    find_osti_aec.main()
    assert [(c["q"], c["page"]) for c in calls] == [(find_osti_aec.QUERIES[0][0], 1),
                                                    (find_osti_aec.QUERIES[0][0], 2)]
    assert all("find_osti_aec" in c["_ua"] and "entry_date_start" not in c for c in calls)
    assert [e["url"].rsplit("/", 1)[1] for e in appended] == ["2001", "2003"]  # DOI known
    assert {e["topic"] for e in appended} == {find_osti_aec.QUERIES[0][1]}
    assert files["next"].read_text() == "q=1 p=1 since=2026-10-12\n"


def test_the_last_query_hands_over_to_watch_and_never_exhausts(monkeypatch, tmp_path):
    last = len(find_osti_aec.QUERIES) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [[]])
    monkeypatch.setattr(sys, "argv", ["find_osti_aec.py", "--cursor",
                                      f"q={last} p=9 since=2026-10-12"])
    find_osti_aec.main()
    assert files["next"].read_text() == "watch:2026-10-12\n"
    assert not files["exhausted"].exists()


def test_watch_pass_reads_the_entry_date_window(monkeypatch, tmp_path):
    calls, appended, files = _setup(monkeypatch, tmp_path, [[HEAT_PUMP], [SEISMIC]])
    monkeypatch.setattr(sys, "argv", ["find_osti_aec.py", "--cursor", "watch:2026-10-10",
                                      "--pages", "2", "--append"])
    find_osti_aec.main()
    assert [c["q"] for c in calls] == [q for q, _t in find_osti_aec.QUERIES[:2]]
    assert all((c["entry_date_start"], c["entry_date_end"], c["sort"]) ==
               ("10/10/2026", "10/12/2026", "entry_date asc") for c in calls)
    assert len(appended) == 2
    assert files["next"].read_text() == "watch:2026-10-10:2026-10-12 q=2 p=1\n"


@pytest.mark.parametrize("cursor", [
    "q={n} p=1", "q=999 p=1 since=2026-10-02", "watch:2026-10-02:2026-10-12 q={n} p=1",
    "watch:2026-10-02:2026-10-12 q=999 p=1",
])
def test_query_index_outside_the_family_is_refused(monkeypatch, tmp_path, cursor):
    calls, appended, files = _setup(monkeypatch, tmp_path, [])
    opened = []
    monkeypatch.setattr(find_osti_sim.dedup, "open_keys", lambda: opened.append(1))
    monkeypatch.setattr(sys, "argv", ["find_osti_aec.py", "--cursor",
                                      cursor.format(n=len(find_osti_aec.QUERIES)), "--append"])
    with pytest.raises(SystemExit) as exc:
        find_osti_aec.main()
    assert exc.value.code == 2
    # no request, no dedup state, and above all no NEXT: the window is not "complete"
    assert calls == [] and opened == [] and appended == []
    assert not any(path.exists() for path in files.values())


def test_unexpected_answer_holds(monkeypatch, tmp_path):
    answer = SimpleNamespace(status_code=503, json=lambda: [])
    _, appended, files = _setup(monkeypatch, tmp_path, [answer])
    monkeypatch.setattr(sys, "argv", ["find_osti_aec.py", "--append"])
    find_osti_aec.main()
    assert appended == [] and files["hold"].read_text().strip() and not files["next"].exists()


def test_control_plane_wiring():
    backends = json.loads((ROOT / "registry" / "backends.json").read_text())
    rotation = json.loads((ROOT / "registry" / "rotation.json").read_text())
    aec = backends["find_osti_aec"]
    assert aec["script"] == "find_osti_aec.py" and aec["required"] is False
    for name in ("find_osti_aec", "find_osti_sim"):
        # configuration only: the pointer itself is runtime state that every round moves
        assert rotation[name]["flag"] == "--cursor" and rotation[name]["dynamic"] is True
    # the superseded public-domain finder stays off
    assert backends["find_osti"]["enabled"] is False
