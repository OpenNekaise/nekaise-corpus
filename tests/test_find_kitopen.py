import sys

import pytest

import find_kitopen


def _record(title, suffix="1"):
    return {
        "title": title,
        "rights": ["https://creativecommons.org/licenses/by/4.0/"],
        "identifiers": [f"https://publikationen.bibliothek.kit.edu/1000/{suffix}"],
    }


def _empty_registry(monkeypatch):
    monkeypatch.setattr(find_kitopen.registry, "existing_keys", lambda: (set(), set(), set()))


@pytest.mark.parametrize(
    ("rights", "expected"),
    [
        (["https://creativecommons.org/publicdomain/zero/1.0/"], "cc0"),
        (["https://creativecommons.org/licenses/by-sa/4.0/"], "cc-by-sa"),
        (["https://creativecommons.org/licenses/by/4.0/"], "cc-by"),
        (["https://creativecommons.org/licenses/by-nc/4.0/"], None),
        (["info:eu-repo/semantics/openAccess"], None),
        (["KITopen License"], None),
    ],
)
def test_license_gate_is_redistributable_and_fail_closed(rights, expected):
    assert find_kitopen.license_for(rights) == expected


def test_fulltext_url_rejects_landing_pages_and_other_hosts():
    assert find_kitopen.fulltext_url([
        "https://doi.org/10.1/example",
        "https://publikationen.bibliothek.kit.edu/1000196250",
    ]) is None
    direct = "https://publikationen.bibliothek.kit.edu/1000196250/187612997"
    assert find_kitopen.fulltext_url([direct]) == direct


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_kitopen,
        "fetch_page",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_kitopen.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_kitopen.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_kitopen.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_candidate_cap_finishes_page_and_reports_opaque_next_cursor(
    monkeypatch, tmp_path, capsys
):
    _empty_registry(monkeypatch)
    records = [
        _record("Urban building analysis", "1"),
        _record("Concrete architecture", "2"),
        _record("Timber construction", "3"),
    ]
    monkeypatch.setattr(find_kitopen, "fetch_page", lambda *_args: "page")
    monkeypatch.setattr(find_kitopen, "parse_page", lambda _text: (records, "opaque-token"))
    monkeypatch.setattr(find_kitopen.time, "sleep", lambda _seconds: None)
    appended = []
    monkeypatch.setattr(find_kitopen.registry, "append_entries", appended.extend)
    next_file = tmp_path / "next"
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(next_file))
    monkeypatch.setattr(
        sys, "argv", ["find_kitopen.py", "--max", "1", "--token", "START", "--append"]
    )

    find_kitopen.main()

    assert len(appended) == 3
    assert next_file.read_text() == "opaque-token\n"
    assert "kept 3/3 scanned" in capsys.readouterr().out


def test_exhausted_set_reports_end_and_requests_backend_disable(monkeypatch, tmp_path):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_kitopen, "fetch_page", lambda *_args: "page")
    monkeypatch.setattr(find_kitopen, "parse_page", lambda _text: ([], ""))
    monkeypatch.setattr(find_kitopen.time, "sleep", lambda _seconds: None)
    next_file = tmp_path / "next"
    exhausted_file = tmp_path / "exhausted"
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(next_file))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(exhausted_file))
    monkeypatch.setattr(sys, "argv", ["find_kitopen.py", "--set", "ddc:720"])

    find_kitopen.main()

    assert next_file.read_text() == "END\n"
    assert exhausted_file.read_text() == "KITopen set ddc:720 fully harvested\n"
