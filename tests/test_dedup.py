"""Finder membership through the store (ADR 0001 stage 3, step 3).

Every finder used to dedup against registry.existing_keys()' whole in-memory sets; it now asks
dedup.Keys, which sends candidate batches to the store's known(). These tests pin that the two are
the same decision: identical proposals, ids (-2/-3 suffixes, manifest-only collisions, intra-batch
repeats), request counts and rotation signals, for the legacy set path and the store path, with
the SQLite index and without it.
"""
import ast
import json
import sys
from pathlib import Path

import pytest
import yaml

import blocklist
import crawl_docs
import dedup
import find_github
import find_ibpsa
import find_osti
import find_scielo
import registry
import run_round
import store
from store import Prefix, Table
from test_find_ibpsa import LISTING, _response

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
IBPSA = "https://publications.ibpsa.org/proceedings/bs/2025/papers"


def entry(sid, title, url, **extra):
    return {"id": sid, "title": title, "url": url, "source": "test", "license": "open",
            "topic": "construction", "format": "pdf", **extra}


# Registry entries, manifest-only rows (no registry entry: e.g. pruned docs whose ids must still
# never be reused) and blocklisted URLs that every scenario below runs into.
ENTRIES = {
    "curated.yaml": [entry("hand-x", "fresh: doc/known.tex", "https://h.org/x.pdf"),
                     entry("hand-y", "Known Scielo Title", "https://h.org/y.pdf")],
    "reports.yaml": [entry("ost-known-title", "Known Title", "https://www.osti.gov/servlets/purl/1")],
    "scielo.yaml": [entry("sci-s1", "Scielo one", "https://www.scielo.br/pdf/ac/v1/s1.pdf")],
    "crawl.yaml": [entry("crawl-mosaik_docs-known-html", "known", "https://m.io/en/latest/known.html")],
    "github.yaml": [entry("gh-done-readme", "done: README.md",
                          "https://raw.githubusercontent.com/o/done/main/README.md",
                          source="gh_done", format="md")],
}
MANIFEST_ONLY = {
    "reports": [entry("ost-heat-pump-study", "Something else", "https://e.org/m1.pdf")],
    "scielo": [entry("sci-s2", "Scielo two", "https://e.org/m2.pdf")],
    "ibpsa": [entry("ibp-model-predictive-control-of-a-heat-pump", "Other paper",
                    "https://e.org/m3.pdf")],
    "crawl": [entry("crawl-mosaik_docs-api-html", "api", "https://e.org/m4.html")],
    "github": [entry("gh-fresh-doc-a", "other", "https://e.org/m5.md")],
}
BLOCKED = ["https://www.osti.gov/servlets/purl/2", f"{IBPSA}/bs2025_3.pdf/",
           "https://www.scielo.br/pdf/ac/v1/s5.pdf", "https://m.io/en/latest/blocked.html",
           "https://raw.githubusercontent.com/o/fresh/main/doc/blocked.tex"]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "registry").mkdir(parents=True)
    (root / "manifest").mkdir()
    for name, rows in ENTRIES.items():
        (root / "registry" / name).write_text(yaml.safe_dump({"sources": rows}))
    for stem, rows in MANIFEST_ONLY.items():
        (root / "manifest" / f"{stem}.jsonl").write_text(
            "".join(json.dumps({**r, "status": "ok"}) + "\n" for r in rows))
    (root / "pruned_urls.txt").write_text("".join(u + "\n" for u in BLOCKED))
    # the legacy path reads these module globals; the store path reads the root
    monkeypatch.setattr(registry, "REG_DIR", root / "registry")
    monkeypatch.setattr(registry, "MAN_DIR", root / "manifest")
    monkeypatch.setattr(blocklist, "PATH", root / "pruned_urls.txt")
    monkeypatch.setattr(dedup, "_default_root", lambda: root)
    return root


class LegacyKeys:
    """Exactly the pre-conversion finder code: existing_keys() sets + registry.uniquify_ids."""

    def __init__(self):
        self.urls, self.titles, self.ids = registry.existing_keys()

    def prefetch(self, **_candidates):
        pass

    def uniquify_ids(self, entries):
        registry.uniquify_ids(entries, self.ids)


