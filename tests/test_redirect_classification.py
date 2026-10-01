"""A redirect can invalidate rights evidence without prohibiting collection."""
import hashlib
import sys

import pytest

import build_corpus as bc
import clean_corpus
import lint_registry
import pipeline_repo
import registry
import store
from test_openalex_review_fixes import curl_env, loader, pdf, row  # noqa: F401
from test_openalex_sim import fake_transport
from test_pipeline_store import TEXT


ORIGIN = "https://repo.example.org/records/1/files/paper.pdf"
DEST = "https://cdn.example.net/paper.pdf"


def source():
    return {**row(ORIGIN), "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "license_evidence": "licence of the original copy", "rights_verified_at": "2026-10-01",
            "selected_version": "acceptedVersion", "persistent_id": "https://doi.org/10.1234/test"}


@pytest.mark.parametrize("curl", [False, True])
def test_changed_copy_is_collected_without_inheriting_rights(monkeypatch, loader, curl):
    payload = b"%PDF-1.7 " + b"x" * 600
    if curl:
        fake_transport(monkeypatch, {ORIGIN: (403, {}, b"forbidden")})
        asked = curl_env(monkeypatch, {ORIGIN: (302, DEST, b""), DEST: (200, None, payload)})
    else:
        asked = fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""),
                                             DEST: pdf(payload)})
    rec = bc.download_one(source())
    assert rec["error"] is None
    assert rec["license"] == "unverified" and not rec.get("license_url")
    assert not rec.get("selected_version")
    assert rec["redirect_rights"]["original"]["license_evidence"] == source()["license_evidence"]
    assert rec["redirect_rights"]["reason"] == "redirect"
    assert rec["url"] == ORIGIN and rec["final_url"] == DEST
    assert rec["redirect_chain"] == [ORIGIN, DEST]
    assert rec["sha256"] == hashlib.sha256(payload).hexdigest()
    urls = [u for u, _ in asked] if curl else asked
    assert urls == [ORIGIN, DEST]


def test_repeated_fetch_does_not_nest_original_evidence(monkeypatch, loader):
    fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""), DEST: pdf()})
    first = bc.download_one(source())
    recipe = {k: first[k] for k in registry.FIELDS if k in first}
    second = bc.download_one(recipe)
    assert second["redirect_rights"]["original"] == first["redirect_rights"]["original"]
    assert second["license_evidence"] == first["license_evidence"]


@pytest.mark.parametrize("evidence", ["", "redirect: unknown", "a generic open grant"])
def test_unbound_unverified_family_evidence_still_fails_lint(evidence):
    entry = {**source(), "license": "unverified", "license_evidence": evidence}
    assert any("requires an evidenced" in e for e in lint_registry.entry_errors(entry, "test"))


@pytest.mark.parametrize("destination", ["https://www.jstage.jst.go.jp/a.pdf",
                                         "https://papers.ssrn.com/a.pdf"])
def test_unknown_intermediate_does_not_disable_destination_checks(monkeypatch, loader, destination):
    asked = fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""),
                                         DEST: (302, {"Location": destination}, b"")})
    rec = bc.download_one(source())
    assert asked == [ORIGIN, DEST] and rec["refused_hop"] == destination
    assert not rec["raw_path"]


def test_proven_same_copy_keeps_original_rights(monkeypatch, loader):
    dest = "https://repo.example.org/api/records/1/files/paper.pdf/content"
    fake_transport(monkeypatch, {ORIGIN: (302, {"Location": dest}, b""), dest: pdf()})
    rec = bc.download_one(source())
    for key in bc.REDIRECT_RIGHTS_FIELDS:
        assert rec.get(key) == source().get(key)
    assert "redirect_rights" not in rec


