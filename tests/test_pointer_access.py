"""Pointer probes use the loader's policy predicates before *any* request. No live HTTP."""
import json

import pytest
import requests

import audit_licence_evidence as audit
import build_corpus
import host_policy
import pipeline_repo
import registry
import robots_policy
import store
from test_licence_audit import Clock
from test_store_contract import entry, write


def pointer(sid="hand-pointer", url="https://allowed.example/a.pdf", **kw):
    return entry(sid, url=url, license="proprietary-internal", **kw)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    robots_policy.clear_memory()

    def no_network(*a, **kw):
        pytest.fail("unexpected live network request")
    monkeypatch.setattr(requests.sessions.Session, "request", no_network)

    def make(entries=None, rules=None, hosts=None, vendors=None):
        root = tmp_path / "repo"
        rules = {"proprietary_internal_pointers": pipeline_repo.pointer_rule(), **(rules or {})}
        pipeline_repo.write_repo(root, entries=entries or [pointer()], restrictions=rules,
                                 policy=hosts or {})
        (root / "registry/vendors.json").write_text(json.dumps({"vendors": vendors or {}}))
        return root, store.FileStore(root)
    return make


class Spy:
    def __init__(self, routes, clock):
        self.routes, self.clock, self.calls, self.headers = routes, clock, [], {}

    def get(self, url, **kw):
        self.calls.append((url, self.clock(), kw))
        assert kw.get("allow_redirects") is False
        assert kw.get("stream") is True
        assert url in self.routes, f"forbidden/unexpected request: {url}"
        status, headers, body = self.routes[url]
        r = requests.Response()
        r.url, r.status_code, r.headers = url, status, requests.structures.CaseInsensitiveDict(headers)
        r._content, r._content_consumed = body, True
        return r


def run(root, st, routes, *, apply=True):
    clock = Clock()
    spy = Spy(routes, clock)
    http = audit.Throttle(3.1, session=spy, sleep=clock.sleep, clock=clock)
    logs = []
    assert audit.run_pointer_transition(root, probe=True, apply=apply, st=st, http=http,
                                        log=logs.append) == 0
    with st.read() as view:
        rows = view.scan(store.Table.ENTRIES).rows
    return rows, spy.calls, logs


