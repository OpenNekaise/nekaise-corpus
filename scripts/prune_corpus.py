#!/usr/bin/env python3
"""prune_corpus.py — quality gate. Drop low-value docs after a load.

Next-token CPT memorizes text literally, so junk in = junk out. This removes, from the manifest +
registry + disk, docs that are: failed downloads, thin/empty, garbage extractions (symbol soup from
scanned / math-heavy PDFs), non-English, off-topic (too few built-environment keywords; books use a
length-scaled density gate), or byte-duplicates (same sha256 under two ids). It prunes only
*machine-discovered* sources (registry.DISCOVERED_PREFIXES); hand-curated originals are left alone.
Verdicts come from the quality metrics build_corpus stored in each manifest row (scripts/quality.py)
— no text re-reading. Dropped URLs land in pruned_urls.txt so discovery never re-churns them.

Writes go through the store (ADR 0001 stage 3, step 6): the decisions are computed from one read
view, exactly as before, then ONE transaction applies survivor metric updates, registry and
manifest deletions (tombstones carry the reason), blocklist additions and decision-ledger rows —
through the round's broker inside a round, under this command's own writer standalone. The
dropped documents' bytes are quarantined before that transaction and deleted only once it is
settled (see "artifact side effects" below).

    python scripts/prune_corpus.py            # dry run -- report what would be pruned
    python scripts/prune_corpus.py --apply    # prune (delete files, rewrite manifest + registry)
    python scripts/prune_corpus.py --drop-ids-from reviewed.txt --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import blocklist
import corpus_stats
import host_policy
import ops
import quality
import registry
import store
import store_broker

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
TRANSIENT_FETCH_STATUSES = frozenset({202, 429, 503})
DNS_ERROR_MARKERS = (
    "nameresolutionerror",
    "failed to resolve",
    "name or service not known",
    "temporary failure in name resolution",
    "getaddrinfo failed",
    "nodename nor servname provided",
    "no address associated with hostname",
)
REPEATED_DNS_MIN_RUNS = 3
REPEATED_DNS_MIN_DAYS = 3
# MDPI's Akamai edge returns a host-wide 403 to both requests and curl from this operator's
# network (site root and article PDFs, re-probed 2026-08-28).  That is not evidence that an
# individual CC-BY article URL is dead.  Keep this narrow: other hosts can use 403 for a durable
# access-policy decision and need their own reviewed classification.
TRANSIENT_403_HOSTS = frozenset({"mdpi.com"})
# Durable retry window for rows the loader marked `transient` (polite-host challenges, circuit
# skips, network timeouts — build_corpus.POLITE_HOSTS). Discovery cursors advance before the
# fetch, so pruning such a row would lose it for good; instead it stays in registry + manifest and
# every later round's loader retries it, until either bound below is reached. Then it is pruned
# as "failed" but NEVER blocklisted, so a later re-walk can still rediscover it.
RETRY_MAX_ATTEMPTS = 20
RETRY_MAX_AGE_DAYS = 14


def retry_pending(row: dict, now: datetime | None = None) -> bool:
    """Whether a failed row is still inside its transient retry window.

    A transient row that was never actually requested (retry_attempts 0: circuit-skipped) has no
    window yet and is preserved until a real attempt starts one.
    """
    if row.get("status") == "ok" or not row.get("transient"):
        return False
    attempts = int(row.get("retry_attempts") or 0)
    if attempts == 0:
        return True
    if attempts >= RETRY_MAX_ATTEMPTS:
        return False
    try:
        first = datetime.strptime(str(row.get("first_failed_at")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return now - first < timedelta(days=RETRY_MAX_AGE_DAYS)


def _usable_text(row: dict) -> bool:
    text_path = row.get("text_path")
    return row.get("status") == "ok" and bool(text_path) and (HERE / text_path).exists()


class HandoffError(RuntimeError):
    """This run's loader deferral handoff is missing, corrupt or from another run."""


