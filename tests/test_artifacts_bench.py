"""Benchmark: stage 4 step 3 immutable artifact versions (ADR 0001) — the cost of writing
versions, of the claim contract at staging and sealing, of the artifact gate and of refreshing the
corpus/ materialization — on a synthetic projection of N documents in a throwaway schema of the
TEST database and a throwaway directory.

Opt-in — as a test: NEKAISE_PG_BENCH=1 (rows: NEKAISE_PG_BENCH_ROWS, default 200000) with
NEKAISE_PG_TEST_DSN; as a script:

    python tests/test_artifacts_bench.py --rows 1620000 [--dsn "host=... dbname=nekaise_test"]

Every staged workload runs twice, in a versioned run (claims checked, versions registered) and
in an unchecked run (the step-2 rules), so the difference is the price of the contract. Prints
one JSON report. Refuses the live database (dbname=nekaise)."""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifact_store  # noqa: E402
import materialize  # noqa: E402
import store_staging  # noqa: E402
from test_store_pg_staging_bench import Clock, _row, _stage, populate  # noqa: E402

SHA = "0" * 40
BATCH = 20_000


def _corpus(i: int, rev: int = 0) -> bytes:
    return (f"# doc-{i:08d}\n\nsource: x\n\n---\n\ncleaned body {i} revision {rev} "
            ).encode() + b"thermal comfort and ventilation " * 40


def _open(st, w, run_id, policy):
    return st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                       cleaning_ruleset="none", artifacts=policy)


def _finish(st, w, run_id, policy, clock, prefix):
    gates = ["artifacts"] if policy == "versioned" else ["tests"]
    frozen = st.freeze(w, run_id, required_gates=gates)
    if policy == "versioned":
        res = clock(f"{prefix}.verify_gate", artifact_store.verify_run, st, w, run_id)
        assert not res["failed"], res
    st.record_gate(w, frozen, gates[0], passed=True)
    return clock(f"{prefix}.promote", st.promote, w, frozen)


def cas(root: Path, clock: Clock) -> dict:
    local = artifact_store.LocalArtifacts(root)
    for i in range(2000):
        clock("cas.put_text_10k", local.put_bytes, "text", _corpus(i, 99) * 3)
    for i in range(20):
        clock("cas.put_raw_5mb", local.put_bytes, "raw", os.urandom(5 << 20))
    for i in range(200):
        clock("cas.put_existing_10k", local.put_bytes, "text", _corpus(i, 99) * 3)
    for g in range(4):   # the cleaner's way: 5 000 pending versions, one group commit
        clock("cas.group_write_and_commit_5000", lambda g=g: local.commit(
            [artifact_store.write_pending(root, "corpus", _corpus(i, 50 + g))
             for i in range(5000)]))
    return {"versions": 22020}


def checkpoints(st, root: Path, clock: Clock, policy: str) -> dict:
    """A loader's 16 checkpoints of 25 new documents, each with new raw and text versions."""
    local = artifact_store.LocalArtifacts(root)
    run_id = f"load-{policy}"
    with st.writer() as w:
        _open(st, w, run_id, policy)
        for c in range(16):
            rows = []
            for k in range(25):
                i = 10_000_000 + c * 25 + k + (0 if policy == "versioned" else 1000)
                r = _row(i)
                raw = local.put_bytes("raw", f"%PDF {i} {policy}".encode() * 50)
                text = local.put_bytes("text", _corpus(i, 7))
                rows.append({**r, "sha256": raw.sha256, "bytes": raw.size,
                             "text_sha256": text.sha256})
            clock(f"{policy}.checkpoint_25", _stage, st, w, run_id, "fetch", f"ckpt-{c:04d}",
                  lambda b, rows=rows: b.upsert_manifest(rows))
        gen = _finish(st, w, run_id, policy, clock, policy)
    return {"generation": gen}