MODES = ["legacy-index", "legacy-canonical", "store-index", "store-canonical"]


def use_mode(monkeypatch, mode):
    if mode.endswith("canonical"):
        monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    else:
        monkeypatch.delenv("NEKAISE_DISABLE_INDEX", raising=False)
    if mode.startswith("legacy"):
        monkeypatch.setattr(dedup, "open_keys", LegacyKeys)
    else:
        monkeypatch.setattr(dedup, "open_keys", REAL_OPEN_KEYS)


REAL_OPEN_KEYS = dedup.open_keys


# --- finder scenarios: each returns everything observable about one run -------------------------

def run_osti(monkeypatch, tmp_path, capsys):
    purl = "https://www.osti.gov/servlets/purl"
    hits = [("Known Title!", f"{purl}/100"),        # title known (normalized)
            ("New A", f"{purl}/1/"),                # url known (trailing slash)
            ("New B", f"{purl}/2"),                 # blocklisted
            ("Heat Pump Study", f"{purl}/3"),       # id collides with a manifest-only id
            ("Heat pump study", f"{purl}/4"),       # same slug again: intra-batch id repeat
            ("Other", f"{purl}/3"),                 # intra-batch url repeat
            ("—", f"{purl}/5"), ("?", f"{purl}/6")]  # titles that normalize to ""
    requests_seen = []

    def from_osti(term, rows, page):
        requests_seen.append((term, page))
        return hits if page == 1 else []

    monkeypatch.setattr(find_osti, "QUERIES", [("heat", "building_energy")])
    monkeypatch.setattr(find_osti, "from_osti", from_osti)
    monkeypatch.setattr(find_osti.time, "sleep", lambda _s: None)
    appended = []
    monkeypatch.setattr(find_osti.registry, "append_entries", appended.extend)
    monkeypatch.setattr(sys, "argv", ["find_osti.py", "--pages", "1", "--append"])
    find_osti.main()
    return {"appended": appended, "requests": requests_seen, "out": capsys.readouterr().out}


def run_ibpsa(monkeypatch, tmp_path, capsys, extra=()):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _response(200, LISTING)

    monkeypatch.setattr(find_ibpsa.requests, "get", get)
    appended = []
    monkeypatch.setattr(find_ibpsa.registry, "append_entries", appended.extend)
    signals = tmp_path / f"signals-{len(list(tmp_path.glob('signals-*')))}"
    signals.mkdir()
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(signals / "hold"))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(signals / "exhausted"))
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", "2", *extra, "--append"])
    find_ibpsa.main()
    return {"appended": appended, "requests": calls, "out": capsys.readouterr().out,
            "signals": {p.name: p.read_text() for p in sorted(signals.iterdir())}}


def run_ibpsa_capped(monkeypatch, tmp_path, capsys):
    return run_ibpsa(monkeypatch, tmp_path, capsys, ("--max", "1"))


def run_scielo(monkeypatch, tmp_path, capsys):
    requests_seen = []
    titles = {"S1": "Scielo one", "S2": "Scielo two", "S3": "Concreto armado",
              "S4": "Known Scielo Title", "S5": "Bloqueado"}

    def get(path, **params):
        requests_seen.append((path, params.get("code")))
        if path == "article/identifiers/":
            # S1 is registered, S2 only in the manifest: both skipped WITHOUT a metadata request;
            # S3 twice (the second is a url repeat), S4 has a known title, S5 is blocklisted
            return {"objects": [{"code": c} for c in ("S1", "S2", "S3", "S3", "S4", "S5")]}
        code = params["code"]
        return {"article": {"v12": [{"_": titles[code], "l": "pt"}], "v40": [{"_": "pt"}]},
                "fulltexts": {"pdf": {"pt": f"https://www.scielo.br/pdf/ac/v1/{code.lower()}.pdf"}}}

    monkeypatch.setattr(find_scielo, "_get", get)
    monkeypatch.setattr(find_scielo, "JOURNALS", [("scl", "1678-8621", "construction")])
    monkeypatch.setattr(find_scielo.time, "sleep", lambda _s: None)
    appended = []
    monkeypatch.setattr(find_scielo.registry, "append_entries", appended.extend)
    monkeypatch.setattr(sys, "argv", ["find_scielo.py", "--append"])
    find_scielo.main()
    return {"appended": appended, "requests": requests_seen, "out": capsys.readouterr().out}