def deferred_ids(path: Path | None = None) -> set[str]:
    """Ids this round's loader only deferred (build_corpus.write_deferred), never judged.

    The handoff is keyed by a non-empty shared run token (NEKAISE_RUN_ID, set by run_round for
    both steps). Standalone prunes (no run id) ignore any handoff file entirely. Inside a round
    the handoff is expected: missing, corrupt or mismatched fails closed with HandoffError rather
    than silently dropping the protection.
    """
    run_id = os.environ.get("NEKAISE_RUN_ID") or ""
    if not run_id:
        return set()
    path = path or ops.WORKSPACE / "fetch-deferred.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HandoffError(f"loader deferral handoff unreadable for run {run_id}: {exc}") from exc
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not data.get("run_id") or not isinstance(ids, list):
        raise HandoffError(f"loader deferral handoff malformed for run {run_id}")
    if data["run_id"] != run_id:
        raise HandoffError(
            f"loader deferral handoff belongs to run {data['run_id']!r}, not {run_id!r}")
    return {str(sid) for sid in ids}


def protected_ids(manifest: list[dict], policy: dict[str, dict],
                  deferred: set[str]) -> set[str]:
    """Rows the pruner must not judge this run: suspended-host rows (a fetch suspension keeps
    held documents as they are and never ages the rest) and rows the loader only deferred."""
    return {
        r["id"] for r in manifest
        if r["id"] in deferred or host_policy.suspended(r.get("url"), policy)
    }


def _host_matches(url: str | None, domains: set[str] | frozenset[str]) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _is_dns_resolution_error(error: str | None) -> bool:
    err = (error or "").lower()
    return any(marker in err for marker in DNS_ERROR_MARKERS)


def repeated_dns_failure_urls(ledger: list[dict]) -> set[str]:
    """Return URLs whose DNS failures span enough independent runs and days to be durable.

    Both guards matter: retries inside one run do not create separate ledger rows, but a local or
    provider-wide DNS incident can still affect many rounds on one day.  Rows without the modern
    run/date provenance do not count toward permanent suppression.
    """
    evidence: dict[str, tuple[set[str], set[date]]] = {}
    for row in ledger:
        if row.get("reason") != "failed" or not _is_dns_resolution_error(row.get("error")):
            continue
        url = blocklist.normalize(row.get("url"))
        run_id = row.get("run_id")
        stamp = row.get("pruned_at")
        if not url or not isinstance(run_id, str) or not run_id or not isinstance(stamp, str):
            continue
        try:
            day = date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        runs, days = evidence.setdefault(url, (set(), set()))
        runs.add(run_id)
        days.add(day)
    return {
        url for url, (runs, days) in evidence.items()
        if len(runs) >= REPEATED_DNS_MIN_RUNS and len(days) >= REPEATED_DNS_MIN_DAYS
    }


def _blocklistable(row: dict, reason: str,
                   repeated_dns_urls: set[str] | frozenset[str] = frozenset()) -> bool:
    """Return whether a prune verdict is durable enough for the URL blocklist."""
    if reason != "failed":
        return True
    if row.get("transient"):
        return False  # loader-classified challenge / circuit skip / timeout on a polite host
    status = row.get("http_status")
    if status in TRANSIENT_FETCH_STATUSES:
        return False
    if status == 403 and _host_matches(row.get("url"), TRANSIENT_403_HOSTS):
        return False
    err = (row.get("error") or "").lower()
    if _is_dns_resolution_error(err):
        return blocklist.normalize(row.get("url")) in repeated_dns_urls
    if "certificate" in err or "ssl" in err:
        return True  # cert mismatch = decommissioned/re-pointed host, permanently dead
    return not any(k in err for k in ("timeout", "timed out", "connection", "too many requests"))


def reviewed_title_drops(spec: str | None, manifest: list[dict]) -> dict[str, str]:
    """Load explicitly reviewed off-topic ids from a file (or stdin for ``-``).

    This gives maintenance reviews a provenance-preserving path through the normal prune ledger
    without teaching the global text gate a source-specific exception. Fail closed on unknown or
    hand-curated ids: a stale review list must not silently target a different corpus state, and
    explicit title review never overrides the curated-source protection boundary.
    """
    if not spec:
        return {}
    lines = sys.stdin.read().splitlines() if spec == "-" else Path(spec).read_text().splitlines()
    ids = {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}
    if not ids:
        raise ValueError("reviewed id list is empty")
    by_id = {r["id"]: r for r in manifest}
    unknown = sorted(ids - set(by_id))
    if unknown:
        raise ValueError(f"reviewed id list contains unknown ids: {', '.join(unknown[:5])}")
    protected = sorted(sid for sid in ids if not registry.discovered(sid))
    if protected:
        raise ValueError(f"reviewed id list contains hand-curated ids: {', '.join(protected[:5])}")
    return {sid: "off-topic-title" for sid in ids}