def clean_batch(st, root: Path, clock: Clock, policy: str, n: int) -> dict:
    """A cleaner's patch of BATCH rows with new corpus versions (their raw and text claims are
    unchanged legacy claims: the seal looks each before-image up)."""
    local = artifact_store.LocalArtifacts(root)
    run_id = f"clean-{policy}"
    offset = 0 if policy == "versioned" else BATCH
    ids = [(i + offset) % n for i in range(BATCH)]
    t0 = time.monotonic()
    patch = {}
    # as the cleaner writes them: pending versions, made durable in groups (two syncfs each)
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        arts = local.commit([artifact_store.write_pending(root, "corpus", _corpus(i))
                             for i in chunk])
        for i, art in zip(chunk, arts):
            patch[f"doc-{i:08d}"] = {"corpus_path": f"corpus/doc-{i:08d}.md",
                                     "corpus_sha256": art.sha256, "corpus_chars": art.size,
                                     "cleaner_version": "clean_corpus/2;rules=none"}
    write_s = time.monotonic() - t0
    with st.writer() as w:
        _open(st, w, run_id, policy)
        clock(f"{policy}.clean_batch_{BATCH}", _stage, st, w, run_id, "clean", "meta-0001",
              lambda b: b.update_manifest_fields(patch))
        gen = _finish(st, w, run_id, policy, clock, policy)
    return {"generation": gen, "cas_write_s": round(write_s, 2)}


