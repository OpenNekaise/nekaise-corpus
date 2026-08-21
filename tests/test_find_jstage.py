import sys
import xml.etree.ElementTree as ET

import pytest

import find_jstage


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_jstage.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_fetch_page_preserves_missing_total_for_past_end_sentinel(monkeypatch):
    response = type("Response", (), {
        "content": b"""
            <feed xmlns="http://www.w3.org/2005/Atom"
                  xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
              <opensearch:totalResults />
              <entry><title /><link /><id /><updated /></entry>
            </feed>
        """,
        "raise_for_status": lambda self: None,
    })()
    monkeypatch.setattr(find_jstage.requests, "get", lambda *_args, **_kwargs: response)

    total, entries = find_jstage.fetch_page("journal", 3201, 100)

    assert total is None
    assert len(entries) == 1
    assert find_jstage.parse(entries[0]) is None


def test_past_end_sentinel_exits_nonzero_before_rotation_can_advance(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    sentinel = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom"><title /><link /></entry>'
    )
    monkeypatch.setattr(find_jstage, "fetch_page", lambda *_args: (None, [sentinel]))
    monkeypatch.setattr(
        find_jstage.registry,
        "append_entries",
        lambda _entries: pytest.fail("an exhausted series must not append"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_jstage.py", "--series", "gijutsu", "--start", "3201", "--append"],
    )

    with pytest.raises(SystemExit) as exc:
        find_jstage.main()

    assert exc.value.code == 1
    assert "refusing rotation advance" in capsys.readouterr().err


def test_successful_page_reports_live_total(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_jstage, "fetch_page", lambda *_args: (3171, []))
    monkeypatch.setattr(find_jstage.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sys, "argv", ["find_jstage.py", "--series", "gijutsu", "--count", "1"])

    find_jstage.main()

    assert "0 scanned of 3171 total" in capsys.readouterr().out
