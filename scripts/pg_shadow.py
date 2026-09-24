#!/usr/bin/env python3
"""pg_shadow.py — keep the PostgreSQL store a verified shadow of the git-tracked files
(ADR 0001, stage 2).

FileStore stays authoritative until the stage-4 cutover. Every successful legacy round is already
a durable git commit, so the shadow replays COMMITS, never the working tree:

    python scripts/pg_shadow.py import [--commit REV]   # load a commit into an empty schema
    python scripts/pg_shadow.py sync                    # replay first-parent commits since the watermark
    python scripts/pg_shadow.py verify [--commit REV]   # compare the shadow with git at its watermark
    python scripts/pg_shadow.py status
    python scripts/pg_shadow.py enable                  # under the round lock: require --commit rounds

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
# Present while a shadow replicates this checkout: run_round then refuses uncommitted rounds, since
# the shadow only sees commits. Delete it to retire the shadow.
SHADOW_MARKER = ROOT / "workspace" / ".pg-shadow"
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


# --- replaying one revision step ------------------------------------------------------------------

def _put_rows(cur, table: str, rows) -> None:
    extra = table == "manifest"
    cols = "id, row_text, url_norm, url_key, title_norm, title_key" + (", sha256" if extra else "")
    ph = ", ".join(["%s"] * (7 if extra else 6))
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols.split(", ")[1:])
    params = []
    for r in rows:
        store.validate_json(r, f"{table} row {r.get('id')!r}")
        rec = [r["id"], canonical_row(r), *store_pg._keys_for(r)]
        if extra:
            sha = r.get("sha256")
            rec.append(sha if isinstance(sha, str) and sha else None)
        params.append(rec)
    cur.executemany(f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT (id) DO UPDATE SET "
                    f"{sets}", params)


def effective_ledger_paths(paths: list[str]) -> list[str]:
    """FileStore's precedence: the legacy monolith, when present, is the whole ledger."""
    ledger = [p for p in paths if kind_of(p) == "ledger"]
    return ["registry/pruned.jsonl"] if "registry/pruned.jsonl" in ledger else ledger


