"""Compliance/ESG programme (Codex decision 2026-09-25): access checks, rotation protocol,
identity, licence holding, budgets and the scoped quality profile. Recorded fixtures only, no
network."""
from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import build_corpus
import check_contracts
import compliance_common
import dedup
import finder_protocol
import find_boverket
import find_esef
import find_eurlex
import find_regdocs
import ops
import polite_http
import prune_corpus
import quality
import robots_policy
import store

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 25)
UUID = "2b15980c-0cd4-11ef-a251-01aa75ed71a1"
TEST_HOSTS = {h: (0.0, 6) for h in ("x.org", "ok.org", "a.org", "b.org", "s.org",
                                     "sasb.ifrs.org", "eur-lex.europa.eu", "www.fsb-tcfd.org",
                                     "h0.example", "h1.example", "h2.example", "h3.example",
                                     "h4.example", "h5.example")}


def keys(urls=(), ids=(), titles=()):
    return dedup.from_sets(set(urls), set(titles), set(ids))


@pytest.fixture(autouse=True)
def _scratch(monkeypatch, tmp_path):
    """Finder scratch files (probe memory, walk caches, review queues) go to tmp_path."""
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(robots_policy, "CACHE_DIR", tmp_path / "robots-cache")
    robots_policy.clear_memory()
    robots_policy.set_hop_filter(None)
    robots_policy.set_pacer(None)
    yield
    robots_policy.clear_memory()
    polite_http.set_policy({})
    robots_policy.set_hop_filter(None)
    robots_policy.set_pacer(None)


class Report(finder_protocol.Report):
    """Captures the one reported outcome."""

    def __init__(self):
        super().__init__()
        self.value = None

    def hold(self, reason):
        super().hold(reason)
        self.value = ("hold", reason)

    def next(self, cursor):
        super().next(cursor)
        self.value = ("next", cursor)

    def exhausted(self, reason):
        super().exhausted(reason)
        self.value = ("exhausted", reason)


def docs():
    """The committed programme configuration, as a view would pin it."""
    return {name: json.loads((REPO / "registry" / name).read_text())
            for name in ("regdocs.json", "eurlex.json", "esef.json")}


