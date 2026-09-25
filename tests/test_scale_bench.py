"""Benchmark (ADR 0001 stage 4 step 5): a 400-document round's metadata work at 160M synthetic
documents, and the seal-time basis lookup (nk_basis_text) with many unfolded generations.

Opt-in, as a script only (hours of load; hundreds of GB):

    python tests/test_scale_bench.py scale --rows 160000000 --rounds 20 --dsn "host=... dbname=..."
    python tests/test_scale_bench.py basis --rows 1620000 --generations 0,1,16,64,65,128 [--cases hot,disjoint]

Refuses the live database (dbname=nekaise). The ADR's acceptance: at 160M synthetic metadata
rows, bounded memory and a 400-document round's metadata work below 30 s p95 on documented
hardware.

scale — a throwaway schema; `rows` documents (an entry + a manifest row each, ~600-byte rows as
tests/test_store_pg_staging_bench.py's, 9-digit ids) generated IN THE SERVER by parallel
sessions (INSERT ... SELECT over generate_series, committed per million), secondary indexes
dropped for the load and rebuilt with the schema's own definitions, ANALYZE; the rows are checked
to be canonical (store.canonical_row) and their derived columns exact on a sample. A baseline
generation 0 is promoted with a contracts receipt carrying the full recount (timed: the
integrity sweep's recount cost). Then `rounds` rounds, each the metadata work of a typical
400-document round against the 160M-row projection: open the run; discovery merge of 400 new
entries (known() over their URLs and titles, persisted + applied); 16 loader checkpoints of 25
rows; a prune (100 existing documents dropped from entries and manifest, blocklist + ledger, 300
survivors' metrics updated); a cleaner patch of 400 rows; freeze; the per-run checks at the frozen
state (verify_generation.run_checks = the contracts gate's work, with counters chained from the
parent's recorded ones; lint_registry.changed_lint); gate receipts; promotion; the fold of the
promoted generation into the projection. Each phase and each round's total is timed; the report
gives p50/p95/max. Payload bytes, network, subprocess start-up and the test-suite gate are not
metadata work and are excluded.

basis — `rows` documents; then K generations promoted one by one, each revising the SAME 400
documents (the worst case for nk_basis_text: its per-key history grows by one revision per
generation) and never folded; after each K in the list, a VERSIONED run stages a 400-row patch of
those documents whose claims are unchanged, so sealing looks every basis row up
(nk_batch_artifacts -> nk_basis_text); the batch's time is measured, and the overlay's read
costs at that K. The same with 400 fresh (disjoint) documents per generation gives the typical
case.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402

SHA = "0" * 40
TOPICS = ("building_energy", "structures_civil", "urban", "materials")
CHUNK = 1_000_000


def sid(i: int) -> str:
    return f"doc-{i:09d}"


def row(i: int) -> dict:
    s = sid(i)
    return {"id": s, "title": f"Building energy study {i}", "url": f"https://e.org/{s}.pdf",
            "source": "bench", "license": "open", "topic": TOPICS[i % 4], "format": "pdf",
            "status": "ok", "http_status": 200,
            "sha256": hashlib.sha256(s.encode()).hexdigest(), "bytes": 100_000 + i,
            "raw_path": f"raw/bench/{s}.pdf", "text_path": f"text/{s}.md",
            "text_chars": 40_000 + i % 997, "error": None, "fetched_at": "2026-09-25T00:00:00Z",
            "text_sha256": hashlib.sha256(s.encode() + b"t").hexdigest(),
            "extractor_version": "x1", "persistent_id": f"https://doi.org/10.5555/BENCH.{i}",
            "quality": {"total": 1.5, "w20": {"domain": i % 7, "alpha": 0.75025}}}


def entry(r: dict) -> dict:
    return {k: r[k] for k in ("id", "title", "url", "source", "license", "topic", "format",
                              "persistent_id")}


# The same rows, generated in the server: canonical JSON (sorted keys, compact) by construction.
ID = "'doc-' || lpad(g::text, 9, '0')"
MANIFEST_SQL = f"""
INSERT INTO {{schema}}.manifest (id, row_text, url_norm, url_key, title_norm, title_key, sha256,
                                  shard, topic_key, pids)
