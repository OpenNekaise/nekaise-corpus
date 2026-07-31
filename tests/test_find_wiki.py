import sys

import pytest

import find_wiki


def test_category_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        find_wiki,
        "api_get",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )

    with pytest.raises(SystemExit) as exc:
        find_wiki.walk_categories(
            object(),
            "de",
            [("Bauwesen", "construction")],
            depth=1,
            budget=10,
        )

    assert exc.value.code == 1


def test_langlinks_failure_exits_nonzero_before_partial_append(monkeypatch):
    monkeypatch.setattr(find_wiki.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_wiki, "origin_titles", lambda: {"Building": "construction"})
    monkeypatch.setattr(
        find_wiki,
        "api_get",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_wiki.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_wiki.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_wiki.main()

    assert exc.value.code == 1