def replay(conn, st: store_pg.PgStore, parent: str | None, commit: str, paths: list[str],
           repo: Path = ROOT, log=None) -> dict:
    """Apply the row-level difference between `parent` (None = empty) and `commit`, restricted to
    the changed `paths`, inside the caller's transaction. Memory is bounded by one file plus the
    id set of the changed keyed files: a row that moves between shards is found through that id
    set, so it is updated rather than deleted."""
    stats = Counter()
    by_kind: dict[str, list[str]] = {}
    for p in paths:
        if (k := kind_of(p)) is not None:
            by_kind.setdefault(k, []).append(p)
    old = (lambda p, k: parse(k, git_bytes(parent, p, repo))) if parent else \
        (lambda p, k: parse(k, None))
    new = lambda p, k: parse(k, git_bytes(commit, p, repo))  # noqa: E731
    with conn.cursor() as cur:
        for kind in ("entries", "manifest"):
            files = by_kind.get(kind, [])
            # Only ids that leave or enter a file are remembered across files, so memory is
            # bounded by one file plus the size of the change: a row that moves between shards
            # both departs and arrives and is therefore updated, never deleted.
            departed: set[str] = set()
            arrived: set[str] = set()
            for n, p in enumerate(files, 1):
                before, after = old(p, kind), new(p, kind)
                departed.update(i for i in before if i not in after)
                arrived.update(i for i in after if i not in before)
                puts = [r for i, r in after.items() if not store.same_row(before.get(i), r)]
                for i in range(0, len(puts), 5000):
                    _put_rows(cur, kind, puts[i:i + 5000])
                stats[f"{kind}_put"] += len(puts)
                if log and n % 25 == 0:
                    log(f"  {kind}: {n}/{len(files)} files")
            gone = sorted(departed - arrived)
            if gone:
                cur.execute(f"DELETE FROM {kind} WHERE id = ANY(%s)", [gone])
            stats[f"{kind}_del"] += len(gone)
        for p in by_kind.get("blocklist", []):
            b, a = set(old(p, "blocklist")), set(new(p, "blocklist"))
            if b - a:
                cur.execute("DELETE FROM blocklist WHERE key = ANY(%s)",
                            [[key_digest(u) for u in b - a]])
            for u in a - b:
                store.validate_json(u, "blocklist url")
            cur.executemany("INSERT INTO blocklist (key, url) VALUES (%s, %s) ON CONFLICT DO "
                            "NOTHING", [(key_digest(u), u) for u in sorted(a - b)])
            stats["blocklist_add"] += len(a - b)
            stats["blocklist_del"] += len(b - a)
        ledger_files = by_kind.get("ledger", [])
        if ledger_files:
            legacy = "registry/pruned.jsonl"
            if legacy in ledger_files or (parent and git_bytes(parent, legacy, repo) is not None) \
                    or git_bytes(commit, legacy, repo) is not None:
                # the monolith appeared, changed or disappeared: rebuild the ledger from scratch
                cur.execute("DELETE FROM ledger")
                counts = Counter()
                for p in effective_ledger_paths(tracked_paths(commit, repo)):
                    counts.update(canonical_row(r) for r in new(p, "ledger"))
            else:
                counts = Counter()
                for p in ledger_files:  # keep only each file's net change
                    diff = Counter(canonical_row(r) for r in new(p, "ledger"))
                    diff.subtract(canonical_row(r) for r in old(p, "ledger"))
                    counts.update({t: n for t, n in diff.items() if n})
            for text, n in sorted(counts.items()):
                key = key_digest(text)
                if n > 0:
                    base = cur.execute("SELECT COALESCE(max(n) + 1, 0) FROM ledger WHERE key = %s",
                                       [key]).fetchone()[0]
                    cur.executemany("INSERT INTO ledger (key, n, row_text) VALUES (%s, %s, %s)",
                                    [(key, base + k, text) for k in range(n)])
                elif n < 0:
                    cur.execute("DELETE FROM ledger WHERE key = %s AND n IN (SELECT n FROM ledger "
                                "WHERE key = %s ORDER BY n DESC LIMIT %s)", [key, key, -n])
                stats["ledger"] += abs(n)
        journal = by_kind.get("events", [])
        if journal:
            removed, appeared = {}, {}
            for p in journal:  # per file, remembering only events that left or entered it
                b = {e["seq"]: e for e in old(p, "events")}
                a = {e["seq"]: e for e in new(p, "events")}
                removed.update({q: e for q, e in b.items() if not store.same_row(e, a.get(q))})
                appeared.update({q: e for q, e in a.items() if not store.same_row(e, b.get(q))})
            for seq, e in removed.items():  # an event may only move between files unchanged
                if not store.same_row(e, appeared.get(seq)):
                    raise SystemExit(f"{commit[:10]} rewrites or removes journal event {seq}; the "
                                     "journal is append-only, re-import required")
            added = [appeared[q] for q in sorted(set(appeared) - set(removed))]
            cur.executemany("INSERT INTO events (seq, run_id, op, row_text) VALUES (%s,%s,%s,%s)",
                            [(e["seq"], e["run_id"], e["op"], canonical_row(e)) for e in added])
            stats["events"] += len(added)
        if "rotation" in by_kind:
            rot = new("registry/rotation.json", "rotation")
            cur.execute("DELETE FROM rotation")
            cur.executemany("INSERT INTO rotation (name, value_text) VALUES (%s, %s)",
                            [(k, canonical_row(v)) for k, v in sorted(rot.items())])
            stats["rotation"] += 1
        if "backend_state" in by_kind:
            bs = new(f"registry/{store.BACKEND_STATE_FILE}", "backend_state")
            cur.execute("DELETE FROM backend_state")
            cur.executemany("INSERT INTO backend_state (name, enabled, reason) VALUES (%s,%s,%s)",
                            [(k, bool(v["enabled"]), v.get("reason")) for k, v in sorted(bs.items())])
    if "config" in by_kind or parent is None:
        st.pin_config({name: data for name in store.CONFIG_FILES
                       if (data := git_bytes(commit, f"registry/{name}", repo)) is not None}, conn)
        stats["config"] += 1
    return dict(stats)


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
    t0 = time.time()
    with st.writer(timeout=60) as w:
        conn = st._writer_conn(w)
        with conn.transaction():
            occupied = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM entries UNION ALL SELECT 1 FROM manifest UNION ALL "
                "SELECT 1 FROM blocklist UNION ALL SELECT 1 FROM ledger UNION ALL SELECT 1 FROM "
                "events UNION ALL SELECT 1 FROM rotation UNION ALL SELECT 1 FROM backend_state "
                "UNION ALL SELECT 1 FROM replication_receipts)").fetchone()[0]
            if _watermark(conn) is not None or occupied or conn.execute(
                    "SELECT generation FROM state").fetchone()[0] != 0:
                raise SystemExit("schema is not empty; import only into a fresh schema")
            stats = replay(conn, st, None, commit, tracked_paths(commit, repo), repo, log)
            _advance(conn, None, commit, stats,
                     {"imported_from": {"commit": commit, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                             time.gmtime())}})
    log(f"imported {commit[:10]}: {stats} in {time.time() - t0:.0f}s")


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
                stats = replay(conn, st, parent, commit, changed_paths(parent, commit, repo), repo)
                _advance(conn, parent, commit, stats)
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
        config = {}
        for name, text, digest in conn.execute("SELECT name, doc_text, digest FROM config"):
            actual = hashlib.sha256(text.encode()).hexdigest()
            config[name] = actual if actual == digest else f"stored digest {digest} != {actual}"
        out["config"] = store._digest(config)
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


