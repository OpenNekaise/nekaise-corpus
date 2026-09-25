"""Compliance/ESG programme (Codex decision 2026-09-25): access checks, rotation protocol,
identity, licence holding and the scoped quality profile. Recorded fixtures only, no network."""
from __future__ import annotations

import json
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
import polite_http
import prune_corpus
import quality
import robots_policy

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 25)


def keys(urls=(), ids=(), titles=()):
    return dedup.from_sets(set(urls), set(titles), set(ids))


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


@pytest.mark.parametrize(("status", "body", "outcome"), [
    (404, b"", True),
    (200, b"User-agent: *\nDisallow: /private/\n", True),
    (403, b"forbidden", "unavailable"),
    (503, b"", "unavailable"),
    (200, b"<html><title>Just a moment</title>challenge</html>", "unavailable"),
])
def test_robots_fetch_outcomes(status, body, outcome):
    robots_policy.clear_memory()
    fetch = lambda _url: (status, body)  # noqa: E731
    if outcome == "unavailable":
        with pytest.raises(robots_policy.RobotsUnavailable):
            robots_policy.decision("https://example.org/doc.pdf", fetch)
    else:
        assert robots_policy.decision("https://example.org/doc.pdf", fetch)[0] is outcome
    robots_policy.clear_memory()


# ------------------------------------------------------------------------------------ polite_http
class FakeResp:
    def __init__(self, status=200, body=b"", ctype="application/xml", location=None, url=""):
        self.status_code, self._body, self.url = status, body, url
        self.headers = {"content-type": ctype}
        if location:
            self.headers["location"] = location

    @property
    def content(self):
        return getattr(self, "_content", self._body)

    def iter_content(self, _n):
        for i in range(0, len(self._body), 4):
            yield self._body[i:i + 4]

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def web(monkeypatch):
    """polite_http against canned answers; robots allow everything unless a test says not."""
    calls, answers, robots = [], {}, {}
    monkeypatch.setattr(polite_http, "_pace", lambda *_a: None)

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
    polite_http.set_policy({})
    yield SimpleNamespace(calls=calls, answers=answers, robots=robots)
    polite_http.set_policy({})


def test_polite_http_refuses_suspended_and_robots_denied_hosts_without_requesting(web):
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


def test_polite_http_redirect_hops_are_rechecked(web):
    polite_http.set_policy({"fsb-tcfd.org": {"status": "suspended", "decided_at": "x"}})
    web.answers["https://ok.org/a"] = FakeResp(302, location="https://www.fsb-tcfd.org/b")
    with pytest.raises(polite_http.Refused):
        polite_http.get("https://ok.org/a")
    assert web.calls == ["https://ok.org/a"]  # the refused hop was never requested


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


@pytest.fixture
def no_probe_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(find_boverket, "_probe_memory_path", lambda: tmp_path / "probes.json")


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
        assert compliance_common.quality_profile(row, None) == "normative"
    with pytest.raises(ValueError):
        find_boverket.parse_feed("<html/>")


def test_bfs_run_hard_max_resume_and_watch(no_probe_memory):
    probes = []
    probe = lambda url: probes.append(url) or url.endswith("BFS2020-4_Konsolidering.pdf")  # noqa
    r = Report()
    out = find_boverket.run_bfs("START", 2, 8, keys(), r, TODAY, lambda: FEED, probe)
    # grund (1) fits; the amendment + its consolidation (2) would exceed --max 2: stop there
    assert [e["id"] for e in out] == ["bov-bfs-bfs2011-6"]
    assert r.value == ("next", "1")
    r = Report()
    out = find_boverket.run_bfs("1", 4, 8, keys(), r, TODAY, lambda: FEED, probe)
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


def test_bfs_feed_failure_and_probe_deferral_hold(no_probe_memory):
    def boom():
        raise polite_http.Deferred("503")
    r = Report()
    assert find_boverket.run_bfs("START", 10, 8, keys(), r, TODAY, boom) == []
    assert r.value[0] == "hold"
    r = Report()
    out = find_boverket.run_bfs("1", 10, 8, keys(), r, TODAY, lambda: FEED, lambda _u: None)
    assert out == [] and r.value[0] == "hold"  # nothing possible at the cursor: hold, not advance


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