def prune_ledger_rows(manifest: list[dict], drop: dict[str, str], blocklisted_urls: set[str],
                      *, now: str | None = None) -> list[dict]:
    """The decision-ledger rows for `drop`, in id order.

    pruned_urls.txt remains the fast compatibility blocklist.  The sharded ledger preserves why a
    source was rejected so discovery quality can be measured by backend/query over time. The rows
    are appended by the prune transaction (FileStore: registry/pruned-<bucket>.jsonl, the bytes
    the legacy appender wrote)."""
    if not drop:
        return []
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run_id = os.environ.get("NEKAISE_RUN_ID")
    by_id = {r["id"]: r for r in manifest}
    rows = []
    for sid in sorted(drop):
        r = by_id[sid]
        rows.append({
            "id": sid,
            "url": r.get("url"),
            "title": r.get("title"),
            "reason": drop[sid],
            "source": r.get("source"),
            "topic": r.get("topic"),
            "license": r.get("license"),
            "http_status": r.get("http_status"),
            "error": r.get("error"),
            "sha256": r.get("sha256"),
            "quality": r.get("quality"),
            "blocklisted": blocklist.normalize(r.get("url")) in blocklisted_urls,
            "pruned_at": now,
            **({"run_id": run_id} if run_id else {}),
        })
    return rows


@dataclass
class Plan:
    """Prune decisions over one read view (nothing written yet)."""
    manifest: list[dict]          # every row, legacy manifest order
    drop: dict[str, str]          # id -> reason
    quality: dict[str, dict]      # id -> {"quality": metrics} for survivors whose metrics were
                                  # computed now (pre-metrics rows)
    retrying: int
    protected: set[str]


def decide(manifest: list[dict], reviewed_drop: dict[str, str], policy: dict[str, dict],
           deferred: set[str]) -> Plan:
    """The pruning decisions, exactly as the legacy pruner made them. `manifest` must be in the
    legacy manifest order (the first-seen title wins). Rows lacking metrics get them computed
    from their text (in place); the survivors among them are Plan.quality."""
    protected = protected_ids(manifest, policy, deferred)
    # near-dup gate: seed titles are always kept; a discovered doc whose normalized title already
    # exists (same paper from another source, v1/v2, etc.) is dropped so CPT doesn't over-weight it.
    # Only a copy whose text is actually retained here may claim a title or bytes and displace an
    # alternative: a suspended-host row missing locally must never destroy an available mirror.
    seen_titles = {registry.norm(r.get("title")) for r in manifest
                   if not registry.discovered(r["id"]) and r.get("status") == "ok"
                   and not registry.suspended_unavailable(r, policy, HERE)}
    drop: dict[str, str] = dict(reviewed_drop)
    computed: list[str] = []
    retrying = 0
    for r in manifest:
        if not registry.discovered(r["id"]):
            continue
        if r["id"] in reviewed_drop:
            continue
        if r["id"] in protected:
            if _usable_text(r):  # a held protected doc with retained text claims its title
                seen_titles.add(registry.norm(r.get("title")))
            continue
        if r["status"] != "ok":
            if retry_pending(r):
                retrying += 1
                continue  # kept in registry + manifest; the next round's loader retries it
            drop[r["id"]] = "failed"
            continue
        if r["id"].startswith("pat-") and quality.off_domain_title(r.get("title", "")):
            drop[r["id"]] = "off-topic-title"
            continue
        tp = r.get("text_path")
        if not tp or not (HERE / tp).exists():
            drop[r["id"]] = "no-text"
            continue
        m = r.get("quality")
        if not m:  # pre-metrics row: compute once from the file; persisted on --apply
            m = r["quality"] = quality.metrics(quality.body((HERE / tp).read_text()))
            computed.append(r["id"])
        q = quality.verdict(m, quality.is_booklike(r["id"], r.get("format", "pdf")))
        if q != "ok":
            drop[r["id"]] = q
            continue
        tk = registry.norm(r.get("title"))
        if tk and tk in seen_titles:
            drop[r["id"]] = "dup-title"
        else:
            seen_titles.add(tk)

    # byte-dup gate: identical sha256 under two ids (same file reached via two URLs) would be
    # double-weighted by CPT. Drop the discovered copy, keep curated; curated==curated is reported.
    by_sha: dict[str, list] = {}
    for r in manifest:
        if (r.get("status") == "ok" and r.get("sha256") and r["id"] not in drop
                and (r["id"] not in protected or _usable_text(r))):
            by_sha.setdefault(r["sha256"], []).append(r)
    for twins in by_sha.values():
        if len(twins) < 2:
            continue
        # curated first, then protected (never dropped here), stable by id
        twins.sort(key=lambda r: (registry.discovered(r["id"]) and r["id"] not in protected,
                                  r["id"]))
        for r in twins[1:]:
            if r["id"] in protected:
                continue
            if registry.discovered(r["id"]):
                drop[r["id"]] = "dup-bytes"
            else:
                print(f"  note: same bytes, both hand-curated (kept): {twins[0]['id']} == {r['id']}")
    by_id = {r["id"]: r for r in manifest}
    kept_metrics = {sid: {"quality": by_id[sid]["quality"]} for sid in computed
                    if sid not in drop}
    return Plan(manifest, drop, kept_metrics, retrying, protected)


