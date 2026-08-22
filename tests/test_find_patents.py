import sys

import pytest

import find_patents


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_patents.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


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