def enable(dsn: str, schema: str, repo: Path = ROOT) -> None:
    """Activate capture: under the canonical round lock, with every tracked change committed,
    write the marker that makes run_round refuse uncommitted rounds. Everything before this point
    is in git, so import + sync cover it; everything after is committed by construction."""
    import ops
    with ops.named_lock("corpus-round", timeout=600, workspace=repo / "workspace"):
        if dirty := [l for l in git("status", "--porcelain", "--", *TRACKED, repo=repo)
                     .splitlines() if l]:
            raise SystemExit(f"{len(dirty)} uncommitted tracked change(s); commit or recover "
                             "them before enabling the shadow")
        marker = repo / "workspace" / ".pg-shadow"
        marker.parent.mkdir(parents=True, exist_ok=True)
        ops.atomic_write_text(marker, f"{dsn}\n{schema}\n")
    print(f"shadow capture enabled at {rev_parse('HEAD', repo)[:10]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("import", "sync", "verify", "status", "enable"))
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--limit", type=int, help="sync at most N commits")
    ap.add_argument("--dsn", default=os.environ.get("NEKAISE_PG_DSN", store_pg.DEFAULT_DSN))
    ap.add_argument("--schema", default=os.environ.get("NEKAISE_PG_SCHEMA", "nekaise"))
    args = ap.parse_args(argv)
    st = store_pg.PgStore(ROOT, dsn=args.dsn, schema=args.schema)
    if args.command == "import":
        do_import(st, args.commit)
    elif args.command == "enable":
        enable(args.dsn, args.schema)
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
        dirty = [l for l in git("status", "--porcelain", "--", *TRACKED).splitlines() if l]
        print(json.dumps({"generation": gen, "watermark": wm, "commits_behind_head": lag,
                          "uncommitted_tracked_changes": len(dirty),
                          "receipts": receipts[0], "last_applied": str(receipts[1])}, indent=2))
        if dirty:
            print("WARNING: tracked state has uncommitted changes the shadow cannot see",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
