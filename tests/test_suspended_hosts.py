"""Fetch-suspended hosts (registry/host_policy.json) on a machine that lacks their payloads.

Successful rows on a suspended host keep their provenance, but on a fresh clone the loader may
not re-fetch them. They must be represented as "locally unavailable, suspended" — not expected in
corpus/, not counted as training text — and they must never displace an available mirror.
"""
import sys

import pytest

import clean_corpus as cc
import prune_corpus
import quality
import registry
import run_round

POLICY = {"escholarship.org": {"status": "suspended", "reason": "WAF", "decided_at": "2026-09-24"}}
ESC = "https://escholarship.org/content/qt1/qt1.pdf"
TEXT = ("Building energy simulation of HVAC systems, thermal comfort, ventilation, insulation "
        "and heat pump performance in residential and commercial buildings. ") * 60


def _row(sid, url, text_path=None, **extra):
    return {"id": sid, "url": url, "title": extra.pop("title", sid), "source": "x",
            "license": "cc-by", "topic": "building_energy", "format": "pdf", "status": "ok",
            "text_chars": 1000, "text_path": text_path or f"text/{sid}.md", **extra}


def test_eligibility_is_manifest_based_and_availability_is_reported_separately(tmp_path):
    missing = _row("ope-missing", ESC)
    held = _row("ope-held", ESC.replace("qt1", "qt2"))
    other = _row("nlr-other", "https://docs.nlr.gov/docs/fy24osti/1.pdf")  # also missing
    (tmp_path / "text").mkdir()
    (tmp_path / "text" / "ope-held.md").write_text("held")

    eligible, excluded = registry.partition_manifest_ok_rows([missing, held, other], {})
    unavailable = registry.locally_unavailable_rows(eligible, POLICY, tmp_path)

    assert [r["id"] for r in eligible] == ["ope-missing", "ope-held", "nlr-other"]
    assert excluded == []
    assert [r["id"] for r in unavailable] == ["ope-missing"]  # only suspended hosts


def test_doc_stats_count_suspended_rows_whatever_is_held(monkeypatch, tmp_path):
    import store
    import pipeline_repo
    monkeypatch.setattr(registry, "ROOT", tmp_path)  # nothing held locally
    pipeline_repo.pin_policy(tmp_path, policy=POLICY)
    st = store.FileStore(tmp_path)
    with st.writer() as w:
        with st.transaction("seed", expected_version=st.version(), writer=w) as tx:
            tx.upsert_manifest([_row("ope-missing", ESC),
                                _row("nlr-1", "https://docs.nlr.gov/docs/fy24osti/1.pdf")])
    with st.read() as view:
        assert run_round.doc_stats(view) == (2, 500, 0)


def test_clean_check_passes_on_a_fresh_clone_and_reports_unavailable(
    monkeypatch, tmp_path, capsys
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "nlr-1.md").write_text("# t\n\n---\n\nbody")
    body = cc.split_header((corpus / "nlr-1.md").read_text())[1]
    rows = [
        _row("ope-missing", ESC, corpus_path="corpus/ope-missing.md", corpus_chars=10),
        _row("nlr-1", "https://docs.nlr.gov/docs/fy24osti/1.pdf", corpus_chars=len(body)),
    ]
    monkeypatch.setattr(cc, "HERE", tmp_path)
    monkeypatch.setattr(cc, "CORPUS", corpus)
    monkeypatch.setattr(cc, "STAMP", corpus / ".ruleset")
    import pipeline_repo
    import store  # --check reads the manifest and its pinned policy through the store
    pipeline_repo.write_repo(tmp_path, policy=POLICY)
    st = store.FileStore(tmp_path)
    with st.writer() as w:
        with st.transaction("seed", expected_version=st.version(), writer=w) as tx:
            tx.upsert_manifest(rows)
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--check"])

    cc.main()  # would raise SystemExit(1) on "missing from corpus/: ope-missing.md"

    out = capsys.readouterr().out
    assert "locally unavailable, suspended host" in out and "ope-missing" in out
    assert "OK — corpus/ matches the manifest exactly" in out


