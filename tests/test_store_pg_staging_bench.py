"""Benchmark: stage 4 step 2 staging and constant-size promotion (ADR 0001) on a synthetic
projection of N documents in a throwaway schema of the TEST database.

Opt-in — as a test: NEKAISE_PG_BENCH=1 (rows: NEKAISE_PG_BENCH_ROWS, default 200000) with
NEKAISE_PG_TEST_DSN; as a script:

    python tests/test_store_pg_staging_bench.py --rows 1620000 [--dsn "host=... dbname=nekaise_test"]

Two workloads: a typical small round (discovery merge of 400 entries, 16 loader checkpoints of
25 rows, a 100-document prune with blocklist and ledger, 400 cleaner patches, freeze, gate,
promote, fold) and a full re-clean (every manifest row patched in 20 000-row batches, promoted in
one constant-size transaction, read through the overlay, then folded). Prints one JSON report.
Refuses the live database (dbname=nekaise)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import statistics
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402
import store_broker  # noqa: E402
import store_staging  # noqa: E402
from store import Prefix, Table  # noqa: E402

SHA = "0" * 40
BATCH = 20_000


def _row(i: int) -> dict:
    sid = f"doc-{i:08d}"
    return {"id": sid, "title": f"Building energy study {i}", "url": f"https://e.org/{sid}.pdf",
            "source": "bench", "license": "open", "topic": ("building_energy", "structures_civil",
                                                             "urban", "materials")[i % 4],
            "format": "pdf", "status": "ok", "http_status": 200,
            "sha256": hashlib.sha256(sid.encode()).hexdigest(), "bytes": 100_000 + i,
            "raw_path": f"raw/bench/{sid}.pdf", "text_path": f"text/{sid}.md",
            "text_chars": 40_000 + i % 997, "error": None, "fetched_at": "2026-09-25T00:00:00Z",
            "text_sha256": hashlib.sha256(sid.encode() + b"t").hexdigest(),
            "extractor_version": "x1",
            "quality": {"total": 1.5, "w20": {"domain": i % 7, "alpha": 0.75025}}}


def _gone(n: int) -> list[str]:
    """The 100 documents the small round prunes."""
    return [f"doc-{i:08d}" for i in range(7, n, max(1, n // 100))][:100]


def _entry(row: dict) -> dict:
    return {k: row[k] for k in ("id", "title", "url", "source", "license", "topic", "format")}


def populate(st, n: int) -> float:
    """COPY n synthetic documents into the projection (entries + manifest, derived columns)."""
    import store_pg
    t0 = time.monotonic()
    with st._connect() as conn:
        with conn.cursor() as cur:
            with cur.copy("COPY manifest (id, row_text, url_norm, url_key, title_norm, title_key, "
                          "sha256, shard, topic_key) FROM STDIN") as cp:
                for i in range(n):
                    r = _row(i)
                    shard, topic, _ = store.legacy_manifest_key(r)
                    cp.write_row((r["id"], store.canonical_row(r), *store_pg._keys_for(r),
                                  r["sha256"], shard, topic))
            with cur.copy("COPY entries (id, row_text, url_norm, url_key, title_norm, title_key) "
                          "FROM STDIN") as cp:
                for i in range(n):
                    e = _entry(_row(i))
                    cp.write_row((e["id"], store.canonical_row(e), *store_pg._keys_for(e)))
            cur.execute("INSERT INTO rotation (name, value_text) VALUES ('find_books', %s)",
                        [store.canonical_row({"flag": "--offset", "next": 0, "step": 25})])
        conn.commit()
        conn.execute("ANALYZE")
    return time.monotonic() - t0


class Clock:
    def __init__(self):
        self.times: dict[str, list[float]] = {}

    def __call__(self, name, fn, *a, **kw):
        t0 = time.monotonic()
        out = fn(*a, **kw)
        self.times.setdefault(name, []).append(time.monotonic() - t0)
        return out

    def report(self) -> dict:
        """name -> "n=<count> total=<s> p50=<s> max=<s>" """
        return {k: f"n={len(v)} total={sum(v):.3f}s p50={statistics.median(v):.4f}s "
                   f"max={max(v):.4f}s" for k, v in self.times.items()}


def _stage(st, w, run_id, step, batch, fn):
    rec = store_broker.Recorder()
    fn(rec)
    with st.read_staged(run_id, writer=w) as v:
        version = v.version()
    return st.stage_batch(w, run_id, step, batch, rec.requests, expected_version=version)


def _reads(st, clock: Clock, prefix: str, n: int) -> None:
    ids = [f"doc-{i:08d}" for i in range(0, n, max(1, n // 25))][:25]
    urls = [f"https://e.org/doc-{i:08d}.pdf" for i in range(0, n, max(1, n // 10_000))][:10_000]
    with st.read() as v:
        clock(f"{prefix}.get_manifest_25", v.get_manifest, ids)
        clock(f"{prefix}.known_10k_urls", v.known, urls=urls)
        clock(f"{prefix}.scan_first_page_2000", v.scan, Table.MANIFEST, limit=2000)
        clock(f"{prefix}.scan_where_page", v.scan, Table.MANIFEST, where=Prefix("id", "doc-0001"),
              limit=2000)
        clock(f"{prefix}.aggregate_topic", lambda: list(v.aggregate_manifest(
            group_by=("topic",), sums=("text_chars",))))


def _full_scan(st) -> int:
    rows, cursor = 0, None
    with st.read() as v:
        while True:
            page = v.scan(Table.MANIFEST, cursor=cursor, limit=store.MAX_PAGE,
                          fields=("id", "corpus_path"))
            rows += len(page.rows)
            if (cursor := page.next_cursor) is None:
                return rows


def small_round(st, n: int, clock: Clock) -> dict:
    with st.writer(round_id="small") as w:
        run = clock("small.open_run", st.open_run, w, "small", producer_commit=SHA,
                    extractor_version="x1", cleaning_ruleset="rules-1")
        broker = store_broker.Broker(st, w, "small", stage=run)
        with broker.serving():
            def compute(view, batch):
                fresh = [_entry(_row(n + i)) for i in range(400)]
                taken = view.known(urls=[e["url"] for e in fresh]).urls
                batch.insert_entries([e for e in fresh if e["url"] not in taken])
                batch.rotation_set("find_books", {"flag": "--offset", "next": 25, "step": 25})
            clock("small.discovery_merge_400", broker.computed_batch, "discover", "merge", compute)
        for c in range(16):
            rows = [_row(n + c * 25 + i) for i in range(25)]
            clock("small.loader_checkpoint_25", _stage, st, w, "small", "fetch",
                  f"ckpt-{c:04d}", lambda tx, rows=rows: tx.upsert_manifest(rows))
        gone = _gone(n)
        survivors = {f"doc-{i:08d}": {"quality": {"total": 2.5}} for i in range(3, 303)}

        def prune(tx):
            tx.update_manifest_fields(survivors)
            tx.delete_manifest(gone, reason="prune: junk")
            tx.delete_entries(gone, reason="prune: junk")
            tx.blocklist_add([f"https://e.org/{g}.pdf" for g in gone])
            tx.ledger_append([{"id": g, "reason": "junk"} for g in gone])
        clock("small.prune_100", _stage, st, w, "small", "prune", "apply", prune)
        patches = {f"doc-{i:08d}": {"corpus_path": f"corpus/doc-{i:08d}.md",
                                    "corpus_sha256": "c" * 64} for i in range(1000, 1500)
                   if f"doc-{i:08d}" not in gone}
        clock("small.clean_patch_400", _stage, st, w, "small", "clean", "meta-0001",
              lambda tx: tx.update_manifest_fields(patches))
        frozen = clock("small.freeze", st.freeze, w, "small", required_gates=["tests"])
        with st.read_staged("small", writer=w) as v:
            clock("small.staged_known_10k", v.known,
                  urls=[f"https://e.org/doc-{i:08d}.pdf" for i in range(n, n + 10_000)])
        clock("small.gate_receipt", st.record_gate, w, frozen, "tests", passed=True)
        generation = clock("small.promote", st.promote, w, frozen)
        _reads(st, clock, "small.after_promote", n)
        folded = clock("small.fold", store_staging.fold_all, st, w)
        _reads(st, clock, "small.after_fold", n)
    revs = st._connect(autocommit=True).execute(
        "SELECT count(*) FROM revisions WHERE run_id = 'small'").fetchone()[0]
    return {"generation": generation, "revisions": revs, "folded": folded}


def full_reclean(st, n: int, clock: Clock) -> dict:
    gone = set(_gone(n))
    with st.writer(round_id="reclean") as w:
        st.open_run(w, "reclean", producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="rules-2")
        for b, start in enumerate(range(0, n + 400, BATCH)):
            patch = {f"doc-{i:08d}": {"corpus_path": f"corpus/doc-{i:08d}.md",
                                      "corpus_sha256": hashlib.sha256(b"%d" % i).hexdigest(),
                                      "corpus_chars": 39_000 + i % 991}
                     for i in range(start, min(start + BATCH, n + 400))
                     if f"doc-{i:08d}" not in gone}
            clock("reclean.stage_20k", _stage, st, w, "reclean", "clean", f"meta-{b:04d}",
                  lambda tx, patch=patch: tx.update_manifest_fields(patch))
        frozen = clock("reclean.freeze", st.freeze, w, "reclean", required_gates=["tests"])
        st.record_gate(w, frozen, "tests", passed=True)
        clock("reclean.promote", st.promote, w, frozen)
        _reads(st, clock, "reclean.overlay", n)
        rows_overlay = clock("reclean.overlay_full_scan", _full_scan, st)
        while True:
            progress = clock("reclean.fold_20k", st.fold, w, limit=BATCH)
            if progress.done or progress.generation is None:
                break
        _reads(st, clock, "reclean.after_fold", n)
        rows_folded = clock("reclean.after_fold_full_scan", _full_scan, st)
    revs = st._connect(autocommit=True).execute(
        "SELECT count(*) FROM revisions WHERE run_id = 'reclean'").fetchone()[0]
    return {"revisions": revs, "rows_overlay_scan": rows_overlay, "rows_folded_scan": rows_folded}


def run(dsn: str, n: int) -> dict:
    import store_pg
    if re.search(r"dbname=nekaise(\s|$)", dsn):
        raise SystemExit("refusing the live database: benchmark against nekaise_test")
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"nekaise-bench-{uuid.uuid4().hex[:8]}"
    (root / "registry").mkdir(parents=True)
    (root / "registry" / "backends.json").write_text(json.dumps(
        {"find_books": {"script": "find_books.py", "args": []}}))
    st = store_pg.PgStore(root, dsn=dsn, schema=f"bench_{uuid.uuid4().hex[:10]}")
    clock = Clock()
    try:
        st.pin_config_from_files()
        report = {"rows": n, "populate_s": round(populate(st, n), 1)}
        t0 = time.monotonic()
        report["small_round"] = small_round(st, n, clock)
        report["small_round_total_s"] = round(time.monotonic() - t0, 2)
        t0 = time.monotonic()
        report["full_reclean"] = full_reclean(st, n, clock)
        report["full_reclean_total_s"] = round(time.monotonic() - t0, 1)
        report["timings"] = clock.report()
        report["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        return report
    finally:
        st.drop()


@pytest.mark.skipif(not (os.environ.get("NEKAISE_PG_BENCH") and
                         os.environ.get("NEKAISE_PG_TEST_DSN")),
                    reason="benchmark: set NEKAISE_PG_BENCH=1 and NEKAISE_PG_TEST_DSN")
def test_staging_benchmark():
    n = int(os.environ.get("NEKAISE_PG_BENCH_ROWS", "200000"))
    report = run(os.environ["NEKAISE_PG_TEST_DSN"], n)
    print(json.dumps(report, indent=1))
    t = {k: float(v.split("max=")[1].rstrip("s")) for k, v in report["timings"].items()}
    # promotion is constant-size: a full re-clean promotes about as fast as a small round
    assert t["reclean.promote"] < 5 * max(t["small.promote"], 0.05)
    assert report["full_reclean"]["rows_overlay_scan"] == report["full_reclean"]["rows_folded_scan"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--dsn", default=os.environ.get("NEKAISE_PG_TEST_DSN",
                                                    "host=/home/zengp/.local/share/nekaise-pg/run "
                                                    "dbname=nekaise_test"))
    args = ap.parse_args()
    print(json.dumps(run(args.dsn, args.rows), indent=1))
