"""Regression tests for Codex's review of the OpenAlex phase-1 change (MERGE AFTER FIXES):
one section per finding. Recorded fixtures / canned transports, no network."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import dedup
import find_sources
import host_policy
import oa_resolution as oar
import openalex_families as fam
import prune_corpus
import run_round
from test_openalex_sim import (NOW, POLICY, FakeHttp, FakeView, fake_transport, good, load, loc,
                               make_run, page, work)
from test_transient_retry import _run_prune

UA_HONEST = build_corpus.HONEST_UA


@pytest.fixture
def loader(monkeypatch, tmp_path):
    monkeypatch.setattr(build_corpus, "HOST_POLICY", POLICY)
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "TEXT", tmp_path / "text")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})


def row(url, source="openalex_sim", sid="oas-x"):
    return {"id": sid, "title": "X", "url": url, "source": source, "license": "cc-by",
            "topic": "building_energy", "format": "pdf"}


def pdf(body=b"%PDF-1.7 ok"):
    return (200, {"Content-Type": "application/pdf"}, body)


def curl_env(monkeypatch, routes):
    """A fake curl: routes {url: (status, location or None, body)}; records requested URLs."""
    asked = []

    def fake(cmd, **kwargs):
        url = cmd[-1]
        asked.append((url, cmd[cmd.index("-A") + 1]))
        status, location, body = routes[url]
        head = f"HTTP/1.1 {status} X\r\n" + (f"Location: {location}\r\n" if location else "")
        Path(cmd[cmd.index("-D") + 1]).write_text(head + "\r\n")
        kwargs["stdout"].write(body)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_corpus.subprocess, "run", fake)
    return asked


# --- P1.1 redirects must not carry licence evidence to another copy -----------------------------

def test_redirect_to_jstage_is_refused_for_a_licensed_copy_via_requests(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://zenodo.org/licensed.pdf": (
            302, {"Location": "https://www.jstage.jst.go.jp/unlicensed.pdf"}, b""),
        "https://www.jstage.jst.go.jp/unlicensed.pdf": pdf(),
    })
    rec = build_corpus.download_one(row("https://zenodo.org/licensed.pdf"))
    assert requested == ["https://zenodo.org/licensed.pdf"]  # the J-STAGE hop never happened
    assert rec["status"] == "failed" and not rec.get("raw_path")
    assert "NO-GO host www.jstage.jst.go.jp" in rec["error"]
    assert not rec.get("transient")  # this URL does not serve the licensed copy


def test_redirect_to_jstage_is_refused_for_a_licensed_copy_via_curl(monkeypatch, loader):
    fake_transport(monkeypatch, {"https://zenodo.org/licensed.pdf": (403, {}, b"blocked")})
    asked = curl_env(monkeypatch, {
        "https://zenodo.org/licensed.pdf": (302, "https://www.jstage.jst.go.jp/u.pdf", b""),
        "https://www.jstage.jst.go.jp/u.pdf": (200, None, b"%PDF-1.7 " + b"x" * 600),
    })
    rec = build_corpus.download_one(row("https://zenodo.org/licensed.pdf"))
    assert [u for u, _ in asked] == ["https://zenodo.org/licensed.pdf"]
    assert "NO-GO host" in rec["error"] and not rec.get("raw_path")


def test_redirect_to_another_domain_needs_equivalence(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://zenodo.org/a.pdf": (302, {"Location": "https://mirror.example.org/a.pdf"}, b""),
    })
    rec = build_corpus.download_one(row("https://zenodo.org/a.pdf"))
    assert requested == ["https://zenodo.org/a.pdf"]
    assert "leaves the licensed copy (zenodo.org -> mirror.example.org)" in rec["error"]
    assert rec["redirect_chain"] == ["https://zenodo.org/a.pdf"]
    assert rec["refused_hop"] == "https://mirror.example.org/a.pdf"


UUID = "2e0aa826-03a0-4f97-a019-d266f30c50a9"


@pytest.mark.parametrize("origin, dest, ok", [
    # the same repository identifier on the same host / a configured host pair
    ("https://zenodo.org/records/1/files/a.pdf",
     "https://zenodo.org/api/records/1/files/a.pdf/content", True),
    (f"https://www.repository.cam.ac.uk/bitstreams/{UUID}/download",
     f"https://api.repository.cam.ac.uk/server/api/core/bitstreams/{UUID}/content", True),
    ("https://europepmc.org/articles/PMC123/pdf", "https://www.ebi.ac.uk/x/PMC123.pdf", True),
    ("https://arxiv.org/pdf/2608.16638v2", "https://arxiv.org/pdf/2608.16638v2.pdf", True),
    ("https://arxiv.org/pdf/2608.16638", "https://arxiv.org/pdf/2608.16638v2", False),
    ("https://repo.example.edu/handle/1234/567", "https://repo.example.edu/bitstream/1234/567/"
                                                 "thesis.pdf", True),
    ("https://orbi.example.be/files/energy-model-paper.pdf",
     "https://orbi.example.be/cdnfiles/energy-model-paper.pdf", True),
    # a shared domain is NOT a shared copy
    ("https://zenodo.org/records/1/files/a.pdf", "https://zenodo.org/records/2/files/a.pdf",
     False),
    ("https://a.vic.edu.au/x/1234567", "https://b.vic.edu.au/x/1234567", False),
    ("https://a.ac.uk/x", "https://b.ac.uk/x", False),
    ("https://lirias.kuleuven.be/retrieve/1", "https://lirias2repo.kuleuven.be/x.pdf", False),
    (f"https://www.repository.cam.ac.uk/bitstreams/{UUID}/download",
     "https://api.repository.cam.ac.uk/server/api/core/bitstreams/"
     "11111111-2222-3333-4444-555555555555/content", False),
    ("https://repo.example.org/a/fulltext.pdf", "https://repo.example.org/b/fulltext.pdf",
     False),  # a generic file name identifies nothing
    ("https://lbl-srg.github.io/x", "https://other.github.io/x", False),
    ("https://zenodo.org/records/1", "https://zenodo.org.evil.example/records/1", False),
])
def test_copy_equivalence_rules(origin, dest, ok):
    assert oar.same_copy(origin, dest) is ok


def test_cdn_redirect_is_followed_and_the_chain_recorded(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://europepmc.org/articles/PMC7/pdf": (
            302, {"Location": "https://www.ebi.ac.uk/europepmc/PMC7.pdf"}, b""),
        "https://www.ebi.ac.uk/europepmc/PMC7.pdf": pdf(),
    })
    rec = build_corpus.download_one(row("https://europepmc.org/articles/PMC7/pdf"))
    assert rec.get("error") is None
    assert rec["final_url"] == "https://www.ebi.ac.uk/europepmc/PMC7.pdf"
    assert rec["redirect_chain"] == requested


def test_rows_without_copy_bound_rights_may_follow_resolvers(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://doi.org/10.5281/zenodo.1": (302, {"Location": "https://zenodo.org/a.pdf"}, b""),
        "https://zenodo.org/a.pdf": pdf(),
    })
    rec = build_corpus.download_one(row("https://doi.org/10.5281/zenodo.1", source="curated"))
    assert rec.get("error") is None
    assert rec["redirect_chain"] == requested and rec["final_url"] == "https://zenodo.org/a.pdf"


# --- P1.2 one canonical hostname everywhere ----------------------------------------------------

@pytest.mark.parametrize("value, host", [
    ("https://papers.ssrn.com./x", "papers.ssrn.com"),
    ("https://cdn.mdpi.com..:443/a", "cdn.mdpi.com"),
    ("https://user:pw@SSRN.COM:8443/x", "ssrn.com"),
    ("ssrn.com.", "ssrn.com"),
    ("https://ｓｓｒｎ.com/x", "ssrn.com"),  # full-width look-alike folds via IDNA
    ("https://bücher.example/x", "xn--bcher-kva.example"),
    ("", ""),
])
def test_canonical_host(value, host):
    assert host_policy.canonical_host(value) == host


@pytest.mark.parametrize("url", ["https://papers.ssrn.com./x", "https://cdn.mdpi.com./a.pdf",
                                 "https://PAPERS.SSRN.COM:443/x", "https://u@ssrn.com./x"])
def test_trailing_dot_and_spelling_variants_are_suspended(url):
    assert host_policy.suspended(url, POLICY)
    assert oar.copy_refusal(url, POLICY) is not None


def test_trailing_dot_direct_url_is_never_requested(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {})
    rec = build_corpus.download_one(row("https://papers.ssrn.com./x.pdf", source="curated"))
    assert requested == [] and rec["transient"]


def test_trailing_dot_intermediate_redirect_is_refused_via_requests(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://doi.org/10.3390/x": (302, {"Location": "https://cdn.mdpi.com./x.pdf"}, b""),
    })
    rec = build_corpus.download_one(row("https://doi.org/10.3390/x", source="curated"))
    assert requested == ["https://doi.org/10.3390/x"]
    assert rec["suspended_redirect"]["host"] == "cdn.mdpi.com"


def test_trailing_dot_intermediate_redirect_is_refused_via_curl(monkeypatch, loader):
    asked = curl_env(monkeypatch, {
        "https://example.org/a.pdf": (302, "https://papers.ssrn.com./a.pdf", b""),
    })
    with pytest.raises(build_corpus.HostSuspended):
        build_corpus._curl_follow("https://example.org/a.pdf")
    assert [u for u, _ in asked] == ["https://example.org/a.pdf"]


# --- P1.3 every statement about a copy is kept -------------------------------------------------

def test_same_location_listed_twice_with_contradictory_licences_fails_closed():
    copy = loc("https://zenodo.org/records/1/files/a.pdf", "cc-by")
    w = work(locations=[{**copy, "license": "cc-by-nc"}])
    w["best_oa_location"] = copy
    assert oar.select_copy(w, POLICY).status == "unresolved"
    w2 = work(locations=[{**copy, "license_id": "https://openalex.org/licenses/cc-by-sa"}])
    w2["best_oa_location"] = copy
    assert oar.select_copy(w2, POLICY).status == "unresolved"  # BY vs BY-SA: a conflict
    same = work(locations=[dict(copy)])
    same["best_oa_location"] = copy
    assert oar.select_copy(same, POLICY).status == "resolved"  # identical statements agree


def test_conflicting_version_statements_fail_closed():
    url = "https://zenodo.org/records/1/files/a.pdf"
    w = work(locations=[loc(url, "cc-by", "acceptedVersion")])
    w["best_oa_location"] = loc(url, "cc-by", "publishedVersion")
    assert oar.select_copy(w, POLICY).status == "unresolved"


# --- P1.4 Crossref grants are version- and date-bound ------------------------------------------

def publisher_copy(version="acceptedVersion", license=None):
    return work(doi="10.1016/j.x.1", locations=[{
        "id": "doi:10.1016/j.x.1", "pdf_url": "https://zenodo.org/records/7/files/p.pdf",
        "license": license, "version": version}])


def grant(kind, url="https://creativecommons.org/licenses/by/4.0/", start=(2020, 1, 1)):
    lic = {"content-version": kind, "URL": url}
    if start is not None:
        lic["start"] = {"date-parts": [list(start)]}
    return {"license": [lic]}


def test_unspecified_crossref_grant_never_grants_on_its_own():
    for version in ("acceptedVersion", "publishedVersion"):
        res = oar.select_copy(publisher_copy(version), POLICY, crossref=grant("unspecified"))
        assert res.status == "unresolved"


def test_future_dated_grant_is_not_a_grant():
    res = oar.select_copy(publisher_copy("acceptedVersion"), POLICY,
                          crossref=grant("am", start=[2030, 1, 1]))
    assert res.status == "unresolved"
    past = oar.select_copy(publisher_copy("acceptedVersion"), POLICY,
                           crossref=grant("am", start=[2020, 1, 1]))
    assert past.status == "resolved" and past.rights.url.endswith("/by/4.0/")
    # an embargoed grant next to an accepted OpenAlex statement fails closed
    both = oar.select_copy(publisher_copy("acceptedVersion", "cc-by"), POLICY,
                           crossref=grant("am", start=[2030, 1, 1]))
    assert both.status == "unresolved"


def test_grant_for_another_version_does_not_apply():
    assert oar.select_copy(publisher_copy("acceptedVersion"), POLICY,
                           crossref=grant("vor")).status == "unresolved"


def test_unspecified_grant_corroborates_and_can_contradict():
    agrees = oar.select_copy(publisher_copy("publishedVersion", "cc-by"), POLICY,
                             crossref=grant("unspecified"))
    assert agrees.status == "resolved"
    contradicts = oar.select_copy(
        publisher_copy("publishedVersion", "cc-by"), POLICY,
        crossref=grant("unspecified", "https://creativecommons.org/licenses/by-nc/4.0/"))
    assert contradicts.status == "unresolved"


# --- P2.5 persistent identity through the store, across rounds and sources ---------------------

def test_a_doi_registered_by_another_source_blocks_the_family():
    keys = dedup.from_sets(set(), set(), set(), pids={"doi:10.1234/good.1"})  # an ojs- row
    run = make_run(keys=keys)
    run.consider(good(1), "building_energy", "sim")
    assert run.out == [] and run.stats.duplicates == {"persistent_id": 1}


def test_a_legacy_row_alias_blocks_a_related_version_in_a_later_round(tmp_path):
    # round 1 registered a legacy ope- row whose origin_ids name the SSRN preprint; round 2
    # (another process, another view) proposes the preprint's DOI under a new id: rejected
    view = FakeView(rows=[{"id": "ope-published", "persistent_id": "https://doi.org/10.5555/p.2",
                           "origin_ids": "doi:10.5555/p.2 doi:10.2139/ssrn.99 openalex:W5"}])
    base = {"source": "openalex_sim", "license": "cc-by", "topic": "building_energy",
            "format": "pdf"}
    again = {**base, "id": dedup.identity_id("10.2139/ssrn.99"), "title": "Preprint",
             "url": "https://zenodo.org/pre.pdf", "persistent_id": "https://doi.org/10.2139/ssrn.99"}
    ojs = {**base, "id": "ojs-x", "source": "ojs", "title": "Other", "url": "https://j.example/x",
           "persistent_id": "https://doi.org/10.5555/P.2"}
    fresh = {**base, "id": dedup.identity_id("10.7777/new"), "title": "New",
             "url": "https://zenodo.org/new.pdf", "persistent_id": "https://doi.org/10.7777/new"}
    path = tmp_path / "p.json"
    path.write_text(json.dumps([again, ojs, fresh]))
    merged, _, _ = run_round.merge_proposals(
        view, [{"index": 0, "name": "find_openalex_sim", "proposal": path}])
    assert [e["title"] for e in merged] == ["New"]


# --- P2.6 no unfinished work passes the cursor; retries settle only via the store --------------

def preprints(n):
    return [work(f"Modelica building HVAC simulation preprint number {i}", type_="preprint",
                 doi=f"10.2139/ssrn.{i}", wid=f"W{i}", locations=[]) for i in range(n)]


def test_lookup_cap_stops_at_the_unfinished_work(tmp_path):
    http = FakeHttp(pages=[page(preprints(4))])
    ledger = fam.Ledger(tmp_path / "r.jsonl")
    run = make_run(http=http, lookup_max=2, ledger=ledger)  # one work = Crossref + Unpaywall
    nxt = run.run(fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "sim")
    assert (nxt.sim.w, nxt.sim.p, nxt.sim.k) == (0, 1, 1)  # work #1 is next, on the same page
    assert run.stats.stopped == "lookup cap"
    assert list(ledger.rows) == ["doi:10.2139/ssrn.0"]  # only the FINISHED work is recorded


def test_failed_lookup_stops_at_the_work_and_keeps_it(tmp_path):
    class Flaky(FakeHttp):
        def __call__(self, url, **kw):
            if "crossref" in url:
                return SimpleNamespace(status_code=500, headers={}, json=lambda: {})
            return super().__call__(url, **kw)

    run = make_run(http=Flaky(pages=[page(preprints(2))]))
    nxt = run.run(fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "sim")
    assert nxt.sim.k == 0 and run.stats.stopped.startswith("lookup failed")


def test_retry_is_settled_only_once_the_store_holds_the_work(tmp_path):
    ledger = fam.Ledger(tmp_path / "r.jsonl")
    ledger.upsert("doi:10.1234/good.3", ids=["doi:10.1234/good.3", "openalex:W103"],
                  status="unresolved", next_retry_at=fam.iso(NOW - 1), topic="controls_bas",
                  last_tried=fam.iso(NOW - 10), family_name="simulation")
    http = FakeHttp(singles={f"{fam.OPENALEX}/W103": good(3)})
    run = make_run(http=http, ledger=ledger)
    run.run(fam.parse_cursor("sim1 t=2 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "legacy")
    assert run.api.searches == 0 and [e["topic"] for e in run.out] == ["controls_bas"]
    assert ledger.rows["doi:10.1234/good.3"]["status"] == "proposed"  # NOT resolved yet
    # the round rolled back: the store does not hold it, so a later retry proposes it again
    later = NOW + 2 * 86400
    run2 = make_run(http=FakeHttp(singles={f"{fam.OPENALEX}/W103": good(3)}), ledger=ledger)
    run2.now, run2.today = later, fam.iso(later)[:10]
    run2.retry_due()
    assert len(run2.out) == 1
    # once registered, the retry settles
    run3 = make_run(http=FakeHttp(singles={f"{fam.OPENALEX}/W103": good(3)}), ledger=ledger,
                    keys=dedup.from_sets(set(), set(), set(), pids={"doi:10.1234/good.3"}))
    run3.now = NOW + 4 * 86400
    run3.retry_due()
    assert run3.out == [] and ledger.rows["doi:10.1234/good.3"]["status"] == "resolved"


# --- P2.7 malformed responses never advance -----------------------------------------------------

@pytest.mark.parametrize("data", [
    {"error": "Invalid query parameters"},
    {"results": [], "meta": {}},
    {"results": [{"id": "https://openalex.org/W1"}], "meta": {"count": 0}},
    {"results": "x", "meta": {"count": 3}},
    {"results": [{"title": "no id"}], "meta": {"count": 1}},
    [],
])
def test_malformed_search_keeps_the_cursor(data):
    reported, appended = [], []
    args = SimpleNamespace(family_cursor="sim1 t=0 q=1 w=2 p=3 k=4 sq=0 sw=0 sp=1 sk=0",
                           resolution_file=None, append=True, lookup_max=25, per=100, max=25)
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        save_cooldowns=lambda c: None, get=FakeHttp(pages=[data]),
        openalex_relevant=find_sources.openalex_relevant, append_entries=appended.extend,
        request_hold=lambda r: None, report_next=reported.append, now=NOW, sleep=lambda s: None, clock=lambda: NOW, pacer=fam.LocalPacer())
    assert code == 1 and reported == [] and appended == []


def test_malformed_lookup_is_a_failure_not_a_missing_record():
    http = FakeHttp(singles={f"{fam.OPENALEX}/W1": {"error": "x"},
                             "https://api.unpaywall.org/v2/10.1/x": {"error": True}})
    api = fam.Api(http, lookup_max=5, cooldowns={}, save_cooldowns=lambda c: None,
                  now=lambda: NOW, sleep=lambda s: None)
    for kind, key in (("openalex", "W1"), ("unpaywall", "10.1/x")):
        with pytest.raises(fam.UpstreamError):
            api.lookup(kind, key)


# --- P2.8 Unpaywall for works without an OpenAlex PDF, SSRN DOIs included ----------------------

def test_unpaywall_finds_a_licensed_copy_for_a_work_with_no_openalex_pdf():
    w = work(doi="10.1016/j.enbuild.2026.5", locations=[])
    unpaywall = {"doi": "10.1016/j.enbuild.2026.5", "oa_locations": [
        {"url_for_pdf": "https://papers.ssrn.com/x.pdf", "license": "cc-by",
         "version": "acceptedVersion"},
        {"url_for_pdf": "https://zenodo.org/records/5/files/aam.pdf", "license": "cc-by",
         "version": "acceptedVersion"}]}
    http = FakeHttp(singles={"https://api.unpaywall.org/v2/10.1016/j.enbuild.2026.5": unpaywall})
    run = make_run(http=http)
    run.consider(w, "building_energy", "sim")
    (entry,) = run.out
    assert entry["url"] == "https://zenodo.org/records/5/files/aam.pdf"
    assert "unpaywall" in entry["resolution"]


# --- P2.9 suspended-redirect rows are protected, retry history kept ------------------------------

def test_suspended_redirect_is_persisted_and_protected_from_pruning(monkeypatch, loader, tmp_path):
    fake_transport(monkeypatch, {
        "https://doi.org/10.3390/b1": (302, {"Location": "https://www.mdpi.com/b1/pdf"}, b""),
    })
    rec = build_corpus.download_one(row("https://doi.org/10.3390/b1", source="curated",
                                        sid="doi-mdpi-b1"))
    previous = {"transient": True, "retry_attempts": 7,
                "first_failed_at": "2026-01-01T00:00:00Z"}
    build_corpus.note_retry(rec, previous)
    assert rec["suspended_redirect"] == {"host": "www.mdpi.com",
                                         "url": "https://www.mdpi.com/b1/pdf",
                                         "decided_at": "2026-09-25"}
    assert rec["retry_attempts"] == 7 and rec["first_failed_at"] == "2026-01-01T00:00:00Z"
    assert not prune_corpus.retry_pending(rec)  # expired by age...
    removed, blocked, kept = _run_prune(monkeypatch, [rec], tmp_path=tmp_path)
    assert removed == set() and blocked == [] and "doi-mdpi-b1" in kept  # ...but protected


def test_protection_ends_when_the_suspension_is_lifted():
    rec = {"id": "x", "url": "https://doi.org/10.3390/b1",
           "suspended_redirect": {"url": "https://www.mdpi.com/b1/pdf"}}
    assert prune_corpus.protected_ids([rec], POLICY, set()) == {"x"}
    assert prune_corpus.protected_ids([rec], {}, set()) == set()
    assert prune_corpus.protected_ids([{**rec, "suspended_redirect": "junk"}], POLICY,
                                      set()) == set()


# --- P2.10 every destination gets its host's pacing, identity and polite rules -----------------

def test_redirect_destination_uses_its_hosts_ua_semaphore_delay_and_circuit(monkeypatch, loader):
    seen, waited, sems = [], [], []

    def send(self, request, **kwargs):
        seen.append((request.url, request.headers["User-Agent"]))
        resp = requests.Response()
        resp.url, resp.request, resp.connection = request.url, request, self
        if request.url.startswith("https://doi.org/"):
            resp.status_code = 302
            resp.headers["Location"] = "https://publications.ibpsa.org/p.pdf"
        else:
            resp.status_code, resp._content = 202, b"<html>sgcaptcha"
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    real_wait, real_sem = build_corpus._wait_for_host, build_corpus._host_sem
    monkeypatch.setattr(build_corpus, "_wait_for_host", lambda h: waited.append(h) or real_wait(h))
    monkeypatch.setattr(build_corpus, "_host_sem",
                        lambda u: sems.append(host_policy.canonical_host(u)) or real_sem(u))
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *a, **k: pytest.fail("a polite host is never retried with curl"))
    rec = build_corpus.download_one(row("https://doi.org/10.1/x", source="curated"))
    assert seen[1] == ("https://publications.ibpsa.org/p.pdf", UA_HONEST)
    assert sems == waited == ["doi.org", "publications.ibpsa.org"]
    assert rec["transient"] and "challenge" in rec["error"]
    assert build_corpus._tripped("publications.ibpsa.org")  # circuit opened for the destination
    again = build_corpus.download_one(row("https://doi.org/10.1/y", source="curated", sid="y"))
    assert "circuit open" in again["error"] and len(seen) == 3  # destination not requested


def test_curl_never_reaches_a_polite_host(monkeypatch, loader):
    asked = curl_env(monkeypatch, {
        "https://example.org/a.pdf": (302, "https://publications.ibpsa.org/a.pdf", b""),
    })
    assert build_corpus._curl_follow("https://example.org/a.pdf") == b""
    assert [u for u, _ in asked] == ["https://example.org/a.pdf"]


def test_curl_hops_use_each_hosts_ua(monkeypatch, loader):
    monkeypatch.setitem(build_corpus.HOST_UA, "mirror.example.org", "honest/1")
    asked = curl_env(monkeypatch, {
        "https://example.org/a.pdf": (302, "https://mirror.example.org/a.pdf", b""),
        "https://mirror.example.org/a.pdf": (200, None, b"%PDF-1.7 body"),
    })
    assert build_corpus._curl_follow("https://example.org/a.pdf") == b"%PDF-1.7 body"
    assert asked == [("https://example.org/a.pdf", build_corpus.UA),
                     ("https://mirror.example.org/a.pdf", "honest/1")]


# --- P2.11 budget scheduling never depends on a family's progress ------------------------------

def run_id_for(slot):
    return next(r for r in (f"20260925T{i:06d}Z-abc" for i in range(10_000))
                if fam.budget_slot(r) == slot)


def test_budget_slot_comes_from_the_round_not_a_cursor():
    ids = [f"20260925T{i:06d}Z-{i:08x}" for i in range(20_000)]
    slots = [fam.budget_slot(r) for r in ids]
    share = {s: slots.count(s) / len(slots) for s in set(fam.SCHEDULE)}
    assert abs(share["sim"] - 0.6) < 0.03 and abs(share["legacy"] - 0.2) < 0.03
    assert abs(share["ssrn"] - 0.1) < 0.02 and abs(share["ai"] - 0.1) < 0.02
    # a frozen sim cursor (a failing family) does not freeze anyone's slot
    assert len({fam.budget_slot(r, tick=0) for r in ids[:50]}) > 1


def test_legacy_yields_unless_its_slot_or_a_disabled_owner():
    enabled = {"find_openalex_sim": True, "find_openalex_ai": True}
    assert fam.legacy_may_search(run_id_for("legacy"), enabled)[0]
    assert not fam.legacy_may_search(run_id_for("sim"), enabled)[0]
    assert not fam.legacy_may_search(run_id_for("ai"), enabled)[0]
    assert fam.legacy_may_search(run_id_for("ai"), {**enabled, "find_openalex_ai": False})[0]
    assert fam.legacy_may_search(None, enabled)[0]  # standalone


def test_legacy_backend_yields_with_a_hold_and_no_request(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(find_sources, "BACKENDS", {"openalex": lambda *a: calls.append(a) or []})
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    monkeypatch.setattr(find_sources, "load_context", lambda partners=(): (
        POLICY, {"find_openalex_sim": True, "find_openalex_ai": True}))
    monkeypatch.setattr(find_sources.dedup, "open_keys",
                        lambda: find_sources.dedup.from_sets(set(), set(), set()))
    hold = tmp_path / "hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setenv("NEKAISE_RUN_ID", run_id_for("sim"))
    monkeypatch.setattr(sys, "argv", [
        "find_sources.py", "--backends", "openalex", "--query-count", "1",
        "--query-cursor", "7", "--budget-partner", "find_openalex_sim",
        "--budget-partner", "find_openalex_ai"])
    assert find_sources.main() == 0
    assert calls == [] and "belongs to find_openalex_sim" in hold.read_text()


def test_family_off_its_slot_spends_no_search_and_still_advances():
    reported = []
    http = FakeHttp()
    args = SimpleNamespace(family="building-ai", family_cursor="ai1 t=3 q=0 w=0 p=1 k=0",
                           resolution_file=None, append=True, lookup_max=25, per=100, max=25)
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        save_cooldowns=lambda c: None, get=http, openalex_relevant=find_sources.openalex_relevant,
        append_entries=lambda e: None, request_hold=lambda r: None, report_next=reported.append,
        now=NOW, sleep=lambda s: None, clock=lambda: NOW, pacer=fam.LocalPacer(), run_id=run_id_for("sim"))
    assert code == 0 and http.calls == [] and reported == ["ai1 t=4 q=0 w=0 p=1 k=0"]


# --- P3 the building-AI family ----------------------------------------------------------------

def test_ai_family_cursor_and_relevance():
    cur = fam.parse_cursor("ai1 t=1 q=2 w=3 p=4 k=5", fam.BUILDING_AI)
    assert cur.render() == "ai1 t=1 q=2 w=3 p=4 k=5"
    with pytest.raises(ValueError):
        fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0", fam.BUILDING_AI)
    rel = find_sources.openalex_relevant
    bare = {"primary_topic": None, "topics": []}
    keep = ["LLM-Powered Agent for Occupant-Centric Indoor Environment Control",
            "Large language models in building energy applications: a survey",
            "LARGE LANGUAGE MODELS AS TOOLS FOR PUBLIC BUILDING ENERGY MANAGEMENT"]
    drop = ["ProofCouncil: An LLM Agent for Solving Open Mathematical Problems",
            "Building LLM agents for enterprise search",
            "Deep Neural Networks With Koopman Operators for Autonomous Vehicles"]
    assert all(fam.ai_relevant({**bare, "title": t}, rel, True) for t in keep)
    assert not any(fam.ai_relevant({**bare, "title": t}, rel, True) for t in drop)
    # a building-operation phrase in the abstract counts, the verb "building" does not
    abstract = {"abstract_inverted_index": {"we": [0], "control": [1], "HVAC": [2]}}
    assert fam.ai_relevant({**bare, **abstract, "title": "An agentic controller"}, rel, True)
    verb = {"abstract_inverted_index": {"building": [0], "agents": [1]}}
    assert not fam.ai_relevant({**bare, **verb, "title": "An agentic controller"}, rel, True)


def test_ai_family_runs_its_own_walk_and_source():
    http = FakeHttp(pages=[page([work("Large language model agents for HVAC fault diagnosis in "
                                      "buildings", doi="10.1234/ai.1", wid="W301",
                                      locations=[loc("https://zenodo.org/ai.pdf", "cc-by")])])])
    run = make_run(http=http)
    run.family = fam.BUILDING_AI
    nxt = run.run(fam.parse_cursor("ai1 t=0 q=0 w=0 p=1 k=0", fam.BUILDING_AI), "ai")
    (entry,) = run.out
    assert entry["source"] == "openalex_ai" and entry["resolution"].startswith("ai1:")
    assert nxt.render() == "ai1 t=1 q=0 w=1 p=1 k=0"
    assert "large language model" in http.calls[0][1]["filter"]


def test_a_page_is_deduplicated_in_one_store_round_trip():
    keys = dedup.from_sets(set(), set(), set())
    calls = []
    real = keys._backend.lookup
    keys._backend.lookup = lambda *a: calls.append(a) or real(*a)
    works = [good(n) for n in range(5)]
    run = make_run(http=FakeHttp(pages=[page(works)]), keys=keys)
    run.run(fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "sim")
    assert len(run.out) == 5
    assert len(calls[0][3]) == 10  # every DOI + OpenAlex id of the page in the first call
    # the per-work checks after that are answered from the batch (only the selected copies'
    # own new keys, if any, need another lookup)
    assert all(not a[3] for a in calls[1:])
