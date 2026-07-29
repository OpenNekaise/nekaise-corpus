"""Focused tests for host-specific download handshakes."""

from types import SimpleNamespace
import hashlib
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import urlparse

import build_corpus


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
    assert row["extractor_version"].startswith("build_corpus/2;")


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
