import sys

import pytest
import requests

import find_patents


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_patents.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_fetch_retries_transient_5xx_with_bounded_backoff(monkeypatch):
    calls = 0
    sleeps = []

    class Response:
        text = "recovered"
        status_code = 200

        def raise_for_status(self):
            nonlocal calls
            calls += 1
            if calls < 3:
                failed = requests.Response()
                failed.status_code = 503
                raise requests.HTTPError("service unavailable", response=failed)

    monkeypatch.setattr(find_patents.requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(find_patents.time, "sleep", sleeps.append)

    assert find_patents.fetch("https://example.test/sitemap.html") == "recovered"
    assert calls == 3
    assert sleeps == [2.0, 4.0]


def test_fetch_exhausts_transient_timeouts(monkeypatch):
    calls = 0
    sleeps = []

    def timeout(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.Timeout("timed out")

    monkeypatch.setattr(find_patents.requests, "get", timeout)
    monkeypatch.setattr(find_patents.time, "sleep", sleeps.append)

    with pytest.raises(requests.Timeout, match="timed out"):
        find_patents.fetch("https://example.test/sitemap.html")

    assert calls == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_fetch_does_not_retry_permanent_http_error(monkeypatch):
    calls = 0

    def not_found(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        failed = requests.Response()
        failed.status_code = 404
        raise requests.HTTPError("not found", response=failed)

    monkeypatch.setattr(find_patents.requests, "get", not_found)
    monkeypatch.setattr(
        find_patents.time,
        "sleep",
        lambda _delay: pytest.fail("permanent errors must not be retried"),
    )

    with pytest.raises(requests.HTTPError, match="not found"):
        find_patents.fetch("https://example.test/sitemap.html")

    assert calls == 1


def test_subpage_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)

    def fetch(url):
        if url.endswith("2020-W01.html"):
            return "href='2020-W01-p1.html'\nhref='2020-W01-p2.html'"
        if url.endswith("2020-W01-p1.html"):
            return "<li>US123B1 - Concrete foundation :"
        raise TimeoutError("offline")

    monkeypatch.setattr(find_patents, "fetch", fetch)
    monkeypatch.setattr(find_patents.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--bucket", "2020-W01", "--append"],
    )

    with pytest.raises(SystemExit) as exc:
        find_patents.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_bucket_is_not_a_fetch_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_patents, "fetch", lambda _url: "")
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda _entries: pytest.fail("an empty result must not append"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--bucket", "2020-W01", "--append"],
    )

    find_patents.main()

    assert "0 NEW built-environment US patents" in capsys.readouterr().out