def run_crawl_docs(monkeypatch, tmp_path, capsys):
    base = "https://m.io/en/latest"
    monkeypatch.setattr(crawl_docs.registry, "load_eligibility", lambda: {})
    monkeypatch.setattr(crawl_docs, "crawl", lambda *_a: [
        f"{base}/known.html/", f"{base}/blocked.html", f"{base}/api.html", f"{base}/api.html"])
    appended = []
    monkeypatch.setattr(crawl_docs.registry, "append_entries", appended.extend)
    monkeypatch.setattr(sys, "argv", [
        "crawl_docs.py", "--seed", f"{base}/", "--prefix", "/en/latest/", "--source",
        "mosaik_docs", "--topic", "controls_bas", "--append"])
    crawl_docs.main()
    return {"appended": appended, "out": capsys.readouterr().out}


def run_github(monkeypatch, tmp_path, capsys):
    raw = "https://raw.githubusercontent.com/o/fresh/main"
    monkeypatch.setattr(find_github, "REPOS", [
        {"repo": "o/done", "license": "open", "topic": "urban"},
        # its blocklisted .tex proves an earlier walk; the requested man-page pass is still due
        {"repo": "o/fresh", "license": "open", "topic": "urban", "docs": ["tex", "man"],
         "include": ["doc/"]}])
    passes = tmp_path / f"passes-{len(list(tmp_path.glob('passes-*')))}.json"
    monkeypatch.setattr(find_github, "PASSES", passes)
    walked = []

    def from_repo(spec):
        walked.append(spec["repo"])
        hit = {"source": "gh_fresh", "license": "open", "topic": "urban", "format": "tex"}
        return [{**hit, "id": "gh-fresh-doc-blocked", "title": "fresh: doc/blocked.tex",
                 "url": f"{raw}/doc/blocked.tex"},
                {**hit, "id": "gh-fresh-doc-known", "title": "fresh: doc/known.tex",
                 "url": f"{raw}/doc/known.tex"},
                {**hit, "id": "gh-fresh-doc-a", "title": "fresh: doc/a.tex",
                 "url": f"{raw}/doc/a.tex"},
                {**hit, "id": "gh-fresh-doc-a", "title": "fresh: doc/a copy.tex",
                 "url": f"{raw}/doc/a.tex/"}]

    monkeypatch.setattr(find_github, "from_repo", from_repo)
    appended = []
    monkeypatch.setattr(find_github.registry, "append_entries", appended.extend)
    monkeypatch.setattr(sys, "argv", ["find_github.py", "--append"])
    find_github.main()
    return {"appended": appended, "walked": walked, "out": capsys.readouterr().out,
            "passes": find_github.load_passes(passes)}


SCENARIOS = [run_osti, run_ibpsa, run_ibpsa_capped, run_scielo, run_crawl_docs, run_github]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_legacy_and_store_membership_give_identical_runs(scenario, corpus, monkeypatch, tmp_path,
                                                         capsys):
    results = {}
    for mode in MODES:
        use_mode(monkeypatch, mode)
        results[mode] = scenario(monkeypatch, tmp_path, capsys)
    for mode in MODES[1:]:
        assert results[mode] == results[MODES[0]], mode
    got = results["store-index"]
    ids = [e["id"] for e in got["appended"]]
    if scenario is run_osti:
        # manifest-only collision -> -2, the same slug again in the batch -> -3
        assert ids == ["ost-heat-pump-study-2", "ost-heat-pump-study-3", "ost-", "ost--2"]
        assert [e["url"].rsplit("/", 1)[1] for e in got["appended"]] == ["3", "4", "5", "6"]
    elif scenario is run_ibpsa:
        assert ids == ["ibp-model-predictive-control-of-a-heat-pump-2",
                       "ibp-daylight-simulation-of-atria"]
        assert got["signals"] == {}
    elif scenario is run_ibpsa_capped:
        assert ids == ["ibp-model-predictive-control-of-a-heat-pump-2"]
        assert "has 2 new papers" in got["signals"]["hold"]
    elif scenario is run_scielo:
        assert ids == ["sci-s3"]
        # no metadata request for the registered or the manifest-only article
        assert [c for p, c in got["requests"] if p == "article/"] == ["S3", "S3", "S4", "S5"]
    elif scenario is run_crawl_docs:
        assert ids == ["crawl-mosaik_docs-api-html-2", "crawl-mosaik_docs-api-html-3"]
    elif scenario is run_github:
        assert got["walked"] == ["o/fresh"]  # o/done is ingested (manifest gh- row)
        assert ids == ["gh-fresh-doc-a-2"]
        today = find_github.date.today().isoformat()
        assert got["passes"] == {"gh_fresh": {"man": today, "tex": today}}


