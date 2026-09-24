import sys
from types import SimpleNamespace

import pytest

import bes_relevance
import find_escholarship
import lint_registry

CC_BY = "https://creativecommons.org/licenses/by/4.0/"


def _node(title, n, rights=CC_BY, link=True, **extra):
    return {
        "id": f"ark:/13030/qt{n}",
        "title": title,
        "rights": rights,
        "contentLink": f"https://escholarship.org/content/qt{n}/qt{n}.pdf" if link else None,
        "contentType": "application/pdf" if link else None,
        "status": "PUBLISHED",
        "keywords": extra.get("keywords"),
        "subjects": extra.get("subjects"),
    }


def _page(nodes, more):
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"data": {"unit": {"items": {"total": 999, "more": more, "nodes": nodes}}}},
    )


def _setup(monkeypatch, tmp_path, responses, known=None):
    known = known or (set(), set(), set())
    calls = []
    queue = iter(responses)

    def post(url, **kwargs):
        calls.append(kwargs)
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(find_escholarship.requests, "post", post)
    monkeypatch.setattr(find_escholarship.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(find_escholarship.dedup, "open_keys", lambda: find_escholarship.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_escholarship.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "hold")}
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


@pytest.mark.parametrize(
    ("rights", "expected"),
    [
        ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
        ("https://creativecommons.org/licenses/by/3.0/", "cc-by"),
        ("https://creativecommons.org/licenses/by-sa/4.0/", "cc-by-sa"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
        ("https://creativecommons.org/publicdomain/mark/1.0/", "public-domain"),
        ("https://creativecommons.org/licenses/by-nc/4.0/", None),
        ("https://creativecommons.org/licenses/by-nd/4.0/", None),
        ("https://creativecommons.org/licenses/by-nc-sa/4.0/", None),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", None),
        (None, None),
    ],
)
def test_rights_gate_keeps_only_redistributable_licenses(rights, expected):
    assert find_escholarship.license_for(rights) == expected
    if expected:
        assert expected in lint_registry.LICENSES


def test_candidate_requires_direct_escholarship_pdf_and_relevance():
    ok = find_escholarship.candidate(_node("Smart ventilation in residential buildings", "a"), True)
    assert ok["url"] == "https://escholarship.org/content/qta/qta.pdf"
    assert ok["id"].startswith("esc-") and ok["license"] == "cc-by"
    assert ok["license_url"] == CC_BY and ok["persistent_id"] == "ark:/13030/qta"
    assert ok["topic"] in lint_registry.TOPICS
    assert find_escholarship.candidate(_node("Smart ventilation in buildings", "b", link=False),
                                       True) is None
    offsite = _node("Smart ventilation in buildings", "c")
    offsite["contentLink"] = "https://publisher.example/c.pdf"
    assert find_escholarship.candidate(offsite, True) is None
    assert find_escholarship.candidate(_node("Hadron collisions at 13 TeV", "d"), True) is None


def test_cursor_round_trip_and_validation():
    assert find_escholarship.parse_cursor("cedr_cbe:START") == (0, None)
    assert find_escholarship.parse_cursor("lbnl_rw:abc") == (3, "abc")
    assert find_escholarship.format_cursor(1, None) == "lbnl_et_btus:START"
    for bad in ("nope:START", "cedr_cbe", "cedr_cbe:"):
        with pytest.raises(ValueError):
            find_escholarship.parse_cursor(bad)


def test_walk_pages_paces_requests_and_reports_opaque_cursor(monkeypatch, tmp_path, capsys):
    responses = [
        _page([_node("Thermal comfort in offices", "1"),
               _node("Occupant survey", "2", rights=None)], "tok-2"),
        _page([_node("Radiant cooling ceilings", "3")], "tok-3"),
    ]
    calls, appended, files = _setup(monkeypatch, tmp_path, responses)
    monkeypatch.setattr(sys, "argv", ["find_escholarship.py", "--cursor", "cedr_cbe:START",
                                      "--pages", "2", "--append"])

    find_escholarship.main()

    assert [e["title"] for e in appended] == ["Thermal comfort in offices",
                                              "Radiant cooling ceilings"]
    assert files["next"].read_text() == "cedr_cbe:tok-3\n"
    posts = [c for c in calls if c != "sleep"]
    assert posts[0]["json"]["variables"] == {"id": "cedr_cbe", "first": 100, "more": None}
    assert posts[1]["json"]["variables"]["more"] == "tok-2"
    assert calls.index("sleep") == 1  # crawl-delay between the two requests
    assert posts[0]["headers"]["User-Agent"] == find_escholarship.UA


def test_unit_end_moves_to_next_unit(monkeypatch, tmp_path):
    responses = [_page([], None), _page([], "tok-b")]
    calls, _, files = _setup(monkeypatch, tmp_path, responses)
    monkeypatch.setattr(sys, "argv", ["find_escholarship.py", "--cursor", "cedr_cbe:last",
                                      "--pages", "2"])

    find_escholarship.main()

    posts = [c for c in calls if c != "sleep"]
    assert posts[1]["json"]["variables"] == {"id": "lbnl_et_btus", "first": 100, "more": None}
    assert files["next"].read_text() == "lbnl_et_btus:tok-b\n"


def test_last_unit_tail_keeps_the_final_page_token_to_reprobe(monkeypatch, tmp_path, capsys):
    _, _, files = _setup(monkeypatch, tmp_path, [_page([], None)])
    monkeypatch.setattr(sys, "argv", ["find_escholarship.py", "--cursor", "lbnl_rw:tail"])

    find_escholarship.main()

    assert files["next"].read_text() == "lbnl_rw:tail\n"
    assert "re-probes it" in capsys.readouterr().out


def test_strict_units_reject_ambiguous_titles(monkeypatch, tmp_path):
    nodes = [_node("Time windows for scheduling detectors", "1"),
             _node("Energy performance of windows in homes", "2")]
    _, appended, _ = _setup(monkeypatch, tmp_path, [_page(nodes, "next")])
    monkeypatch.setattr(sys, "argv", ["find_escholarship.py", "--cursor", "lbnl_rw:START",
                                      "--pages", "1", "--append"])

    find_escholarship.main()

    assert [e["title"] for e in appended] == ["Energy performance of windows in homes"]


def test_finder_identity_is_honest():
    assert find_escholarship.UA.startswith("nekaise-corpus/")
    assert "Mozilla" not in find_escholarship.UA


@pytest.mark.parametrize("status", [202, 403, 429, 503])
def test_challenge_holds_rotation_without_proposals(monkeypatch, tmp_path, status):
    challenge = SimpleNamespace(status_code=status, raise_for_status=lambda: None,
                                json=lambda: {})
    _, appended, files = _setup(monkeypatch, tmp_path, [challenge])
    monkeypatch.setattr(sys, "argv", ["find_escholarship.py", "--append"])

    find_escholarship.main()

    assert appended == []
    assert f"HTTP {status}" in files["hold"].read_text()
    assert not files["next"].exists()


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, tmp_path):
    responses = [_page([_node("Thermal comfort in offices", "1")], "tok-2"),
                 TimeoutError("offline")]
    _, appended, files = _setup(monkeypatch, tmp_path, responses)
    monkeypatch.setattr(sys, "argv", ["find_escholarship.py", "--pages", "2", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_escholarship.main()

    assert exc.value.code == 1
    assert appended == [] and not files["next"].exists()


@pytest.mark.parametrize(
    "title",
    [
        "Smart ventilation energy and indoor air quality performance in residential buildings",
        "A labeled dataset for building HVAC systems operating in faulted and fault-free states",
        "High-performance windows improve thermal survivability of occupants during cold snaps",
        "Thermal decay in underfloor air distribution (UFAD) systems",
        "Enter the AHU (36th Chamber of ASHRAE): A Multi-site Field Study of ASHRAE G36",
    ],
)
def test_relevance_gate_accepts_building_science(title):
    assert bes_relevance.relevant(title, strict=True)
    assert bes_relevance.topic_for(title) in lint_registry.TOPICS


@pytest.mark.parametrize(
    "title",
    [
        "Analysis of NOx Formation in a Hydrogen-Fueled Gas Turbine Engine",
        "A net-zero emissions strategy for China's power sector using carbon capture",
        "Construction of a Bayesian estimator for time windows",
        "Protein folding in cells of residential mice",
        "Future Projections of Lifecycle Cost of Light-Duty Vehicles",
    ],
)
def test_relevance_gate_rejects_observed_off_domain_titles(title):
    assert not bes_relevance.relevant(title, strict=True)


@pytest.mark.parametrize(
    "title",
    [
        # measured 2026-09-24: all of these were vetoed by the old unconditional KILL list
        "Battery storage for grid-interactive buildings",
        "Indoor air quality and airborne virus transmission",
        "Soil thermal conductivity for ground-source heat pumps",
        "The Effects of Ventilation, Humidity, and Temperature on Bacterial Growth",
        "iPlugie: Intelligent electric vehicle charging in buildings",
        "SolarPlus Optimizer: Integrated Control of Solar, Batteries, and Flexible Loads for "
        "Small Commercial Buildings",
    ],
)
def test_soft_exclusions_are_rescued_by_a_building_anchor(title):
    assert bes_relevance.relevant(title, strict=True)


@pytest.mark.parametrize(
    "title",
    [
        "Battery electrolyte degradation at high voltage",
        "Airborne virus transmission in bats",
        "Soil carbon under drought",
        "Electric vehicle fleet charging economics",
        "Genome of a bacterium isolated from building dust",  # hard veto beats the anchor
        "Perovskite windows for buildings",
    ],
)
def test_exclusions_without_anchor_or_hard_exclusions_still_veto(title):
    assert not bes_relevance.relevant(title, strict=True)


def test_relevance_loose_mode_and_fields_of_research():
    assert not bes_relevance.relevant("Survey of homes and offices", strict=True)
    assert bes_relevance.relevant("Survey of homes and offices", strict=False)
    assert bes_relevance.relevant("Survey results", keywords=["3302 Building (for-2020)"])
    # bare division / urban-planning labels also tag vehicle and grid papers: not enough
    assert not bes_relevance.relevant(
        "Feeder sets", keywords=["33 Built Environment and Design (for-2020)"]
    )


@pytest.mark.parametrize("rights,expected", [
    ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
    ("https://creativecommons.org/licenses/by-sa/4.0/", "cc-by-sa"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("https://creativecommons.org/publicdomain/mark/1.0/", "public-domain"),
    # found by the maintainer's publication review: substring matching accepted these
    ("https://example.invalid/creativecommons.org/licenses/by/4.0/", None),
    ("Not licensed under https://creativecommons.org/licenses/by/4.0/", None),
    ("https://creativecommons.org/licenses/by-nc/4.0/", None),
    (None, None),
])
def test_rights_are_parsed_strictly(rights, expected):
    import find_escholarship
    assert find_escholarship.license_for(rights) == expected
