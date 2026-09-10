import hashlib
import tarfile

import pytest

import backup_corpus as backup


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("corpus", "registry", "manifest", "scripts"):
        (root / name).mkdir()
    (root / "corpus" / ".ruleset").write_text("none\n")
    (root / "corpus" / "sample.md").write_text("Concrete structure 測定\n")
    (root / "registry" / "eligibility.json").write_text("{}\n")
    (root / "manifest" / "curated.jsonl").write_text("{}\n")
    (root / "pruned_urls.txt").write_text("")
    (root / "requirements.lock").write_text("pyyaml==6.0\n")
    (root / "scripts" / "clean_corpus.py").write_text("raise SystemExit(0)\n")
    mount = tmp_path / "ssd"
    mount.mkdir()
    monkeypatch.setattr(backup, "require_mount", lambda path: None)
    return root, mount


def test_roundtrip_and_repeated_backup_preserve_previous_copy(source):
    root, mount = source
    first = backup.backup(root, mount)
    archive = first / "corpus.tar.gz"
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (first / "SHA256SUMS").read_text() == f"{expected}  corpus.tar.gz\n"
    with tarfile.open(archive) as stream:
        assert stream.extractfile("corpus/sample.md").read() == (root / "corpus/sample.md").read_bytes()
        assert stream.extractfile("corpus/.ruleset").read() == b"none\n"
        assert "registry/eligibility.json" in stream.getnames()
        assert "manifest/curated.jsonl" in stream.getnames()
        assert not any(name.startswith(("raw/", "text/")) for name in stream.getnames())
    (root / "corpus/sample.md").write_text("Updated structure\n")
    second = backup.backup(root, mount)
    assert second != first
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
    assert not list(first.parent.glob("*.partial"))


def test_missing_mount_writes_nothing(tmp_path):
    absent = tmp_path / "unmounted"
    with pytest.raises(RuntimeError, match="not mounted"):
        backup.require_mount(absent)
    assert not absent.exists()


def test_dry_run_writes_nothing(source):
    root, mount = source
    assert backup.backup(root, mount, dry_run=True) is None
    assert not list(mount.iterdir())


def test_incomplete_cleaning_refused(source):
    root, mount = source
    (root / "corpus/.ruleset").write_text("IN-PROGRESS none")
    with pytest.raises(RuntimeError, match="cleaning is incomplete"):
        backup.backup(root, mount)
    assert not list(mount.iterdir())


def test_insufficient_space_refused(source, monkeypatch):
    root, mount = source
    usage = backup.shutil.disk_usage(mount)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: usage._replace(free=0))
    with pytest.raises(RuntimeError, match="Not enough free space"):
        backup.backup(root, mount)
    assert not list(mount.iterdir())


@pytest.mark.parametrize("failure", ["checksum", "copy", "check"])
def test_failed_backup_never_published(source, monkeypatch, failure):
    root, mount = source
    if failure == "checksum":
        monkeypatch.setattr(backup, "hash_file", lambda path: "bad")
    elif failure == "copy":
        def fail(root, path):
            path.write_bytes(b"incomplete")
            raise OSError("disk disconnected")
        monkeypatch.setattr(backup, "write_archive", fail)
    else:
        (root / "scripts/clean_corpus.py").write_text("raise SystemExit(1)\n")
    with pytest.raises((RuntimeError, OSError, backup.subprocess.CalledProcessError)):
        backup.backup(root, mount)
    parent = mount / "nekaise-corpus-backups"
    assert not parent.exists() or all(path.name.endswith(".partial") for path in parent.iterdir())


def test_symlink_input_refused(source):
    root, mount = source
    (root / "corpus/link.md").symlink_to(root / "corpus/sample.md")
    with pytest.raises(RuntimeError, match="link or special"):
        backup.backup(root, mount)