SELECT id, '{{{{"bytes":' || (100000 + g) || ',"error":null,"extractor_version":"x1",'
        || '"fetched_at":"2026-09-25T00:00:00Z","format":"pdf","http_status":200,"id":"' || id
        || '","license":"open","persistent_id":"https://doi.org/10.5555/BENCH.' || g
        || '","quality":{{{{"total":1.5,"w20":{{{{"alpha":0.75025,"domain":'
        || mod(g, 7) || '}}}}}}}},"raw_path":"raw/bench/' || id || '.pdf","sha256":"' || sha
        || '","source":"bench","status":"ok","text_chars":' || (40000 + mod(g, 997))
        || ',"text_path":"text/' || id || '.md","text_sha256":"' || tsha
        || '","title":"Building energy study ' || g || '","topic":"' || topic
        || '","url":"https://e.org/' || id || '.pdf"}}}}',
       'https://e.org/' || id || '.pdf',
       sha256(convert_to('https://e.org/' || id || '.pdf', 'UTF8')),
       'building energy study ' || g,
       sha256(convert_to('building energy study ' || g, 'UTF8')),
       sha, {{shard}}, topic, ARRAY['doi:10.5555/bench.' || g]
FROM (SELECT g, {ID} AS id,
             encode(sha256(convert_to({ID}, 'UTF8')), 'hex') AS sha,
             encode(sha256(convert_to({ID} || 't', 'UTF8')), 'hex') AS tsha,
             (ARRAY['building_energy','structures_civil','urban','materials'])[mod(g, 4) + 1] AS topic
      FROM generate_series(%s::bigint, %s::bigint) g) s
"""
ENTRIES_SQL = f"""
INSERT INTO {{schema}}.entries (id, row_text, url_norm, url_key, title_norm, title_key, pids)
SELECT id, '{{{{"format":"pdf","id":"' || id || '","license":"open","persistent_id":'
        || '"https://doi.org/10.5555/BENCH.' || g || '","source":"bench",'
        || '"title":"Building energy study ' || g || '","topic":"' || topic
        || '","url":"https://e.org/' || id || '.pdf"}}}}',
       'https://e.org/' || id || '.pdf',
       sha256(convert_to('https://e.org/' || id || '.pdf', 'UTF8')),
       'building energy study ' || g,
       sha256(convert_to('building energy study ' || g, 'UTF8')),
       ARRAY['doi:10.5555/bench.' || g]
FROM (SELECT g, {ID} AS id,
             (ARRAY['building_energy','structures_civil','urban','materials'])[mod(g, 4) + 1] AS topic
      FROM generate_series(%s::bigint, %s::bigint) g) s
