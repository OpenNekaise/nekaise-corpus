"""Focused tests for host-specific download handshakes."""

from types import SimpleNamespace
import hashlib
from concurrent.futures import ProcessPoolExecutor
import sys
from urllib.parse import urlparse

import pytest

import build_corpus


@pytest.fixture(autouse=True)
def _deferred_file_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(build_corpus, "deferred_path", lambda: tmp_path / "fetch-deferred.json")


def test_extraction_workers_use_spawn_context():
    assert build_corpus.EXTRACTION_CONTEXT.get_start_method() == "spawn"


def test_main_never_downloads_pointer_only_sources(monkeypatch, capsys):
    pointer = {
        "id": "vendor-standard",
        "title": "Vendor standard",
        "url": "https://example.org/standard.pdf",
        "source": "vendor",
        "license": "proprietary-internal",
        "topic": "standards_protocols",
        "format": "pdf",
    }
    monkeypatch.setattr(build_corpus.registry, "load_entries", lambda: [pointer])
    monkeypatch.setattr(build_corpus.registry, "load_eligibility", lambda: {})
    monkeypatch.setattr(build_corpus, "load_manifest", lambda: {})
    monkeypatch.setattr(
        build_corpus,
        "download_one",
        lambda _source: (_ for _ in ()).throw(AssertionError("pointer-only source downloaded")),
    )
    monkeypatch.setattr(sys, "argv", ["build_corpus.py"])

    build_corpus.main()

    output = capsys.readouterr().out
    assert "pointer-only sources: 1 skipped by license policy" in output
    assert "sources: 0 total, 0 to fetch" in output


def test_main_never_downloads_policy_restricted_sources(monkeypatch, capsys):
    restricted = {
        "id": "pat-cn123",
        "title": "Translated patent",
        "url": "https://patents.example/patent/CN123/en",
        "source": "google_patents",
        "license": "open",
        "topic": "materials",
        "format": "html",
    }
    rules = {
        "translated": {
            "match": {"id_prefix": "pat-cn"},
        },
    }
    monkeypatch.setattr(build_corpus.registry, "load_entries", lambda: [restricted])
    monkeypatch.setattr(build_corpus.registry, "load_eligibility", lambda: rules)
    monkeypatch.setattr(build_corpus, "load_manifest", lambda: {})
    monkeypatch.setattr(
        build_corpus,
        "download_one",
        lambda _source: (_ for _ in ()).throw(AssertionError("restricted source downloaded")),
    )
    monkeypatch.setattr(sys, "argv", ["build_corpus.py"])

    build_corpus.main()

    output = capsys.readouterr().out
    assert "policy-restricted sources: 1 skipped by eligibility policy" in output
    assert "sources: 0 total, 0 to fetch" in output


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def response(url, content=b"", content_type="text/html"):
    return SimpleNamespace(
        url=url,
        content=content,
        headers={"Content-Type": content_type},
    )


def test_publications_gc_archive_notice_is_followed_with_session_and_referer(monkeypatch):
    url = "https://publications.gc.ca/collections/collection_2025/cnrc-nrc/NR24-28-1965-eng.pdf"
    notice = "https://publications.gc.ca/site/archivee-archived.html?url=example"
    session = FakeSession([
        response(notice, b"<html>archive notice</html>"),
        response(url, b"%PDF-1.4 document", "application/pdf"),
    ])
    monkeypatch.setattr(build_corpus.requests, "Session", lambda: session)

    got = build_corpus._fetch_publications_gc_ca(url)

    assert got.content.startswith(b"%PDF-")
    assert session.calls[1][1]["headers"] == {"Referer": notice}


def test_publications_gc_direct_pdf_needs_no_second_request(monkeypatch):
    url = "https://publications.gc.ca/collections/example.pdf"
    session = FakeSession([response(url, b"%PDF-1.7 document", "application/pdf")])
    monkeypatch.setattr(build_corpus.requests, "Session", lambda: session)

    got = build_corpus._fetch_publications_gc_ca(url)

    assert got.content.startswith(b"%PDF-")
    assert len(session.calls) == 1