PDF = (206, {"Content-Type": "application/pdf"}, b"%PDF-1.4 example")
ROBOTS = (200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 7\n")


@pytest.mark.parametrize("match", [{"source": "test"}, {"id_prefix": "hand-"},
                                   {"license": "proprietary-internal"},
                                   {"license": "proprietary"}])
def test_independent_denials_never_probe_or_apply(setup, match):
    rules = {"independent": pipeline_repo.restriction(match, collection="deny")}
    root, st = setup(rules=rules)
    rows, calls, logs = run(root, st, {})
    assert not calls
    assert rows[0]["license"] == "proprietary-internal"
    assert any("collection" in line for line in logs)


def test_suspended_host_makes_zero_requests_including_robots(setup, monkeypatch):
    url = "https://sub.blocked.example.:443/a.pdf"
    root, st = setup([pointer(url=url)], hosts={"blocked.example": {}})
    with st.read() as view:
        _, policy = store.pinned_policy(view)
    # Same fixture/verdict as the loader's transport gate, including canonical host spelling.
    monkeypatch.setattr(build_corpus, "HOST_POLICY", policy)
    with pytest.raises(build_corpus.HostSuspended):
        build_corpus.check_hop(url)
    assert host_policy.suspended(url, policy)
    rows, calls, _ = run(root, st, {})
    assert not calls and rows[0]["license"] == "proprietary-internal"


@pytest.mark.parametrize("robots_redirect", [False, True])
def test_redirect_to_suspended_host_is_never_requested(setup, robots_redirect):
    root, st = setup(hosts={"blocked.example": {}})
    redirect = (302, {"Location": "https://blocked.example/secret.pdf"}, b"")
    routes = {"https://allowed.example/robots.txt": redirect if robots_redirect else ROBOTS}
    if not robots_redirect:
        routes["https://allowed.example/a.pdf"] = redirect
    rows, calls, _ = run(root, st, routes)
    assert [c[0] for c in calls] == list(routes)
    assert all("blocked.example" not in c[0] for c in calls)
    assert rows[0]["license"] == "proprietary-internal"


def test_robots_exclusion_blocks_content(setup):
    root, st = setup()
    rows, calls, _ = run(root, st, {"https://allowed.example/robots.txt":
                                  (200, {}, b"User-agent: *\nDisallow: /\n")})
    assert [c[0] for c in calls] == ["https://allowed.example/robots.txt"]
    assert rows[0]["license"] == "proprietary-internal"


@pytest.mark.parametrize("redirect", [False, True])
def test_vendor_exception_is_scoped_to_configured_vendor_host(setup, redirect):
    root, st = setup([pointer(source="vendor_test")], vendors={"test": {
        "source": "vendor_test", "hosts": ["allowed.example"], "crawl_delay": 9}})
    routes = {"https://allowed.example/robots.txt":
              (200, {}, b"User-agent: *\nDisallow: /\nCrawl-delay: 8\n"),
              "https://allowed.example/a.pdf": PDF}
    if redirect:
        routes["https://allowed.example/a.pdf"] = (302, {"Location": "https://other.example/a.pdf"}, b"")
        routes["https://other.example/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /\n")
    rows, calls, _ = run(root, st, routes)
    assert rows[0]["license"] == ("proprietary-internal" if redirect else "proprietary")
    assert calls[1][1] - calls[0][1] >= 9


def test_permitted_pdfs_share_pacing_across_candidates(setup):
    root, st = setup([pointer(), pointer("hand-second", "https://allowed.example/b.pdf")])
    rows, calls, _ = run(root, st, {"https://allowed.example/robots.txt": ROBOTS,
                                  "https://allowed.example/a.pdf": PDF,
                                  "https://allowed.example/b.pdf": PDF})
    assert all(r["license"] == "proprietary" for r in rows)
    assert len(calls) == 3
    assert all(b[1] - a[1] >= 7 for a, b in zip(calls, calls[1:]))


@pytest.mark.parametrize("change", ["url", "source", "restriction", "host"])
def test_apply_rechecks_fresh_identity_and_policy(setup, monkeypatch, change):
    root, st = setup()
    original = audit.store_broker.run_batch
    applied = []

    def changed(st, step, body, **kw):
        applied.append(True)
        if change in ("url", "source"):
            p = pointer(**({"url": "https://elsewhere.example/a.pdf"} if change == "url"
                           else {"source": "changed"}))
            write(st, "change-entry", lambda tx: tx.upsert_entries([p]))
        elif change == "restriction":
            pipeline_repo.pin_policy(root, policy={}, restrictions={"hold":
                pipeline_repo.restriction({"source": "test"}, collection="deny")})
        else:
            pipeline_repo.write_policy(root, {"allowed.example": {}})
        return original(st, step, body, **kw)
    monkeypatch.setattr(audit.store_broker, "run_batch", changed)
    rows, _, _ = run(root, st, {"https://allowed.example/robots.txt": ROBOTS,
                               "https://allowed.example/a.pdf": PDF})
    assert rows[0]["license"] == "proprietary-internal"
    assert applied == [True]


def test_collection_gate_matches_loader_for_each_independent_rule(setup):
    rules = {"hold": pipeline_repo.restriction({"source": "test"}, collection="deny")}
    assert not registry.is_collection_eligible(pointer(), rules)
    root, st = setup(rules=rules)
    rows, calls, _ = run(root, st, {})
    assert not calls and rows[0]["license"] == "proprietary-internal"


def test_altered_pointer_hold_is_not_exempted(setup):
    root, st = setup(rules={"proprietary_internal_pointers": pipeline_repo.restriction(
        {"license": "proprietary-internal", "source": "test"}, collection="deny")})
    rows, calls, _ = run(root, st, {})
    assert not calls and rows[0]["license"] == "proprietary-internal"


def test_default_view_hold_does_not_block_collection(setup):
    root, st = setup(rules={"use-only": pipeline_repo.restriction(
        {"source": "test"}, collection="allow", default_corpus="deny")})
    rows, _, _ = run(root, st, {"https://allowed.example/robots.txt": ROBOTS,
                               "https://allowed.example/a.pdf": PDF})
    assert rows[0]["license"] == "proprietary"
    with st.read() as view:
        restrictions, _ = store.pinned_policy(view)
    assert not registry.is_default_corpus_eligible(rows[0], restrictions)


@pytest.mark.parametrize("response", [(200, {"Content-Type": "application/pdf"}, b"<html>login"),
                                     (403, {}, b"challenge"), (429, {}, b"slow down")])
def test_refusal_or_mislabeled_html_never_reclassifies(setup, response):
    root, st = setup()
    rows, calls, _ = run(root, st, {"https://allowed.example/robots.txt": ROBOTS,
                                  "https://allowed.example/a.pdf": response})
    assert len(calls) == 2 and rows[0]["license"] == "proprietary-internal"


def test_host_caps_include_robots_and_redirects(setup, monkeypatch):
    monkeypatch.setattr(build_corpus, "HOST_RUN_CAP", {"allowed.example": 1})
    root, st = setup()
    rows, calls, _ = run(root, st, {"https://allowed.example/robots.txt": ROBOTS})
    assert len(calls) == 1 and rows[0]["license"] == "proprietary-internal"


def test_redirect_limit(setup):
    root, st = setup()
    redirect = (302, {"Location": "/a.pdf"}, b"")
    rows, calls, logs = run(root, st, {"https://allowed.example/robots.txt": ROBOTS,
                                     "https://allowed.example/a.pdf": redirect})
    assert len(calls) == robots_policy.MAX_REDIRECTS + 2
    assert rows[0]["license"] == "proprietary-internal"
    assert any("too many redirects" in line for line in logs)


def test_permitted_relative_redirect(setup):
    root, st = setup()
    rows, calls, _ = run(root, st, {"https://allowed.example/robots.txt": ROBOTS,
        "https://allowed.example/a.pdf": (302, {"Location": "/final.pdf"}, b""),
        "https://allowed.example/final.pdf": PDF})
    assert len(calls) == 3 and rows[0]["license"] == "proprietary"
    assert "/final.pdf" in rows[0]["license_evidence"]


def test_default_client_is_constructed_once_for_all_candidates(setup, monkeypatch):
    root, st = setup([pointer(), pointer("hand-second", "https://allowed.example/b.pdf")])
    clock = Clock()
    spy = Spy({"https://allowed.example/robots.txt": ROBOTS,
               "https://allowed.example/a.pdf": PDF,
               "https://allowed.example/b.pdf": PDF}, clock)
    original, created = audit.Throttle, []

    def make(interval):
        created.append(interval)
        return original(interval, session=spy, sleep=clock.sleep, clock=clock)
    monkeypatch.setattr(audit, "Throttle", make)
    assert audit.run_pointer_transition(root, probe=True, apply=False, st=st,
                                        log=lambda s: None) == 0
    assert len(created) == 1 and len(spy.calls) == 3
    assert spy.calls[2][1] - spy.calls[1][1] >= 7
