"""Immutable local artifact versions (ADR 0001 stage 4 step 3, scripts/artifact_store.py) and the
materialization lock/stamp protocol (scripts/materialize.py) — the parts that need no database.

Crash injection covers every durability boundary of a write (after the bytes are written, after
the file is fsynced, after the content address is linked, after its directory is fsynced), both
as an exception (the writer unwinds) and as a real process kill (nothing unwinds): an address is
either absent or complete, a retry converges to the same version, and a stray temporary file is
all that is ever left behind."""
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import artifact_store
import materialize
import store
from artifact_store import LocalArtifacts

REPO = Path(__file__).resolve().parents[1]
DATA = "測定は 2019 年 3 月 14（暖房期）に行った。\nConcrete C25/30 25 30 2400 31\n".encode() * 50


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def incoming(root: Path) -> list[str]:
    d = root / "artifacts" / ".incoming"
    return sorted(os.listdir(d)) if d.exists() else []


# --- the write protocol ------------------------------------------------------------------------------

def test_put_writes_one_read_only_content_addressed_version(tmp_path):
    local = LocalArtifacts(tmp_path)
    art = local.put_bytes("text", DATA)
    assert art == artifact_store.Artifact("text", sha(DATA), len(DATA),
                                          f"artifacts/text/{sha(DATA)[:2]}/{sha(DATA)[2:4]}/"
                                          f"{sha(DATA)}")
    path = tmp_path / art.locator
    assert path.read_bytes() == DATA
    assert stat.S_IMODE(path.stat().st_mode) == 0o444      # never written again
    assert incoming(tmp_path) == []
    # the same bytes again: the same version, nothing rewritten
    inode = path.stat().st_ino
    assert local.put_bytes("text", DATA) == art and path.stat().st_ino == inode
    # stages are separate identities
    assert local.put_bytes("corpus", DATA).locator.startswith("artifacts/corpus/")
    assert local.verify("text", art.sha256, len(DATA)) and not local.verify("raw", art.sha256)


def test_streamed_put_hashes_while_writing(tmp_path):
    local = LocalArtifacts(tmp_path)
    chunks = [DATA[i:i + 7] for i in range(0, len(DATA), 7)]
    assert local.put_stream("raw", iter(chunks)).sha256 == sha(DATA)
    src = tmp_path / "src.bin"
    src.write_bytes(DATA)
    assert local.put_file("raw", src).sha256 == sha(DATA)


def test_identity_validation():
    with pytest.raises(artifact_store.ArtifactError):
        artifact_store.local_locator("pdf", "a" * 64)
    for bad in ("A" * 64, "a" * 63, None, 5, "g" * 64):
        with pytest.raises(artifact_store.ArtifactError):
            artifact_store.check_identity("raw", bad)
    with pytest.raises(artifact_store.ArtifactError):
        LocalArtifacts(Path("/nonexistent")).put_bytes("nope", b"x")


@pytest.mark.parametrize("point", ["written", "synced", "linked", "published"])
def test_a_crash_at_every_boundary_leaves_no_partial_version(tmp_path, monkeypatch, point):
    local = LocalArtifacts(tmp_path)
    path = local.path("raw", sha(DATA))

    def crash(p):
        if p == point:
            raise KeyboardInterrupt(f"killed at {p}")
    monkeypatch.setattr(artifact_store, "_crash", crash)
    with pytest.raises(KeyboardInterrupt):
        local.put_bytes("raw", DATA)
    if point in ("written", "synced"):
        assert not path.exists()                  # never linked: no address at all
    else:
        assert path.read_bytes() == DATA          # linked only after the fsync: complete
    assert incoming(tmp_path) == []               # the unwinding writer dropped its temp name
    monkeypatch.setattr(artifact_store, "_crash", lambda p: None)
    assert local.put_bytes("raw", DATA).sha256 == sha(DATA)
    assert path.read_bytes() == DATA


KILL_CHILD = r"""
import os, sys
sys.path.insert(0, "scripts")
import artifact_store
point = sys.argv[2]
def crash(p):
    if p == point:
        os._exit(9)
artifact_store._crash = crash
artifact_store.LocalArtifacts(sys.argv[1]).put_bytes("raw", sys.stdin.buffer.read())
"""


