#!/usr/bin/env python3
"""artifact_gc.py — reference-checked garbage-collection REPORT for local artifact versions
(ADR 0001 stage 4 step 5). DRY RUN ONLY: this tool has no code path that deletes, moves or
rewrites anything; it reports what a later, reviewed collector could remove.

    python scripts/artifact_gc.py [--grace-days 30] [--sample 20]

A version artifacts/<stage>/<aa>/<bb>/<sha256> is a collection CANDIDATE only when all hold:

* no manifest row claims it (stage, sha256) — in the projection, or in any revision of a run
  that is not aborted (open, frozen and promoted runs; promoted revisions are the history of
  every generation, so raw originals and verbatim text ever admitted stay referenced forever,
  as ADR 0001 section 5 requires);
* no run that is not aborted references it (run_artifacts);
* it is not linked from anywhere else (st_nlink == 1: a materialized corpus/ file, or a legacy
  raw/text/corpus file adopted by hard link, keeps it);
* it is older than --grace-days (30 by default, the ADR's grace period): a version written by a
  run that is staging right now may not have its reference committed yet, and the grace makes
  that race harmless.

Bounded memory: the referenced identities stream from PostgreSQL in (stage, sha256) order (a
server-side sort) and the local versions are listed in the same order directory by directory (the
content-addressed layout is sorted by its own fan-out); the two sorted streams are merged. The
report goes to workspace/artifact-gc-report.json and stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator

import ops
import store

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "workspace" / "artifact-gc-report.json"
GRACE_DAYS = 30

REFERENCED_SQL = """
SELECT stage, sha FROM (
    SELECT s.stage, nk_claim(m.row, s.stage) ->> 1 AS sha
      FROM manifest m CROSS JOIN (VALUES ('raw'), ('text'), ('corpus')) s(stage)
    UNION ALL
    SELECT s.stage, nk_claim(r.row_text::jsonb, s.stage) ->> 1
      FROM revisions r JOIN runs u ON u.run_id = r.run_id
      CROSS JOIN (VALUES ('raw'), ('text'), ('corpus')) s(stage)
     WHERE r.tbl = 'manifest' AND r.op = 'put' AND u.status <> 'aborted'
    UNION ALL
    SELECT a.stage, a.sha256 FROM run_artifacts a JOIN runs u ON u.run_id = a.run_id
     WHERE u.status <> 'aborted'
) refs WHERE sha IS NOT NULL ORDER BY stage COLLATE "C", sha COLLATE "C"
"""


def referenced(conn) -> Iterator[tuple[str, str]]:
    """Every referenced (stage, sha256), sorted, duplicates included (a server-side cursor)."""
    with conn.cursor(name="artifact_gc_refs") as cur:
        cur.itersize = 20_000
        cur.execute(REFERENCED_SQL)
        for stage, sha in cur:
            yield stage, sha


def local_versions(root: Path, misplaced: list | None = None) -> Iterator[tuple[str, str,
                                                                              os.stat_result]]:
    """Every local version, sorted by (stage, sha256): each fan-out directory listed and sorted
    on its own (at most one directory's names in memory). Only the content-addressed layout
    counts — artifacts/<stage>/<sha[:2]>/<sha[2:4]>/<sha>, regular files — so the order is the
    identities' order; anything else found there (a stray file or directory, a version filed
    under the wrong fan-out) is appended to `misplaced` and never reported as collectable."""
    import stat as stat_

    import artifact_store
    misplaced = [] if misplaced is None else misplaced
    base = Path(root) / artifact_store.DIRNAME
    hexdir = re.compile(r"[0-9a-f]{2}")
    for stage in sorted(artifact_store.STAGES):
        top = base / stage
        if not top.is_dir():
            continue
        for aa in sorted(os.listdir(top)):
            if not (hexdir.fullmatch(aa) and (top / aa).is_dir()):
                misplaced.append(f"{stage}/{aa}")
                continue
            for bb in sorted(os.listdir(top / aa)):
                d = top / aa / bb
                if not (hexdir.fullmatch(bb) and d.is_dir()):
                    misplaced.append(f"{stage}/{aa}/{bb}")
                    continue
                for name in sorted(os.listdir(d)):
                    if not (artifact_store.is_identity(name) and name[:2] == aa
                            and name[2:4] == bb):
                        misplaced.append(f"{stage}/{aa}/{bb}/{name}")
                        continue
                    try:
                        st = os.lstat(d / name)
                    except FileNotFoundError:
                        continue
                    if not stat_.S_ISREG(st.st_mode):
                        misplaced.append(f"{stage}/{aa}/{bb}/{name}")
                        continue
                    yield stage, name, st


def report(st, root: Path, *, grace_days: float = GRACE_DAYS, sample: int = 20,
           now: float | None = None) -> dict:
    """Merge the two sorted streams (module docstring); nothing is changed."""
    now = time.time() if now is None else now
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "dry_run": True,
           "grace_days": grace_days, "scanned": 0, "scanned_bytes": 0, "referenced": 0,
           "linked_elsewhere": 0, "young": 0, "misplaced": 0, "misplaced_sample": [],
           "candidates": {"count": 0, "bytes": 0, "by_stage": {}, "sample": []}}
    misplaced: list[str] = []
    # autocommit, then an explicit transaction: its isolation level applies to the one
    # snapshot the named cursor streams from
    with st._connect(autocommit=True) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        refs = _checked_order(referenced(conn))
        ref = next(refs, None)
        for stage, sha, info in local_versions(root, misplaced):
            out["scanned"] += 1
            out["scanned_bytes"] += info.st_size
            key = (stage, sha)
            while ref is not None and ref < key:
                ref = next(refs, None)
            if ref == key:
                out["referenced"] += 1
                continue
            if info.st_nlink > 1:
                out["linked_elsewhere"] += 1
                continue
            if now - info.st_mtime < grace_days * 86400:
                out["young"] += 1
                continue
            c = out["candidates"]
            c["count"] += 1
            c["bytes"] += info.st_size
            per = c["by_stage"].setdefault(stage, {"count": 0, "bytes": 0})
            per["count"] += 1
            per["bytes"] += info.st_size
            if len(c["sample"]) < sample:
                c["sample"].append(f"{stage}/{sha}")
        conn.execute("ROLLBACK")
    out["misplaced"], out["misplaced_sample"] = len(misplaced), misplaced[:sample]
    return out


def _checked_order(refs):
    """The referenced stream, refusing to go on if it is not sorted (the merge would then
    report referenced versions as candidates)."""
    prev = None
    for ref in refs:
        if prev is not None and ref < prev:
            raise RuntimeError(f"referenced identities out of order ({prev} then {ref}): "
                               "the merge would be wrong; nothing reported")
        prev = ref
        yield ref


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--grace-days", type=float, default=GRACE_DAYS)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    import staged_runs
    st = store.open(root=root)
    if not staged_runs.staged_authority(st):
        print("file authority: no artifact versions are referenced through PostgreSQL here; "
              "nothing to report")
        return 0
    out = report(st, root, grace_days=args.grace_days, sample=args.sample)
    ops.atomic_write_text(root / "workspace" / "artifact-gc-report.json",
                          json.dumps(out, indent=1, sort_keys=True) + "\n")
    c = out["candidates"]
    print(f"DRY RUN — scanned {out['scanned']} versions ({out['scanned_bytes'] / 1e9:.2f} GB): "
          f"referenced {out['referenced']}, linked elsewhere {out['linked_elsewhere']}, "
          f"younger than {args.grace_days:g} days {out['young']}; candidates {c['count']} "
          f"({c['bytes'] / 1e9:.2f} GB). Nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
