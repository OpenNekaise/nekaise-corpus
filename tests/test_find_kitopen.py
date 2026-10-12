import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import find_kitopen as fk
import lint_registry
import registry

CC_BY = "https://creativecommons.org/licenses/by/4.0/deed.de"
FLAG = "info:eu-repo/semantics/openAccess"
KITOPEN = "KITopen License, https://publikationen.bibliothek.kit.edu/kitopen-lizenz"


def _record(title, n, rights=(FLAG, CC_BY), fulltext=True, deleted=False, extra=""):
    status = ' status="deleted"' if deleted else ""
    ids = [f"https://publikationen.bibliothek.kit.edu/{n}"]
    if fulltext:
        ids.append(f"https://publikationen.bibliothek.kit.edu/{n}/9{n}")
    body = "".join(f"<dc:identifier>{i}</dc:identifier>" for i in ids)
    body += "".join(f"<dc:rights>{r}</dc:rights>" for r in rights)
    meta = ("" if deleted else f"""<metadata>
<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
           xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>{title}</dc:title>{body}{extra}</oai_dc:dc></metadata>""")
    return f"<record><header{status}><identifier>oai:x:{n}</identifier></header>{meta}</record>"


def _page(records, token=None):
    token_xml = f"<resumptionToken>{token}</resumptionToken>" if token else "<resumptionToken/>"
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f"<ListRecords>{''.join(records)}{token_xml}</ListRecords></OAI-PMH>")


def _error(code):
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f'<error code="{code}">message</error></OAI-PMH>')


class _Clock:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)


