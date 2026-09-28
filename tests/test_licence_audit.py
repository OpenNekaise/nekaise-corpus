"""scripts/audit_licence_evidence.py — licence evidence for arXiv / OpenAlex rows (recorded
fixtures only, no network) and the licence-excluded eligibility it feeds (phase B, lint flag)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import audit_licence_evidence as audit
import clean_corpus
import corpus_stats
import lint_registry
import pipeline_repo
import registry
import store
from runids import rid
from test_store_contract import STORES, mrow, write

FIX = Path(__file__).resolve().parent / "fixtures" / "licence_audit"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


# --- fakes -----------------------------------------------------------------------------------------

class Resp:
    def __init__(self, text="", status=200, headers=None, url="https://fake/"):
        self.text, self.status_code, self.headers, self.url = text, status, headers or {}, url

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    """Answers requests from `route(url, params)`; records every call."""

    def __init__(self, route):
        self.route, self.calls, self.headers = route, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.route(url, params or {})


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def throttle(route, interval=audit.ARXIV_MIN_INTERVAL):
    clock = Clock()
    return audit.Throttle(interval, session=Session(route), sleep=clock.sleep, clock=clock), clock


# --- classification --------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,verdict,tag", [
    ("http://arxiv.org/licenses/nonexclusive-distrib/1.0/", "pointer-only", "arxiv-nonexclusive"),
    ("http://arxiv.org/licenses/assumed-1991-2003/", "pointer-only", "arxiv-nonexclusive"),
    (None, "pointer-only", "arxiv-nonexclusive"),
    ("http://creativecommons.org/licenses/by/4.0/", "eligible", "cc-by"),
    ("http://creativecommons.org/licenses/by-sa/4.0/", "eligible", "cc-by-sa"),
    ("http://creativecommons.org/publicdomain/zero/1.0/", "eligible", "cc0"),
    ("http://creativecommons.org/licenses/publicdomain/", "eligible", "public-domain"),
    ("http://creativecommons.org/licenses/by-nc-nd/4.0/", "excluded-nc-nd", "cc-by-nc-nd"),
    ("http://creativecommons.org/licenses/by-nc-sa/3.0/", "excluded-nc-nd", "cc-by-nc-sa"),
    ("http://creativecommons.org/licenses/by-nc/4.0/", "excluded-nc-nd", "cc-by-nc"),
    ("https://example.org/licenses/by/4.0/", "unresolved", "unverified"),
])
def test_licence_url_classification(raw, verdict, tag):
    got = audit.classify_licence_url(raw)
    assert got[:2] == (verdict, tag)
    assert got[1] in registry.RESTRICTED_USE_LICENSES | {"cc-by", "cc-by-sa", "cc0", "public-domain"}


@pytest.mark.parametrize("raw,verdict,tag", [
    ("cc-by", "eligible", "cc-by"), ("CC-BY-SA", "eligible", "cc-by-sa"),
    ("public-domain", "eligible", "public-domain"), ("cc0", "eligible", "cc0"),
    ("cc-by-nc", "excluded-nc-nd", "cc-by-nc"), ("cc-by-nc-nd", "excluded-nc-nd", "cc-by-nc-nd"),
    ("cc-by-nd", "excluded-nc-nd", "cc-by-nd"),
    ("other-oa", "pointer-only", "publisher-oa"),
    ("publisher-specific-oa", "pointer-only", "publisher-oa"),
    (None, "unresolved", "unverified"), ("", "unresolved", "unverified"),
])
def test_openalex_licence_classification(raw, verdict, tag):
    assert audit.classify_openalex(raw)[:2] == (verdict, tag)


def test_arxiv_ids_and_versions_from_urls():
    assert audit.arxiv_id("https://arxiv.org/pdf/1809.08860") == ("1809.08860", None)
    assert audit.arxiv_id("https://arxiv.org/pdf/2209.13283v3") == ("2209.13283", 3)
    assert audit.arxiv_id("https://arxiv.org/abs/astro-ph/9901234v2") == ("astro-ph/9901234", 2)
    assert audit.arxiv_id("https://arxiv.org/pdf/math.GT/0309136.pdf") == ("math.GT/0309136", None)
    assert audit.arxiv_id("https://e.org/x.pdf") == (None, None)


def test_oai_records_parse():
    rec = audit.parse_arxiv_raw(fixture("arxiv_two_versions_nonexclusive.xml"))
    assert rec["license"] == "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"
    assert [n for n, _ in rec["versions"]] == [1, 2]
    assert rec["versions"][1][1] == "2020-03-27T12:30:33Z"
    assert audit.parse_arxiv_raw(fixture("arxiv_error.xml")) == {"error": "idDoesNotExist"}
    assert "by-nc-nd" in audit.parse_arxiv_raw(fixture("arxiv_by_nc_nd.xml"))["license"]


def test_fetched_version_is_pinned_conservatively():
    vs = [(1, "2019-11-06T05:14:54Z"), (2, "2020-03-27T12:30:33Z")]
    assert audit.fetched_version(3, 1, vs, None) == (3, "url")
    assert audit.fetched_version(None, 1, vs, "2026-01-01T00:00:00Z") == (1, "pdf-stamp")
    assert audit.fetched_version(None, None, vs, "2026-01-01T00:00:00Z") == (2, "dates")
    assert audit.fetched_version(None, None, vs, "2020-01-01T00:00:00Z") == (1, "dates")
    # v2 submitted two days before the fetch: maybe not announced yet
    assert audit.fetched_version(None, None, vs, "2020-03-29T00:00:00Z") == (None,
                                                                             "ambiguous-dates")
    assert audit.fetched_version(None, None, vs, None) == (None, "unknown")
    assert audit.stamp_version("x arXiv:1911.02206v2 [eess.SY] 27 Mar 2020", "1911.02206") == 2
    # a paper citing two versions of itself pins nothing
    assert audit.stamp_version("arXiv:1911.02206v1 ... arXiv:1911.02206v2", "1911.02206") is None


def target(sid="arx-a", url="https://arxiv.org/pdf/1911.02206", fetched="2026-08-23T12:00:00Z",
           **extra):
    return {"id": sid, "url": url, "cohort": "arxiv", "fetched_at": fetched,
            "text_path": f"text/{sid}.md", "text_chars": 4000, "corpus_chars": 4000,
            "source": "arxiv", "license": "open", **extra}


def test_verdict_pins_the_fetched_version_to_the_record_licence():
    two = fixture("arxiv_two_versions_nonexclusive.xml")
    cc_two = two.replace("http://arxiv.org/licenses/nonexclusive-distrib/1.0/",
                         "http://creativecommons.org/licenses/by/4.0/")
    now = "2026-09-25T00:00:00Z"
    res = audit.arxiv_verdict(target(), audit.parse_arxiv_raw(cc_two), None, now)
    assert (res["verdict"], res["licence"], res["version"], res["version_basis"]) == (
        "eligible", "cc-by", "v2", "dates")
    assert res["license_url"] == "http://creativecommons.org/licenses/by/4.0/"
    assert "oai:arXiv.org:1911.02206" in res["evidence"] and "v2" in res["evidence"]
    # the PDF stamp says v1 was fetched: the record licence is v2's, so nothing is pinned
    res = audit.arxiv_verdict(target(), audit.parse_arxiv_raw(cc_two),
                              "arXiv:1911.02206v1 [eess.SY] 6 Nov 2019", now)
    assert (res["verdict"], res["licence"], res["version"]) == ("unresolved", "unverified", "v1")
    res = audit.arxiv_verdict(target(), audit.parse_arxiv_raw(two), None, now)
    assert (res["verdict"], res["licence"]) == ("pointer-only", "arxiv-nonexclusive")
    res = audit.arxiv_verdict(target(), audit.parse_arxiv_raw(fixture("arxiv_error.xml")), None,
                              now)
    assert (res["verdict"], res["licence"]) == ("unresolved", "unverified")
    assert res["license_url"].startswith("https://oaipmh.arxiv.org/oai?verb=GetRecord")


# --- the arXiv pass --------------------------------------------------------------------------------

def write_targets(out: Path, targets: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "targets.jsonl").write_text("".join(json.dumps(t) + "\n" for t in targets))


def test_arxiv_pass_is_throttled_resumable_and_retries_errors(tmp_path):
    out = tmp_path / "audit"
    data = tmp_path / "data"
    (data / "text").mkdir(parents=True)
    (data / "text" / "arx-a.md").write_text("title\narXiv:1911.02206v2 [eess.SY] 27 Mar 2020\n")
    write_targets(out, [
        target("arx-a"),
        target("arx-b", url="https://arxiv.org/pdf/2501.08704"),
        target("arx-c", url="https://arxiv.org/pdf/2311.00720"),
        target("arx-d", url="https://e.org/not-arxiv.pdf")])
    records = {"1911.02206": "arxiv_two_versions_nonexclusive.xml",
               "2501.08704": "arxiv_by_nc_nd.xml", "2311.00720": "arxiv_cc_by.xml"}
    fail = {"2311.00720"}

    def route(url, params):
        aid = params["identifier"].removeprefix("oai:arXiv.org:")
        if aid in fail:
            fail.discard(aid)
            return Resp(status=503, headers={"Retry-After": "7"})
        return Resp(fixture(records[aid]))

    http, clock = throttle(route)
    assert audit.run_arxiv(out, data_root=data, sample=None, seed=1, limit=None, http=http,
                           log=lambda *a: None) == 0
    got = audit.latest_results(out / "results.jsonl")
    assert got["arx-a"]["verdict"] == "pointer-only" and got["arx-a"]["version_basis"] == "pdf-stamp"
    assert got["arx-b"]["verdict"] == "excluded-nc-nd" and got["arx-b"]["licence"] == "cc-by-nc-nd"
    assert got["arx-c"]["verdict"] == "eligible"          # after honouring Retry-After
    assert got["arx-d"]["verdict"] == "unresolved"        # no arXiv id: no request at all
    assert 7 in clock.slept
    calls = http.session.calls
    assert len(calls) == 4 and all(p["metadataPrefix"] == "arXivRaw" for _, p in calls)
    # >= 3 s between consecutive requests on the one session (the clock only moves by sleeping)
    assert sum(clock.slept) >= audit.ARXIV_MIN_INTERVAL * (len(calls) - 1)
    # resumable: a second run asks nothing
    http2, _ = throttle(route)
    audit.run_arxiv(out, data_root=data, sample=None, seed=1, limit=None, http=http2,
                    log=lambda *a: None)
    assert http2.session.calls == []


def test_network_errors_are_recorded_and_retried(tmp_path):
    out = tmp_path / "audit"
    write_targets(out, [target("arx-a")])

    def broken(url, params):
        raise ConnectionError("down")

    http, _ = throttle(broken)
    audit.run_arxiv(out, data_root=None, sample=None, seed=1, limit=None, http=http,
                    log=lambda *a: None)
    assert audit.latest_results(out / "results.jsonl")["arx-a"]["verdict"] is None
    http, _ = throttle(lambda u, p: Resp(fixture("arxiv_two_versions_nonexclusive.xml")))
    audit.run_arxiv(out, data_root=None, sample=None, seed=1, limit=None, http=http,
                    log=lambda *a: None)
    assert audit.latest_results(out / "results.jsonl")["arx-a"]["verdict"] == "pointer-only"


def test_stratified_sample_is_deterministic_and_proportional():
    targets = [target(f"arx-{i}", url=f"https://arxiv.org/pdf/{10 + i % 16:02d}01.0{i:04d}")
               for i in range(400)]
    a = audit.stratified_sample(targets, 50, seed=3)
    assert [t["id"] for t in a] == [t["id"] for t in audit.stratified_sample(targets, 50, seed=3)]
    assert 40 <= len(a) <= 50 and len({t["id"] for t in a}) == len(a)


# --- the OpenAlex pass ----------------------------------------------------------------------------

def oa_target(sid, url):
    return {**target(sid, url=url), "cohort": "openalex", "source": "openalex"}


def test_openalex_pass_matches_the_fetched_location_without_searching(tmp_path):
    out = tmp_path / "audit"
    write_targets(out, [
        oa_target("ope-nc", "https://www.frontiersin.org/articles/10.3389/feart.2021.777323/pdf"),
        oa_target("ope-by", "https://link.springer.com/content/pdf/10.1007/s10462-022-10286-2.pdf"),
        oa_target("ope-esc", "https://escholarship.org/content/qt14b833kh/qt14b833kh.pdf"),
        oa_target("ope-none", "https://repository.example.edu/file/xyz")])

    def route(url, params):
        assert "search" not in params and "search" not in params.get("filter", "")
        head = {"X-RateLimit-Remaining-USD": "0.09"}
        if params["filter"].startswith("doi:"):
            return Resp(fixture("openalex_doi_list.json"), headers=head)
        return Resp(fixture("openalex_landing_list.json"), headers=head)

    http, _ = throttle(route, audit.OPENALEX_MIN_INTERVAL)
    assert audit.run_openalex(out, http=http, log=lambda *a: None) == 0
    got = audit.latest_results(out / "results.jsonl")
    # the fetched Frontiers PDF location is NC even though a PMC location says cc-by
    assert (got["ope-nc"]["verdict"], got["ope-nc"]["licence"]) == ("excluded-nc-nd", "cc-by-nc")
    assert got["ope-nc"]["license_url"] == "https://openalex.org/licenses/cc-by-nc"
    assert (got["ope-by"]["verdict"], got["ope-by"]["licence"]) == ("eligible", "cc-by")
    assert got["ope-esc"]["verdict"] == "unresolved" and got["ope-esc"]["work_id"].endswith("W3")
    assert got["ope-none"]["verdict"] == "unresolved" and got["ope-none"]["work_id"] is None
    doi_filter = http.session.calls[0][1]["filter"]
    assert doi_filter.startswith("doi:") and "10.3389/feart.2021.777323" in doi_filter


def test_openalex_pass_stops_at_the_budget_reserve(tmp_path):
    out = tmp_path / "audit"
    write_targets(out, [oa_target("ope-by", "https://link.springer.com/content/pdf/"
                                            "10.1007/s10462-022-10286-2.pdf")])
    http, _ = throttle(lambda u, p: Resp(fixture("openalex_doi_list.json"),
                                         headers={"X-RateLimit-Remaining-USD": "0.01"}))
    assert audit.run_openalex(out, http=http, log=lambda *a: None) == 2
    assert not (out / "results.jsonl").exists()
    http, _ = throttle(lambda u, p: Resp(status=429, headers={"Retry-After": "60"}))
    assert audit.run_openalex(out, http=http, log=lambda *a: None) == 2


def test_dois_and_landing_pages_from_urls():
    assert audit.doi_from_url("https://www.nature.com/articles/s41467-022-29890-5.pdf") == \
        "10.1038/s41467-022-29890-5"
    assert audit.doi_from_url("https://journals.plos.org/plosone/article/file?id=10.1371/"
                              "journal.pone.0224998&type=printable") == "10.1371/journal.pone.0224998"
    assert audit.doi_from_url("https://ehjournal.biomedcentral.com/counter/pdf/10.1186/"
                              "1476-069X-2-4") == "10.1186/1476-069x-2-4"
    assert audit.doi_from_url("https://www.osti.gov/servlets/purl/1532218") is None
    assert audit.landing_candidates("https://www.osti.gov/servlets/purl/1532218")[0] == \
        "https://www.osti.gov/biblio/1532218"


# --- enumerate, report, apply over a real store -----------------------------------------------------

def audit_store(factory, root: Path):
    pipeline_repo.pin_policy(root, policy={})      # eligibility + host policy for pinned views
    st = factory(root)
    if hasattr(st, "drop"):
        import atexit
        atexit.register(st.drop)
    return st


def sha_of(sid: str) -> str:
    return (sid.encode().hex() * 64)[:64]


def rows_fixture():
    arx = dict(source="arxiv", license="open", corpus_chars=4000)
    rows = [
        mrow("arx-pointer", url="https://arxiv.org/pdf/1911.02206", **arx),
        mrow("arx-cc", url="https://arxiv.org/pdf/2311.00720", **arx),
        mrow("arx-nc", url="https://arxiv.org/pdf/2501.08704", **arx),
        mrow("ope-open", url="https://escholarship.org/content/qt1/qt1.pdf", source="openalex",
             license="open"),
        mrow("ope-arx", url="https://arxiv.org/pdf/1809.08860", source="openalex",
             license="open"),
        mrow("ope-evid", url="https://e.org/ev.pdf", source="openalex", license="open",
             license_evidence="OpenAlex OA location license: other-oa"),
        mrow("ope-real", url="https://e.org/real.pdf", source="openalex", license="open",
             license_evidence="checked by a human: CC BY on the landing page"),
        mrow("oer-open", source="oapen", license="open"),       # other source: counted only
        mrow("hand-pd", license="public-domain", text_chars=10_000_000),
    ]
    for r in rows:
        r["sha256"] = sha_of(r["id"])
        r["corpus_path"] = f"corpus/{r['id']}.md"
    return rows


def seed_rows(st, rows, root: Path | None = None):
    entries = [pipeline_repo.entry_of(r) for r in rows]
    write(st, rid("seed-audit"), lambda tx: (tx.insert_entries(entries),
                                             tx.upsert_manifest(rows)))
    if root is not None:           # the cleaned files the rows claim
        for r in rows:
            path = root / r["corpus_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"cleaned {r['id']}\n")


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_enumerate_finds_the_debt_through_a_view(factory, tmp_path):
    st = audit_store(factory, tmp_path / "repo")
    seed_rows(st, rows_fixture())
    with st.read() as view:
        targets, facts = audit.enumerate_targets(view, snapshot={"commit": "c1"},
                                                 log=lambda *a: None)
    assert {t["id"]: t["cohort"] for t in targets} == {
        "arx-pointer": "arxiv", "arx-cc": "arxiv", "arx-nc": "arxiv",
        "ope-open": "openalex", "ope-arx": "openalex-arxiv",
        "ope-evid": "openalex"}                 # discovery-time evidence is not authoritative
    assert all(t["snapshot"] == {"commit": "c1"} and t["sha256"] for t in targets)
    assert facts["open_without_evidence_by_source"] == {"arxiv": 3, "openalex": 3, "oapen": 1}
    assert facts["open_with_evidence_by_source"] == {"openalex": 1}          # ope-real
    assert facts["targets_by_host"]["openalex"] == {"escholarship.org": 1, "e.org": 1}
    # the lint rule's whole scope, registry-only rows included
    assert facts["lint_scope_without_evidence"]["arx-ok:open"] == 3


def result(sid, url, verdict, licence, checked="2026-09-25T10:00:00Z", payload=None):
    return {"id": sid, "url": url, "cohort": "arxiv", "verdict": verdict, "licence": licence,
            "license_url": "http://arxiv.org/licenses/nonexclusive-distrib/1.0/",
            "evidence": f"arXiv OAI-PMH ... -> {verdict}", "checked_at": checked,
            "reason": "test", "raw_license": None, "version": "v1",
            "evidence_source": "arxiv-oai-pmh:arXivRaw", "resolver": 1,
            "payload_sha256": payload or sha_of(sid)}


def write_audit(out: Path, rows: list[dict], results: list[dict]) -> None:
    by_id = {r["id"]: r for r in rows}
    write_targets(out, [{**by_id[r["id"]], "cohort": r["cohort"]} for r in results])
    audit.append_jsonl(out / "results.jsonl", results)


AUDITED = [
    result("arx-pointer", "https://arxiv.org/pdf/1911.02206", "pointer-only",
           "arxiv-nonexclusive"),
    result("arx-cc", "https://arxiv.org/pdf/2311.00720", "eligible", "cc-by"),
    result("arx-nc", "https://arxiv.org/pdf/2501.08704", "excluded-nc-nd", "cc-by-nc-nd"),
    result("ope-open", "https://escholarship.org/content/qt1/qt1.pdf", "unresolved",
           "unverified"),
    result("ope-evid", "https://e.org/ev.pdf", "pointer-only", "publisher-oa"),
]


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_apply_reclassifies_moves_views_and_never_erases(factory, tmp_path):
    root = tmp_path / "repo"
    st = audit_store(factory, root)
    rows = rows_fixture()
    seed_rows(st, rows, root)
    out = tmp_path / "audit"
    moved_url = result("ope-arx", "https://arxiv.org/pdf/other", "eligible", "cc-by")
    changed_payload = result("arx-cc", "https://arxiv.org/pdf/2311.00720", "eligible",
                             "cc-by", payload="f" * 64)
    write_audit(out, rows, AUDITED + [moved_url])
    logs: list[str] = []
    with st.read() as v:
        before = {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=100).rows}
    inode = (root / "corpus" / "arx-nc.md").stat().st_ino
    # dry run: nothing written
    assert audit.run_apply(root, out, apply=False, st=st, log=logs.append,
                           operator_decision="test: toy corpus") == 0
    with st.read() as v:
        assert {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=100).rows} == before
    assert audit.run_apply(root, out, apply=True, st=st, log=logs.append,
                           operator_decision="test: toy corpus") == 0
    with st.read() as v:
        restrictions, _ = store.pinned_policy(v)
        after = {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=100).rows}
        entries = v.get_entries(after)
        stats = corpus_stats.compute(v, restrictions)
        ledger = v.control_get(audit.CONTROL_DOC)
    assert set(after) == set(before) and set(entries) == set(before)   # nothing deleted
    for sid, lic, cls in [("arx-pointer", "arxiv-nonexclusive", "arxiv-nonexclusive"),
                          ("arx-cc", "cc-by", "open"), ("arx-nc", "cc-by-nc-nd", "nc-nd"),
                          ("ope-open", "unverified", "unverified"),
                          ("ope-evid", "publisher-oa", "publisher-oa")]:
        for rec in (after[sid], entries[sid]):
            assert rec["license"] == lic
            assert rec["license_evidence"] and rec["license_url"].startswith("http")
            assert rec["rights_verified_at"] == "2026-09-25"
        assert registry.use_class(after[sid], restrictions) == cls
        assert registry.is_collection_eligible(after[sid], restrictions)      # collect-all
        # provenance kept: raw/text claims and hashes untouched
        for key in ("raw_path", "sha256", "text_path", "status"):
            assert after[sid][key] == before[sid][key]
        # the cleaned file sits in its view, same bytes, and the claim names it
        assert after[sid]["corpus_path"] == registry.corpus_path_for(after[sid], restrictions)
        assert (root / after[sid]["corpus_path"]).read_text() == f"cleaned {sid}\n"
    assert not (root / "corpus" / "arx-nc.md").exists()
    assert (root / "collection" / "nc-nd" / "corpus" / "arx-nc.md").stat().st_ino == inode
    assert not (root / "corpus" / clean_corpus.RECLASSIFYING).exists()
    assert after["ope-arx"] == before["ope-arx"]                      # URL changed: skipped
    assert stats.excluded == 4
    assert ledger["baseline"]["documents"] == 9
    assert ledger["changesets"][0]["left_default_docs"] == 4
    assert ledger["changesets"][0]["operator_decision"] == "test: toy corpus"
    # idempotent: a second apply changes nothing
    logs.clear()
    assert audit.run_apply(root, out, apply=True, st=st, log=logs.append) == 0
    assert any("'applied': 5" in line for line in logs)
    with st.read() as v:
        assert {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=100).rows} == after
    # a result bound to other bytes than the row now holds is never applied
    reaudit = tmp_path / "audit2"
    write_audit(reaudit, rows, [changed_payload])
    logs.clear()
    audit.run_apply(root, reaudit, apply=False, st=st, log=logs.append)
    assert any("final': 0" in line or "stale" in line for line in logs)


class _Interrupted(Exception):
    pass


def _interrupt_after(n_chunks):
    """shard_batches that is interrupted once `n_chunks` batches have committed."""
    real = audit.store_broker.shard_batches

    def gen(todo, size):
        for i, chunk in enumerate(real(todo, 1), 1):
            yield chunk
            if i >= n_chunks:
                raise _Interrupted("killed between batches")
        raise _Interrupted("killed after every batch, before the default copies were dropped")
    return gen


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
@pytest.mark.parametrize("cut", [1, 99])        # after the first batch / after every batch
def test_interrupted_apply_is_finished_by_a_retry(factory, tmp_path, monkeypatch, cut):
    """Codex review ca1 P1-1 + P1-2: a retry removes the default copies an interrupted apply
    left behind, clears the marker only then, and the ledger counts every committed departure
    exactly once."""
    root = tmp_path / "repo"
    st = audit_store(factory, root)
    rows = rows_fixture()
    seed_rows(st, rows, root)
    out = tmp_path / "audit"
    write_audit(out, rows, AUDITED)
    monkeypatch.setattr(audit.store_broker, "shard_batches", _interrupt_after(cut))
    with pytest.raises(_Interrupted):
        audit.run_apply(root, out, apply=True, st=st, log=lambda *a: None,
                        operator_decision="test: toy corpus")
    assert (root / "corpus" / clean_corpus.RECLASSIFYING).exists()
    with st.read() as v:
        committed = v.control_get(audit.CONTROL_DOC)
        after = {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=100).rows}
    moved_so_far = [sid for sid, r in after.items()
                    if (r.get("corpus_path") or "").startswith("collection/")]
    assert sum(c["left_default_docs"] for c in committed["changesets"]) == len(moved_so_far)
    monkeypatch.undo()
    logs: list[str] = []
    assert audit.run_apply(root, out, apply=True, st=st, log=logs.append,
                           operator_decision="test: toy corpus") == 0
    with st.read() as v:
        ledger = v.control_get(audit.CONTROL_DOC)
        after = {r["id"]: r for r in v.scan(store.Table.MANIFEST, limit=100).rows}
    for sid in ("arx-pointer", "arx-nc", "ope-open", "ope-evid"):
        assert after[sid]["corpus_path"].startswith("collection/")
        assert not (root / "corpus" / f"{sid}.md").exists()          # no stranded default copy
        assert (root / after[sid]["corpus_path"]).read_text() == f"cleaned {sid}\n"
    assert not (root / "corpus" / clean_corpus.RECLASSIFYING).exists()
    assert sum(c["left_default_docs"] for c in ledger["changesets"]) == 4    # exactly once


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_apply_refuses_cumulative_default_view_loss_above_one_percent(factory, tmp_path):
    root = tmp_path / "repo"
    st = audit_store(factory, root)
    rows = rows_fixture()
    seed_rows(st, rows, root)
    out = tmp_path / "audit"
    write_audit(out, rows, AUDITED[:1])
    logs: list[str] = []
    assert audit.run_apply(root, out, apply=True, st=st, log=logs.append) == 1
    assert any("REFUSED" in line and "1%" in line for line in logs)
    with st.read() as v:
        assert v.get_manifest(["arx-pointer"])["arx-pointer"]["license"] == "open"


def test_apply_waits_for_every_target_unless_partial(tmp_path):
    out = tmp_path / "audit"
    rows = rows_fixture()
    write_audit(out, rows, AUDITED[:2])
    audit.append_jsonl(out / "targets.jsonl", [{**rows[2], "cohort": "arxiv"}])  # no result
    logs: list[str] = []
    assert audit.run_apply(tmp_path, out, apply=False, log=logs.append) == 1
    assert any("REFUSED" in line and "missing" in line for line in logs)


def test_result_states_bind_to_the_payload():
    t = {"id": "a", "sha256": "1" * 64}
    ok = {"id": "a", "verdict": "eligible", "payload_sha256": "1" * 64,
          "evidence_source": "arxiv-oai-pmh:arXivRaw", "resolver": 1}
    assert audit.result_state(t, ok) == "final"
    assert audit.result_state(t, None) == "missing"
    assert audit.result_state(t, {"id": "a", "verdict": None}) == "transient"
    assert audit.result_state(t, {**ok, "payload_sha256": "2" * 64}) == "stale"
    assert audit.result_state(t, {k: v for k, v in ok.items() if k != "payload_sha256"}) == \
        "stale"
    old_openalex = {**ok, "evidence_source": "openalex:works", "resolver": 1}
    assert audit.result_state(t, old_openalex) == "stale"          # resolver upgraded
    assert audit.result_state(t, {**ok, "stale": True}) == "stale"


def test_bind_attaches_payloads_and_revokes_stale_stamps(tmp_path):
    out, data = tmp_path / "audit", tmp_path / "data"
    (data / "text").mkdir(parents=True)
    (data / "text" / "arx-a.md").write_text("arXiv:1911.02206v2 [eess.SY]")
    (data / "text" / "arx-b.md").write_text("EDITED since the audit")
    import hashlib
    good = hashlib.sha256(b"arXiv:1911.02206v2 [eess.SY]").hexdigest()
    targets = [target("arx-a", sha256="1" * 64, text_sha256=good, snapshot={"commit": "c"}),
               target("arx-b", sha256="2" * 64, text_sha256=good, snapshot={"commit": "c"}),
               target("arx-c", sha256="3" * 64, snapshot={"commit": "c"})]
    write_targets(out, targets)
    legacy = [{"id": sid, "cohort": "arxiv", "url": t["url"], "verdict": "pointer-only",
               "licence": "arxiv-nonexclusive", "version_basis": basis,
               "evidence_source": "arxiv-oai-pmh:arXivRaw", "checked_at": "2026-09-25T09:00:00Z"}
              for sid, t, basis in (("arx-a", targets[0], "pdf-stamp"),
                                    ("arx-b", targets[1], "pdf-stamp"),
                                    ("arx-c", targets[2], "dates"))]
    audit.append_jsonl(out / "results.jsonl", legacy)
    assert set(audit.audit_state(targets, audit.latest_results(out / "results.jsonl"))
               .values()) == {"stale"}
    audit.run_bind(out, data_root=data, log=lambda *a: None)
    state = audit.audit_state(targets, audit.latest_results(out / "results.jsonl"))
    # arx-b's stamp no longer matches its text and no OAI response is saved: re-audit it
    assert state == {"arx-a": "final", "arx-b": "stale", "arx-c": "final"}
    got = audit.latest_results(out / "results.jsonl")
    assert got["arx-a"]["payload_sha256"] == "1" * 64 and got["arx-a"]["snapshot"] == {
        "commit": "c"}
    # with the saved OAI response the verdict is re-derived offline from the version dates
    (out / "oai").mkdir()
    (out / "oai" / "1911.02206.xml").write_text(fixture("arxiv_two_versions_nonexclusive.xml"))
    audit.run_bind(out, data_root=data, log=lambda *a: None)
    got = audit.latest_results(out / "results.jsonl")["arx-b"]
    assert audit.result_state(targets[1], got) == "final"
    assert got["version_basis"] == "dates" and "stamp_ignored" in got


def test_report_counts_states_transitions_and_examples(tmp_path):
    targets = [target(f"arx-{i}", corpus_chars=400, sha256=sha_of(f"arx-{i}"))
               for i in range(20)]
    results = {f"arx-{i}": result(f"arx-{i}", "u", v, lic)
               for i, (v, lic) in enumerate([("pointer-only", "arxiv-nonexclusive")] * 6
                                            + [("eligible", "cc-by")] * 4)}
    rep = audit.build_report(targets, results, {"corpus": {"documents": 1000, "tokens": 10_000}})
    c = rep["cohorts"]["arxiv"]
    assert rep["states"] == {"final": 10, "missing": 10}
    assert c["counts"]["pointer-only"] == 6 and c["estimated_population_docs"]["pointer-only"] == 12
    assert rep["class_transitions"] == {"open -> arxiv-nonexclusive": 6, "open -> open": 4}
    assert rep["impact"]["collection_delta"] == 0
    assert rep["impact"]["estimated_leaving_default_view_docs"] == 12
    assert rep["impact"]["estimated_leaving_default_view_tokens"] == 1200
    assert len(rep["examples"]["pointer-only"]) == 6 and rep["examples"]["unresolved"] == []


def test_pdd_is_public_domain_with_its_jurisdiction():
    for url in ("http://creativecommons.org/licenses/publicdomain/",
                "https://creativecommons.org/publicdomain/certification/1.0/us/"):
        verdict, tag, reason = audit.classify_licence_url(url)
        assert (verdict, tag) == ("eligible", "public-domain")
        assert "US" in reason and "not CC0" in reason


def test_openalex_matching_keeps_identity_parameters():
    plos = "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0224998" \
           "&type=printable"
    other = "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0000001" \
            "&type=printable"
    assert audit._loc_matches(plos, {"pdf_url": plos.replace("&type=printable", "")})
    assert not audit._loc_matches(plos, {"pdf_url": other})
    esc = "https://escholarship.org/content/qt1/qt1.pdf?t=abc"
    assert audit._loc_matches(esc, {"pdf_url": "https://escholarship.org/content/qt1/qt1.pdf"})
    # the location that decided an NC verdict is the one recorded, not the first match
    t = {**target("ope-x", url=plos), "cohort": "openalex"}
    work = {"id": "W9", "locations": [
        {"pdf_url": plos, "license": "cc-by", "license_id": "https://openalex.org/licenses/cc-by"},
        {"pdf_url": plos, "license": "cc-by-nc",
         "license_id": "https://openalex.org/licenses/cc-by-nc", "landing_page_url": "L"}]}
    res = audit.openalex_verdict(t, work, "2026-09-25T00:00:00Z", "E")
    assert (res["verdict"], res["licence"]) == ("excluded-nc-nd", "cc-by-nc")
    assert res["deciding_location"]["license"] == "cc-by-nc"
    assert res["license_url"] == "https://openalex.org/licenses/cc-by-nc"
    # a publisher grant on another location is a candidate only
    work2 = {"id": "W8", "locations": [
        {"pdf_url": plos, "license": None},
        {"pdf_url": None, "landing_page_url": "https://doi.org/10.1371/x", "license": "cc-by",
         "version": "publishedVersion"}]}
    res = audit.openalex_verdict(t, work2, "2026-09-25T00:00:00Z", "E")
    assert res["verdict"] == "unresolved" and res["publisher_candidate"]["license"] == "cc-by"


def test_openalex_conflicting_license_and_license_id_is_unverified():
    """Codex review ca1 P1-3: one location saying cc-by in `license` but cc-by-nc in
    `license_id` must never be certified open."""
    url = "https://repo.example/paper.pdf"
    t = {**target("ope-c", url=url), "cohort": "openalex"}
    work = {"id": "W7", "locations": [
        {"pdf_url": url, "license": "cc-by",
         "license_id": "https://openalex.org/licenses/cc-by-nc"}]}
    res = audit.openalex_verdict(t, work, "2026-09-25T00:00:00Z", "E")
    assert (res["verdict"], res["licence"]) == ("unresolved", "unverified")
    assert "disagree" in res["reason"]
    # agreeing statements still decide normally
    work["locations"][0]["license_id"] = "https://openalex.org/licenses/cc-by"
    res = audit.openalex_verdict(t, work, "2026-09-25T00:00:00Z", "E")
    assert (res["verdict"], res["licence"]) == ("eligible", "cc-by")
    # license_id alone is a statement too
    work["locations"][0].update(license=None,
                                license_id="https://openalex.org/licenses/cc-by-nd")
    res = audit.openalex_verdict(t, work, "2026-09-25T00:00:00Z", "E")
    assert res["verdict"] == "excluded-nc-nd"


@pytest.mark.parametrize("factory", STORES, ids=lambda f: f.__name__)
def test_pointer_transition_is_prepared_not_run(factory, tmp_path):
    import requests
    import robots_policy

    robots_policy.clear_memory()
    root = tmp_path / "repo"
    st = audit_store(factory, root)
    ptr = [{**pipeline_repo.entry_of(mrow("ashrae-a", url="https://ashrae.example/a.pdf")),
            "license": "proprietary-internal"},
           {**pipeline_repo.entry_of(mrow("vendor-page", url="https://v.example/standard")),
            "license": "proprietary-internal", "format": "html"},
           {**pipeline_repo.entry_of(mrow("ashrae-login", url="https://ashrae.example/b.pdf")),
            "license": "proprietary-internal"}]
    write(st, rid("seed-ptr"), lambda tx: tx.insert_entries(ptr))
    logs: list[str] = []
    assert audit.run_pointer_transition(root, probe=False, apply=False, st=st,
                                        log=logs.append) == 0
    assert any("2 unprobed candidates" in line for line in logs)

    def route(url, params):
        r = requests.Response()
        r.url, r.status_code = url, 200
        r._content, r._content_consumed = b"", True
        if url.endswith("robots.txt"):
            r._content = b"User-agent: *\nAllow: /\n"
        elif url.endswith("a.pdf"):
            r.status_code, r._content = 206, b"%PDF-1.4"
            r.headers["Content-Type"] = "application/pdf"
        else:
            r.status_code = 302
            r.headers["Location"] = "/login?next=b"
        return r

    http, _ = throttle(route)
    http.session.get = lambda url, params=None, timeout=None, **kw: route(url, params)
    logs.clear()
    assert audit.run_pointer_transition(root, probe=True, apply=True, st=st, http=http,
                                        log=logs.append) == 0
    with st.read() as v:
        got = v.get_entries(["ashrae-a", "vendor-page", "ashrae-login"])
    assert got["ashrae-a"]["license"] == "proprietary"
    assert "public PDF" in got["ashrae-a"]["license_evidence"]
    assert got["ashrae-login"]["license"] == "proprietary-internal"     # a login stays a pointer
    assert got["vendor-page"]["license"] == "proprietary-internal"


# --- the rights-evidence lint rule (off by default) --------------------------------------------------

def test_rights_evidence_lint_rule_is_opt_in(tmp_path, capsys):
    root = tmp_path / "repo"
    sha = {"sha256": "0" * 64}
    rows = [mrow("arx-a", source="arxiv", license="open", **sha),
            mrow("arx-b", source="arxiv", license="arxiv-nonexclusive",
                 license_evidence="arXiv OAI-PMH ...", rights_verified_at="2026-09-25",
                 license_url="http://arxiv.org/licenses/nonexclusive-distrib/1.0/", **sha),
            mrow("arxiv-curated", source="arxiv", license="open", **sha),
            mrow("hand-x", license="open", **sha)]
    pipeline_repo.write_repo(root, entries=[pipeline_repo.entry_of(r) for r in rows],
                             manifest=rows)
    assert lint_registry.main(root) == 0                       # off: the debt is tolerated
    capsys.readouterr()
    assert lint_registry.main(root, require_rights_evidence=True) == 1
    out = capsys.readouterr().out
    assert "arx-a: no license_evidence" in out and "arx-a: license 'open'" in out
    assert "arxiv-curated: no license_evidence" in out
    assert "arx-b" not in out and "hand-x" not in out


def test_restricted_use_licences_classify_never_filter():
    for lic in registry.RESTRICTED_USE_LICENSES:
        row = {"id": "arx-x", "license": lic}
        assert registry.is_collection_eligible(row, {})
        assert not registry.is_default_corpus_eligible(row, {})
        assert not store.evaluate(store.default_corpus_where({}), row)
        assert lic in lint_registry.LICENSES
    assert not registry.RESTRICTED_USE_LICENSES & registry.OPEN_USE_LICENSES
    assert registry.is_default_corpus_eligible({"id": "arx-x", "license": "cc-by"}, {})


def test_pre_fix_openalex_results_are_re_audited():
    """Codex review ca2: a result saved by the resolver before license/license_id conflicts were
    classified must not count as final — the OpenAlex pass re-audits it."""
    url = "https://repo.example/paper.pdf"
    t = {**target("ope-c", url=url), "cohort": "openalex"}
    work = {"id": "W7", "locations": [
        {"pdf_url": url, "license": "cc-by",
         "license_id": "https://openalex.org/licenses/cc-by-nc"}]}
    fresh = audit.openalex_verdict(t, work, "2026-09-25T00:00:00Z", "E")
    assert fresh["resolver"] == audit.RESOLVER["openalex:works"] == 3
    assert audit.result_state(t, fresh) == "final"
    stale = {**fresh, "resolver": 2, "verdict": "eligible", "licence": "cc-by"}
    assert audit.result_state(t, stale) != "final"