"""


def _load_chunk(args) -> float:
    dsn, schema, table, lo, hi, shard = args
    import psycopg
    from psycopg import sql
    t0 = time.monotonic()
    with psycopg.connect(dsn) as conn:
        conn.execute("SET synchronous_commit = on")
        text = (MANIFEST_SQL if table == "manifest" else ENTRIES_SQL).format(
            schema=f'"{schema}"', shard=sql.Literal(shard).as_string(conn))
        conn.execute(text, [lo, hi])
        conn.commit()
    return time.monotonic() - t0


def populate(st, dsn: str, n: int, sessions: int, log) -> dict:
    """Server-side parallel load of n documents with the secondary indexes rebuilt afterwards."""
    import state_codec
    schema = st.schema
    shard = f"{state_codec.manifest_shard(sid(0))}.jsonl"
    if {f"{state_codec.manifest_shard(sid(i))}.jsonl" for i in (1, n // 2, n - 1)} != {shard}:
        raise SystemExit("the synthetic ids must route to one manifest shard")
    out = {}
    with st._connect(autocommit=True) as conn:
        defs = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename IN "
            "('manifest', 'entries') AND indexname NOT LIKE '%%pkey'", [schema]).fetchall()
        for name, _ in defs:
            conn.execute(f'DROP INDEX "{schema}"."{name}"')
    for table in ("manifest", "entries"):
        t0 = time.monotonic()
        jobs = [(dsn, schema, table, lo, min(lo + CHUNK - 1, n - 1), shard)
                for lo in range(0, n, CHUNK)]
        with ProcessPoolExecutor(max_workers=sessions) as pool:
            for k, _ in enumerate(pool.map(_load_chunk, jobs), 1):
                if k % 10 == 0:
                    log(f"  {table}: {k}/{len(jobs)} chunks, {time.monotonic() - t0:.0f} s")
        out[f"load_{table}_s"] = round(time.monotonic() - t0, 1)
        log(f"loaded {table} in {out[f'load_{table}_s']} s")
    t0 = time.monotonic()
    with st._connect(autocommit=True) as conn:
        conn.execute("SET maintenance_work_mem = '4GB'")
        conn.execute("SET max_parallel_maintenance_workers = 8")
        for name, ddl in defs:
            t1 = time.monotonic()
            conn.execute(ddl)
            log(f"  index {name}: {time.monotonic() - t1:.0f} s")
        out["index_build_s"] = round(time.monotonic() - t0, 1)
        t0 = time.monotonic()
        conn.execute(f'ANALYZE "{schema}".manifest')
        conn.execute(f'ANALYZE "{schema}".entries')
        out["analyze_s"] = round(time.monotonic() - t0, 1)
        out["manifest_bytes"] = conn.execute(
            f"SELECT pg_total_relation_size('\"{schema}\".manifest')").fetchone()[0]
        out["entries_bytes"] = conn.execute(
            f"SELECT pg_total_relation_size('\"{schema}\".entries')").fetchone()[0]
    check_sample(st, n)
    return out


def check_sample(st, n: int) -> None:
    """The server-generated rows are exactly what the store writes for row(i)."""
    import store_pg
    ids = sorted({0, 1, 7, n // 3, n // 2, n - 1})
    with st._connect(autocommit=True) as conn:
        got = {i: t for i, t in conn.execute(
            "SELECT id, row_text FROM manifest WHERE id = ANY(%s)", [[sid(i) for i in ids]])}
        keys = {r[0]: tuple(bytes(v) if isinstance(v, memoryview) else v for v in r[1:])
                for r in conn.execute(
                    "SELECT id, url_norm, url_key, title_norm, title_key, sha256, shard, "
                    "topic_key, pids FROM manifest WHERE id = ANY(%s)", [[sid(i) for i in ids]])}
        ent = dict(conn.execute("SELECT id, row_text FROM entries WHERE id = ANY(%s)",
                                [[sid(i) for i in ids]]).fetchall())
    for i in ids:
        r = row(i)
        assert got[sid(i)] == store.canonical_row(r), (got[sid(i)], store.canonical_row(r))
        assert keys[sid(i)] == (*store_pg.revision_keys("manifest", r),
                                store_pg.pids_for(r)), sid(i)
        assert ent[sid(i)] == store.canonical_row(entry(r))


class Clock:
    def __init__(self):
        self.times: dict[str, list[float]] = {}

    def __call__(self, name, fn, *a, **kw):
        t0 = time.monotonic()
        out = fn(*a, **kw)
        self.times.setdefault(name, []).append(time.monotonic() - t0)
        return out

    @staticmethod
    def stats(v: list[float]) -> dict:
        s = sorted(v)
        p95 = s[min(len(s) - 1, max(0, round(0.95 * len(s) + 0.5) - 1))]
        return {"n": len(s), "p50": round(statistics.median(s), 4), "p95": round(p95, 4),
                "max": round(s[-1], 4), "total": round(sum(s), 2)}

    def report(self) -> dict:
        return {k: self.stats(v) for k, v in self.times.items()}


def _stage(st, w, run_id, step, batch, fn):
    import store_broker
    rec = store_broker.Recorder()
    fn(rec)
    with st.read_staged(run_id, writer=w) as v:
        version = v.version()
    return st.stage_batch(w, run_id, step, batch, rec.requests, expected_version=version)


def baseline(st, clock: Clock) -> dict:
    """Generation 0: the projection as it is, promoted by a baseline run whose contracts receipt
    carries the full recount (what step 6's baseline tool records)."""
    import verify_generation
    with st.writer() as w:
        st.open_run(w, "baseline", kind="baseline", producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        frozen = st.freeze(w, "baseline", required_gates=["contracts"])
        with st.read_staged("baseline", seq=frozen.seq, writer=w) as view:
            restrictions, _ = store.pinned_policy(view)
            counters = clock("baseline.full_recount", verify_generation.full_counters, view,
                             restrictions)
        st.record_gate(w, frozen, "contracts", passed=True,
                       detail={"report": {"counters": counters, "counter_mode": "full"}})
        st.promote(w, frozen)
    return counters


def one_round(st, root: Path, n: int, r: int, clock: Clock, rng, pruned: set) -> None:
    import lint_registry
    import store_broker
    import store_staging
    import verify_generation
    run_id = f"round-{r:03d}-{uuid.uuid4().hex[:6]}"
    known_pids: list[int] = []
    base = n + r * 1000
    t_round = time.monotonic()
    with st.writer(round_id=run_id) as w:
        clock("open_run", st.open_run, w, run_id, producer_commit=SHA, extractor_version="x1",
              cleaning_ruleset="none", artifacts="unchecked")

        def compute(view, batch):
            fresh = [entry(row(base + i)) for i in range(400)]
            hits = view.known(urls=[e["url"] for e in fresh],
                              titles=[e["title"] for e in fresh], ids=[e["id"] for e in fresh])
            # persistent identities of the candidates, half of them already in the corpus
            probe = [f"doi:10.5555/bench.{base + i}" for i in range(200)] + \
                [f"doi:10.5555/bench.{i}" for i in rng.sample(range(n), 400) if i not in pruned][:200]
            known_pids.append(len(clock("discovery.known_pids_400", view.known_pids, probe)))
            batch.insert_entries([e for e in fresh if e["url"] not in hits.urls
                                  and e["id"] not in hits.ids])
        rec = store_broker.Recorder()
        with st.read_staged(run_id, writer=w) as v:
            clock("discovery.known_400", compute, v, rec)
            version = v.version()
        # (a few of the 200 random existing identities may have been pruned by earlier rounds)
        if not known_pids or known_pids[-1] < 190:
            raise SystemExit(f"known_pids found {known_pids} of the 200 existing identities")
        clock("discovery.merge_400", st.stage_batch, w, run_id, "discover", "merge",
              rec.requests, expected_version=version)
        for c in range(16):
            rows = [row(base + c * 25 + i) for i in range(25)]
            clock("loader.checkpoint_25", _stage, st, w, run_id, "fetch", f"ckpt-{c:04d}",
                  lambda tx, rows=rows: tx.upsert_manifest(rows))
        picks = sorted(set(i for i in rng.sample(range(n), 440) if i not in pruned))[:400]
        pruned.update(picks[:100])   # never picked again (a pruned id is gone)
        gone = [sid(i) for i in picks[:100]]
        survivors = {sid(i): {"quality": {"total": 2.5}} for i in picks[100:400]}

        def prune(tx):
            tx.update_manifest_fields(survivors)
            tx.delete_manifest(gone, reason="prune: junk")
            tx.delete_entries(gone, reason="prune: junk")
            tx.blocklist_add([f"https://e.org/{g}.pdf" for g in gone])
            tx.ledger_append([{"id": g, "url": f"https://e.org/{g}.pdf", "reason": "junk",
                               "pruned_at": "2026-09-25T00:00:00Z", "blocklisted": True}
                              for g in gone])
        clock("prune.apply_100", _stage, st, w, run_id, "prune", "apply", prune)
        cleaned = [sid(base + i) for i in range(400)]
        patches = {i: {"corpus_path": f"corpus/{i}.md", "corpus_sha256": "c" * 64,
                       "corpus_chars": 39_000, "cleaner_version": "clean_corpus/2;rules=none"}
                   for i in cleaned}
        clock("clean.patch_400", _stage, st, w, run_id, "clean", "meta-0001",
              lambda tx: tx.update_manifest_fields(patches))
        frozen = clock("freeze", st.freeze, w, run_id, required_gates=["contracts", "lint"])

        def checks():
            with st.read_staged(run_id, seq=frozen.seq, writer=w) as view:
                restrictions, _ = store.pinned_policy(view)
                rep = verify_generation.run_checks(view, restrictions, root=root)
                errors, _, _ = lint_registry.changed_lint(view, restrictions)
            return rep, errors
        rep, lint_errors = clock("gates.contracts_and_lint_checks", checks)
        if rep.errors or lint_errors or rep.mode != "delta":
            raise SystemExit(f"round {r}: checks failed or not incremental: {rep.errors[:3]} "
                             f"{lint_errors[:3]} mode={rep.mode}")
        clock("gates.receipts", lambda: (
            st.record_gate(w, frozen, "contracts", passed=True,
                           detail={"report": rep.as_report()}),
            st.record_gate(w, frozen, "lint", passed=True)))
        clock("promote", st.promote, w, frozen)
        clock("fold", store_staging.fold_all, st, w)
    clock("round_total", lambda: None)
    clock.times["round_total"][-1] = time.monotonic() - t_round


LIVE_SOCKET = Path.home() / ".local" / "share" / "nekaise-pg" / "run"


def refuse_live(dsn: str) -> None:
    """Never load a benchmark into the live cluster (its WAL goes to the backup SSD): the DSN
    must name a socket directory that is not the live cluster's and a database other than the
    live one, parsed as libpq parses it (quoting, URIs)."""
    from psycopg.conninfo import conninfo_to_dict
    d = conninfo_to_dict(dsn)
    host = d.get("host") or ""
    if not host or os.path.realpath(host) == os.path.realpath(LIVE_SOCKET):
        raise SystemExit(f"refusing {dsn!r}: name a throwaway cluster's socket explicitly (not "
                         f"the live {LIVE_SOCKET})")
    if d.get("dbname") == "nekaise":
        raise SystemExit(f"refusing {dsn!r}: the live database name")


def scale(dsn: str, n: int, rounds: int, sessions: int, keep: bool, log,
          reuse: str | None = None) -> dict:
    """`reuse`: the schema of an earlier run of this benchmark, kept (--keep, or the process was
    killed): its projection and baseline generation are used as they are (unfinished runs
    aborted), so the rounds can be measured again without a multi-hour load."""
    refuse_live(dsn)
    import random

    import store_pg
    import store_staging
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"nekaise-scale-{uuid.uuid4().hex[:8]}"
    (root / "registry").mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1] / "registry"
    for name in store.CONFIG_FILES:
        if (repo / name).exists():
            (root / "registry" / name).write_bytes((repo / name).read_bytes())
    if reuse:
        st = store_pg.PgStore(root, dsn=dsn, schema=reuse, create=False)
    else:
        st = store_pg.PgStore(root, dsn=dsn, schema=f"scale_{uuid.uuid4().hex[:10]}")
    clock = Clock()
    report = {"rows": n, "rounds": rounds, "schema": st.schema, "hardware": hardware(),
              "server": server_settings(st), "reused": bool(reuse)}
    try:
        if reuse:
            with st.writer() as w:
                for run in store_staging.unfinished_runs(st, w):
                    st.abort_run(w, run["run_id"], reason="benchmark rerun")
            with st._connect(autocommit=True) as conn:
                report["rows_present"] = conn.execute(
                    "SELECT (SELECT count(*) FROM manifest), (SELECT count(*) FROM entries)"
                ).fetchone()
                report["generation"] = conn.execute(
                    "SELECT current_generation FROM dataset").fetchone()[0]
            with st.read() as v:
                import verify_generation
                report["baseline_counters"] = verify_generation.recorded_counters(v, 0)
            if report["baseline_counters"] is None:
                raise SystemExit(f"{reuse} has no recorded baseline counters")
            prior = report["generation"]
        else:
            st.pin_config_from_files()
            t0 = time.monotonic()
            report["populate"] = populate(st, dsn, n, sessions, log)
            report["populate_s"] = round(time.monotonic() - t0, 1)
            log(f"populated in {report['populate_s']} s")
            report["baseline_counters"] = baseline(st, clock)
            log(f"baseline recount {clock.times['baseline.full_recount'][0]:.0f} s")
            prior = 0
        rng = random.Random(20260925 + prior)
        pruned: set[int] = set()
        for r in range(prior, prior + rounds):
            one_round(st, root, n, r, clock, rng, pruned)
            log(f"round {r}: {clock.times['round_total'][-1]:.2f} s")
        report["timings"] = clock.report()
        report["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        with st._connect(autocommit=True) as conn:
            report["database_bytes"] = conn.execute(
                "SELECT pg_database_size(current_database())").fetchone()[0]
        return report
    finally:
        if not keep:
            st.drop()


def hardware() -> dict:
    cpu = next((line.split(":", 1)[1].strip() for line in
                Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")),
               platform.processor())
    mem = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                   if line.startswith("MemTotal"))) // 1024 // 1024
    disk = subprocess.run(["lsblk", "-dno", "NAME,MODEL,SIZE"], capture_output=True,
                          text=True).stdout.strip()
    return {"cpu": cpu, "cores": os.cpu_count(), "ram_gb": mem, "disks": disk,
            "kernel": platform.release(), "python": platform.python_version()}


def server_settings(st) -> dict:
    with st._connect(autocommit=True) as conn:
        return {name: conn.execute(f"SHOW {name}").fetchone()[0] for name in (
            "server_version", "shared_buffers", "effective_cache_size", "work_mem",
            "maintenance_work_mem", "max_wal_size", "wal_compression", "fsync",
            "synchronous_commit", "wal_level", "archive_mode", "jit")}


# --- basis: nk_basis_text with accumulated unfolded generations -----------------------------------

def basis(dsn: str, n: int, generations: list[int], keep: bool, log,
          cases: tuple[str, ...] = ("hot", "disjoint")) -> dict:
    refuse_live(dsn)
    import store_pg
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"nekaise-basis-{uuid.uuid4().hex[:8]}"
    (root / "registry").mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1] / "registry"
    for name in store.CONFIG_FILES:
        if (repo / name).exists():
            (root / "registry" / name).write_bytes((repo / name).read_bytes())
    out = {"rows": n, "hardware": hardware(), "cases": {}}
    for case in cases:
        st = store_pg.PgStore(root, dsn=dsn, schema=f"basis_{uuid.uuid4().hex[:10]}")
        try:
            st.pin_config_from_files()
            populate(st, dsn, n, 8, log)
            out["server"] = server_settings(st)
            clock = Clock()
            baseline(st, clock)
            hot = [sid(i) for i in range(1000, 1400)]
            done = 0
            results = {}
            for k in generations:
                while done < k:
                    keys = hot if case == "hot" else [sid(5000 + done * 400 + i)
                                                      for i in range(400)]
                    _promote_patch(st, f"g{done}-{uuid.uuid4().hex[:6]}", keys, done)
                    done += 1
                    if done % 64 == 0:
                        with st._connect(autocommit=True) as conn:
                            conn.execute("ANALYZE revisions, batches, runs")
                results[k] = _measure_basis(st, hot, k)
                log(f"{case} K={k}: {results[k]}")
            out["cases"][case] = results
        finally:
            if not keep:
                st.drop()
    return out