def test_fetch_records_optional_provenance_and_text_hash(tmp_path, monkeypatch):
    body = b"Building ventilation and structural design guidance."
    got = SimpleNamespace(
        status_code=200,
        content=body,
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(build_corpus.requests, "get", lambda *args, **kwargs: got)
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "TEXT", tmp_path / "text")
    src = {
        "id": "test-doc",
        "title": "Test document",
        "url": "https://example.org/test.txt",
        "source": "test",
        "license": "cc-by",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "language": "en",
        "topic": "construction",
        "format": "txt",
    }

    row = build_corpus.fetch_one(src)

    rendered = (tmp_path / row["text_path"]).read_bytes()
    assert row["status"] == "ok"
    assert row["language"] == "en"
    assert row["license_url"].startswith("https://creativecommons.org/")
    assert row["text_sha256"] == hashlib.sha256(rendered).hexdigest()
    assert row["extractor_version"].startswith("build_corpus/3;")


def test_download_and_extraction_are_separate_stages(tmp_path, monkeypatch):
    got = SimpleNamespace(
        status_code=200,
        content=b"Building envelope and ventilation guidance.",
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(build_corpus.requests, "get", lambda *args, **kwargs: got)
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "TEXT", tmp_path / "text")
    monkeypatch.setattr(
        build_corpus,
        "extract_for",
        lambda *_args: (_ for _ in ()).throw(AssertionError("download invoked extraction")),
    )
    src = {
        "id": "staged",
        "title": "Staged",
        "url": "https://example.org/staged.txt",
        "source": "test",
        "license": "cc-by",
        "topic": "construction",
        "format": "txt",
    }

    downloaded = build_corpus.download_one(src)

    assert downloaded["raw_path"] == "raw/test/staged.txt"
    assert downloaded["status"] == "failed"  # finalized only by the extraction stage
    monkeypatch.setattr(build_corpus, "extract_for", lambda _fmt, data: data.decode())
    with ProcessPoolExecutor(max_workers=1) as pool:
        extracted = pool.submit(build_corpus.extract_downloaded, downloaded).result(timeout=10)
    assert extracted["status"] == "ok"
    assert extracted["text_chars"] > 0
    assert not any(key.startswith("_") for key in extracted)

    duplicate_src = {
        **src,
        "id": "staged-duplicate",
        "title": "Duplicate with independent provenance",
        "url": "https://example.org/duplicate.txt",
    }
    duplicate = build_corpus.download_one(duplicate_src)
    reused = build_corpus.reuse_extraction(duplicate, extracted)
    rendered = (tmp_path / reused["text_path"]).read_text()
    assert rendered.startswith("# Duplicate with independent provenance")
    assert "source: https://example.org/duplicate.txt" in rendered
    assert build_corpus.quality.body(rendered) == "Building envelope and ventilation guidance."


def test_curl_fallback_uses_file_instead_of_inheritable_pipe(tmp_path, monkeypatch):
    blocked = SimpleNamespace(
        status_code=403,
        content=b"blocked",
        raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    monkeypatch.setattr(build_corpus.requests, "get", lambda *args, **kwargs: blocked)
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")

    def fake_run(_command, **kwargs):
        assert "capture_output" not in kwargs
        assert kwargs["stderr"] is build_corpus.subprocess.DEVNULL
        kwargs["stdout"].write(b"fallback building guidance " * 30)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_corpus.subprocess, "run", fake_run)
    src = {
        "id": "fallback",
        "title": "Fallback",
        "url": "https://example.org/fallback.txt",
        "source": "test",
        "license": "cc-by",
        "topic": "construction",
        "format": "txt",
    }

    downloaded = build_corpus.download_one(src)

    assert downloaded["http_status"] == 200
    assert downloaded["bytes"] > 512
    assert (tmp_path / downloaded["raw_path"]).read_bytes().startswith(b"fallback")


