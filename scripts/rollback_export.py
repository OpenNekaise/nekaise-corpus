#!/usr/bin/env python3
"""rollback_export.py — the PostgreSQL → FileStore rollback exporter (ADR 0001 stage 4 step 5;
used by step 6's rollback): a sharded legacy file layout materialized from one promoted
generation, verified against the database before anyone switches to it.

    python scripts/rollback_export.py export --target DIR [--generation G]
    python scripts/rollback_export.py verify --target DIR [--generation G]
    python scripts/rollback_export.py payloads --target DATA_ROOT [--generation G] [--dry-run]

`payloads` (link_payloads) is the payload half: every raw/text claim of G whose immutable version
is held here becomes a hard link at its legacy path (whatever was there first preserved as a
version), corpus/ must already be G's complete materialization, and corpus/.ruleset is set to
G's ruleset — so the legacy loader and cleaner find exactly G's bytes.

`export` writes, from ONE snapshot of generation G (default: the current one), exactly the
tracked layout FileStore reads (store.TRACKED_PATHS):

* registry/<shard>.yaml — entries routed by id prefix (state_codec.shard_filename), each shard
  a machine-shard header plus its entries in id order (the store keeps no file order);
* manifest/<shard>.jsonl — rows routed by state_codec.manifest_shard, in (topic, id) order, one
  JSON object per line (what FileStore's routed writer produces and validate_layout requires);
* pruned_urls.txt (the blocklist), registry/pruned-<bucket>.jsonl (the decision ledger, routed by
  id), registry/journal/<day>.jsonl (the legacy journal events, in sequence order, rolled like
  FileStore's), registry/rotation.json, registry/backend_state.json, registry/github_passes.json;
* the configuration documents (registry/<name>.json) as the generation pinned them — their
  exact bytes from the sealed configuration set.

Everything streams page by page in key order (entries by id; the manifest in its legacy
(shard, topic, id) order, so each shard file is written once, start to end): memory stays
bounded by one page plus one open file per registry shard. The tree is written under a temporary
sibling directory and renamed to DIR only when complete; DIR must not exist.

`verify` (run by `export` too) opens DIR as a FileStore and compares its canonical export
(store.export: every table, the runtime state and the configuration, row-order independent) with
the canonical export of generation G read from PostgreSQL, file digest by file digest. Only an
identical export makes the tree a valid rollback target; a difference exits 1 naming the files.
The FileStore side loads its tables in memory (the file store's own, accepted cost; this is the
fallback-window tool for today's corpus size, not a scale path).

Step 6's rollback (not this tool): fence writers, export the latest promoted generation here,
verify, then move the tree into the checkout and switch the host authority record back to file
(store_authority.write_record(..., lift_fence=True)) — never restore a stale cutover copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import state_codec as codec
import store
from store import Table

ROOT = Path(__file__).resolve().parents[1]


class ExportError(store.StoreError):
    """The export could not be produced or does not verify."""


def _scan(view, table: Table, order: str = "key"):
    cursor = None
    while True:
        page = view.scan(table, cursor=cursor, limit=store.MAX_PAGE, order=order)
        yield from page.rows
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def open_generation(st, generation: int | None):
    """A read view of generation `generation` (default: the current one)."""
    import store_staging
    if generation is None:
        with st.read() as view:
            generation = view.generation
        if generation is None:
            raise ExportError("no promoted generation to export")
    return store_staging.read_generation(st, generation)


def write_layout(view, out: Path) -> dict:
    """Write the legacy layout of `view` under `out` (which must be empty). Returns counts."""
    reg, man = out / "registry", out / "manifest"
    (reg / "journal").mkdir(parents=True)
    man.mkdir()
    counts = {}
    # registry shards: one append handle per shard (entries arrive in id order, not by shard)
    with ExitStack() as stack:
        shards: dict[str, object] = {}
        n = 0
        for e in _scan(view, Table.ENTRIES):
            name = codec.shard_filename(e["id"])
            f = shards.get(name)
            if f is None:
                f = shards[name] = stack.enter_context((reg / name).open("w", encoding="utf-8"))
                f.write(codec.shard_header(Path(name).stem))
            f.write(codec.emit_entry(e))
            n += 1
        counts["entries"] = n
    # manifest shards: legacy order is (shard file, topic, id): each file written start to end
    n, current, f = 0, None, None
    with ExitStack() as stack:
        for row in _scan(view, Table.MANIFEST, order="legacy"):
            stem = codec.manifest_shard(row["id"])
            if stem != current:
                f = stack.enter_context((man / f"{stem}.jsonl").open("w", encoding="utf-8"))
                current = stem
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
        counts["manifest"] = n
    with (out / "pruned_urls.txt").open("w", encoding="utf-8") as f:
        n = 0
        for row in _scan(view, Table.BLOCKLIST):
            f.write(f"{row['url']}\n")
            n += 1
        counts["blocklist"] = n
    with ExitStack() as stack:
        ledgers: dict[str, object] = {}
        n = 0
        for row in _scan(view, Table.LEDGER):
            name = codec.prune_ledger_name(row["id"])
            if name not in ledgers:
                ledgers[name] = stack.enter_context((reg / name).open("w", encoding="utf-8"))
            ledgers[name].write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
        counts["ledger"] = n
    counts["events"] = _write_journal(view, reg / "journal")
    rotation = view.rotation_get()
    if rotation:
        (reg / "rotation.json").write_text(
            json.dumps(rotation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    backend_state = {k: {"enabled": v.enabled, "reason": v.reason}
                     for k, v in sorted(view.backend_state_get().items())}
    if backend_state:
        (reg / store.BACKEND_STATE_FILE).write_text(json.dumps(
            backend_state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    for name in store.CONTROL_FILES:
        doc = view.control_get(name)
        if doc is not None:
            (reg / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False,
                                               sort_keys=True) + "\n", encoding="utf-8")
    for name, data in _config_bytes(view).items():
        (reg / name).write_bytes(data)
    return counts


def _write_journal(view, journal: Path) -> int:
    """The legacy journal events in sequence order, one file per UTC day of each event, rolled
    at store.JOURNAL_ROLL_BYTES like FileStore's."""
    n, day, index, size, f = 0, None, 0, 0, None
    try:
        for row in _scan(view, Table.EVENTS):
            line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            at = row.get("at")
            this = at[:10] if isinstance(at, str) and len(at) >= 10 else (day or "0000-00-00")
            if this != day or (size and size + len(line.encode()) > store.JOURNAL_ROLL_BYTES):
                index = 0 if this != day else index + 1
                day = this
                if f is not None:
                    f.close()
                name = f"{day}.jsonl" if index == 0 else f"{day}.{index:03d}.jsonl"
                f = (journal / name).open("a", encoding="utf-8")
                size = f.tell()
            f.write(line)
            size += len(line.encode())
            n += 1
    finally:
        if f is not None:
            f.close()
    return n


