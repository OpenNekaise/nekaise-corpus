#!/usr/bin/env python3
"""generation_review.py — the maintainer's generation-range review (ADR 0001 stage 4, step 4).

Under PostgreSQL authority there are no per-round data commits to review: the unit of review is
a contiguous range of promoted generations (reviewed_through, hi]. `evidence()` gathers, for
that range, what a reviewer must see — and nothing that needs a full-corpus scan:

* revisions: every generation's producer commit (and the code commits between consecutive
  ones, from git), extractor version and cleaning ruleset;
* decisions: configuration changes between generations (which pinned documents changed) and
  each generation's operation counts (inserts, updates, prune tombstones, blocklist and ledger
  rows) from its promotion record;
* quality/yield: per generation, the manifest rows its run staged by status (ok / failed) and
  the entries it added, from the run's own revisions (bounded by the range, not the corpus);
* failures: the runs aborted while the range was being produced (their abort reasons), and runs
  still unfinished;
* gate receipts: every required gate each promoted run passed, bound to its frozen state;
* backup health: the WAL archiver's last success/failure and the newest base backup's age.

The part read from the database (everything but git summaries and backup health, which change
with time) has a digest. A verdict names the range's upper end and that digest; `record()`
recomputes the evidence under the writer and refuses a verdict whose digest differs, so a
reviewer can only endorse exactly what it saw (never a self-supplied summary), and the database
(schema v7, review_verdicts) keeps verdicts contiguous and immutable:

* `ok` — the range is reviewed; with no finding open it is endorsed (publication may reach it);
* `finding` — reviewed, but endorsement stops — and publication stops altogether, even for
  generations endorsed before the finding (an empty-range finding about reviewed data) — until a
  later verdict
  resolves it — one whose range covers a generation promoted after the finding (the repair,
  promoted as a compensating generation);
* `integrity` — the same, and growth rounds are refused while it is open (`growth_block`).

Commands:
    python scripts/generation_review.py status
    python scripts/generation_review.py evidence [--through G]
    python scripts/generation_review.py record --through G --verdict ok|finding|integrity
        --reviewer NAME --evidence-digest D [--summary TEXT] [--resolves 3,4]
A maintainer's action agent cannot take the writer (its window holds it): it writes the verdict
file named by NEKAISE_REVIEW_VERDICT_FILE instead, which the maintainer validates and records
(`verdict_from_file`).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import store

ROOT = Path(__file__).resolve().parents[1]
VERDICTS = ("ok", "finding", "integrity")
# at most this many generations per review (the watermark advances range by range)
MAX_RANGE = 200
MAX_FAILURES = 50
MAX_GIT_LINES = 40
VERDICT_ENV = "NEKAISE_REVIEW_VERDICT_FILE"


class ReviewError(store.StoreError):
    """A verdict was refused (stale evidence, a malformed verdict, a gap in the range)."""


def _txn(st, writer):
    import store_staging
    return store_staging._writer_txn(st, writer)


def state(st, writer) -> dict:
    """The review watermarks, the open findings and the current generation."""
    with _txn(st, writer) as conn:
        reviewed, endorsed, verdicts, open_f, open_i = conn.execute(
            "SELECT reviewed_through, endorsed_through, verdicts, open_findings, open_integrity "
            "FROM review_state").fetchone()
        head = conn.execute("SELECT current_generation FROM dataset").fetchone()[0]
        findings = [{"verdict": seq, "kind": kind, "range": [lo, hi],
                     "summary": json.loads(detail).get("summary")}
                    for seq, kind, lo, hi, detail in conn.execute(
                        "SELECT seq, verdict, lo_generation, hi_generation, detail_text FROM "
                        "review_verdicts WHERE verdict <> 'ok' AND resolved_by IS NULL ORDER BY "
                        "seq").fetchall()]
    return {"current_generation": head, "reviewed_through": reviewed,
            "endorsed_through": endorsed, "verdicts": verdicts, "open_findings": open_f,
            "open_integrity": open_i, "findings": findings,
            # what publication may reach now: nothing while any finding is open — also one
            # raised about generations already endorsed (the database enforces the same)
            "publishable_through": None if open_f or open_i else endorsed}


def growth_block(st, writer) -> str | None:
    """Why growth rounds are refused by the review, or None: an open integrity finding."""
    s = state(st, writer)
    if s["open_integrity"]:
        ids = [f["verdict"] for f in s["findings"] if f["kind"] == "integrity"]
        return (f"integrity finding(s) {ids} of the generation-range review are open: growth "
                "is blocked until a verdict resolves them (after a compensating repair)")
    return None


def _range(conn, through: int | None) -> tuple[int, int, int | None]:
    reviewed = conn.execute("SELECT reviewed_through FROM review_state").fetchone()[0]
    head = conn.execute("SELECT current_generation FROM dataset").fetchone()[0]
    lo = (-1 if reviewed is None else reviewed) + 1
    hi = head if through is None else through
    if hi is None:
        hi = lo - 1
    if head is None and hi >= lo or head is not None and hi > head:
        raise ReviewError(f"generation {hi} is not promoted (current: {head})")
    if hi < lo - 1:
        raise ReviewError(f"generations through {hi} are already reviewed (through {lo - 1})")
    return lo, min(hi, lo + MAX_RANGE - 1), head


def _database_evidence(conn, lo: int, hi: int) -> dict:
    """The digest-bound part: rows that never change once the range is promoted."""
    gens = []
    rows = conn.execute(
        "SELECT g.generation, g.run_id, r.kind, g.producer_commit, g.config_digest, "
        "g.extractor_version, g.cleaning_ruleset, g.frozen_seq, g.frozen_digest, g.counts_text, "
        "to_char(g.promoted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM "
        "generations g JOIN runs r USING (run_id) WHERE g.generation >= %s AND g.generation <= "
        "%s ORDER BY g.generation", [lo, hi]).fetchall()
    for (g, run_id, kind, commit, config, extractor, ruleset, fseq, fdigest, counts,
         at) in rows:
        gates = {gate: {"verdict": verdict, **json.loads(detail)} for gate, verdict, detail in
                 conn.execute("SELECT gate, verdict, detail_text FROM gate_receipts WHERE "
                              "run_id = %s ORDER BY gate", [run_id]).fetchall()}
        yield_rows = dict(conn.execute(
            "SELECT COALESCE(row_text::jsonb ->> 'status', '(none)'), count(*) FROM revisions "
            "WHERE run_id = %s AND tbl = 'manifest' AND op = 'put' GROUP BY 1 ORDER BY 1",
            [run_id]).fetchall())
        gens.append({"generation": g, "run": run_id, "kind": kind, "producer_commit": commit,
                     "config_digest": config, "extractor_version": extractor,
                     "cleaning_ruleset": ruleset, "frozen": [fseq, fdigest],
                     "counts": json.loads(counts), "manifest_puts_by_status": yield_rows,
                     "gates": gates, "promoted_at": at})
    # configuration decisions: the documents whose pinned bytes changed at each generation
    decisions = []
    previous = None
    if lo > 0:
        row = conn.execute("SELECT config_digest, cleaning_ruleset FROM generations WHERE "
                           "generation = %s", [lo - 1]).fetchone()
        previous = row
    for gen in gens:
        before = previous
        previous = (gen["config_digest"], gen["cleaning_ruleset"])
        if before is None or before == previous:
            continue
        changed = [name for (name,) in conn.execute(
            "SELECT COALESCE(a.name, b.name) FROM (SELECT name, sha256 FROM config_set_members "
            "WHERE digest = %s) a FULL JOIN (SELECT name, sha256 FROM config_set_members WHERE "
            "digest = %s) b USING (name) WHERE a.sha256 IS DISTINCT FROM b.sha256 ORDER BY 1",
            [before[0], gen["config_digest"]]).fetchall()]
        decisions.append({"generation": gen["generation"], "configuration_changed": changed,
                          "cleaning_ruleset": [before[1], gen["cleaning_ruleset"]]
                          if before[1] != gen["cleaning_ruleset"] else None})
    # failures: runs that tried to produce a generation of the range and were aborted
    parents = [lo - 1 + i for i in range(hi - lo + 1)] if hi >= lo else []
    cond = "(parent_generation = ANY(%s)" + (" OR parent_generation IS NULL)" if lo == 0 else ")")
    total, = conn.execute(f"SELECT count(*) FROM runs WHERE status = 'aborted' AND {cond}",
                          [parents]).fetchone()
    failures = [{"run": r, "kind": k, "parent": p, "detail": json.loads(d)}
                for r, k, p, d in conn.execute(
                    f"SELECT run_id, kind, parent_generation, detail_text FROM runs WHERE status "
                    f"= 'aborted' AND {cond} ORDER BY started_at, run_id LIMIT %s",
                    [parents, MAX_FAILURES]).fetchall()]
    return {"range": [lo, hi], "generations": gens, "decisions": decisions,
            "failures": {"total": total, "shown": failures}}


def _backup_health(conn, bases: Path | None) -> dict:
    """WAL archiving and base backups, as facts with their ages; failures are reported."""
    out: dict[str, Any] = {}
    try:
        with conn.transaction():   # a savepoint: a failure here leaves the transaction usable
            archived, last_ok, failed, last_fail = conn.execute(
                "SELECT archived_count, extract(epoch FROM now() - last_archived_time), "
                "failed_count, extract(epoch FROM now() - last_failed_time) FROM "
                "pg_stat_archiver").fetchone()
        out["wal"] = {"archived": archived,
                      "last_archived_seconds_ago": None if last_ok is None else float(last_ok),
                      "failed": failed,
                      "last_failed_seconds_ago": None if last_fail is None else float(last_fail),
                      "failing": last_fail is not None and (last_ok is None or
                                                            float(last_fail) < float(last_ok))}
    except Exception as exc:
        out["wal"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    if bases is None:
        import pg_backup
        bases = pg_backup.BASES
    try:
        done = sorted((p for p in Path(bases).iterdir()
                       if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name)
        newest = done[-1] if done else None
        out["base_backup"] = {"location": str(bases), "count": len(done),
                              "newest": newest.name if newest else None,
                              "newest_age_hours": round((time.time() - newest.stat().st_mtime)
                                                        / 3600, 2) if newest else None}
    except OSError as exc:
        out["base_backup"] = {"location": str(bases), "error": f"{type(exc).__name__}: {exc}"}
    return out


def _git_revisions(root: Path, commits: list[str]) -> list[dict]:
    """For each change of producer commit in the range, the code commits it brought (bounded)."""
    out = []
    for before, after in zip(commits, commits[1:]):
        if before == after:
            continue
        got = subprocess.run(["git", "log", "--oneline", f"--max-count={MAX_GIT_LINES}",
                              f"{before}..{after}"], cwd=root, capture_output=True, text=True)
        out.append({"from": before, "to": after,
                    "commits": got.stdout.splitlines() if got.returncode == 0
                    else [f"(git log failed: {got.stderr.strip()[:200]})"]})
    return out


def evidence(st, writer, *, through: int | None = None, root: Path = ROOT,
             bases: Path | None = None) -> dict:
    """The evidence for the next unreviewed range (reviewed_through, min(through, +MAX_RANGE)],
    read under `writer` (nothing can be promoted meanwhile). `digest` covers the database part."""
    with _txn(st, writer) as conn:
        lo, hi, head = _range(conn, through)
        db = _database_evidence(conn, lo, hi)
        health = _backup_health(conn, bases)
        prior = conn.execute("SELECT producer_commit FROM generations WHERE generation = %s",
                             [lo - 1]).fetchone() if lo > 0 else None
    digest = store._digest(db)
    commits = ([prior[0]] if prior else []) + [g["producer_commit"] for g in db["generations"]]
    return {**db, "digest": digest, "current_generation": head,
            "code_revisions": _git_revisions(Path(root), commits),
            "backup_health": health}


def summary(ev: dict) -> dict:
    """A compact form of the evidence for a prompt: the digest, the range, per-generation
    headline numbers, decisions, failures and backup health."""
    return {"range": ev["range"], "digest": ev["digest"],
            "current_generation": ev["current_generation"],
            "generations": [{"generation": g["generation"], "run": g["run"], "kind": g["kind"],
                             "producer_commit": g["producer_commit"][:12],
                             "counts": g["counts"], "manifest_puts_by_status":
                                 g["manifest_puts_by_status"],
                             "gates": {k: v["verdict"] for k, v in g["gates"].items()}}
                            for g in ev["generations"][-60:]],
            "omitted_generations": max(0, len(ev["generations"]) - 60),
            "decisions": ev["decisions"], "failures": ev["failures"],
            "code_revisions": ev["code_revisions"][-20:], "backup_health": ev["backup_health"]}


def record(st, writer, *, through: int, verdict: str, reviewer: str, evidence_digest: str,
           detail: dict | None = None, resolves: list[int] = (), root: Path = ROOT) -> dict:
    """Record a verdict over (reviewed_through, through] (an empty range only for a finding about
    generations already reviewed). The evidence is recomputed under `writer` and must have
    exactly the digest the reviewer saw; the database keeps verdicts contiguous and immutable,
    resolves only open findings and only by a verdict covering a generation promoted after them
    (the compensating repair), acknowledges the range's outbox rows for the review consumer and
    moves the reviewed and endorsed watermarks. Returns the new review state."""
    if verdict not in VERDICTS:
        raise ReviewError(f"verdict must be one of {VERDICTS}")
    resolves = sorted({int(r) for r in resolves})
    detail = dict(detail or {})
    store.validate_json(detail, "review detail")
    with _txn(st, writer) as conn:
        lo, hi, _ = _range(conn, through)
        if hi != through and not (through == lo - 1):
            raise ReviewError(f"a verdict covers at most {MAX_RANGE} generations: review "
                              f"through {hi} first")
        db = _database_evidence(conn, lo, through)
        if store._digest(db) != evidence_digest:
            raise ReviewError(f"the evidence for generations {lo}..{through} is not what the "
                              "reviewer saw (digest differs): review it again")
        seq = conn.execute("SELECT verdicts + 1 FROM review_state").fetchone()[0]
        conn.execute("INSERT INTO review_verdicts (seq, lo_generation, hi_generation, verdict, "
                     "reviewer, evidence_digest, resolves_text, detail_text) VALUES (%s, %s, %s, "
                     "%s, %s, %s, %s, %s)",
                     [seq, lo, through, verdict, reviewer, evidence_digest,
                      json.dumps(resolves, separators=(",", ":")), store._canonical(detail)])
    return state(st, writer)


def verdict_from_file(path: Path) -> dict:
    """A verdict an action agent wrote (NEKAISE_REVIEW_VERDICT_FILE): exactly {"through",
    "verdict", "evidence_digest", "summary"} plus optional "findings" (list of text) and
    "resolves" (list of verdict numbers). Raises ReviewError when malformed."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ReviewError(f"unreadable verdict file {path}: {exc}") from exc
    required = {"through", "verdict", "evidence_digest", "summary"}
    if not isinstance(doc, dict) or not required <= set(doc) \
            or set(doc) - required - {"findings", "resolves"}:
        raise ReviewError("the verdict file must hold through, verdict, evidence_digest, summary "
                          "(and optionally findings, resolves)")
    if isinstance(doc["through"], bool) or not isinstance(doc["through"], int):
        raise ReviewError("through must be a generation number")
    if doc["verdict"] not in VERDICTS or not isinstance(doc["summary"], str):
        raise ReviewError("verdict must be ok, finding or integrity, with a text summary")
    if not isinstance(doc["evidence_digest"], str) or len(doc["evidence_digest"]) != 64:
        raise ReviewError("evidence_digest must be the evidence's sha256")
    findings = doc.get("findings", [])
    resolves = doc.get("resolves", [])
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        raise ReviewError("findings must be a list of text")
    if not isinstance(resolves, list) or not all(
            isinstance(r, int) and not isinstance(r, bool) and r > 0 for r in resolves):
        raise ReviewError("resolves must be a list of verdict numbers")
    if doc["verdict"] != "ok" and not findings:
        raise ReviewError("a finding needs at least one finding text")
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("status", "evidence", "record"))
    ap.add_argument("--through", type=int)
    ap.add_argument("--verdict", choices=VERDICTS)
    ap.add_argument("--reviewer")
    ap.add_argument("--evidence-digest")
    ap.add_argument("--summary", default="")
    ap.add_argument("--resolves", default="")
    ap.add_argument("--lock-timeout", type=float, default=30)
    args = ap.parse_args(argv)
    import staged_runs
    st = store.open(root=ROOT)
    if not staged_runs.staged_authority(st):
        print("generation-range review needs PostgreSQL authority for this root (the file "
              "store's publication review is the maintainer's outgoing-commit review)",
              file=sys.stderr)
        return 2
    with st.writer(timeout=args.lock_timeout) as writer:
        if args.command == "status":
            print(json.dumps(state(st, writer), indent=2))
        elif args.command == "evidence":
            print(json.dumps(evidence(st, writer, through=args.through), indent=2))
        else:
            if args.through is None or not (args.verdict and args.reviewer
                                            and args.evidence_digest):
                ap.error("record needs --through, --verdict, --reviewer and --evidence-digest")
            resolves = [int(x) for x in args.resolves.split(",") if x.strip()]
            print(json.dumps(record(st, writer, through=args.through, verdict=args.verdict,
                                    reviewer=args.reviewer,
                                    evidence_digest=args.evidence_digest,
                                    detail={"summary": args.summary}, resolves=resolves),
                             indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
