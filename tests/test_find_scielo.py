import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import coverage
import find_scielo
import lint_registry
import registry


def _rec(title, pdf=None, lang="es", licence=None, v702=None, doi=None, titles=None):
    article = {"v12": titles or [{"_": title, "l": lang}], "v40": [{"_": lang}]}
    if v702:
        article["v702"] = [{"_": v702}]
    rec = {"article": article}
    if pdf:
        rec["fulltexts"] = {"pdf": {lang: pdf}}
    if licence:
        rec["license"] = licence
    if doi:
        rec["doi"] = doi
    return rec


def test_journal_table_is_append_only_and_every_collection_is_reviewed():
    first = [("scl", "1413-4152"), ("scl", "1679-7825"), ("scl", "1678-8621"),
             ("scl", "1983-4195"), ("scl", "2175-3369"), ("scl", "2448-167X"),
             ("scl", "2236-9996"), ("scl", "2318-0331"), ("scl", "2675-5475"),
             ("scl", "2238-1031"), ("scl", "1516-1439")]
    assert [j[:2] for j in find_scielo.JOURNALS[:find_scielo.FIRST_NEW_JOURNAL]] == first
    assert len({j[:2] for j in find_scielo.JOURNALS}) == len(find_scielo.JOURNALS)
    for collection, issn, topic, gate in find_scielo.JOURNALS:
        assert collection in find_scielo.HOSTS and collection in find_scielo.HOST_ALIASES
        assert topic in lint_registry.TOPICS and isinstance(gate, bool)
        assert coverage.SOURCE_GENRE[f"scielo_{collection}"] == "research_papers"
    # robots.txt Disallow / (Chile, Peru) and unreachable hosts are never configured
    assert not {"chl", "per", "cub", "ecu", "ury", "ven"} & set(find_scielo.HOSTS)
    for base in find_scielo.HOSTS.values():
        assert "scielo.cl" not in base and "scielo.org.pe" not in base


def test_pdf_url_stays_on_the_collections_reviewed_host():
    mex = _rec("t", pdf="http://www.scielo.org.mx/pdf/ralconpat/v8n1/a-51.pdf")
    assert find_scielo.pdf_url(mex, "mex") == "https://www.scielo.org.mx/pdf/ralconpat/v8n1/a-51.pdf"
    # Colombia serves plain HTTP only; Portugal's canonical host has no www
    col = _rec("t", pdf="https://www.scielo.org.co/pdf/biut/v28n1/x.pdf")
    assert find_scielo.pdf_url(col, "col") == "http://www.scielo.org.co/pdf/biut/v28n1/x.pdf"
    prt = _rec("t", pdf="http://www.scielo.pt/pdf/cct/n50/y.pdf")
    assert find_scielo.pdf_url(prt, "prt") == "https://scielo.pt/pdf/cct/n50/y.pdf"
    # a PDF link on another national host (not robots-cleared for this collection) is refused
    assert find_scielo.pdf_url(_rec("t", pdf="http://www.scielo.cl/pdf/ric/v1/z.pdf"), "mex") is None
    assert find_scielo.pdf_url(_rec("t", pdf="http://www.scielo.org.ve/pdf/a/v1/z.pdf"), "scl") is None
    assert find_scielo.pdf_url(_rec("t", pdf="http://www.scielo.br/scielo.php?pid=1"), "scl") is None
    assert find_scielo.pdf_url(mex, "chl") is None
    legacy = _rec("t", v702="V:\\Scieloza\\serial\\JSAICE\\v52n1\\markup\\v52n1a01.htm")
    assert find_scielo.pdf_url(legacy, "sza") == \
        "https://www.scielo.org.za/pdf/jsaice/v52n1/v52n1a01.pdf"
    modern = _rec("t", v702="ac/v26/1678-8621-AC-26-e149370.xml")
    assert find_scielo.pdf_url(modern, "scl") == \
        "https://www.scielo.br/pdf/ac/v26/1678-8621-AC-26-e149370.pdf"