def test_fair_sources_round_robins_hosts():
    sources = [
        {"id": "a1", "url": "https://a.example/1"},
        {"id": "a2", "url": "https://a.example/2"},
        {"id": "b1", "url": "https://b.example/1"},
        {"id": "c1", "url": "https://c.example/1"},
        {"id": "b2", "url": "https://b.example/2"},
    ]

    ordered = build_corpus.fair_sources(sources)
    hosts = [urlparse(source["url"]).netloc for source in ordered]

    assert hosts == ["a.example", "b.example", "c.example", "a.example", "b.example"]


# --- polite hosts: honest identity, challenge classification, circuit, durable retry ---------

def _polite_env(monkeypatch, tmp_path, answer):
    """Route requests.get to `answer(url)`; forbid curl; reset the per-run circuit."""
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]["User-Agent"]))
        result = answer(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(build_corpus.requests, "get", get)
    monkeypatch.setattr(
        build_corpus.subprocess, "run",
        lambda *_a, **_k: pytest.fail("polite hosts must never use the curl fallback"),
    )
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    return calls


def _ibpsa(n, host="publications.ibpsa.org"):
    return {
        "id": f"ibp-{n}", "title": f"Paper {n}", "source": "ibpsa", "license": "open",
        "url": f"https://{host}/proceedings/bs/2025/papers/bs2025_{n}.pdf",
        "topic": "building_energy", "format": "pdf",
    }


def _answer(status, body):
    return SimpleNamespace(
        status_code=status, content=body,
        raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError(f"HTTP {status}"))
        if status >= 400 else None,
    )


def test_polite_hosts_are_serial_paced_and_use_an_honest_ua_only():
    for host in ("publications.ibpsa.org", "escholarship.org"):
        assert host in build_corpus.POLITE_HOSTS
        assert build_corpus.HOST_CONCURRENCY[host] == 1
        assert not build_corpus.HOST_UA[host].startswith("Mozilla")
    assert build_corpus.HOST_DELAY["publications.ibpsa.org"] >= 3.0
    assert build_corpus.HOST_DELAY["escholarship.org"] >= 4.0  # robots Crawl-delay: 4


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (202, b"<html><meta http-equiv='refresh' content='0;/.well-known/sgcaptcha/'>"),
        (403, b"403 - Forbidden | Access to this page is forbidden."),
        (429, b"slow down"),
        (503, b"busy"),
        (200, b"<html><script src='/.well-known/sgcaptcha/c.js'></script></html>"),
    ],
)
def test_polite_challenge_trips_circuit_without_fallback(tmp_path, monkeypatch, status, body):
    calls = _polite_env(monkeypatch, tmp_path, lambda _url: _answer(status, body))

    first = build_corpus.download_one(_ibpsa(1))
    second = build_corpus.download_one(_ibpsa(2))

    assert first["transient"] is True and "challenge" in first["error"]
    assert first["http_status"] == status
    assert second["transient"] is True and "circuit open" in second["error"]
    assert len(calls) == 1  # never requested again this run, never a second identity
    assert calls[0][1] == build_corpus.HOST_UA["publications.ibpsa.org"]


def test_escholarship_refusal_is_transient_with_honest_ua(tmp_path, monkeypatch):
    calls = _polite_env(monkeypatch, tmp_path, lambda _url: _answer(403, b"Request blocked."))

    row = build_corpus.download_one(_ibpsa(1, host="escholarship.org"))

    assert row["transient"] is True
    assert calls[0][1] == build_corpus.HONEST_UA


def test_circuit_is_rechecked_after_the_pacing_wait(tmp_path, monkeypatch):
    calls = _polite_env(monkeypatch, tmp_path, lambda _url: pytest.fail("must not request"))
    monkeypatch.setattr(
        build_corpus, "_wait_for_host",
        lambda host: build_corpus._trip_host(host, "opened by another worker"),
    )

    row = build_corpus.download_one(_ibpsa(1))

    assert calls == []
    assert "opened by another worker" in row["error"] and row["transient"] is True


