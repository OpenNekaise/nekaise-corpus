"""The building-simulation OpenAlex family and its shared safeguards (Codex phase 1):
licence adapters, per-copy rights, redirect host policy, DOI/version dedup, cursor preservation,
the shared OpenAlex budget, and SSRN never being fetched. Recorded fixtures, no network."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import build_corpus
import dedup
import find_sources
import lint_registry
import oa_resolution as oar
import openalex_families as fam
import run_round
import store

FIX = Path(__file__).parent / "fixtures" / "openalex"
POLICY = {
    host: {"status": "suspended", "reason": "test", "decided_at": "2026-09-25", "backends": []}
    for host in ("escholarship.org", "mdpi.com", "ssrn.com")
}
NOW = 1_790_000_000.0  # 2026-09-21


def load(name):
    return json.loads((FIX / name).read_text())


def loc(pdf, license=None, version="publishedVersion", **extra):
    return {"pdf_url": pdf, "license": license, "version": version, **extra}


def work(title="Calibrating a Modelica Buildings library model of an office building HVAC plant",
         *, locations=(), doi="10.1234/abc.1", wid="W1", type_="article", **extra):
    return {
        "id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title, "type": type_, "publication_year": 2025,
        "publication_date": "2025-03-01", "language": "en",
        "locations": list(locations), "best_oa_location": None,
        "primary_topic": {"subfield": {"id": "https://openalex.org/subfields/2215"}},
        "topics": [], "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "open_access": {"is_oa": True}, **extra,
    }


# --- licence adapters ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, status, tag, url", [
    ("cc-by", "accepted", "cc-by", None),                      # bare label: no invented version
    ("https://openalex.org/licenses/cc-by-sa", "accepted", "cc-by-sa", None),
    ("CC-BY-4.0", "accepted", "cc-by", "https://creativecommons.org/licenses/by/4.0/"),
    ("CC-BY-SA-4.0", "accepted", "cc-by-sa", "https://creativecommons.org/licenses/by-sa/4.0/"),
    ("cc0", "accepted", "cc0", None),
    ("CC0-1.0", "accepted", "cc0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    ("http://creativecommons.org/licenses/by/4.0/", "accepted", "cc-by",
     "http://creativecommons.org/licenses/by/4.0/"),
    ("https://creativecommons.org/publicdomain/mark/1.0/", "accepted", "public-domain",
     "https://creativecommons.org/publicdomain/mark/1.0/"),
    ("cc-by-nc", "rejected", None, None),
    ("cc-by-nc-sa", "rejected", None, None),
    ("CC-BY-ND-4.0", "rejected", None, None),
    ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "rejected", None, None),
    ("public-domain", "unknown", None, None),                  # a label is not verification
    ("pd", "unknown", None, None),
    ("other-oa", "unknown", None, None),
    ("implied-oa", "unknown", None, None),
    ("publisher-specific-oa", "unknown", None, None),
    ("https://www.elsevier.com/tdm/userlicense/1.0/", "unknown", None, None),
    ("https://example.org/creativecommons.org/licenses/by/4.0/", "unknown", None, None),
])
def test_structured_licence_adapter(value, status, tag, url):
    ev = oar.structured_licence("p", value)
    assert (ev.status, ev.tag, ev.url) == (status, tag, url)


def test_missing_licence_is_no_evidence():
    assert oar.structured_licence("p", None) is None
    assert oar.structured_licence("p", "  ") is None
    assert oar.combine([]).status == "unknown"


def test_combine_is_fail_closed():
    by = oar.structured_licence("openalex", "cc-by")
    by4 = oar.structured_licence("crossref", "https://creativecommons.org/licenses/by/4.0/")
    by3 = oar.structured_licence("x", "https://creativecommons.org/licenses/by/3.0/")
    sa = oar.structured_licence("unpaywall", "cc-by-sa")
    nc = oar.structured_licence("unpaywall", "cc-by-nc")
    other = oar.structured_licence("unpaywall", "publisher-specific-oa")
    assert oar.combine([by, by4]) == oar.Rights(
        "accepted", "cc-by", "https://creativecommons.org/licenses/by/4.0/",
        "openalex=cc-by; crossref=https://creativecommons.org/licenses/by/4.0/")
    assert oar.combine([by]).url is None
    assert oar.combine([by, sa]).status == "conflict"
    assert oar.combine([by, other]).status == "conflict"
    assert oar.combine([by4, by3]).status == "conflict"
    assert oar.combine([by, nc]).status == "rejected"
    pd = oar.structured_licence("openalex", "public-domain")
    assert oar.combine([pd]).status == "unknown"


def test_openalex_license_and_license_id_must_agree():
    agree = oar.openalex_location_evidence(
        {"license": "cc-by", "license_id": "https://openalex.org/licenses/cc-by"})
    assert len(agree) == 2 and oar.combine(agree).status == "accepted"  # both kept, they agree
    clash = oar.openalex_location_evidence(
        {"license": "cc-by", "license_id": "https://openalex.org/licenses/cc-by-nc"})
    assert oar.combine(clash).status == "rejected"


def test_crossref_licence_applies_only_to_its_content_version_and_never_tdm():
    msg = {"license": [
        {"content-version": "vor", "URL": "http://creativecommons.org/licenses/by/4.0/",
         "start": {"date-parts": [[2020, 1, 1]]}},
        {"content-version": "tdm", "URL": "https://www.elsevier.com/tdm/userlicense/1.0/"},
    ]}
    direct, corroborating = oar.crossref_evidence(msg, "publishedVersion")
    assert [e.tag for e in direct] == ["cc-by"] and corroborating == []
    assert oar.crossref_evidence(msg, "acceptedVersion") == ([], [])
    assert oar.crossref_evidence(msg, "submittedVersion") == ([], [])


# --- per-copy rights ----------------------------------------------------------------------------

def test_rights_are_required_for_the_selected_copy_and_versions_are_independent():
    w = work(locations=[
        loc("https://www.frontiersin.org/articles/1/pdf", "cc-by-nc"),            # published: NC
        loc("https://zenodo.org/records/9/files/am.pdf", "cc-by", "acceptedVersion"),
        loc("https://arxiv.org/pdf/2501.00001", None, "submittedVersion"),       # arXiv default
    ])
    res = oar.select_copy(w, POLICY)
    assert res.status == "resolved"
    assert res.copy.url == "https://zenodo.org/records/9/files/am.pdf"
    assert res.copy.version == "acceptedVersion"
    assert res.rights.tag == "cc-by"
    assert "rights_rejected:www.frontiersin.org" in res.reasons
    assert "rights_unknown:arxiv.org" in res.reasons


def test_unknown_licence_never_becomes_open():
    w = work(locations=[loc("https://zenodo.org/records/9/files/a.pdf", "other-oa"),
                        loc("https://arxiv.org/pdf/2501.00002", None)])
    res = oar.select_copy(w, POLICY)
    assert res.status == "unresolved" and res.copy is None


def test_two_providers_disagreeing_on_a_copy_leave_it_unresolved():
    w = work(locations=[loc("https://zenodo.org/records/9/files/a.pdf", "cc-by")])
    unpaywall = {"oa_locations": [{"url_for_pdf": "https://zenodo.org/records/9/files/a.pdf",
                                   "license": "cc-by-nc", "version": "publishedVersion"}]}
    assert oar.select_copy(w, POLICY, unpaywall=unpaywall).status == "unresolved"


def test_copies_on_suspended_paused_and_never_hosts_are_not_selected():
    w = work(locations=[
        loc("https://www.mdpi.com/1996-1073/18/10/2539/pdf", "cc-by"),
        loc("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf/a.pdf", "cc-by"),
        loc("https://www.osti.gov/servlets/purl/1", "cc-by"),
        loc("https://papers.ssrn.com/sol3/Delivery.cfm/1.pdf", "cc-by"),
        loc("https://doi.org/10.1234/abc.1", "cc-by"),
    ])
    res = oar.select_copy(w, POLICY)
    assert res.status == "unresolved"
    assert {r.split(":")[0] for r in res.reasons} == {
        "host_suspended", "host_paused", "host_never_fetch", "resolver_url"}
    # ssrn is refused even if the pinned policy lost it
    assert oar.copy_refusal("https://papers.ssrn.com/x.pdf", {}) == "host_never_fetch:ssrn.com"


def test_jstage_work_is_excluded_whatever_copy_exists():
    w = work(locations=[loc("https://zenodo.org/records/9/files/a.pdf", "cc-by"),
                        {"landing_page_url": "https://www.jstage.jst.go.jp/article/x/_article"}])
    assert oar.select_copy(w, POLICY).status == "excluded"


def test_recorded_ssrn_preprint_stays_unresolved():
    w = load("ssrn_work.json")
    crossref = load("crossref_ssrn.json")
    unpaywall = load("unpaywall_ssrn.json")
    res = oar.select_copy(w, POLICY, crossref=crossref, unpaywall=unpaywall)
    assert res.status == "unresolved" and res.reasons == ["no_pdf_copy"]


def test_recorded_mdpi_work_does_not_borrow_another_versions_licence():
    page = load("search_modelica_2025.json")
    mdpi = next(w for w in page["results"] if "10.3390" in (w["doi"] or ""))
    # the publisher PDF (cc-by) is on a suspended host; the repository and DOAJ locations carry
    # other licences but no PDF: nothing may be selected
    res = oar.select_copy(mdpi, POLICY)
    assert res.status == "unresolved"
    assert "host_suspended:www.mdpi.com" in res.reasons


# --- version identity ---------------------------------------------------------------------------

def test_same_work_needs_title_and_author_evidence():
    a = {"title": "Automated Modelica model repair with language model agents", "year": 2026,
         "authors": ["Shutong Feng", "Yaoli Zhang"]}
    assert oar.same_work(a, {**a, "authors": ["S. Feng"]})
    assert not oar.same_work(a, {**a, "authors": ["Someone Else"]})       # ambiguous
    assert not oar.same_work(a, {**a, "title": "Modelica model repair"})   # too different
    assert not oar.same_work(a, {**a, "year": 2019})


def test_crossref_relations_explicit_or_confirmed():
    msg = {"relation": {
        "is-preprint-of": [{"id": "10.1016/J.ENBUILD.2026.1", "id-type": "doi"}],
        "references": [{"id": "10.9999/weak", "id-type": "doi"}],
        "is-supplemented-by": [{"id": "https://example.org/x", "id-type": "uri"}],
    }}
    rec = {"title": "Automated Modelica model repair with language model agents",
           "authors": ["Shutong Feng"], "year": 2026}
    assert oar.crossref_related_dois(msg) == [("is-preprint-of", "10.1016/j.enbuild.2026.1")]
    assert oar.crossref_related_dois(msg, rec, lambda d: dict(rec)) == [
        ("is-preprint-of", "10.1016/j.enbuild.2026.1"), ("references", "10.9999/weak")]
    assert oar.crossref_related_dois(msg, rec, lambda d: None) == [
        ("is-preprint-of", "10.1016/j.enbuild.2026.1")]


# --- DOI / version dedup ------------------------------------------------------------------------

def test_persistent_ids_normalize_to_one_identity_id():
    spellings = ["https://doi.org/10.2139/SSRN.7231715", "doi:10.2139/ssrn.7231715",
                 "10.2139/ssrn.7231715", "http://dx.doi.org/10.2139/ssrn.7231715"]
    assert {dedup.normalize_pid(s) for s in spellings} == {"doi:10.2139/ssrn.7231715"}
    assert len({dedup.identity_id(s) for s in spellings}) == 1
    assert dedup.identity_id("https://openalex.org/W7172451386") == "oas-w7172451386"
    assert dedup.normalize_pid("NREL/TP-5500-1") is None
    ident = dedup.identity_id("10.2139/ssrn.7231715")
    assert ident.startswith("oas-10-2139-ssrn-7231715-")
    assert store.norm_title("x")  # sanity: store helpers importable


def test_identity_known_covers_every_declared_version():
    held = dedup.identity_id("10.1016/j.enbuild.2026.1")
    keys = dedup.from_sets(set(), set(), {held})
    preprint = {"persistent_id": "https://doi.org/10.2139/ssrn.7231715",
                "origin_ids": "doi:10.2139/ssrn.7231715 doi:10.1016/j.enbuild.2026.1"}
    assert keys.identity_known(preprint)
    assert not keys.identity_known({"persistent_id": "https://doi.org/10.2139/ssrn.1"})
    assert not keys.identity_known({"title": "no identity"})
    keys.add_identity({"persistent_id": "https://doi.org/10.5555/new"})
    assert keys.identity_known({"origin_ids": "doi:10.5555/NEW"})


class FakeView:
    def __init__(self, ids=(), urls=(), titles=(), rows=()):
        self.ids, self.urls, self.titles = set(ids), set(urls), set(titles)
        self.rows = list(rows)  # registry/manifest rows with persistent_id / origin_ids

    def known_pids(self, pids):
        declared = {p for row in self.rows for p in store.codec.row_pids(row)}
        return frozenset(set(pids) & declared)

    def known(self, *, urls=(), titles=(), ids=(), include_blocklist=True):
        return store.KnownHits(frozenset(set(urls) & self.urls),
                               frozenset(set(titles) & self.titles),
                               frozenset(set(ids) & self.ids))


def test_serial_merge_rejects_the_same_doi_under_another_url_and_title(tmp_path):
    held = dedup.identity_id("10.1016/j.enbuild.2026.1")

    def proposal(name, entries):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(entries))
        return path

    base = {"source": "openalex_sim", "license": "cc-by", "topic": "building_energy",
            "format": "pdf"}
    a = {**base, "id": dedup.identity_id("10.7777/a"), "title": "Paper A", "url": "https://zenodo.org/a.pdf",
         "persistent_id": "https://doi.org/10.7777/a"}
    a_again = {**base, "id": "ojs-paper-a-copy", "title": "Paper A (journal copy)",
               "url": "https://journal.example/a.pdf", "persistent_id": "https://doi.org/10.7777/A"}
    b = {**base, "id": dedup.identity_id("10.2139/ssrn.9"), "title": "Paper B preprint",
         "url": "https://zenodo.org/b.pdf", "persistent_id": "https://doi.org/10.2139/ssrn.9",
         "origin_ids": "doi:10.2139/ssrn.9 doi:10.1016/j.enbuild.2026.1"}
    legacy = {**base, "id": "ope-legacy", "source": "openalex", "title": "Legacy",
              "url": "https://zenodo.org/legacy.pdf"}
    results = [
        {"index": 0, "name": "find_openalex_sim", "proposal": proposal("one", [a, b, legacy])},
        {"index": 1, "name": "find_ojs", "proposal": proposal("two", [a_again])},
    ]
    merged, accepted, _ = run_round.merge_proposals(FakeView(ids={held}), results)
    assert [e["title"] for e in merged] == ["Paper A", "Legacy"]
    assert accepted == {"find_openalex_sim": 2, "find_ojs": 0}


def test_family_distinguishes_held_from_known_not_held():
    ident = dedup.identity_id("10.1234/abc.1")
    rows = {}
    run = make_run(keys=dedup.from_sets(set(), set(), {ident}),
                   held_rows=lambda ids: {i: rows[i] for i in ids if i in rows})
    run.consider(work(locations=[loc("https://zenodo.org/r/1/a.pdf", "cc-by")]),
                 "building_energy", "sim")
    assert run.stats.duplicates == {"identity_known_not_held": 1}
    assert run.ledger.rows["doi:10.1234/abc.1"]["status"] == "known_not_held"
    assert run.out == []  # never replaced automatically
    rows[ident] = {"id": ident, "status": "ok"}
    run2 = make_run(keys=dedup.from_sets(set(), set(), {ident}),
                    held_rows=lambda ids: {i: rows[i] for i in ids if i in rows})
    run2.consider(work(locations=[loc("https://zenodo.org/r/1/a.pdf", "cc-by")]),
                  "building_energy", "sim")
    assert run2.stats.duplicates == {"identity_held": 1}


def test_blocklisted_location_url_keeps_a_prune_decision():
    pruned = "https://zenodo.org/r/1/a.pdf"
    run = make_run(keys=dedup.from_sets({pruned}, set(), set()))
    run.consider(work(locations=[loc("https://arxiv.org/pdf/2501.1", "cc-by"),
                                 loc(pruned, "cc-by")]), "building_energy", "sim")
    assert run.out == [] and run.stats.duplicates == {"url": 1}


# --- family runs: fake API ----------------------------------------------------------------------

class FakeHttp:
    """requests.get replacement: routes by URL; records every request."""

    def __init__(self, pages=None, singles=None, fail_search=False, remaining="700"):
        self.pages = list(pages or [])
        self.singles = singles or {}
        self.fail_search = fail_search
        self.remaining = remaining
        self.calls = []

    def __call__(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        host = url.split("/")[2]
        assert host in {"api.openalex.org", "api.crossref.org", "api.unpaywall.org"}, url
        if url == fam.OPENALEX:
            if self.fail_search:
                return SimpleNamespace(status_code=503, headers={"Retry-After": "120"},
                                       json=lambda: {})
            data = self.pages.pop(0)
            return SimpleNamespace(status_code=200, json=lambda: data,
                                   headers={"x-ratelimit-remaining": self.remaining,
                                            "x-ratelimit-reset": "3600"})
        data = self.singles.get(url)
        if data is None:
            return SimpleNamespace(status_code=404, headers={}, json=lambda: {})
        return SimpleNamespace(status_code=200, headers={}, json=lambda: data)


def make_run(*, http=None, keys=None, max_docs=25, lookup_max=25, per=100, ledger=None,
             held_rows=lambda ids: {}, cooldowns=None):
    api = fam.Api(http or FakeHttp(), lookup_max=lookup_max, cooldowns=cooldowns or {},
                  save_cooldowns=lambda c: None, now=lambda: NOW, sleep=lambda s: None)
    return fam.FamilyRun(api=api, policy=POLICY, keys=keys or dedup.from_sets(set(), set(), set()),
                         ledger=ledger or fam.Ledger(None), per=per, max_docs=max_docs,
                         openalex_relevant=find_sources.openalex_relevant, now=NOW,
                         held_rows=held_rows)


def page(works, count=None):
    return {"meta": {"count": count if count is not None else len(works)}, "results": works}


def good(n, **kw):
    return work(f"EnergyPlus calibration of office building HVAC model number {n}",
                doi=f"10.1234/good.{n}", wid=f"W{100 + n}",
                locations=[loc(f"https://zenodo.org/records/{n}/files/p.pdf", "cc-by")], **kw)


def test_cursor_round_trip_and_version_guard():
    cur = fam.parse_cursor("sim1 t=12 q=3 w=2 p=4 k=17 sq=1 sw=0 sp=2 sk=0")
    assert cur.render() == "sim1 t=12 q=3 w=2 p=4 k=17 sq=1 sw=0 sp=2 sk=0"
    with pytest.raises(ValueError, match="migrate"):
        fam.parse_cursor("sim2 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0")
    with pytest.raises(ValueError):
        fam.parse_cursor(f"sim1 t=0 q={len(fam.SIM_QUERIES)} w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0")


def test_max_cap_keeps_the_unfinished_page_and_resumes_after_consumed_results():
    works = [good(n) for n in range(5)]
    http = FakeHttp(pages=[page(works, count=500), page(works, count=500)])
    run = make_run(http=http, max_docs=2, per=5)
    nxt = run.run(fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "sim")
    assert [e["url"] for e in run.out] == [w["locations"][0]["pdf_url"] for w in works[:2]]
    assert nxt.render() == "sim1 t=1 q=0 w=0 p=1 k=2 sq=0 sw=0 sp=1 sk=0"
    assert run.stats.stopped == "max"
    # the next run re-reads the same page and continues at result 2
    run2 = make_run(http=http, max_docs=2, per=5)
    nxt2 = run2.run(nxt, "sim")
    assert [e["url"] for e in run2.out] == [w["locations"][0]["pdf_url"] for w in works[2:4]]
    assert nxt2.render() == "sim1 t=2 q=0 w=0 p=1 k=4 sq=0 sw=0 sp=1 sk=0"
    assert http.calls[0][1]["page"] == http.calls[1][1]["page"] == 1


def test_finished_page_advances_page_then_window_then_query():
    http = FakeHttp(pages=[page([good(1)] * 1 + [work("x", doi=None, wid="W9")] * 4, count=12),
                           page([good(2)], count=6)])
    run = make_run(http=http, per=5)
    nxt = run.run(fam.parse_cursor("sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"), "sim")
    assert (nxt.sim.q, nxt.sim.w, nxt.sim.p, nxt.sim.k) == (0, 0, 2, 0)
    nxt = make_run(http=http, per=5).run(nxt, "sim")  # short page: window done
    assert (nxt.sim.q, nxt.sim.w, nxt.sim.p, nxt.sim.k) == (0, 1, 1, 0)
    last = fam.Position(q=len(fam.SIM_QUERIES) - 1, w=len(fam.WINDOWS) - 1, p=3, k=9)
    last.advance_window(len(fam.SIM_QUERIES))
    assert (last.q, last.w, last.p, last.k) == (0, 0, 1, 0)  # a new pass, never "exhausted"


def test_upstream_failure_keeps_cursor_and_is_not_exhaustion(tmp_path):
    reported, holds, appended = [], [], []
    args = SimpleNamespace(family_cursor="sim1 t=0 q=2 w=1 p=3 k=5 sq=0 sw=0 sp=1 sk=0",
                           resolution_file=None, append=True, lookup_max=25, per=100, max=25)
    saved = {}
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        save_cooldowns=saved.update, get=FakeHttp(fail_search=True),
        openalex_relevant=find_sources.openalex_relevant, append_entries=appended.extend,
        request_hold=holds.append, report_next=reported.append, now=NOW, sleep=lambda s: None, clock=lambda: NOW, pacer=fam.LocalPacer())
    assert code == 1
    assert reported == [] and appended == []   # nothing proposed, the committed cursor stays
    assert saved["openalex"] == NOW + 120      # Retry-After persisted as a cooldown


def test_active_cooldown_holds_without_a_request():
    holds, reported = [], []
    http = FakeHttp()
    args = SimpleNamespace(family_cursor="sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0",
                           resolution_file=None, append=True, lookup_max=25, per=100, max=25)
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()),
        cooldowns={"openalex": NOW + 60}, save_cooldowns=lambda c: None, get=http,
        openalex_relevant=find_sources.openalex_relevant, append_entries=lambda e: None,
        request_hold=holds.append, report_next=reported.append, now=NOW, sleep=lambda s: None, clock=lambda: NOW, pacer=fam.LocalPacer())
    assert code == 0 and holds and reported == [] and http.calls == []


def test_budget_headers_persist_a_cooldown_until_reset():
    saved = {}
    http = FakeHttp(pages=[page([])], remaining="5")
    api = fam.Api(http, lookup_max=0, cooldowns={}, save_cooldowns=saved.update,
                  now=lambda: NOW, sleep=lambda s: None)
    api.search({}, 100)
    assert saved == {"openalex": NOW + 3600}


def test_successful_run_reports_next_cursor_and_rights_evidence(tmp_path):
    reported, appended = [], []
    http = FakeHttp(pages=[page([good(1)])])
    args = SimpleNamespace(family_cursor="sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0",
                           resolution_file=str(tmp_path / "r.jsonl"), append=True,
                           lookup_max=25, per=100, max=25)
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        save_cooldowns=lambda c: None, get=http, openalex_relevant=find_sources.openalex_relevant,
        append_entries=appended.extend, request_hold=lambda r: None,
        report_next=reported.append, now=NOW, sleep=lambda s: None, clock=lambda: NOW, pacer=fam.LocalPacer())
    assert code == 0
    assert reported == ["sim1 t=1 q=0 w=1 p=1 k=0 sq=0 sw=0 sp=1 sk=0"]
    (entry,) = appended
    assert entry["id"] == dedup.identity_id("10.1234/good.1")
    assert entry["license"] == "cc-by" and "license_url" not in entry  # bare label: no version
    assert entry["persistent_id"] == "https://doi.org/10.1234/good.1"
    assert entry["origin_ids"] == "doi:10.1234/good.1 openalex:W101"
    assert entry["selected_version"] == "publishedVersion"
    assert entry["rights_verified_at"] == "2026-09-21"
    assert lint_registry.entry_errors(entry, "papers.yaml") == []
    params = http.calls[0][1]
    assert "title_and_abstract.search:" in params["filter"]
    assert "type:article|preprint" in params["filter"]


def test_lint_rejects_an_openalex_sim_entry_without_evidence():
    entry = {"id": "oas-x", "title": "t", "url": "https://zenodo.org/a.pdf",
             "source": "openalex_sim", "license": "open", "topic": "building_energy",
             "format": "pdf"}
    errors = lint_registry.entry_errors(entry, "papers.yaml")
    assert any("requires an evidenced open licence" in e for e in errors)
    assert any("lacks license_evidence" in e for e in errors)


def test_relevance_needs_simulation_and_building_evidence():
    rel = find_sources.openalex_relevant
    no_subfield = {"primary_topic": None, "topics": []}
    assert fam.sim_relevant({**no_subfield, "title": "EnergyPlus model calibration of an "
                                                     "office building"}, rel)
    assert not fam.sim_relevant({**no_subfield, "title": "Digital twin of a jet engine"}, rel)
    assert not fam.sim_relevant({**no_subfield, "title": "Multi-agent systems for energy "
                                                         "trading"}, rel)
    assert not fam.sim_relevant({**no_subfield, "title": "Retrofit of office buildings"}, rel)
    assert fam.sim_relevant({**no_subfield, "title": "Gebäudesimulation im Bestand"}, rel)
    ssrn = load("ssrn_work.json")
    assert fam.sim_relevant(ssrn, rel)  # Modelica is a building-simulation anchor


def test_ssrn_family_never_requests_ssrn_and_records_the_work(tmp_path):
    ssrn = load("ssrn_work.json")
    unpaywall = dict(load("unpaywall_ssrn.json"))
    # even an SSRN PDF offered by Unpaywall is never a copy to fetch
    unpaywall["oa_locations"] = [{"url_for_pdf": "https://papers.ssrn.com/sol3/Delivery.cfm/x.pdf",
                                  "license": "cc-by", "version": "acceptedVersion"}]
    http = FakeHttp(pages=[page([ssrn])], singles={
        "https://api.crossref.org/works/10.2139/ssrn.7231715": {"message":
                                                                load("crossref_ssrn.json")},
        "https://api.unpaywall.org/v2/10.2139/ssrn.7231715": unpaywall})
    ledger = fam.Ledger(tmp_path / "r.jsonl")
    run = make_run(http=http, ledger=ledger)
    cursor = fam.parse_cursor("sim1 t=5 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0")
    assert fam.budget_slot(None, cursor.t) == "ssrn"
    nxt = run.run(cursor, "ssrn")
    assert run.out == []
    assert all("ssrn.com" not in url for url, _ in http.calls)
    assert f"primary_location.source.id:{fam.SSRN_SOURCE_ID}" in http.calls[0][1]["filter"]
    assert "open_access.is_oa" not in http.calls[0][1]["filter"]  # no known OA copy needed
    assert ledger.rows["doi:10.2139/ssrn.7231715"]["status"] == "unresolved"
    assert "host_never_fetch:ssrn.com" in ledger.rows["doi:10.2139/ssrn.7231715"]["reasons"]
    # the Unpaywall METADATA lookup ran for the SSRN DOI (no content request to SSRN)
    assert "https://api.unpaywall.org/v2/10.2139/ssrn.7231715" in [u for u, _ in http.calls]
    assert (nxt.ssrn.w, nxt.sim.w) == (1, 0)


def test_ssrn_preprint_resolves_through_an_explicit_published_version(tmp_path):
    ssrn = load("ssrn_work.json")
    crossref = dict(load("crossref_ssrn.json"))
    crossref["relation"] = {"is-preprint-of": [{"id": "10.1016/j.autcon.2026.9",
                                                "id-type": "doi"}]}
    published = work(ssrn["title"], doi="10.1016/j.autcon.2026.9", wid="W8",
                     locations=[loc("https://zenodo.org/records/8/files/aam.pdf",
                                    "https://openalex.org/licenses/cc-by", "acceptedVersion")])
    http = FakeHttp(singles={
        "https://api.crossref.org/works/10.2139/ssrn.7231715": {"message": crossref},
        f"{fam.OPENALEX}/doi:10.1016/j.autcon.2026.9": published})
    run = make_run(http=http)
    run.consider(ssrn, "building_energy", "ssrn")
    (entry,) = run.out
    assert entry["url"] == "https://zenodo.org/records/8/files/aam.pdf"
    assert entry["persistent_id"] == "https://doi.org/10.1016/j.autcon.2026.9"
    assert "doi:10.2139/ssrn.7231715" in entry["origin_ids"].split()
    assert "is-preprint-of" in entry["resolution"]
    assert entry["selected_version"] == "acceptedVersion"


# --- shared OpenAlex budget ---------------------------------------------------------------------

def test_real_backends_share_the_openalex_budget():
    backends = run_round.load_backends()
    sim = backends["find_openalex_sim"]
    assert sim["script"] == "find_sources.py" and sim["required"] is False
    assert sim["args"][:2] == ["--family", "simulation"]
    for flag, value in (("--max", "25"), ("--lookup-max", "250"), ("--per", "100")):
        assert sim["args"][sim["args"].index(flag) + 1] == value
    legacy = backends["find_openalex"]["args"]
    assert legacy[legacy.index("--budget-partner") + 1] == "find_openalex_sim"
    command = run_round.finder_command(
        "find_openalex_sim", sim,
        {"find_openalex_sim": {"flag": "--family-cursor",
                               "next": "sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"}},
        python="python")
    assert command[-3:] == ["--family-cursor", "sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0",
                            "--append"]


# --- loader: host policy on every redirect hop --------------------------------------------------

def fake_transport(monkeypatch, routes):
    """Route requests' HTTP adapter to canned responses; requests' own redirect machinery (and
    the loader's hook) runs for real. Returns the list of URLs actually requested."""
    requested = []

    def send(self, request, **kwargs):
        requested.append(request.url)
        status, headers, body = routes[request.url]
        resp = requests.Response()
        resp.status_code, resp._content, resp.url = status, body, request.url
        resp.headers.update(headers)
        resp.request, resp.connection = request, self
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return requested


@pytest.fixture
def loader(monkeypatch, tmp_path):
    monkeypatch.setattr(build_corpus, "HOST_POLICY", POLICY)
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "TEXT", tmp_path / "text")
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *a, **k: pytest.fail("curl must not run for a suspended hop"))