@pytest.mark.parametrize("article,journal,tag,url", [
    ("by/4.0", "BY-NC", "cc-by", "https://creativecommons.org/licenses/by/4.0/"),
    ("by-nc-nd/3.0", "BY/4.0", "cc-by-nc-nd", "https://creativecommons.org/licenses/by-nc-nd/3.0/"),
    ("by-nc/3.0", "BY-NC-ND/4.0", "cc-by-nc", "https://creativecommons.org/licenses/by-nc/3.0/"),
    ("by", "BY-NC", "cc-by", None),
    # a jurisdiction port is another instrument than the unported licence: the URL keeps it
    ("by/3.0/br", None, "cc-by", "https://creativecommons.org/licenses/by/3.0/br/"),
    ("BY-NC/3.0/ES", None, "cc-by-nc", "https://creativecommons.org/licenses/by-nc/3.0/es/"),
    ("by-nc-nd/3.0/igo", None, "cc-by-nc-nd",
     "https://creativecommons.org/licenses/by-nc-nd/3.0/igo/"),
    ("by/3.0/igo", None, "unverified", None),   # the shared parser does not know this port
    ("by/br", None, "unverified", None),        # a port without a version is not a licence code
    # the journal's current licence never yields a tag: it does not cover every issue
    (None, "BY/4.0", "unverified", None),
    (None, "BY", "unverified", None),
    (None, "BY-SA/4.0", "unverified", None),
    (None, "BY-NC-SA", "unverified", None),
    (None, "nd", "unverified", None),          # v541 "nd" = not declared
    (None, None, "unverified", None),
    (None, "all rights reserved", "unverified", None),
    ("copyright the authors", "BY", "unverified", None),   # an unreadable article value wins
])
def test_licence_comes_from_the_article_record_only(article, journal, tag, url):
    rec = _rec("t", licence=article)
    got_tag, got_url, evidence = find_scielo.licence(rec, journal, "mex", "S2007-1", "2007-6835")
    assert (got_tag, got_url) == (tag, url)
    assert got_tag in lint_registry.LICENSES and "ArticleMeta" in evidence
    if article:
        assert "article record license" in evidence and "code=S2007-1" in evidence
    else:
        assert "context only" in evidence and "issn=2007-6835" in evidence
        assert f"'{journal or ''}'" in evidence


def test_candidate_fields_and_title_gate():
    rec = _rec("Durabilidad del concreto reforzado en ambiente marino",
               pdf="http://www.scielo.org.mx/pdf/ralconpat/v8n1/a.pdf", licence="by-nc-nd/4.0",
               doi="10.21041/ra.v8i1.1")
    entry = find_scielo.candidate(rec, "mex", "2007-6835", "materials", False, "BY/4.0",
                                  "S2007-68352018000100051", "2026-10-12")
    assert entry["id"] == "sci-s2007-68352018000100051" and entry["source"] == "scielo_mex"
    assert entry["license"] == "cc-by-nc-nd" and entry["language"] == "es"
    bare = find_scielo.candidate({k: v for k, v in rec.items() if k != "license"}, "mex",
                                 "2007-6835", "materials", False, "BY/4.0",
                                 "S2007-68352018000100051", "2026-10-12")
    assert bare["license"] == "unverified" and "license_url" not in bare
    assert not lint_registry.entry_errors(bare, "scielo.yaml")
    assert entry["origin_ids"] == "doi:10.21041/ra.v8i1.1"
    assert entry["rights_verified_at"] == "2026-10-12"
    assert registry.shard_filename(entry["id"]) == "scielo.yaml"
    assert not lint_registry.entry_errors(entry, "scielo.yaml")

    def gated(title, **kw):
        r = _rec(title, pdf="https://www.scielo.br/pdf/rmat/v1/a.pdf", lang="pt", **kw)
        return find_scielo.candidate(r, "scl", "1517-7076", "materials", True, "BY", "S1", "x")

    assert gated("Concretos com agregados reciclados de resíduos de construção e demolição")
    assert gated("Desempenho térmico de paredes de alvenaria")
    assert not gated("Structure and properties of starch of four Brazilian sweet potatoes")
    assert not gated("Síntese de ligas ternárias pelo método do campo de fase")
    assert not gated("Corrosion of galvanic pairs of Co-Cr alloys with silver amalgam")
    # bare "structural" is materials science too: it needs an engineering phrase or AEC term
    for title in ("Structural characterization of starch nanoparticles",
                  "Structural properties of magnetic thin films",
                  "Caracterização estrutural e morfológica de filmes de quitosana",
                  "Propiedades estructurales y ópticas de películas delgadas de ZnO",
                  "Crystal structures of layered double hydroxides"):
        assert not gated(title), title
    for title in ("Structural reliability of reinforced members under fatigue",
                  "Análise estrutural de pórticos planos com ligações semirrígidas",
                  "Structural behavior of cold-formed profiles",
                  "Estruturas de aço em situação de incêndio",
                  "Diseño estructural de naves industriales",
                  "Optimization of plane trusses with genetic algorithms",
                  "Structural characterization of lime mortars from historic masonry"):
        assert gated(title), title
    # a translated title can carry the built-environment term
    assert gated("Estudo de ligantes álcali-ativados",
                 titles=[{"_": "Estudo de ligantes álcali-ativados", "l": "pt"},
                         {"_": "Alkali-activated binders for masonry mortars", "l": "en"}])
    # no PDF on the reviewed host: nothing to propose
    assert find_scielo.candidate(_rec("Puentes de concreto"), "mex", "1", "materials", False,
                                 "BY", "S2", "x") is None


