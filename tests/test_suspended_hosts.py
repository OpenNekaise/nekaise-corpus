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


def test_missing_suspended_row_is_unavailable_not_eligible(tmp_path):
    missing = _row("ope-missing", ESC)
    held = _row("ope-held", ESC.replace("qt1", "qt2"))
    other = _row("nlr-other", "https://docs.nlr.gov/docs/fy24osti/1.pdf")  # also missing
    (tmp_path / "text").mkdir()
    (tmp_path / "text" / "ope-held.md").write_text("held")
    unavailable = []

    eligible, excluded = registry.partition_manifest_ok_rows(
        [missing, held, other], {}, unavailable, policy=POLICY, root=tmp_path)

    assert [r["id"] for r in eligible] == ["ope-held", "nlr-other"]  # other hosts unchanged
    assert [r["id"] for r in unavailable] == ["ope-missing"]
    assert excluded == []


def test_doc_stats_do_not_count_locally_unavailable_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "ROOT", tmp_path)
    monkeypatch.setattr(registry, "load_host_policy", lambda: POLICY)
    monkeypatch.setattr(registry, "load_manifest_rows",
                        lambda: [_row("ope-missing", ESC),
                                 _row("nlr-1", "https://docs.nlr.gov/docs/fy24osti/1.pdf")])
    monkeypatch.setattr(registry, "load_eligibility", lambda: {})

    assert run_round.doc_stats() == (1, 250, 0)


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
    monkeypatch.setattr(registry, "load_host_policy", lambda: POLICY)
    monkeypatch.setattr(registry, "load_manifest_rows", lambda: rows)
    monkeypatch.setattr(registry, "load_eligibility", lambda: {})
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--check"])

    cc.main()  # would raise SystemExit(1) on "missing from corpus/: ope-missing.md"

    out = capsys.readouterr().out
    assert "locally unavailable, suspended host" in out and "ope-missing" in out
    assert "OK — corpus/ matches the manifest exactly" in out


def _prune(monkeypatch, tmp_path, rows):
    removed, blocked = [], []
    monkeypatch.setattr(prune_corpus, "HERE", tmp_path)
    monkeypatch.setattr(prune_corpus, "deferred_ids", lambda: set())
    monkeypatch.setattr(prune_corpus.host_policy, "load", lambda: POLICY)
    monkeypatch.setattr(prune_corpus.registry, "load_manifest_rows", lambda: rows)
    monkeypatch.setattr(prune_corpus.registry, "load_prune_ledger_rows", lambda: [])
    monkeypatch.setattr(prune_corpus.registry, "remove_ids",
                        lambda ids: removed.extend(ids) or len(ids))
    monkeypatch.setattr(prune_corpus.registry, "write_manifest_rows", lambda _k: None)
    monkeypatch.setattr(prune_corpus.blocklist, "add",
                        lambda urls: blocked.extend(urls) or len(urls))
    monkeypatch.setattr(prune_corpus, "write_prune_ledger", lambda *_a: 0)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply"])
    prune_corpus.main()
    return set(removed), blocked


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


def test_readme_contract_accepts_either_view_and_nothing_else():
    import check_contracts

    rows = [_row("nlr-1", "https://docs.nlr.gov/1.pdf", text_chars=1_000_000)]
    unavailable = [_row("ope-missing", ESC, topic="architecture", text_chars=1_000_000)]

    def readme(docs, chars, topics):
        return (f"| **Documents** | **{docs}** |\n~{chars} chars\n"
                f"| **Policy-excluded provenance** | **0** rows (not fetched or training-ready) |\n"
                f"| **Topics** | {topics}\n")

    holder = readme(2, "2M", 2)  # written on the machine that holds the suspended payloads
    fresh = readme(1, "1M", 1)   # written on a fresh clone / CI
    assert check_contracts.readme_stats_errors(holder, rows, unavailable, 0) == []
    assert check_contracts.readme_stats_errors(fresh, rows, unavailable, 0) == []
    assert len(check_contracts.readme_stats_errors(readme(3, "3M", 3), rows, unavailable, 0)) == 3
    assert check_contracts.readme_stats_errors(holder, rows, [], 0)  # no tolerance without cause
