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
import ops
import pipeline_repo
import prune_corpus
import quality
import run_round
import store
import store_broker
from pipeline_repo import artifacts, entry_of, journal_runs, manifest_rows, tracked, write_repo

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
               manifest=PRUNE_ROWS, blocklist=["https://e.org/old"], ledger=DNS_LEDGER)
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
        json.dumps({"run_id": "rnd-p", "ids": ["ost-deferred"]}) + "\n")


def run_prune(monkeypatch, root, *args):
    pipeline_repo.point(monkeypatch, root, policy=SUSPENDED)
    monkeypatch.setattr(sys, "argv", ["prune_corpus.py", "--apply", *args])
    prune_corpus.main()


def test_prune_matches_the_legacy_pruner(tmp_path, monkeypatch, clock, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-p")
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
    root = write_repo(tmp_path / "r", entries=[entry_of(r) for r in rows], manifest=rows)
    for key in ("raw_path", "text_path"):
        (root / rows[0][key]).parent.mkdir(parents=True, exist_ok=True)
        (root / rows[0][key]).write_text(HEADER + TEXT)
    before = tracked(root, journal=True)
    monkeypatch.setattr(prune_corpus, "deferred_ids", lambda: set())
    run_prune(monkeypatch, root)
    assert tracked(root, journal=True) == before


def test_prune_dry_run_writes_nothing(tmp_path, monkeypatch, clock, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-p")
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
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-p")
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
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-p")
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


@pytest.mark.parametrize("outcome", ["committed", "rolled_back"])
def test_round_prune_runs_as_one_broker_batch_and_the_round_settles_its_bytes(
        tmp_path, outcome):
    root = tmp_path / "r"
    build_prune_repo(root)
    files, meta = artifacts(root), tracked(root)
    st = store.FileStore(root)
    with st.writer(round_id="rnd-p") as w:
        broker = store_broker.Broker(st, w, "rnd-p")
        with broker.serving():
            env = ops.with_holder(dict(os.environ, **broker.env(), NEKAISE_RUN_ID="rnd-p"),
                                  os.getpid(), st.workspace / ".corpus-round.lock", "rnd-p")
            env.pop("NEKAISE_DISABLE_INDEX", None)
            out = _child(root, env, "prune_corpus", "--apply", policy=SUSPENDED)
        assert out.returncode == 0, out.stderr
        assert journal_runs(root) == ["rnd-p.prune.apply"]
        [qdir] = list(prune_corpus.quarantine_root(root).iterdir())
        assert qdir.name == "rnd-p.prune.apply"  # kept: the round has not ended yet
        assert json.loads((qdir / "record.json").read_text())["state"] == "committed"
        if outcome == "rolled_back":  # run_round restores the round's snapshot, then settles
            for rel in set(tracked(root, journal=True)) - set(meta):
                (root / rel).unlink()
            for rel, data in meta.items():
                (root / rel).write_bytes(data)
        run_round.settle_prune_quarantine(root, "rnd-p", outcome, st, w)
    assert not quarantined(root)
    if outcome == "rolled_back":
        assert artifacts(root) == files and tracked(root) == meta
    else:
        assert "raw/osti/ost-404.pdf" not in artifacts(root) and "ost-404" not in manifest_rows(root)


def test_a_standalone_prune_waits_for_no_broker_and_takes_its_own_writer(tmp_path, monkeypatch,
                                                                       clock):
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-p")
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
        write_repo(root, entries=LOAD_ENTRIES, manifest=[have, drift, timeout])
    else:  # every fetch succeeds: a re-run has nothing left to retry
        write_repo(root, entries=[e for e in LOAD_ENTRIES
                                  if e["id"] not in ("ost-l-timeout", "ost-l-404")],
                   manifest=[have, drift])
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
    pipeline_repo.point(monkeypatch, root, policy={})
    serve(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["build_corpus.py", "--workers", "1", *args])
    build_corpus.main()


def test_load_matches_the_legacy_loader(tmp_path, monkeypatch, clock, capsys):
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-l")
    a, b = twins(tmp_path, build_load_repo)
    pipeline_repo.point(monkeypatch, a, policy={})
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
    pipeline_repo.point(monkeypatch, a, policy={})
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
    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-l")
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
        assert len([e for e in v.scan(store.Table.EVENTS).rows if e["op"] == "upsert"]) == 25
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
    write_repo(root, entries=[entry_of(r) for r in rows], manifest=rows, restrictions=RESTRICT)
    (root / "corpus").mkdir()
    (root / "corpus" / "pat-cn1.md").write_text("old restricted copy")
    (root / "corpus" / "ost-gone.md").write_text("orphan")


def run_clean(monkeypatch, root, *args):
    pipeline_repo.point(monkeypatch, root, policy={})
    monkeypatch.setattr(sys, "argv", ["clean_corpus.py", "--workers", "1", *args])
    clean_corpus.main()


@pytest.mark.parametrize("rules", ["none", "toc_leaders,page_markers"])
def test_clean_matches_the_legacy_cleaner_then_patches_only_changes(tmp_path, monkeypatch, clock,
                                                                   rules):
    a, b = twins(tmp_path, build_clean_repo)
    pipeline_repo.point(monkeypatch, a, policy={})
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
        pipeline_repo.point(monkeypatch, root, policy={})
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
        last = [e for e in v.scan(store.Table.EVENTS).rows if e["op"] == "update"][-1]
    assert last["id"] == "ost-c-new"


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

    monkeypatch.setenv("NEKAISE_RUN_ID", "rnd-p")
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
