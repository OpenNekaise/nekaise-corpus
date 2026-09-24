"""Eligibility and host policy come from the SAME store view as the data they are applied to
(store.pinned_policy; Codex review of stage 3 step 7). The working tree may change while a tool
waits for the round lock: a tool must never pair a policy it read earlier (or later) with rows
from its view. Here the view's pinned configuration and the working tree disagree on purpose:
every tool follows the pinned one, and invalid pinned policy fails closed."""
from __future__ import annotations

import json
import sys

import pytest

import check_contracts
import corpus_stats
import coverage as coverage_report
import crawl_docs
import dedup
import lint_registry
import pipeline_repo
import registry
import run_round
import store
import update_readme_stats as urs

ROWS = [
    {"id": "ost-a", "title": "A", "url": "https://e.org/a.pdf", "source": "osti",
     "license": "public-domain", "topic": "building_energy", "format": "pdf", "status": "ok",
     "text_chars": 100},
    {"id": "ost-b", "title": "B", "url": "https://e.org/b.pdf", "source": "soep",
     "license": "public-domain", "topic": "building_energy", "format": "pdf", "status": "ok",
     "text_chars": 300},
]
PINNED = {"soep": pipeline_repo.restriction({"source": "soep"})}  # what the view pinned


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Working tree: NO restriction. Every view pins PINNED (restricting ost-b) instead, as a
    view opened after an operator's policy edit (or a PgStore's pinned config) would."""
    root = pipeline_repo.write_repo(tmp_path / "r", entries=[pipeline_repo.entry_of(r)
                                                             for r in ROWS], manifest=ROWS)
    real = store.FileStore._config

    def pinned(self):
        snap = real(self)
        docs = dict(snap.documents)
        docs["eligibility.json"] = {"version": 1, "restrictions": PINNED}
        return store.ConfigSnapshot(docs, snap.digests)
    monkeypatch.setattr(store.FileStore, "_config", pinned)
    monkeypatch.setattr(registry, "ROOT", root)
    return root


def test_doc_stats_uses_the_views_pinned_policy(repo):
    with store.FileStore(repo).read() as view:
        assert run_round.doc_stats(view) == (1, 25, 1)  # ost-b excluded by the pinned rule


def test_readme_stats_use_the_views_pinned_policy(repo, monkeypatch, capsys):
    monkeypatch.setattr(urs, "HERE", repo)
    urs.main(["--print-tokens"])
    assert capsys.readouterr().out.strip() == "25"  # 100 chars / 4: ost-b is excluded


def test_coverage_uses_the_views_pinned_policy(repo, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["coverage.py", "--sources"])
    coverage_report.main()
    out = capsys.readouterr().out
    assert "corpus coverage — 1 training-eligible docs" in out
    assert "1 training-excluded provenance rows omitted" in out


def test_crawl_docs_refuses_by_the_pinned_policy(repo, monkeypatch):
    monkeypatch.setattr(dedup, "_default_root", lambda: repo)
    assert crawl_docs.pinned_restrictions() == PINNED


def test_lint_checks_restrictions_from_the_pinned_policy(repo, capsys):
    assert lint_registry.main(repo) == 0  # the pinned rule matches ost-b's entry
    capsys.readouterr()


@pytest.mark.parametrize("tool", ["lint", "contracts", "doc_stats"])
def test_invalid_pinned_policy_fails_closed(tmp_path, monkeypatch, tool, capsys):
    root = pipeline_repo.write_repo(tmp_path / "r", entries=[pipeline_repo.entry_of(ROWS[0])],
                                    manifest=ROWS[:1])
    (root / "registry" / "eligibility.json").write_text(json.dumps(
        {"version": 1, "restrictions": {"bad": {"match": {"license": "open"}}}}))
    monkeypatch.setattr(registry, "ROOT", root)
    if tool == "lint":
        assert lint_registry.main(root) == 1
        assert "invalid pinned eligibility.json" in capsys.readouterr().out
    elif tool == "contracts":
        monkeypatch.setattr(check_contracts, "ROOT", root)
        assert check_contracts.main() == 1
        assert "invalid pinned eligibility.json" in capsys.readouterr().out
    else:
        with store.FileStore(root).read() as view, pytest.raises(store.StoreError,
                                                                  match="invalid pinned"):
            run_round.doc_stats(view)


def test_corpus_stats_default_is_the_validated_pinned_policy(repo):
    with store.FileStore(repo).read() as view:
        assert corpus_stats.compute(view).documents == 1
