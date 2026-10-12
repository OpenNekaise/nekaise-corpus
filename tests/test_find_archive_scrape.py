import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import find_archive_scrape as fas
import lint_registry
import registry
import run_round
import store

BTHL = "buildingtechnologyheritagelibrary"
PDM = "http://creativecommons.org/publicdomain/mark/1.0/"
CC_BY = "http://creativecommons.org/licenses/by/3.0/"
CC_BY_NC = "https://creativecommons.org/licenses/by-nc/4.0/"
QUERY = ("query", "steam heating", "equipment_systems")
REAL_APPEND = registry.append_entries


def _item(identifier, added, title="Steam heating boilers", **extra):
    return {"identifier": identifier, "title": title, "mediatype": "texts",
            "collection": [BTHL, "catalogs"], "addeddate": added,
            "format": ["DjVuTXT", "Metadata"], **extra}


def _meta(identifier, name=None, size=50000, **record):
    return {"files": [{"name": "scan.pdf", "format": "Image Container PDF", "size": "9"},
                      {"name": name or f"{identifier}_djvu.txt", "format": "DjVuTXT",
                       "size": str(size)}],
            "metadata": {"identifier": identifier, "title": "Steam heating boilers", **record}}


def _setup(monkeypatch, tmp_path, pages, metas=None, known=None):
    """pages: scrape answers in order ({'items': [...], 'cursor': True for "more pages"}, a
    list of items, an Exception or a response), or a function of the request parameters
    (_serve); metas: {identifier: metadata answer}."""
    known = known or (set(), set(), set())
    metas, calls = metas or {}, []
    queue = None if callable(pages) else iter(pages)

    def get(url, params=None, **_kwargs):
        if url.startswith(fas.METADATA):
            identifier = url[len(fas.METADATA):]
            calls.append(("meta", identifier))
            answer = metas[identifier]
        else:
            calls.append(("scrape", params))
            answer = pages(params) if queue is None else next(queue)
            if isinstance(answer, list):
                answer = {"items": answer}
            if isinstance(answer, dict) and answer.get("cursor") is True:  # the real page cursor
                last = answer["items"][-1]
                answer = {**answer, "cursor": fas.seek_cursor(last["addeddate"],
                                                               last["identifier"])}
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, SimpleNamespace):
            return answer
        return SimpleNamespace(status_code=200, json=lambda answer=answer: answer)

    monkeypatch.setattr(fas.requests, "get", get)
    monkeypatch.setattr(fas.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(fas.dedup, "open_keys", lambda: fas.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(fas.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    for path in files.values():
        path.unlink(missing_ok=True)
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    return calls, appended, files


def _serve(items, count):
    """A scrape service over `items` (in service order) that honours the page cursor: the
    answer starts after the cursor's key, `count` items per page."""
    def answer(params):
        if BTHL not in params["q"]:
            return []  # the other targets hold nothing
        rows = items
        if cursor := params.get("cursor"):
            key = fas.cursor_key(cursor)
            assert key is not None, "the finder sent a cursor the service does not understand"
            rows = [i for i in items if (i["addeddate"], i["identifier"]) > key]
        page = rows[:count]
        return {"items": page, **({"cursor": True} if len(rows) > count else {})}
    return answer


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["find_archive_scrape.py", *argv])
    fas.main()


def _lookups(calls):
    return [c[1] for c in calls if c != "sleep" and c[0] == "meta"]


def _scrapes(calls):
    return [c[1] for c in calls if c != "sleep" and c[0] == "scrape"]


# --- licence --------------------------------------------------------------------------------------

@pytest.mark.parametrize("record, tag", [
    ({"licenseurl": PDM, "date": "1952"}, "public-domain"),
    ({"licenseurl": "https://creativecommons.org/publicdomain/zero/1.0/"}, "cc0"),
    ({"licenseurl": CC_BY, "date": "1952"}, "cc-by"),
    ({"licenseurl": "http://creativecommons.org/licenses/by-sa/4.0/"}, "cc-by-sa"),
    ({"licenseurl": "http://creativecommons.org/licenses/by-nd/3.0/", "date": "1910"}, "cc-by-nd"),
    ({"licenseurl": CC_BY_NC, "date": "1910"}, "cc-by-nc"),
    ({"licenseurl": "http://creativecommons.org/licenses/by-nc-nd/3.0/"}, "cc-by-nc-nd"),
    ({"licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/legalcode"}, "cc-by-nc"),
    ({"licenseurl": "https://creativecommons.org/licenses/by-nd/4.0/deed.en"}, "cc-by-nd"),
    ({"licenseurl": "http://creativecommons.org/licenses/by-nc-sa/3.0/us/"}, "cc-by-nc-sa"),
    ({"licenseurl": "http://creativecommons.org/licenses/by-nc-nd/2.5/ca/legalcode.fr"},
     "cc-by-nc-nd"),
    ({"licenseurl": CC_BY, "rights": "https://creativecommons.org/licenses/by-nc/4.0/legalcode"},
     "unverified"),
    ({"licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/legalcode/extra"},
     "unverified"),
    ({"licenseurl": "http://creativecommons.org/licenses/publicdomain/"}, "public-domain"),
    ({"licenseurl": "https://example.org/licenses/by/4.0/", "date": "1910"}, "unverified"),
    # every licence value counts: equal tags agree, different tags conflict
    ({"licenseurl": [PDM, "https://creativecommons.org/publicdomain/mark/1.0/"]}, "public-domain"),
    ({"licenseurl": [CC_BY, CC_BY_NC]}, "unverified"),
    ({"licenseurl": [CC_BY_NC, CC_BY]}, "unverified"),
    ({"licenseurl": [CC_BY, PDM], "date": "1910"}, "unverified"),
    ({"licenseurl": [CC_BY, "see the publisher"], "date": "1910"}, "unverified"),
    ({"licenseurl": ["", CC_BY]}, "cc-by"),
    # `rights` and `possible-copyright-status` values are rights statements too
    ({"licenseurl": CC_BY, "rights": CC_BY_NC}, "unverified"),
    ({"licenseurl": CC_BY_NC, "rights": CC_BY}, "unverified"),
    ({"licenseurl": CC_BY, "possible-copyright-status": CC_BY_NC}, "unverified"),
    ({"licenseurl": CC_BY, "rights": CC_BY}, "cc-by"),
    ({"rights": CC_BY_NC, "date": "1910"}, "cc-by-nc"),
    ({"licenseurl": CC_BY, "rights": "Not licensed under Creative Commons"}, "unverified"),
    ({"licenseurl": CC_BY, "rights": "CC BY-NC"}, "unverified"),
    ({"licenseurl": CC_BY, "rights": "Non-commercial use only"}, "unverified"),
    ({"licenseurl": CC_BY, "rights": "Contact the library"}, "unverified"),
    ({"licenseurl": PDM, "rights": "All rights reserved"}, "unverified"),
    ({"licenseurl": PDM, "rights": "Not public domain"}, "unverified"),
    ({"licenseurl": PDM, "rights": ["Public domain", "In copyright"]}, "unverified"),
    ({"licenseurl": CC_BY_NC, "rights": "All rights reserved"}, "unverified"),
    ({"date": "1910", "possible-copyright-status": "IN_COPYRIGHT"}, "unverified"),
    ({"date": "1910", "rights": "Copyright 1910 by the Acme Company"}, "unverified"),
    ({"date": "1910", "rights": "Not in the public domain"}, "unverified"),
    ({"date": "1910", "possible-copyright-status": "NOT_IN_COPYRIGHT"}, "public-domain"),
    ({"date": "1910", "rights": "Public domain."}, "public-domain"),
    ({"date": "1935", "rights": "Public domain"}, "unverified"),  # a statement is not a licence
    ({"licenseurl": CC_BY, "rights": "copyright not renewed"}, "cc-by"),
    ({"licenseurl": PDM, "rights": "Pre-1923 document in public domain"}, "public-domain"),
    ({"date": "1915", "rights": "Published pre-1923, in the public domain"}, "public-domain"),
    ({"date": "1915", "rights": "No copyright date"}, "unverified"),
    # no licence: only certain dates, every value, latest year
    ({"date": "1927"}, "public-domain"),
    ({"date": "1920-03-29"}, "public-domain"),
    ({"date": "1920-01-01T00:00:00Z", "year": "1920"}, "public-domain"),
    ({"date": "1898-1918"}, "public-domain"),
    ({"date": "1929"}, "unverified"),
    ({"date": "1925-1935"}, "unverified"),
    ({"date": "1925-35"}, "unverified"),
    ({"date": "1911-12"}, "unverified"),
    ({"date": "[1924]"}, "unverified"),
    ({"date": "c. 1920"}, "unverified"),
    ({"date": "ca. 1910"}, "unverified"),
    ({"date": "1920?"}, "unverified"),
    ({"date": "192-"}, "unverified"),
    ({"year": "circa 1920"}, "unverified"),
    ({"date": ["1920", "circa 1925"]}, "unverified"),
    ({"date": "1920", "year": "circa 1920"}, "unverified"),
    ({"date": ["1920", "1931"]}, "unverified"),
    ({"date": "0900"}, "unverified"),
    ({"date": "1935-1920"}, "unverified"),
    ({"date": "1920-02-30"}, "unverified"),                 # not a calendar date
    ({"date": "1920-13-01"}, "unverified"),
    ({"date": "1920-02-30T99:99:99Z"}, "unverified"),
    ({"date": "1920-02-28T25:00:00Z"}, "unverified"),
    ({"date": "1920-02-29T00:00:00Z"}, "public-domain"),    # 1920 was a leap year
    ({}, "unverified"),
])
def test_licence_reads_every_metadata_value(record, tag):
    got, evidence, licence_url = fas.licence(record)
    assert got == tag and got in lint_registry.LICENSES
    assert evidence.startswith("Internet Archive item metadata")
    for value in fas.date_values(record):
        assert value in evidence  # the complete date evidence is kept
    for field in ("licenseurl", "rights", "possible-copyright-status"):
        for value in fas._values(record.get(field)):
            assert value in evidence
    assert (licence_url is not None) == (
        tag != "unverified" and any("creativecommons.org" in v for f in ("licenseurl", "rights")
                                    for v in fas._values(record.get(f))))


# --- identity -------------------------------------------------------------------------------------

def test_ids_come_from_the_full_identifier():
    long_a = "proceedingsofamericansocietyofcivilengineersvolume41part1"
    long_b = "proceedingsofamericansocietyofcivilengineersvolume41part2"
    assert registry.slug(long_a)[:52] == registry.slug(long_b)[:52]  # the old id collided
    ids = {fas.item_id(i) for i in (long_a, long_b, "Foo_Catalog", "foo-catalog", "Foo.Catalog")}
    assert len(ids) == 5
    assert all(i.startswith("arc-") and registry.shard_filename(i) == "archive.yaml"
               for i in ids)
    assert fas.item_id(long_a) == fas.item_id(long_a)


def test_entry_resolves_the_real_text_file_name_and_classifies():
    target = fas.TARGETS[0]
    item = _item("GuthLighting", "2010-09-05T15:24:42Z", title="Guth lighting equipment")
    meta = _meta("GuthLighting", name="Guth Lighting Equip.1925._djvu.txt",
                 title="Guth lighting equipment", licenseurl=PDM, date="1925",
                 language="English", subject=["lighting", "Division 26"])
    entry = fas.entry_for(item, meta, target, "2026-10-12")
    assert entry["id"] == fas.item_id("GuthLighting")
    assert entry["url"] == ("https://archive.org/download/GuthLighting/"
                            "Guth%20Lighting%20Equip.1925._djvu.txt")
    assert entry["license"] == "public-domain" and entry["license_url"] == PDM
    assert entry["title"] == f"Guth lighting equipment (1925) [{entry['id']}]"
    assert entry["topic"] == "equipment_systems" and entry["language"] == "en"
    assert entry["format"] == "txt" and entry["source"] == "internet_archive"
    assert entry["document_type"] == "trade-literature" and entry["published_at"] == "1925"
    assert registry.shard_filename(entry["id"]) == "archive.yaml"
    assert not lint_registry.entry_errors(entry, "archive.yaml")
    # a file named the legacy way keeps the legacy URL form
    plain = fas.entry_for(_item("abc00def", "2010-09-05T15:24:42Z"), _meta("abc00def"),
                          target, "2026-10-12")
    assert plain["url"] == fas.legacy_url("abc00def")
    assert plain["license"] == "unverified" and "license_url" not in plain
    # an uncertain date is shown but is not rights evidence
    circa = fas.entry_for(_item("c1", "2010-09-05T15:24:42Z"), _meta("c1", date="ca. 1920"),
                          target, "2026-10-12")
    assert circa["published_at"] == "1920" and circa["license"] == "unverified"
    assert "ca. 1920" in circa["license_evidence"]


def test_query_target_titles_carry_the_volume_not_the_id():
    meta = _meta("procasce41", title="Proceedings", date="1915", volume="41")
    entry = fas.entry_for(_item("procasce41", "2010-09-05T15:24:42Z"), meta, QUERY, "d")
    assert entry["title"] == "Proceedings, v. 41 (1915)" and "document_type" not in entry
    assert entry["license"] == "public-domain"


@pytest.mark.parametrize("meta", [
    None,                                                     # gone or dark (fetch_metadata)
    _meta("x1", size=1500),                                   # below the thin floor
    {"files": [{"name": "x1_djvu.txt", "format": "DjVuTXT", "size": "90000",
                "private": "true"}], "metadata": {"identifier": "x1", "title": "T"}},
    {"files": [{"name": "x1.pdf", "format": "Text PDF", "size": "90000"}],
     "metadata": {"identifier": "x1", "title": "T"}},
])
def test_uncollectable_items_are_skipped(meta):
    item = _item("x1", "2010-09-05T15:24:42Z")
    assert fas.entry_for(item, meta, fas.TARGETS[0], "2026-10-12") is None


def test_metadata_for_another_item_is_unexpected():
    with pytest.raises(fas.Unexpected):
        fas.entry_for(_item("x1", "2010-09-05T15:24:42Z"), _meta("other"), fas.TARGETS[0], "d")


def test_wrong_query_guard():
    collection = fas.TARGETS[0]
    assert fas.applies(_item("a", "x"), collection)
    assert not fas.applies(_item("a", "x", collection=["opensource"]), collection)
    assert not fas.applies(_item("a", "x", mediatype="audio"), collection)
    assert fas.applies(_item("a", "x", title="Practical steam and hot water heating",
                             year=1905), QUERY)
    assert fas.applies(_item("a", "x", title="A manual", subject=["Steam-heating"],
                             date="1890-01-01T00:00:00Z"), QUERY)
    assert not fas.applies(_item("a", "x", title="Steam heating", year=1950), QUERY)
    assert not fas.applies(_item("a", "x", title="Steam heating"), QUERY)       # no date
    assert not fas.applies(_item("a", "x", title="Poultry keeping", year=1905), QUERY)


# --- the walk -------------------------------------------------------------------------------------

def test_walk_seeks_past_the_persisted_key_and_skips_known_items_without_lookup(monkeypatch,
                                                                              tmp_path):
    boundary = "2010-01-01T00:00:00Z"
    items = [_item("the-next", boundary),
             _item("known-id", "2010-01-02T00:00:00Z"),
             _item("legacy-row", "2010-01-02T00:00:00Z"),
             _item("no-text", "2010-01-03T00:00:00Z", format=["Metadata"]),
             _item("new-one", "2010-01-04T00:00:00Z"),
             _item("new-two", "2010-01-05T00:00:00Z")]
    known = ({fas.legacy_url("legacy-row")}, set(), {fas.item_id("known-id")})
    metas = {i: _meta(i) for i in ("the-next", "new-two")}
    metas["new-one"] = _meta("new-one", licenseurl=PDM)
    calls, appended, files = _setup(
        monkeypatch, tmp_path, [{"items": items, "cursor": True}], metas, known)
    _run(monkeypatch, "--cursor", f"t=0 a={boundary} i=the-marker", "--pages", "1", "--append")

    scrape = _scrapes(calls)[0]
    assert scrape["q"] == f"collection:{BTHL} AND mediatype:texts"
    assert scrape["sorts"] == "addeddate asc"  # naming the identifier breaks the service's paging
    assert fas.cursor_key(scrape["cursor"]) == (boundary, "the-marker")
    assert _lookups(calls) == ["the-next", "new-one", "new-two"]
    assert calls.count("sleep") == 3                     # 4 requests, paced
    assert [e["id"] for e in appended] == [fas.item_id(i) for i in
                                           ("the-next", "new-one", "new-two")]
    # the page budget ended inside the target: the cursor is the last item dealt with
    assert files["next"].read_text() == "t=0 a=2010-01-05T00:00:00Z i=new-two\n"
    assert not files["exhausted"].exists()


def test_seek_cursor_is_the_service_page_cursor_form():
    import base64
    service = base64.b64encode(b'[{"addeddate":"2010-09-19T11:45:18.0Z"},'
                               b'{"identifierSorter":"TheBookOf100HomeBookB"}]').decode()
    assert fas.seek_cursor("2010-09-19T11:45:18Z", "TheBookOf100HomeBookB") == service
    assert fas.cursor_key(service) == ("2010-09-19T11:45:18Z", "TheBookOf100HomeBookB")
    for other in ("W3siaWRlbnRpZmllciI6IngifV0=", "not base64 !", "", "e30="):
        assert fas.cursor_key(other) is None


def test_resume_does_not_need_the_marker_item_to_exist(monkeypatch, tmp_path):
    stamp = "2010-01-01T00:00:00Z"
    remaining = [_item("a", stamp), _item("c", stamp), _item("d", "2010-01-02T00:00:00Z")]
    metas = {i["identifier"]: _meta(i["identifier"]) for i in remaining}
    # "b" was dealt with and then deleted upstream; "a" sorts before it, "c" and "d" after
    calls, appended, files = _setup(monkeypatch, tmp_path, _serve(remaining, 10), metas)
    _run(monkeypatch, "--cursor", f"t=0 a={stamp} i=b", "--pages", "1", "--append")
    assert _lookups(calls) == ["c", "d"]
    assert [e["id"] for e in appended] == [fas.item_id("c"), fas.item_id("d")]
    assert files["next"].read_text() == "t=1 a=START\n"


def test_more_than_four_pages_of_one_timestamp_do_not_stall_the_walk(monkeypatch, tmp_path):
    stamp = "2010-01-01T00:00:00Z"
    monkeypatch.setattr(fas, "COUNT", 2)
    tied = [_item(f"tie{i:02d}", stamp) for i in range(11)]       # 6 pages of 2
    items = [*tied, _item("after", "2010-01-02T00:00:00Z")]
    known = (set(), set(), {fas.item_id(i["identifier"]) for i in tied})
    cursor, scanned, proposed = "t=0 a=START", [], []
    for _round in range(4):
        calls, appended, files = _setup(monkeypatch, tmp_path, _serve(items, 2),
                                        {"after": _meta("after")}, known)
        _run(monkeypatch, "--cursor", cursor, "--pages", "4", "--append")
        assert not files["hold"].exists() and len(_scrapes(calls)) <= 4
        new_cursor = files["next"].read_text().strip()
        assert new_cursor != cursor
        cursor = new_cursor
        proposed += [e["id"] for e in appended]
        scanned.append(cursor)
        if not cursor.startswith("t=0 "):
            break
    # 8 tied items per run: no run replays what an earlier run dealt with
    assert scanned == [f"t=0 a={stamp} i=tie07", "t=3 a=START"]
    assert proposed == [fas.item_id("after")]


def test_a_slug_collision_with_a_held_item_does_not_hide_another_identifier(monkeypatch,
                                                                             tmp_path):
    long_a = "proceedingsofamericansocietyofcivilengineersvolume41part1"
    long_b = "proceedingsofamericansocietyofcivilengineersvolume41part2"
    known = ({fas.legacy_url(long_a)}, set(), {f"arc-{registry.slug(long_a)[:52]}"})
    items = [_item(long_a, "2010-01-01T00:00:00Z"), _item(long_b, "2010-01-02T00:00:00Z")]
    calls, appended, _ = _setup(monkeypatch, tmp_path, [items], {long_b: _meta(long_b)}, known)
    _run(monkeypatch, "--cursor", "t=0 a=START", "--pages", "1", "--append")
    assert _lookups(calls) == [long_b]
    assert [e["id"] for e in appended] == [fas.item_id(long_b)]


def test_service_cursor_is_followed_inside_a_run_and_target_end_advances(monkeypatch, tmp_path):
    pages = [{"items": [_item("a1", "2010-01-01T00:00:00Z")], "cursor": True},
             {"items": [_item("a2", "2010-01-02T00:00:00Z")]},
             []]                                              # target 1: nothing
    metas = {"a1": _meta("a1"), "a2": _meta("a2")}
    calls, appended, files = _setup(monkeypatch, tmp_path, pages, metas)
    _run(monkeypatch, "--cursor", "t=0 a=START", "--pages", "3", "--append")
    scrapes = _scrapes(calls)
    assert "cursor" not in scrapes[0]
    assert fas.cursor_key(scrapes[1]["cursor"]) == ("2010-01-01T00:00:00Z", "a1")
    assert "cursor" not in scrapes[2]
    assert scrapes[2]["q"].startswith("collection:averylibrarytradecatalogs")
    assert len(appended) == 2
    assert files["next"].read_text() == "t=2 a=START\n"


def test_budgets_stop_before_the_next_lookup(monkeypatch, tmp_path):
    items = [_item(f"n{i}", f"2010-01-0{i}T00:00:00Z") for i in range(1, 6)]
    metas = {f"n{i}": _meta(f"n{i}") for i in range(1, 6)}
    calls, appended, files = _setup(monkeypatch, tmp_path, [items], metas)
    _run(monkeypatch, "--cursor", "t=0 a=START", "--lookups", "2", "--append")
    assert [e["id"] for e in appended] == [fas.item_id("n1"), fas.item_id("n2")]
    assert files["next"].read_text() == "t=0 a=2010-01-02T00:00:00Z i=n2\n"

    calls, appended, files = _setup(monkeypatch, tmp_path, [items], metas)
    _run(monkeypatch, "--cursor", "t=0 a=START", "--max", "3", "--append")
    assert len(appended) == 3
    assert files["next"].read_text() == "t=0 a=2010-01-03T00:00:00Z i=n3\n"


def test_uncollectable_items_sharing_one_timestamp_cannot_trap_the_cursor(monkeypatch, tmp_path):
    stamp = "2010-01-01T00:00:00Z"
    items = [_item(f"dark{i:02d}", stamp) for i in range(7)] + [_item("good", stamp)]
    metas = {i["identifier"]: {"is_dark": True} for i in items[:-1]}
    metas["dark03"] = {}                                   # gone
    metas["good"] = _meta("good")
    cursor, looked_up, proposed = "t=0 a=START", [], []
    for _round in range(5):  # each run may make 3 lookups; the tie holds 8 items
        calls, appended, files = _setup(monkeypatch, tmp_path, _serve(items, 500), metas)
        _run(monkeypatch, "--cursor", cursor, "--lookups", "3", "--pages", "1", "--append")
        assert not files["hold"].exists()
        new_cursor = files["next"].read_text().strip()
        assert new_cursor != cursor  # every run moves the position
        cursor = new_cursor
        looked_up += _lookups(calls)
        proposed += [e["id"] for e in appended]
        if cursor == "t=1 a=START":
            break
    assert looked_up == [i["identifier"] for i in items]   # every item exactly once
    assert proposed == [fas.item_id("good")]


@pytest.mark.parametrize("answer", [
    # the service ignored the cursor and answered from the start of the target
    [_item("first", "2009-01-01T00:00:00Z")],
    # it answered the marker again (the seek is exclusive)
    [_item("the-marker", "2010-01-01T00:00:00Z"), _item("next", "2010-01-01T00:00:00Z")],
    # an empty page that still promises more
    {"items": [], "cursor": "W3siYWRkZWRkYXRlIjoiMjAxMC0wMS0wMVQwMDowMDowMC4wWiJ9XQ=="},
])
def test_a_seek_the_service_does_not_honour_holds(monkeypatch, tmp_path, answer):
    _, appended, files = _setup(monkeypatch, tmp_path, [answer],
                                {"next": _meta("next"), "first": _meta("first")})
    _run(monkeypatch, "--cursor", "t=0 a=2010-01-01T00:00:00Z i=the-marker", "--append")
    assert appended == [] and files["hold"].read_text().strip()
    assert not files["next"].exists()


TIE = "2010-01-01T00:00:00Z"


@pytest.mark.parametrize("cursor, pages", [
    # identifiers out of order inside one timestamp
    ("t=0 a=START", [[_item("c", TIE), _item("a", TIE)]]),
    # the service orders by code point: "Z" sorts before "a"
    ("t=0 a=START", [[_item("a", TIE), _item("Z", TIE)]]),
    # a repeated key
    ("t=0 a=START", [[_item("a", TIE), _item("a", TIE)]]),
    # an identifier before the persisted key of the same timestamp
    (f"t=0 a={TIE} i=b", [[_item("a", TIE), _item("c", TIE)]]),
    (f"t=0 a={TIE} i=b", [[_item("b", TIE)]]),
])
@pytest.mark.parametrize("budget", [(), ("--max", "1"), ("--lookups", "1")])
def test_keys_must_increase_strictly_from_the_persisted_key(monkeypatch, tmp_path, cursor,
                                                            pages, budget):
    """HOLD before anything is proposed — also when a budget would stop the run after the
    first item, where an unordered page would move NEXT past an item never dealt with."""
    metas = {i: _meta(i) for i in "abcdZ"}
    _, appended, files = _setup(monkeypatch, tmp_path, pages, metas)
    _run(monkeypatch, "--cursor", cursor, *budget, "--append")
    assert appended == [] and "does not follow" in files["hold"].read_text()
    assert not files["next"].exists()


@pytest.mark.parametrize("second", [
    [_item("a", TIE), _item("d", TIE)],      # starts before the first page's last key
    [_item("c", TIE)],                       # repeats it
])
@pytest.mark.parametrize("budget", [(), ("--max", "3"), ("--lookups", "3")])
def test_keys_must_increase_across_a_page_boundary(monkeypatch, tmp_path, second, budget):
    """Each page alone is sorted; the second does not follow the first. The first page's items
    are not proposed either."""
    first = {"items": [_item("b", TIE), _item("c", TIE)], "cursor": True}
    metas = {i: _meta(i) for i in "abcd"}
    _, appended, files = _setup(monkeypatch, tmp_path, [first, second], metas)
    _run(monkeypatch, "--cursor", "t=0 a=START", *budget, "--append")
    assert appended == [] and "does not follow" in files["hold"].read_text()
    assert not files["next"].exists()


def test_ordered_ties_pass_and_a_budget_stop_resumes_inside_the_tie(monkeypatch, tmp_path):
    items = [_item(i, TIE) for i in ("A", "Z", "a", "b")]   # code-point order
    metas = {i["identifier"]: _meta(i["identifier"]) for i in items}
    calls, appended, files = _setup(monkeypatch, tmp_path, _serve(items, 10), metas)
    _run(monkeypatch, "--cursor", "t=0 a=START", "--max", "1", "--pages", "1", "--append")
    assert [e["id"] for e in appended] == [fas.item_id("A")]
    assert files["next"].read_text() == f"t=0 a={TIE} i=A\n"
    calls, appended, files = _setup(monkeypatch, tmp_path, _serve(items, 10), metas)
    _run(monkeypatch, "--cursor", f"t=0 a={TIE} i=A", "--pages", "1", "--append")
    assert _lookups(calls) == ["Z", "a", "b"] and len(appended) == 3


def test_licence_evidence_keeps_every_value_however_long():
    rights = [f"Statement {n}: " + "x" * 150 for n in range(6)] + ["In copyright"]
    urls = [PDM] * 3 + ["http://creativecommons.org/licenses/by/3.0/"]
    tag, evidence, _ = fas.licence({"licenseurl": urls, "rights": rights,
                                    "date": ["1910"] * 40 + ["circa 1911"]})
    assert tag == "unverified"
    for value in (*rights, *urls, "circa 1911"):
        assert value in evidence


def test_user_agent_names_the_tool_and_a_version():
    # archive.org/developers/bots.html
    assert fas.UA["User-Agent"].startswith(f"nekaise-corpus-find_archive_scrape/{fas.VERSION} ")
    assert fas.VERSION and fas.VERSION[0].isdigit()


@pytest.mark.parametrize("cursor", [
    "opaque", "W3siaWRlbnRpZmllciI6IngifV0=",               # [{"identifier":"x"}]
    fas.seek_cursor("2010-01-01T00:00:00Z", "someone-else"),
    fas.seek_cursor("2010-01-02T00:00:00Z", "p1"),
])
def test_a_page_cursor_that_is_not_the_last_items_key_holds(monkeypatch, tmp_path, cursor):
    page = {"items": [_item("p1", "2010-01-01T00:00:00Z")], "cursor": cursor}
    _, appended, files = _setup(monkeypatch, tmp_path, [page], {"p1": _meta("p1")})
    _run(monkeypatch, "--cursor", "t=0 a=START", "--append")
    assert appended == [] and "cursor form" in files["hold"].read_text()
    assert not files["next"].exists()


def test_query_target_keeps_the_catalog_exclusion_and_the_title_rule(monkeypatch, tmp_path):
    index = next(i for i, t in enumerate(fas.TARGETS) if t[1] == "steam heating")
    common = {"year": 1905, "collection": ["americana"]}
    items = [
        _item("book1", "2008-01-01T00:00:00Z", title="Steam heating for buildings", **common),
        _item("cat1", "2008-01-02T00:00:00Z", title="Steam heating catalogue no. 4", **common),
        _item("stray", "2008-01-03T00:00:00Z", title="Bee keeping", **common),
        _item("book1scan2", "2008-01-04T00:00:00Z", title="Steam heating for buildings",
              **common),
        _item("book1vol2", "2008-01-05T00:00:00Z", title="Steam heating for buildings",
              **common),
    ]
    book = {"title": "Steam heating for buildings", "date": "1905", "language": "eng"}
    metas = {"book1": _meta("book1", **book), "book1scan2": _meta("book1scan2", **book),
             "book1vol2": _meta("book1vol2", **book, volume="2")}
    calls, appended, files = _setup(monkeypatch, tmp_path, [items], metas)
    _run(monkeypatch, "--cursor", f"t={index} a=START", "--pages", "1", "--append")
    assert _scrapes(calls)[0]["q"] == (
        '(subject:"steam heating" OR title:"steam heating") AND '
        "mediatype:texts AND date:[1800-01-01 TO 1928-12-31]")
    assert _lookups(calls) == ["book1", "book1scan2", "book1vol2"]
    # another scan of the same volume is dropped; another volume is kept
    assert [e["title"] for e in appended] == ["Steam heating for buildings (1905)",
                                              "Steam heating for buildings, v. 2 (1905)"]
    entry = appended[0]
    assert entry["license"] == "public-domain" and "1905" in entry["license_evidence"]
    assert entry["topic"] == "equipment_systems" and "document_type" not in entry
    assert files["next"].read_text() == f"t={index + 1} a=START\n"


class _View:
    """The round merger's store view: nothing known."""

    def known_pids(self, pids):
        return frozenset()

    def known(self, *, urls=(), titles=(), ids=(), include_blocklist=True):
        return store.KnownHits(frozenset(), frozenset(), frozenset())


def test_catalogs_with_one_title_all_pass_the_round_merger(monkeypatch, tmp_path):
    """Through the cron path: the finder stages a proposal, run_round.merge_proposals (which
    drops equal normalized titles) must accept every distinct Archive item."""
    twins = ["HeatingCatalogue1925", "heatingcatalogue-1925", "Heating.Catalogue.1925"]
    items = [_item(i, f"2010-01-0{n}T00:00:00Z", title="Heating catalogue")
             for n, i in enumerate(twins, 1)]
    metas = {i: _meta(i, title="Heating catalogue", date="1925") for i in twins}
    _, _, files = _setup(monkeypatch, tmp_path, [items], metas)
    proposal = tmp_path / "proposal.json"
    monkeypatch.setenv(registry.PROPOSAL_ENV, str(proposal))
    monkeypatch.setattr(fas.registry, "append_entries", REAL_APPEND)  # proposal mode, for real
    _run(monkeypatch, "--cursor", "t=0 a=START", "--pages", "1", "--append")
    assert files["next"].read_text() == "t=1 a=START\n"  # the cursor passed all 3

    staged = json.loads(proposal.read_text())
    assert len(staged) == 3
    merged, accepted, _ = run_round.merge_proposals(
        _View(), [{"index": 0, "name": "find_archive_scrape", "proposal": proposal}])
    assert accepted == {"find_archive_scrape": 3}
    assert [e["id"] for e in merged] == [fas.item_id(i) for i in twins]
    assert len({registry.norm(e["title"]) for e in merged}) == 3
    assert {e["url"] for e in merged} == {fas.legacy_url(i) for i in twins}
    for entry in merged:
        assert not lint_registry.entry_errors(entry, "archive.yaml")


def test_last_target_finished_becomes_a_watch_cursor(monkeypatch, tmp_path):
    last = len(fas.TARGETS) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [[]])
    _run(monkeypatch, "--cursor", f"t={last} a=2020-01-01T00:00:00Z i=x")
    today = datetime.now(timezone.utc).date()
    assert files["next"].read_text() == f"watch:{today}\n"
    assert not files["exhausted"].exists()


def test_watch_cursor_waits_then_restarts_the_walk(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).date()
    recent = f"watch:{today - timedelta(days=fas.WATCH_DAYS - 1)}"
    calls, _, files = _setup(monkeypatch, tmp_path, [])
    _run(monkeypatch, "--cursor", recent)
    assert calls == [] and files["next"].read_text() == recent + "\n"

    due = f"watch:{today - timedelta(days=fas.WATCH_DAYS)}"
    page = {"items": [_item("w1", "2026-10-01T00:00:00Z")], "cursor": True}
    calls, appended, files = _setup(monkeypatch, tmp_path, [page], {"w1": _meta("w1")})
    _run(monkeypatch, "--cursor", due, "--pages", "1", "--append")
    assert _scrapes(calls)[0]["q"] == f"collection:{BTHL} AND mediatype:texts"
    assert "cursor" not in _scrapes(calls)[0]
    assert len(appended) == 1
    assert files["next"].read_text() == "t=0 a=2026-10-01T00:00:00Z i=w1\n"


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=503, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: {"error": "bad query"}),
    SimpleNamespace(status_code=200, json=lambda: ["not", "an", "object"]),
    SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html"))),
    {"items": [_item("x", "2010-01-01T00:00:00Z")], "cursor": 7},
    # another query's result: the items are not in the collection
    [_item("s1", "2010-01-01T00:00:00Z", collection=["opensource"]),
     _item("s2", "2010-01-02T00:00:00Z", collection=["opensource"])],
    # not in addeddate order inside the page
    [_item("o1", "2010-01-02T00:00:00Z"), _item("o2", "2010-01-01T00:00:00Z")],
    # older than the previous page's last item (each page alone is sorted)
    [_item("p1", "2009-06-01T00:00:00Z"), _item("p2", "2009-07-01T00:00:00Z")],
    [_item("o1", "yesterday")],
    [_item("o1", "2010-02-30T00:00:00Z")],                    # not a calendar date
    [_item("two words", "2010-01-01T00:00:00Z")],
    [{"title": "no identifier", "mediatype": "texts", "collection": [BTHL],
      "addeddate": "2010-01-01T00:00:00Z"}],
])
def test_unexpected_answer_holds_and_proposes_nothing(monkeypatch, tmp_path, answer):
    first = {"items": [_item("ok1", "2009-12-01T00:00:00Z")], "cursor": True}
    _, appended, files = _setup(monkeypatch, tmp_path, [first, answer],
                                {"ok1": _meta("ok1"), "x": _meta("x")})
    _run(monkeypatch, "--cursor", "t=0 a=2009-01-01T00:00:00Z i=m", "--append")
    assert appended == [] and files["hold"].read_text().strip()
    assert not files["next"].exists()


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=500, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: ["list"]),
    {"error": "Metadata service unavailable"},
    {"error": "x", "files": [], "metadata": {"identifier": "m2"}},
    {"metadata": {"identifier": "m2", "title": "T"}},        # no file list
    {"files": [], "metadata": None},
    {"files": {"name": "m2_djvu.txt"}, "metadata": {"identifier": "m2"}},
    {"server": "ia800000.us.archive.org", "is_dark": "maybe"},
])
def test_metadata_service_failure_holds_and_appends_nothing(monkeypatch, tmp_path, answer):
    items = [_item("m1", "2010-01-01T00:00:00Z"), _item("m2", "2010-01-02T00:00:00Z")]
    _, appended, files = _setup(monkeypatch, tmp_path, [items],
                                {"m1": _meta("m1"), "m2": answer})
    _run(monkeypatch, "--cursor", "t=0 a=START", "--append")
    assert appended == [] and files["hold"].read_text().strip()  # m1 is not appended either
    assert not files["next"].exists()