def test_polite_timeout_is_transient_but_hard_404_is_not(tmp_path, monkeypatch):
    answers = {
        "https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_1.pdf":
            build_corpus.requests.Timeout("read timed out"),
        "https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_2.pdf":
            _answer(404, b"not found"),
    }
    _polite_env(monkeypatch, tmp_path, answers.__getitem__)

    timed_out = build_corpus.download_one(_ibpsa(1))
    missing = build_corpus.download_one(_ibpsa(2))

    assert timed_out["transient"] is True
    assert "transient" not in missing and missing["http_status"] == 404


def test_polite_pdf_is_downloaded_normally(tmp_path, monkeypatch):
    _polite_env(monkeypatch, tmp_path, lambda _url: _answer(200, b"%PDF-1.7 building paper"))

    row = build_corpus.download_one(_ibpsa(1))

    assert row["raw_path"] and "transient" not in row


def test_non_polite_hosts_keep_their_fallback_and_no_circuit(tmp_path, monkeypatch):
    requested = []
    monkeypatch.setattr(
        build_corpus.requests, "get",
        lambda url, **_k: requested.append(url) or _answer(202, b"<html>"),
    )
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})
    for n in (1, 2):
        row = build_corpus.download_one({
            "id": f"x-{n}", "title": "X", "source": "test", "license": "open",
            "url": f"https://example.org/{n}.pdf", "topic": "construction", "format": "pdf",
        })
        assert row["transient"] is True  # recoverable, retried later; but no circuit

    assert len(requested) == 2


def test_retry_bookkeeping_counts_only_real_requests():
    first = {"id": "ibp-1", "status": "failed", "transient": True}
    build_corpus.note_retry(first, None)
    assert first["retry_attempts"] == 1 and first["first_failed_at"]

    skipped = {"id": "ibp-1", "status": "failed", "transient": True, "_not_requested": True}
    build_corpus.note_retry(skipped, first)
    assert skipped["retry_attempts"] == 1
    assert skipped["first_failed_at"] == first["first_failed_at"]
    assert "_not_requested" not in skipped

    again = {"id": "ibp-1", "status": "failed", "transient": True}
    build_corpus.note_retry(again, skipped)
    assert again["retry_attempts"] == 2

    ok = {"id": "ibp-1", "status": "ok"}
    build_corpus.note_retry(ok, again)
    assert "retry_attempts" not in ok and "transient" not in ok


def test_host_run_cap_defers_the_excess_without_touching_it(monkeypatch):
    monkeypatch.setattr(build_corpus, "HOST_RUN_CAP", {"publications.ibpsa.org": 2})
    srcs = [_ibpsa(n) for n in range(4)] + [
        {"id": "x", "url": "https://example.org/x.pdf"}
    ]

    kept, deferred = build_corpus.cap_per_host(srcs)

    assert [s["id"] for s in kept] == ["ibp-0", "ibp-1", "x"]
    assert deferred == ["ibp-2", "ibp-3"]


def test_host_run_cap_never_defers_restoration_of_previously_ok_rows(monkeypatch):
    monkeypatch.setattr(build_corpus, "HOST_RUN_CAP", {"publications.ibpsa.org": 1})
    srcs = [_ibpsa(n) for n in range(3)]
    manifest = {"ibp-1": {"id": "ibp-1", "status": "ok"}, "ibp-2": {"id": "ibp-2", "status": "ok"}}

    kept, deferred = build_corpus.cap_per_host(srcs, manifest)

    assert [s["id"] for s in kept] == ["ibp-0", "ibp-1", "ibp-2"]
    assert deferred == []



def test_retry_window_starts_only_with_a_real_attempt():
    skipped = {"id": "ibp-1", "status": "failed", "transient": True, "_not_requested": True}
    build_corpus.note_retry(skipped, None)
    assert skipped["retry_attempts"] == 0 and "first_failed_at" not in skipped

    attempted = {"id": "ibp-1", "status": "failed", "transient": True}
    build_corpus.note_retry(attempted, skipped)
    assert attempted["retry_attempts"] == 1 and attempted["first_failed_at"]


def _plain(url_suffix="1"):
    return {"id": f"nlr-{url_suffix}", "title": "Report", "source": "nlr",
            "license": "public-domain", "topic": "building_energy", "format": "pdf",
            "url": f"https://docs.nlr.gov/docs/fy24osti/{url_suffix}.pdf"}