def _prune(monkeypatch, tmp_path, rows):
    """prune --apply over a store at tmp_path holding `rows`; returns (removed ids, blocked)."""
    import pipeline_repo
    pipeline_repo.write_repo(tmp_path, entries=[pipeline_repo.entry_of(r) for r in rows],
                             manifest=rows)
    pipeline_repo.point(monkeypatch, tmp_path, policy=POLICY)
    monkeypatch.setattr(prune_corpus, "deferred_ids", lambda: set())
    before = pipeline_repo.entry_ids(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply"])
    prune_corpus.main()
    blocked = [u for u in (tmp_path / "pruned_urls.txt").read_text().splitlines() if u]
    return before - pipeline_repo.entry_ids(tmp_path), blocked


def _mirror_pair(tmp_path, suspended_has_text):
    (tmp_path / "text").mkdir()
    (tmp_path / "text" / "nlr-mirror.md").write_text(TEXT)
    if suspended_has_text:
        (tmp_path / "text" / "ope-susp.md").write_text(TEXT)
    metrics = quality.metrics(TEXT)
    assert quality.verdict(metrics, False) == "ok"
    title = "Same Building Paper"
    return [
        _row("ope-susp", ESC, title=title, sha256="a" * 64, quality=metrics),
        _row("nlr-mirror", "https://docs.nlr.gov/docs/fy24osti/9.pdf", title=title,
             sha256="a" * 64, quality=metrics),
    ]


def test_unavailable_suspended_copy_never_destroys_an_available_mirror(monkeypatch, tmp_path):
    removed, blocked = _prune(monkeypatch, tmp_path, _mirror_pair(tmp_path, False))

    assert removed == set() and blocked == []  # neither dup-title nor dup-bytes


def test_held_suspended_copy_with_text_still_dedups_its_mirror(monkeypatch, tmp_path):
    removed, _ = _prune(monkeypatch, tmp_path, _mirror_pair(tmp_path, True))

    assert removed == {"nlr-mirror"}  # the available protected copy keeps its claim
    assert "ope-susp" not in removed


@pytest.mark.parametrize("has_text", [False, True])
def test_suspended_row_itself_is_never_pruned(monkeypatch, tmp_path, has_text):
    removed, _ = _prune(monkeypatch, tmp_path, _mirror_pair(tmp_path, has_text))
    assert "ope-susp" not in removed


SUSPENDED = [_row(f"ope-s{n}", ESC.replace("qt1", f"qt{n}"), text_chars=1_000_000,
                 topic="architecture") for n in range(2)]
OTHER = [_row("nlr-1", "https://docs.nlr.gov/1.pdf", text_chars=1_000_000)]


def _readme_for(monkeypatch, tmp_path, held: int) -> str:
    """Run update_readme_stats on a machine holding `held` of the 2 suspended payloads."""
    import update_readme_stats as urs

    root = tmp_path / f"held-{held}"
    (root / "text").mkdir(parents=True)
    for row in SUSPENDED[:held] + OTHER:
        (root / row["text_path"]).write_text("text")
    readme = root / "README.md"
    readme.write_text(f"head\n{urs.START}\nold\n{urs.END}\ntail\n")
    shards = {}
    for row in SUSPENDED + OTHER:  # the stats now read the manifest through the store
        shards.setdefault(registry.manifest_shard(row["id"]), []).append(dict(row))
    (root / "manifest").mkdir()
    for stem, group in shards.items():
        (root / "manifest" / f"{stem}.jsonl").write_text(registry.manifest_shard_text(group))
    monkeypatch.setattr(urs, "HERE", root)
    monkeypatch.setattr(registry, "ROOT", root)
    import pipeline_repo
    pipeline_repo.pin_policy(root, policy=POLICY)  # the stats read their view's pinned policy
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "du", lambda _path: "1G")  # disk usage is not a manifest statistic
    urs.main([])
    return readme.read_text()


def test_readme_statistics_are_identical_on_every_machine(monkeypatch, tmp_path, capsys):
    fresh, partial, full = (_readme_for(monkeypatch, tmp_path, held) for held in (0, 1, 2))

    assert fresh == partial == full
    assert "| **Documents** | **3** |" in full and "~3M chars" in full
    assert "local availability: 2 eligible rows" in capsys.readouterr().err  # fresh clone


def test_readme_contract_uses_one_coherent_view(monkeypatch, tmp_path):
    import check_contracts

    import corpus_stats
    from collections import Counter
    good = _readme_for(monkeypatch, tmp_path, 1)
    rows = SUSPENDED + OTHER
    chars = sum(r["text_chars"] for r in rows)
    stats = corpus_stats.CorpusStats(len(rows), 0, chars, chars,
                                     list(Counter(r["topic"] for r in rows).items()), {})
    assert check_contracts.readme_stats_errors(good, stats) == []

    def readme(docs, chars, topics):
        return (f"| **Documents** | **{docs}** |\n~{chars} chars\n"
                f"| **Policy-excluded provenance** | **0** rows (not fetched or training-ready) |\n"
                f"| **Topics** | {topics}\n")

    # the formerly tolerated "fresh-clone view" and inconsistent mixtures are all rejected
    assert check_contracts.readme_stats_errors(readme(1, "1M", 1), stats)
    assert check_contracts.readme_stats_errors(readme(1, "3M", 2), stats)
    assert check_contracts.readme_stats_errors(readme(3, "1M", 2), stats)
    assert check_contracts.readme_stats_errors(readme(3, "3M", 1), stats)
    assert check_contracts.readme_stats_errors(readme(3, "3M", 2), stats) == []