@pytest.mark.parametrize("answer", [{}, {"is_dark": True},
                                    {"is_dark": True, "metadata": {"identifier": "m1"}}])
def test_gone_and_dark_items_are_skipped_and_the_walk_goes_on(monkeypatch, tmp_path, answer):
    items = [_item("m1", "2010-01-01T00:00:00Z"), _item("m2", "2010-01-02T00:00:00Z")]
    _, appended, files = _setup(monkeypatch, tmp_path, [items],
                                {"m1": answer, "m2": _meta("m2")})
    _run(monkeypatch, "--cursor", "t=0 a=START", "--pages", "1", "--append")
    assert [e["id"] for e in appended] == [fas.item_id("m2")] and not files["hold"].exists()
    assert files["next"].read_text() == "t=1 a=START\n"


def test_network_failure_exits_nonzero_without_append(monkeypatch, tmp_path):
    first = {"items": [_item("ok1", "2009-01-01T00:00:00Z")], "cursor": True}
    _, appended, files = _setup(monkeypatch, tmp_path, [first, TimeoutError("offline")],
                                {"ok1": _meta("ok1")})
    monkeypatch.setattr(sys, "argv", ["find_archive_scrape.py", "--append"])
    with pytest.raises(SystemExit) as exc:
        fas.main()
    assert exc.value.code == 1
    assert appended == [] and not files["next"].exists() and not files["hold"].exists()


