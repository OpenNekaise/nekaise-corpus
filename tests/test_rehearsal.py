"""The stage 4 step 5 rehearsal (ADR 0001): one throwaway PostgreSQL-authoritative checkout taken
through everything step 6 relies on, end to end, with real processes:

discovery -> fetch -> prune -> clean -> every gate (the contracts gate recording the generation's
counters) -> promotion; a failing gate aborts; a round SIGKILLed mid-fetch is recovered (orphans
stopped, run aborted) and the next round promotes; the recoverability growth block refuses a
round on a cluster that archives no WAL; the generation-range review records a verdict over the
promoted range; the integrity sweeps (metadata recount against the recorded counters, artifact
re-verification) and the GC report find nothing wrong and delete nothing; the schema is RESTORED
into a temporary cluster and its fingerprint and canonical export equal the original's; and the
PostgreSQL -> FileStore ROLLBACK: the latest promoted generation exported as a verified legacy
layout, its payloads linked to their legacy paths, authority switched back to file, and a legacy
file-store round runs every file gate and commits.

The restore uses a physical base backup (pg_basebackup -X stream) when the test cluster allows
replication, else a logical dump of the schema (the throwaway test cluster runs wal_level=minimal;
the physical point-in-time drill of the LIVE base + WAL archive is pg_backup.py restore-test, run
weekly and recorded in the ADR). Opt-in: NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import pg_backup
import round_recovery
import store
import store_authority
from runids import rid
from staged_world import DSN, World, entry, git, kill

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")


def ok(result) -> None:
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]


def dsn_socket(dsn: str) -> tuple[str, str]:
    parts = dict(p.split("=", 1) for p in dsn.split())
    return parts["host"], parts["dbname"]


def full_counters(world):
    import verify_generation
    with world.store().read() as view:
        restrictions, _ = store.pinned_policy(view)
        return verify_generation.full_counters(view, restrictions), view.generation


def contracts_counters(world, run_id):
    (text,), = [r for r in world.q("SELECT detail_text FROM gate_receipts WHERE run_id = %s AND "
                                   "gate = 'contracts' AND verdict = 'passed'", [run_id])]
    return json.loads(text)["report"]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def test_the_step5_rehearsal(world, tmp_path):
    p = world.payloads
    p.bodies["ost-r-thin"] = b"tiny"
    seeds = [entry(p, "ost-r-0"), entry(p, "ost-r-1"), entry(p, "ost-r-thin")]
    world.build(seeds)
    root = world.root

    # 1. a whole round: discovery -> fetch -> prune -> clean -> gates -> promotion
    world.finder([entry(p, "ost-r-new")])
    r1 = rid("rh-1")
    ok(world.run("--run-id", r1))
    assert world.run_row(r1)["status"] == "promoted" and world.generation() == 0
    with world.store().read() as view:
        assert set(view.get_manifest(["ost-r-0", "ost-r-1", "ost-r-new", "ost-r-thin"])) == {
            "ost-r-0", "ost-r-1", "ost-r-new"}                 # the thin one was pruned
        ledger = [r["id"] for r in view.scan(store.Table.LEDGER, limit=10).rows]
    assert ledger == ["ost-r-thin"]
    report = contracts_counters(world, r1)
    counters, g = full_counters(world)
    assert report["counter_mode"] == "full" and report["counters"] == counters and g == 0

    # 2. a failing gate aborts; 3. a killed round is recovered; the next round promotes
    world.finder([])
    world.commit("a failing test", **{"tests__test_bad.py": "def test_bad():\n    assert False\n"})
    bad = rid("rh-bad")
    assert world.run("--run-id", bad).returncode == 1
    assert world.run_row(bad)["status"] == "aborted"
    world.commit("fix", **{"tests__test_bad.py": "def test_bad():\n    assert True\n"})
    p.hold["ost-r-late"] = threading.Event()
    world.finder([entry(p, "ost-r-late")])
    dead = rid("rh-dead")
    proc = world.start("--run-id", dead)
    world.wait_for(lambda: "ost-r-late" in p.waiting, what="the held download")
    kill(proc)
    orphans = round_recovery.round_processes(dead)
    assert orphans
    p.release()
    ok(world.run("--recover", "latest"))
    assert world.run_row(dead)["status"] == "aborted"
    assert not any(round_recovery._alive(pid) for pid in orphans)
    r2 = rid("rh-2")
    ok(world.run("--run-id", r2))
    assert world.generation() == 1
    report = contracts_counters(world, r2)
    counters, _ = full_counters(world)
    assert report["counter_mode"] == "delta" and report["counters"] == counters
    assert report["counters"]["documents"] == 4

    # 4. the recoverability block: this cluster archives no WAL, so growth is refused
    refused = world.run("--run-id", rid("rh-blocked"),
                        env={"NEKAISE_TEST_WAIVE_RECOVERABILITY": "0"})
    assert refused.returncode == 1 and "growth blocked: recoverability" in refused.stderr
    assert world.run_row(rid("rh-blocked")) is None

    # 5. the generation-range review over what was promoted
    ev = world.run("evidence", script="generation_review.py")
    ok(ev)
    evidence = json.loads(ev.stdout)
    assert evidence["range"] == [0, 1]
    ok(world.run("record", "--through", "1", "--verdict", "ok", "--reviewer", "rehearsal",
                 "--evidence-digest", evidence["digest"], "--summary", "rehearsal",
                 script="generation_review.py"))
    assert world.q("SELECT reviewed_through, endorsed_through FROM review_state") == [(1, 1)]

    # 6. integrity sweeps and the GC report (in-place: nothing deleted)
    ok(world.run("metadata", "--seconds", "300", script="integrity_sweep.py"))
    ok(world.run("artifacts", "--seconds", "300", script="integrity_sweep.py"))
    sweep = json.loads((root / "workspace" / "integrity-sweep.json").read_text())
    assert sweep["metadata"]["failure_count"] == 0 and sweep["metadata"]["recorded"] == "equal"
    assert sweep["artifacts"]["failure_count"] == 0 and sweep["artifacts"]["checked"] > 0
    files_before = sorted(p.relative_to(root) for p in (root / "artifacts").rglob("*")
                          if p.is_file())
    ok(world.run(script="artifact_gc.py"))
    gc = json.loads((root / "workspace" / "artifact-gc-report.json").read_text())
    assert gc["dry_run"] and gc["candidates"]["count"] == 0
    assert files_before == sorted(p.relative_to(root) for p in (root / "artifacts").rglob("*")
                                  if p.is_file())

    # 7. restore the schema into a temporary cluster: same fingerprint, same canonical export
    restored = restore_schema(world, tmp_path / "restore")
    try:
        import store_pg
        rst = store_pg.PgStore(root, dsn=restored["dsn"], schema=world.schema, create=False)
        with world.store().read() as a, rst.read() as b:
            store.export(tmp_path / "export-live", view=a)
            store.export(tmp_path / "export-restored", view=b)
        assert (json.loads((tmp_path / "export-live" / "EXPORT.json").read_text()) ==
                json.loads((tmp_path / "export-restored" / "EXPORT.json").read_text()))
        assert restored["fingerprint_equal"], restored["mismatches"]
    finally:
        restored["stop"]()

    # 8. rollback: export the latest promoted generation, link payloads, switch to file, and
    # run a legacy file-store round over it
    rollback(world, tmp_path / "rollback")


def restore_schema(world, work: Path) -> dict:
    """A copy of the world's schema in a new temporary cluster: a physical base backup
    (-X stream) of the test cluster when it allows replication, else a logical dump of the
    schema. Compares the recovery fingerprint of the schema before and after."""
    host, db = dsn_socket(DSN)
    with pg_backup.connect(host, db, autocommit=True) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        live = pg_backup.fingerprint(conn, [world.schema])
        conn.execute("ROLLBACK")
        physical = conn.execute("SELECT current_setting('wal_level') <> 'minimal' AND "
                                "current_setting('max_wal_senders')::int > 0").fetchone()[0]
    work.mkdir(parents=True)
    started = time.monotonic()
    data = work / "data"
    if physical:
        base = work / "base"
        subprocess.run([str(pg_backup.PGBIN / "pg_basebackup"), "-h", host, "-D", str(base),
                        "-Ft", "-z", "-X", "stream", "-c", "fast"], check=True,
                       capture_output=True)
        pg_backup.extract(base, data)
        sock = pg_backup.start_instance(work, data, conf="", timeout=600)
        target_db = db
    else:
        subprocess.run([str(pg_backup.PGBIN / "initdb"), "-D", str(data), "-A", "trust",
                        "--locale=C.UTF-8", "-E", "UTF8"], check=True, capture_output=True)
        sock = pg_backup.start_instance(work, data, conf="", timeout=600)
        target_db = "restored"
        subprocess.run([str(pg_backup.PGBIN / "createdb"), "-h", str(sock), target_db],
                       check=True, capture_output=True)
        dump = subprocess.run([str(pg_backup.PGBIN / "pg_dump"), "-h", host, "-d", db, "-n",
                               world.schema, "-Fc"], check=True, capture_output=True).stdout
        subprocess.run([str(pg_backup.PGBIN / "pg_restore"), "-h", str(sock), "-d", target_db,
                        "--exit-on-error"], input=dump, check=True, capture_output=True)
    with pg_backup.connect(str(sock), target_db, autocommit=True) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        got = pg_backup.fingerprint(conn, [world.schema])
        conn.execute("ROLLBACK")
    mismatches = pg_backup.diff(live, got)
    print(json.dumps({"restore": "physical base backup (-X stream)" if physical else
                      "logical dump of the schema", "seconds": round(time.monotonic() - started, 1),
                      "summary": pg_backup.summary(got)}))
    return {"dsn": f"host={sock} dbname={target_db}", "physical": physical,
            "fingerprint_equal": not mismatches, "mismatches": mismatches,
            "stop": lambda: pg_backup.stop_instance(data)}


def rollback(world, target: Path) -> None:
    import rollback_export
    root = world.root
    st = world.store()
    meta = rollback_export.export(st, target, log=lambda *_: None)
    assert meta["identical"] and meta["generation"] == world.generation()
    payloads = rollback_export.link_payloads(st, root, log=lambda *_: None)
    assert payloads["linked"] > 0   # (verify_payloads passed first, or it would have raised)
    # fence: the database half first, then the host record (lifting the cutover fence)
    epoch = st.set_authority("file", root=root, reason="rehearsal rollback")
    store_authority.write_record(root, "file", reason="rehearsal rollback", epoch=epoch,
                                 lift_fence=True)
    # the verified tree replaces the tracked layout; configuration stays the checkout's
    for rel in ("manifest", "pruned_urls.txt"):
        dst = root / rel
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
    for path in (root / "registry").glob("*.yaml"):
        path.unlink()
    shutil.copytree(target / "manifest", root / "manifest")
    shutil.copy2(target / "pruned_urls.txt", root / "pruned_urls.txt")
    for src in (target / "registry").iterdir():
        dst = root / "registry" / src.name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        elif src.name not in store.CONFIG_FILES:
            shutil.copy2(src, dst)
        else:
            assert src.read_bytes() == dst.read_bytes(), src.name   # the generation's config
    (root / "README.md").write_text("# rehearsal\n\n<!-- STATS:START -->\n<!-- STATS:END -->\n")
    file_env = {k: v for k, v in world.env.items()
                if k not in ("NEKAISE_STORE", "NEKAISE_PG_DSN", "NEKAISE_PG_SCHEMA")}
    got = subprocess.run([os.sys.executable, str(root / "scripts" / "update_readme_stats.py")],
                         cwd=root, env=file_env, capture_output=True, text=True)
    ok(got)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "rollback to file authority: generation export")
    # a legacy file-store round over the rolled-back state: discovery, fetch, prune, clean,
    # README stats, every file gate (check, index, lint, contracts, tests), a commit
    head = git(root, "rev-parse", "HEAD")
    world.finder([entry(world.payloads, "ost-r-after")])
    run_id = rid("rh-file")
    got = subprocess.run([os.sys.executable, str(root / "scripts" / "run_round.py"),
                          "--commit", "--run-id", run_id], cwd=root,
                         env=file_env, capture_output=True, text=True, timeout=600)
    ok(got)
    for gate in ("check", "index", "lint", "contracts", "tests"):
        assert f"== {gate}: " in got.stdout, gate
    assert git(root, "rev-parse", "HEAD") != head
    assert "Corpus run: " + run_id in git(root, "log", "-1", "--format=%B")
    fs = store.open(root=root, backend="file")
    with fs.read() as view:
        ids = {r["id"] for r in view.scan(store.Table.MANIFEST, limit=100).rows}
    assert ids == {"ost-r-0", "ost-r-1", "ost-r-new", "ost-r-late", "ost-r-after"}
    assert {q.stem for q in (root / "corpus").glob("*.md")} == ids
    # the PostgreSQL schema is fenced: a store bound to its old record no longer writes
    with pytest.raises(store.StoreError):
        with world.store().writer(timeout=1):
            pass


def test_rollback_payloads_are_verified_before_anything_changes(world):
    """link_payloads verifies every raw/text claim (hash and size, regular readable files only)
    and corpus/ file by file against the generation BEFORE it links anything; any failure raises
    with nothing changed, so the authority switch never happens on unverified payloads."""
    import artifact_store
    import rollback_export
    p = world.payloads
    world.build([entry(p, "ost-v-0"), entry(p, "ost-v-1")])
    world.finder([])
    ok(world.run("--run-id", rid("rv-1")))
    root, st = world.root, world.store()
    assert rollback_export.verify_payloads(st, root, log=lambda *_: None)["ok"]
    with st.read() as view:
        rows = view.get_manifest(["ost-v-0", "ost-v-1"])
    local = artifact_store.LocalArtifacts(root)

    def refused(match):
        before = sorted(q.relative_to(root) for q in root.rglob("*") if "raw" in q.parts
                        or "text" in q.parts)
        with pytest.raises(rollback_export.ExportError, match="do not verify"):
            rollback_export.link_payloads(st, root, log=lambda *_: None)
        got = rollback_export.verify_payloads(st, root, log=lambda *_: None)
        assert any(match in f for f in got["failures"]), got["failures"]
        after = sorted(q.relative_to(root) for q in root.rglob("*") if "raw" in q.parts
                       or "text" in q.parts)
        assert before == after                       # nothing was linked
        assert store_authority.record_for(root).mode == "postgres"

    # (1) a held text version with same-size damage
    text_sha = rows["ost-v-0"]["text_sha256"]
    version = local.path("text", text_sha)
    good = version.read_bytes()
    os.chmod(version, 0o644)
    version.write_bytes(bytes([good[0] ^ 1]) + good[1:])
    refused("is damaged")
    version.write_bytes(good)
    # (2) a raw version gone and a DIRECTORY at its legacy path (never "legacy ok")
    raw_sha, raw_path = rows["ost-v-1"]["sha256"], rows["ost-v-1"]["raw_path"]
    rv = local.path("raw", raw_sha)
    saved = rv.read_bytes()
    os.chmod(rv, 0o644)
    rv.unlink()
    (root / raw_path).mkdir(parents=True)
    refused("not a readable regular file")
    # a regular legacy file with the wrong bytes is refused too
    (root / raw_path).rmdir()
    (root / raw_path).write_bytes(saved[:-1] + b"X")
    refused("does not hold its raw claim")
    (root / raw_path).write_bytes(saved)             # the right bytes: accepted as legacy
    assert rollback_export.verify_payloads(st, root, log=lambda *_: None)["ok"]
    # (3) corpus/ tampered, or holding a document the generation does not have
    member = root / "corpus" / "ost-v-0.md"
    body = member.read_bytes()
    member.unlink()
    member.write_bytes(body + b"tampered")
    refused("does not hold its cleaned claim")
    member.unlink()
    member.write_bytes(body)
    (root / "corpus" / "stray.md").write_text("not a member")
    refused("document files")
    (root / "corpus" / "stray.md").unlink()
    out = rollback_export.link_payloads(st, root, log=lambda *_: None)
    assert out["linked"] >= 1
    # (4) collect-all: EVERY cleaned view is verified, a classified one included
    import materialize
    stamp = materialize.view_dir(root, "nc") / materialize.STAMP
    saved_stamp = stamp.read_bytes() if stamp.exists() else None
    if saved_stamp is not None:
        stamp.unlink()
    got = rollback_export.verify_payloads(st, root, log=lambda *_: None)
    assert not got["ok"]
    assert any("collection/nc/corpus/ is not a complete materialization" in f
               for f in got["failures"]), got["failures"]
    if saved_stamp is not None:
        stamp.write_bytes(saved_stamp)