def src(url):
    return {"id": "oas-x", "title": "X", "url": url, "source": "openalex_sim",
            "license": "cc-by", "topic": "building_energy", "format": "pdf"}


def test_doi_redirect_to_ssrn_is_refused_before_the_request(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://doi.org/10.2139/ssrn.7231715": (
            302, {"Location": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7231715"}, b""),
    })
    rec = build_corpus.download_one(src("https://doi.org/10.2139/ssrn.7231715"))
    assert requested == ["https://doi.org/10.2139/ssrn.7231715"]
    assert "fetch-suspended host papers.ssrn.com" in rec["error"]
    assert rec["transient"] is True and rec["status"] == "failed"
    build_corpus.note_retry(rec, None)
    assert rec["retry_attempts"] == 0 and "first_failed_at" not in rec  # no ageing


def test_relative_and_cdn_hops_are_checked_too(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://zenodo.org/records/1/files/a.pdf": (
            301, {"Location": "/api/records/1/files/a.pdf/content"}, b""),
        "https://zenodo.org/api/records/1/files/a.pdf/content": (
            302, {"Location": "https://cdn.mdpi.com/a.pdf"}, b""),
    })
    rec = build_corpus.download_one(src("https://zenodo.org/records/1/files/a.pdf"))
    assert requested == ["https://zenodo.org/records/1/files/a.pdf",
                         "https://zenodo.org/api/records/1/files/a.pdf/content"]
    assert "cdn.mdpi.com" in rec["error"] and rec["transient"]