def materialization(st, root: Path, clock: Clock, n: int) -> dict:
    out = {}
    out["full"] = clock("mat.full_refresh", materialize.refresh, st, root)
    # a small round revising 400 cleaned documents, then the incremental refresh
    local = artifact_store.LocalArtifacts(root)
    patch = {}
    for i in range(0, BATCH, BATCH // 400):
        art = local.put_bytes("corpus", _corpus(i, 1))
        patch[f"doc-{i:08d}"] = {"corpus_sha256": art.sha256, "corpus_chars": art.size}
    with st.writer() as w:
        _open(st, w, "small", "versioned")
        _stage(st, w, "small", "clean", "meta-0001", lambda b: b.update_manifest_fields(patch))
        _finish(st, w, "small", "versioned", clock, "small")
    out["diff"] = clock("mat.diff_refresh_400", materialize.refresh, st, root)
    out["noop"] = clock("mat.diff_refresh_noop", materialize.refresh, st, root)
    return out


def run(dsn: str, n: int) -> dict:
    import store_pg
    if re.search(r"dbname=nekaise(\s|$)", dsn):
        raise SystemExit("refusing the live database: benchmark against nekaise_test")
    # a directory on the corpus's own disk (git-ignored workspace/): fsync and syncfs costs are
    # the point, and /tmp may be tmpfs
    base = Path(os.environ.get("NEKAISE_BENCH_DIR",
                               Path(__file__).resolve().parents[1] / "workspace"))
    root = base / f"nekaise-abench-{uuid.uuid4().hex[:8]}"
    (root / "registry").mkdir(parents=True)
    (root / "registry" / "backends.json").write_text(json.dumps(
        {"find_books": {"script": "find_books.py", "args": []}}))
    (root / "registry" / "eligibility.json").write_text(json.dumps(
        {"version": 1, "restrictions": {}}))
    (root / "registry" / "host_policy.json").write_text(json.dumps({"version": 1, "hosts": {}}))
    st = store_pg.PgStore(root, dsn=dsn, schema=f"abench_{uuid.uuid4().hex[:10]}")
    clock = Clock()
    try:
        st.pin_config_from_files()
        report = {"rows": n, "populate_s": round(populate(st, n), 1)}
        report["cas"] = cas(root, clock)
        for policy in ("unchecked", "versioned"):
            report[f"checkpoints_{policy}"] = checkpoints(st, root, clock, policy)
            report[f"clean_{policy}"] = clean_batch(st, root, clock, policy, n)
        report["materialize"] = materialization(st, root, clock, n)
        with st.writer() as w:
            store_staging.fold_all(st, w)
        report["timings"] = clock.report()
        report["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        return report
    finally:
        st.drop()
        shutil.rmtree(root, ignore_errors=True)


CLEAN_RULES = "toc_leaders,patent_id_soup,patent_furniture,site_chrome,ocr_debris,code_annotations"


def _text(i: int) -> bytes:
    return (f"# Building energy study {i}\n\nsource: https://e.org/doc-{i:08d}.pdf\nlicense: open\n"
            f"topic: building_energy\n\n---\n\nPage {i % 300}\nHeat pump sizing {i} .......... 4\n"
            f"Real prose about HVAC and ventilation in building {i}. ").encode() * 3


def clean_run(dsn: str, n: int, workers: int = 8) -> dict:
    """clean_corpus.build_versioned itself over n documents whose (legacy) text files all need
    cleaning under the production ruleset, inside a versioned staged run: wall time and the
    process's peak RSS (run one size per process to compare peaks)."""
    import clean_corpus
    import store_broker
    import store_pg
    if re.search(r"dbname=nekaise(\s|$)", dsn):
        raise SystemExit("refusing the live database: benchmark against nekaise_test")
    base = Path(os.environ.get("NEKAISE_BENCH_DIR",
                               Path(__file__).resolve().parents[1] / "workspace"))
    root = base / f"nekaise-cbench-{uuid.uuid4().hex[:8]}"
    (root / "registry").mkdir(parents=True)
    (root / "registry" / "backends.json").write_text(json.dumps(
        {"find_books": {"script": "find_books.py", "args": []}}))
    (root / "registry" / "eligibility.json").write_text(json.dumps(
        {"version": 1, "restrictions": {}}))
    (root / "registry" / "host_policy.json").write_text(json.dumps({"version": 1, "hosts": {}}))
    (root / "text").mkdir()
    schema = f"cbench_{uuid.uuid4().hex[:10]}"
    st = store_pg.PgStore(root, dsn=dsn, schema=schema)
    report = {"rows": n, "workers": workers}
    try:
        st.pin_config_from_files()
        import hashlib
        import store
        t0 = time.monotonic()
        with st._connect() as conn:
            with conn.cursor() as cur:
                with cur.copy("COPY manifest (id, row_text, url_norm, url_key, title_norm, "
                              "title_key, sha256, shard, topic_key) FROM STDIN") as cp:
                    for i in range(n):
                        data = _text(i)
                        (root / "text" / f"doc-{i:08d}.md").write_bytes(data)
                        r = {**_row(i), "text_sha256": hashlib.sha256(data).hexdigest()}
                        shard, topic, _ = store.legacy_manifest_key(r)
                        cp.write_row((r["id"], store.canonical_row(r), *store_pg._keys_for(r),
                                      r["sha256"], shard, topic))
            conn.commit()
            conn.execute("ANALYZE")
        report["populate_s"] = round(time.monotonic() - t0, 1)
        run_id = f"cbench-{uuid.uuid4().hex[:8]}"
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        with st.writer(round_id=run_id) as w:
            with store_broker.staged_round(st, w, run_id, producer_commit=SHA,
                                           extractor_version="x1",
                                           cleaning_ruleset=CLEAN_RULES) as rnd:
                os.environ.update({**rnd.broker.env(), "NEKAISE_STORE": "postgres",
                                   "NEKAISE_PG_DSN": dsn, "NEKAISE_PG_SCHEMA": schema,
                                   "NEKAISE_RUN_ID": run_id})
                clean_corpus.HERE = root
                sys.argv = ["clean_corpus.py", "--workers", str(workers)]
                t0 = time.monotonic()
                clean_corpus.main()
                report["build_versioned_s"] = round(time.monotonic() - t0, 1)
                rnd.freeze(["artifacts"])
                t0 = time.monotonic()
                result = rnd.verify_artifacts()
                report["artifact_gate_s"] = round(time.monotonic() - t0, 1)
                report["verified"] = result["verified"]
                rnd.promote()
        report["peak_rss_mb_before"] = round(rss0 / 1024)
        report["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        return report
    finally:
        for k in ("NEKAISE_STORE", "NEKAISE_PG_DSN", "NEKAISE_PG_SCHEMA", "NEKAISE_RUN_ID"):
            os.environ.pop(k, None)
        st.drop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(not (os.environ.get("NEKAISE_PG_BENCH") and
                         os.environ.get("NEKAISE_PG_TEST_DSN")),
                    reason="benchmark: set NEKAISE_PG_BENCH=1 and NEKAISE_PG_TEST_DSN")
def test_artifacts_benchmark():
    n = int(os.environ.get("NEKAISE_PG_BENCH_ROWS", "200000"))
    report = run(os.environ["NEKAISE_PG_TEST_DSN"], n)
    print(json.dumps(report, indent=1, default=str))
    assert report["materialize"]["diff"]["mode"] == "diff"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--clean", action="store_true",
                    help="benchmark clean_corpus.build_versioned alone over --rows documents "
                         "(one size per process: its peak RSS is the process's)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dsn", default=os.environ.get("NEKAISE_PG_TEST_DSN",
                                                    "host=/home/zengp/.local/share/nekaise-pg-test/run "
                                                    "dbname=nekaise_test"))
    args = ap.parse_args()
    if args.clean:
        print(json.dumps(clean_run(args.dsn, args.rows, args.workers), indent=1))
    else:
        print(json.dumps(run(args.dsn, args.rows), indent=1, default=str))
