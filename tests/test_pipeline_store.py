"""The loader, pruner and cleaner write through the store (ADR 0001 stage 3, step 6).

Equivalence: for the same inputs each step leaves the tracked files (registry, manifest,
blocklist, prune ledger) byte-identical to its legacy path (tests/legacy_pipeline.py, the scripts
at 8dde58e2ba) — the store journal being the only new tracked metadata — and the same raw/,
text/ and corpus/ artifacts (cleaned artifact hashes unchanged). Also: transaction identities,
crash/failure recovery for each step, the prune quarantine inside a real round broker, an
architectural guard against direct file writers, and (with a test database) the same steps
against the PostgreSQL store."""
import ast
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import build_corpus
import clean_corpus
import legacy_pipeline
import pipeline_repo
import prune_corpus
import quality
import ops
import run_round
import store
import store_broker
from pipeline_repo import artifacts, entry_of, journal_runs, manifest_rows, tracked, write_repo
from runids import rid

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TEXT = ("Building energy simulation of HVAC systems, thermal comfort, ventilation, insulation "
        "and heat pump performance in residential and commercial buildings. ") * 60
HEADER = "# t\n\nsource: x\nlicense: y\ntopic: z\n\n---\n\n"
_REAL_STRFTIME = time.strftime
_FIXED = time.gmtime(1_790_000_000)


@pytest.fixture
def clock(monkeypatch):
    """Every timestamp (fetched_at, pruned_at, run ids, journal) from one fixed instant."""
    monkeypatch.setattr(time, "strftime", lambda fmt, t=None: _REAL_STRFTIME(fmt, _FIXED))


def twins(tmp_path, build) -> tuple[Path, Path]:
    """Two byte-identical repositories: `legacy` for the reference path, `new` for the store."""
    a = tmp_path / "legacy"
    build(a)
    b = tmp_path / "new"
    shutil.copytree(a, b)  # copy2: mtimes too (the cleaner's up-to-date check reads them)
    return a, b


# --- architecture ---------------------------------------------------------------------------------

FORBIDDEN = {
    ("registry", "write_manifest_rows"), ("registry", "remove_ids"),
    ("registry", "append_entries"), ("registry", "write_prune_ledger_rows"),
    ("registry", "prune_ledger_path"), ("registry", "load_manifest_rows"),
    ("registry", "load_entries"), ("registry", "load_prune_ledger_rows"),
    ("blocklist", "add"), ("blocklist", "PATH"), ("ops", "append_jsonl"),
    # policy comes from the view's pinned configuration (store.pinned_policy), not the tree
    ("registry", "load_eligibility"), ("registry", "load_host_policy"), ("host_policy", "load"),
}


@pytest.mark.parametrize("script", ["build_corpus.py", "prune_corpus.py", "clean_corpus.py"])
def test_steps_never_write_tracked_files_directly(script):
    """The loader, pruner and cleaner read and write registry, manifest, blocklist and ledger
    only through the store (a broker batch or their own writer's transaction)."""
    tree = ast.parse((SCRIPTS / script).read_text())
    used = {(n.value.id, n.attr) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert not used & FORBIDDEN, f"{script} uses {sorted(used & FORBIDDEN)}"
    for node in ast.walk(tree):  # and no literal path into the tracked layout
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith(("manifest/", "registry/", "pruned_urls")), \
                f"{script}: {node.value!r}"


# --- pruner ----------------------------------------------------------------------------------------

M = quality.metrics(TEXT)
SUSPENDED = {"susp.example.org": {"status": "suspended", "reason": "WAF",
                                  "decided_at": "2026-09-24"}}
DNS_ERROR = "HTTPSConnectionPool: Failed to resolve 'gone.example' (NameResolutionError)"


def prow(sid, **kw):
    return {"id": sid, "title": f"Title {sid}", "url": f"https://e.org/{sid}.pdf",
            "source": "osti", "license": "public-domain", "topic": "building_energy",
            "format": "pdf", "status": "ok", "http_status": 200, "sha256": f"sha-{sid}",
            "bytes": 10, "raw_path": f"raw/osti/{sid}.pdf", "text_path": f"text/{sid}.md",
            "text_chars": len(TEXT), "corpus_path": f"corpus/{sid}.md", "corpus_chars": 5,
            "error": None, "fetched_at": "2026-09-01T00:00:00Z", "quality": M, **kw}


def failed(sid, **kw):
    return prow(sid, status="failed", sha256=None, raw_path=None, text_path=None,
                corpus_path=None, quality=None, **kw)


PRUNE_ROWS = [
    prow("hand-a", title="Shared Title"),
    prow("ost-dup-title", title="Shared Title"),
    prow("ost-good"),
    {k: v for k, v in prow("ost-premetrics").items() if k != "quality"},
    {k: v for k, v in prow("ost-premetrics-thin").items() if k != "quality"},
    prow("ost-notext"),
    failed("ost-404", http_status=404, error="404 Client Error"),
    failed("ost-503", http_status=503, error="503 Server Error"),
    failed("ost-transient", http_status=202, error="challenge", transient=True,
           retry_attempts=1, first_failed_at=_REAL_STRFTIME("%Y-%m-%dT%H:%M:%SZ")),
    failed("ost-dns", http_status=None, error=DNS_ERROR, url="https://gone.example/x.pdf"),
    prow("ost-bytes-a", sha256="same-bytes", title="Bytes A"),
    prow("ost-bytes-b", sha256="same-bytes", title="Bytes B"),
    prow("ost-manifest-only", text_path="text/missing.md"),  # no registry entry
    prow("ost-susp", url="https://susp.example.org/x.pdf", text_path="text/none.md"),
    prow("ost-deferred", text_path="text/none2.md"),
]
DNS_LEDGER = [{"id": "ost-dns", "url": "https://gone.example/x.pdf", "reason": "failed",
               "error": DNS_ERROR, "run_id": f"old-{d}", "pruned_at": f"2026-09-0{d}T00:00:00Z"}
              for d in (1, 2, 3)]


def build_prune_repo(root: Path) -> None:
    write_repo(root, entries=[entry_of(r) for r in PRUNE_ROWS if r["id"] != "ost-manifest-only"],
               manifest=PRUNE_ROWS, blocklist=["https://e.org/old"], ledger=DNS_LEDGER,
               policy=SUSPENDED)
    for r in PRUNE_ROWS:
        for key, body in (("raw_path", "%PDF raw"), ("text_path", HEADER + TEXT),
                          ("corpus_path", HEADER + TEXT)):
            rel = r.get(key)
            if not rel or r["id"] == "ost-notext" and key == "text_path" or "none" in rel \
                    or "missing" in rel:
                continue
            if r["id"] == "ost-premetrics-thin" and key == "text_path":
                body = HEADER + "too short"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "workspace" / "fetch-deferred.json").write_text(
        json.dumps({"run_id": rid("rnd-p"), "ids": ["ost-deferred"]}) + "\n")