def _setup(monkeypatch, tmp_path, pages, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(pages)

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        assert url == "https://publikationen.bibliothek.kit.edu/oai"  # not the excluded /oai/
        assert kwargs["allow_redirects"] is False
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, text=item)

    monkeypatch.setattr(fk, "datetime", _Clock)
    monkeypatch.setattr(fk.requests, "get", get)
    monkeypatch.setattr(fk.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(fk.dedup, "open_keys", lambda: fk.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(fk.registry, "append_entries", appended.extend)
    files = {n: tmp_path / n for n in ("next", "exhausted", "hold")}
    for env, name in (("NEKAISE_ROTATION_NEXT_FILE", "next"),
                      ("NEKAISE_BACKEND_EXHAUSTED_FILE", "exhausted"),
                      ("NEKAISE_ROTATION_HOLD_FILE", "hold")):
        monkeypatch.setenv(env, str(files[name]))
    return calls, appended, files


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["find_kitopen.py", *argv])
    fk.main()


@pytest.mark.parametrize(("rights", "expected"), [
    (["https://creativecommons.org/publicdomain/zero/1.0/"], "cc0"),
    ([FLAG, "https://creativecommons.org/licenses/by-sa/4.0/deed.de"], "cc-by-sa"),
    ([FLAG, CC_BY], "cc-by"),
    ([FLAG, "https://creativecommons.org/licenses/by-nc/4.0/deed.de"], "cc-by-nc"),
    ([FLAG, "https://creativecommons.org/licenses/by-nc-nd/4.0/deed.de"], "cc-by-nc-nd"),
    (["https://creativecommons.org/licenses/by-nd/3.0/deed.de"], "cc-by-nd"),
    (["https://creativecommons.org/licenses/by-nc-sa/4.0/"], "cc-by-nc-sa"),
    ([FLAG, KITOPEN], "publisher-oa"),
    ([FLAG], "unverified"),
    ([], "unverified"),
    (["KITopen License"], "unverified"),                       # not the exact statement
    ([CC_BY, KITOPEN], "unverified"),                          # conflicting statements
    ([CC_BY, "https://creativecommons.org/licenses/by-nc/4.0/"], "unverified"),
    (["CC BY-like terms"], "unverified"),
    (["https://example.org/licenses/by/4.0/"], "unverified"),  # CC-looking path, other host
    (["https://creativecommons.org/licenses/by-nc/4.0/", "All rights reserved"], "unverified"),
])
def test_licence_classes_are_exact_and_fail_closed(rights, expected):
    tag, url, evidence = fk.license_for(rights)
    assert tag == expected and evidence
    assert tag in lint_registry.LICENSES
    assert (url is None) == (tag == "unverified")


def test_fulltext_url_rejects_landing_pages_and_other_hosts():
    assert fk.fulltext_url([
        "https://doi.org/10.1/example",
        "https://publikationen.bibliothek.kit.edu/1000196250",
    ]) is None
    direct = "https://publikationen.bibliothek.kit.edu/1000196250/187612997"
    assert fk.fulltext_url([direct]) == direct


def test_candidate_carries_metadata_and_lints():
    extra = ("<dc:type>doc-type:doctoralThesis</dc:type><dc:language>ger</dc:language>"
             "<dc:date>2025-03-01</dc:date>"
             "<dc:relation>info:eu-repo/semantics/altIdentifier/doi/10.5445/IR/1000</dc:relation>")
    records, token = fk.parse_page(_page([
        _record("Tragverhalten von Holzbetonverbunddecken", 1, rights=(FLAG, KITOPEN),
                extra=extra),
        _record("Landing page only", 2, fulltext=False),
        _record("Deleted", 3, deleted=True),
    ], "tok"))
    assert token == "tok" and len(records) == 2
    got = [fk.candidate(r, "architecture", "2026-10-12") for r in records]
    assert got[1] is None
    entry = got[0]
    assert entry["license"] == "publisher-oa" and entry["license_url"].endswith("kitopen-lizenz")
    assert entry["document_type"] == "thesis" and entry["language"] == "de"
    assert entry["published_at"] == "2025-03-01"
    assert entry["persistent_id"] == "doi:10.5445/ir/1000"
    assert entry["topic"] == "materials" and entry["rights_verified_at"] == "2026-10-12"
    assert registry.shard_filename(entry["id"]) == "kitopen.yaml"
    assert not lint_registry.entry_errors(entry, "kitopen.yaml")


def test_walk_follows_token_then_next_set_then_becomes_a_due_watch(monkeypatch, tmp_path):
    known = ({"https://publikationen.bibliothek.kit.edu/1/91"}, set(), set())
    pages = [_page([_record("Held already", 1), _record("Stadtquartier", 2)], "tok-2"),
             _page([_record("Fassade", 3, rights=(FLAG,))]),
             _page([_record("Stahlbetonbrücke", 4, rights=(FLAG, KITOPEN))])]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages, known)

    _run(monkeypatch, "--cursor", "0:START", "--pages", "2", "--append")
    made = [c for c in calls if c != "sleep"]
    assert made[0] == {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": "ddc:720"}
    assert made[1] == {"verb": "ListRecords", "resumptionToken": "tok-2"}
    assert calls.count("sleep") == 1
    assert files["next"].read_text() == "walk:2026-10-12:1:START\n"  # start date persisted

    # the walk was interrupted for a long time: its start date still bounds the first watch
    _run(monkeypatch, "--cursor", "walk:2026-07-01:1:START", "--pages", "2", "--append")
    made = [c for c in calls if c != "sleep"]
    assert made[2]["set"] == "ddc:690" and "from" not in made[2]
    assert [e["license"] for e in appended] == ["cc-by", "unverified", "publisher-oa"]
    assert [e["topic"] for e in appended] == ["urban", "architecture", "materials"]
    # walked: the first watch pass reads everything since the walk's own start date
    assert files["next"].read_text() == "watch:2026-07-01\n"
    assert not files["exhausted"].exists()


def test_watch_not_due_makes_no_request(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    _run(monkeypatch, "--cursor", "watch:2026-10-06")
    assert calls == [] and files["next"].read_text() == "watch:2026-10-06\n"


@pytest.mark.parametrize("cursor,since", [("watch:2026-07-01", "2026-06-29"),
                                          ("watch:2026-10-05", "2026-10-03")])
def test_due_watch_pass_bounds_every_set_by_date_then_waits(monkeypatch, tmp_path, cursor,
                                                            since):
    pages = [_page([_record("Neues Quartier", 5)], "t"), _error("noRecordsMatch"),
             _page([_record("Neue Brücke", 6)])]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)

    _run(monkeypatch, "--cursor", cursor, "--pages", "1", "--append")
    first = files["next"].read_text().strip()
    assert first.startswith("watch:") and first.endswith(":2026-10-12:0:t")
    assert calls[0]["set"] == "ddc:720" and calls[0]["from"] == since

    _run(monkeypatch, "--cursor", first, "--pages", "5", "--append")
    made = [c for c in calls if c != "sleep"]
    # token page -> badly empty would HOLD; here the token page is the noRecordsMatch error
    assert made[1] == {"verb": "ListRecords", "resumptionToken": "t"}
    assert files["hold"].exists()  # an error on a continuation page is not "finished"
    assert not (tmp_path / "exhausted").exists()


def test_watch_pass_end_records_its_start_date(monkeypatch, tmp_path):
    pages = [_error("noRecordsMatch"), _page([_record("Neue Brücke", 6)])]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    _run(monkeypatch, "--cursor", "watch:2026-10-01:2026-10-10:0:START", "--append")
    made = [c for c in calls if c != "sleep"]
    assert [c["set"] for c in made] == ["ddc:720", "ddc:690"]
    assert all(c["from"] == "2026-09-29" for c in made)  # since minus OVERLAP_DAYS
    assert len(appended) == 1
    assert files["next"].read_text() == "watch:2026-10-10\n"


def test_rejected_token_restarts_its_set_once(monkeypatch, tmp_path):
    pages = [_error("badResumptionToken"), _page([_record("Fassade", 3)], "fresh")]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    _run(monkeypatch, "--cursor", "1:stale", "--pages", "2", "--append")
    made = [c for c in calls if c != "sleep"]
    assert made[1] == {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": "ddc:690"}
    assert len(appended) == 1 and files["next"].read_text() == "walk:2026-10-12:1:fresh\n"


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, text="maintenance"),
    SimpleNamespace(status_code=301, text=""),   # a redirect is never followed
    "<html><body>challenge</body></html>",
    "not xml at all",
    _page([]),                                   # empty ListRecords proves nothing
    _error("badArgument"),
])
def test_unexpected_answer_holds_and_proposes_nothing(monkeypatch, tmp_path, answer):
    pages = [_page([_record("Fassade", 3)], "tok"), answer]
    _, appended, files = _setup(monkeypatch, tmp_path, pages)
    _run(monkeypatch, "--cursor", "0:START", "--pages", "2", "--append")
    assert appended == [] and files["hold"].read_text().strip()
    assert not files["next"].exists() and not files["exhausted"].exists()


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, tmp_path, capsys):
    pages = [_page([_record("Fassade", 3)], "tok"), TimeoutError("offline")]
    _, appended, files = _setup(monkeypatch, tmp_path, pages)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--cursor", "0:START", "--pages", "2", "--append")
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_candidate_cap_finishes_page_and_keeps_opaque_token(monkeypatch, tmp_path, capsys):
    records = [_record("Urban building analysis", 1), _record("Concrete architecture", 2),
               _record("Timber construction", 3)]
    _, appended, files = _setup(monkeypatch, tmp_path, [_page(records, "opaque:token/1")])
    _run(monkeypatch, "--cursor", "0:START", "--max", "1", "--append")
    assert len(appended) == 3
    assert files["next"].read_text() == "walk:2026-10-12:0:opaque:token/1\n"
    assert fk.parse_cursor("walk:2026-10-12:0:opaque:token/1")["token"] == "opaque:token/1"
    assert "kept 3/3 scanned" in capsys.readouterr().out


@pytest.mark.parametrize("cursor", ["", "START", "END", "x:START", "watch",
                                    "watch:2026-13-01", "watch:2026-10-01:2026-10-02",
                                    "watch:2026-10-01:0:START", "walk:2026-13-01:0:START",
                                    # a set index outside SETS is an error, not a finished walk
                                    "2:START", "99:START", "walk:2026-10-01:2:START",
                                    "watch:2026-10-01:2026-10-02:2:START"])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        fk.parse_cursor(cursor)


def test_cursor_round_trip():
    for text in ("walk:2026-10-01:0:START", "walk:2026-10-01:1:tok", "watch:2026-10-01",
                 "watch:2026-10-01:2026-10-12:1:START", "watch:2026-10-01:2026-10-12:0:a:b"):
        assert fk.cursor_text(fk.parse_cursor(text)) == text
    today = fk.date(2026, 10, 12)
    assert fk.cursor_text(fk.parse_cursor("0:START", today)) == "walk:2026-10-12:0:START"


def test_out_of_range_set_index_makes_no_request(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--cursor", "99:START")
    assert exc.value.code == 2 and calls == [] and not files["next"].exists()
