import sys

import pytest

import find_archive


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_archive.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_request_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_archive,
        "QUERIES",
        [("first", "architecture"), ("second", "materials")],
    )

    def search(term, *_args):
        if term == "first":
            return [{"identifier": "first-book", "title": "First building book", "year": 1920}]
        raise TimeoutError("offline")

    monkeypatch.setattr(
        find_archive,
        "search_archive",
        search,
    )
    monkeypatch.setattr(find_archive.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        find_archive.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_archive.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_archive.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_api_error_envelope_exits_nonzero(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_archive, "QUERIES", [("deep", "architecture")])

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "[DEEP_PAGING] requested results exceed 10000"}

    monkeypatch.setattr(find_archive.requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(sys, "argv", ["find_archive.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_archive.main()

    assert exc.value.code == 1
    assert "DEEP_PAGING" in capsys.readouterr().err


def test_successful_empty_response_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_archive, "QUERIES", [("dry", "architecture")])
    monkeypatch.setattr(find_archive, "search_archive", lambda *_args: [])
    monkeypatch.setattr(find_archive.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(sys, "argv", ["find_archive.py", "--append"])

    find_archive.main()

    assert "0 NEW pre-1929 public-domain texts" in capsys.readouterr().out