@pytest.mark.parametrize("point", ["written", "synced", "linked", "published"])
def test_a_killed_writer_leaves_no_partial_version_and_its_temp_is_swept(tmp_path, point):
    got = subprocess.run([sys.executable, "-c", KILL_CHILD, str(tmp_path), point], input=DATA,
                         capture_output=True, cwd=REPO)
    assert got.returncode == 9, got.stderr
    local = LocalArtifacts(tmp_path)
    path = local.path("raw", sha(DATA))
    assert path.exists() == (point in ("linked", "published"))
    if path.exists():
        assert path.read_bytes() == DATA
    left = incoming(tmp_path)
    assert len(left) == 1                          # nothing unwound: the temp name stays
    assert local.sweep_incoming() == 1             # its writer is gone
    assert incoming(tmp_path) == []
    assert local.put_bytes("raw", DATA).sha256 == sha(DATA)
    assert path.read_bytes() == DATA


GROUP = [DATA + bytes([i]) for i in range(5)]


def test_a_group_commit_makes_every_version_durable_at_once(tmp_path, monkeypatch):
    local = LocalArtifacts(tmp_path)
    syncs = []
    monkeypatch.setattr(artifact_store, "sync_filesystem", lambda p: syncs.append(Path(p)))
    local.put_bytes("corpus", GROUP[0])                      # one version exists already
    pending = [artifact_store.write_pending(tmp_path, "corpus", d, os.getpid()) for d in GROUP]
    assert pending[0].tmp is None and all(p.tmp for p in pending[1:])
    assert not any(local.has("corpus", p.sha256) for p in pending[1:])   # nothing addressed yet
    arts = local.commit(pending)
    assert [a.sha256 for a in arts] == [sha(d) for d in GROUP]
    assert len(syncs) == 2                                   # before linking and after
    assert all(local.read_bytes("corpus", sha(d)) == d for d in GROUP)
    assert incoming(tmp_path) == []
    assert local.commit([]) == [] and len(syncs) == 2


@pytest.mark.parametrize("point", ["written", "group-synced", "group-linked", "group-published"])
def test_a_crashed_group_commit_leaves_complete_versions_or_none(tmp_path, monkeypatch, point):
    local = LocalArtifacts(tmp_path)

    def crash(p):
        if p == point:
            raise KeyboardInterrupt(p)
    monkeypatch.setattr(artifact_store, "_crash", crash)
    with pytest.raises(KeyboardInterrupt):
        pending = [artifact_store.write_pending(tmp_path, "corpus", d) for d in GROUP]
        local.commit(pending)
    for d in GROUP:
        path = local.path("corpus", sha(d))
        assert not path.exists() or path.read_bytes() == d  # an address is only ever complete
        if point in ("written", "group-synced"):
            assert not path.exists()
    assert incoming(tmp_path)                              # stray temporaries only
    monkeypatch.setattr(artifact_store, "_crash", lambda p: None)
    local.commit([artifact_store.write_pending(tmp_path, "corpus", d) for d in GROUP])
    assert all(local.read_bytes("corpus", sha(d)) == d for d in GROUP)
    for e in os.scandir(local.base / ".incoming"):
        os.utime(e.path, (1, 1))                             # the crashed writer's, now stale
    local.sweep_incoming()
    assert incoming(tmp_path) == []


def test_pending_versions_cross_a_real_process_pool(tmp_path):
    """The cleaner's workers are processes: a pending version must survive pickling (the first
    Pending, a bare tuple subclass, did not — found by the step-3 review benchmark)."""
    import pickle
    from concurrent.futures import ProcessPoolExecutor

    import clean_corpus
    p = artifact_store.Pending("corpus", "a" * 64, 3, None)
    assert pickle.loads(pickle.dumps(p)) == p
    src = tmp_path / "t.md"
    src.write_bytes(b"# t\n\n---\n\nPage 12\nReal prose about ventilation.\n")
    with ProcessPoolExecutor(max_workers=1) as pool:
        [res] = pool.submit(clean_corpus._clean_many_versioned,
                            [("t", str(src), ["page_markers"], str(tmp_path), os.getpid())]
                            ).result()
    pending = res[4]
    [art] = LocalArtifacts(tmp_path).commit([pending])
    assert LocalArtifacts(tmp_path).read_bytes("corpus", art.sha256) == \
        b"# t\n\n---\n\nReal prose about ventilation.\n"