def test_allowed_redirect_chain_still_downloads_and_is_recorded(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {
        "https://zenodo.org/records/1/files/a.pdf": (
            302, {"Location": "https://zenodo.org/api/records/1/files/a.pdf/content"}, b""),
        "https://zenodo.org/api/records/1/files/a.pdf/content": (
            200, {"Content-Type": "application/pdf"}, b"%PDF-1.7 ok"),
    })
    rec = build_corpus.download_one(src("https://zenodo.org/records/1/files/a.pdf"))
    assert rec.get("error") is None and rec["bytes"] == len(b"%PDF-1.7 ok")
    assert requested[-1] == "https://zenodo.org/api/records/1/files/a.pdf/content"
    assert rec["redirect_chain"] == requested
    assert rec["final_url"] == requested[-1]


def test_a_suspended_registry_url_is_never_requested(monkeypatch, loader):
    requested = fake_transport(monkeypatch, {})
    rec = build_corpus.download_one(src("https://papers.ssrn.com/sol3/Delivery.cfm?id=1"))
    assert requested == [] and rec["transient"]


def test_curl_fallback_follows_hops_itself_and_checks_each(monkeypatch, tmp_path):
    monkeypatch.setattr(build_corpus, "HOST_POLICY", POLICY)
    commands = []

    def fake_curl(cmd, **kwargs):
        commands.append(cmd)
        assert "-L" not in cmd and "-sSL" not in cmd
        headers = Path(cmd[cmd.index("-D") + 1])
        if cmd[-1] == "https://example.org/a.pdf":
            headers.write_bytes(b"HTTP/2 302\r\nlocation: https://papers.ssrn.com/a.pdf\r\n\r\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_corpus.subprocess, "run", fake_curl)
    with pytest.raises(build_corpus.HostSuspended):
        build_corpus._curl_follow("https://example.org/a.pdf", "ua")
    assert [c[-1] for c in commands] == ["https://example.org/a.pdf"]


def test_curl_fallback_follows_an_allowed_redirect(monkeypatch):
    monkeypatch.setattr(build_corpus, "HOST_POLICY", POLICY)
    seen = []

    def fake_curl(cmd, **kwargs):
        seen.append(cmd[-1])
        headers = Path(cmd[cmd.index("-D") + 1])
        if cmd[-1] == "https://example.org/a.pdf":
            headers.write_bytes(b"HTTP/1.1 301 Moved\r\nLocation: /b.pdf\r\n\r\n")
        else:
            headers.write_bytes(b"HTTP/1.1 200 OK\r\n\r\n")
            kwargs["stdout"].write(b"%PDF-1.4 body")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_corpus.subprocess, "run", fake_curl)
    assert build_corpus._curl_follow("https://example.org/a.pdf", "ua") == b"%PDF-1.4 body"
    assert seen == ["https://example.org/a.pdf", "https://example.org/b.pdf"]


