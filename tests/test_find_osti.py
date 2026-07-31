import sys

import pytest

import find_osti


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_osti.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_osti, "QUERIES", [("first", "building_energy"), ("second", "materials")])
    monkeypatch.setattr(
        find_osti,
        "from_osti",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_osti.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_osti.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_osti.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_page_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_osti, "QUERIES", [("dry", "building_energy")])
    monkeypatch.setattr(find_osti, "from_osti", lambda *_args: [])
    monkeypatch.setattr(sys, "argv", ["find_osti.py", "--append"])

    find_osti.main()

    assert "0 NEW OSTI full-text records" in capsys.readouterr().out