class FakeApi:
    """ArticleMeta double: `listing[(issn, offset)]` -> (codes, total); every article is a
    concrete paper on its collection's host."""

    def __init__(self, listing, fail=None, records=None):
        self.listing, self.fail, self.calls = listing, fail or {}, []
        self.records = records or {}

    def __call__(self, path, **params):
        self.calls.append((path, dict(params)))
        if path in self.fail:
            raise self.fail[path]
        if path == "journal/":
            return [{"v541": [{"_": "BY/4.0"}]}]
        if path == "article/identifiers/":
            codes, total = self.listing.get((params["issn"], params["offset"]), ([], 0))
            return {"meta": {"total": total}, "objects": [{"code": c} for c in codes]}
        code = params["code"]
        if code in self.fail:
            raise self.fail[code]
        if code in self.records:
            return self.records[code]
        host = find_scielo.HOSTS[params["collection"]].split("//")[1]
        rec = _rec(f"Concreto reforzado {code}", pdf=f"http://{host}/pdf/x/v1/{code}.pdf",
                   licence="by/4.0")
        return {**rec, "code": code, "collection": params["collection"]}


def _run(monkeypatch, tmp_path, api, cursor, journals, known=None, extra=()):
    known = known or (set(), set(), set())
    monkeypatch.setattr(find_scielo, "_get", api)
    monkeypatch.setattr(find_scielo, "JOURNALS", journals)
    monkeypatch.setattr(find_scielo, "PER", 2)
    monkeypatch.setattr(find_scielo.time, "sleep", lambda _s: None)
    monkeypatch.setattr(find_scielo.dedup, "open_keys",
                        lambda: find_scielo.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(find_scielo.registry, "append_entries", appended.extend)
    files = {name: tmp_path / name for name in ("next", "exhausted", "hold")}
    for f in files.values():
        f.unlink(missing_ok=True)
    monkeypatch.setenv("NEKAISE_ROTATION_NEXT_FILE", str(files["next"]))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(files["exhausted"]))
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(files["hold"]))
    monkeypatch.setattr(sys, "argv", ["find_scielo.py", "--cursor", cursor, "--append", *extra])
    find_scielo.main()
    return appended, files


TWO = [("mex", "1111-1111", "materials", False), ("col", "2222-2222", "urban", False)]


def test_walk_is_journal_major_and_skips_held_ids_without_a_lookup(monkeypatch, tmp_path):
    api = FakeApi({("1111-1111", 0): (["A1", "A2"], 3), ("1111-1111", 2): (["A3"], 3),
                   ("2222-2222", 0): (["B1"], 1)})
    known = (set(), set(), {"sci-a2"})
    appended, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO, known,
                           ("--pages", "2"))
    assert [e["id"] for e in appended] == ["sci-a1", "sci-a3"]
    assert [p["code"] for path, p in api.calls if path == "article/"] == ["A1", "A3"]
    assert sum(path == "journal/" for path, _ in api.calls) == 1   # read once per journal
    assert all("from" not in p for path, p in api.calls if path == "article/identifiers/")
    assert files["next"].read_text() == "walk j=1 o=0\n"

    appended, files = _run(monkeypatch, tmp_path, api, "walk j=1 o=0", TWO)
    assert [e["url"] for e in appended] == ["http://www.scielo.org.co/pdf/x/v1/B1.pdf"]
    # the walk ends as a watch cursor whose first sweep is due at once; never EXHAUSTED
    assert files["next"].read_text() == f"watch:{find_scielo.FIRST_SWEEP_SINCE}\n"
    assert not files["exhausted"].exists()


def test_full_page_keeps_the_journal_and_advances_the_offset(monkeypatch, tmp_path):
    api = FakeApi({("1111-1111", 0): (["A1", "A2"], 5)})
    _, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO, extra=("--pages", "1"))
    assert files["next"].read_text() == "walk j=0 o=2\n"


