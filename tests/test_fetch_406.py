"""HTTP 406 must not poison permanent discovery dedup or hammer a refused host."""
from types import SimpleNamespace

import pytest

import build_corpus as loader
import fetch_backoff
import oa_resolution
import prune_corpus
from test_transient_retry import _run_prune


def source(n=1, host="repo.example.edu"):
    return {"id": f"oas-{n}", "title": f"Building simulation {n}", "source": "openalex_sim",
            "url": f"https://{host}/{n}.pdf", "license": "unverified",
            "format": "pdf", "topic": "simulation_modeling"}


def answer(status, headers=None):
    def raise_for_status():
        if status >= 400:
            raise loader.requests.HTTPError(f"HTTP {status}")
    return SimpleNamespace(status_code=status, headers=headers or {}, content=b"response",
                           raise_for_status=raise_for_status)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "HERE", tmp_path)
    monkeypatch.setattr(fetch_backoff, "ROOT", tmp_path)
    monkeypatch.setattr(loader, "HOST_DELAY", {})
    monkeypatch.setattr(loader, "_tripped_hosts", {})
    monkeypatch.setattr(loader.subprocess, "run", lambda *a, **kw: pytest.fail("no curl"))
    return tmp_path


def test_406_holds_host_across_runs_without_losing_discovery_or_aging_skips(env, monkeypatch):
    calls = []
    monkeypatch.setattr(loader.requests, "get",
                        lambda url, **kw: calls.append(url) or answer(406))
    first = loader.download_one(source())
    loader.note_retry(first, None)
    assert first["http_status"] == 406 and first["retry_attempts"] == 1
    assert prune_corpus.retry_pending(first)
    assert not prune_corpus._blocklistable(first, "failed")

    monkeypatch.setattr(loader, "_tripped_hosts", {})  # another process/round
    skipped = loader.download_one(source())
    loader.note_retry(skipped, first)
    assert skipped["retry_attempts"] == 1
    assert skipped["first_failed_at"] == first["first_failed_at"]
    never_requested = loader.download_one(source(2))
    loader.note_retry(never_requested, None)
    assert never_requested["retry_attempts"] == 0
    assert calls == [source()["url"]]
    # Preserve discovery provenance while the host is cooling. Its cursor must not walk
    # past temporarily unavailable copies without queuing them for the loader.
    assert oa_resolution.copy_refusal(source(2)["url"], {}) is None
    assert oa_resolution.copy_refusal(source(2, "sibling.example.edu")["url"], {}) is None

    until = fetch_backoff.active(source()["url"])
    monkeypatch.setattr(fetch_backoff.time, "time", lambda: until + 1)
    assert oa_resolution.copy_refusal(source()["url"], {}) is None
    loader.download_one(source())
    assert calls == [source()["url"], source()["url"]]


@pytest.mark.parametrize("retry_after, expected", [
    (None, 86400), ("1", 86400), ("172800", 172800), ("nonsense", 86400),
    ("NaN", 86400), ("inf", 86400),
    ("Sat, 03 Jan 1970 00:00:00 GMT", 172800),
])
def test_retry_after_and_minimum_hold(env, monkeypatch, retry_after, expected):
    monkeypatch.setattr(fetch_backoff.time, "time", lambda: 0)
    assert fetch_backoff.defer(source()["url"], retry_after) == expected
    assert fetch_backoff.defer(source()["url"], "0") == expected  # never shorten


def test_prune_expiry_406_and_permanent_failure_controls(env, monkeypatch):
    monkeypatch.setattr(loader.requests, "get", lambda *a, **kw: answer(406))
    pending = loader.download_one(source())
    loader.note_retry(pending, None)
    expired = dict(pending, **source(2), retry_attempts=prune_corpus.RETRY_MAX_ATTEMPTS)
    legacy = dict(pending, **source(3))
    legacy.pop("transient")  # old loader rows lack the marker
    controls = [dict(pending, **source(n), http_status=status, transient=False,
                     error=f"HTTP {status}") for n, status in ((4, 404), (5, 410))]
    removed, blocked, kept = _run_prune(monkeypatch, [pending, expired, legacy, *controls],
                                      tmp_path=env)
    assert kept == {pending["id"]}
    assert removed == {r["id"] for r in [expired, legacy, *controls]}
    assert set(blocked) == {r["url"] for r in controls}


def test_406_cooldown_rechecked_after_wait(env, monkeypatch):
    monkeypatch.setattr(loader.requests, "get", lambda *a, **kw: pytest.fail("no request"))
    monkeypatch.setattr(loader, "_wait_for_host",
                        lambda host: fetch_backoff.defer(source()["url"], None))
    row = loader.download_one(source())
    assert row["transient"] and row["_not_requested"]


def test_cooldown_storage_failure_cannot_make_406_permanent(env, monkeypatch):
    monkeypatch.setattr(loader.requests, "get", lambda *a, **kw: answer(406))
    monkeypatch.setattr(fetch_backoff, "defer",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    row = loader.download_one(source())
    assert row["http_status"] == 406 and row["transient"]
    assert "cooldown persistence failed" in row["error"]
    assert "circuit open" in loader.download_one(source(2))["error"]


def test_redirect_cools_only_the_response_host(env, monkeypatch):
    destination = source(2, "destination.example.edu")["url"]
    monkeypatch.setattr(loader.requests, "get", lambda url, **kw:
                        answer(302, {"Location": destination}) if url == source()["url"]
                        else answer(406))
    row = loader.download_one(source())
    assert row["http_status"] == 406 and row["transient"]
    assert row["final_url"] == destination
    assert fetch_backoff.active(destination)
    assert fetch_backoff.active(source()["url"]) is None


def test_curl_406_is_classified_and_cannot_ignore_the_hold(env, monkeypatch):
    monkeypatch.setattr(loader.requests, "get", lambda *a, **kw: answer(403))
    calls = []

    def curl(cmd, **kwargs):
        calls.append(cmd)
        from pathlib import Path
        Path(cmd[cmd.index("-D") + 1]).write_bytes(
            b"HTTP/1.1 406 Not Acceptable\r\nRetry-After: 172800\r\n\r\n")
        kwargs["stdout"].write(b"Not Acceptable")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(loader.subprocess, "run", curl)
    row = loader.download_one(source())
    assert row["http_status"] == 406 and row["transient"]
    assert fetch_backoff.active(source()["url"]) > fetch_backoff.time.time() + 170000
    with pytest.raises(loader.ChallengeRefused):
        loader._curl_follow(source()["url"])
    assert len(calls) == 1
