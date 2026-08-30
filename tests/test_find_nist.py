import sys

import pytest

import find_nist
import lint_registry


def test_wave_3_query_universe_is_pinned_and_topics_are_valid():
    assert find_nist.QUERY_WAVE == 3
    assert len(find_nist.QUERIES) == 15
    assert {topic for _term, topic in find_nist.QUERIES} <= lint_registry.TOPICS


@pytest.mark.parametrize(
    "title",
    [
        "Annex 47 Report 1: Commissioning Overview",
        "A simulation study of fault detection in HVAC systems",
        "Programmers guide to the BACnet communications DLL",
        "Summer attic and whole-house ventilation",
        "Sensitivity analysis of installation faults on heat pump performance",
        "Seismic provisions for structural building codes",
    ],
)
def test_wave_3_title_gate_accepts_aec_titles(title):
    assert find_nist.title_in_scope(title)


@pytest.mark.parametrize(
    "title",
    [
        "On strongly continuous stochastic processes",
        "Message handling systems interoperability tests",
        "Guidelines for smart grid cybersecurity",
        "Code extension techniques for the 7-bit coded character set",
        "Material Handling Workstation implementation",
    ],
)
def test_wave_3_title_gate_rejects_observed_crossref_false_positives(title):
    assert not find_nist.title_in_scope(title)


def test_crossref_applies_title_gate_before_returning_hits(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"items": [
                {
                    "title": ["HVAC functional inspection and testing guide"],
                    "resource": {"primary": {"URL": "https://nvlpubs.nist.gov/hvac.pdf"}},
                },
                {
                    "title": ["Message handling systems interoperability tests"],
                    "resource": {"primary": {"URL": "https://nvlpubs.nist.gov/it.pdf"}},
                },
            ]}}

    monkeypatch.setattr(find_nist.requests, "get", lambda *_args, **_kwargs: Response())

    assert find_nist.from_crossref("building controls interoperability", 50, 0) == [
        ("HVAC functional inspection and testing guide", "https://nvlpubs.nist.gov/hvac.pdf")
    ]


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_nist.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_nist,
        "QUERIES",
        [("first", "building_energy"), ("second", "materials")],
    )

    def from_crossref(term, _rows, _offset):
        if term == "second":
            raise TimeoutError("offline")
        return [("Concrete envelope report", "https://nvlpubs.nist.gov/a.pdf")]

    monkeypatch.setattr(find_nist, "from_crossref", from_crossref)
    monkeypatch.setattr(find_nist.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        find_nist.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_nist.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_page_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_nist, "QUERIES", [("dry", "building_energy")])
    monkeypatch.setattr(find_nist, "from_crossref", lambda *_args: [])
    monkeypatch.setattr(find_nist.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        find_nist.registry,
        "append_entries",
        lambda _entries: pytest.fail("an empty result must not append"),
    )
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--append"])

    find_nist.main()

    assert "0 NEW NIST/NBS technical series PDFs" in capsys.readouterr().out
