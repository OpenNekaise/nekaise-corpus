"""Durable retry of transient polite-host failures across rounds (loader -> pruner -> loader).

Discovery cursors advance before the fetch, so a challenge-failed candidate that the pruner
removes is never rediscovered. The loader marks such rows `transient`; the pruner keeps them for
a bounded retry window and never blocklists them.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

import build_corpus
import prune_corpus
from runids import rid

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


def _run_prune(monkeypatch, rows, deferred=None, tmp_path=None):
    """prune --apply over a throwaway store holding `rows` (and their registry entries); returns
    (registry entries removed, urls blocklisted, manifest ids kept)."""
    import tempfile

    import pipeline_repo
    root = pipeline_repo.write_repo(
        Path(tempfile.mkdtemp(dir=tmp_path)) if tmp_path else Path(tempfile.mkdtemp()),
        entries=[pipeline_repo.entry_of(r) for r in rows], manifest=rows)
    pipeline_repo.point(monkeypatch, root)
    if deferred is None:
        monkeypatch.setattr(prune_corpus, "deferred_ids", lambda: set())
    else:  # the real hand-off file, written by the loader of the same run
        monkeypatch.setenv("NEKAISE_RUN_ID", rid("run-1"))
        path = root / "fetch-deferred.json"
        monkeypatch.setattr(build_corpus, "deferred_path", lambda: path)
        build_corpus.write_deferred(deferred)
        monkeypatch.setattr(prune_corpus, "deferred_ids",
                            lambda real=prune_corpus.deferred_ids: real(path))
    before = pipeline_repo.entry_ids(root)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply"])
    prune_corpus.main()
    blocked = [u for u in (root / "pruned_urls.txt").read_text().splitlines() if u]
    return (before - pipeline_repo.entry_ids(root), blocked,
            set(pipeline_repo.manifest_rows(root)))


def test_prune_keeps_pending_retries_and_expires_old_ones_without_blocklisting(monkeypatch,
                                                                                tmp_path):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        _failed("ibp-pending", transient=True, retry_attempts=1,
                first_failed_at=_stamp(now - timedelta(hours=1))),
        _failed("ibp-expired", transient=True, retry_attempts=prune_corpus.RETRY_MAX_ATTEMPTS,
                first_failed_at=_stamp(now - timedelta(hours=1))),
        _failed("ibp-hard", http_status=404, error="404 Client Error"),
    ]

    removed, blocked, kept = _run_prune(monkeypatch, rows, tmp_path=tmp_path)

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


def test_unattempted_rows_are_preserved_until_actually_attempted():
    never = _failed("ibp-skip", transient=True, retry_attempts=0)  # circuit-skipped only
    assert prune_corpus.retry_pending(never, NOW + timedelta(days=365))


def _ok_without_text(sid, url=None):
    return {"id": sid, "url": url or f"https://publications.ibpsa.org/{sid}.pdf", "title": sid,
            "source": "ibpsa", "topic": "building_energy", "license": "open", "status": "ok",
            "sha256": "0" * 64, "text_path": f"text/{sid}.md"}  # text file is missing


def test_deferred_rows_are_never_dropped_or_blocklisted(monkeypatch, tmp_path):
    rows = [_ok_without_text("ibp-deferred"), _ok_without_text("ibp-judged")]

    removed, blocked, kept = _run_prune(monkeypatch, rows, ["ibp-deferred"], tmp_path)

    assert "ibp-deferred" in kept and "ibp-deferred" not in removed
    assert all("ibp-deferred" not in url for url in blocked)
    assert removed == {"ibp-judged"}  # control: an undeferred no-text row is still pruned


def test_stale_deferred_file_from_another_run_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "fetch-deferred.json"
    monkeypatch.setattr(build_corpus, "deferred_path", lambda: path)
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("old-run"))
    build_corpus.write_deferred(["ibp-x"])
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("new-run"))

    with pytest.raises(prune_corpus.HandoffError, match=rid("old-run")):
        prune_corpus.deferred_ids(path)


def test_standalone_prune_ignores_any_handoff_file(monkeypatch, tmp_path):
    path = tmp_path / "fetch-deferred.json"
    path.write_text('{"run_id": null, "ids": ["ibp-x"]}')  # an old standalone-era handoff
    monkeypatch.delenv("NEKAISE_RUN_ID", raising=False)
    assert prune_corpus.deferred_ids(path) == set()
    monkeypatch.setenv("NEKAISE_RUN_ID", "")
    assert prune_corpus.deferred_ids(path) == set()


@pytest.mark.parametrize(
    "content",
    [None, "not json", '{"run_id": null, "ids": []}', '{"run_id": "", "ids": []}',
     json.dumps({"run_id": rid("run-1")}), json.dumps([rid("run-1")])],
)
def test_round_prune_fails_closed_on_missing_or_corrupt_handoff(monkeypatch, tmp_path, content):
    path = tmp_path / "fetch-deferred.json"
    if content is not None:
        path.write_text(content)
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("run-1"))

    with pytest.raises(prune_corpus.HandoffError):
        prune_corpus.deferred_ids(path)


def test_prune_main_exits_nonzero_without_the_handoff(monkeypatch, tmp_path, capsys):
    import pipeline_repo
    root = pipeline_repo.write_repo(tmp_path / "repo", manifest=[_failed("ibp-a")],
                                    entries=[pipeline_repo.entry_of(_failed("ibp-a"))])
    pipeline_repo.point(monkeypatch, root)
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("run-1"))  # no handoff written
    before = pipeline_repo.tracked(root)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply"])

    with pytest.raises(SystemExit) as exc:
        prune_corpus.main()

    assert exc.value.code == 1
    assert "refusing to prune" in capsys.readouterr().err
    assert pipeline_repo.tracked(root, journal=True) == before  # nothing written


def test_standalone_load_writes_no_handoff(monkeypatch, tmp_path):
    path = tmp_path / "fetch-deferred.json"
    monkeypatch.delenv("NEKAISE_RUN_ID", raising=False)
    build_corpus.write_deferred(["ibp-x"], path)
    assert not path.exists()


def test_suspended_host_rows_stay_as_they_are(monkeypatch, tmp_path):
    held = _ok_without_text("ope-held", "https://escholarship.org/content/qt2/qt2.pdf")
    failed = dict(_failed("ope-failed"), url="https://escholarship.org/content/qt3/qt3.pdf")

    removed, blocked, kept = _run_prune(monkeypatch, [held, failed], tmp_path=tmp_path)

    assert removed == set() and blocked == []
    assert kept == {"ope-held", "ope-failed"}


def test_81_ibpsa_restorations_are_all_fetched_despite_the_run_cap(tmp_path, monkeypatch):
    import pipeline_repo
    rows = {f"ibp-{n}": {**_ok_without_text(f"ibp-{n}"), "format": "pdf",
                         "raw_path": f"raw/ibpsa/ibp-{n}.pdf"} for n in range(81)}
    root = pipeline_repo.write_repo(tmp_path / "repo", manifest=list(rows.values()),
                                    entries=[pipeline_repo.entry_of(r) for r in rows.values()])
    pipeline_repo.point(monkeypatch, root)
    requested = []
    monkeypatch.setattr(build_corpus, "deferred_path", lambda: tmp_path / "deferred.json")
    monkeypatch.setattr(build_corpus, "download_one",
                        lambda src: requested.append(src["id"]) or {
                            **src, "status": "failed", "error": "x", "http_status": None,
                            "raw_path": None})
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("run-81"))
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1"])

    build_corpus.main()

    assert build_corpus.HOST_RUN_CAP["publications.ibpsa.org"] == 80
    assert len(requested) == 81
    assert '"ids": []' in (tmp_path / "deferred.json").read_text()


def test_nlr_timeout_survives_prune_for_retry(tmp_path, monkeypatch):
    """A non-polite host's timeout is kept for retry after its discovery cursor moved on."""
    monkeypatch.setattr(build_corpus.requests, "get", lambda *_a, **_k: (_ for _ in ()).throw(
        build_corpus.requests.Timeout("read timed out")))
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    row = build_corpus.download_one({
        "id": "nlr-x", "title": "X", "source": "nlr", "license": "public-domain",
        "url": "https://docs.nlr.gov/docs/fy24osti/1.pdf", "topic": "building_energy",
        "format": "pdf",
    })
    build_corpus.note_retry(row, None)

    removed, blocked, kept = _run_prune(monkeypatch, [row], tmp_path=tmp_path)

    assert kept == {"nlr-x"} and removed == set() and blocked == []