def run_prune(monkeypatch, root, *args):
    pipeline_repo.point(monkeypatch, root, policy=SUSPENDED)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply", *args])
    prune_corpus.main()


def test_prune_matches_the_legacy_pruner(tmp_path, monkeypatch, clock, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    a, b = twins(tmp_path, build_prune_repo)
    pipeline_repo.point(monkeypatch, a, policy=SUSPENDED)
    drop = legacy_pipeline.legacy_prune()
    run_prune(monkeypatch, b)
    out = capsys.readouterr().out

    assert drop == {"ost-dup-title": "dup-title", "ost-premetrics-thin": "thin",
                    "ost-notext": "no-text", "ost-404": "failed", "ost-503": "failed",
                    "ost-dns": "failed", "ost-bytes-b": "dup-bytes",
                    "ost-manifest-only": "no-text"}
    assert tracked(a) == tracked(b)  # registry, manifest, blocklist, ledger: identical bytes
    assert artifacts(a) == artifacts(b)  # the same raw/text/corpus files deleted
    assert "pruned 8 docs (7 registry entries removed, 7 urls blocklisted)" in out
    rows = manifest_rows(b)
    assert rows["ost-premetrics"]["quality"] == quality.metrics(TEXT)  # survivor metric persisted
    assert journal_runs(b) == [f"prune-{_REAL_STRFTIME('%Y%m%dT%H%M%SZ', _FIXED)}-"
                               + journal_runs(b)[0].rsplit("-", 1)[1].split(".")[0] + ".apply"]
    assert not quarantined(b)  # standalone: purged after the commit
    with store.FileStore(b).read() as v:  # tombstones carry the reason
        events = [e for e in v.scan(store.Table.EVENTS).rows if e["op"] == "delete"]
    assert {(e["table"], e["id"], e["reason"]) for e in events} >= {
        ("entries", "ost-404", "prune: failed"), ("manifest", "ost-bytes-b", "prune: dup-bytes")}


def test_a_prune_that_changes_nothing_records_no_transaction(tmp_path, monkeypatch, clock):
    rows = [prow("ost-good")]
    root = write_repo(tmp_path / "r", entries=[entry_of(r) for r in rows], manifest=rows,
                      policy=SUSPENDED)
    for key in ("raw_path", "text_path"):
        (root / rows[0][key]).parent.mkdir(parents=True, exist_ok=True)
        (root / rows[0][key]).write_text(HEADER + TEXT)
    before = tracked(root, journal=True)
    monkeypatch.setattr(prune_corpus, "deferred_ids", lambda: set())
    run_prune(monkeypatch, root)
    assert tracked(root, journal=True) == before


def test_prune_dry_run_writes_nothing(tmp_path, monkeypatch, clock, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    root = tmp_path / "r"
    build_prune_repo(root)
    before, files = tracked(root, journal=True), artifacts(root)
    pipeline_repo.point(monkeypatch, root, policy=SUSPENDED)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py"])
    prune_corpus.main()
    assert "dry run" in capsys.readouterr().out
    assert tracked(root, journal=True) == before and artifacts(root) == files


def quarantined(root: Path) -> list[str]:
    q = prune_corpus.quarantine_root(root)
    return sorted(p.name for p in q.iterdir()) if q.exists() else []


def _fail_commits(monkeypatch, exc=RuntimeError):
    def boom(self, view, digest):
        raise exc("injected commit failure")
    monkeypatch.setattr(store.FileStore, "_commit", boom)


def test_a_failed_prune_transaction_keeps_bytes_recoverable(tmp_path, monkeypatch, clock):
    """The transaction fails after the bytes moved aside: metadata is untouched, the bytes wait
    in the quarantine, and the next prune restores them first and then prunes normally."""
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    a, b = twins(tmp_path, build_prune_repo)
    run_prune(monkeypatch, a)  # the reference: an uninterrupted prune
    meta, files = tracked(b, journal=True), artifacts(b)
    with monkeypatch.context() as m:
        _fail_commits(m)
        with pytest.raises(RuntimeError, match="injected"):
            run_prune(m, b)
    assert tracked(b, journal=True) == meta  # nothing committed
    assert artifacts(b) != files  # the dropped documents' bytes are in the quarantine
    [qdir] = list(prune_corpus.quarantine_root(b).iterdir())
    assert json.loads((qdir / "record.json").read_text())["state"] == "moved"
    run_prune(monkeypatch, b)  # settles (restores), then prunes
    assert tracked(b) == tracked(a) and artifacts(b) == artifacts(a)
    assert not quarantined(b)


def test_a_prune_killed_after_its_commit_is_completed_by_the_next(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    a, b = twins(tmp_path, build_prune_repo)
    run_prune(monkeypatch, a)
    with monkeypatch.context() as m:
        m.setattr(prune_corpus, "mark_committed",
                  lambda qdir: (_ for _ in ()).throw(KeyboardInterrupt("killed")))
        with pytest.raises(KeyboardInterrupt):
            run_prune(m, b)
    assert tracked(b) == tracked(a)  # the transaction committed
    assert quarantined(b)
    monkeypatch.setattr(prune_corpus, "deferred_ids", lambda: {"ost-deferred"})
    run_prune(monkeypatch, b)  # rows absent: the quarantine is purged; nothing else to prune
    assert tracked(b) == tracked(a) and artifacts(b) == artifacts(a)
    assert not quarantined(b)


# --- prune inside a real round: the broker and the retained quarantine ---------------------------

def _child(root: Path, env: dict, module: str, *args, policy=None):
    return subprocess.run([sys.executable, "-c", pipeline_repo.CHILD, str(root),
                           json.dumps(policy) if policy else "", module, *args],
                          env=env, capture_output=True, text=True, cwd=SCRIPTS.parent)


NEW_DOC = prow("ost-new", text_path="text/ost-new-missing.md", corpus_path=None)

# A round's fetch, simulated: a new document (row, entry, raw bytes) written through the broker.
FETCH_CHILD = """
import json, sys
sys.path[:0] = ["scripts", "tests"]
import pipeline_repo, store, store_broker
root, row = sys.argv[1], json.loads(sys.argv[2])
pipeline_repo.point(None, root)
(pipeline_repo.Path(root) / row["raw_path"]).write_bytes(b"%PDF new in round")
with store_broker.step_session(store.FileStore(pipeline_repo.Path(root)), "fetch") as s:
    with s.batch("ckpt-0001") as b:
        b.insert_entries([pipeline_repo.entry_of(row)])
        b.upsert_manifest([row])
"""

# The round's prune, optionally killed at one of the quarantine states (TEST_PRUNE_CRASH):
#   moving   - after the first file moved, before the record says "moved"
#   moved    - every file moved, the transaction not submitted
#   after    - the transaction committed, the record still says "moved"
PRUNE_CHILD = """
import json, os, sys
sys.path[:0] = ["scripts", "tests"]
import pipeline_repo, prune_corpus, store_broker
root, policy = sys.argv[1], json.loads(sys.argv[2])
pipeline_repo.point(None, root, policy=policy)
crash = os.environ.get("TEST_PRUNE_CRASH")
if crash == "moving":
    real = os.replace
    def replace(src, dst):
        real(src, dst)
        if "prune-quarantine" in str(dst):
            os._exit(9)
    prune_corpus.os.replace = replace
elif crash == "moved":
    store_broker.StepSession.submit = lambda *a, **k: os._exit(9)
elif crash == "after":
    prune_corpus.mark_committed = lambda qdir: os._exit(9)
sys.argv = ["prune_corpus.py", "--apply"]
prune_corpus.main()
"""


def _round_env(monkeypatch, root: Path):
    for d in ("README.md",):
        (root / d).write_text("readme\n")
    monkeypatch.setattr(run_round, "ROOT", root)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", root / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", root / "workspace")
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))
    monkeypatch.setattr(run_round, "run_verify_parallel", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(run_round.ops, "run_event",
                        lambda run_id, event, **kw: events.append((event, kw)))
    return events


def _fake_pipeline(root: Path, crash: str | None, fail_with):
    """run_command for the round: real children for fetch and prune; clean fails (or not)."""
    def run(step, cmd, env, run_id):
        if step == "fetch":
            out = subprocess.run([sys.executable, "-c", FETCH_CHILD, str(root),
                                  json.dumps(NEW_DOC)], env=env, capture_output=True, text=True,
                                 cwd=SCRIPTS.parent)
            assert out.returncode == 0, out.stderr
        elif step == "prune":
            out = subprocess.run([sys.executable, "-c", PRUNE_CHILD, str(root),
                                  json.dumps(SUSPENDED)],
                                 env={**env, "TEST_PRUNE_CRASH": crash or ""},
                                 capture_output=True, text=True, cwd=SCRIPTS.parent)
            if crash:
                assert out.returncode == 9, out.stderr
                raise fail_with("prune killed")
            assert out.returncode == 0, out.stderr
        elif step == "clean" and fail_with is not None:
            raise fail_with("clean failed")
    return run


def _round(monkeypatch, root, crash=None, fail_with=RuntimeError):
    monkeypatch.setattr(run_round, "run_command", _fake_pipeline(root, crash, fail_with))
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--skip-tests",
                                      "--allow-dirty", "--run-id", rid("rnd-p")])
    return run_round.main()