def _promote_patch(st, run_id: str, keys: list[str], g: int) -> None:
    with st.writer() as w:
        st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        _stage(st, w, run_id, "clean", "meta", lambda tx: tx.update_manifest_fields(
            {k: {"quality": {"total": 1.0 + g}} for k in keys}))
        fr = st.freeze(w, run_id, required_gates=["tests"])
        st.record_gate(w, fr, "tests", passed=True)
        st.promote(w, fr)


def _measure_basis(st, hot: list[str], k: int) -> dict:
    """A versioned run patching the hot documents with unchanged claims: sealing looks every
    basis row up. Measured: the batch (apply + seal), then overlay reads; the run is aborted."""
    run_id = f"probe-{k}-{uuid.uuid4().hex[:6]}"
    out = {}
    with st.writer() as w:
        st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="versioned")
        t0 = time.monotonic()
        _stage(st, w, run_id, "clean", "meta", lambda tx: tx.update_manifest_fields(
            {h: {"quality": {"total": 0.5}} for h in hot}))
        out["versioned_patch_400_s"] = round(time.monotonic() - t0, 3)
        st.abort_run(w, run_id, reason="benchmark probe")
    with st.read() as v:
        t0 = time.monotonic()
        v.get_manifest(hot[:25])
        out["get_manifest_25_s"] = round(time.monotonic() - t0, 4)
        t0 = time.monotonic()
        v.known(urls=[f"https://e.org/{h}.pdf" for h in hot])
        out["known_400_s"] = round(time.monotonic() - t0, 4)
        t0 = time.monotonic()
        v.scan(store.Table.MANIFEST, limit=2000)
        out["scan_first_page_s"] = round(time.monotonic() - t0, 4)
    return out