# --- membership itself -----------------------------------------------------------------------------

def candidates():
    urls, titles, ids = [], [], []
    for rows in list(ENTRIES.values()) + list(MANIFEST_ONLY.values()):
        for r in rows:
            urls += [r["url"], r["url"] + "/", " " + r["url"], r["url"].upper()]
            titles += [r["title"], registry.norm(r["title"]), registry.norm(r["title"]) + " x"]
            ids += [r["id"], r["id"] + "-2", r["id"].upper()]
    for u in BLOCKED:
        urls += [u, u.rstrip("/"), u + "/"]
    return urls + ["", "https://new.example/x"], titles + ["", "unknown title"], ids + ["", "new"]


@pytest.mark.parametrize("index", [True, False], ids=["index", "canonical"])
def test_keys_membership_equals_legacy_sets(corpus, monkeypatch, index):
    if not index:
        monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    urls, titles, ids = candidates()
    monkeypatch.delenv("NEKAISE_DISABLE_INDEX", raising=False)
    legacy = registry.existing_keys()  # the production (indexed) legacy sets
    if not index:
        monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    keys = dedup.open_keys(corpus)
    keys.prefetch(urls=urls[::2], titles=titles[::2])  # half prefetched, half looked up lazily
    local = ("https://new.example/x", "unknown title", "new")
    for known, values, add in zip((keys.urls, keys.titles, keys.ids), (urls, titles, ids), local):
        known.add(add)
    for kind, known, values, sets, add in zip(("url", "title", "id"),
                                              (keys.urls, keys.titles, keys.ids),
                                              (urls, titles, ids), legacy, local):
        expected = set(sets) | {add}
        assert [v in known for v in values] == [v in expected for v in values], kind


def test_indexed_and_canonical_known_agree_on_finder_inputs(corpus, monkeypatch):
    urls, titles, ids = candidates()
    st = store.FileStore(corpus)
    answers = []
    for disable in ("0", "1"):
        monkeypatch.setenv("NEKAISE_DISABLE_INDEX", disable)
        with st.read() as view:
            answers.append(view.known(urls=urls, titles=titles, ids=ids))
    assert answers[0] == answers[1]
    assert (corpus / "workspace" / "corpus-index.sqlite3").exists()  # the index did answer once
    assert "https://www.osti.gov/servlets/purl/2" in answers[0].urls        # blocklist
    assert "ost-heat-pump-study" in answers[0].ids                          # manifest-only


def test_prefetch_batches_and_lazy_lookups_count_round_trips(corpus):
    keys = dedup.open_keys(corpus)
    page = [f"https://www.osti.gov/servlets/purl/{i}" for i in range(store.MAX_KNOWN + 5)]
    keys.prefetch(urls=page, titles=["known title"])
    assert keys.lookups == 1                    # one view, MAX_KNOWN-sized known() chunks
    assert page[1] in keys.urls and page[2] in keys.urls and page[3] not in keys.urls
    assert "known title" in keys.titles
    assert keys.lookups == 1                    # all answered from the batch
    assert "Known Title" not in keys.titles     # not normalized: never equal to a stored key
    assert keys.lookups == 1                    # ...so it needs no round trip
    assert "never prefetched" not in keys.titles
    assert keys.lookups == 2                    # a single lazy lookup


