#!/usr/bin/env python3
"""Rebuildable SQLite acceleration index for registry/manifest lookups.

Git-tracked YAML/JSONL remains the source of truth.  This git-ignored database only avoids parsing
~130 MB of tracked state in every finder process.  A cheap size/mtime signature invalidates it
whenever any source shard or the URL blocklist changes; deleting the DB is always safe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import yaml

import ops

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "workspace" / "corpus-index.sqlite3"


def _norm(value: str) -> str:
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def _paths(reg_dir: Path, man_dir: Path, blocklist_path: Path) -> list[Path]:
    return (
        sorted(reg_dir.glob("*.yaml"))
        + sorted(man_dir.glob("*.jsonl"))
        + ([blocklist_path] if blocklist_path.exists() else [])
    )


def source_signature(reg_dir: Path, man_dir: Path, blocklist_path: Path) -> str:
    rows = []
    for path in _paths(reg_dir, man_dir, blocklist_path):
        st = path.stat()
        rows.append((str(path.resolve()), st.st_size, st.st_mtime_ns))
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _db_path(reg_dir: Path, requested: Path | None) -> Path:
    if requested:
        return requested
    if reg_dir.resolve() == (ROOT / "registry").resolve():
        return DEFAULT_DB
    return reg_dir.parent / "workspace" / "corpus-index.sqlite3"


def _current_signature(db: Path) -> str | None:
    if not db.exists():
        return None
    try:
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='source_signature'").fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def rebuild(reg_dir: Path, man_dir: Path, blocklist_path: Path,
            db_path: Path | None = None) -> Path:
    reg_dir, man_dir, blocklist_path = map(Path, (reg_dir, man_dir, blocklist_path))
    db = _db_path(reg_dir, db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    signature = source_signature(reg_dir, man_dir, blocklist_path)
    fd, tmp_name = tempfile.mkstemp(prefix=".corpus-index.", suffix=".sqlite3", dir=db.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with sqlite3.connect(tmp) as conn:
            conn.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE known (
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (kind, value)
                ) WITHOUT ROWID;
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    url TEXT,
                    title TEXT,
                    sha256 TEXT,
                    status TEXT,
                    source TEXT,
                    license TEXT,
                    topic TEXT,
                    format TEXT,
                    language TEXT,
                    manifest_shard TEXT
                );
            """)
            known_batch: list[tuple[str, str]] = []

            def add_known(entry: dict) -> None:
                sid = entry.get("id") or ""
                url = (entry.get("url") or "").rstrip("/")
                title = _norm(entry.get("title") or "")
                if sid:
                    known_batch.append(("id", sid))
                if url:
                    known_batch.append(("url", url))
                if title:
                    known_batch.append(("title", title))

            for path in sorted(reg_dir.glob("*.yaml")):
                for entry in (yaml.safe_load(path.read_text()) or {}).get("sources") or []:
                    add_known(entry)
                    if len(known_batch) >= 10_000:
                        conn.executemany("INSERT OR IGNORE INTO known VALUES (?,?)", known_batch)
                        known_batch.clear()

            for path in sorted(man_dir.glob("*.jsonl")):
                batch = []
                with path.open() as f:
                    for line in f:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        add_known(row)
                        batch.append((
                            row.get("id"), row.get("url"), row.get("title"), row.get("sha256"),
                            row.get("status"), row.get("source"), row.get("license"),
                            row.get("topic"), row.get("format"), row.get("language"), path.stem,
                        ))
                        if len(batch) >= 2_000:
                            conn.executemany(
                                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                batch,
                            )
                            batch.clear()
                    if batch:
                        conn.executemany(
                            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch,
                        )
                    if len(known_batch) >= 10_000:
                        conn.executemany("INSERT OR IGNORE INTO known VALUES (?,?)", known_batch)
                        known_batch.clear()

            if blocklist_path.exists():
                for line in blocklist_path.read_text().splitlines():
                    if value := line.strip().rstrip("/"):
                        known_batch.append(("url", value))
            if known_batch:
                conn.executemany("INSERT OR IGNORE INTO known VALUES (?,?)", known_batch)
            conn.execute("INSERT INTO meta VALUES ('source_signature', ?)", (signature,))
            conn.commit()
        os.replace(tmp, db)
    finally:
        if tmp.exists():
            tmp.unlink()
    return db


def ensure(reg_dir: Path, man_dir: Path, blocklist_path: Path,
           db_path: Path | None = None) -> Path:
    db = _db_path(Path(reg_dir), db_path)
    signature = source_signature(Path(reg_dir), Path(man_dir), Path(blocklist_path))
    if _current_signature(db) == signature:
        return db
    with ops.named_lock("corpus-index", timeout=120):
        if _current_signature(db) != signature:
            rebuild(Path(reg_dir), Path(man_dir), Path(blocklist_path), db)
    return db


def existing_keys(reg_dir: Path, man_dir: Path, blocklist_path: Path,
                  include_blocklist: bool = True, db_path: Path | None = None):
    # A separate no-blocklist index would double storage. Query registry+manifest from documents
    # and YAML is not possible without another provenance column in known, so the rare caller that
    # excludes the blocklist uses the canonical parser fallback in registry.py.
    if not include_blocklist:
        raise ValueError("indexed no-blocklist lookup is unsupported")
    db = ensure(reg_dir, man_dir, blocklist_path, db_path)
    with sqlite3.connect(db) as conn:
        values = {
            kind: {row[0] for row in conn.execute("SELECT value FROM known WHERE kind=?", (kind,))}
            for kind in ("url", "title", "id")
        }
    return values["url"], values["title"], values["id"]


def record_appended_entries(reg_dir: Path, man_dir: Path, blocklist_path: Path,
                            entries: list[dict], prior_signature: str,
                            db_path: Path | None = None) -> bool:
    """Keep a fresh index fresh after registry.append_entries without a full rebuild.

    Returns False if the cache was already stale before the append; correctness then falls back to
    ensure() rebuilding it on the next lookup.
    """
    db = _db_path(Path(reg_dir), db_path)
    with ops.named_lock("corpus-index", timeout=120):
        if _current_signature(db) != prior_signature:
            return False
        batch = []
        for entry in entries:
            sid = entry.get("id") or ""
            url = (entry.get("url") or "").rstrip("/")
            title = _norm(entry.get("title") or "")
            batch.extend(
                (kind, value) for kind, value in (("id", sid), ("url", url), ("title", title))
                if value
            )
        with sqlite3.connect(db) as conn:
            conn.executemany("INSERT OR IGNORE INTO known VALUES (?,?)", batch)
            signature = source_signature(Path(reg_dir), Path(man_dir), Path(blocklist_path))
            conn.execute(
                "UPDATE meta SET value=? WHERE key='source_signature'", (signature,),
            )
            conn.commit()
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("rebuild", "status"), nargs="?", default="status")
    args = ap.parse_args()
    reg, man, blocked = ROOT / "registry", ROOT / "manifest", ROOT / "pruned_urls.txt"
    db = rebuild(reg, man, blocked) if args.command == "rebuild" else ensure(reg, man, blocked)
    with sqlite3.connect(db) as conn:
        docs = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        known = conn.execute("SELECT count(*) FROM known").fetchone()[0]
    print(f"{db}: {docs} documents / {known} known keys")


if __name__ == "__main__":
    main()
