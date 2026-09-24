import sys

import pytest

import find_ojs

BASE = "https://proceedings.open.tudelft.nl/clima2022"


def _record(n, title, rights=(), relations=None, deleted=False, lang="eng"):
    if deleted:
        return (f'<record><header status="deleted"><identifier>oai:x:article/{n}</identifier>'
                f"</header></record>")
    relations = [f"{BASE}/article/view/{n}/{n + 10}"] if relations is None else relations
    rights_xml = "".join(f"<dc:rights>{r}</dc:rights>" for r in rights)
    rel_xml = "".join(f"<dc:relation>{r}</dc:relation>" for r in relations)
    return (f"<record><header><identifier>oai:x:article/{n}</identifier></header><metadata>"
            f'<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title xml:lang="en-US">{title}</dc:title><dc:date>2022-04-21</dc:date>'
            f"<dc:identifier>{BASE}/article/view/{n}</dc:identifier>"
            f"<dc:identifier>10.34641/clima.2022.{n}</dc:identifier>"
            f"<dc:language>{lang}</dc:language>{rel_xml}{rights_xml}"
            f"</oai_dc:dc></metadata></record>")


def _page(records, token=""):
    tok = f'<resumptionToken cursor="0" completeListSize="9">{token}</resumptionToken>'
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            f"<ListRecords>{''.join(records)}{tok}</ListRecords></OAI-PMH>")


PAGE1 = _page([
    _record(1, "Deleted", deleted=True),
    _record(28, "From Diesel to Electric to NZEB in a Hotel",
            ["Copyright (c) 2022 J. Raposo", "https://creativecommons.org/licenses/by/4.0"]),
    _record(29, "Heat Pumps with R290", ["https://creativecommons.org/licenses/by-nc/4.0"]),
    _record(30, "Author Copyright Only", ["Copyright (c) 2022 A. Author"]),
], token="tok-2")
PAGE2 = _page([
    _record(31, "Radiant Ceiling Panels", ["https://creativecommons.org/licenses/by-sa/4.0/"]),
    _record(32, "No Galley Record", ["https://creativecommons.org/licenses/by/4.0"],
            relations=[]),
    _record(33, "Ventilation in Schools", ["https://creativecommons.org/publicdomain/zero/1.0/"],
            lang="nld"),
])


@pytest.mark.parametrize(("rights", "expected"), [
    (["https://creativecommons.org/licenses/by/4.0"], "cc-by"),
    (["http://creativecommons.org/licenses/by-sa/3.0/"], "cc-by-sa"),
    (["https://creativecommons.org/publicdomain/zero/1.0/"], "cc0"),
    (["https://creativecommons.org/publicdomain/mark/1.0/"], "public-domain"),
    (["https://creativecommons.org/licenses/by-nc/4.0"], None),
    (["https://creativecommons.org/licenses/by-nd/4.0"], None),
    (["https://creativecommons.org/licenses/by-nc-sa/4.0"], None),
    (["Copyright (c) 2022 Author"], None),
    ([], None),
])
def test_license_gate_is_fail_closed(rights, expected):
    assert find_ojs.license_for(rights)[0] == expected


def test_galley_view_becomes_direct_download_of_the_same_journal_only():
    assert (find_ojs.galley_download(BASE, [f"{BASE}/article/view/28/38"])
            == f"{BASE}/article/download/28/38")
    assert find_ojs.galley_download(BASE, [f"{BASE}/article/view/28"]) is None
    assert find_ojs.galley_download(BASE, ["https://other.org/j/article/view/1/2"]) is None


def _mock_pages(monkeypatch, pages):
    calls = []

    def fetch(base, token):
        calls.append(token)
        return pages[len(calls) - 1]

    monkeypatch.setattr(find_ojs, "fetch_page", fetch)
    monkeypatch.setattr(find_ojs.time, "sleep", lambda _s: None)
    return calls


