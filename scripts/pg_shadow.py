#!/usr/bin/env python3
"""pg_shadow.py — keep the PostgreSQL store a verified shadow of the git-tracked files
(ADR 0001, stage 2).

FileStore stays authoritative until the stage-4 cutover. Every successful legacy round is already
a durable git commit, so the shadow replays COMMITS, never the working tree:

    python scripts/pg_shadow.py import [--commit REV]   # load a commit into an empty schema
    python scripts/pg_shadow.py sync                    # replay first-parent commits since the watermark
    python scripts/pg_shadow.py verify [--commit REV]   # compare the shadow with git at its watermark
    python scripts/pg_shadow.py status

* Only git objects are read (`git show REV:path`), so no round lock is needed and a round in
  progress is invisible until it commits.
* Each commit is applied in ONE database transaction that also checks and advances the watermark
  and writes a replication receipt; a commit that already has a receipt is skipped, so re-running
  is always safe. Replication writes rows verbatim (including any store journal events) and never
  creates store events of its own, so both stores export identically.
* The watermark must lie on the target's first-parent chain. A rewritten or shallow history stops
  sync with an explicit error; it never guesses.
* verify streams both sides into order-independent multiset digests per table (sum of row sha256
  mod 2^256, plus counts), so memory stays bounded at any corpus size. Divergence exits non-zero
  and is never repaired automatically.

Imports and syncs hold the PgStore writer lock, so two syncs never interleave.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg

import registry
import store
import store_pg
from store import canonical_row, key_digest, norm_url

ROOT = Path(__file__).resolve().parents[1]
TRACKED = ("registry", "manifest", "pruned_urls.txt")
MOD = 1 << 256


# --- git access ----------------------------------------------------------------------------------

def git(*args: str, repo: Path = ROOT, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, text=True,
                          capture_output=True).stdout


def git_bytes(rev: str, path: str, repo: Path = ROOT) -> bytes | None:
    out = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    return out.stdout if out.returncode == 0 else None


def rev_parse(rev: str, repo: Path = ROOT) -> str:
    return git("rev-parse", "--verify", f"{rev}^{{commit}}", repo=repo).strip()


def tracked_paths(rev: str, repo: Path = ROOT) -> list[str]:
    return [p for p in git("ls-tree", "-r", "--name-only", rev, "--", *TRACKED, repo=repo)
            .splitlines() if p]


def changed_paths(parent: str, commit: str, repo: Path = ROOT) -> list[str]:
    out = git("diff", "--name-only", "--no-renames", parent, commit, "--", *TRACKED, repo=repo)
    return sorted(p for p in out.splitlines() if p)


# --- classification of tracked files -------------------------------------------------------------

def kind_of(path: str) -> str | None:
    name = Path(path).name
    if path == "pruned_urls.txt":
        return "blocklist"
    if path.startswith("manifest/") and name.endswith(".jsonl"):
        return "manifest"
    if not path.startswith("registry/"):
        return None
    rel = path[len("registry/"):]
    if "/" in rel:
        return "events" if rel.startswith("journal/") and name.endswith(".jsonl") else None
    if name.endswith(".yaml"):
        return "entries"
    if name == "pruned.jsonl" or (name.startswith("pruned-") and name.endswith(".jsonl")):
        return "ledger"
    if name == "rotation.json":
        return "rotation"
    if name == store.BACKEND_STATE_FILE:
        return "backend_state"
    if name in store.CONFIG_FILES:
        return "config"
    return None


def parse(kind: str, data: bytes | None):
    """Rows of one tracked file (empty when the file is absent at that revision)."""
    if data is None:
        return {} if kind in ("entries", "manifest", "rotation", "backend_state") else []
    text = data.decode()
    if kind == "entries":
        return {e["id"]: e for e in (registry.parse_yaml(text) or {}).get("sources") or []}
    if kind == "manifest":
        return {r["id"]: r for r in map(json.loads, filter(str.strip, text.splitlines()))}
    if kind in ("ledger", "events"):
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    if kind == "blocklist":
        return [u for u in (norm_url(l) for l in text.splitlines() if l.strip()) if u]
    if kind in ("rotation", "backend_state"):
        return json.loads(text)
    raise ValueError(kind)


# --- deltas ---------------------------------------------------------------------------------------

def empty_delta() -> dict:
    return {"entries_put": {}, "entries_del": set(), "manifest_put": {}, "manifest_del": set(),
            "blocklist_add": set(), "blocklist_del": set(), "ledger": Counter(),
            "events": [], "rotation": None, "backend_state": None, "config": None}


def commit_delta(parent: str | None, commit: str, paths: list[str], repo: Path = ROOT) -> dict:
    """Row-level changes between two revisions, restricted to `paths`. Keyed tables are diffed over
    the UNION of the changed files, so a row that moves between shards is an update, not a loss."""
    delta = empty_delta()
    by_kind: dict[str, list[str]] = {}
    for p in paths:
        if (k := kind_of(p)) is not None:
            by_kind.setdefault(k, []).append(p)
    for kind in ("entries", "manifest"):
        old, new = {}, {}
        for p in by_kind.get(kind, []):
            old.update(parse(kind, git_bytes(parent, p, repo)) if parent else {})
            new.update(parse(kind, git_bytes(commit, p, repo)))
        delta[f"{kind}_put"] = {i: r for i, r in new.items() if old.get(i) != r}
        delta[f"{kind}_del"] = set(old) - set(new)
    old_b, new_b = set(), set()
    for p in by_kind.get("blocklist", []):
        old_b |= set(parse("blocklist", git_bytes(parent, p, repo)) if parent else [])
        new_b |= set(parse("blocklist", git_bytes(commit, p, repo)))
    delta["blocklist_add"], delta["blocklist_del"] = new_b - old_b, old_b - new_b
    for p in by_kind.get("ledger", []):
        old_rows = parse("ledger", git_bytes(parent, p, repo)) if parent else []
        delta["ledger"].update(canonical_row(r) for r in parse("ledger", git_bytes(commit, p, repo)))
        delta["ledger"].subtract(canonical_row(r) for r in old_rows)
    for p in by_kind.get("events", []):
        old_seq = {e["seq"] for e in (parse("events", git_bytes(parent, p, repo)) if parent else [])}
        delta["events"] += [e for e in parse("events", git_bytes(commit, p, repo))
                            if e["seq"] not in old_seq]
    if "rotation" in by_kind:
        delta["rotation"] = parse("rotation", git_bytes(commit, "registry/rotation.json", repo))
    if "backend_state" in by_kind:
        delta["backend_state"] = parse(
            "backend_state", git_bytes(commit, f"registry/{store.BACKEND_STATE_FILE}", repo))
    if "config" in by_kind:
        delta["config"] = {name: data for name in store.CONFIG_FILES
                           if (data := git_bytes(commit, f"registry/{name}", repo)) is not None}
    return delta


def delta_stats(delta: dict) -> dict:
    return {"entries_put": len(delta["entries_put"]), "entries_del": len(delta["entries_del"]),
            "manifest_put": len(delta["manifest_put"]), "manifest_del": len(delta["manifest_del"]),
            "blocklist_add": len(delta["blocklist_add"]), "blocklist_del": len(delta["blocklist_del"]),
            "ledger": sum(abs(v) for v in delta["ledger"].values()), "events": len(delta["events"]),
            "rotation": delta["rotation"] is not None, "config": delta["config"] is not None}


# --- applying deltas ------------------------------------------------------------------------------

def _put_rows(cur, table: str, rows) -> None:
    extra = table == "manifest"
    cols = "id, row_text, url_norm, url_key, title_norm, title_key" + (", sha256" if extra else "")
    ph = ", ".join(["%s"] * (7 if extra else 6))
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols.split(", ")[1:])
    params = []
    for r in rows:
        rec = [r["id"], store_pg._check_text(canonical_row(r), f"{table} row {r['id']}"),
               *store_pg._keys_for(r)]
        if extra:
            sha = r.get("sha256")
            rec.append(sha if isinstance(sha, str) and sha else None)
        params.append(rec)
    cur.executemany(f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT (id) DO UPDATE SET "
                    f"{sets}", params)


def apply_delta(conn, st: store_pg.PgStore, delta: dict) -> None:
    with conn.cursor() as cur:
        for table in ("entries", "manifest"):
            if delta[f"{table}_del"]:
                cur.execute(f"DELETE FROM {table} WHERE id = ANY(%s)", [sorted(delta[f"{table}_del"])])
            rows = list(delta[f"{table}_put"].values())
            for i in range(0, len(rows), 5000):
                _put_rows(cur, table, rows[i:i + 5000])
        if delta["blocklist_del"]:
            cur.execute("DELETE FROM blocklist WHERE key = ANY(%s)",
                        [[key_digest(u) for u in delta["blocklist_del"]]])
        cur.executemany("INSERT INTO blocklist (key, url) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        [(key_digest(u), store_pg._check_text(u, "blocklist url"))
                         for u in sorted(delta["blocklist_add"])])
        for text, n in sorted(delta["ledger"].items()):
            key = key_digest(text)
            if n > 0:
                base = cur.execute("SELECT COALESCE(max(n) + 1, 0) FROM ledger WHERE key = %s",
                                   [key]).fetchone()[0]
                cur.executemany("INSERT INTO ledger (key, n, row_text) VALUES (%s, %s, %s)",
                                [(key, base + k, store_pg._check_text(text, "ledger row"))
                                 for k in range(n)])
            elif n < 0:
                cur.execute("DELETE FROM ledger WHERE key = %s AND n IN (SELECT n FROM ledger "
                            "WHERE key = %s ORDER BY n DESC LIMIT %s)", [key, key, -n])
        cur.executemany("INSERT INTO events (seq, run_id, op, row_text) VALUES (%s, %s, %s, %s)",
                        [(e["seq"], e["run_id"], e["op"], canonical_row(e))
                         for e in sorted(delta["events"], key=lambda e: e["seq"])])
        if delta["rotation"] is not None:
            cur.execute("DELETE FROM rotation")
            cur.executemany("INSERT INTO rotation (name, value_text) VALUES (%s, %s)",
                            [(k, canonical_row(v)) for k, v in sorted(delta["rotation"].items())])
        if delta["backend_state"] is not None:
            cur.execute("DELETE FROM backend_state")
            cur.executemany("INSERT INTO backend_state (name, enabled, reason) VALUES (%s,%s,%s)",
                            [(k, bool(v["enabled"]), v.get("reason"))
                             for k, v in sorted(delta["backend_state"].items())])
    if delta["config"] is not None:
        st.pin_config(delta["config"], conn)


def _watermark(conn) -> str | None:
    rep = conn.execute("SELECT replication FROM state FOR UPDATE").fetchone()[0] or {}
    return rep.get("watermark")


def _advance(conn, parent: str | None, commit: str, stats: dict, extra: dict | None = None) -> None:
    conn.execute("UPDATE state SET generation = generation + 1, replication = replication || %s",
                 [psycopg.types.json.Jsonb({"watermark": commit, **(extra or {})})])
    conn.execute("INSERT INTO replication_receipts (commit, parent, stats) VALUES (%s, %s, %s)",
                 [commit, parent, psycopg.types.json.Jsonb(stats)])


# --- commands -------------------------------------------------------------------------------------

def do_import(st: store_pg.PgStore, rev: str, repo: Path = ROOT, log=print) -> None:
    commit = rev_parse(rev, repo)
    with st.writer(timeout=60) as w:
        conn = st._writer_conn(w)
        with conn.transaction():
            if _watermark(conn) is not None or conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM entries UNION ALL SELECT 1 FROM manifest)").fetchone()[0]:
                raise SystemExit("schema is not empty; import only into a fresh schema")
            totals = Counter()
            paths = tracked_paths(commit, repo)
            t0 = time.time()
            for n, p in enumerate(paths, 1):  # one file at a time: memory bounded by the largest
                d = commit_delta(None, commit, [p], repo)
                apply_delta(conn, st, d)
                totals.update({k: v for k, v in delta_stats(d).items() if isinstance(v, int)})
                if n % 25 == 0:
                    log(f"  {n}/{len(paths)} files, {time.time() - t0:.0f}s")
            if not any(kind_of(p) == "config" for p in paths):
                st.pin_config({}, conn)
            _advance(conn, None, commit, dict(totals),
                     {"imported_from": {"commit": commit, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                             time.gmtime())}})
    log(f"imported {commit[:10]}: {dict(totals)} in {time.time() - t0:.0f}s")


def first_parent_chain(watermark: str, target: str, repo: Path = ROOT) -> list[str]:
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{watermark}^{{commit}}"],
                      capture_output=True).returncode:
        raise SystemExit(f"watermark {watermark[:10]} is not in this repository (shallow or "
                         "rewritten history); re-import required")
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", watermark, target],
                      capture_output=True).returncode:
        raise SystemExit(f"watermark {watermark[:10]} is not an ancestor of {target[:10]}; "
                         "history was rewritten, re-import required")
    chain = git("rev-list", "--first-parent", "--reverse", f"{watermark}..{target}",
                repo=repo).split()
    if chain and git("rev-parse", f"{chain[0]}^1", repo=repo, check=False).strip() != watermark:
        raise SystemExit(f"watermark {watermark[:10]} is not on {target[:10]}'s first-parent "
                         "chain; history was rewritten, re-import required")
    return chain


def do_sync(st: store_pg.PgStore, target: str = "HEAD", repo: Path = ROOT, log=print,
            limit: int | None = None) -> int:
    target = rev_parse(target, repo)  # pinned once
    applied = 0
    with st.writer(timeout=60) as w:
        conn = st._writer_conn(w)
        with conn.transaction():
            watermark = _watermark(conn)
        if watermark is None:
            raise SystemExit("shadow has no watermark; run import first")
        for commit in first_parent_chain(watermark, target, repo)[:limit]:
            parent = git("rev-parse", f"{commit}^1", repo=repo).strip()
            with conn.transaction():
                if conn.execute("SELECT 1 FROM replication_receipts WHERE commit = %s",
                                [commit]).fetchone():
                    continue
                if _watermark(conn) != parent:
                    raise SystemExit(f"watermark moved concurrently; expected {parent[:10]}")
                d = commit_delta(parent, commit, changed_paths(parent, commit, repo), repo)
                apply_delta(conn, st, d)
                _advance(conn, parent, commit, delta_stats(d))
            applied += 1
    log(f"synced {applied} commit(s); watermark {target[:10] if applied else watermark[:10]}")
    return applied


class MultisetDigest:
    """Order-independent digest of a multiset of strings: count + sum of sha256 mod 2^256."""

    def __init__(self):
        self.n, self.acc = 0, 0

    def add(self, text: str) -> None:
        self.n += 1
        self.acc = (self.acc + int.from_bytes(hashlib.sha256(text.encode()).digest())) % MOD

    def value(self) -> str:
        return f"{self.n}:{self.acc:064x}"


def git_digests(rev: str, repo: Path = ROOT) -> dict[str, str]:
    """Per-table digests of the tracked files at `rev`, streamed file by file."""
    d = {t: MultisetDigest() for t in ("entries", "manifest", "blocklist", "ledger", "events")}
    rotation, backend_state, config = {}, {}, {}
    paths = tracked_paths(rev, repo)
    ledger_paths = [p for p in paths if kind_of(p) == "ledger"]
    if "registry/pruned.jsonl" in ledger_paths:  # the legacy monolith wins, as in FileStore
        ledger_paths = ["registry/pruned.jsonl"]
    for p in paths:
        kind = kind_of(p)
        if kind in ("entries", "manifest"):
            for row in parse(kind, git_bytes(rev, p, repo)).values():
                d[kind].add(canonical_row(row))
        elif kind == "blocklist":
            for u in set(parse(kind, git_bytes(rev, p, repo))):
                d["blocklist"].add(u)
        elif kind == "ledger" and p in ledger_paths:
            for row in parse(kind, git_bytes(rev, p, repo)):
                d["ledger"].add(canonical_row(row))
        elif kind == "events":
            for e in parse(kind, git_bytes(rev, p, repo)):
                d["events"].add(canonical_row(e))
        elif kind == "rotation":
            rotation = parse(kind, git_bytes(rev, p, repo))
        elif kind == "backend_state":
            backend_state = parse(kind, git_bytes(rev, p, repo))
        elif kind == "config":
            config[Path(p).name] = hashlib.sha256(git_bytes(rev, p, repo)).hexdigest()
    out = {k: v.value() for k, v in d.items()}
    out["rotation"] = store._digest(rotation)
    out["backend_state"] = store._digest({k: {"enabled": bool(v["enabled"]), "reason": v.get("reason")}
                                          for k, v in backend_state.items()})
    out["config"] = store._digest(config)
    return out


def pg_digests(st: store_pg.PgStore) -> tuple[str | None, dict[str, str]]:
    """Per-table digests of one consistent snapshot of the shadow, and its watermark."""
    with st._connect() as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        watermark = (conn.execute("SELECT replication FROM state").fetchone()[0] or {}).get("watermark")
        out = {}
        for table, expr in (("entries", "row_text"), ("manifest", "row_text"), ("blocklist", "url"),
                            ("ledger", "row_text"), ("events", "row_text")):
            dig = MultisetDigest()
            with conn.cursor(name=f"verify_{table}") as cur:
                cur.itersize = 20000
                cur.execute(f"SELECT {expr} FROM {table}")
                for (text,) in cur:
                    dig.add(text)
            out[table] = dig.value()
        out["rotation"] = store._digest({n: json.loads(t) for n, t in
                                         conn.execute("SELECT name, value_text FROM rotation")})
        out["backend_state"] = store._digest({n: {"enabled": e, "reason": r} for n, e, r in
                                              conn.execute("SELECT name, enabled, reason FROM "
                                                           "backend_state")})
        out["config"] = store._digest(dict(conn.execute("SELECT name, digest FROM config")))
        conn.rollback()
    return watermark, out


def do_verify(st: store_pg.PgStore, repo: Path = ROOT, log=print) -> bool:
    watermark, pg = pg_digests(st)
    if watermark is None:
        raise SystemExit("shadow has no watermark; run import first")
    files = git_digests(watermark, repo)
    bad = sorted(k for k in files if files[k] != pg.get(k))
    for k in sorted(files):
        log(f"  {k:14s} {'OK ' if k not in bad else 'DIFF'} git={files[k][:24]} pg={pg.get(k, '')[:24]}")
    log(f"verify at {watermark[:10]}: {'OK' if not bad else 'DIVERGED: ' + ', '.join(bad)}")
    return not bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("import", "sync", "verify", "status"))
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--limit", type=int, help="sync at most N commits")
    ap.add_argument("--dsn", default=os.environ.get("NEKAISE_PG_DSN", store_pg.DEFAULT_DSN))
    ap.add_argument("--schema", default=os.environ.get("NEKAISE_PG_SCHEMA", "nekaise"))
    args = ap.parse_args(argv)
    st = store_pg.PgStore(ROOT, dsn=args.dsn, schema=args.schema)
    if args.command == "import":
        do_import(st, args.commit)
    elif args.command == "sync":
        do_sync(st, args.commit, limit=args.limit)
    elif args.command == "verify":
        return 0 if do_verify(st) else 1
    else:
        with st._connect() as conn:
            gen, rep = conn.execute("SELECT generation, replication FROM state").fetchone()
            receipts = conn.execute("SELECT count(*), max(applied_at) FROM replication_receipts").fetchone()
        wm = (rep or {}).get("watermark")
        lag = len(git("rev-list", "--first-parent", f"{wm}..HEAD").split()) if wm else None
        print(json.dumps({"generation": gen, "watermark": wm, "commits_behind_head": lag,
                          "receipts": receipts[0], "last_applied": str(receipts[1])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