def _opted_in() -> bool:
    return bool(os.environ.get("NEKAISE_PG_BENCH") and os.environ.get("NEKAISE_PG_TEST_DSN"))


def test_scale_benchmark_harness():
    """The harness itself, small (opt-in: NEKAISE_PG_BENCH=1; rows NEKAISE_PG_BENCH_ROWS)."""
    import pytest
    if not _opted_in():
        pytest.skip("benchmark: set NEKAISE_PG_BENCH=1 and NEKAISE_PG_TEST_DSN")
    n = int(os.environ.get("NEKAISE_PG_BENCH_ROWS", "200000"))
    report = scale(os.environ["NEKAISE_PG_TEST_DSN"], n, 5, 4, False, lambda *_: None)
    assert report["timings"]["round_total"]["p95"] < 30
    assert report["max_rss_mb"] < 1024


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("scale", "basis"))
    ap.add_argument("--rows", type=int, default=160_000_000)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--generations", default="0,1,16,64,65,256")
    ap.add_argument("--cases", default="hot,disjoint")
    ap.add_argument("--keep", action="store_true", help="keep the schema (debugging)")
    ap.add_argument("--reuse-schema", default=None,
                    help="scale: rerun the rounds on a kept, populated schema")
    ap.add_argument("--dsn", default=os.environ.get(
        "NEKAISE_PG_BENCH_DSN", os.environ.get(
            "NEKAISE_PG_TEST_DSN",
            "host=/home/zengp/.local/share/nekaise-pg-test/run dbname=nekaise_test")))
    ap.add_argument("--out", default=None, help="write the JSON report here too")
    args = ap.parse_args()
    refuse_live(args.dsn)

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
    if args.mode == "scale":
        report = scale(args.dsn, args.rows, args.rounds, args.sessions, args.keep, log,
                       reuse=args.reuse_schema)
    else:
        report = basis(args.dsn, args.rows, [int(x) for x in args.generations.split(",")],
                       args.keep, log, tuple(args.cases.split(",")))
    text = json.dumps(report, indent=1, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
