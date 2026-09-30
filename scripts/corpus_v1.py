#!/usr/bin/env python3
"""corpus_v1.py — build corpus_v1/: every corpus/ document, cleaned for LLM training.

corpus_v1/ is the training view for continued pretraining / mid-training of a small
built-environment LLM. It holds EVERY document of the default view corpus/ (same ids), cleaned
from the verbatim text/<id>.md in two layers:

1. the deterministic ruleset (scripts/v1_rules.py, RULESET_VERSION) — patents parsed from their
   page template, page furniture removed, paragraphs re-flowed; applied to every file;
2. model repairs (scripts/sonnet_clean.py) — broken text (bad OCR) rewritten into normal text
   by a model and checked; stored in corpus_v1/.revisions/<id>.md and used instead of layer 1
   while their recorded source key still matches text/<id>.md.

It is incremental: a file is rebuilt only when its text/ source changed, the ruleset version
changed, or its revision changed; ids that left corpus/ are removed. The nightly job
(scripts/corpus_v1_night.py) runs it first, so documents added by the dig loop reach corpus_v1/
within a day.

    python scripts/corpus_v1.py               # incremental build
    python scripts/corpus_v1.py --ids a.md b.md
    python scripts/corpus_v1.py --report      # coverage, ruleset, revisions, damage ranking
    python scripts/corpus_v1.py --sample 20 --show   # print cleaned samples
    python scripts/corpus_v1.py --audit 400 --out DIR  # THIS checkout's rules vs live corpus_v1

State lives in corpus_v1/.state.sqlite: one row per id with the source key (size, mtime_ns),
ruleset, revision key, output size and the OCR damage score that ranks model repair.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import multiprocessing
from pathlib import Path

import v1_rules

HERE = Path(__file__).resolve().parents[1]
# The data (text/, corpus/, corpus_v1/) is never in git: a checkout elsewhere (the night
# session's worktree) points here at the live repo's data with CORPUS_V1_DATA.
DATA = Path(os.environ.get("CORPUS_V1_DATA", HERE))
TEXT = DATA / "text"
CORPUS = DATA / "corpus"
OUT = DATA / "corpus_v1"
REVISIONS = OUT / ".revisions"
DB = OUT / ".state.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS build (
  id TEXT PRIMARY KEY, src_size INTEGER, src_mtime INTEGER, ruleset TEXT, revision TEXT,
  in_chars INTEGER, out_chars INTEGER, damage REAL, kind TEXT, built_at REAL);
CREATE TABLE IF NOT EXISTS revision (
  id TEXT PRIMARY KEY, src_size INTEGER, src_mtime INTEGER, src_sha256 TEXT, model TEXT,
  prompt TEXT, status TEXT, parts INTEGER, parts_fallback INTEGER, parts_refused INTEGER,
  in_chars INTEGER, out_chars INTEGER, at TEXT);
"""


def connect() -> sqlite3.Connection:
    OUT.mkdir(exist_ok=True)
    REVISIONS.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=60)
    con.executescript(SCHEMA)
    return con


def split_doc(text: str) -> tuple[str, list[str]]:
    """(header through the '---' line, body lines). No header -> ('', all lines)."""
    lines = text.split("\n")
    for i, x in enumerate(lines[:40]):
        if x.strip() == "---":
            return "\n".join(lines[: i + 1]), lines[i + 1:]
    return "", lines


def source_key(doc_id: str) -> tuple[int, int] | None:
    try:
        st = (TEXT / doc_id).stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


def rule_clean(doc_id: str) -> tuple[str, str, int, float | None, str]:
    """(header, cleaned body, input chars, damage score, kind) by the deterministic rules."""
    text = (TEXT / doc_id).read_text(encoding="utf-8", errors="replace")
    header, body = split_doc(text)
    cleaned = "\n".join(v1_rules.clean_body(body, header))
    kind = "patent" if v1_rules.is_patent(header) else "doc"
    return header, cleaned, len(text), v1_rules.damage_score(cleaned), kind


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_one(args: tuple[str, str | None]) -> tuple:
    """Clean and publish one document. Membership is checked immediately before the write and
    again after it: a document that left the training view meanwhile is unpublished, and the
    caller drops its state row (("REMOVED", id)). Every full build also ends with a sweep."""
    doc_id, revision = args
    if not is_member(doc_id):
        return ("REMOVED", doc_id)
    header, body, in_chars, damage, kind = rule_clean(doc_id)
    if revision:  # a checked model repair of this exact source overrides the rules
        body = (REVISIONS / doc_id).read_text(encoding="utf-8")
    out = (header + "\n\n" if header else "") + body.strip() + "\n"
    if not is_member(doc_id):
        return ("REMOVED", doc_id)
    write_atomic(OUT / doc_id, out)
    if not is_member(doc_id):
        (OUT / doc_id).unlink(missing_ok=True)
        return ("REMOVED", doc_id)
    size, mtime = source_key(doc_id) or (0, 0)
    return (doc_id, size, mtime, v1_rules.RULESET_VERSION, revision, in_chars, len(out), damage,
            kind, time.time())