def _pre_round(tmp_path, monkeypatch):
    root = tmp_path / "r"
    build_prune_repo(root)
    events = _round_env(monkeypatch, root)
    return root, tracked(root), artifacts(root), events


def test_a_successful_round_deletes_what_its_prune_quarantined(tmp_path, monkeypatch):
    root, meta, files, _ = _pre_round(tmp_path, monkeypatch)
    assert _round(monkeypatch, root, fail_with=None) == 0
    assert not quarantined(root) and not run_round.ops.StateSnapshot.pending()
    rows = manifest_rows(root)
    assert "ost-404" not in rows and "ost-new" not in rows  # both pruned
    gone = {rel for rel in files if "ost-dup-title" in rel or "ost-premetrics-thin" in rel}
    assert gone and not gone & set(artifacts(root))
    assert "raw/osti/ost-new.pdf" not in artifacts(root)  # pruned in the round that fetched it
    assert [r.rsplit(".", 1)[1] for r in journal_runs(root)] == ["ckpt-0001", "apply"]


@pytest.mark.parametrize("crash", [None, "moving", "moved", "after"],
                         ids=["committed", "moving", "moved", "after-commit"])
def test_round_rollback_settles_the_prune_quarantine_for_old_and_new_documents(
        tmp_path, monkeypatch, crash):
    """The round fails after (or while) its prune moved bytes aside, whatever the quarantine's
    recorded state: rollback restores the pre-round metadata, puts back every file whose
    document the restored state holds, discards the bytes of the document new in this round,
    and only then discards the snapshot."""
    root, meta, files, events = _pre_round(tmp_path, monkeypatch)
    assert _round(monkeypatch, root, crash=crash) == 1
    assert tracked(root) == meta
    extra = set(artifacts(root)) - set(files)
    if crash == "moving":  # the interrupted move never reached the new document's bytes
        assert extra <= {"raw/osti/ost-new.pdf"}
    else:
        assert not extra
    assert {k: v for k, v in artifacts(root).items() if k in files} == files
    assert not quarantined(root) and not run_round.ops.StateSnapshot.pending()
    assert ("state_rolled_back", {}) in events


def _recover(entry: str, monkeypatch, root: Path, run_id: str) -> None:
    """Recover an interrupted round through one of the shared routine's entrypoints:
    `run_round --recover`, or the maintainer's automatic recovery inside a maintenance window
    (its window writer; scripts/round_recovery.py either way)."""
    if entry == "recover":
        monkeypatch.setattr(sys, "argv", ["run_round.py", "--recover", run_id])
        assert run_round.main() == 0
        return
    import maintainer
    monkeypatch.setattr(maintainer, "ROOT", root)
    st = store.FileStore(root)
    with st.writer() as w, maintainer.window_writer(st, w):
        assert maintainer.recover_pending_round() == run_id


