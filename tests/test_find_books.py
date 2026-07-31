import json
import sys

import find_books
import pytest
import requests


def test_oapen_license_cache_ignores_transient_negative_entries(tmp_path, monkeypatch):
    cache = tmp_path / "licenses.json"
    cache.write_text(json.dumps({"good": "cc-by", "transient-or-negative": None}))
    monkeypatch.setattr(find_books, "LICENSE_CACHE", cache)

    assert find_books.load_license_cache() == {"good": "cc-by"}


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_books.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_search_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_books, "QUERIES", [("first", "architecture")])
    monkeypatch.setattr(
        find_books,
        "search_subject",
        lambda *_args: ([], "'first' @925"),
    )
    monkeypatch.setattr(
        find_books.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_books.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_books.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_search_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_books, "QUERIES", [("dry", "architecture")])
    monkeypatch.setattr(find_books, "search_subject", lambda *_args: ([], None))
    monkeypatch.setattr(sys, "argv", ["find_books.py", "--append"])

    find_books.main()

    assert "0 NEW OAPEN books" in capsys.readouterr().out


def test_license_request_failure_propagates(monkeypatch):
    class FailedResponse:
        def raise_for_status(self):
            raise requests.HTTPError("service unavailable")

    monkeypatch.setattr(find_books.requests, "get", lambda *_args, **_kwargs: FailedResponse())

    with pytest.raises(requests.HTTPError, match="service unavailable"):
        find_books.license_of("book-handle")