@pytest.mark.parametrize(
    "error",
    [
        build_corpus.requests.Timeout("read timed out"),
        build_corpus.requests.ConnectionError("Connection reset by peer"),
    ],
)
def test_recoverable_network_failures_are_transient_on_every_host(tmp_path, monkeypatch, error):
    monkeypatch.setattr(build_corpus.requests, "get",
                        lambda *_a, **_k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)

    row = build_corpus.download_one(_plain())

    assert row["transient"] is True


def test_dns_failures_keep_the_pruner_evidence_rule(tmp_path, monkeypatch):
    error = build_corpus.requests.ConnectionError("NameResolutionError: Failed to resolve host")
    monkeypatch.setattr(build_corpus.requests, "get",
                        lambda *_a, **_k: (_ for _ in ()).throw(error))

    assert "transient" not in build_corpus.download_one(_plain())


@pytest.mark.parametrize(
    ("status", "body", "transient"),
    [
        (404, b"missing", False),
        (410, b"gone", False),
        (403, b"forbidden", False),   # non-polite 403: unchanged, still blocklistable
        (200, b"<html>not a pdf", False),  # fake PDF
        (503, b"busy", True),
        (429, b"slow down", True),
    ],
)
def test_hard_failures_are_unchanged_and_recoverable_statuses_are_transient(
    tmp_path, monkeypatch, status, body, transient
):
    import prune_corpus

    monkeypatch.setattr(build_corpus.requests, "get", lambda *_a, **_k: _answer(status, body))
    monkeypatch.setattr(  # the default-host curl fallback also fails
        build_corpus.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=22),
    )
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)
    monkeypatch.setattr(build_corpus, "RAW", tmp_path / "raw")

    row = build_corpus.download_one(_plain())

    assert bool(row.get("transient")) is transient
    assert prune_corpus._blocklistable(row, "failed") is (not transient)


def test_main_never_requests_or_records_suspended_hosts(tmp_path, monkeypatch, capsys):
    src = {"id": "ope-lbnl", "title": "LBNL paper", "source": "openalex", "license": "cc-by",
           "url": "https://escholarship.org/content/qt1/qt1.pdf", "topic": "building_energy",
           "format": "pdf"}
    held = {**src, "id": "ope-held", "url": "https://escholarship.org/content/qt2/qt2.pdf",
            "status": "ok", "raw_path": "raw/openalex/ope-held.pdf"}
    written = []
    monkeypatch.setattr(build_corpus.registry, "load_entries", lambda: [src, dict(held)])
    monkeypatch.setattr(build_corpus.registry, "load_eligibility", lambda: {})
    monkeypatch.setattr(build_corpus, "load_manifest", lambda: {"ope-held": dict(held)})
    monkeypatch.setattr(build_corpus, "write_manifest", written.append)
    monkeypatch.setattr(build_corpus, "HERE", tmp_path)  # held doc's raw file is missing
    monkeypatch.setattr(
        build_corpus, "download_one",
        lambda _s: (_ for _ in ()).throw(AssertionError("suspended host requested")),
    )
    monkeypatch.setattr(sys, "argv", ["build_corpus.py"])

    build_corpus.main()

    assert "host fetch suspended" in capsys.readouterr().out
    assert written == []  # no failure rows, nothing to age toward pruning


def test_escholarship_fetch_suspension_is_committed_policy():
    import check_contracts
    import host_policy

    rule = host_policy.suspended("https://escholarship.org/content/qt1/qt1.pdf",
                                 host_policy.load())
    assert rule and rule["decided_at"] == "2026-09-24" and "WAF" in rule["reason"]
    assert "find_escholarship" in rule["backends"]
    backends = check_contracts.run_round.load_backends()
    assert check_contracts.host_policy_contract_errors(backends) == []
    enabled = {name: {**cfg, "enabled": True} for name, cfg in backends.items()}
    assert check_contracts.host_policy_contract_errors(enabled) == [
        "find_escholarship: backend for suspended host escholarship.org must be disabled"
    ]