RECOVERED = {"recover": ("run_recovered", {}),
             "maintainer": ("run_recovered", {"recovered_by": "ai_maintainer"})}


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
@pytest.mark.parametrize("crash", [None, "moving", "moved", "after"],
                         ids=["committed", "moving", "moved", "after-commit"])
def test_recover_settles_the_prune_quarantine_of_an_interrupted_round(tmp_path, monkeypatch,
                                                                      crash, entry):
    root, meta, files, events = _pre_round(tmp_path, monkeypatch)
    with pytest.raises(KeyboardInterrupt):  # the round process itself dies: no rollback
        _round(monkeypatch, root, crash=crash, fail_with=KeyboardInterrupt)
    assert run_round.ops.StateSnapshot.pending() == [rid("rnd-p")]
    assert quarantined(root)
    _recover(entry, monkeypatch, root, rid("rnd-p"))
    assert tracked(root) == meta
    assert {k: v for k, v in artifacts(root).items() if k in files} == files
    assert set(artifacts(root)) - set(files) <= ({"raw/osti/ost-new.pdf"}
                                                  if crash == "moving" else set())
    assert not quarantined(root) and not run_round.ops.StateSnapshot.pending()
    assert RECOVERED[entry] in events


def test_an_unfinished_settlement_keeps_the_round_recoverable(tmp_path, monkeypatch, capsys):
    root, meta, files, events = _pre_round(tmp_path, monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(prune_corpus, "settle_quarantine",
                  lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
        assert _round(monkeypatch, root) == 1
        assert run_round.ops.StateSnapshot.pending() == [rid("rnd-p")]  # not discarded
        assert quarantined(root)
        assert any(e == "rollback_failed" for e, _ in events)
        monkeypatch.setattr(sys, "argv", ["run_round.py", "--recover", rid("rnd-p")])
        assert run_round.main() == 1  # recover reports failure and keeps the snapshot too
        assert "snapshot is kept" in capsys.readouterr().err
        assert run_round.ops.StateSnapshot.pending() == [rid("rnd-p")]
    assert run_round.main() == 0  # the cause is gone: recovery completes
    assert tracked(root) == meta
    assert {k: v for k, v in artifacts(root).items() if k in files} == files
    assert not quarantined(root) and not run_round.ops.StateSnapshot.pending()


def test_a_round_settles_a_standalone_prunes_leftover_before_fetching(tmp_path, monkeypatch,
                                                                     clock):
    """A standalone prune killed after its commit leaves its bytes aside; the next round deletes
    them (their rows are gone) before its own fetch runs."""
    root, _, _, _ = _pre_round(tmp_path, monkeypatch)
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    with monkeypatch.context() as m:
        m.setattr(prune_corpus, "mark_committed",
                  lambda qdir: (_ for _ in ()).throw(KeyboardInterrupt("killed")))
        with pytest.raises(KeyboardInterrupt):
            run_prune(m, root)
    monkeypatch.delenv("NEKAISE_RUN_ID")
    assert quarantined(root)
    seen = {}

    def record(step, cmd, env, run_id):
        seen.setdefault(step, quarantined(root))
    monkeypatch.setattr(run_round, "run_command", record)
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--skip-tests",
                                      "--allow-dirty", "--run-id", rid("rnd-2")])
    assert run_round.main() == 0
    assert seen["fetch"] == []


def test_the_settlement_rule_is_per_file(tmp_path):
    """Restore a file when its document's row exists in the settled state and its path is free;
    otherwise delete it (the row is gone, or a newer file took the path)."""
    root = tmp_path / "r"
    for rel in ("raw/a.pdf", "raw/b.pdf", "raw/c.pdf"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    rows = [{"id": "a", "raw_path": "raw/a.pdf"}, {"id": "b", "raw_path": "raw/b.pdf"},
            {"id": "c", "raw_path": "raw/c.pdf"}]
    qdir = prune_corpus.quarantine_files(root, "t1", rows, None)
    (root / "raw" / "c.pdf").write_text("newer")
    got = prune_corpus.settle_quarantine(root, qdir, lambda ids: {"a", "c"} & set(ids))
    assert got == {"restored": 1, "discarded": 2}
    assert (root / "raw" / "a.pdf").read_text() == "raw/a.pdf"
    assert not (root / "raw" / "b.pdf").exists()
    assert (root / "raw" / "c.pdf").read_text() == "newer"
    assert not qdir.exists()


def test_a_standalone_prune_waits_for_no_broker_and_takes_its_own_writer(tmp_path, monkeypatch,
                                                                       clock):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    root = tmp_path / "r"
    build_prune_repo(root)
    st = store.FileStore(root)
    with st.writer():  # somebody else holds the round lock
        with pytest.raises(RuntimeError, match="is held"):
            run_prune(monkeypatch, root, "--lock-timeout", "0")
    assert not journal_runs(root)


# --- loader ----------------------------------------------------------------------------------------

class Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content = status, content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise build_corpus.requests.HTTPError(f"{self.status_code} Client Error")


def lentry(sid, fmt="md", **kw):
    return {"id": sid, "title": f"Doc {sid}", "url": f"https://h{sid[-1]}.example/{sid}.{fmt}",
            "source": "osti", "license": "public-domain", "topic": "building_energy",
            "format": fmt, **kw}


def body_for(sid: str) -> bytes:
    if sid.startswith("ost-l-dup") or sid == "ost-l-reuse":
        return ("Shared body about heat pumps and ventilation.\n" * 20).encode()
    return (f"Document {sid}: building insulation and HVAC controls.\n" * 20).encode()


LOAD_ENTRIES = sorted(
    [lentry(f"ost-l-{n:02d}") for n in range(31)]
    + [lentry("ost-l-dup-a"), lentry("ost-l-dup-b"), lentry("ost-l-reuse"),
       lentry("ost-l-drift"), lentry("ost-l-timeout"), lentry("ost-l-404"),
       lentry("ost-l-have"), lentry("ost-l-pointer", license="proprietary-internal")],
    key=lambda e: e["id"])  # file order == id order: the store reads entries by id


def build_load_repo(root: Path, failures: bool = True) -> None:
    template_body = body_for("ost-l-reuse")
    import hashlib
    have = {**entry_of(lentry("ost-l-have")), "status": "ok", "http_status": 200,
            "sha256": hashlib.sha256(template_body).hexdigest(), "bytes": len(template_body),
            "raw_path": "raw/osti/ost-l-have.md", "text_path": "text/ost-l-have.md",
            "text_chars": 5, "corpus_path": None, "corpus_chars": 0, "error": None,
            "fetched_at": "2026-09-01T00:00:00Z", "extractor_version": "old-extractor",
            "quality": {"template": True}}
    drift = {**have, **entry_of(lentry("ost-l-drift")), "sha256": "0" * 64,
             "raw_path": "raw/osti/ost-l-drift.md", "text_path": "text/ost-l-drift.md"}
    timeout = {**entry_of(lentry("ost-l-timeout")), "status": "failed", "http_status": None,
               "error": "read timed out", "transient": True, "retry_attempts": 2,
               "first_failed_at": "2026-09-20T00:00:00Z"}
    if failures:
        write_repo(root, entries=LOAD_ENTRIES, manifest=[have, drift, timeout], policy={})
    else:  # every fetch succeeds: a re-run has nothing left to retry
        write_repo(root, entries=[e for e in LOAD_ENTRIES
                                  if e["id"] not in ("ost-l-timeout", "ost-l-404")],
                   manifest=[have, drift], policy={})
    for rel, data in (("raw/osti/ost-l-have.md", template_body),
                      ("text/ost-l-have.md", HEADER.encode() + template_body)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


def serve(monkeypatch):
    def get(url, **_kw):
        sid = url.rsplit("/", 1)[1].rsplit(".", 1)[0]
        if sid == "ost-l-timeout":
            raise build_corpus.requests.Timeout("read timed out")
        if sid == "ost-l-404":
            return Resp(404, b"missing")
        return Resp(200, body_for(sid))
    monkeypatch.setattr(build_corpus.requests, "get", get)
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})