def test_committed_programme_configs_are_valid_and_enabled_sources_appendable():
    sources = find_regdocs.load_config()
    for key in find_regdocs.runnable(sources):
        assert sources[key]["license"] in compliance_common.CURRENT_LICENSES, key
    assert all(not c.get("enabled") for c in sources.values() if c.get("blocked"))
    assert find_eurlex.validate(find_eurlex.load_config()) == []
    esef = find_esef.load_config()
    backends = json.loads((REPO / "registry" / "backends.json").read_text())
    assert esef["license"] not in compliance_common.CURRENT_LICENSES
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


YM_PAGE = b"""<html><body>
<a href="https://finlex.fi/fi/lainsaadanto/2023/751?language=fin"><img/></a>
<a href="https://finlex.fi/fi/lainsaadanto/2023/751?language=fin">Rakentamislaki 751/2023 - FINLEX \xc2\xae</a>
<a href="https://finlex.fi/fi/lainsaadanto/saadoskokoelma/2017/1007">Ymp\xc3\xa4rist\xc3\xb6ministeri\xc3\xb6n asetus rakennusten paloturvallisuudesta hyvin pitk\xc3\xa4 otsikko joka jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu ja jatkuu loppuun | FINLEX</a>
<a href="https://example.org/other">Other</a>
</body></html>"""


def finlex_cfg():
    return find_regdocs.load_config()["finlex-building-decrees"]


def test_link_pages_rewrites_keep_best_label_and_language_suffix():
    cfg = finlex_cfg()
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
    assert long_fi["title"] != long_sv["title"]  # truncation never collapses the variants
    assert len(out) == 4 and all(e["license"] == "cc-by" for e in out)
    assert all(compliance_common.quality_profile(e, find_regdocs_doc()) == "normative" for e in out)


def find_regdocs_doc():
    return json.loads((REPO / "registry" / "regdocs.json").read_text())


SITEMAP = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.boverket.se/sv/PBL-kunskapsbanken/regler-om-byggande/brandskydd/</loc></url>
<url><loc>https://www.boverket.se/sv/PBL-kunskapsbanken/tillganglighetsredogorelse-x/</loc></url>
<url><loc>https://www.boverket.se/sv/om-boverket/jobb/</loc></url>
<url><loc>https://www.boverket.se/sv/byggande/tillganglighet/</loc></url></urlset>"""


