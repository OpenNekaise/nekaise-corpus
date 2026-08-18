import sys

import find_openaire
import pytest


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_openaire.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_openaire, "QUERIES", [("first", "building_energy")])
    monkeypatch.setattr(
        find_openaire,
        "from_openaire",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_openaire.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_openaire.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_openaire.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_page_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_openaire, "QUERIES", [("dry", "building_energy")])
    monkeypatch.setattr(find_openaire, "from_openaire", lambda *_args: [])
    monkeypatch.setattr(sys, "argv", ["find_openaire.py", "--append"])

    find_openaire.main()

    assert "0 NEW EU project deliverables" in capsys.readouterr().out