def run_load(monkeypatch, root, *args):
    pipeline_repo.point(monkeypatch, root, policy=None)  # the repository's own host policy
    serve(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1", *args])
    build_corpus.main()


def test_load_matches_the_legacy_loader(tmp_path, monkeypatch, clock, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-l"))
    a, b = twins(tmp_path, build_load_repo)
    pipeline_repo.point(monkeypatch, a, policy=None)
    serve(monkeypatch)
    legacy_pipeline.legacy_load(workers=1)
    run_load(monkeypatch, b)
    out = capsys.readouterr().out

    assert tracked(a) == tracked(b)
    assert artifacts(a) == artifacts(b)
    assert (a / "workspace" / "fetch-deferred.json").read_bytes() == \
        (b / "workspace" / "fetch-deferred.json").read_bytes()
    rows = manifest_rows(b)
    assert rows["ost-l-reuse"]["extractor_version"] == "old-extractor"  # reused the template
    assert rows["ost-l-timeout"]["retry_attempts"] == 3  # retry bookkeeping carried
    assert "ost-l-pointer" not in rows
    assert "1 DRIFTED" in out
    done = 38  # every eligible entry except the one already held
    runs = journal_runs(b)
    assert len(runs) == -(-done // build_corpus.CHECKPOINT_EVERY)  # one per 25 results + last
    assert [r.rsplit(".", 1)[1] for r in runs] == ["ckpt-0001", "ckpt-0002"]
    assert len({r.rsplit(".", 1)[0] for r in runs}) == 1  # one session, distinct batch names


def test_reextract_writes_bounded_batches_with_the_legacy_bytes(tmp_path, monkeypatch, clock):
    def build(root):
        rows = []
        for n in range(5):
            sid = f"crawl-page-{n}"
            rows.append({**entry_of(lentry(sid, fmt="html")), "status": "ok",
                         "raw_path": f"raw/x/{sid}.html", "text_chars": 1,
                         "text_path": f"text/{sid}.md"})
            (root / "raw" / "x").mkdir(parents=True, exist_ok=True)
            (root / "raw" / "x" / f"{sid}.html").write_text(f"<main><p>Page {n} text</p></main>")
        write_repo(root, entries=[entry_of(r) for r in rows], manifest=rows)
    a, b = twins(tmp_path, build)
    pipeline_repo.point(monkeypatch, a, policy=None)
    legacy_pipeline.legacy_load(reextract=True, fmt="html")
    monkeypatch.setattr(build_corpus, "REEXTRACT_BATCH_ROWS", 2)
    run_load(monkeypatch, b, "--reextract", "--format", "html")
    assert tracked(a) == tracked(b) and artifacts(a) == artifacts(b)
    assert [r.rsplit(".", 1)[1] for r in journal_runs(b)] == [
        "reextract-0001", "reextract-0002", "reextract-0003"]


def test_verify_reads_only(tmp_path, monkeypatch, clock, capsys):
    root = tmp_path / "r"
    build_load_repo(root)
    before = tracked(root, journal=True)
    run_load(monkeypatch, root, "--verify")
    assert "verify: 1 match | 0 sha256 MISMATCH | 1 not downloaded" in capsys.readouterr().out
    assert tracked(root, journal=True) == before


def test_a_failed_checkpoint_leaves_committed_checkpoints_and_the_rerun_completes(
        tmp_path, monkeypatch, clock):
    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-l"))
    a, b = twins(tmp_path, lambda root: build_load_repo(root, failures=False))
    run_load(monkeypatch, a)
    real = store.FileStore._commit
    calls = {"n": 0}

    def second_fails(self, view, digest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(self, view, digest)
    with monkeypatch.context() as m:
        m.setattr(store.FileStore, "_commit", second_fails)
        with pytest.raises(OSError):
            run_load(m, b)
    st = store.FileStore(b)
    assert not st.pending_transactions()
    assert len(journal_runs(b)) == 1  # the first checkpoint stands: its 25 results
    with st.read() as v:
        [receipt] = v.scan(store.Table.EVENTS).rows
        assert receipt["counts"] == {"manifest": {"upsert": 25}}
    run_load(monkeypatch, b)  # fetches only what the failed run did not record
    assert tracked(b) == tracked(a) and artifacts(b) == artifacts(a)


def test_host_caps_now_pick_in_entry_id_order(monkeypatch):
    """Documented deviation: the store keeps no registry file order, so a binding per-run host
    cap defers by entry id (the legacy loader deferred by shard file order)."""
    monkeypatch.setattr(build_corpus, "HOST_RUN_CAP", {"publications.ibpsa.org": 2})
    srcs = [{"id": sid, "url": f"https://publications.ibpsa.org/{sid}.pdf"}
            for sid in ("ibp-c", "ibp-a", "ibp-b")]
    kept, deferred = build_corpus.cap_per_host(sorted(srcs, key=lambda s: s["id"]))
    assert [s["id"] for s in kept] == ["ibp-a", "ibp-b"] and deferred == ["ibp-c"]


# --- cleaner ---------------------------------------------------------------------------------------

RESTRICT = {"no-train": {"status": "restricted", "match": {"id_prefix": "pat-cn"},
                         "backends": ["find_patents"], "reason": "r", "decided_at": "2026-09-01",
                         "evidence_urls": ["https://e.org"]}}


def build_clean_repo(root: Path) -> None:
    rows = []
    for n in range(6):
        sid = f"ost-c-{n}"
        body = f"Page 12\nHeat pump sizing {n} .......... 4\nReal prose about HVAC {n}.\n"
        (root / "text").mkdir(parents=True, exist_ok=True)
        (root / "text" / f"{sid}.md").write_text(HEADER + body)
        rows.append({**entry_of(lentry(sid)), "status": "ok", "text_path": f"text/{sid}.md",
                     "text_chars": len(body), "corpus_path": None, "corpus_chars": 0})
    rows.append({**entry_of(lentry("ost-c-missing")), "status": "ok",
                 "text_path": "text/ost-c-missing.md", "corpus_path": None, "corpus_chars": 0})
    (root / "text" / "pat-cn1.md").write_text(HEADER + "restricted")
    rows.append({**entry_of(lentry("pat-cn1")), "status": "ok", "text_path": "text/pat-cn1.md",
                 "corpus_path": "corpus/pat-cn1.md", "corpus_chars": 10,
                 "corpus_sha256": "x", "cleaner_version": "clean_corpus/2;rules=none"})
    write_repo(root, entries=[entry_of(r) for r in rows], manifest=rows, restrictions=RESTRICT,
               policy={})
    (root / "corpus").mkdir()
    (root / "corpus" / "pat-cn1.md").write_text("old restricted copy")
    (root / "corpus" / "ost-gone.md").write_text("orphan")


def run_clean(monkeypatch, root, *args):
    pipeline_repo.point(monkeypatch, root, policy=None)  # the repository's own host policy
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1", *args])
    clean_corpus.main()


@pytest.mark.parametrize("rules", ["none", "toc_leaders,page_markers"])
def test_clean_matches_the_legacy_cleaner_then_patches_only_changes(tmp_path, monkeypatch, clock,
                                                                   rules):
    a, b = twins(tmp_path, build_clean_repo)
    pipeline_repo.point(monkeypatch, a, policy=None)
    legacy_pipeline.legacy_clean(rules_spec=rules)
    run_clean(monkeypatch, b, "--rules", rules)
    assert tracked(a) == tracked(b)
    assert artifacts(a) == artifacts(b)  # corpus files, their hashes, and the stamp
    assert (b / "corpus" / ".ruleset").read_text() == f"{rules}\n"
    assert (b / "workspace" / "policy-excluded-corpus" / "pat-cn1.md").exists()
    assert [r.rsplit(".", 1)[1] for r in journal_runs(b)] == ["restricted-0001", "meta-0001"]
    # an incremental re-run with the same ruleset changes nothing: no transaction
    before = tracked(b, journal=True)
    run_clean(monkeypatch, b)
    assert tracked(b, journal=True) == before
    # a new document: only its row is patched, and still exactly like the legacy cleaner
    for root in (a, b):
        (root / "text" / "ost-c-new.md").write_text(HEADER + "New prose.\n")
    for root, runner in ((a, None), (b, run_clean)):
        pipeline_repo.point(monkeypatch, root, policy=None)
        st = store.FileStore(root)
        with st.writer() as w:
            with st.transaction("add-new", expected_version=st.version(), writer=w) as tx:
                tx.upsert_manifest([{**entry_of(lentry("ost-c-new")), "status": "ok",
                                     "text_path": "text/ost-c-new.md", "corpus_path": None,
                                     "corpus_chars": 0}])
        if runner is None:
            legacy_pipeline.legacy_clean()
        else:
            runner(monkeypatch, root)
    assert tracked(a) == tracked(b) and artifacts(a) == artifacts(b)
    with store.FileStore(b).read() as v:
        last = v.scan(store.Table.EVENTS).rows[-1]
    assert last["op"] == "commit" and last["counts"] == {"manifest": {"update": 1}}


def test_a_failed_metadata_batch_leaves_the_stamp_in_progress_and_the_rerun_repairs(
        tmp_path, monkeypatch, clock, capsys):
    a, b = twins(tmp_path, build_clean_repo)
    run_clean(monkeypatch, a, "--rules", "toc_leaders")
    monkeypatch.setattr(clean_corpus, "METADATA_BATCH_ROWS", 2)
    real = store.FileStore._commit
    calls = {"n": 0}

    def third_fails(self, view, digest):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("killed between metadata batches")
        return real(self, view, digest)
    with monkeypatch.context() as m:
        m.setattr(store.FileStore, "_commit", third_fails)
        with pytest.raises(KeyboardInterrupt):
            run_clean(m, b, "--rules", "toc_leaders")
    assert (b / "corpus" / ".ruleset").read_text().startswith("IN-PROGRESS")
    assert len(journal_runs(b)) == 2  # the committed batches stand
    capsys.readouterr()
    with pytest.raises(SystemExit):
        run_clean(monkeypatch, b, "--check")
    assert "never finished" in capsys.readouterr().out
    run_clean(monkeypatch, b, "--rules", "toc_leaders")  # rebuilds, patches the rest
    assert tracked(b) == tracked(a) and artifacts(b) == artifacts(a)


# --- PostgreSQL: the same three steps against the other backend ------------------------------------

def _seed_postgres(pg, fs_root: Path) -> None:
    """Copy a file store's state into an empty PostgreSQL store (one transaction)."""
    rows = {}
    with store.FileStore(fs_root).read() as v:
        for table in (store.Table.ENTRIES, store.Table.MANIFEST, store.Table.LEDGER,
                      store.Table.BLOCKLIST):
            rows[table] = v.scan(table, limit=store.MAX_PAGE).rows
    with pg.writer() as w:
        with pg.transaction("seed", expected_version=pg.version(), writer=w) as tx:
            tx.insert_entries(rows[store.Table.ENTRIES])
            tx.upsert_manifest(rows[store.Table.MANIFEST])
            tx.ledger_append(rows[store.Table.LEDGER])
            tx.blocklist_add([r["url"] for r in rows[store.Table.BLOCKLIST]])


@pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"), reason="NEKAISE_PG_TEST_DSN not set")
@pytest.mark.parametrize("build,steps", [
    (build_load_repo, [("fetch", lambda m, r: run_load(m, r))]),
    (build_prune_repo, [("prune", lambda m, r: run_prune(m, r)),
                        ("clean", lambda m, r: run_clean(m, r, "--rules", "toc_leaders"))]),
], ids=["fetch", "prune-clean"])
def test_the_steps_against_postgres_match_the_file_store(tmp_path, monkeypatch, clock, build,
                                                         steps):
    """The same steps through the store API against PostgreSQL leave the same state (every table
    but the journal, whose run ids are per session) and the same artifacts."""
    import store_pg

    monkeypatch.setenv("NEKAISE_RUN_ID", rid("rnd-p"))
    fs_root, pg_root = tmp_path / "fs", tmp_path / "pg"
    build(fs_root)
    shutil.copytree(fs_root, pg_root)
    schema = f"t_{uuid.uuid4().hex[:12]}"
    pg = store_pg.PgStore(pg_root, dsn=os.environ["NEKAISE_PG_TEST_DSN"], schema=schema)
    try:
        pg.pin_config_from_files()
        _seed_postgres(pg, fs_root)
        for _name, step in steps:
            with monkeypatch.context() as m:
                step(m, fs_root)
            with monkeypatch.context() as m:
                m.setenv("NEKAISE_STORE", "postgres")
                m.setenv("NEKAISE_PG_DSN", os.environ["NEKAISE_PG_TEST_DSN"])
                m.setenv("NEKAISE_PG_SCHEMA", schema)
                step(m, pg_root)
        assert artifacts(fs_root) == artifacts(pg_root)
        with store.FileStore(fs_root).read() as a, pg.read() as b:
            ea = store.export(tmp_path / "ea", view=a)
            eb = store.export(tmp_path / "eb", view=b)
            ops_a = [(e["table"], e["op"], e["id"]) for e in a.scan(
                store.Table.EVENTS, limit=store.MAX_PAGE).rows if e["op"] != "commit"]
            ops_b = [(e["table"], e["op"], e["id"]) for e in b.scan(
                store.Table.EVENTS, limit=store.MAX_PAGE).rows
                if e["op"] != "commit" and e["run_id"] != "seed"]
        for name in ("entries.jsonl", "manifest.jsonl", "blocklist.txt", "ledger.jsonl"):
            assert ea.files[name] == eb.files[name], name
        # the same mutations (the loader records results in completion order, which varies)
        assert sorted(ops_a) == sorted(ops_b)
    finally:
        pg.drop()


# --- policy and stamp are read under the step's writer, from the pinned configuration -------------

def test_the_cleaner_reads_its_stamp_under_the_lock_and_policy_from_the_view(tmp_path,
                                                                           monkeypatch):
    root = tmp_path / "r"
    build_clean_repo(root)
    st = store.FileStore(root)
    held = []
    real = clean_corpus.stamped_ruleset
    monkeypatch.setattr(clean_corpus, "stamped_ruleset",
                        lambda: (held.append(st._lock_holder()), real())[1])
    pinned = []
    real_pinned = store.pinned_policy
    monkeypatch.setattr(store, "pinned_policy",
                        lambda view: (pinned.append(view), real_pinned(view))[1])
    run_clean(monkeypatch, root)
    assert held == [str(os.getpid())]  # the stamp was read while this run held the round lock
    assert len(pinned) == 1


@pytest.mark.parametrize("doc,bad", [
    ("eligibility.json", {"version": 1, "restrictions": {"x": {"match": {"id_prefix": "a"}}}}),
    ("host_policy.json", {"version": 1, "hosts": {"E.org": {"status": "suspended"}}}),
    ("host_policy.json", None),
])
def test_pinned_policy_fails_closed(tmp_path, doc, bad):
    root = write_repo(tmp_path / "r", policy={})
    path = root / "registry" / doc
    if bad is None:
        path.unlink()
    else:
        path.write_text(json.dumps(bad))
    with store.FileStore(root).read() as v:
        with pytest.raises(store.StoreError, match=doc.split(".")[0]):
            store.pinned_policy(v)


# --- invocation identities inside one broker (a maintenance window) -------------------------------

def test_two_invocations_in_one_window_never_collide(tmp_path, monkeypatch):
    """A maintenance window's broker serves several commands: each invocation carries its own
    token, so a second clean with different patches is not refused as a replay."""
    root = tmp_path / "r"
    build_clean_repo(root)
    st = store.FileStore(root)
    with st.writer(round_id="maint-w") as w:
        broker = store_broker.Broker(st, w, "maint-w")
        with broker.serving():
            env = ops.with_holder(dict(os.environ, **broker.env()), os.getpid(),
                                  st.workspace / ".corpus-round.lock", "maint-w")
            env.pop("NEKAISE_RUN_ID", None)
            for rules in ("none", "toc_leaders"):
                out = _child(root, env, "clean_corpus", "--workers", "1", "--rules", rules,
                             policy={})
                assert out.returncode == 0, out.stderr
    runs = journal_runs(root)
    assert len(runs) == 3 and len(set(runs)) == 3  # the second has no restricted rows left
    tokens = {r.split(".")[2].split("-")[0] for r in runs}
    assert len(tokens) == 2 and all(t.startswith("i") for t in tokens)
    assert all(r.startswith("maint-w.clean.i") for r in runs)
    assert [r.rsplit("-", 2)[-2:] for r in runs] == [["restricted", "0001"], ["meta", "0001"],
                                                     ["meta", "0001"]]


# --- a crash inside a checkpoint's commit: store recovery precedes the snapshot restore ----------

TWO_CHECKPOINTS = """
import json, sys
sys.path[:0] = ["scripts", "tests"]
import pipeline_repo, store, store_broker
root = sys.argv[1]
pipeline_repo.point(None, root)
st = store.FileStore(pipeline_repo.Path(root))
with store_broker.step_session(st, "fetch") as s:
    rows = s.view.get_manifest(["ost-good", "ost-premetrics"])
    for n, sid in enumerate(["ost-good", "ost-premetrics"], 1):  # both in manifest/reports.jsonl
        with s.batch(f"ckpt-{n:04d}") as b:
            b.upsert_manifest([{**rows[sid], "error": f"checkpoint {n}"}])
"""


def _crash_second_commit(monkeypatch):
    """The round process 'dies' inside the second transaction's commit: its first data write
    fails after the recovery marker was published, and the immediate rollback is lost (power
    loss), leaving the transaction prepared with the shard half-applied."""
    real_commit, real_write = store.FileStore._commit, store._write_durable
    real_rollback = store.FileStore._rollback
    state = {"commits": 0, "armed": False, "lost": False}

    def commit(self, view, digest):
        state["commits"] += 1
        state["armed"] = state["commits"] == 2
        return real_commit(self, view, digest)

    def write(path, data):
        if state["armed"] and "store-transactions" not in str(path):
            state["armed"] = False
            real_write(path, data)  # atomic: the file holds the post-image
            raise OSError("process died mid-commit")
        return real_write(path, data)

    def rollback(self, txn, meta):
        if not state["lost"]:
            state["lost"] = True
            return None
        return real_rollback(self, txn, meta)
    monkeypatch.setattr(store.FileStore, "_commit", commit)
    monkeypatch.setattr(store, "_write_durable", write)
    monkeypatch.setattr(store.FileStore, "_rollback", rollback)


def _checkpoint_round(monkeypatch, root, fail_with):
    def run(step, cmd, env, run_id):
        if step == "fetch":
            out = subprocess.run([sys.executable, "-c", TWO_CHECKPOINTS, str(root)], env=env,
                                 capture_output=True, text=True, cwd=SCRIPTS.parent)
            assert out.returncode != 0 and "process died mid-commit" in out.stderr
            assert [t.state for t in store.FileStore(root).pending_transactions()] == ["prepared"]
            raise fail_with("fetch failed")
    monkeypatch.setattr(run_round, "run_command", run)
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--skip-tests",
                                      "--allow-dirty", "--run-id", rid("rnd-c")])
    return run_round.main()


@pytest.mark.parametrize("entry", ["rollback", "recover", "maintainer"])
def test_a_crash_inside_a_later_checkpoint_commit_is_recovered_before_the_snapshot(
        tmp_path, monkeypatch, entry):
    root, meta, files, events = _pre_round(tmp_path, monkeypatch)
    _crash_second_commit(monkeypatch)
    if entry == "rollback":
        assert _checkpoint_round(monkeypatch, root, RuntimeError) == 1
    else:
        with pytest.raises(KeyboardInterrupt):  # the round process itself dies
            _checkpoint_round(monkeypatch, root, KeyboardInterrupt)
        assert run_round.ops.StateSnapshot.pending() == [rid("rnd-c")]
        _recover(entry, monkeypatch, root, rid("rnd-c"))
    assert tracked(root) == meta
    assert not store.FileStore(root).pending_transactions()
    assert not run_round.ops.StateSnapshot.pending()
    assert ("store_transaction_recovered",
            {"transaction": f"{rid('rnd-c')}.fetch.ckpt-0002", "action": "rolled_back"}) in events


# --- quarantine record formats ---------------------------------------------------------------

def _quarantine(root: Path, name: str, record: dict | None, files: dict[str, str]) -> Path:
    qdir = prune_corpus.quarantine_root(root) / name
    for rel, text in files.items():
        (qdir / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
        (qdir / "files" / rel).write_text(text)
    qdir.mkdir(parents=True, exist_ok=True)
    if record is not None:
        (qdir / "record.json").write_text(json.dumps(record))
    return qdir


def test_first_format_quarantines_are_settled_with_the_per_file_rule(tmp_path):
    """Records written by f1c77c0a66 ({"ids", "files"}) are settled, not dropped: each file is
    attributed to the id its name carries."""
    root = tmp_path / "r"
    files = {"raw/osti/ost-a.pdf": "a raw", "text/ost-a.md": "a text",
             "raw/osti/ost-b.1.pdf": "b raw", "corpus/ost-b.1.md": "b corpus"}
    qdir = _quarantine(root, "old", {"txn": "old", "run": None, "state": "moved",
                                     "ids": ["ost-a", "ost-b.1"], "files": sorted(files)}, files)
    got = prune_corpus.settle_quarantine(root, qdir, lambda ids: {"ost-a"} & set(ids))
    assert got == {"restored": 2, "discarded": 2}
    assert (root / "raw/osti/ost-a.pdf").read_text() == "a raw"
    assert (root / "text/ost-a.md").read_text() == "a text"
    assert not (root / "raw/osti/ost-b.1.pdf").exists() and not qdir.exists()


@pytest.mark.parametrize("record", [
    {"txn": "x", "state": "moved"},                                   # unknown format
    {"items": "not a list"},
    {"ids": ["ost-a"], "files": ["raw/osti/unrelated.pdf"]},          # unattributable file
    None,                                                             # files but no record
])
def test_unrecognized_quarantines_are_kept(tmp_path, record):
    root = tmp_path / "r"
    qdir = _quarantine(root, "odd", record, {"raw/osti/unrelated.pdf": "bytes"})
    with pytest.raises(prune_corpus.QuarantineError):
        prune_corpus.settle_quarantine(root, qdir, lambda ids: set(ids))
    assert (qdir / "files" / "raw/osti/unrelated.pdf").read_text() == "bytes"


def test_an_empty_quarantine_without_a_record_is_removed(tmp_path):
    root = tmp_path / "r"
    qdir = _quarantine(root, "empty", None, {})
    assert prune_corpus.settle_quarantine(root, qdir, lambda ids: set()) == {
        "restored": 0, "discarded": 0}
    assert not qdir.exists()