def valid_revisions(con, ids: list[str] | None = None) -> dict[str, str]:
    """id -> revision key for revisions whose recorded source still matches text/."""
    out = {}
    sql = "SELECT id, src_size, src_mtime, src_sha256, prompt FROM revision WHERE status='ok'"
    rows = (con.execute(sql).fetchall() if ids is None else
            [r for i in ids for r in con.execute(sql + " AND id=?", (i,)).fetchall()])
    for doc_id, size, mtime, sha, prompt in rows:
        if prompt not in TRUSTED_REVISION_PROMPTS or not valid_id(doc_id):
            continue
        if source_key(doc_id) == (size, mtime) and (REVISIONS / doc_id).exists():
            out[doc_id] = f"{prompt}:{sha[:12]}"
    return out


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,250}\.md")
# Model repairs are trusted only if made under the current checks: p2 repairs were validated by
# the checks the 2026-09-30 maintainer review found unsafe, so they no longer override rules.
TRUSTED_REVISION_PROMPTS = frozenset({"p3"})


def valid_id(doc_id: str) -> bool:
    """A document id is a plain file name: no directories, no '..'. Checked before any path is
    built from an id, so no id can reach outside corpus_v1/ (or read outside text/)."""
    return bool(_ID.fullmatch(doc_id)) and ".." not in doc_id


def is_member(doc_id: str) -> bool:
    """corpus_v1/ mirrors the default training view: only ids present in corpus/ belong."""
    return valid_id(doc_id) and (CORPUS / doc_id).is_file()


def remove(con, doc_id: str) -> None:
    if not valid_id(doc_id):
        raise ValueError(f"refusing a non-plain document id: {doc_id!r}")
    (OUT / doc_id).unlink(missing_ok=True)
    con.execute("DELETE FROM build WHERE id=?", (doc_id,))
    con.commit()


def rebuild_one(con, doc_id: str) -> None:
    """Rebuild one file in-process (after a model repair), honouring its current revision.
    A document that left the training view meanwhile is removed, never written."""
    if not is_member(doc_id):
        remove(con, doc_id)
        return
    res = build_one((doc_id, valid_revisions(con, [doc_id]).get(doc_id)))
    if res[0] == "REMOVED":
        remove(con, doc_id)
        return
    con.execute("INSERT OR REPLACE INTO build VALUES (?,?,?,?,?,?,?,?,?,?)", res)
    con.commit()