def test_harvest_pages_gates_and_dedups(monkeypatch):
    calls = _mock_pages(monkeypatch, [PAGE1, PAGE2])
    site = find_ojs.SITES[0]
    known_urls = {f"{BASE}/article/download/31/41"}  # already registered

    out, scanned, complete = find_ojs.harvest(site, known_urls, set(), maxn=100)

    assert calls == ["", "tok-2"]
    assert complete and scanned == 6  # deleted records are not scanned
    assert [e["title"] for e in out] == ["From Diesel to Electric to NZEB in a Hotel",
                                         "Ventilation in Schools"]
    first, second = out
    assert first["id"] == "ojs-clima2022-from-diesel-to-electric-to-nzeb-in-a-hotel"
    assert first["url"] == f"{BASE}/article/download/28/38"
    assert first["license"] == "cc-by" and first["format"] == "pdf"
    assert first["license_url"] == "https://creativecommons.org/licenses/by/4.0"
    assert first["persistent_id"] == "https://doi.org/10.34641/clima.2022.28"
    assert first["language"] == "en" and first["published_at"] == "2022-04-21"
    assert "verb=GetRecord" in first["license_evidence"]
    assert second["license"] == "cc0" and second["language"] == "nl"
    assert find_ojs.registry.shard_path(first["id"]).name == "ojs.yaml"


def test_capped_run_requests_rotation_hold(monkeypatch, tmp_path, capsys):
    _mock_pages(monkeypatch, [PAGE1, PAGE2])
    monkeypatch.setattr(find_ojs.registry, "existing_keys", lambda: (set(), set(), set()))
    appended = []
    monkeypatch.setattr(find_ojs.registry, "append_entries", appended.extend)
    hold = tmp_path / "hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_ojs.py", "--site", "0", "--max", "1", "--append"])

    find_ojs.main()

    assert len(appended) == 1 and hold.exists()
    assert "site not finished" in capsys.readouterr().out


def test_completed_site_advances_without_hold(monkeypatch, tmp_path):
    _mock_pages(monkeypatch, [PAGE1, PAGE2])
    monkeypatch.setattr(find_ojs.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_ojs.registry, "append_entries", lambda _e: {})
    hold = tmp_path / "hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_ojs.py", "--site", "0", "--append"])

    find_ojs.main()

    assert not hold.exists()


def test_index_past_the_end_reports_exhaustion(monkeypatch, tmp_path):
    monkeypatch.setattr(find_ojs, "fetch_page",
                        lambda *_a: pytest.fail("no request past the last venue"))
    exhausted = tmp_path / "exhausted"
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(exhausted))
    monkeypatch.setattr(sys, "argv", ["find_ojs.py", "--site", str(len(find_ojs.SITES))])

    find_ojs.main()

    assert "OJS venues harvested" in exhausted.read_text()


def test_oai_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    monkeypatch.setattr(find_ojs.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_ojs, "fetch_page",
                        lambda *_a: (_ for _ in ()).throw(TimeoutError("offline")))
    monkeypatch.setattr(find_ojs.registry, "append_entries",
                        lambda _e: pytest.fail("partial results must not be appended"))
    monkeypatch.setattr(sys, "argv", ["find_ojs.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_ojs.main()

    assert exc.value.code == 1
    assert "refusing a partial append" in capsys.readouterr().err


def test_site_keys_are_unique_and_sources_are_stable():
    keys = [s["key"] for s in find_ojs.SITES]
    assert len(keys) == len(set(keys))
    assert keys[:2] == ["clima2022", "jfde"]  # rotation indexes into this append-only list


def test_control_characters_in_abstracts_do_not_fail_the_page():
    page = PAGE2.replace("Radiant Ceiling Panels", "Radiant step-by\x02step Panels")
    records, token = find_ojs.parse_page(page)
    assert records[0]["title"] == "Radiant step-by step Panels" and token == ""