def test_cursor_round_trip():
    assert fas.parse_cursor("t=0 a=START") == (0, None, None)
    assert fas.parse_cursor("t=3 a=2010-01-05T00:00:00Z i=Foo_1.b-c") == (
        3, "2010-01-05T00:00:00Z", "Foo_1.b-c")
    assert fas.format_cursor(3, "2010-01-05T00:00:00Z", "Foo_1") == (
        "t=3 a=2010-01-05T00:00:00Z i=Foo_1")
    assert fas.format_cursor(4, None, None) == "t=4 a=START"


@pytest.mark.parametrize("cursor", [
    "", "t=1", "t=a a=START", "a=START t=1", "t=1 a=2020", "t=1 a=2020-01-01", "watch:2020",
    "END", "980", "t=1 a=START i=x", "t=1 a=2010-01-05T00:00:00Z", "t=1 a=2010-02-30T00:00:00Z i=x",
    "t=1 a=2010-13-01T00:00:00Z", "t=1 a=2010-01-01T25:00:00Z i=x", "watch:2026-02-30",
    "t=1 a=2010-01-01T00:00:00Z i=two words", "t=1 a=2010-01-01T00:00:00Z i=",
])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        fas.parse_cursor(cursor)


def test_targets_keep_the_collections_first_and_every_old_query():
    import find_archive
    assert [t[1] for t in fas.TARGETS[:2]] == [BTHL, "averylibrarytradecatalogs"]
    assert [(t[1], t[2]) for t in fas.TARGETS[2:]] == list(find_archive.QUERIES)
    assert all(t[2] in lint_registry.TOPICS for t in fas.TARGETS)
    assert all(topic in lint_registry.TOPICS for topic, _ in fas.TOPIC_RULES)