def test_watch_waits_then_sweeps_every_journal_with_from(monkeypatch, tmp_path):
    today = datetime.now(timezone.utc).date()
    recent = f"watch:{today - timedelta(days=find_scielo.WATCH_DAYS - 1)}"
    api = FakeApi({})
    appended, files = _run(monkeypatch, tmp_path, api, recent, TWO)
    assert api.calls == [] and appended == [] and files["next"].read_text() == recent + "\n"

    since = today - timedelta(days=find_scielo.WATCH_DAYS)
    api = FakeApi({("1111-1111", 0): (["N1"], 1), ("2222-2222", 0): ([], 0)})
    appended, files = _run(monkeypatch, tmp_path, api, f"watch:{since}", TWO,
                           extra=("--pages", "1"))
    listed = [p for path, p in api.calls if path == "article/identifiers/"]
    wanted = (since - timedelta(days=find_scielo.SWEEP_MARGIN_DAYS)).isoformat()
    assert [(p["issn"], p["from"]) for p in listed] == [("1111-1111", wanted),
                                                        ("2222-2222", wanted)]
    assert [e["id"] for e in appended] == ["sci-n1"]
    assert files["next"].read_text() == f"watch:{today}\n"


def test_interrupted_sweep_resumes_and_keeps_its_dates(monkeypatch, tmp_path):
    api = FakeApi({("2222-2222", 0): (["B9"], 1)})
    appended, files = _run(monkeypatch, tmp_path, api, "sweep:2026-09-01:2026-09-10 j=1 o=0", TWO)
    assert [p["from"] for path, p in api.calls if path == "article/identifiers/"] == ["2026-08-29"]
    assert [e["id"] for e in appended] == ["sci-b9"]
    assert files["next"].read_text() == "watch:2026-09-10\n"


def test_api_failure_never_burns_a_page(monkeypatch, tmp_path):
    # the first page fails on an unexpected answer: HOLD, nothing proposed
    api = FakeApi({}, fail={"article/identifiers/": find_scielo.Unexpected("HTTP 503")})
    appended, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO)
    assert appended == [] and files["hold"].exists() and not files["next"].exists()
    # the first page fails on the network: nonzero exit, no append, pointer kept
    api = FakeApi({("1111-1111", 0): (["A1", "A2"], 5)},
                  fail={"A2": requests.ConnectionError("reset")})
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO)
    assert exc.value.code == 1
    # a later page fails: the complete pages are proposed and the cursor stays at the failed one
    api = FakeApi({("1111-1111", 0): (["A1", "A2"], 5), ("1111-1111", 2): (["A3", "A4"], 5)},
                  fail={"A4": requests.ConnectionError("reset")})
    appended, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO,
                           extra=("--pages", "3"))
    assert [e["id"] for e in appended] == ["sci-a1", "sci-a2"]
    assert files["next"].read_text() == "walk j=0 o=2\n"


@pytest.mark.parametrize("listing,records", [
    # a short page while meta.total says more remain is not the end of the journal
    ({("1111-1111", 0): (["A1"], 5)}, {}),
    ({("1111-1111", 0): ([], 100)}, {}),
    # more identifiers than the page size, or an identifier without a code
    ({("1111-1111", 0): (["A1", "A2", "A3"], 3)}, {}),
    ({("1111-1111", 0): (["A1", ""], 2)}, {}),
    # an article answer that is an error object, null, or another article's record
    ({("1111-1111", 0): (["A1", "A2"], 2)}, {"A2": {"error": "temporary metadata failure"}}),
    ({("1111-1111", 0): (["A1", "A2"], 2)}, {"A2": None}),
    ({("1111-1111", 0): (["A1", "A2"], 2)},
     {"A2": {"code": "ZZ", "article": {"v12": [{"_": "Puentes", "l": "es"}]}}}),
    ({("1111-1111", 0): (["A1", "A2"], 2)}, {"A2": {"code": "A2", "article": {}}}),
])
def test_inconsistent_answers_hold_before_the_page_is_accepted(monkeypatch, tmp_path, listing,
                                                               records):
    api = FakeApi(listing, records=records)
    appended, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO)
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_an_inconsistent_later_page_keeps_only_the_complete_pages(monkeypatch, tmp_path):
    api = FakeApi({("1111-1111", 0): (["A1", "A2"], 5), ("1111-1111", 2): (["A3", "A4"], 5),
                   ("1111-1111", 4): ([], 5)},
                  records={"A4": {"error": "temporary metadata failure"}})
    appended, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=0", TWO,
                           extra=("--pages", "3"))
    assert [e["id"] for e in appended] == ["sci-a1", "sci-a2"]
    assert files["next"].read_text() == "walk j=0 o=2\n"
    # the last page of a journal: empty while total says one more remains -> not finished
    api = FakeApi({("1111-1111", 4): ([], 5)})
    appended, files = _run(monkeypatch, tmp_path, api, "walk j=0 o=4", TWO)
    assert appended == [] and files["hold"].exists() and not files["next"].exists()