def test_sitemap_pages_scope_and_titles():
    cfg = find_regdocs.load_config()["boverket-web"]
    out = find_regdocs.universe("boverket-web", cfg, find_regdocs.Budget(2), "2026-09-25",
                                fetch=lambda _u, _e: SITEMAP)
    urls = [e["url"] for e in out]
    assert urls == ["https://www.boverket.se/sv/PBL-kunskapsbanken/regler-om-byggande/brandskydd/",
                    "https://www.boverket.se/sv/byggande/tillganglighet/"]
    assert out[0]["topic"] == "architecture" and out[0]["license"] == "unverified"
    assert compliance_common.quality_profile(out[0], find_regdocs_doc()) is None
    ok, held = compliance_common.split_appendable(out)
    assert ok == [] and len(held) == 2  # never appended before the collect-all split


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
    out = find_regdocs.run(r.value[1] if r.value else json.dumps(cur), 5, 8, sources,
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


def test_regdocs_changed_universe_restarts_and_deferral_holds():
    sources = static_sources()
    cur = json.dumps({"s": "one", "o": 2, "f": "stale", "w": {}})
    r = Report()
    out = find_regdocs.run(cur, 5, 8, sources, keys(), r, TODAY)
    assert out[0]["url"] == "https://a.org/0.pdf"  # fingerprint changed: restart at 0
    link = {"lp": src(mechanism="link_pages", seeds=["https://s.org/"], link_include="x",
                      items=None)}

    def refuse(_u, _e):
        raise polite_http.Deferred("robots unavailable")
    r = Report()
    assert find_regdocs.run("START", 5, 8, link, keys(), r, TODAY, fetch=refuse) == []
    assert r.value[0] == "hold"


# ------------------------------------------------------------------------------------ Cellar
def row(lang, mtype, item, title="Directive"):
    return {"lang": f"http://publications.europa.eu/resource/authority/language/{lang}",
            "mtype": mtype, "item": item, "title": title}


CELLAR = "http://publications.europa.eu/resource/cellar/u.00{}.0{}/DOC_{}"


def test_cellar_item_selection_prefers_xhtml_keeps_parts_and_falls_back_to_pdf():
    rows = [row("SWE", "xhtml", CELLAR.format(24, 3, 1)), row("SWE", "xhtml", CELLAR.format(24, 3, 2)),
            row("SWE", "pdfa2a", CELLAR.format(24, 1, 1)), row("SWE", "fmx4", CELLAR.format(24, 2, 1)),
            row("ELL", "pdfa1a", CELLAR.format(5, 1, 1)), row("XXX", "xhtml", "http://x/DOC_1")]
    items = find_eurlex.select_items(rows, ["sv", "el"])
    assert [(i["lang"], i["mtype"], i["part"], i["parts"]) for i in items] == [
        ("el", "pdfa1a", 1, 1), ("sv", "xhtml", 1, 2), ("sv", "xhtml", 2, 2)]
    seed = {"celex": "32024L1275", "name": "EPBD", "topic": "building_energy"}
    entries = [find_eurlex.entry_for(seed, "32024L1275", i, "2026-09-25") for i in items]
    assert [e["id"] for e in entries] == ["eur-32024l1275-el", "eur-32024l1275-sv-p1",
                                          "eur-32024l1275-sv-p2"]
    assert entries[0]["format"] == "pdf" and entries[1]["format"] == "html"
    assert entries[1]["url"].startswith("https://publications.europa.eu/resource/cellar/")
    assert len({e["title"] for e in entries}) == 3
    assert all(e["license"] == "open" for e in entries)
    cons = find_eurlex.entry_for(seed, "02024L1275-20250101", items[0], "2026-09-25")
    assert cons["license"] == "cc-by" and cons["document_type"] == "consolidated-act"
    corr = find_eurlex.entry_for(seed, "32024L1275R(04)", items[0], "2026-09-25")
    assert corr["document_type"] == "corrigendum" and corr["id"] == "eur-32024l1275r-04-el"
    for e in (*entries, cons, corr):
        assert compliance_common.quality_profile(e, None) == "normative"
    spoof = {**entries[0], "persistent_id": "celex:32099L0001"}
    assert compliance_common.quality_profile(spoof, None) is None
    assert compliance_common.quality_profile({**entries[0], "url": "https://evil.org/x"},
                                             None) is None


def test_cellar_rejects_injection_in_celex():
    with pytest.raises(ValueError):
        find_eurlex.items_query('32024L1275" } DROP')


def fake_cellar(works):
    """query() answering related/items SPARQL from {celex: [(lang, item)]} and relations."""
    calls = []

    def query(q):
        calls.append(q)
        if "?rel" in q:
            seed = q.split('resource_legal_id_celex "')[1].split('"')[0]
            return [{"rel": "amends", "celex": c} for c in works.get(("rel", seed), [])]
        celex = q.split('resource_legal_id_celex "')[1].split('"')[0]
        return [row(l, "xhtml", f"http://publications.europa.eu/resource/cellar/{celex}.{l}/DOC_1")
                for l in works.get(celex, [])]
    return query, calls


def test_cellar_rotation_caps_resume_and_watch(monkeypatch):
    works = {"S1": ["SWE", "ENG", "FIN"], ("rel", "S1"): ["A1"], "A1": ["SWE"], "S2": ["ENG"]}
    query, calls = fake_cellar(works)
    cfg = {"seeds": [{"celex": "S1", "name": "one", "topic": "urban"},
                     {"celex": "S2", "name": "two", "topic": "urban"}],
           "languages": ["sv", "en", "fi"], "expand": ["amends"], "rights_reviewed_at": "2026-09-25"}
    # short synthetic CELEX-like identifiers keep the fixture readable
    monkeypatch.setattr(find_eurlex, "CELEX_RE", __import__("re").compile(r"[A-Z0-9()\-]{2,40}"))
    r = Report()
    out = find_eurlex.run("START", 2, 5, 10, cfg, keys(), r, TODAY, query)
    assert [e["language"] for e in out] == ["en", "fi"]
    cur = json.loads(r.value[1])
    assert cur == {"s": 0, "k": "S1", "i": 2}
    r = Report()
    out = find_eurlex.run(json.dumps(cur), 10, 5, 10, cfg, keys(), r, TODAY, query)
    assert [(e["id"]) for e in out] == ["eur-s1-sv", "eur-a1-sv", "eur-s2-en"]
    assert r.value == ("next", "watch:2026-09-25")
    r = Report()
    n = len(calls)
    assert find_eurlex.run("watch:2026-09-20", 10, 5, 10, cfg, keys(), r, TODAY, query) == []
    assert len(calls) == n and r.value == ("next", "watch:2026-09-20")


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


def filing(period, report, fxo, date_added="2025-01-01"):
    return {"attributes": {"period_end": period, "report_url": report, "fxo_id": fxo,
                           "date_added": date_added}}


def esef_cfg():
    return {"issuers": [ISSUER], "license": "proprietary", "license_evidence": "issuer copyright",
            "rights_reviewed_at": "2026-09-25"}


def test_esef_annual_classification_languages_and_relative_urls():
    filings = [
        filing("2024-09-30", "/L/2024-09-30/ESEF/DK/0/aarsleff-2024-09-30-da/reports/a-da.xhtml", "F0"),
        filing("2024-09-30", "/L/2024-09-30/ESEF/DK/0/aarsleff-2024-09-30-en/reports/a-en.xhtml", "F0"),
        filing("2024-12-31", "/L/2024-12-31/ESEF/DK/0/q/reports/q.xhtml", "Q1"),  # interim
        filing("2023-09-30", None, "F9"),  # no report
    ]
    out = find_esef.annual_entries(ISSUER, filings, esef_cfg())
    assert [e["url"] for e in out] == [
        "https://filings.xbrl.org/L/2024-09-30/ESEF/DK/0/aarsleff-2024-09-30-da/reports/a-da.xhtml",
        "https://filings.xbrl.org/L/2024-09-30/ESEF/DK/0/aarsleff-2024-09-30-en/reports/a-en.xhtml"]
    assert [e["language"] for e in out] == ["da", "en"]
    assert len({e["title"] for e in out}) == 2 and len({e["id"] for e in out}) == 2
    ok, held = compliance_common.split_appendable(out)
    assert ok == [] and len(held) == 2


def test_esef_pagination_hard_max_and_watch():
    pages = {
        find_esef.first_page_url(ISSUER["lei"]): {
            "data": [filing("2023-09-30", "/r/2023-da.xhtml", "A"),
                     filing("2024-09-30", "/r/2024-da.xhtml", "B")],
            "links": {"next": "/api/entities/X/filings?page%5Bnumber%5D=2"}},
        "https://filings.xbrl.org/api/entities/X/filings?page%5Bnumber%5D=2": {
            "data": [filing("2025-09-30", "/r/2025-da.xhtml", "C")], "links": {}},
    }
    seen = []
    fetch = lambda url: seen.append(url) or pages[url]  # noqa: E731
    r = Report()
    out = find_esef.run("START", 1, 1, 2, 4, esef_cfg(), keys(), r, TODAY, fetch)
    assert len(out) == 1 and json.loads(r.value[1])["p"] == find_esef.first_page_url(ISSUER["lei"])
    r2 = Report()
    out2 = find_esef.run(r.value[1], 5, 1, 2, 4, esef_cfg(),
                         keys(urls=[out[0]["url"]]), r2, TODAY, fetch)
    assert [e["url"].rsplit("/", 1)[-1] for e in out2] == ["2024-da.xhtml", "2025-da.xhtml"]
    assert r2.value == ("next", "watch:2026-09-25")


# ------------------------------------------------------------------------------------ gate
GREEK = ("Οδηγία για την ενεργειακή απόδοση των κτιρίων και τις απαιτήσεις του κράτους μέλους "
         * 60)
SV_LEGAL = ("Företaget ska lämna upplysningar om väsentliga konsekvenser, risker och möjligheter "
            "samt om styrning av hållbarhetsfrågor enligt direktivet. " * 40)


def test_normative_profile_keeps_eu_languages_the_generic_gate_drops():
    for text in (GREEK, SV_LEGAL):
        m = quality.metrics(text)
        assert quality.verdict(m, False) != "ok"
        assert quality.verdict_for(m, False, "normative") == "ok"
    assert quality.verdict_for(quality.metrics("§ 1 Kort."), False, "normative") == "thin"
    assert quality.verdict_for(quality.metrics("1.0 2.0 3.0 " * 200), False, "normative") != "ok"
    with pytest.raises(ValueError):
        quality.verdict_for(quality.metrics(GREEK), False, "lenient")


def test_prune_uses_the_profile_only_for_verified_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_corpus, "HERE", tmp_path)
    monkeypatch.setattr(prune_corpus, "ACCESS", None)
    (tmp_path / "text").mkdir()
    rows = []
    for sid, persistent, url in (
            ("eur-32024l1275-el", "celex:32024L1275",
             "https://publications.europa.eu/resource/cellar/u.0005.03/DOC_1"),
            ("eur-32024l1275-bg", "celex:32024L1276",  # spoofed identity
             "https://publications.europa.eu/resource/cellar/u.0001.03/DOC_1")):
        (tmp_path / "text" / f"{sid}.md").write_text(GREEK)
        rows.append({"id": sid, "title": sid, "url": url, "source": "eurlex", "license": "open",
                     "topic": "building_energy", "format": "html", "status": "ok",
                     "persistent_id": persistent, "text_path": f"text/{sid}.md",
                     "quality": quality.metrics(GREEK)})
    plan = prune_corpus.decide(rows, {}, {}, set(), None)
    assert "eur-32024l1275-el" not in plan.drop
    assert plan.drop["eur-32024l1275-bg"] == "thin"


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
    monkeypatch.setattr(build_corpus, "_wait_for_host", lambda _h: None)
    monkeypatch.setattr(build_corpus.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("programme rows never use curl"))
    state = SimpleNamespace(calls=calls, robots={}, answers={})

    def decision(url, fetcher=None):
        v = state.robots.get(url.split("/")[2], (True, None))
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(robots_policy, "decision", decision)

    def get(url, **kw):
        calls.append((url, kw.get("stream")))
        return state.answers[url]
    monkeypatch.setattr(build_corpus.requests, "get", get)
    return state


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


