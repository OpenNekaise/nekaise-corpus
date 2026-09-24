"""Durable retry of transient polite-host failures across rounds (loader -> pruner -> loader).

Discovery cursors advance before the fetch, so a challenge-failed candidate that the pruner
removes is never rediscovered. The loader marks such rows `transient`; the pruner keeps them for
a bounded retry window and never blocklists them.
"""
import sys
from datetime import datetime, timedelta, timezone

import build_corpus
import prune_corpus

NOW = datetime(2026, 9, 24, 12, 0, 0)


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _failed(sid, **extra):
    return {"id": sid, "url": f"https://publications.ibpsa.org/{sid}.pdf", "title": sid,
            "source": "ibpsa", "topic": "building_energy", "license": "open",
            "status": "failed", "http_status": 202, "error": "HTTP 202 challenge", **extra}


def test_retry_window_is_bounded_by_attempts_and_age():
    fresh = _failed("ibp-a", transient=True, retry_attempts=3,
                    first_failed_at=_stamp(NOW - timedelta(days=1)))
    assert prune_corpus.retry_pending(fresh, NOW)
    worn = dict(fresh, retry_attempts=prune_corpus.RETRY_MAX_ATTEMPTS)
    assert not prune_corpus.retry_pending(worn, NOW)
    old = dict(fresh, first_failed_at=_stamp(
        NOW - timedelta(days=prune_corpus.RETRY_MAX_AGE_DAYS)))
    assert not prune_corpus.retry_pending(old, NOW)
    assert not prune_corpus.retry_pending(_failed("ibp-b"), NOW)  # hard failure: no retry
    assert not prune_corpus.retry_pending(dict(fresh, first_failed_at="garbage"), NOW)


def test_transient_rows_are_never_blocklisted_even_with_403():
    row = _failed("ibp-a", transient=True, http_status=403)
    assert prune_corpus._blocklistable(row, "failed") is False
    assert prune_corpus._blocklistable(_failed("ibp-b", http_status=403), "failed") is True


def _run_prune(monkeypatch, rows):
    removed, blocked, written = [], [], []
    monkeypatch.setattr(prune_corpus.registry, "load_manifest_rows", lambda: rows)
    monkeypatch.setattr(prune_corpus.registry, "load_prune_ledger_rows", lambda: [])
    monkeypatch.setattr(prune_corpus.registry, "remove_ids",
                        lambda ids: removed.extend(ids) or len(ids))
    monkeypatch.setattr(prune_corpus.registry, "write_manifest_rows", written.extend)
    monkeypatch.setattr(prune_corpus.blocklist, "add",
                        lambda urls: blocked.extend(urls) or len(urls))
    monkeypatch.setattr(prune_corpus, "write_prune_ledger", lambda *_a: 0)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply"])
    prune_corpus.main()
    return set(removed), blocked, {r["id"] for r in written}


def test_prune_keeps_pending_retries_and_expires_old_ones_without_blocklisting(monkeypatch):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        _failed("ibp-pending", transient=True, retry_attempts=1,
                first_failed_at=_stamp(now - timedelta(hours=1))),
        _failed("ibp-expired", transient=True, retry_attempts=prune_corpus.RETRY_MAX_ATTEMPTS,
                first_failed_at=_stamp(now - timedelta(hours=1))),
        _failed("ibp-hard", http_status=404, error="404 Client Error"),
    ]

    removed, blocked, kept = _run_prune(monkeypatch, rows)

    assert removed == {"ibp-expired", "ibp-hard"}
    assert kept == {"ibp-pending"}  # stays in registry + manifest for the next loader run
    assert blocked == ["https://publications.ibpsa.org/ibp-hard.pdf"]


def test_round_trip_loader_retries_a_kept_row_until_it_succeeds(tmp_path, monkeypatch):
    """Round 1 hits a captcha, prune keeps the row, round 2's loader fetches it."""
    src = {"id": "ibp-x", "title": "X", "source": "ibpsa", "license": "open",
           "url": "https://publications.ibpsa.org/x.pdf", "topic": "building_energy",
           "format": "pdf"}
    answers = iter([
        type("R", (), {"status_code": 202, "content": b"<html>sgcaptcha",
                       "raise_for_status": lambda self: None})(),
        type("R", (), {"status_code": 200, "content": b"%PDF-1.7 ok",
                       "raise_for_status": lambda self: None})(),
    ])
    monkeypatch.setattr(build_corpus.requests, "get", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})

    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    round1 = build_corpus.download_one(src)
    build_corpus.note_retry(round1, None)
    assert prune_corpus.retry_pending(round1)

    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})  # a new run starts closed
    round2 = build_corpus.download_one(src)
    assert round2["raw_path"] and "transient" not in round2


def test_ok_rows_are_never_in_a_retry_window():
    assert not prune_corpus.retry_pending({"status": "ok", "transient": True})