# --- artifact side effects: a per-transaction quarantine ------------------------------------------
#
# A prune deletes metadata (registry entry, manifest row) in one store transaction and bytes
# (raw/, text/, corpus/) that no transaction can restore. So the bytes of the dropped documents
# are MOVED into workspace/prune-quarantine/<transaction>/ before the transaction runs, and only
# deleted once its outcome is settled:
#   * committed, standalone (or in the maintainer's window): purged right after the commit;
#   * committed inside a round: kept until the round ends — run_round purges them when the round
#     succeeds and moves them back when it rolls the round's metadata back;
#   * unknown (the transaction failed or the process died): the next prune (or run_round
#     --recover) settles it by the store's state — the dropped rows still present means the
#     transaction did not commit (or was rolled back), so the bytes go back; all absent means it
#     committed, so they are deleted. Settling is idempotent and resumable.

QUARANTINE_DIR = "prune-quarantine"


def quarantine_root(root: Path) -> Path:
    return Path(root) / "workspace" / QUARANTINE_DIR


def _write_record(qdir: Path, record: dict) -> None:
    ops.atomic_write_text(qdir / "record.json", json.dumps(record, indent=2, sort_keys=True) + "\n")


def quarantine_files(root: Path, txn: str, rows: list[dict], run: str | None) -> Path | None:
    """Move the raw/text/corpus files of `rows` (the dropped documents) into the quarantine of
    transaction `txn`. Returns its directory, or None when there is nothing to move."""
    root = Path(root)
    base = root.resolve()
    files: list[str] = []
    for r in rows:
        for key in ("raw_path", "text_path", "corpus_path"):
            rel = r.get(key)
            if not rel or rel in files or not (root / rel).exists():
                continue
            if base not in (root / rel).resolve().parents:
                print(f"  note: {r['id']}: {key} {rel!r} is outside the repository; left in place")
                continue
            files.append(rel)
    if not files:
        return None
    qdir = quarantine_root(root) / txn
    if qdir.exists():
        raise RuntimeError(f"prune quarantine {qdir} already exists; settle it first")
    qdir.mkdir(parents=True)
    record = {"txn": txn, "run": run, "ids": sorted(r["id"] for r in rows), "files": files,
              "state": "moving"}
    _write_record(qdir, record)  # before any move: a crash leaves a record to settle
    for rel in files:
        target = qdir / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(root / rel, target)
    record["state"] = "moved"
    _write_record(qdir, record)
    return qdir