def test_uniquify_matches_registry_uniquify_ids(corpus):
    batch = [{"id": "ost-heat-pump-study"}, {"id": "ost-heat-pump-study"},
             {"id": "ost-known-title"}, {"id": "x" * 60}, {"id": "x" * 60}, {"id": "fresh"}]
    legacy = [dict(e) for e in batch]
    registry.uniquify_ids(legacy, registry.existing_keys()[2])
    keys = dedup.open_keys(corpus)
    ours = [dict(e) for e in batch]
    keys.uniquify_ids(ours)
    assert ours == legacy
    assert [e["id"] for e in ours][:3] == ["ost-heat-pump-study-2", "ost-heat-pump-study-3",
                                           "ost-known-title-2"]
    later = [{"id": "fresh"}]
    keys.uniquify_ids(later)                    # reservations of this run persist
    assert later == [{"id": "fresh-2"}]


def test_standalone_lookup_fails_clearly_while_a_round_holds_the_lock(corpus, monkeypatch):
    monkeypatch.setenv(dedup.LOCK_TIMEOUT_ENV, "0")
    st = store.FileStore(corpus)
    with st.writer():
        keys = dedup.open_keys(corpus)
        with pytest.raises(dedup.DedupUnavailable, match="retry when no round is running"):
            _ = "https://x.org/a" in keys.urls


# --- filtered scans used by finders ----------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["gh-", "gh-fresh", "vnd-", "ost-", "sci-", "o", "pat-", "hand-"])
@pytest.mark.parametrize("table", [Table.ENTRIES, Table.MANIFEST], ids=["entries", "manifest"])
def test_id_prefix_scan_pushdown_equals_the_full_scan(corpus, prefix, table):
    reg, man = corpus / "registry", corpus / "manifest"
    vendors = [entry(f"vnd-acme-{i}", f"Acme {i}", f"https://acme.example/{i}.pdf")
               for i in range(12)]
    for e in vendors:
        path = reg / registry.shard_filename(e["id"])
        rows = yaml.safe_load(path.read_text())["sources"] if path.exists() else []
        path.write_text(yaml.safe_dump({"sources": rows + [e]}))
        with (man / f"{registry.manifest_shard(e['id'])}.jsonl").open("a") as f:
            f.write(json.dumps(e) + "\n")
    (reg / "patents-us.yaml").write_text(yaml.safe_dump({"sources": [
        entry("pat-us123", "Patent", "https://patents.google.com/patent/US123/en")]}))
    st = store.FileStore(corpus)
    where = Prefix("id", prefix)
    with st.read() as pushed:
        got = list(dedup.scan_all(pushed, table, where=where, fields=("id", "source"), page=3))
        pinned = st._routed_files(table, prefix) is not None
        assert pinned == (prefix in ("gh-", "gh-fresh", "vnd-", "ost-", "sci-"))
        loaded = ("entries" if table is Table.ENTRIES else "manifest") in pushed._loaded_tables
        assert loaded != pinned  # pinned scans never load the whole table
    with st.read() as full:
        full.scan(table, limit=1)  # loads the whole table: no pushdown
        want = list(dedup.scan_all(full, table, where=where, fields=("id", "source"), page=3))
    assert got == want
    if prefix in ("vnd-", "ost-", "sci-", "o"):
        assert len(got) >= 1 + 11 * (prefix == "vnd-")


def test_wiki_origin_titles_come_from_a_filtered_scan(corpus):
    (corpus / "registry" / "curated.yaml").write_text(yaml.safe_dump({"sources": [
        entry("wiki-b", "B", "https://en.wikipedia.org/wiki/Building_automation",
              source="wikipedia", topic="controls_bas"),
        entry("wiki-a", "A", "https://en.wikipedia.org/wiki/HVAC%2FR", source="wikipedia"),
        entry("wiki-c", "C", "https://example.org/no-wiki-path", source="wikipedia"),
        entry("hand-z", "Z", "https://en.wikipedia.org/wiki/Other", source="other"),
    ]}))
    import find_wiki
    assert find_wiki.origin_titles() == {"HVAC/R": "construction",
                                         "Building_automation": "controls_bas"}