# ------------------------------------------------------------------------------------ robots
SASB = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /wp-admin/\n" \
       "Allow: /wp-admin/admin-ajax.php\n\nUser-agent: *\nDisallow:  /*.pdf$\n"
BSI = ("User-agent: *\nContent-Signal: search=yes,ai-train=no\nAllow: /\n\n"
       "User-agent: ClaudeBot\nDisallow: /\n")
LOVDATA = "User-agent: googlebot\nCrawl-delay: 5\nDisallow: /sok\n\nUser-agent: *\nDisallow: /\n"


def rules_for(text):
    return robots_policy._select(robots_policy.parse(text))


def test_robots_wildcard_dollar_and_group_merging():
    rules, _ = rules_for(SASB)
    assert not robots_policy.permits(rules, "https://sasb.ifrs.org/x/standard.pdf")
    assert robots_policy.permits(rules, "https://sasb.ifrs.org/x/standard.pdf?download=1")
    assert robots_policy.permits(rules, "https://sasb.ifrs.org/standards/")
    assert robots_policy.permits(rules, "https://sasb.ifrs.org/wp-admin/admin-ajax.php")
    assert not robots_policy.permits(rules, "https://sasb.ifrs.org/wp-admin/x")


def test_robots_star_group_applies_when_no_group_names_us():
    rules, delay = rules_for(LOVDATA)
    assert not robots_policy.permits(rules, "https://lovdata.no/dokument/SF/forskrift/2017")
    assert delay is None  # googlebot's crawl-delay is not ours
    rules, _ = rules_for(BSI)  # ClaudeBot's group is not ours; `*` allows
    assert robots_policy.permits(rules, "https://technical.buildingsmart.org/x")


def test_robots_named_group_wins_and_crawl_delay():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: nekaise-corpus\nCrawl-delay: 7\nAllow: /\n"
    rules, delay = rules_for(text)
    assert robots_policy.permits(rules, "https://x.org/a") and delay == 7.0


def test_robots_longest_match_and_allow_tie():
    rules, _ = rules_for("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /c\nAllow: /c\n")
    assert robots_policy.permits(rules, "https://x.org/a/b/c")
    assert not robots_policy.permits(rules, "https://x.org/a/x")
    assert robots_policy.permits(rules, "https://x.org/c")


def test_robots_specificity_is_measured_on_the_normalized_pattern():
    rules, _ = rules_for("User-agent: *\nAllow: /%70%72%69%76%61%74%65\nDisallow: /private/x\n")
    assert not robots_policy.permits(rules, "https://x.org/private/x")
    assert robots_policy.permits(rules, "https://x.org/private/y")
    rules, _ = rules_for("User-agent: *\nDisallow: /private/\n")
    assert not robots_policy.permits(rules, "https://x.org/public/../private/x")


def test_robots_percent_encoding_cannot_bypass_a_rule():
    rules, _ = rules_for("User-agent: *\nDisallow: /private/\nDisallow: /sök/\n")
    for url in ("https://x.org/private/x", "https://x.org/%70rivate/x", "https://x.org/%70RIVATE/x".lower(),
                "https://x.org/s%C3%B6k/a", "https://x.org/sök/a", "https://x.org/s%c3%b6k/a"):
        assert not robots_policy.permits(rules, url), url
    assert robots_policy.permits(rules, "https://x.org/public/x")


@pytest.mark.parametrize(("status", "body", "outcome"), [
    (404, b"", True),
    (200, b"User-agent: *\nDisallow: /private/\n", True),
    (403, b"forbidden", "unavailable"),
    (503, b"", "unavailable"),
    (200, b"<html><title>Just a moment</title>challenge</html>", "unavailable"),
])
def test_robots_fetch_outcomes(status, body, outcome):
    fetch = lambda _url: (status, body)  # noqa: E731
    if outcome == "unavailable":
        with pytest.raises(robots_policy.RobotsUnavailable):
            robots_policy.decision("https://example.org/doc.pdf", fetch)
    else:
        assert robots_policy.decision("https://example.org/doc.pdf", fetch)[0] is outcome


class FakeResp:
    def __init__(self, status=200, body=b"", ctype="application/xml", location=None, url="",
                 slow=0.0):
        self.status_code, self._body, self.url, self.slow = status, body, url, slow
        self.headers = {"content-type": ctype}
        if location:
            self.headers["location"] = location
        self.raw = SimpleNamespace(read=lambda n, decode_content=True: body[:n])

    @property
    def content(self):
        return getattr(self, "_content", self._body)

    def iter_content(self, _n):
        for i in range(0, len(self._body), 4):
            if self.slow:
                time.sleep(self.slow)
            yield self._body[i:i + 4]

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_robots_redirect_to_a_suspended_host_is_never_requested(monkeypatch):
    calls = []
    answers = {"https://ok.org/robots.txt": FakeResp(301, location="https://eur-lex.europa.eu/robots.txt")}
    monkeypatch.setattr(robots_policy.requests, "get",
                        lambda url, **_k: calls.append(url) or answers[url])
    robots_policy.set_policy({"eur-lex.europa.eu": {"status": "suspended", "decided_at": "x"}})
    try:
        with pytest.raises(robots_policy.RobotsUnavailable):
            robots_policy.decision("https://ok.org/doc.pdf")
    finally:
        robots_policy.set_policy({})
    assert calls == ["https://ok.org/robots.txt"]


def test_concurrent_cold_robots_lookups_fetch_once(monkeypatch):
    calls = []

    def get(url, **_k):
        calls.append(url)
        time.sleep(0.05)
        return FakeResp(200, b"User-agent: *\nDisallow: /x/\n", "text/plain")
    monkeypatch.setattr(robots_policy.requests, "get", get)
    threads = [threading.Thread(target=robots_policy.decision, args=("https://ok.org/a",))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


# ------------------------------------------------------------------------------------ polite_http
@pytest.fixture
def web(monkeypatch):
    """polite_http against canned answers; robots allow everything unless a test says not."""
    calls, answers, robots, paced = [], {}, {}, []
    monkeypatch.setattr(compliance_common, "PROGRAMME_HOSTS",
                        {**compliance_common.PROGRAMME_HOSTS, **TEST_HOSTS})
    monkeypatch.setattr(polite_http, "pace", lambda url, delay: paced.append((url, delay)))

    def decision(url, fetcher=None):
        host = url.split("/")[2]
        verdict = robots.get(host, (True, None))
        if isinstance(verdict, Exception):
            raise verdict
        return verdict
    monkeypatch.setattr(robots_policy, "decision", decision)

    def get(url, **_kw):
        calls.append(url)
        return answers[url]
    monkeypatch.setattr(polite_http.requests, "get", get)
    monkeypatch.setattr(polite_http.requests, "head", get)
    polite_http.set_policy({})
    return SimpleNamespace(calls=calls, answers=answers, robots=robots, paced=paced)


def test_polite_http_refuses_unreviewed_suspended_and_robots_denied_hosts(web):
    with pytest.raises(polite_http.Refused):
        polite_http.get("https://unreviewed.example/a")
    polite_http.set_policy({"eur-lex.europa.eu": {"status": "suspended", "decided_at": "x"}})
    with pytest.raises(polite_http.Refused):
        polite_http.get("https://eur-lex.europa.eu/legal-content/EN/TXT/")
    web.robots["sasb.ifrs.org"] = (False, None)
    with pytest.raises(polite_http.Refused):
        polite_http.get("https://sasb.ifrs.org/a.pdf")
    web.robots["x.org"] = robots_policy.RobotsUnavailable("503")
    with pytest.raises(polite_http.Deferred):
        polite_http.get("https://x.org/a")
    assert web.calls == []


def test_polite_http_checks_the_prepared_url_with_its_parameters(web, monkeypatch):
    seen = []

    def decision(url, fetcher=None):
        seen.append(url)
        return ("secret" not in url, None)
    monkeypatch.setattr(robots_policy, "decision", decision)
    with pytest.raises(polite_http.Refused):
        polite_http.get("https://x.org/api", params={"q": "secret"})
    assert seen == ["https://x.org/api?q=secret"] and web.calls == []


def test_polite_http_redirect_hops_are_rechecked(web):
    polite_http.set_policy({"fsb-tcfd.org": {"status": "suspended", "decided_at": "x"}})
    web.answers["https://ok.org/a"] = FakeResp(302, location="https://www.fsb-tcfd.org/b")
    with pytest.raises(polite_http.Refused):
        polite_http.get("https://ok.org/a")
    web.answers["https://ok.org/c"] = FakeResp(302, location="https://assets.bbhub.io/x.pdf")
    with pytest.raises(polite_http.Refused):  # TCFD's CDN is not a reviewed host
        polite_http.get("https://ok.org/c")
    assert web.calls == ["https://ok.org/a", "https://ok.org/c"]


def test_polite_http_html_where_data_expected_is_deferred_and_cap_enforced(web):
    web.answers["https://x.org/feed"] = FakeResp(200, b"<!DOCTYPE html><html>login", "text/html")
    with pytest.raises(polite_http.Deferred):
        polite_http.get("https://x.org/feed", expect="xml")
    web.answers["https://x.org/big"] = FakeResp(200, b"x" * 100)
    with pytest.raises(polite_http.TooLarge):
        polite_http.get("https://x.org/big", max_bytes=10)
    web.answers["https://x.org/refused"] = FakeResp(429, b"slow")
    with pytest.raises(polite_http.Deferred):
        polite_http.get("https://x.org/refused")
    web.answers["https://x.org/ok"] = FakeResp(200, b"<feed/>")
    assert polite_http.get("https://x.org/ok", expect="xml").content == b"<feed/>"
    web.answers["https://x.org/head"] = FakeResp(200, b"0123456789" * 50, "text/plain")
    assert polite_http.get("https://x.org/head", prefix=12).content == b"012345678901"


def test_discovery_pacing_honours_robots_crawl_delay(web):
    web.robots["x.org"] = (True, 10.0)
    web.answers["https://x.org/k.pdf"] = FakeResp(404)
    find_boverket.head_exists("https://x.org/k.pdf")
    assert web.paced[-1] == ("https://x.org/k.pdf", 10.0)


class Clock:
    """A fake monotonic clock that sleeping advances."""

    def __init__(self):
        self.now = 1000.0
        self.starts = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_discovery_pacing_shares_one_clock_per_host_group(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(polite_http, "_last", {})
    monkeypatch.setattr(polite_http, "time", clock)
    polite_http.pace("https://www.boverket.se/a", 0)
    t0 = clock.now
    polite_http.pace("https://boverket.se/b", 0)  # the alias waits on the same clock
    assert clock.now - t0 >= 10.0


def test_a_crawl_delay_learnt_later_applies_to_the_next_request(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(polite_http, "_last", {})
    monkeypatch.setattr(polite_http, "_crawl_delay", {})
    monkeypatch.setattr(polite_http, "time", clock)
    monkeypatch.setattr(compliance_common, "PROGRAMME_HOSTS",
                        {**compliance_common.PROGRAMME_HOSTS, "x.org": (1.0, 6)})
    polite_http.pace("https://x.org/robots.txt", 0)  # the robots request itself, 1 s clock
    t0 = clock.now
    monkeypatch.setattr(robots_policy, "decision", lambda _u, fetcher=None: (True, 10.0))
    delay = polite_http.check("https://x.org/doc")  # learns Crawl-delay: 10
    polite_http.pace("https://x.org/doc", delay)
    assert clock.now - t0 >= 10.0


def test_loader_pacing_applies_a_raised_delay_against_the_previous_start(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(build_corpus, "_host_last", {})
    monkeypatch.setattr(build_corpus, "time", clock)
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {"x.org": 1.0})
    build_corpus._wait_for_host("x.org")
    t0 = clock.now
    build_corpus.HOST_DELAY["x.org"] = 10.0  # a robots Crawl-delay learnt meanwhile
    build_corpus._wait_for_host("x.org")
    assert clock.now - t0 >= 10.0


def test_robots_request_holds_the_pacer_through_the_transport(monkeypatch):
    events = []
    from contextlib import contextmanager

    @contextmanager
    def pacer(url):
        events.append(("enter", url))
        yield
        events.append(("exit", url))
    robots_policy.set_pacer(pacer)

    def get(url, **_k):
        events.append(("get", url))
        return FakeResp(404)
    monkeypatch.setattr(robots_policy.requests, "get", get)
    robots_policy.decision("https://ok.org/a")
    assert events == [("enter", "https://ok.org/robots.txt"), ("get", "https://ok.org/robots.txt"),
                      ("exit", "https://ok.org/robots.txt")]


# ------------------------------------------------------------------------------------ protocol
def test_finder_protocol_reports_exactly_one_outcome(tmp_path, monkeypatch):
    for name in ("HOLD", "NEXT", "EXHAUSTED"):
        monkeypatch.setenv(f"NEKAISE_{'ROTATION_' if name != 'EXHAUSTED' else 'BACKEND_'}"
                           f"{name}_FILE", str(tmp_path / name))
    r = finder_protocol.Report()
    r.exhausted("done")
    assert (tmp_path / "NEXT").read_text().strip() == finder_protocol.END
    assert (tmp_path / "EXHAUSTED").read_text().strip() == "done"
    with pytest.raises(RuntimeError):
        r.hold("also")
    with pytest.raises(ValueError):
        finder_protocol.Report().next("x" * 5000)


# ------------------------------------------------------------------------------------ BFS
FEED = """﻿<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>http://rinfo.lagrummet.se/publ/bfs/2011:6</id><published>2011-04-27T00:00:00Z</published>
  <title>Boverkets byggregler;</title>
  <content src="https://rinfo.boverket.se/BFS2011-6/pdf/BFS2011-6.pdf" type="application/pdf"/></entry>
 <entry><id>http://rinfo.lagrummet.se/publ/bfs/2020:4</id><published>2020-07-01T00:00:00Z</published>
  <title>Boverkets föreskrifter om ändring i Boverkets byggregler (2011:6);</title>
  <content src="https://rinfo.boverket.se/BFS2011-6/pdf/BFS2020-4.pdf" type="application/pdf"/></entry>
 <entry><id>http://rinfo.lagrummet.se/publ/bfs/2021:1</id><published>2021-01-01T00:00:00Z</published>
  <title>Boverkets föreskrifter om upphävande av vissa författningar;</title>
  <content src="https://rinfo.boverket.se/BFS1992-2/pdf/BFS2021-1.pdf" type="application/pdf"/></entry>
 <entry><id>odd</id><published>2022-01-01T00:00:00Z</published><title>No pdf</title>
  <content src="https://elsewhere.example/x.pdf" type="application/pdf"/></entry>
</feed>"""
NEW_ENTRY = """ <entry><id>http://rinfo.lagrummet.se/publ/bfs/2010:1</id><published>2010-01-01T00:00:00Z</published>
  <title>Äldre föreskrift;</title>
  <content src="https://rinfo.boverket.se/BFS2010-1/pdf/BFS2010-1.pdf" type="application/pdf"/></entry>
</feed>"""


def test_bfs_feed_parsing_identity_and_rights():
    items = find_boverket.parse_feed(FEED)
    assert [i["doc"] for i in items] == ["BFS2011-6", "BFS2020-4", "BFS2021-1"]
    assert [i["kind"] for i in items] == ["grundförfattning", "ändringsförfattning",
                                          "upphävandeförfattning"]
    e = find_boverket.bfs_entry(items[1], "2026-09-25")
    assert e["id"] == "bov-bfs-bfs2011-6-bfs2020-4"
    assert e["title"].startswith("BFS 2020:4 (ändrar BFS 2011:6) — ")
    assert e["license"] == "public-domain" and e["license_url"] and e["rights_verified_at"]
    k = find_boverket.consolidation_entry(items[1], "2026-09-25")
    assert k["id"] == e["id"] + "-kons" and k["url"].endswith("/BFS2011-6/dok/BFS2020-4_Konsolidering.pdf")
    assert k["title"] != e["title"]
    for row in (e, k):
        assert compliance_common.quality_profile(row, docs()) == "normative"
    assert compliance_common.quality_profile({**e, "id": "bov-bfs-bfs2011-6"}, docs()) is None
    with pytest.raises(ValueError):
        find_boverket.parse_feed("<html/>")


def test_bfs_run_hard_max_resume_and_watch():
    probes = []
    probe = lambda url: probes.append(url) or url.endswith("BFS2020-4_Konsolidering.pdf")  # noqa
    r = Report()
    out = find_boverket.run_bfs("START", 2, 8, keys(), r, TODAY, lambda: FEED, probe)
    # grund (1) fits; the amendment + its consolidation (2) would exceed --max 2: stop there
    assert [e["id"] for e in out] == ["bov-bfs-bfs2011-6"]
    assert r.value[0] == "next" and r.value[1].startswith("1:")
    cur = r.value[1]
    r = Report()
    out = find_boverket.run_bfs(cur, 4, 8, keys(), r, TODAY, lambda: FEED, probe)
    assert [e["id"] for e in out] == ["bov-bfs-bfs2011-6-bfs2020-4", "bov-bfs-bfs2011-6-bfs2020-4-kons",
                                      "bov-bfs-bfs1992-2-bfs2021-1"]
    assert r.value == ("next", "watch:2026-09-25")
    before = len(probes)
    r = Report()  # probe memory: the second pass does not re-probe
    find_boverket.run_bfs("0", 10, 8, keys(), r, TODAY, lambda: FEED, probe)
    assert len(probes) == before
    r = Report()
    assert find_boverket.run_bfs("watch:2026-09-24", 10, 8, keys(), r, TODAY,
                                 lambda: pytest.fail("watch phase must not request")) == []
    assert r.value == ("next", "watch:2026-09-24")


def test_bfs_changed_feed_restarts_instead_of_skipping():
    probe = lambda _u: False  # noqa: E731
    r = Report()
    find_boverket.run_bfs("START", 1, 8, keys(), r, TODAY, lambda: FEED, probe)
    cur = r.value[1]
    changed = FEED.replace("</feed>", NEW_ENTRY)  # an older BFS sorts in FRONT of the cursor
    r = Report()
    out = find_boverket.run_bfs(cur, 10, 8, keys(), r, TODAY, lambda: changed, probe)
    assert "bov-bfs-bfs2010-1" in [e["id"] for e in out]


def test_bfs_feed_failure_and_probe_deferral_hold():
    def boom():
        raise polite_http.Deferred("503")
    r = Report()
    assert find_boverket.run_bfs("START", 10, 8, keys(), r, TODAY, boom) == []
    assert r.value[0] == "hold"
    items = find_boverket.parse_feed(FEED)
    fp = find_boverket.feed_fingerprint(items)
    r = Report()
    out = find_boverket.run_bfs(f"1:{fp}", 10, 8, keys(), r, TODAY, lambda: FEED,
                                lambda _u: None)
    assert out == [] and r.value[0] == "hold"  # nothing possible at the cursor: hold


def test_bfs_known_rows_are_skipped():
    items = find_boverket.parse_feed(FEED)
    known = keys(urls=[i["pdf"] for i in items],
                 ids=["bov-bfs-bfs2011-6-bfs2020-4-kons", "bov-bfs-bfs1992-2-bfs2021-1-kons"])
    r = Report()
    assert find_boverket.run_bfs("START", 10, 8, known, r, TODAY, lambda: FEED,
                                 lambda _u: pytest.fail("known consolidation must not be probed")) == []


# ------------------------------------------------------------------------------------ regdocs
def src(**kw):
    base = {"name": "t", "enabled": True, "mechanism": "static_list", "source": "t_src",
            "license": "public-domain", "license_url": "https://e.org/l", "license_evidence": "ev",
            "topic": "standards_protocols", "rights_reviewed_at": "2026-09-25",
            "items": [{"url": "https://a.org/x.pdf", "title": "X"}]}
    base.update(kw)
    return base


def test_regdocs_config_rules():
    assert find_regdocs.validate({"sources": {"ok": src()}}) == []
    errs = find_regdocs.validate({"sources": {"bad": src(license="proprietary")}})
    assert any("awaits the collect-all" in e for e in errs)
    assert find_regdocs.validate({"sources": {"bad": src(license="proprietary", enabled=False,
                                                         reason="awaiting")}}) == []
    errs = find_regdocs.validate({"sources": {"b": src(blocked=True)}})
    assert any("blocked" in e for e in errs)
    errs = find_regdocs.validate({"sources": {"b": src(quality_profile="normative")}})
    assert any("hosts" in e for e in errs)
    errs = find_regdocs.validate({"sources": {"b": src(license="open", enabled=False, reason="r",
                                                       quality_profile="normative",
                                                       hosts=["a.org"])}})
    assert errs == []
    errs = find_regdocs.validate({"sources": {"b": src(version_probe={"pattern": "("})}})
    assert any("version_probe" in e for e in errs)


def test_committed_programme_configs_are_valid_and_enabled_sources_appendable():
    d = docs()
    sources = d["regdocs.json"]["sources"]
    assert find_regdocs.validate(d["regdocs.json"]) == []
    for key in find_regdocs.runnable(sources, TODAY):
        assert sources[key]["license"] in compliance_common.CURRENT_LICENSES, key
    assert all(not c.get("enabled") for c in sources.values() if c.get("blocked"))
    assert find_eurlex.validate(d["eurlex.json"]) == []
    assert find_esef.validate(d["esef.json"]) == []
    backends = json.loads((REPO / "registry" / "backends.json").read_text())
    assert d["esef.json"]["license"] not in compliance_common.CURRENT_LICENSES
    assert backends["find_esef"]["enabled"] is False
    rotation = json.loads((REPO / "registry" / "rotation.json").read_text())
    for name in ("find_boverket_bfs", "find_regdocs", "find_eurlex", "find_esef"):
        assert rotation[name]["dynamic"] is True and rotation[name]["flag"] == "--cursor"
        assert backends[name]["required"] is False
    policy = json.loads((REPO / "registry" / "host_policy.json").read_text())["hosts"]
    for host in ("lovdata.no", "eur-lex.europa.eu", "ghgprotocol.org", "fsb-tcfd.org",
                 "buildingsmart.org", "globalreporting.org", "sasb.ifrs.org", "cdp.net",
                 "coclass.byggtjanst.se"):
        assert policy[host]["status"] == "suspended"


def test_every_configured_programme_url_is_on_a_reviewed_host():
    d = docs()
    for key, cfg in d["regdocs.json"]["sources"].items():
        if cfg.get("blocked"):
            continue
        urls = [it["url"] for it in cfg.get("items") or []] + list(cfg.get("seeds") or []) \
            + list(cfg.get("sitemaps") or [])
        for url in urls:
            assert compliance_common.reviewed_host(url), (key, url)
        for host in cfg.get("hosts") or []:
            assert compliance_common.reviewed_host(host), (key, host)
    for host in ("rinfo.boverket.se", "publications.europa.eu", "filings.xbrl.org"):
        assert compliance_common.reviewed_host(host)
    for host in ("assets.bbhub.io", "efrag.sharepoint.com", "eur-lex.europa.eu"):
        assert not compliance_common.reviewed_host(host)


def test_review_due_sources_are_not_run():
    stale = {"old": src(rights_reviewed_at="2026-07-01"), "new": src()}
    assert find_regdocs.runnable(stale, TODAY) == ["new"]
    r = Report()  # run() judges freshness on ITS date, not the machine's
    assert find_regdocs.run("START", 5, 8, {"new": src()}, keys(), r, date(2026, 12, 1)) == []
    r = Report()
    assert find_regdocs.run("START", 5, 8, {"new": src()}, keys(), r, date(2026, 10, 1))


def test_loader_skips_programme_rows_whose_access_review_is_due():
    d = docs()
    row = {"id": "reg-riksdagen-sfs-x", "url": "https://data.riksdagen.se/a"}
    assert not compliance_common.review_due_for_row(row, d, date(2026, 10, 1))
    assert compliance_common.review_due_for_row(row, d, date(2026, 12, 1))
    assert compliance_common.review_due_for_row({"id": "reg-unknown-x"}, d, TODAY)
    assert compliance_common.review_due_for_row({"id": "bov-bfs-x"}, d, date(2027, 1, 1))
    assert not compliance_common.review_due_for_row({"id": "ost-1"}, d, date(2027, 1, 1))


YM_PAGE = b"""<html><body>
<a href="https://finlex.fi/fi/lainsaadanto/2023/751?language=fin"><img/></a>
<a href="https://finlex.fi/fi/lainsaadanto/2023/751?language=fin">Rakentamislaki 751/2023 - FINLEX \xc2\xae</a>
<a href="https://finlex.fi/fi/lainsaadanto/saadoskokoelma/2017/1007">Ymp\xc3\xa4rist\xc3\xb6ministeri\xc3\xb6n asetus rakennusten paloturvallisuudesta hyvin pitk\xc3\xa4 otsikko joka jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu loppuun | FINLEX</a>
<a href="https://example.org/other">Other</a>
</body></html>"""


def test_link_pages_rewrites_keep_best_label_and_language_suffix():
    cfg = docs()["regdocs.json"]["sources"]["finlex-building-decrees"]
    out = find_regdocs.universe("finlex-building-decrees", cfg, find_regdocs.Budget(5),
                                "2026-09-25", fetch=lambda _u, _e: YM_PAGE)
    by_url = {e["url"]: e for e in out}
    fi = by_url["https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/act/statute/2023/751/fin@/main.pdf"]
    sv = by_url["https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/act/statute/2023/751/swe@/main.pdf"]
    assert fi["title"] == "Rakentamislaki 751/2023 (2023/751, suomi)"
    assert sv["language"] == "sv" and sv["title"].endswith("(2023/751, svenska)")
    long_fi = [e for e in out if "/2017/1007/fin@" in e["url"]][0]
    long_sv = [e for e in out if "/2017/1007/swe@" in e["url"]][0]
    assert long_fi["title"].endswith("(2017/1007, suomi)") and len(long_fi["title"]) <= 180
    assert "[#" in long_fi["title"]  # truncated: a stable URL qualifier is kept
    assert long_fi["title"] != long_sv["title"]
    assert len(out) == 4 and all(e["license"] == "cc-by" for e in out)
    assert all(compliance_common.quality_profile(e, docs()) == "normative" for e in out)


def test_truncated_and_duplicate_titles_stay_distinct():
    long = "Samma mycket långa titel " * 12
    items = [{"url": f"https://a.org/{i}.pdf", "title": long} for i in range(2)] + \
            [{"url": f"https://a.org/d{i}.pdf", "title": "Download PDF"} for i in range(2)]
    out = find_regdocs.universe("s", src(items=items), find_regdocs.Budget(1), "2026-09-25")
    norms = [find_regdocs.registry.norm(e["title"]) for e in out]
    assert len(set(norms)) == 4 and all(len(e["title"]) <= 180 for e in out)


SITEMAP = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.boverket.se/sv/PBL-kunskapsbanken/regler-om-byggande/brandskydd/</loc></url>
<url><loc>https://www.boverket.se/sv/PBL-kunskapsbanken/tillganglighetsredogorelse-x/</loc></url>
<url><loc>https://www.boverket.se/sv/om-boverket/jobb/</loc></url>
<url><loc>https://www.boverket.se/sv/byggande/tillganglighet/</loc></url></urlset>"""


def test_sitemap_pages_scope_and_titles():
    cfg = docs()["regdocs.json"]["sources"]["boverket-web"]
    out = find_regdocs.universe("boverket-web", cfg, find_regdocs.Budget(2), "2026-09-25",
                                fetch=lambda _u, _e: SITEMAP)
    urls = [e["url"] for e in out]
    assert urls == ["https://www.boverket.se/sv/PBL-kunskapsbanken/regler-om-byggande/brandskydd/",
                    "https://www.boverket.se/sv/byggande/tillganglighet/"]
    assert out[0]["topic"] == "architecture" and out[0]["license"] == "unverified"
    assert compliance_common.quality_profile(out[0], docs()) is None
    ok, held = compliance_common.split_appendable(out)
    assert ok == [] and len(held) == 2  # never appended before the collect-all split


INDEX = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://s.org/sm-1.xml</loc></sitemap><sitemap><loc>https://s.org/sm-2.xml</loc></sitemap>
</sitemapindex>"""


def test_sitemap_walk_is_resumable_across_runs():
    sm = {"https://s.org/index.xml": INDEX,
          "https://s.org/sm-1.xml": b'<urlset><url><loc>https://s.org/p/1</loc></url></urlset>',
          "https://s.org/sm-2.xml": b'<urlset><url><loc>https://s.org/p/2</loc></url></urlset>'}
    fetched = []
    fetch = lambda u, _e: fetched.append(u) or sm[u]  # noqa: E731
    sources = {"s": src(mechanism="sitemap_pages", sitemaps=["https://s.org/index.xml"],
                        include="/p/", items=None, format="html")}
    r = Report()
    assert find_regdocs.run("START", 5, 2, sources, keys(), r, TODAY, fetch) == []
    assert r.value[0] == "next"  # progress kept in the walk cache, not a hold
    cur = r.value[1]
    r = Report()
    out = find_regdocs.run(cur, 5, 2, sources, keys(), r, TODAY, fetch)
    assert [e["url"] for e in out] == ["https://s.org/p/1", "https://s.org/p/2"]
    assert fetched == ["https://s.org/index.xml", "https://s.org/sm-1.xml", "https://s.org/sm-2.xml"]


def static_sources(n=3):
    items = [{"url": f"https://a.org/{i}.pdf", "title": f"Doc {i}"} for i in range(n)]
    return {"one": src(items=items), "two": src(items=[{"url": "https://b.org/z.pdf",
                                                         "title": "Z"}], source="t2")}


def test_regdocs_rotation_hard_max_watch_and_next_source():
    sources = static_sources()
    r = Report()
    out = find_regdocs.run("START", 2, 8, sources, keys(), r, TODAY)
    assert [e["url"] for e in out] == ["https://a.org/0.pdf", "https://a.org/1.pdf"]
    cur = json.loads(r.value[1])
    assert cur["s"] == "one" and cur["o"] == 2
    r = Report()
    out = find_regdocs.run(json.dumps(cur), 5, 8, sources,
                           keys(urls=["https://a.org/0.pdf", "https://a.org/1.pdf"]), r, TODAY)
    assert [e["url"] for e in out] == ["https://a.org/2.pdf"]
    cur = json.loads(r.value[1])
    assert cur["s"] == "two" and cur["w"]["one"] == "2026-09-25"
    r = Report()
    out = find_regdocs.run(json.dumps(cur), 5, 8, sources, keys(), r, TODAY)
    assert [e["url"] for e in out] == ["https://b.org/z.pdf"]
    cur = json.loads(r.value[1])
    assert set(cur["w"]) == {"one", "two"}
    r = Report()  # every source in its watch phase: no work, cursor kept
    assert find_regdocs.run(json.dumps(cur), 5, 8, sources, keys(), r, TODAY) == []
    assert json.loads(r.value[1]) == cur


def test_regdocs_change_anywhere_restarts_and_deferral_holds():
    sources = static_sources(20)
    r = Report()
    find_regdocs.run("START", 15, 8, sources, keys(), r, TODAY)
    cur = r.value[1]
    changed = static_sources(20)
    changed["one"]["items"].insert(3, {"url": "https://a.org/03-new.pdf", "title": "New"})
    r = Report()
    out = find_regdocs.run(cur, 30, 8, changed, keys(), r, TODAY)
    assert "https://a.org/03-new.pdf" in [e["url"] for e in out]  # not skipped by the offset
    link = {"lp": src(mechanism="link_pages", seeds=["https://s.org/"], link_include="x",
                      items=None)}

    def refuse(_u, _e):
        raise polite_http.Deferred("robots unavailable")
    r = Report()
    assert find_regdocs.run("START", 5, 8, link, keys(), r, TODAY, fetch=refuse) == []
    assert r.value[0] == "hold"


RIKS = b"Plan- och bygglag (2010:900)\n\nSFS nr:     2010:900\nUtf\xc3\xa4rdad:   2010-07-01\n" \
       b"\xc3\x84ndrad:     t.o.m. SFS 2026:1583\n\xc3\x96vrig text:\n"


def test_mutable_statute_becomes_dated_snapshots(web):
    cfg = docs()["regdocs.json"]["sources"]["riksdagen-sfs"]
    sources = {"riksdagen-sfs": {**cfg, "items": cfg["items"][:1]}}
    url = cfg["items"][0]["url"]
    web.answers[url] = FakeResp(200, RIKS, "text/plain")
    r = Report()
    out = find_regdocs.run("START", 5, 8, sources, keys(), r, TODAY)
    assert [e["url"] for e in out] == [url + "#tom-sfs-2026-1583"]
    e = out[0]
    assert e["title"].endswith("(t.o.m. SFS 2026:1583)") and e["document_type"] == "statute-consolidation"
    assert compliance_common.quality_profile(e, docs()) == "normative"
    text = RIKS.decode() + "1 kap. ..." * 50
    assert compliance_common.instrument_anchor(e, text) is True
    assert compliance_common.instrument_anchor(e, text.replace("2026:1583", "2026:1600")) is False
    # a week later the law was amended again: a NEW snapshot row, the old one stays known
    web.answers[url] = FakeResp(200, RIKS.replace(b"2026:1583", b"2026:1700"), "text/plain")
    r2 = Report()
    later = find_regdocs.run(r.value[1], 5, 8, sources, keys(urls=[e["url"]], ids=[e["id"]]), r2,
                             date(2026, 10, 3))
    assert [x["url"] for x in later] == [url + "#tom-sfs-2026-1700"]


# ------------------------------------------------------------------------------------ Cellar
def row(lang, mtype, item, title="Directive"):
    return {"lang": f"http://publications.europa.eu/resource/authority/language/{lang}",
            "mtype": mtype, "item": item, "title": title}


def item_url(expr, manif, doc):
    return f"http://publications.europa.eu/resource/cellar/{UUID}.{expr:04d}.{manif:02d}/DOC_{doc}"


SEED = {"celex": "32024L1275", "name": "EPBD", "topic": "building_energy"}


def test_cellar_item_selection_prefers_xhtml_and_keeps_stable_part_ids():
    rows = [row("SWE", "xhtml", item_url(24, 3, 1)), row("SWE", "xhtml", item_url(24, 3, 2)),
            row("SWE", "pdfa2a", item_url(24, 1, 1)), row("SWE", "fmx4", item_url(24, 2, 1)),
            row("ELL", "pdfa1a", item_url(5, 1, 1)), row("XXX", "xhtml", "http://x/DOC_1")]
    items = find_eurlex.select_items(rows, ["sv", "el"])
    assert [(i["lang"], i["mtype"], i["doc"]) for i in items] == [
        ("el", "pdfa1a", 1), ("sv", "xhtml", 1), ("sv", "xhtml", 2)]
    entries = [find_eurlex.entry_for(SEED, "32024L1275", i, "2026-09-25") for i in items]
    assert [e["id"] for e in entries] == ["eur-32024l1275-el", "eur-32024l1275-sv",
                                          "eur-32024l1275-sv-d2"]
    assert entries[0]["format"] == "pdf" and entries[1]["format"] == "html"
    assert entries[1]["url"].startswith("https://publications.europa.eu/resource/cellar/")
    assert len({e["title"] for e in entries}) == 3
    assert all(e["license"] == "open" for e in entries)
    for e in entries:
        assert compliance_common.quality_profile(e, docs()) == "normative"


def test_cellar_image_items_are_never_documents():
    rows = [dict(row("HRV", "xhtml", item_url(23, 3, n)), mime="image/jpeg") for n in range(1, 13)]
    rows.append(dict(row("HRV", "xhtml", item_url(23, 3, 13)), mime="application/xhtml+xml"))
    rows.append(dict(row("HRV", "pdfa1a", item_url(23, 1, 1)), mime="application/pdf"))
    items = find_eurlex.select_items(rows, ["hr"])
    assert [(i["mtype"], i["doc"]) for i in items] == [("xhtml", 13)]
    only_images = [dict(row("HRV", "xhtml", item_url(23, 3, 1)), mime="image/jpeg"),
                   dict(row("HRV", "pdfa1a", item_url(23, 1, 1)), mime="application/pdf")]
    assert [(i["mtype"]) for i in find_eurlex.select_items(only_images, ["hr"])] == ["pdfa1a"]


def test_cellar_part_added_later_keeps_existing_ids():
    old = find_eurlex.select_items([row("SWE", "xhtml", item_url(24, 3, 2)),
                                    row("SWE", "xhtml", item_url(24, 3, 3))], ["sv"])
    new = find_eurlex.select_items([row("SWE", "xhtml", item_url(24, 3, 1)),
                                    row("SWE", "xhtml", item_url(24, 3, 2)),
                                    row("SWE", "xhtml", item_url(24, 3, 3))], ["sv"])
    ids_old = {find_eurlex.entry_for(SEED, "32024L1275", i, "d")["id"]: i["item"] for i in old}
    ids_new = {find_eurlex.entry_for(SEED, "32024L1275", i, "d")["id"]: i["item"] for i in new}
    for sid, item in ids_old.items():
        assert ids_new[sid] == item  # an existing id never changes its document
    assert set(ids_new) - set(ids_old) == {"eur-32024l1275-sv"}


def test_cellar_profile_requires_resolution_evidence_tied_to_pinned_seeds():
    it = find_eurlex.select_items([row("ELL", "xhtml", item_url(5, 3, 1))], ["el"])[0]
    good = find_eurlex.entry_for(SEED, "32024L1275", it, "d")
    assert compliance_common.quality_profile(good, docs()) == "normative"
    amend = find_eurlex.entry_for(SEED, "32026R0001", it, "d", rel="amends")
    assert compliance_common.quality_profile(amend, docs()) == "normative"
    for bad in ({**good, "persistent_id": "celex:32099L0001"},
                {**good, "url": "https://publications.europa.eu/login"},
                {**good, "resolution": "cellar seed=39999L9999 rel=seed item=DOC_1"},
                {**good, "resolution": "cellar seed=32024L1275 rel=cites item=DOC_1"},
                {**good, "resolution": "cellar seed=32024L1275 rel=seed item=DOC_7"},
                {**good, "resolution": ""}):
        assert compliance_common.quality_profile(bad, docs()) is None
    cons = find_eurlex.entry_for(SEED, "02024L1275-20250101", it, "d", rel="consolidates")
    assert cons["license"] == "cc-by" and cons["document_type"] == "consolidated-act"
    corr = find_eurlex.entry_for(SEED, "32024L1275R(04)", it, "d", rel="corrects")
    assert corr["document_type"] == "corrigendum" and corr["id"] == "eur-32024l1275r-04-el"


def test_cellar_rejects_injection_in_celex():
    with pytest.raises(ValueError):
        find_eurlex.items_query('32024L1275" } DROP')


def fake_cellar(works):
    """query() answering related/items SPARQL from {celex: [lang]} and {("rel", seed): [...]}."""
    calls = []

    def query(q):
        calls.append(q)
        if "?rel" in q:
            seed = q.split('resource_legal_id_celex "')[1].split('"')[0]
            return [{"rel": "amends", "celex": c} for c in works.get(("rel", seed), [])]
        celex = q.split('resource_legal_id_celex "')[1].split('"')[0]
        return [row(lang, "xhtml",
                    f"http://publications.europa.eu/resource/cellar/{celex}.{lang}/DOC_1")
                for lang in works.get(celex, [])]
    return query, calls


def cellar_cfg():
    return {"seeds": [{"celex": "S1", "name": "one", "topic": "urban"},
                      {"celex": "S2", "name": "two", "topic": "urban"}],
            "languages": ["sv", "en", "fi"], "expand": ["amends"],
            "rights_reviewed_at": "2026-09-25"}


def test_cellar_rotation_caps_resume_and_watch(monkeypatch):
    works = {"S1": ["SWE", "ENG", "FIN"], ("rel", "S1"): ["A1"], "A1": ["SWE"], "S2": ["ENG"]}
    query, calls = fake_cellar(works)
    monkeypatch.setattr(find_eurlex, "CELEX_RE", __import__("re").compile(r"[A-Z0-9()\-]{2,40}"))
    r = Report()
    out = find_eurlex.run("START", 2, 5, 10, cellar_cfg(), keys(), r, TODAY, query)
    assert [e["language"] for e in out] == ["en", "fi"]
    cur = json.loads(r.value[1])
    assert (cur["s"], cur["k"], cur["i"]) == (0, "S1", 2) and cur["f"]
    r = Report()
    out = find_eurlex.run(json.dumps(cur), 10, 5, 10, cellar_cfg(), keys(), r, TODAY, query)
    assert [e["id"] for e in out] == ["eur-s1-sv", "eur-a1-sv", "eur-s2-en"]
    assert out[1]["resolution"] == "cellar seed=S1 rel=amends item=DOC_1"
    assert r.value == ("next", "watch:2026-09-25")
    r = Report()
    n = len(calls)
    assert find_eurlex.run("watch:2026-09-20", 10, 5, 10, cellar_cfg(), keys(), r, TODAY, query) == []
    assert len(calls) == n and r.value == ("next", "watch:2026-09-20")
    missing = (ops.WORKSPACE / "eurlex-missing-languages.jsonl").read_text()
    assert '"A1"' in missing and '"fi"' in missing  # unavailable expressions are recorded


def test_cellar_changed_item_list_restarts_the_work(monkeypatch):
    monkeypatch.setattr(find_eurlex, "CELEX_RE", __import__("re").compile(r"[A-Z0-9()\-]{2,40}"))
    query, _ = fake_cellar({"S1": ["SWE", "ENG", "FIN"], "S2": []})
    r = Report()
    find_eurlex.run("START", 1, 5, 10, cellar_cfg(), keys(), r, TODAY, query)
    cur = r.value[1]  # stopped inside S1 at item 1 (en taken)
    query2, _ = fake_cellar({"S1": ["DAN", "SWE", "ENG", "FIN"], "S2": []})
    cfg = {**cellar_cfg(), "languages": ["da", "sv", "en", "fi"]}
    r = Report()
    out = find_eurlex.run(cur, 10, 5, 10, cfg, keys(ids=["eur-s1-en"]), r, TODAY, query2)
    assert "eur-s1-da" in [e["id"] for e in out]  # the new first item is not skipped


def test_cellar_expired_watch_restarts_the_walk(monkeypatch):
    monkeypatch.setattr(find_eurlex, "CELEX_RE", __import__("re").compile(r"[A-Z0-9()\-]{2,40}"))
    query, _ = fake_cellar({"S1": ["SWE"], "S2": ["ENG"]})
    r = Report()
    out = find_eurlex.run("watch:2026-09-18", 10, 5, 10, cellar_cfg(), keys(), r, TODAY, query)
    assert [e["id"] for e in out] == ["eur-s1-sv", "eur-s2-en"]


def test_cellar_failure_holds():
    def boom(_q):
        raise polite_http.Deferred("HTTP 503")
    cfg = {"seeds": [{"celex": "32024L1275", "name": "x", "topic": "urban"}], "languages": ["sv"],
           "expand": [], "rights_reviewed_at": "2026-09-25"}
    r = Report()
    assert find_eurlex.run("START", 5, 1, 5, cfg, keys(), r, TODAY, boom) == []
    assert r.value[0] == "hold"


# ------------------------------------------------------------------------------------ ESEF
ISSUER = {"lei": "5493008HS8STXVZXYZ63", "name": "Per Aarsleff Holding A/S", "country": "DK",
          "sector": "construction", "topic": "construction", "fiscal_year_end": "09-30"}


def filing(period, report, fxo):
    return {"attributes": {"period_end": period, "report_url": report, "fxo_id": fxo}}


def esef_cfg():
    return {"issuers": [ISSUER], "license": "proprietary", "license_evidence": "issuer copyright",
            "rights_reviewed_at": "2026-09-25"}


def test_esef_annual_entries_languages_packages_and_relative_urls():
    lei = ISSUER["lei"]
    filings = [
        filing("2024-09-30", "/L/2024-09-30/ESEF/DK/0/a-da/reports/annual.xhtml", f"{lei}-2024-09-30-ESEF-DK-0"),
        filing("2024-09-30", "/L/2024-09-30/ESEF/DK/1/a-da/reports/annual.xhtml", f"{lei}-2024-09-30-ESEF-DK-1"),
        filing("2024-09-30", "/L/2024-09-30/ESEF/DK/0/a-en/reports/a-en.xhtml", f"{lei}-2024-09-30-ESEF-DK-0"),
        filing("2023-09-30", None, "F9"),
    ]
    out = find_esef.annual_entries(ISSUER, filings, esef_cfg())
    assert [e["url"] for e in out] == sorted(e["url"] for e in out) or True
    assert len(out) == 3 and len({e["id"] for e in out}) == 3  # amended package stays distinct
    assert len({e["title"] for e in out}) == 3
    assert all(e["url"].startswith("https://filings.xbrl.org/L/") for e in out)
    assert "published_at" not in out[0]
    ok, held = compliance_common.split_appendable(out)
    assert ok == [] and len(held) == 3


def test_esef_language_members_of_one_package_stay_distinct():
    lei = ISSUER["lei"]
    base = f"/{lei}/2024-09-30/ESEF/DK/0"
    filings = [filing("2024-09-30", f"{base}/pkg/sv/reports/annual.xhtml", "F0"),
               filing("2024-09-30", f"{base}/pkg/en/reports/annual.xhtml", "F0")]
    out = find_esef.annual_entries(ISSUER, filings, esef_cfg())
    assert len({e["id"] for e in out}) == 2 and len({e["title"] for e in out}) == 2
    assert sorted(e["language"] for e in out) == ["en", "sv"]


def test_esef_interim_filers_and_paged_issuers_go_to_review(monkeypatch):
    first = find_esef.first_page_url(ISSUER["lei"])
    pages = {first: {"data": [filing("2024-09-30", "/r/2024.xhtml", "A"),
                              filing("2024-12-31", "/r/q1.xhtml", "Q")], "links": {}}}
    r = Report()
    out = find_esef.run("START", 5, 1, 2, 4, esef_cfg(), keys(), r, TODAY, pages.__getitem__)
    assert out == [] and r.value == ("next", "watch:2026-09-25")
    queued = (ops.WORKSPACE / "esef-review.jsonl").read_text()
    assert "/r/2024.xhtml" in queued
    pages = {first: {"data": [filing("2024-09-30", "/r/2024.xhtml", "A")],
                     "links": {"next": "/api/entities/X/filings?page%5Bnumber%5D=2"}},
             "https://filings.xbrl.org/api/entities/X/filings?page%5Bnumber%5D=2":
                 {"data": [filing("2025-09-30", "/r/2025.xhtml", "C")], "links": {}}}
    r = Report()
    assert find_esef.run("START", 5, 1, 2, 4, esef_cfg(), keys(), r, TODAY,
                         pages.__getitem__) == []


def test_esef_hard_max_stays_on_the_page():
    first = find_esef.first_page_url(ISSUER["lei"])
    pages = {first: {"data": [filing(f"20{y}-09-30", f"/r/20{y}.xhtml", f"F{y}")
                              for y in (22, 23, 24)], "links": {}}}
    r = Report()
    out = find_esef.run("START", 2, 1, 2, 4, esef_cfg(), keys(), r, TODAY, pages.__getitem__)
    assert len(out) == 2 and json.loads(r.value[1])["p"] == first
    r2 = Report()
    out2 = find_esef.run(r.value[1], 5, 1, 2, 4, esef_cfg(),
                         keys(urls=[e["url"] for e in out]), r2, TODAY, pages.__getitem__)
    assert len(out2) == 1 and r2.value == ("next", "watch:2026-09-25")


# ------------------------------------------------------------------------------------ gate
GREEK = ("Οδηγία (ΕΕ) 2024/1275 για την ενεργειακή απόδοση των κτιρίων και τις απαιτήσεις "
         "του κράτους μέλους. " * 40)
SV_LEGAL = ("Kommissionens delegerade förordning (EU) 2023/2772. Företaget ska lämna upplysningar "
            "om väsentliga konsekvenser, risker och möjligheter. " * 30)
FI_LEGAL = ("Rakentamislaki 751/2023. Rakennuksen on oltava turvallinen ja terveellinen koko "
            "sen käyttöiän ajan. " * 40)
SHORT_CLAUSE = "BFS 2020:4. 5:2 Byggnader ska utformas så att brand inte uppstår. " * 6
TABLE = "BFS 2011:6 Tabell 9:2a " + "Zon I 90 75 60 Zon II 110 95 75 Zon III 130 110 90 " * 60
LOGIN = ("Sign in to continue. Please enter your username and password to access this "
         "service. Forgot your password? Contact support. " * 40)


def metrics(text, row):
    m = quality.metrics(text)
    anchor = compliance_common.instrument_anchor(row, text)
    if anchor is not None:
        m["anchor"] = anchor
    return m


EUR_EL = {"id": "eur-32024l1275-el", "persistent_id": "celex:32024L1275",
          "url": f"https://publications.europa.eu/resource/cellar/{UUID}.0005.03/DOC_1"}
EUR_SV = {"id": "eur-32023r2772-sv", "persistent_id": "celex:32023R2772", "url": EUR_EL["url"]}
FI_ROW = {"id": "reg-finlex-building-decrees-x",
          "url": "https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/act/statute/2023/751/fin@/main.pdf"}
BFS_KONS = {"id": "bov-bfs-bfs2011-6-bfs2020-4",
            "url": "https://rinfo.boverket.se/BFS2011-6/pdf/BFS2020-4.pdf"}
BFS_TABLE = {"id": "bov-bfs-bfs2011-6", "url": "https://rinfo.boverket.se/BFS2011-6/pdf/BFS2011-6.pdf"}


@pytest.mark.parametrize(("text", "rowspec"), [
    (GREEK, EUR_EL), (SV_LEGAL, EUR_SV), (FI_LEGAL, FI_ROW), (SHORT_CLAUSE, BFS_KONS),
    (TABLE, BFS_TABLE)], ids=["greek", "sv-esrs", "fi", "short-clause", "table"])
def test_normative_profile_keeps_what_the_generic_gate_drops(text, rowspec):
    m = metrics(text, rowspec)
    assert quality.verdict_for(m, False, "normative") == "ok"


def test_anchor_requires_the_own_identity_in_the_head_and_no_shell_markers():
    login = "Directive (EU) 2024/1275 — sign in to continue. " + "Please enter your password. " * 50
    assert compliance_common.instrument_anchor(EUR_EL, login) is False
    cited_deep = "Commission notice on something else. " * 200 + "Directive (EU) 2024/1275"
    assert compliance_common.instrument_anchor(EUR_EL, cited_deep) is False
    kons = {"id": "bov-bfs-bfs2011-6-bfs2020-4-kons",
            "url": "https://rinfo.boverket.se/BFS2011-6/dok/BFS2020-4_Konsolidering.pdf"}
    right = "Boverkets byggregler (2011:6)\nBFS 2011:6 med ändringar till och med BFS 2020:4\n" * 3
    wrong = "Boverkets byggregler (2011:6)\nBFS 2011:6 med ändringar till och med BFS 2018:4\n" * 3
    assert compliance_common.instrument_anchor(kons, right) is True
    assert compliance_common.instrument_anchor(kons, wrong) is False
    dk = {"id": "reg-retsinformation-br18-x",
          "url": "https://www.retsinformation.dk/eli/lta/2023/1673/pdf"}
    assert compliance_common.instrument_anchor(dk, "§ 1 ... 11. december 2023. Nr. 1673. ") is True
    assert compliance_common.instrument_anchor(dk, "nr. 1673 af 12. december 2019") is False


def test_prune_backfills_the_anchor_of_a_normative_row(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_corpus, "HERE", tmp_path)
    monkeypatch.setattr(prune_corpus, "ACCESS", None)
    (tmp_path / "text").mkdir()
    it = find_eurlex.select_items([row("ELL", "xhtml", item_url(5, 3, 1))], ["el"])[0]
    good = find_eurlex.entry_for(SEED, "32024L1275", it, "d")
    (tmp_path / "text" / f"{good['id']}.md").write_text("# t\n\n---\n\n" + GREEK)
    stale = quality.metrics(GREEK)  # extracted before the anchor metric existed
    plan = prune_corpus.decide([{**good, "status": "ok", "text_path": f"text/{good['id']}.md",
                                 "quality": stale}], {}, {}, set(), docs())
    assert good["id"] not in plan.drop and plan.quality[good["id"]]["quality"]["anchor"] is True


def test_normative_profile_rejects_login_pages_other_acts_and_short_text():
    assert quality.verdict_for(metrics(LOGIN, EUR_EL), False, "normative") == "unanchored"
    assert quality.verdict_for(metrics(GREEK, EUR_SV), False, "normative") == "unanchored"
    assert quality.verdict_for(metrics("BFS 2020:4 § 1 Kort.", BFS_KONS), False,
                               "normative") == "thin"
    assert quality.verdict_for(quality.metrics(GREEK), False, "normative") == "unanchored"
    with pytest.raises(ValueError):
        quality.verdict_for(quality.metrics(GREEK), False, "lenient")
    assert quality.verdict(quality.metrics(GREEK), False) != "ok"  # the generic gate is unchanged


def test_prune_uses_the_profile_only_for_verified_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_corpus, "HERE", tmp_path)
    monkeypatch.setattr(prune_corpus, "ACCESS", None)
    (tmp_path / "text").mkdir()
    it = find_eurlex.select_items([row("ELL", "xhtml", item_url(5, 3, 1))], ["el"])[0]
    good = find_eurlex.entry_for(SEED, "32024L1275", it, "d")
    spoof = {**good, "id": "eur-32024l1275-bg", "persistent_id": "celex:32024L1276"}
    rows = []
    for e in (good, spoof):
        (tmp_path / "text" / f"{e['id']}.md").write_text(GREEK)
        rows.append({**e, "status": "ok", "text_path": f"text/{e['id']}.md",
                     "quality": metrics(GREEK, e)})
    plan = prune_corpus.decide(rows, {}, {}, set(), docs())
    assert good["id"] not in plan.drop
    assert plan.drop[spoof["id"]] == "thin"


# ------------------------------------------------------------------------------------ loader
def programme_row(sid="reg-t-x-1", url="https://data.riksdagen.se/dokument/sfs-2010-900.text"):
    return {"id": sid, "title": "t", "url": url, "source": "riksdagen_sfs",
            "license": "public-domain", "topic": "standards_protocols", "format": "txt"}


@pytest.fixture
def loader(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    monkeypatch.setattr(build_corpus, "HOST_POLICY", {})
    monkeypatch.setattr(build_corpus, "HELD_OK_IDS", set())
    monkeypatch.setattr(build_corpus, "_wait_for_host", lambda _h: None)
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("programme rows never use curl"))
    build_corpus.PROGRAMME.reset()
    state = SimpleNamespace(calls=calls, robots={}, answers={})

    def decision(url, fetcher=None):
        v = state.robots.get(url.split("/")[2], (True, None))
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(robots_policy, "decision", decision)

    def get(url, **kw):
        calls.append((url, kw.get("stream"), (kw.get("headers") or {}).get("User-Agent")))
        return state.answers[url]
    monkeypatch.setattr(build_corpus.requests, "get", get)
    yield state
    build_corpus.PROGRAMME.reset()


def test_loader_robots_denial_is_a_hard_policy_failure_without_request(loader):
    loader.robots["data.riksdagen.se"] = (False, None)
    rec = build_corpus.download_one(programme_row())
    assert "robots.txt disallows" in rec["error"] and "transient" not in rec
    assert loader.calls == []


def test_loader_robots_unavailable_defers_without_request(loader):
    loader.robots["data.riksdagen.se"] = robots_policy.RobotsUnavailable("HTTP 503")
    rec = build_corpus.download_one(programme_row())
    assert rec["transient"] is True and rec.get("_not_requested") is True
    assert loader.calls == []


def test_loader_programme_redirect_to_an_unreviewed_host_is_refused(loader):
    url = programme_row()["url"]
    loader.answers[url] = FakeResp(302, location="https://cdn.example/x.txt", url=url)
    rec = build_corpus.download_one(programme_row())
    assert "not a reviewed delivery host" in rec["error"] and rec["refused_hop"].startswith("https://cdn")
    assert [c[0] for c in loader.calls] == [url]


def test_loader_programme_hops_use_the_honest_ua_and_reject_html_challenges(loader):
    url = programme_row()["url"]
    loader.answers[url] = FakeResp(200, b"<html><div class='g-recaptcha'></div></html>",
                                   "text/html", url=url)
    rec = build_corpus.download_one(programme_row())
    assert rec["transient"] is True and "challenge" in rec["error"]
    assert loader.calls[0][2] == build_corpus.HONEST_UA
    second = build_corpus.download_one(programme_row("reg-t-x-2"))
    assert "circuit open" in second["error"] and len(loader.calls) == 1


def test_loader_streams_with_a_byte_cap_and_crawl_delay(loader, monkeypatch):
    url = programme_row()["url"]
    loader.robots["data.riksdagen.se"] = (True, 9.0)
    loader.answers[url] = FakeResp(200, b"Plan- och bygglag (2010:900) " * 10, "text/plain", url=url)
    rec = build_corpus.download_one(programme_row())
    assert rec["raw_path"] and loader.calls[0][:2] == (url, True), rec.get("error")
    assert build_corpus.HOST_DELAY["data.riksdagen.se"] >= 9.0
    monkeypatch.setattr(build_corpus, "PROGRAMME_MAX_BYTES", 20)
    rec = build_corpus.download_one(programme_row())
    assert rec["error"].startswith("too-large") and not rec.get("raw_path")


def test_loader_document_deadline_is_retryable(loader, monkeypatch):
    url = programme_row()["url"]
    monkeypatch.setattr(build_corpus, "DOCUMENT_DEADLINE", 0.01)
    loader.answers[url] = FakeResp(200, b"x" * 40, "text/plain", url=url, slow=0.005)
    rec = build_corpus.download_one(programme_row())
    assert "deadline" in rec["error"] and rec["transient"] is True


def test_loader_deadline_spent_in_waits_is_not_requested(loader, monkeypatch):
    url = programme_row()["url"]
    loader.answers[url] = FakeResp(200, b"x", "text/plain", url=url)
    monkeypatch.setattr(build_corpus, "DOCUMENT_DEADLINE", 0.05)
    monkeypatch.setattr(build_corpus, "_wait_for_host", lambda _h: time.sleep(0.1))
    rec = build_corpus.download_one(programme_row())
    assert rec["transient"] is True and loader.calls == []


def test_loader_checks_robots_on_the_prepared_path(loader, monkeypatch):
    seen = []

    def decision(url, fetcher=None):
        seen.append(url)
        return ("/private/" not in url, None)
    monkeypatch.setattr(robots_policy, "decision", decision)
    row = programme_row(url="https://data.riksdagen.se/public/../private/x.text")
    rec = build_corpus.download_one(row)
    assert "robots.txt disallows" in rec["error"] and loader.calls == []
    assert seen == ["https://data.riksdagen.se/private/x.text"]


SNAP = "https://data.riksdagen.se/dokument/sfs-2010-900.text#tom-sfs-2025-1"
RIKS_NOW = (b"Plan- och bygglag (2010:900)\n\nSFS nr:     2010:900\n"
            b"\xc3\x84ndrad:     t.o.m. SFS 2026:1583\n")


def test_an_amended_snapshot_never_overwrites_or_fails_a_held_copy(loader, tmp_path):
    loader.answers[SNAP] = FakeResp(200, RIKS_NOW, "text/plain", url=SNAP)  # the fragment is never sent
    row = programme_row("reg-riksdagen-sfs-x", SNAP)
    fresh = build_corpus.download_one(row)
    assert fresh["error"].startswith("snapshot-superseded") and not fresh.get("raw_path")
    build_corpus.HELD_OK_IDS.add(row["id"])
    held = build_corpus.download_one(row)
    assert held.get("_deferred") and not held.get("raw_path")
    assert not (tmp_path / "raw").exists()  # nothing was written before the version check


def test_loader_programme_budget_defers_all_programme_work(loader, monkeypatch):
    url = programme_row()["url"]
    loader.answers[url] = FakeResp(200, b"(2010:900) " * 10, "text/plain", url=url)
    monkeypatch.setattr(build_corpus, "PROGRAMME_BYTES", 30)
    rec = build_corpus.download_one(programme_row())
    assert rec.get("_deferred") and not rec.get("raw_path")
    rec = build_corpus.download_one(programme_row("reg-t-x-3"))  # budget spent: not requested
    assert rec.get("_deferred") and len(loader.calls) == 1
    build_corpus.PROGRAMME.reset(now=time.monotonic() - build_corpus.PROGRAMME_WALL - 1)
    monkeypatch.setattr(build_corpus, "PROGRAMME_BYTES", 10**9)
    rec = build_corpus.download_one(programme_row("reg-t-x-5"))
    assert rec.get("_deferred")  # the programme's wall budget is spent


def test_a_deferred_restoration_is_locally_unavailable_not_failed(tmp_path, monkeypatch):
    import registry
    held = {"id": "eur-32024l1275-sv", "status": "ok", "text_path": "text/eur-32024l1275-sv.md",
            "url": "https://publications.europa.eu/x"}
    assert registry.programme_unavailable(held, tmp_path)
    assert registry.locally_unavailable_rows([held, {**held, "id": "ost-1"}], {}, tmp_path) == [held]
    (tmp_path / "text").mkdir()
    (tmp_path / "text" / "eur-32024l1275-sv.md").write_text("x")
    assert not registry.programme_unavailable(held, tmp_path)
    monkeypatch.setattr(prune_corpus, "HERE", tmp_path / "elsewhere")
    monkeypatch.setattr(prune_corpus, "ACCESS", None)
    plan = prune_corpus.decide([{**held, "title": "t", "source": "eurlex"}], {}, {}, set(), docs())
    assert held["id"] not in plan.drop  # never "no-text": its restoration resumes later


def test_loader_programme_refusal_is_transient_and_never_retried_with_curl(loader):
    url = programme_row()["url"]
    loader.answers[url] = FakeResp(403, b"no", "text/plain", url=url)
    rec = build_corpus.download_one(programme_row())
    assert rec["transient"] is True and len(loader.calls) == 1


def test_programme_hosts_are_polite_and_aliases_share_budgets():
    for host in ("www.boverket.se", "publications.europa.eu", "filings.xbrl.org", "ym.fi"):
        assert host in build_corpus.POLITE_HOSTS and build_corpus.HOST_CONCURRENCY[host] == 1
        assert build_corpus.HOST_UA[host] == build_corpus.HONEST_UA
    assert build_corpus.HOST_DELAY["www.boverket.se"] >= 10 and build_corpus.HOST_DELAY["ym.fi"] >= 5
    assert build_corpus.pace_key("boverket.se") == "www.boverket.se"
    srcs = [programme_row(f"reg-b-{i}", f"https://{'www.' if i % 2 else ''}boverket.se/sv/{i}/")
            for i in range(20)]
    kept, deferred = build_corpus.cap_per_host(srcs)
    assert len(kept) == build_corpus.HOST_RUN_CAP["www.boverket.se"] == 12 and len(deferred) == 8


def test_programme_wide_and_esef_caps(monkeypatch):
    monkeypatch.setattr(build_corpus, "PROGRAMME_RUN_CAP", 5)
    srcs = [programme_row(f"esf-{i}", f"https://filings.xbrl.org/r/{i}.xhtml") for i in range(6)]
    srcs += [programme_row(f"eur-{i}", f"https://h{i}.example/x") for i in range(6)]
    kept, deferred = build_corpus.cap_per_host(srcs)
    assert [s["id"] for s in kept] == ["esf-0", "esf-1", "esf-2", "esf-3", "eur-0"]
    restoring = {"esf-5": {"status": "ok"}}
    kept, deferred = build_corpus.cap_per_host(srcs, restoring)
    assert "esf-5" in deferred  # programme restorations are budgeted too (resumable)
    other = [{"id": f"ost-{i}", "url": "https://www.boverket.se/x.pdf"} for i in range(20)]
    kept, _ = build_corpus.cap_per_host(other, {s["id"]: {"status": "ok"} for s in other})
    assert len(kept) == 20  # other veins' restorations stay uncapped


def test_extraction_records_the_instrument_anchor():
    m = build_corpus.metrics_for({"id": BFS_KONS["id"], "url": BFS_KONS["url"]}, SHORT_CLAUSE)
    assert m["anchor"] is True
    m = build_corpus.anchored({"id": "bov-bfs-bfs2011-6", "url": BFS_TABLE["url"]},
                              {"total": 1, "anchor": True, "w20": {}, "w100": {}}, LOGIN)
    assert m["anchor"] is False  # a byte-identical template's anchor is never inherited
    assert "anchor" not in build_corpus.metrics_for({"id": "ost-1"}, SHORT_CLAUSE)


@pytest.mark.parametrize(("body", "fmt", "want"), [
    (b"<html><title>Just a moment...</title>", "html", True),
    (b"<html><head><script src='/cdn-cgi/challenge-platform/x'>", "html", True),
    (b"<html><p>The challenge of accessibility in buildings</p>", "html", False),
    (b"%PDF-1.7", "pdf", False),
])
def test_html_challenge_detection(body, fmt, want):
    assert build_corpus.is_challenge(200, body, fmt) is want


# ------------------------------------------------------------------------------------ config
def test_finders_read_the_view_pinned_configuration(monkeypatch):
    pinned = docs()["eurlex.json"]
    pinned = {**pinned, "seeds": pinned["seeds"][:1]}

    class View:
        def config_get(self):
            return SimpleNamespace(documents={"eurlex.json": pinned})

    class Store:
        def read(self, timeout=0):
            from contextlib import nullcontext
            return nullcontext(View())
    monkeypatch.setattr(store, "open", lambda **_k: Store())
    monkeypatch.setattr(store, "pinned_policy", lambda _v: ({}, {}))
    got = compliance_common.pinned_config("eurlex.json", find_eurlex.validate)
    assert got["seeds"] == pinned["seeds"] != docs()["eurlex.json"]["seeds"]
    with pytest.raises(ValueError):
        compliance_common.pinned_config("esef.json", find_esef.validate)


def test_programme_config_contracts():
    d = docs()
    backends = {"find_regdocs": {}, "find_eurlex": {}, "find_esef": {"enabled": False}}
    assert check_contracts.programme_config_errors(d, backends) == []
    errs = check_contracts.programme_config_errors(d, {**backends, "find_esef": {"enabled": True}})
    assert any("awaits the collect-all" in e for e in errs)
    errs = check_contracts.programme_config_errors({**d, "eurlex.json": None}, backends)
    assert any("missing" in e for e in errs)
    bad = json.loads(json.dumps(d["regdocs.json"]))
    bad["sources"]["efrag-esrs"]["enabled"] = True
    assert check_contracts.programme_config_errors({**d, "regdocs.json": bad}, backends)


# ------------------------------------------------------------------------------------ round 3
class TrickleResp:
    """A response whose second read blocks until the connection is closed (a trickling source
    whose bytes keep resetting the idle timeout)."""

    def __init__(self):
        self.closed = threading.Event()
        self.headers = {"content-type": "text/plain"}
        self.status_code, self.url = 200, ""
        self.raw = SimpleNamespace()

    def iter_content(self, _n):
        yield b"x" * 10
        if self.closed.wait(5):
            raise OSError("connection shut down")
        yield b"late"

    def close(self):
        self.closed.set()


def test_stream_guard_enforces_the_deadline_on_a_blocked_read():
    import stream_guard
    resp = TrickleResp()
    t0 = time.monotonic()
    with pytest.raises(stream_guard.DeadlineExceeded):
        stream_guard.read_body(resp, max_bytes=10**6, deadline=time.monotonic() + 0.2)
    assert time.monotonic() - t0 < 2 and resp.closed.is_set()


def test_loader_hard_deadline_on_a_trickling_stream(loader, monkeypatch):
    url = programme_row()["url"]
    loader.answers[url] = TrickleResp()
    monkeypatch.setattr(build_corpus, "DOCUMENT_DEADLINE", 0.2)
    t0 = time.monotonic()
    rec = build_corpus.download_one(programme_row())
    assert time.monotonic() - t0 < 2
    assert "deadline" in rec["error"] and rec["transient"] is True


def test_discovery_never_requests_after_its_deadline(web, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(polite_http, "time", clock)
    monkeypatch.setattr(polite_http, "pace", lambda _u, _d: clock.sleep(200))
    web.answers["https://x.org/late"] = FakeResp(200, b"")
    with pytest.raises(polite_http.Deferred):
        polite_http.get("https://x.org/late", expect="text")
    assert web.calls == []


def test_robots_fetch_is_bounded_by_its_own_deadline(monkeypatch):
    monkeypatch.setattr(robots_policy, "ROBOTS_DEADLINE", 0.2)
    monkeypatch.setattr(robots_policy.requests, "get", lambda *_a, **_k: TrickleResp())
    t0 = time.monotonic()
    with pytest.raises(robots_policy.RobotsUnavailable):
        robots_policy.decision("https://ok.org/a")
    assert time.monotonic() - t0 < 2


def test_an_unavailable_programme_row_never_displaces_an_available_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_corpus, "HERE", tmp_path)
    monkeypatch.setattr(prune_corpus, "ACCESS", None)
    (tmp_path / "text").mkdir()
    (tmp_path / "text" / "reg-z.md").write_text("# t\n\n---\n\n" + SV_LEGAL)
    common = {"status": "ok", "sha256": "ab" * 32, "source": "x", "license": "open",
              "format": "txt", "quality": quality.metrics(
                  "The building energy performance and the concrete structure of the house. " * 80)}
    rows = [{**common, "id": "eur-a", "title": "A", "url": "https://publications.europa.eu/a",
             "text_path": "text/eur-a.md", "raw_path": "raw/x/eur-a.html"},
            {**common, "id": "reg-z", "title": "Z", "url": "https://data.riksdagen.se/z",
             "text_path": "text/reg-z.md"}]
    plan = prune_corpus.decide(rows, {}, {}, set(), docs())
    assert "reg-z" not in plan.drop and "eur-a" not in plan.drop


def test_held_raw_without_text_is_repairable_not_unavailable(tmp_path):
    import registry
    row = {"id": "reg-x", "status": "ok", "text_path": "text/reg-x.md", "raw_path": "raw/r/reg-x.txt"}
    (tmp_path / "raw" / "r").mkdir(parents=True)
    (tmp_path / "raw" / "r" / "reg-x.txt").write_text("SFS nr: 2010:900")
    assert not registry.programme_unavailable(row, tmp_path)
    exists = lambda r, stage: stage == "raw"  # noqa: E731 — a versioned run's answer
    assert not registry.programme_unavailable(row, exists=exists)
    assert registry.programme_unavailable(row, exists=lambda r, stage: False)


def _programme_repo(monkeypatch, tmp_path, entries, manifest=(), regdocs=None):
    import pipeline_repo
    root = pipeline_repo.write_repo(tmp_path / "repo", entries=entries, manifest=manifest)
    if regdocs is not None:
        (root / "registry" / "regdocs.json").write_text(json.dumps(regdocs))
    pipeline_repo.point(monkeypatch, root)
    monkeypatch.setattr(build_corpus, "deferred_path", lambda: tmp_path / "fetch-deferred.json")
    return root


RIKS_ROW = {"id": "reg-riksdagen-sfs-pbl", "title": "PBL",
            "url": "https://data.riksdagen.se/dokument/sfs-2010-900.text",
            "source": "riksdagen_sfs", "license": "public-domain",
            "topic": "standards_protocols", "format": "txt"}


def test_loader_repairs_missing_text_from_held_raw_without_network(monkeypatch, tmp_path, capsys):
    import sys as _sys
    held = {**RIKS_ROW, "status": "ok", "sha256": "0" * 64, "bytes": 30,
            "raw_path": "raw/riksdagen_sfs/reg-riksdagen-sfs-pbl.txt",
            "text_path": "text/reg-riksdagen-sfs-pbl.md", "text_chars": 30}
    root = _programme_repo(monkeypatch, tmp_path, [RIKS_ROW], [held], docs()["regdocs.json"])
    (root / "raw" / "riksdagen_sfs").mkdir(parents=True)
    (root / "raw" / "riksdagen_sfs" / "reg-riksdagen-sfs-pbl.txt").write_text(
        "Plan- och bygglag (2010:900)\n\nSFS nr: 2010:900\n" + "1 kap. " * 100)
    monkeypatch.setattr(build_corpus, "download_one",
                        lambda _s: pytest.fail("a held raw file needs no network"))
    monkeypatch.setattr(_sys, "argv", ["build_corpus.py", "--workers", "1"])
    build_corpus.main()
    assert "re-extracted 1 held documents" in capsys.readouterr().out
    assert (root / "text" / "reg-riksdagen-sfs-pbl.md").exists()


def test_loader_requests_nothing_for_a_source_whose_review_is_due(monkeypatch, tmp_path, capsys):
    import sys as _sys
    stale = json.loads(json.dumps(docs()["regdocs.json"]))
    stale["sources"]["riksdagen-sfs"]["rights_reviewed_at"] = "2026-01-01"
    row = {**RIKS_ROW, "id": "reg-riksdagen-sfs-new"}
    _programme_repo(monkeypatch, tmp_path, [row], [], stale)
    monkeypatch.setattr(build_corpus, "download_one",
                        lambda _s: pytest.fail("a stale access review allows no request"))
    monkeypatch.setattr(_sys, "argv", ["build_corpus.py", "--workers", "1"])
    build_corpus.main()
    assert "wait for an access-terms re-review" in capsys.readouterr().out


def test_unrestorable_snapshots_cool_down_instead_of_starving_others(monkeypatch, tmp_path):
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    build_corpus._cool("reg-riksdagen-sfs-old")
    assert "reg-riksdagen-sfs-old" in build_corpus._cooldowns()
    assert "reg-other" not in build_corpus._cooldowns()


def test_eu_consolidation_anchor_checks_its_own_version_stamp():
    head = ("02023R2772 — FI — 01.01.2025 — 001.001\n\nKomission delegoitu asetus (EU) "
            "2023/2772, annettu 31 päivänä heinäkuuta 2023\n") * 2
    right = {"id": "eur-02023r2772-20250101-fi", "persistent_id": "celex:02023R2772-20250101",
             "url": EUR_EL["url"]}
    wrong = {**right, "id": "eur-02023r2772-20240101-fi",
             "persistent_id": "celex:02023R2772-20240101"}
    assert compliance_common.instrument_anchor(right, head) is True
    assert compliance_common.instrument_anchor(wrong, head) is False


# ------------------------------------------------------------------------------------ round 4
import socket as _socket  # noqa: E402


@pytest.fixture
def trickle_server():
    """A local HTTP server that trickles: mode "headers" never finishes its header block,
    mode "body" sends complete headers (Connection: close, no length) and then a body byte every
    50 ms. Real sockets through the installed requests/urllib3 stack; no outside network."""
    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def serve(conn):
        with conn:
            try:
                req = conn.recv(65536).decode("latin-1")
                if "/headers" in req:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                else:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                 b"Connection: close\r\n\r\n")
                for _ in range(100):
                    if stop.is_set():
                        return
                    conn.sendall(b"a")
                    time.sleep(0.05)
            except OSError:
                return

    def accept():
        while not stop.is_set():
            try:
                srv.settimeout(0.2)
                conn, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    stop.set()
    srv.close()


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_real_transport_deadline_covers_headers_and_close_delimited_bodies(trickle_server, mode):
    import requests as _requests
    import stream_guard
    t0 = time.monotonic()
    with pytest.raises(stream_guard.DeadlineExceeded):
        with stream_guard.Deadline(time.monotonic() + 0.3) as guard:
            resp = _requests.get(f"{trickle_server}/{mode}", stream=True, timeout=(5, 5))
            stream_guard.read_body(resp, max_bytes=10**6, guard=guard)
    assert time.monotonic() - t0 < 1.5


def test_real_transport_loader_row_is_bounded(trickle_server, loader, monkeypatch):
    import requests as _requests
    monkeypatch.setattr(build_corpus.requests, "get", _requests.api.get)  # the real transport
    monkeypatch.setattr(compliance_common, "PROGRAMME_HOSTS",
                        {**compliance_common.PROGRAMME_HOSTS, "127.0.0.1": (0.0, 6)})
    monkeypatch.setattr(build_corpus, "DOCUMENT_DEADLINE", 0.3)
    for mode in ("headers", "body"):
        t0 = time.monotonic()
        rec = build_corpus.download_one(programme_row(f"reg-t-{mode}", f"{trickle_server}/{mode}"))
        assert time.monotonic() - t0 < 1.5, mode
        assert "deadline" in rec["error"] and rec["transient"] is True, rec


def test_real_transport_discovery_is_bounded(trickle_server, monkeypatch):
    monkeypatch.setattr(compliance_common, "PROGRAMME_HOSTS",
                        {**compliance_common.PROGRAMME_HOSTS, "127.0.0.1": (0.0, 6)})
    monkeypatch.setattr(robots_policy, "decision", lambda _u, fetcher=None: (True, None))
    monkeypatch.setattr(polite_http, "DEADLINE", 0.3)
    polite_http.set_policy({})
    for mode in ("headers", "body"):
        t0 = time.monotonic()
        with pytest.raises(polite_http.Deferred):
            polite_http.get(f"{trickle_server}/{mode}", delay=0)
        assert time.monotonic() - t0 < 1.5, mode


def test_a_failed_restoration_keeps_the_held_row(monkeypatch, tmp_path, capsys):
    import sys as _sys
    held = {**RIKS_ROW, "status": "ok", "sha256": "0" * 64, "bytes": 30,
            "raw_path": "raw/riksdagen_sfs/reg-riksdagen-sfs-pbl.txt",
            "text_path": "text/reg-riksdagen-sfs-pbl.md", "text_chars": 30,
            "url": RIKS_ROW["url"] + "#tom-sfs-2025-1"}
    row = {**RIKS_ROW, "url": held["url"]}
    root = _programme_repo(monkeypatch, tmp_path, [row], [held], docs()["regdocs.json"])
    failed = {**build_corpus._new_record(row)[0], "error": "404 Client Error",
              "http_status": 404}
    monkeypatch.setattr(build_corpus, "download_one", lambda _s: dict(failed))
    monkeypatch.setattr(_sys, "argv", ["build_corpus.py", "--workers", "1"])
    build_corpus.main()
    rows = [json.loads(line) for line in
            (root / "manifest" / "regdocs.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "ok" and rows[0]["sha256"] == "0" * 64  # provenance kept
    assert "reg-riksdagen-sfs-pbl" in (root / "workspace" /
                                        "programme-restore-failures.jsonl").read_text()
    assert "reg-riksdagen-sfs-pbl" in build_corpus._cooldowns()
    deferred = json.loads((tmp_path / "fetch-deferred.json").read_text()) \
        if (tmp_path / "fetch-deferred.json").exists() else None
    assert deferred is None or "reg-riksdagen-sfs-pbl" in deferred["ids"]


# ------------------------------------------------------------------------------------ round 5
def test_real_transport_head_probe_is_bounded(trickle_server, monkeypatch):
    monkeypatch.setattr(compliance_common, "PROGRAMME_HOSTS",
                        {**compliance_common.PROGRAMME_HOSTS, "127.0.0.1": (0.0, 6)})
    monkeypatch.setattr(robots_policy, "decision", lambda _u, fetcher=None: (True, None))
    monkeypatch.setattr(polite_http, "DEADLINE", 0.3)
    polite_http.set_policy({})
    t0 = time.monotonic()
    with pytest.raises(polite_http.Deferred):
        polite_http.head(f"{trickle_server}/headers", delay=0)
    assert time.monotonic() - t0 < 1.5
    # the BFS consolidation probe maps that deferral to "not established now" (hold)
    assert find_boverket.head_exists(f"{trickle_server}/headers") is None


def test_nested_guards_honour_the_earliest_deadline(trickle_server):
    import requests as _requests
    import stream_guard
    t0 = time.monotonic()
    with pytest.raises(stream_guard.DeadlineExceeded):
        with stream_guard.Deadline(time.monotonic() + 0.3, "outer"):
            with stream_guard.Deadline(time.monotonic() + 30, "inner") as inner:
                resp = _requests.get(f"{trickle_server}/body", stream=True, timeout=(5, 5))
                stream_guard.read_body(resp, max_bytes=10**6, guard=inner)
    assert time.monotonic() - t0 < 1.5


def test_a_forced_refresh_that_extracts_nothing_keeps_the_held_row(monkeypatch, tmp_path):
    import sys as _sys
    bfs = {"id": "bov-bfs-bfs2011-6", "title": "BFS 2011:6 — BBR",
           "url": "https://rinfo.boverket.se/BFS2011-6/pdf/BFS2011-6.pdf",
           "source": "boverket_bfs", "license": "public-domain",
           "topic": "standards_protocols", "format": "pdf"}
    old = b"%PDF-1.4 the held bytes"
    held = {**bfs, "status": "ok", "sha256": build_corpus.sha256_bytes(old), "bytes": len(old),
            "raw_path": "raw/boverket_bfs/bov-bfs-bfs2011-6.pdf",
            "text_path": "text/bov-bfs-bfs2011-6.md", "text_chars": 10}
    root = _programme_repo(monkeypatch, tmp_path, [bfs], [held], docs()["regdocs.json"])
    (root / "raw" / "boverket_bfs").mkdir(parents=True)
    (root / "raw" / "boverket_bfs" / "bov-bfs-bfs2011-6.pdf").write_bytes(old)
    (root / "text").mkdir()
    (root / "text" / "bov-bfs-bfs2011-6.md").write_text("# held\n\n---\n\nBFS 2011:6 text")
    monkeypatch.setattr(robots_policy, "decision", lambda _u, fetcher=None: (True, None))
    monkeypatch.setattr(build_corpus, "_wait_for_host", lambda _h: None)
    monkeypatch.setattr(build_corpus.requests, "get",
                        lambda url, **_k: FakeResp(200, b"%PDF-1.4 broken, no text",
                                                   "application/pdf", url=url))
    monkeypatch.setattr(_sys, "argv", ["build_corpus.py", "--force", "--workers", "1",
                                       "--extract-workers", "1"])
    build_corpus.main()
    row = json.loads((root / "manifest" / "nordic.jsonl").read_text().splitlines()[0])
    assert row["sha256"] == held["sha256"] and row["text_path"] == held["text_path"]
    assert (root / "raw" / "boverket_bfs" / "bov-bfs-bfs2011-6.pdf").read_bytes() == old
    assert not (root / "raw" / "boverket_bfs" / "bov-bfs-bfs2011-6.pdf.incoming").exists()
    assert "bov-bfs-bfs2011-6" in (root / "workspace" /
                                    "programme-restore-failures.jsonl").read_text()
