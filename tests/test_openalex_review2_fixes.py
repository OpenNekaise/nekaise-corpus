"""Regression tests for Codex's SECOND review of the OpenAlex phase-1 change: copy identity,
Crossref dates, PG aliases (in test_store_contract / test_store_pg_staging), malformed pages,
destination pacing, cookie-preserving chains, bounded relations, the lookup cap, and the index
rebuild wait. Canned transports, no network."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import corpus_index
import dedup
import find_sources
import oa_resolution as oar
import ops
import openalex_families as fam
import pipeline_repo
import store
from test_openalex_review_fixes import curl_env, loader, pdf, row  # noqa: F401 (fixture)
from test_openalex_sim import NOW, POLICY, FakeHttp, fake_transport, good, make_run, page, work

UUID = "2e0aa826-03a0-4f97-a019-d266f30c50a9"


# --- copy identity -------------------------------------------------------------------------------

def test_a_redirect_to_another_zenodo_record_is_refused(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://zenodo.org/records/1/files/a.pdf": (
            302, {"Location": "https://zenodo.org/records/2/files/a.pdf"}, b""),
    })
    rec = build_corpus.download_one(row("https://zenodo.org/records/1/files/a.pdf"))
    assert requested == ["https://zenodo.org/records/1/files/a.pdf"]
    assert "leaves the licensed copy" in rec["error"] and not rec.get("raw_path")


def test_an_unclassified_multi_label_suffix_never_joins_two_sites():
    assert not oar.same_copy("https://a.vic.edu.au/x/1234567", "https://b.vic.edu.au/x/1234567")
    assert not oar.same_copy("https://a.example.co.za/p.pdf", "https://b.example.co.za/p.pdf")


def test_same_bitstream_through_the_configured_api_host(monkeypatch, loader):
    origin = f"https://www.repository.cam.ac.uk/bitstreams/{UUID}/download"
    dest = f"https://api.repository.cam.ac.uk/server/api/core/bitstreams/{UUID}/content"
    requested = fake_transport(monkeypatch, {origin: (302, {"Location": dest}, b""),
                                             dest: pdf()})
    rec = build_corpus.download_one(row(origin))
    assert rec.get("error") is None and requested == [origin, dest]


# --- Crossref dates ------------------------------------------------------------------------------

def am_copy():
    return work(doi="10.1016/j.x.1", locations=[{
        "id": "doi:10.1016/j.x.1", "pdf_url": "https://zenodo.org/records/7/files/aam.pdf",
        "version": "acceptedVersion"}])


def am_grant(start):
    lic = {"content-version": "am", "URL": "https://creativecommons.org/licenses/by/4.0/"}
    if start is not ...:
        lic["start"] = start
    return {"license": [lic]}


@pytest.mark.parametrize("start", [
    ...,                                        # missing
    None,
    {},
    {"date-parts": [[2020]]},                   # partial: could be any later day
    {"date-parts": [[2020, 5]]},
    {"date-parts": [[2020, 13, 1]]},            # invalid
    {"date-parts": [["2020", 1, 1]]},
    {"date-parts": "2020-01-01"},
    {"date-time": "not a date"},
    {"date-time": "2030-01-01T00:00:00Z"},      # future
])
def test_a_grant_without_a_valid_effective_date_never_authorizes(start):
    res = oar.select_copy(am_copy(), POLICY, crossref=am_grant(start))
    assert res.status == "unresolved"


@pytest.mark.parametrize("start", [{"date-parts": [[2020, 1, 1]]},
                                   {"date-time": "2020-01-01T00:00:00Z"},
                                   {"date-time": "2020-01-01T00:00:00"}])  # naive: read as UTC
def test_a_valid_effective_grant_authorizes_and_naive_dates_never_crash(start):
    assert oar.select_copy(am_copy(), POLICY, crossref=am_grant(start)).status == "resolved"
    naive_now = datetime(2026, 9, 25)
    direct, _ = oar.crossref_evidence(am_grant(start), "acceptedVersion", now=naive_now)
    assert [e.status for e in direct] == ["accepted"]


# --- malformed pages -----------------------------------------------------------------------------

@pytest.mark.parametrize("data, page_no", [
    ({"meta": {"count": 100}, "results": []}, 1),            # empty page where 100 exist
    ({"meta": {"count": 250}, "results": [{"id": "W1"}] * 40}, 2),  # short middle page
    ({"meta": {"count": 150, "page": 1}, "results": [{"id": "W1"}] * 50}, 2),  # wrong page
])
def test_a_short_or_misplaced_page_is_malformed(data, page_no):
    with pytest.raises(fam.UpstreamError):
        fam.check_search(data, 100, page_no)


def test_a_genuinely_last_page_is_accepted():
    assert fam.check_search({"meta": {"count": 150}, "results": [{"id": "W"}] * 50}, 100, 2)
    assert fam.check_search({"meta": {"count": 100}, "results": []}, 100, 2) == ([], 100)
    deep = {"meta": {"count": 50_000}, "results": [{"id": "W"}] * 100}
    assert fam.check_search(deep, 100, 100)  # within the 10,000-result paging depth


def test_malformed_first_page_keeps_the_cursor():
    reported = []
    args = SimpleNamespace(family_cursor="sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0",
                           resolution_file=None, append=True, lookup_max=250, per=100, max=25)
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        save_cooldowns=lambda c: None, get=FakeHttp(pages=[{"meta": {"count": 100},
                                                            "results": []}]),
        openalex_relevant=find_sources.openalex_relevant, append_entries=lambda e: None,
        request_hold=lambda r: None, report_next=reported.append, now=NOW, sleep=lambda s: None)
    assert code == 1 and reported == []


# --- destination pacing ------------------------------------------------------------------------

def test_a_paced_chain_installs_the_defaults_for_a_new_destination(monkeypatch, loader):
    origin = f"https://www.repository.cam.ac.uk/bitstreams/{UUID}/download"
    dest = f"https://api.repository.cam.ac.uk/server/api/core/bitstreams/{UUID}/content"
    fake_transport(monkeypatch, {origin: (302, {"Location": dest}, b""), dest: pdf()})
    monkeypatch.setattr(build_corpus, "HOST_CONCURRENCY", {})
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {"api.repository.cam.ac.uk": 5.0})
    monkeypatch.setattr(build_corpus, "_host_sems", {})
    monkeypatch.setattr(build_corpus, "_host_next", {})
    monkeypatch.setattr(build_corpus.time, "sleep", lambda s: None)
    src = row(origin)
    build_corpus.pace_new_hosts([src], {})
    try:
        rec = build_corpus.download_one(src)
    finally:
        build_corpus.PACED_IDS.clear()
    assert rec.get("error") is None
    assert build_corpus.HOST_CONCURRENCY == {"www.repository.cam.ac.uk": 1,
                                             "api.repository.cam.ac.uk": 1}
    assert build_corpus.HOST_DELAY["api.repository.cam.ac.uk"] == 5.0  # longer delay kept
    assert build_corpus.HOST_DELAY["www.repository.cam.ac.uk"] == build_corpus.PACED_DELAY
    assert build_corpus._host_sems["api.repository.cam.ac.uk"]._initial_value == 1


# --- cookie-preserving chains ---------------------------------------------------------------------

def test_a_cookie_set_on_a_redirect_reaches_the_next_hop(monkeypatch, loader):
    seen = []

    def send(self, request, **kwargs):
        seen.append((request.url, request.headers.get("Cookie"),
                     request.headers.get("User-Agent")))
        resp = requests.Response()
        resp.url, resp.request, resp.connection = request.url, request, self
        if request.url.endswith("/start"):
            resp.status_code = 302
            resp.headers["Location"] = "/file.pdf"
            resp.cookies.set("sid", "abc", domain="docs.example.org", path="/")
        elif request.headers.get("Cookie") == "sid=abc":
            resp.status_code, resp._content = 200, b"%PDF-1.7 ok"
        else:
            resp.status_code, resp._content = 403, b"no session"
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setitem(build_corpus.HOST_UA, "docs.example.org", "honest/2")
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *a, **k: pytest.fail("no curl fallback needed"))
    rec = build_corpus.download_one(row("https://docs.example.org/start", source="curated"))
    assert rec.get("error") is None and rec["bytes"] == len(b"%PDF-1.7 ok")
    # one session per chain: the cookie travels; per-host headers still apply on every hop
    assert seen == [("https://docs.example.org/start", None, "honest/2"),
                    ("https://docs.example.org/file.pdf", "sid=abc", "honest/2")]
    # and never leaks into the next, independent download (which then fails: no session)
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=22))
    build_corpus.download_one(row("https://docs.example.org/file.pdf", source="curated",
                                  sid="other"))
    assert seen[-1][1] is None


def test_curl_keeps_one_cookie_engine_per_chain(monkeypatch, loader):
    commands = []

    def fake(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[cmd.index("-D") + 1]).write_text(
            "HTTP/1.1 302 X\r\nLocation: /b.pdf\r\n\r\n" if cmd[-1].endswith("/a.pdf")
            else "HTTP/1.1 200 OK\r\n\r\n")
        if not cmd[-1].endswith("/a.pdf"):
            kwargs["stdout"].write(b"%PDF-1.4 body")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_corpus.subprocess, "run", fake)
    assert build_corpus._curl_follow("https://example.org/a.pdf") == b"%PDF-1.4 body"
    jars = {(c[c.index("-b") + 1], c[c.index("-c") + 1]) for c in commands}
    assert len(commands) == 2 and len(jars) == 1 and len(next(iter(jars))) == 2


# --- a relation-rich work finishes within its budget ---------------------------------------------

def test_relation_lookups_are_bounded_before_any_network_call():
    msg = {"relation": {"references": [{"id": f"10.9999/weak.{i}", "id-type": "doi"}
                                       for i in range(30)]}}
    asked = []
    rec = {"title": "Automated Modelica model repair with language model agents",
           "authors": ["A Feng"], "year": 2026}
    got = oar.crossref_related_dois(msg, rec, lambda d: asked.append(d) or None)
    assert got == [] and len(asked) == oar.MAX_RELATIONS
    explicit = {"relation": {"has-version": [{"id": f"10.9999/v.{i}", "id-type": "doi"}
                                             for i in range(5)], **msg["relation"]}}
    asked.clear()
    got = oar.crossref_related_dois(explicit, rec, lambda d: asked.append(d) or None)
    assert len(got) == oar.MAX_RELATIONS and asked == []  # explicit ones first, no lookup


def test_a_relation_rich_preprint_finishes_in_one_run():
    pre = work("Modelica building HVAC simulation preprint", type_="preprint",
               doi="10.2139/ssrn.5", wid="W5", locations=[])
    crossref = {"relation": {"references": [{"id": f"10.9999/weak.{i}", "id-type": "doi"}
                                            for i in range(30)]}}
    http = FakeHttp(pages=[page([pre, good(1)])],
                    singles={"https://api.crossref.org/works/10.2139/ssrn.5":
                             {"message": crossref}})
    run = make_run(http=http, lookup_max=fam.MAX_LOOKUPS_PER_WORK)
    nxt = run.run(fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "sim")
    assert run.api.lookups <= fam.MAX_LOOKUPS_PER_WORK
    assert nxt.sim.k != 0 or nxt.sim.w == 1  # the relation-rich work was finished


def test_lookup_max_below_one_works_worst_case_is_refused():
    args = SimpleNamespace(family_cursor="sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0",
                           resolution_file=None, append=True, per=100, max=25,
                           lookup_max=fam.MAX_LOOKUPS_PER_WORK - 1)
    http = FakeHttp()
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        save_cooldowns=lambda c: None, get=http, openalex_relevant=find_sources.openalex_relevant,
        append_entries=lambda e: None, request_hold=lambda r: None, report_next=lambda v: None,
        now=NOW, sleep=lambda s: None)
    assert code == 2 and http.calls == []


def test_committed_family_backends_use_lookup_max_250():
    import json
    backends = json.loads((Path(__file__).resolve().parents[1] / "registry" /
                           "backends.json").read_text())
    for name in ("find_openalex_sim", "find_openalex_ai"):
        args = backends[name]["args"]
        assert args[args.index("--lookup-max") + 1] == "250"
        assert args[args.index("--max") + 1] == "25"


# --- the acceptance predicate is one place ---------------------------------------------------------

def test_every_selection_goes_through_the_one_acceptance_predicate(monkeypatch):
    w = work(locations=[{"pdf_url": "https://zenodo.org/records/1/files/a.pdf",
                         "license": "cc-by-nc", "version": "publishedVersion"}])
    assert oar.select_copy(w, POLICY).status == "unresolved"
    monkeypatch.setattr(oar, "copy_acceptable", lambda rights: True)
    assert oar.select_copy(w, POLICY).status == "resolved"


# --- the index rebuild: waited for, never replaced by a full parse --------------------------------

def test_a_lookup_during_a_foreign_rebuild_waits_then_fails_without_parsing(monkeypatch,
                                                                            tmp_path):
    root = pipeline_repo.write_repo(tmp_path / "repo",
                                    entries=[{"id": "oer-a", "title": "A", "url": "https://a/x",
                                              "source": "t", "license": "cc-by",
                                              "topic": "urban", "format": "pdf"}])
    monkeypatch.setattr(ops, "WORKSPACE", root / "workspace")
    monkeypatch.delenv("NEKAISE_DISABLE_INDEX", raising=False)
    monkeypatch.setenv(corpus_index.WAIT_ENV, "0.3")
    st = store.FileStore(root)
    with ops.named_lock("corpus-index"):  # another process is rebuilding
        with st.read() as view:
            monkeypatch.setattr(type(view), "_known_sets",
                                lambda self: pytest.fail("fell back to parsing every shard"))
            with pytest.raises(store.StoreError, match="rebuild still running"):
                view.known(urls=["https://a/x"])
            with pytest.raises(store.StoreError, match="rebuild still running"):
                view.known_pids(["doi:10.1234/x"])
    with st.read() as view:  # rebuilt once the lock is free
        assert view.known(urls=["https://a/x"]).urls == {"https://a/x"}