def test_loader_streams_with_a_byte_cap_and_crawl_delay(loader, monkeypatch):
    url = programme_row()["url"]
    loader.robots["data.riksdagen.se"] = (True, 9.0)
    loader.answers[url] = FakeResp(200, b"Plan- och bygglag " * 10, "text/plain", url=url)
    rec = build_corpus.download_one(programme_row())
    assert rec["raw_path"] and loader.calls == [(url, True)], rec.get("error")
    assert build_corpus.HOST_DELAY["data.riksdagen.se"] >= 9.0
    monkeypatch.setattr(build_corpus, "PROGRAMME_MAX_BYTES", 20)
    rec = build_corpus.download_one(programme_row())
    assert rec["error"].startswith("too-large") and not rec.get("raw_path")


def test_loader_programme_refusal_is_transient_and_never_retried_with_curl(loader):
    url = programme_row()["url"]
    loader.answers[url] = FakeResp(403, b"no", "text/html", url=url)
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
    kept, _ = build_corpus.cap_per_host(srcs, restoring)
    assert "esf-5" in [s["id"] for s in kept]  # restoration is never capped


@pytest.mark.parametrize(("body", "fmt", "want"), [
    (b"<html><title>Just a moment...</title>", "html", True),
    (b"<html><head><script src='/cdn-cgi/challenge-platform/x'>", "html", True),
    (b"<html><p>The challenge of accessibility in buildings</p>", "html", False),
    (b"%PDF-1.7", "pdf", False),
])
def test_html_challenge_detection(body, fmt, want):
    assert build_corpus.is_challenge(200, body, fmt) is want


# ------------------------------------------------------------------------------------ contracts
def test_programme_config_contracts():
    docs = {"regdocs.json": find_regdocs_doc(), "eurlex.json": find_eurlex.load_config(),
            "esef.json": find_esef.load_config()}
    backends = {"find_regdocs": {}, "find_eurlex": {}, "find_esef": {"enabled": False}}
    assert check_contracts.programme_config_errors(docs, backends) == []
    errs = check_contracts.programme_config_errors(docs, {**backends, "find_esef": {"enabled": True}})
    assert any("awaits the collect-all" in e for e in errs)
    errs = check_contracts.programme_config_errors({**docs, "eurlex.json": None}, backends)
    assert any("missing" in e for e in errs)
    bad = json.loads(json.dumps(docs["regdocs.json"]))
    bad["sources"]["efrag-esrs"]["enabled"] = True
    assert check_contracts.programme_config_errors({**docs, "regdocs.json": bad}, backends)