def mark_committed(qdir: Path) -> None:
    record = json.loads((qdir / "record.json").read_text())
    record["state"] = "committed"
    _write_record(qdir, record)


def _restore(root: Path, qdir: Path, record: dict) -> int:
    moved = 0
    for rel in record.get("files") or []:
        src = qdir / "files" / rel
        if not src.exists():
            continue  # never moved (the move was interrupted) or already back
        target = Path(root) / rel
        if target.exists():
            src.unlink()  # something newer took the path; it wins
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, target)
        moved += 1
    return moved


def settle_quarantines(root: Path, *, view=None, run: str | None = None,
                       outcome: str | None = None) -> dict[str, int]:
    """Finish pending prune quarantines (all, or those of round `run`): restore their files or
    delete them. `outcome` ("committed" or "rolled_back", known to run_round) decides for
    transactions recorded as committed; everything else is decided by `view` — the dropped rows
    all present means restore, all absent means delete. Returns counts per action."""
    counts = {"restored": 0, "purged": 0}
    qroot = quarantine_root(root)
    if not qroot.exists():
        return counts
    for qdir in sorted(p for p in qroot.iterdir() if p.is_dir()):
        record_path = qdir / "record.json"
        if not record_path.exists():  # created, but nothing recorded, so nothing moved
            shutil.rmtree(qdir)
            continue
        record = json.loads(record_path.read_text())
        if run is not None and record.get("run") != run:
            continue
        if record.get("state") == "committed" and outcome in ("committed", "rolled_back"):
            action = "purge" if outcome == "committed" else "restore"
        else:
            if view is None:
                raise RuntimeError(f"prune quarantine {qdir.name}: outcome unknown, needs a view")
            ids = record.get("ids") or []
            present = view.get_manifest(ids)
            if len(present) == len(ids):
                action = "restore"
            elif not present:
                action = "purge"
            else:
                raise RuntimeError(f"prune quarantine {qdir.name}: {len(present)} of {len(ids)} "
                                   "dropped rows still exist; settle it by hand")
        _settle(root, qdir, action, record)
        counts["restored" if action == "restore" else "purged"] += 1
    return counts


def _settle(root: Path, qdir: Path, action: str, record: dict | None = None) -> None:
    """Restore a quarantine's files ("restore") or delete them ("purge"), then drop it."""
    if action == "restore":
        _restore(root, qdir, record or json.loads((qdir / "record.json").read_text()))
    shutil.rmtree(qdir)


def _retained_by_round(session) -> bool:
    """Inside run_round's round the quarantine outlives the step: the round settles it."""
    run = os.environ.get("NEKAISE_RUN_ID") or ""
    return bool(run) and session.brokered and session.round_id == run


def plan_prune(view, args, ap) -> Plan:
    manifest = list(corpus_stats.iter_manifest(view))  # legacy order: first-seen rules need it
    try:
        reviewed_drop = reviewed_title_drops(args.drop_ids_from, manifest)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    policy = host_policy.load()
    try:
        deferred = deferred_ids()
    except HandoffError as exc:
        print(f"ERROR: {exc}; refusing to prune without the loader handoff", file=sys.stderr)
        raise SystemExit(1)
    return decide(manifest, reviewed_drop, policy, deferred)


def report(plan: Plan) -> None:
    disc_total = sum(1 for r in plan.manifest if registry.discovered(r["id"]))
    print(f"discovered docs: {disc_total} | would prune: {dict(Counter(plan.drop.values()))} "
          f"(total {len(plan.drop)}); {plan.retrying} transient failures kept for retry; "
          f"{len(plan.protected)} protected (suspended host / deferred)")


def ledger_evidence(view) -> list[dict]:
    """Prior DNS-failure decisions (the only ledger rows repeated_dns_failure_urls reads)."""
    rows, cursor = [], None
    while True:
        page = view.scan(store.Table.LEDGER, where=store.Eq("reason", "failed"), cursor=cursor,
                         limit=store.MAX_PAGE)
        rows.extend(page.rows)
        if page.next_cursor is None:
            return rows
        cursor = page.next_cursor