# --- github passes: staged in proposal mode, written by run_round through the store -----------------

def test_merge_applies_staged_passes_only_from_successful_finders(tmp_path, monkeypatch):
    monkeypatch.setattr(run_round.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(run_round.registry, "append_entries", lambda _e: {})
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"entries": [], "github_passes": {"gh_a": {"tex": "2026-09-24"}}}))
    applied = []
    run_round.merge_proposals([{"index": 0, "name": "find_github", "proposal": ok}],
                              applied.append)
    assert applied == [{"gh_a": {"tex": "2026-09-24"}}]
    with pytest.raises(RuntimeError, match="no store writer"):
        run_round.merge_proposals([{"index": 0, "name": "find_github", "proposal": ok}])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"entries": [], "surprise": 1}))
    with pytest.raises(ValueError, match="unknown proposal section"):
        run_round.merge_proposals([{"index": 0, "name": "x", "proposal": bad}], applied.append)


def test_round_writes_staged_passes_through_the_store(tmp_path):
    import store_broker
    from test_store_contract import file_store
    st = file_store(tmp_path / "repo")
    (st.reg / "github_passes.json").write_text(json.dumps({"gh_a": {"tex": "2026-01-01"}}))
    with st.writer(round_id="rnd1") as w:
        broker = store_broker.Broker(st, w, "rnd1")
        with broker.serving():
            apply = run_round.github_passes_applier(broker)
            apply({"gh_a": {"tex": "2026-09-24", "man": "2026-09-24"}, "gh_b": {"tex": "2026-09-24"}})
            apply({"gh_a": {"tex": "2026-09-24"}})  # nothing new: no transaction is recorded
    assert json.loads((st.reg / "github_passes.json").read_text()) == {
        "gh_a": {"man": "2026-09-24", "tex": "2026-01-01"}, "gh_b": {"tex": "2026-09-24"}}
    with st.read() as v:
        runs = {e["run_id"] for e in dedup.scan_all(v, Table.EVENTS)}
    assert runs == {"rnd1.discover.github-passes"}


def test_registry_proposal_format_stays_a_list_without_passes(tmp_path, monkeypatch):
    proposal = tmp_path / "p.json"
    monkeypatch.setenv(registry.PROPOSAL_ENV, str(proposal))
    registry.append_entries([{"id": "a"}])
    assert json.loads(proposal.read_text()) == [{"id": "a"}]
    assert registry.stage_github_passes({"gh_a": {"tex": "d1"}})
    assert registry.stage_github_passes({"gh_a": {"tex": "d2", "man": "d2"}})
    registry.append_entries([{"id": "b"}])
    assert registry.read_proposal(proposal) == {
        "entries": [{"id": "a"}, {"id": "b"}], "github_passes": {"gh_a": {"tex": "d1", "man": "d2"}}}


# --- architecture ----------------------------------------------------------------------------------

FORBIDDEN = {"existing_keys", "load_entries", "load_manifest_rows", "uniquify_ids", "MAN_DIR"}


def finder_sources():
    return sorted(SCRIPTS.glob("find_*.py")) + [SCRIPTS / "crawl_docs.py"]


@pytest.mark.parametrize("path", finder_sources(), ids=lambda p: p.name)
def test_finders_never_materialize_corpus_wide_key_sets(path):
    """Finders ask dedup.Keys about their own candidates; none may load every key, entry or
    manifest row, read manifest shards directly, or load the whole blocklist."""
    tree = ast.parse(path.read_text())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = node.value.id if isinstance(node.value, ast.Name) else None
            if owner == "registry" and node.attr in FORBIDDEN:
                bad.append(f"registry.{node.attr}")
            if owner == "blocklist" and node.attr == "load":
                bad.append("blocklist.load")
        if isinstance(node, ast.ImportFrom) and node.module in ("registry", "blocklist"):
            bad += [f"from {node.module} import {a.name}" for a in node.names]
    assert not bad, f"{path.name}: {bad}"