def test_sweep_keeps_a_live_writers_temp(tmp_path):
    local = LocalArtifacts(tmp_path)
    mine = local._tmp_name()
    mine.write_bytes(b"in progress")
    assert local.sweep_incoming() == 0 and mine.exists()
    os.utime(mine, (1, 1))                         # ... unless it is stale
    assert local.sweep_incoming() == 1


def test_a_damaged_existing_version_is_never_overwritten(tmp_path):
    local = LocalArtifacts(tmp_path)
    art = local.put_bytes("raw", DATA)
    path = tmp_path / art.locator
    os.chmod(path, 0o644)
    path.write_bytes(DATA[:10])                    # simulated damage
    with pytest.raises(artifact_store.ArtifactError, match="never overwritten"):
        local.put_bytes("raw", DATA)
    assert path.read_bytes() == DATA[:10]          # left for investigation
    assert not local.verify("raw", art.sha256)


def test_adopt_preserves_a_file_without_copying(tmp_path):
    local = LocalArtifacts(tmp_path)
    legacy = tmp_path / "corpus" / "doc.md"
    legacy.parent.mkdir()
    legacy.write_bytes(DATA)
    art = local.adopt("corpus", legacy)
    assert art.sha256 == sha(DATA)
    assert os.path.samefile(legacy, local.path("corpus", art.sha256))   # the same inode
    legacy.unlink()                                 # the name can go; the version stays
    assert local.read_bytes("corpus", art.sha256) == DATA
    assert incoming(tmp_path) == []


def test_review_same_size_damage_is_detected_by_hash_everywhere(tmp_path):
    """Review P1 #2: an existing address is accepted only with the right hash — for a put, an
    adoption (whose source must then be kept) and a group commit."""
    local = LocalArtifacts(tmp_path)
    path = local.path("corpus", sha(DATA))
    path.parent.mkdir(parents=True)
    path.write_bytes(bytes(len(DATA)))                    # same size, wrong bytes
    legacy = tmp_path / "corpus" / "x.md"
    legacy.parent.mkdir()
    legacy.write_bytes(DATA)
    for attempt in (lambda: local.put_bytes("corpus", DATA),
                    lambda: local.adopt("corpus", legacy),
                    lambda: local.commit([artifact_store.write_pending(tmp_path, "corpus",
                                                                       DATA)])):
        with pytest.raises(artifact_store.ArtifactError, match="does not hold the bytes"):
            attempt()
    assert legacy.read_bytes() == DATA and path.read_bytes() == bytes(len(DATA))


def test_review_publication_fsyncs_the_whole_directory_chain_once_per_process(tmp_path,
                                                                             monkeypatch):
    """Review P1 #3: publishing into existing directories (left by a writer that crashed before
    its sync) still fsyncs every ancestor entry up to the root — once per process."""
    local = LocalArtifacts(tmp_path)
    leaf = local.path("raw", sha(DATA)).parent
    leaf.mkdir(parents=True)                              # created, never synced
    artifact_store._DURABLE_DIRS.clear()
    synced = []
    real = artifact_store._fsync_dir
    monkeypatch.setattr(artifact_store, "_fsync_dir",
                        lambda p: (synced.append(Path(p).resolve()), real(p)))
    local.put_bytes("raw", DATA)
    chain = {leaf, leaf.parent, leaf.parent.parent, local.base, tmp_path}
    assert {p.resolve() for p in chain} <= set(synced)
    synced.clear()
    local.put_bytes("raw", DATA + b"!")                   # another leaf, same ancestors
    other = local.path("raw", sha(DATA + b"!")).parent.resolve()
    assert local.base.resolve() not in synced and tmp_path.resolve() not in synced
    assert other in synced


def test_barrier_fsyncs_each_directory_once(tmp_path):
    local = LocalArtifacts(tmp_path)
    arts = [local.put_bytes("raw", bytes([i]) * 3) for i in range(5)]
    paths = [tmp_path / a.locator for a in arts] * 2
    assert artifact_store.barrier(paths, tmp_path) == len({p.parent for p in paths})


