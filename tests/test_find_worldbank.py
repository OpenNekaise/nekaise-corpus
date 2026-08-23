import sys

import pytest

import find_worldbank


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_worldbank.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_worldbank,
        "fetch_page",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_worldbank.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_worldbank.py", "--append", "--pages", "1"],
    )

    with pytest.raises(SystemExit) as exc:
        find_worldbank.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_page_is_not_an_api_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_worldbank, "fetch_page", lambda *_args: (2060, []))
    monkeypatch.setattr(
        find_worldbank.registry,
        "append_entries",
        lambda _entries: pytest.fail("an empty result must not append"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_worldbank.py", "--append", "--os", "5000", "--pages", "1"],
    )

    find_worldbank.main()

    assert "0 NEW World Bank docs" in capsys.readouterr().out


def test_wds_results_are_not_blanket_labeled_cc_by(monkeypatch):
    _empty_registry(monkeypatch)
    captured = []
    monkeypatch.setattr(
        find_worldbank,
        "fetch_page",
        lambda *_args: (1, [{
            "title": "Urban Water Infrastructure Assessment",
            "pdf_url": "https://documents.worldbank.org/example.pdf",
            "docty": "Environmental Assessment",
        }]),
    )
    monkeypatch.setattr(find_worldbank.registry, "append_entries", captured.extend)
    monkeypatch.setattr(find_worldbank.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_worldbank.py", "--append", "--pages", "1"],
    )

    find_worldbank.main()

    assert len(captured) == 1
    assert captured[0]["source"] == "worldbank_wds"
    assert captured[0]["license"] == "open"