def apply(session, plan: Plan) -> None:
    """ONE store transaction: survivor metric updates, registry + manifest deletions (tombstones
    carry the reason), blocklist additions and ledger rows. Bytes go to the quarantine first."""
    manifest, drop = plan.manifest, plan.drop
    # Blocklist policy: quality-fails and HARD fetch failures (404/410, fake PDFs) never return.
    # TRANSIENT walls (429/202/503, reviewed host-wide 403s, timeouts, connection errors) are NOT
    # blocklisted — the entry leaves the registry but may be rediscovered once the wall lifts
    # (IBPSA's sgcaptcha and Wikimedia rate limits taught us this the hard way). DNS resolution
    # failures become durable only after three prior runs across three days, avoiding endless
    # churn on retired hosts without turning a same-day resolver outage into permanent policy.
    repeated_dns_urls = repeated_dns_failure_urls(ledger_evidence(session.view)) if drop else set()
    block_urls = {
        blocklist.normalize(r.get("url")) for r in manifest
        if r["id"] in drop
        and _blocklistable(r, drop[r["id"]], repeated_dns_urls)
        and r.get("url")
    }
    ledger = prune_ledger_rows(manifest, drop, block_urls)
    by_reason: dict[str, list[str]] = {}
    for sid in sorted(drop):
        by_reason.setdefault(drop[sid], []).append(sid)
    dropped_rows = [r for r in manifest if r["id"] in drop]
    txn = session.identity("apply")
    qdir = quarantine_files(HERE, txn, dropped_rows, os.environ.get("NEKAISE_RUN_ID")) \
        if drop else None
    removed = blocked = 0
    try:
        with session.batch("apply") as b:
            if plan.quality:
                b.update_manifest_fields(plan.quality)
            for reason in sorted(by_reason):
                b.delete_entries(by_reason[reason], reason=f"prune: {reason}")
            for reason in sorted(by_reason):
                b.delete_manifest(by_reason[reason], reason=f"prune: {reason}")
            if block_urls:
                b.blocklist_add(sorted(block_urls))
            if ledger:
                b.ledger_append(ledger)
        results = iter(getattr(b, "results", []))
        if plan.quality:
            next(results)
        removed = sum(next(results) for _ in by_reason)
        for _ in by_reason:
            next(results)
        blocked = next(results) if block_urls else 0
    except BaseException:
        if qdir is not None:
            print(f"prune transaction {txn} failed; its files stay in {qdir} until a later prune "
                  "(or the round's rollback) settles them", file=sys.stderr)
        raise
    if qdir is not None:
        mark_committed(qdir)
        if not _retained_by_round(session):
            _settle(HERE, qdir, "purge")
    keep = len(manifest) - len(drop)
    good_disc = sum(1 for r in manifest
                    if r["id"] not in drop and registry.discovered(r["id"]) and r["status"] == "ok")
    print(f"pruned {len(drop)} docs ({removed} registry entries removed, {blocked} urls "
          f"blocklisted); kept {good_disc} good discovered docs, {keep} manifest rows")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--drop-ids-from",
        metavar="PATH",
        help="newline-delimited, reviewed discovered ids to prune as off-topic-title; '-' = stdin",
    )
    ap.add_argument("--lock-timeout", type=float, default=30,
                    help="standalone runs wait this long for the round lock (default 30 s)")
    args = ap.parse_args()

    st = store.open(root=HERE)
    if not args.apply:
        with st.read(timeout=args.lock_timeout) as view:
            plan = plan_prune(view, args, ap)
        report(plan)
        print("dry run -- pass --apply to prune")
        return
    # Inside a round: the inherited view and the round's broker; standalone: this command's own
    # writer (the round lock) for the whole read-decide-apply.
    with store_broker.step_session(st, "prune", timeout=args.lock_timeout) as session:
        settled = settle_quarantines(HERE, view=session.view)
        if any(settled.values()):
            print(f"settled earlier prune quarantines: {settled}")
        plan = plan_prune(session.view, args, ap)
        report(plan)
        apply(session, plan)


if __name__ == "__main__":
    main()