def _config_bytes(view) -> dict[str, bytes]:
    """The exact bytes of the configuration documents the view pins (a generation's sealed
    configuration set)."""
    import hashlib
    if view.generation is None:
        raise ExportError("the view pins no generation")
    rows = view._q(
        "SELECT m.name, b.bytes FROM generations g JOIN config_set_members m ON m.digest = "
        "g.config_digest JOIN config_blobs b ON b.sha256 = m.sha256 WHERE g.generation = %s",
        [view.generation]).fetchall()
    out = {}
    for name, data in rows:
        data = bytes(data)
        if name not in store.CONFIG_FILES or os.sep in name:
            raise ExportError(f"unexpected configuration document {name!r}")
        want = view.config_get().digests.get(name)
        if hashlib.sha256(data).hexdigest() != want:
            raise ExportError(f"configuration {name}: stored bytes do not match their digest")
        out[name] = data
    return out


def _export_digests(directory: Path) -> dict:
    return json.loads((directory / "EXPORT.json").read_text())


def verify(st, target: Path, generation: int | None, *, log=print) -> dict:
    """Compare the canonical export of the FileStore at `target` with generation G's."""
    target = Path(target)
    with tempfile.TemporaryDirectory(prefix="rollback-verify-") as tmp:
        with open_generation(st, generation) as view:
            g = view.generation
            store.export(Path(tmp) / "pg", view=view)
        fs = store.open(root=target, backend="file")   # an unbound fresh tree: file
        errors, _ = fs.validate_layout()
        if errors:
            raise ExportError(f"the exported layout does not validate: {errors[:5]}")
        with fs.read() as fview:
            store.export(Path(tmp) / "file", view=fview)
        a, b = _export_digests(Path(tmp) / "pg"), _export_digests(Path(tmp) / "file")
    bad = sorted(name for name in set(a["files"]) | set(b["files"])
                 if a["files"].get(name) != b["files"].get(name))
    if a.get("config_digests") != b.get("config_digests"):
        bad.append("config_digests")
    result = {"generation": g, "files": a["files"], "identical": not bad, "differs": bad}
    log(f"rollback export {target}: {'IDENTICAL' if not bad else 'DIFFERS'} to generation {g}"
        + (f" ({', '.join(bad)})" if bad else ""))
    return result