def test_new_hosts_of_scholarly_sources_are_paced(monkeypatch):
    monkeypatch.setattr(build_corpus, "HOST_CONCURRENCY", {"www.osti.gov": 4})
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {"www.boverket.se": 10.0})
    todo = [
        {**src("https://zenodo.org/a.pdf"), "id": "a"},
        {**src("https://www.osti.gov/b.pdf"), "id": "b"},
        {**src("https://www.boverket.se/c.pdf"), "id": "c"},
        {**src("https://restore.example/d.pdf"), "id": "d"},
        {**src("https://other.example/e.pdf"), "id": "e", "source": "ojs"},
    ]
    build_corpus.pace_new_hosts(todo, {"d": {"status": "ok"}})
    assert build_corpus.HOST_CONCURRENCY == {"www.osti.gov": 4, "zenodo.org": 1,
                                             "www.boverket.se": 1}
    assert build_corpus.HOST_DELAY == {"www.boverket.se": 10.0, "zenodo.org": 2.0,
                                       "www.osti.gov": 2.0}


def test_committed_host_policy_suspends_ssrn_and_mdpi_with_no_backends():
    policy = json.loads((Path(__file__).resolve().parents[1] / "registry" /
                         "host_policy.json").read_text())
    assert not build_corpus.host_policy.validate(policy)
    for host in ("ssrn.com", "mdpi.com", "escholarship.org"):
        assert policy["hosts"][host]["status"] == "suspended"
    assert policy["hosts"]["ssrn.com"]["backends"] == []
    assert policy["hosts"]["ssrn.com"]["decided_at"] == "2026-09-25"
    for url in ("https://papers.ssrn.com/x", "https://www.ssrn.com/x", "https://ssrn.com/x"):
        assert build_corpus.host_policy.suspended(url, policy["hosts"])


def test_a_sim_anchored_query_match_implies_the_simulation_anchor_only():
    rel = find_sources.openalex_relevant
    # OpenAlex withholds many publishers' abstracts: the Modelica query match itself proves the
    # simulation anchor, but a building anchor must still come from the record
    no_abstract = {"primary_topic": None, "topics": [],
                   "title": "Defending against cyber-attacks in building HVAC systems"}
    assert not fam.sim_relevant(no_abstract, rel)
    assert fam.sim_relevant(no_abstract, rel, sim_implied=True)
    off_domain = {**no_abstract,
                  "title": "Thermal modelling of a latent heat storage demonstrator"}
    assert not fam.sim_relevant(off_domain, rel, sim_implied=True)
    assert "digital-twin" not in fam.SIM_IMPLIED and "llm-simulation" not in fam.SIM_IMPLIED
    assert fam.SIM_IMPLIED <= {key for key, _, _ in fam.SIM_QUERIES}
