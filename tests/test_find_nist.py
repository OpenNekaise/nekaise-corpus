import sys

import pytest

import find_nist


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