@pytest.mark.parametrize("body", [
    b'<html><input type="password"></html>',
    b'<html><title>Just a moment</title><script src="/cdn-cgi/challenge-platform/x"></script>',
    b'<html><title>Purchase access</title></html>',
])
def test_changed_copy_access_pages_never_reach_fallback_or_raw(monkeypatch, loader, body):
    asked = fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""),
                                         DEST: (200, {}, body)})
    monkeypatch.setattr(bc, "_curl_follow", lambda *_: pytest.fail("access barrier retried"))
    rec = bc.download_one(source())
    assert asked == [ORIGIN, DEST]
    assert "access" in rec["error"] and "rights" not in rec["error"]
    assert not rec["raw_path"] and rec["status"] == "failed"


@pytest.mark.parametrize("status, retry", [(403, False), (429, True), (503, True)])
def test_changed_destination_refusal_never_retries_with_curl(monkeypatch, loader, status, retry):
    fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""),
                                 DEST: (status, {}, b"access denied")})
    monkeypatch.setattr(bc, "_curl_follow", lambda *_: pytest.fail("access barrier retried"))
    rec = bc.download_one(source())
    assert rec["http_status"] == status and "access" in rec["error"]
    assert bool(rec.get("transient")) is retry and not rec["raw_path"]


def test_changed_copy_landing_page_is_not_a_pdf(monkeypatch, loader):
    fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""),
                                 DEST: (200, {}, b"<html>Article abstract and download links</html>")})
    rec = bc.download_one(source())
    assert "not-a-pdf" in rec["error"] and not rec["raw_path"]


def test_changed_copy_pipeline_classifies_registry_manifest_and_cleaned_view(tmp_path, monkeypatch):
    root = pipeline_repo.write_repo(tmp_path / "repo", entries=[source()], policy={})
    pipeline_repo.point(monkeypatch, root, policy={})
    fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""), DEST: pdf()})
    monkeypatch.setattr(bc, "extract_for", lambda *_: TEXT)
    monkeypatch.setattr(bc, "PACED_DELAY", 0)
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1"])
    bc.main()
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1", "--rules", "none"])
    clean_corpus.main()
    rec = pipeline_repo.manifest_rows(root)[source()["id"]]
    with store.FileStore(root).read() as view:
        entry = view.get_entries([rec["id"]])[rec["id"]]
    assert lint_registry.manifest_errors(rec, entry) == []
    assert lint_registry.entry_errors(entry, "registry") == []
    assert entry["license"] == rec["license"] == "unverified"
    assert not entry.get("license_url") and not entry.get("selected_version")
    assert "redirect" in entry["license_evidence"]
    assert not registry.is_default_corpus_eligible(rec, {})
    assert registry.is_collection_eligible(rec, {})
    assert rec["corpus_path"] == f"collection/unverified/corpus/{rec['id']}.md"
    for stage, digest in [("raw", "sha256"), ("text", "text_sha256"), ("corpus", "corpus_sha256")]:
        assert hashlib.sha256((root / rec[f"{stage}_path"]).read_bytes()).hexdigest() == rec[digest]
    assert not list((root / "corpus").glob("*.md"))
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--check"])
    clean_corpus.main()


def test_failed_checkpoint_cannot_commit_only_the_rights_change(tmp_path, monkeypatch):
    root = pipeline_repo.write_repo(tmp_path / "repo", entries=[source()], policy={})
    pipeline_repo.point(monkeypatch, root, policy={})
    fake_transport(monkeypatch, {ORIGIN: (302, {"Location": DEST}, b""), DEST: pdf()})
    monkeypatch.setattr(bc, "extract_for", lambda *_: TEXT)
    monkeypatch.setattr(bc, "PACED_DELAY", 0)
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1"])
    def fail_manifest(self, rows):
        raise OSError("manifest checkpoint unavailable")
    with monkeypatch.context() as m:
        m.setattr(store.WriteView, "upsert_manifest", fail_manifest)
        with pytest.raises(OSError, match="manifest checkpoint unavailable"):
            bc.main()
    with store.FileStore(root).read() as view:
        assert view.get_entries([source()["id"]])[source()["id"]] == source()
        assert view.get_manifest([source()["id"]]) == {}
