"""Focused tests for host-specific download handshakes."""

from types import SimpleNamespace

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