def export(st, target: Path, generation: int | None = None, *, log=print) -> dict:
    """Write generation G's legacy layout to `target` (must not exist), atomically, then
    verify it. Raises ExportError (the tree is left in place for inspection) when it does not
    verify."""
    target = Path(target).resolve()
    if target.exists():
        raise ExportError(f"{target} exists: export into a fresh directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.export-{secrets.token_hex(4)}"
    tmp.mkdir()
    try:
        with open_generation(st, generation) as view:
            g = view.generation
            counts = write_layout(view, tmp)
            dataset = (view.provenance() or {}).get("dataset")
        import artifact_store
        artifact_store.sync_filesystem(tmp)   # every file of the tree durable before it appears
        os.rename(tmp, target)
        store._fsync_dir(target.parent)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    result = verify(st, target, g, log=log)
    meta = {"generation": g, "dataset": dataset, "counts": counts, **result}
    ws = target / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "rollback-export.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    if not result["identical"]:
        raise ExportError(f"the export of generation {g} differs from the database: "
                          f"{result['differs']}")
    log(f"exported generation {g}: {counts}")
    return meta


def _file_sha256(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _regular(path: Path) -> bool:
    """A readable regular file (never a directory, symlink, device or socket)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(path, os.R_OK)


def verify_payloads(st, root: Path, generation: int | None = None, *, log=print) -> dict:
    """Check, without changing anything, that generation G's payloads are exactly available for a
    FileStore rollback: every raw/text claim of G is either a held immutable version whose bytes
    hash to the claim's identity (and, for raw, have the row's size) or a readable regular file
    at the claimed legacy path with those bytes; corpus/ is a complete materialization of G (its
    stamp) AND every member is a regular file at corpus/<id>.md whose bytes hash to the row's
    corpus_sha256, with no other document file in corpus/. Streams G's manifest a page at a
    time. Returns {"ok": bool, "failures": [...], counts}."""
    import artifact_store
    import materialize
    root = Path(root)
    local = artifact_store.LocalArtifacts(root)
    out = {"held": 0, "legacy": 0, "corpus_members": 0, "failures": [], "failure_count": 0}

    def fail(text):
        out["failure_count"] += 1
        if len(out["failures"]) < 50:
            out["failures"].append(text)

    corpus_dir = root / "corpus"
    with open_generation(st, generation) as view:
        g = view.generation
        prov = view.provenance() or {}
        out["generation"], out["ruleset"] = g, prov.get("cleaning_ruleset")
        restrictions, _ = store.pinned_policy(view)
        stamp = materialize.read_stamp(corpus_dir) or {}
        if (stamp.get("state"), stamp.get("generation"), stamp.get("dataset")) != (
                "complete", g, prov.get("dataset")):
            fail(f"corpus/ is not a complete materialization of generation {g} "
                 f"({stamp.get('state')} at {stamp.get('generation')})")
        members = materialize._Refresh(root, corpus_dir, view, restrictions)
        for row in _scan(view, Table.MANIFEST):
            sid = row["id"]
            for stage in ("raw", "text"):
                c = artifact_store.claim(row, stage)
                if c is None:
                    continue
                path, sha = c
                size = row.get("bytes") if stage == "raw" else None
                if isinstance(size, bool) or not isinstance(size, int):
                    size = None
                if not (isinstance(path, str) and path and not os.path.isabs(path)
                        and ".." not in Path(path).parts):
                    fail(f"{sid}: unsafe {stage} path {path!r}")
                    continue
                if not (isinstance(sha, str) and artifact_store.is_identity(sha)):
                    fail(f"{sid}: its {stage} claim has no identity to verify")
                    continue
                if local.has(stage, sha):
                    if local.verify(stage, sha, size):
                        out["held"] += 1
                    else:
                        fail(f"{sid}: its {stage} version {sha[:12]} is damaged")
                    continue
                target = root / path
                if not _regular(target):
                    fail(f"{sid}: its {stage} payload is held nowhere (no version, and "
                         f"{path} is not a readable regular file)")
                    continue
                got, n = _file_sha256(target)
                if got != sha or (size is not None and n != size):
                    fail(f"{sid}: {path} does not hold its {stage} claim ({sha[:12]})")
                    continue
                out["legacy"] += 1
            c = members.wanted(row)
            if c is None:
                continue
            out["corpus_members"] += 1
            dst = corpus_dir / f"{sid}.md"
            if not _regular(dst):
                fail(f"{sid}: corpus/{sid}.md is missing or not a regular file")
            elif _file_sha256(dst)[0] != c[1]:
                fail(f"{sid}: corpus/{sid}.md does not hold its cleaned claim")
    present = sum(1 for p in corpus_dir.glob("*.md")) if corpus_dir.is_dir() else 0
    if present != out["corpus_members"]:
        fail(f"corpus/ holds {present} document files, generation {g} has "
             f"{out['corpus_members']} members")
    out["ok"] = not out["failure_count"]
    log(f"payloads of generation {g}: {'verified' if out['ok'] else 'NOT verified'} "
        f"({out['held']} held versions, {out['legacy']} legacy files, "
        f"{out['corpus_members']} corpus members, {out['failure_count']} failure(s))")
    return out


def link_payloads(st, root: Path, generation: int | None = None, *, dry_run: bool = False,
                  log=print) -> dict:
    """The payload half of a rollback: the legacy pipeline reads raw/ and text/ at each row's
    claimed path, but a staged run wrote its payloads only as immutable versions under
    artifacts/. FIRST verify_payloads (every claim verified by hash and size, corpus/ verified
    file by file against G): any failure raises ExportError before anything is changed — the
    caller switches authority only after this returns. Then every raw/text claim whose verified
    version is held becomes a hard link at its legacy path (a different file already there is
    first preserved as a version, then replaced by an atomic rename), one syncfs makes the new
    names durable, and corpus/.ruleset is set to G's ruleset (the policy that produced corpus/)."""
    import artifact_store
    root = Path(root)
    checked = verify_payloads(st, root, generation, log=log)
    if not checked["ok"]:
        raise ExportError(f"generation {checked['generation']}'s payloads do not verify "
                          f"({checked['failure_count']} failure(s)): {checked['failures'][:5]} — "
                          "nothing was changed; do not switch authority")
    local = artifact_store.LocalArtifacts(root)
    out = {"linked": 0, "already": 0, "legacy": checked["legacy"], "preserved": 0,
           "dry_run": dry_run, "generation": checked["generation"]}
    with open_generation(st, checked["generation"]) as view:
        for row in _scan(view, Table.MANIFEST):
            for stage in ("raw", "text"):
                c = artifact_store.claim(row, stage)
                if c is None or not local.has(stage, c[1]):
                    continue
                path, sha = c
                target, version = root / path, local.path(stage, sha)
                try:
                    have = os.lstat(target)
                except FileNotFoundError:
                    have = None
                vst = os.lstat(version)
                if have is not None and (have.st_ino, have.st_dev) == (vst.st_ino, vst.st_dev):
                    out["already"] += 1
                    continue
                out["linked"] += 1
                if dry_run:
                    continue
                if have is not None:   # never lose bytes: a file there becomes a version
                    if not stat.S_ISREG(have.st_mode):
                        raise ExportError(f"{path} is not a regular file; nothing more linked")
                    local.adopt(stage, target)
                    out["preserved"] += 1
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.rollback-{secrets.token_hex(4)}")
                os.link(version, tmp)
                os.replace(tmp, target)
    if not dry_run:
        artifact_store.sync_filesystem(root)
        import ops
        ops.atomic_write_text(root / "corpus" / ".ruleset", f"{checked['ruleset']}\n")
    log(f"payloads of generation {out['generation']}: {out}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("export", "verify", "payloads"))
    ap.add_argument("--dry-run", action="store_true", help="payloads: count only")
    ap.add_argument("--target", required=True)
    ap.add_argument("--generation", type=int, default=None)
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    import store_pg
    st = store.open(root=Path(args.root).resolve())
    if not isinstance(st, store_pg.PgStore):
        print("ERROR: the store here is not PostgreSQL: nothing to roll back from",
              file=sys.stderr)
        return 2
    try:
        if args.command == "export":
            export(st, Path(args.target), args.generation)
        elif args.command == "payloads":
            # --target is the data root whose raw/ and text/ receive the links
            # verifies every claim and corpus/ first; raises (nothing changed) on any failure
            link_payloads(st, Path(args.target), args.generation, dry_run=args.dry_run)
        else:
            if not verify(st, Path(args.target), args.generation)["identical"]:
                return 1
    except store.StoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