def test_an_out_of_range_journal_index_is_refused_not_turned_into_a_watch(monkeypatch, tmp_path):
    api = FakeApi({})
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, api, "walk j=9999 o=0", TWO)
    assert exc.value.code == 2 and api.calls == []
    assert not (tmp_path / "next").exists()


def test_cursor_forms_round_trip():
    for text in ("walk j=11 o=0", "watch:2026-10-12", "sweep:2026-10-01:2026-10-12 j=3 o=50"):
        assert find_scielo.format_cursor(find_scielo.parse_cursor(text)) == text
    assert find_scielo.parse_cursor("watch:2026-10-12")["since"] == date(2026, 10, 12)
    last = len(find_scielo.JOURNALS) - 1
    assert find_scielo.parse_cursor(f"walk j={last} o=0")["j"] == last
    for bad in ("", "23050", "END", "walk j=1", "watch:soon", "sweep:2026-10-01 j=0 o=0",
                "walk j=9999 o=0", f"walk j={last + 1} o=0", "watch:2026-02-30",
                f"sweep:2026-10-01:2026-10-12 j={last + 1} o=0",
                "sweep:2026-13-01:2026-10-12 j=0 o=0"):
        with pytest.raises(ValueError):
            find_scielo.parse_cursor(bad)


NEW_HOSTS = sorted(base.split("//")[1] for c, base in find_scielo.HOSTS.items() if c != "scl")
ALL_HOSTS = sorted(base.split("//")[1] for base in find_scielo.HOSTS.values())


def test_every_configured_scielo_host_is_polite_paced_and_honest():
    assert "www.scielo.br" in ALL_HOSTS and len(ALL_HOSTS) == len(find_scielo.HOSTS) == 7
    for host in ALL_HOSTS:
        assert host in build_corpus.POLITE_HOSTS
        assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA
        assert build_corpus.HOST_DELAY[host] >= 1.0
        assert host in build_corpus.HOST_RUN_CAP


@pytest.mark.parametrize("host", ALL_HOSTS)
@pytest.mark.parametrize("status,body", [
    (403, b"<html><div class='g-recaptcha'>captcha</div></html>"),
    (429, b"slow down"), (503, b"busy"),
])
def test_a_challenge_on_any_scielo_host_opens_the_circuit_and_never_reaches_curl(
        monkeypatch, tmp_path, host, status, body):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]["User-Agent"]))
        return SimpleNamespace(
            status_code=status, content=body, headers={}, url=url, text=body.decode(),
            raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError(f"HTTP {status}")))

    monkeypatch.setattr(build_corpus.requests, "get", get)
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("a polite host must never reach curl"))
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    scheme = "http" if host.endswith(".co") else "https"

    def row(n):
        return {"id": f"sci-s{n}", "title": f"Concreto {n}", "source": "scielo_mex",
                "license": "unverified", "topic": "materials", "format": "pdf",
                "url": f"{scheme}://{host}/pdf/x/v1/a{n}.pdf"}

    first = build_corpus.download_one(row(1))
    second = build_corpus.download_one(row(2))
    assert first["transient"] is True and "challenge" in first["error"]
    assert second["transient"] is True and "circuit open" in second["error"]
    assert calls == [(row(1)["url"], build_corpus.HONEST_UA)]   # one request, one identity


def test_new_national_hosts_are_serial_paced_and_honest():
    hosts = set(NEW_HOSTS)
    assert hosts == {"www.scielo.org.mx", "www.scielo.org.ar", "scielo.pt", "www.scielo.sa.cr",
                     "www.scielo.org.za", "www.scielo.org.co"}
    for host in hosts:
        assert host in build_corpus.POLITE_HOSTS
        assert build_corpus.HOST_CONCURRENCY[host] == 1
        assert build_corpus.HOST_DELAY[host] >= 2.0
        assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA
        assert build_corpus.HOST_RUN_CAP[host] <= 120