# --- claims ---------------------------------------------------------------------------------------

def test_claims_and_their_changes():
    row = {"id": "a", "raw_path": "raw/s/a.pdf", "sha256": "1" * 64, "text_path": "text/a.md",
           "text_sha256": None, "corpus_path": None, "corpus_sha256": "2" * 64}
    assert artifact_store.claim(row, "raw") == ("raw/s/a.pdf", "1" * 64)
    assert artifact_store.claim(row, "text") == ("text/a.md", None)
    assert artifact_store.claim(row, "corpus") is None          # JSON null path: no claim
    assert artifact_store.changed_claims(row, row) == []
    moved = {**row, "text_sha256": "3" * 64}
    assert artifact_store.changed_claims(moved, row) == [("text", "text/a.md", "3" * 64)]
    assert [c[0] for c in artifact_store.changed_claims(row, None)] == ["raw", "text"]
    # exact JSON equality: 1 and 1.0 and true are different claims
    assert not artifact_store.same_claim(("p", 1), ("p", 1.0))
    assert not artifact_store.same_claim(("p", 1), ("p", True))


def test_versioned_access_resolves_by_identity_then_legacy_path(tmp_path):
    access = artifact_store.VersionedAccess(tmp_path)
    (tmp_path / "text").mkdir()
    (tmp_path / "text" / "a.md").write_bytes(b"legacy text")
    art = access.local.put_bytes("text", b"new text")
    row = {"id": "a", "text_path": "text/a.md", "text_sha256": art.sha256}
    assert access.read_bytes(row, "text") == b"new text"           # the immutable version
    legacy = {"id": "a", "text_path": "text/a.md", "text_sha256": "f" * 64}
    assert access.read_bytes(legacy, "text") == b"legacy text"     # a committed path
    assert access.path({"id": "a", "text_path": "../etc/passwd"}, "text") is None
    assert access.path({"id": "a", "text_path": "/etc/passwd"}, "text") is None
    assert not access.exists({"id": "a"}, "text")


def test_file_store_views_keep_the_legacy_path(tmp_path):
    from test_store_contract import write_config
    write_config(tmp_path)
    st = store.FileStore(tmp_path)
    with st.read() as v:
        assert artifact_store.for_view(v, tmp_path) is None


# --- the materialization protocol ----------------------------------------------------------------

def test_acquire_refuses_anything_but_a_complete_stamp_of_that_generation(tmp_path):
    d = tmp_path / "corpus"
    with pytest.raises(materialize.MaterializeError, match="no stamp"):
        with materialize.acquire(d, generation=3):
            pass
    materialize._write_stamp(d, {"state": "refreshing", "dataset": "u", "generation": 2,
                                 "target": 3})
    with pytest.raises(materialize.MaterializeError, match="refreshing"):
        with materialize.acquire(d, generation=3):
            pass
    (d / materialize.STAMP).write_text("{not json")
    with pytest.raises(materialize.MaterializeError, match="invalid"):
        with materialize.acquire(d, generation=3):
            pass
    materialize._write_stamp(d, {"state": "complete", "dataset": "u", "generation": 3})
    with pytest.raises(materialize.MaterializeError, match="generation 3 of u, not 4"):
        with materialize.acquire(d, generation=4):
            pass
    with pytest.raises(materialize.MaterializeError, match="not 3 of v"):
        with materialize.acquire(d, generation=3, dataset="v"):
            pass
    with materialize.acquire(d, generation=3, dataset="u") as stamp:
        assert stamp["generation"] == 3
        # a refresh cannot take the directory while a consumer holds it ...
        with pytest.raises(materialize.MaterializeError, match="in use by a consumer"):
            with materialize._locked(d, exclusive=True, timeout=0):
                pass
        # ... while other consumers can
        with materialize.acquire(d, generation=3):
            pass
    with materialize._locked(d, exclusive=True, timeout=0):
        with pytest.raises(materialize.MaterializeError, match="being refreshed"):
            with materialize.acquire(d, generation=3):
                pass


def test_stamp_format_is_versioned(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / materialize.STAMP).write_text(json.dumps({"state": "complete", "generation": 1}))
    assert materialize.read_stamp(d) == {"state": "invalid"}