def build(ids: list[str] | None, workers: int, deadline: float | None = None) -> dict:
    con = connect()
    t0 = time.time()
    live = {e.name for e in os.scandir(CORPUS) if e.name.endswith(".md")}
    removed = 0
    if ids is None:
        for (doc_id,) in con.execute("SELECT id FROM build").fetchall():
            if doc_id not in live and valid_id(doc_id):
                (OUT / doc_id).unlink(missing_ok=True)
                con.execute("DELETE FROM build WHERE id=?", (doc_id,))
                removed += 1
        con.commit()
    have = {r[0]: r[1:] for r in con.execute(
        "SELECT id, src_size, src_mtime, ruleset, revision FROM build")}
    revs = valid_revisions(con)
    todo = []
    invalid = 0
    for doc_id in (ids if ids is not None else sorted(live)):
        if not valid_id(doc_id):  # never turned into a path, never removed: just refused
            invalid += 1
            print(f"  refusing non-plain id {doc_id!r}", file=sys.stderr)
            continue
        if doc_id not in live:  # an explicit id outside the training view: never written
            if (OUT / doc_id).exists() or doc_id in have:
                remove(con, doc_id)
                removed += 1
            continue
        key = source_key(doc_id)
        if key is None:
            continue
        want = (*key, v1_rules.RULESET_VERSION, revs.get(doc_id))
        if ids is not None or have.get(doc_id) != want:
            todo.append((doc_id, revs.get(doc_id)))
    # New documents (from the dig loop) first, then ruleset/revision rebuilds.
    todo.sort(key=lambda a: a[0] in have)
    done = errors = 0
    batch: list[tuple] = []

    def flush():
        nonlocal done, batch
        con.executemany("INSERT OR REPLACE INTO build VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
        con.commit()
        done += len(batch)
        batch = []
        print(f"  built {done}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)

    # Unordered streaming: one large scanned book never holds a batch of small files hostage.
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        for res in pool.imap_unordered(_safe_build, todo, chunksize=16):
            if res is None:
                errors += 1
            elif res[0] == "REMOVED":
                remove(con, res[1])
                removed += 1
            else:
                batch.append(res)
            if len(batch) >= 5000:
                flush()
            if deadline and time.time() > deadline:
                pool.terminate()
                break
        flush()
    if ids is None:  # closing sweep: whatever left corpus/ while the build ran is unpublished
        live = {e.name for e in os.scandir(CORPUS) if e.name.endswith(".md")}
        for e in os.scandir(OUT):
            if e.name.endswith(".md") and e.name not in live and valid_id(e.name):
                remove(con, e.name)
                removed += 1
    return {"todo": len(todo), "built": done, "errors": errors, "removed": removed,
            "invalid_ids": invalid,
            "revisions_used": sum(1 for _, r in todo if r), "secs": round(time.time() - t0)}


def _safe_build(args):
    try:
        return build_one(args)
    except Exception as e:  # noqa: BLE001 — one unreadable file never stops the build
        print(f"  error {args[0]}: {e}", file=sys.stderr)
        return None


def report() -> int:
    con = connect()
    live = sum(1 for e in os.scandir(CORPUS) if e.name.endswith(".md"))
    rows = con.execute("SELECT count(*), sum(in_chars), sum(out_chars) FROM build").fetchone()
    print(f"corpus/ docs: {live}; corpus_v1/ built: {rows[0]} "
          f"({rows[2] or 0:,} of {rows[1] or 0:,} chars kept)")
    for rs, n in con.execute("SELECT ruleset, count(*) FROM build GROUP BY ruleset"):
        print(f"  ruleset {rs}: {n}")
    print("  revisions:", dict(con.execute("SELECT status, count(*) FROM revision GROUP BY status")))
    buckets = con.execute("""SELECT CASE WHEN damage IS NULL THEN 'n/a' WHEN damage<0.05 THEN '<0.05'
        WHEN damage<0.1 THEN '0.05-0.1' WHEN damage<0.2 THEN '0.1-0.2' ELSE '>=0.2' END b, count(*)
        FROM build WHERE kind='doc' GROUP BY b""").fetchall()
    print("  OCR damage (non-patent docs):", dict(buckets))
    return 0


def show(n: int, seed: int) -> int:
    ids = [e.name for e in os.scandir(OUT) if e.name.endswith(".md")]
    for doc_id in random.Random(seed).sample(ids, min(n, len(ids))):
        text = (OUT / doc_id).read_text(encoding="utf-8")
        print(f"\n===== {doc_id} ({len(text)} chars)\n{text[:1500]}")
    return 0


def audit(n: int, seed: int, out_dir: Path) -> int:
    """Compare this checkout's rules with the live corpus_v1/ on a stratified sample, writing
    nothing to corpus_v1/. Per file: kept-character ratio old vs new, and the source lines the new
    rules no longer keep (what a reviewer must look at). Model-repaired files are skipped."""
    out_dir.mkdir(parents=True, exist_ok=True)
    con = connect()
    rows = con.execute("SELECT id, kind, damage FROM build").fetchall()
    rng = random.Random(seed)
    groups: dict[str, list[str]] = {}
    for doc_id, kind, _ in rows:
        groups.setdefault(doc_id.split("-", 1)[0], []).append(doc_id)
    patents = groups.pop("pat", [])
    pick: list[str] = rng.sample(patents, min(n // 4, len(patents)))  # a quarter: one template
    per = max(1, (n - len(pick)) // max(1, len(groups)))
    for g in sorted(groups, key=lambda g: -len(groups[g])):
        pick += rng.sample(groups[g], min(per, len(groups[g])))
    pick = pick[:n]
    revised = set(valid_revisions(con))
    summary, changed = [], 0
    with (out_dir / "changes.md").open("w", encoding="utf-8") as fh:
        for doc_id in pick:
            if doc_id in revised or not (OUT / doc_id).exists():
                continue
            header, body, in_chars, damage, kind = rule_clean(doc_id)
            new = (header + "\n\n" if header else "") + body.strip() + "\n"
            old = (OUT / doc_id).read_text(encoding="utf-8")
            if new == old:
                continue
            changed += 1
            new_set = set(x.strip() for x in new.split("\n"))
            gone = [x for x in old.split("\n") if x.strip() and x.strip() not in new_set
                    and x.strip() not in new]
            summary.append((doc_id, len(old), len(new), len(gone)))
            fh.write(f"\n## {doc_id}  old {len(old)} -> new {len(new)} chars, "
                     f"{len(gone)} old lines no longer present\n")
            for x in gone[:15]:
                fh.write(f"- `{x[:200]}`\n")
    lost = sum(o - n for _, o, n, _ in summary if n < o)
    report = {"sampled": len(pick), "changed": changed,
              "chars_removed_net": lost, "worst": sorted(summary, key=lambda r: r[2] / max(r[1], 1))[:15]}
    (out_dir / "summary.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    print(f"details: {out_dir / 'changes.md'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--max-seconds", type=int, default=0)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--audit", type=int, default=0, help="audit N sampled docs (writes nothing)")
    ap.add_argument("--out", type=Path, default=HERE / "workspace" / "corpus-v1-audit")
    a = ap.parse_args()
    if a.audit:
        return audit(a.audit, a.seed, a.out)
    if a.report:
        return report()
    if a.show:
        return show(a.sample or 10, a.seed)
    deadline = time.time() + a.max_seconds if a.max_seconds else None
    print(json.dumps(build(a.ids, a.workers, deadline)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
