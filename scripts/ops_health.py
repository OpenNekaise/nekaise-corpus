#!/usr/bin/env python3
"""ops_health.py — operational alerts and the recoverability growth block (ADR 0001 stage 4
step 5).

    python scripts/ops_health.py check [--json]    # evaluate every check, record, exit 0/1/2

Checks (each: ok | warning | critical, with its facts):

* metadata recoverability — the WAL archiver (archive_mode, archive_timeout, failures) and the
  worst-case age of committed WAL not yet in the archive (pg_backup.exposure) against the RPO
  budget (15 min); the WAL chain from the oldest base has no gap; the newest base backup's age;
  the newest restore drill (its verdict, its measured RTO against 60 min, its age); capacity of
  the backup disk (free space and the projected 35-day WAL volume) and of the data disk;
* payload backups — reported SEPARATELY (backup_schedule's status): the metadata RPO says nothing
  about raw/text/artifact bytes, whose durability is stage 5's;
* the staged lifecycle (only where a PostgreSQL schema has runs): stalled open/frozen runs,
  promotion latency (a frozen run waiting, and the age of the newest promotion when rounds are
  expected), materialization lag (corpus/ behind the current generation), review backlog
  (unreviewed generations and their age, open findings), and the integrity sweeps' freshness and
  failures (scripts/integrity_sweep.py).

A check that cannot read its facts is critical, never "ok". Each evaluation replaces
workspace/ops-health.json atomically and appends every state change (an alert raised, changed
in severity or cleared) to logs/alerts.jsonl — the maintainer's snapshot and the review evidence
read them. Exit status: 2 when any check is critical, 1 for warnings, 0 otherwise.

`recoverability_block(conn)` is the growth block: a staged round refuses to start (and the
maintainer blocks growth) while the metadata's recoverability exceeds the RPO budget — the
archiver is off or failing, the exposure is unknown or above 15 minutes, or the chain from the
newest base cannot be replayed. Repairs (standalone and maintenance runs) stay allowed. It reads
the database the store uses (the same cluster the WAL comes from).
"""
from __future__ import annotations

import argparse
import calendar
import json
import shutil
import sys
import time
from pathlib import Path

import pg_backup

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "workspace" / "ops-health.json"
ALERTS = ROOT / "logs" / "alerts.jsonl"
PAYLOAD_STATUS = ROOT / "workspace" / "backup-status.json"
SWEEP_STATE = ROOT / "workspace" / "integrity-sweep.json"

# thresholds (seconds unless named otherwise)
BASE_WARN_H, BASE_CRIT_H = 26, 50
DRILL_WARN_DAYS, DRILL_CRIT_DAYS = 8, 15
SSD_WARN_FREE, SSD_CRIT_FREE = 0.15, 0.05
DATA_WARN_FREE, DATA_CRIT_FREE = 0.10, 0.04
PAYLOAD_WARN_H, PAYLOAD_CRIT_H = 48, 96
RUN_STALL_WARN, RUN_STALL_CRIT = 3 * 3600, 12 * 3600
FROZEN_WAIT_WARN = 30 * 60
PROMOTION_WARN = 6 * 3600
MATERIALIZE_WARN = 30 * 60
FOLD_LAG_WARN, FOLD_LAG_CRIT = 32, 128
REVIEW_WARN_GENERATIONS, REVIEW_WARN_AGE = 200, 24 * 3600
SWEEP_WARN_DAYS = 8
RETAIN_DAYS = 35
# the share of the (corpus-backup) SSD the metadata backups may use: WAL archive + base backups
METADATA_BUDGET_GB = 200


def _check(name: str, severity: str, summary: str, **facts) -> dict:
    return {"check": name, "severity": severity, "summary": summary, "facts": facts}


def _error(name: str, exc: BaseException) -> dict:
    return _check(name, "critical", f"cannot read the facts: {type(exc).__name__}: {exc}"[:300])


# --- the growth block ---------------------------------------------------------------------------------

def recoverability_block(conn, *, wal=None) -> str | None:
    """Why growth must stop because the metadata is not recoverable within the RPO budget, or
    None. `conn` is a connection to the store's cluster (superuser or pg_monitor: the ready list).
    Any failure to read the facts blocks, and no fact is ever formatted into a crash."""
    try:
        arch = pg_backup.archiver_state(conn)
        exposure, why = pg_backup.exposure(arch)
    except Exception as exc:
        return f"recoverability unknown: cannot read the WAL archiver ({type(exc).__name__}: {exc})"
    if why:
        return f"recoverability: {why}"
    wal = pg_backup.WAL if wal is None else wal
    if exposure > pg_backup.RPO_SECONDS:
        return (f"recoverability: committed WAL may be up to {exposure / 60:.0f} min old without "
                f"being archived (oldest waiting segment {arch['oldest_ready_s'] or 0:.0f} s + "
                f"archive_timeout {arch['archive_timeout_s']} s) — above the "
                f"{pg_backup.RPO_SECONDS // 60}-minute RPO budget"
                + (f"; the archiver is failing ({arch['last_failed_wal']})" if arch["failing"]
                   else ""))
    try:
        # what the server believes it archived must be in THE archive the drills restore from (an
        # archive_command writing elsewhere, or a mount-point mix-up, "succeeds" otherwise)
        last = arch["last_archived_wal"]
        if arch["archived"] and last and pg_backup.segment_like(last) and \
                not pg_backup.archived(last, wal):
            return (f"recoverability: the server reports {last} archived, but it is not in "
                    f"{wal} (the archive the restores read)")
        found = pg_backup.bases()
        if not found:
            return "recoverability: no base backup exists — nothing to recover from"
        first = pg_backup.start_segment(found[-1])
        current = arch["current_wal"]
        if current and pg_backup.segment_number(first)[0] != pg_backup.segment_number(current)[0]:
            return (f"recoverability: the server is on timeline "
                    f"{pg_backup.segment_number(current)[0]} but the newest base starts on "
                    f"timeline {pg_backup.segment_number(first)[0]} — take a new base backup")
        if gaps := pg_backup.chain_gaps(first, wal):
            return (f"recoverability: the WAL archive has gaps after the newest base "
                    f"({', '.join(gaps[:3])})")
    except Exception as exc:
        return f"recoverability unknown: cannot read the backups ({type(exc).__name__}: {exc})"
    return None


def store_recoverability_block(st) -> str | None:
    """recoverability_block over the store's own database connection; never raises (a failure
    is a blocking reason)."""
    try:
        conn = st._connect(autocommit=True)
    except Exception as exc:
        return f"recoverability unknown: cannot connect ({type(exc).__name__}: {exc})"
    try:
        with conn:
            return recoverability_block(conn)
    except Exception as exc:
        return f"recoverability unknown: {type(exc).__name__}: {exc}"[:300]


# --- checks -------------------------------------------------------------------------------------------

def metadata_checks(facts: dict, now: float) -> list[dict]:
    out = []
    arch = facts.get("archiver", {})
    if "error" in arch:
        out.append(_check("archive", "critical", f"cannot read the archiver: {arch['error']}"))
    else:
        exposure, why = facts.get("exposure_s"), facts.get("exposure_error")
        if why:
            out.append(_check("archive", "critical", why, **arch))
        elif arch["failing"] or exposure > pg_backup.RPO_SECONDS:
            out.append(_check("archive", "critical",
                              f"archive lag: exposure {exposure:.0f} s (budget "
                              f"{pg_backup.RPO_SECONDS} s), failing={arch['failing']}",
                              exposure_s=exposure, **arch))
        elif exposure > pg_backup.RPO_SECONDS * 2 / 3:
            out.append(_check("archive", "warning", f"archive lag: exposure {exposure:.0f} s",
                              exposure_s=exposure, **arch))
        else:
            out.append(_check("archive", "ok", f"exposure <= {exposure:.0f} s",
                              exposure_s=exposure, **arch))
    b = facts.get("bases", {})
    if "error" in b:
        out.append(_check("base_backup", "critical", f"cannot read the bases: {b['error']}"))
    elif not b.get("count"):
        out.append(_check("base_backup", "critical", "no base backup"))
    else:
        age = b["newest_age_h"]
        sev = "critical" if age > BASE_CRIT_H else "warning" if age > BASE_WARN_H else "ok"
        out.append(_check("base_backup", sev, f"newest base {age:.1f} h old", **b))
        gaps = facts.get("wal_chain_gaps") or []
        out.append(_check("wal_chain", "critical" if gaps else "ok",
                          f"WAL chain gaps: {gaps[:5]}" if gaps else
                          f"contiguous from {b.get('oldest')}", gaps=gaps[:20]))
        # the retention target: once the drills have run longer than the retention window, the
        # recoverable window must reach back RETAIN_DAYS (a prune that dropped coverage warns)
        first = facts.get("first_drill_at")
        running = (now - calendar.timegm(time.strptime(first, "%Y-%m-%dT%H:%M:%SZ"))) / 86400 \
            if first else 0.0
        coverage = b.get("coverage_days") or 0.0
        short = running > RETAIN_DAYS + 1 and coverage < RETAIN_DAYS
        out.append(_check("retention", "warning" if short else "ok",
                          f"recoverable window {coverage:.1f} days (target {RETAIN_DAYS}; "
                          f"drills running for {running:.1f} days)",
                          coverage_days=coverage, running_days=round(running, 2)))
    drill = facts.get("last_drill")
    if drill is None:
        out.append(_check("restore_drill", "warning", "no restore drill has run"))
    else:
        at = calendar.timegm(time.strptime(drill["at"], "%Y-%m-%dT%H:%M:%SZ"))
        days = (now - at) / 86400
        if not drill.get("ok"):
            sev, text = "critical", f"the newest drill failed: {drill.get('error') or 'mismatch'}"
        elif (drill.get("rto_s") or 0) > pg_backup.RTO_SECONDS:
            sev, text = "critical", f"drill RTO {drill['rto_s']} s above {pg_backup.RTO_SECONDS} s"
        elif days > DRILL_CRIT_DAYS:
            sev, text = "critical", f"newest drill {days:.1f} days old"
        elif days > DRILL_WARN_DAYS:
            sev, text = "warning", f"newest drill {days:.1f} days old"
        else:
            sev, text = "ok", f"drill {days:.1f} days ago, RTO {drill.get('rto_s')} s"
        out.append(_check("restore_drill", sev, text, age_days=round(days, 2), **drill))
    cap = facts.get("capacity", {})
    if "error" in cap:
        out.append(_check("backup_capacity", "critical", f"cannot read: {cap['error']}"))
    else:
        # the backup disk is the corpus backups' (operator, 2026-09-25): the metadata backups
        # (WAL archive + bases) get at most METADATA_BUDGET_GB of it; a retention that would not
        # fit is reported with the retention that does, never silently shortened or overfilled
        need = cap["projected_wal_gb"] + cap["bases_gb"]
        growth = need - cap["wal_archive_gb"] - cap["bases_gb"]
        free = cap["free_fraction"]
        rate = (cap.get("wal_rate") or {}).get("gb_per_day") or 0.0
        fitting = (max(0.0, METADATA_BUDGET_GB - cap["bases_gb"]) / rate) if rate else None
        over = need > METADATA_BUDGET_GB
        sev = ("critical" if free < SSD_CRIT_FREE else
               "warning" if free < SSD_WARN_FREE or growth > cap["free_gb"] or over else "ok")
        text = (f"backup disk {free:.1%} free; metadata backups need ≈ {need:.0f} GB for "
                f"{RETAIN_DAYS} days (WAL at the last 24 h rate + bases; budget "
                f"{METADATA_BUDGET_GB} GB)")
        if over:
            text += (f" — over budget: {fitting:.0f} days of WAL fit" if fitting is not None
                     else " — over budget")
        out.append(_check("backup_capacity", sev, text, need_gb=round(need, 1),
                          budget_gb=METADATA_BUDGET_GB,
                          fitting_retention_days=None if fitting is None else round(fitting, 2),
                          **cap))
    return out


def data_disk_check(root: Path | None = None) -> dict:
    root = ROOT if root is None else root
    try:
        u = shutil.disk_usage(root)
        free = u.free / u.total
        sev = "critical" if free < DATA_CRIT_FREE else "warning" if free < DATA_WARN_FREE else "ok"
        return _check("data_capacity", sev, f"data disk {free:.1%} free",
                      free_gb=round(u.free / 1e9, 1), total_gb=round(u.total / 1e9, 1))
    except OSError as exc:
        return _error("data_capacity", exc)


def payload_check(now: float, path: Path | None = None) -> dict:
    """raw/text/artifact backups (backup_schedule): freshness only, reported apart from the
    metadata RPO (the payload durability gap closes in stage 5)."""
    path = PAYLOAD_STATUS if path is None else path
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return _check("payload_backup", "warning", "no payload backup status recorded")
    except (OSError, ValueError) as exc:
        return _error("payload_backup", exc)
    latest = doc.get("latest_backup") or ""
    stamp = next((part for part in Path(latest).name.split("-") if len(part) == 16
                  and part.endswith("Z")), None)
    age_h = None
    if stamp:
        age_h = (now - calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ"))) / 3600
    bad = doc.get("error") or doc.get("result") not in ("ok", "up-to-date", "completed")
    sev = ("critical" if age_h is None or age_h > PAYLOAD_CRIT_H else
           "warning" if bad or age_h > PAYLOAD_WARN_H else "ok")
    return _check("payload_backup", sev,
                  f"newest payload backup {'?' if age_h is None else f'{age_h:.1f}'} h old "
                  f"({doc.get('result')}) — payload durability is tracked apart from the "
                  "metadata RPO", age_h=None if age_h is None else round(age_h, 2),
                  result=doc.get("result"), error=doc.get("error"), latest=latest)


def lifecycle_checks(conn, now: float, root: Path | None = None) -> list[dict]:
    """Stalled runs, promotion latency, materialization lag, review backlog — over the schema
    `conn`'s search_path names (skipped where the schema has no staged lifecycle yet)."""
    out = []
    has = conn.execute("SELECT to_regclass('runs') IS NOT NULL AND to_regclass('review_state') "
                       "IS NOT NULL").fetchone()[0]
    if not has:
        return [_check("lifecycle", "ok", "no staged lifecycle in this schema")]
    rows = conn.execute("SELECT run_id, status, kind, extract(epoch FROM now() - started_at), "
                        "frozen_seq IS NOT NULL FROM runs WHERE status IN ('open', 'frozen') "
                        "ORDER BY started_at").fetchall()
    stalled = [{"run": r, "status": s, "kind": k, "age_s": round(float(a))}
               for r, s, k, a, _ in rows]
    oldest = max((x["age_s"] for x in stalled), default=0)
    sev = ("critical" if oldest > RUN_STALL_CRIT else "warning" if oldest > RUN_STALL_WARN
           else "ok")
    out.append(_check("stalled_runs", sev, f"{len(stalled)} unfinished run(s), oldest "
                      f"{oldest / 60:.0f} min", runs=stalled[:10]))
    frozen_wait = max((x["age_s"] for x in stalled if x["status"] == "frozen"), default=0)
    head, last_age = conn.execute(
        "SELECT d.current_generation, extract(epoch FROM now() - g.promoted_at) FROM dataset d "
        "LEFT JOIN generations g ON g.generation = d.current_generation").fetchone()
    lat = conn.execute(
        "SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM "
        "g.promoted_at - r.started_at)) FROM generations g JOIN runs r USING (run_id) WHERE "
        "g.promoted_at > now() - interval '1 day'").fetchone()[0]
    sev = ("warning" if frozen_wait > FROZEN_WAIT_WARN
           or (head is not None and last_age is not None and last_age > PROMOTION_WARN) else "ok")
    out.append(_check("promotion_latency", sev,
                      f"newest promotion {'-' if last_age is None else f'{last_age / 60:.0f} min'}"
                      f" ago; run→promotion p95 (24 h) "
                      f"{'-' if lat is None else f'{float(lat):.0f} s'}",
                      generation=head, newest_promotion_s=None if last_age is None
                      else round(float(last_age)), open_to_promoted_p95_s=None if lat is None
                      else round(float(lat), 1), frozen_waiting_s=frozen_wait))
    if head is not None:
        try:
            import materialize
            stamp = materialize.read_stamp(Path(ROOT if root is None else root)
                                           / "corpus") or {}
        except Exception as exc:
            out.append(_error("materialization_lag", exc))
        else:
            behind = head - stamp["generation"] if isinstance(stamp.get("generation"), int) \
                else None
            state = stamp.get("state")
            complete = state == "complete" and behind == 0
            sev = "ok" if complete else ("warning" if last_age is None
                                         or last_age > MATERIALIZE_WARN else "ok")
            out.append(_check("materialization_lag", sev,
                              f"corpus/ is {state or 'absent'} at generation "
                              f"{stamp.get('generation')} (current {head})",
                              behind=behind, state=state))
    # fold lag: promoted generations the projection has not absorbed yet. Every read overlays
    # them, and its cost grows with their number (the basis benchmark: a hot document revised by
    # 256 unfolded generations makes a 400-row patch take ~10x longer and a first scan page
    # ~200x) — a retention pin (a triage, a sweep) or a stuck fold shows up here first
    folded, pins = conn.execute(
        "SELECT (SELECT generation FROM projection_state), (SELECT count(*) FROM "
        "generation_retention WHERE until IS NULL OR until > now())").fetchone()
    if head is not None:
        lag = head - (-1 if folded is None else folded)
        sev = ("critical" if lag > FOLD_LAG_CRIT else "warning" if lag > FOLD_LAG_WARN
               else "ok")
        out.append(_check("fold_lag", sev, f"{lag} promoted generation(s) not folded yet "
                          f"({pins} active retention pin(s))", unfolded=lag, pins=int(pins)))
    reviewed, endorsed, open_f, open_i = conn.execute(
        "SELECT reviewed_through, endorsed_through, open_findings, open_integrity FROM "
        "review_state").fetchone()
    backlog = 0 if head is None else head - (-1 if reviewed is None else reviewed)
    age = conn.execute("SELECT extract(epoch FROM now() - promoted_at) FROM generations WHERE "
                       "generation = %s", [(-1 if reviewed is None else reviewed) + 1]
                       ).fetchone() if backlog else None
    age_s = float(age[0]) if age else 0.0
    sev = ("critical" if open_i else
           "warning" if open_f or backlog > REVIEW_WARN_GENERATIONS or age_s > REVIEW_WARN_AGE
           else "ok")
    out.append(_check("review_backlog", sev,
                      f"{backlog} unreviewed generation(s), oldest {age_s / 3600:.1f} h; "
                      f"open findings {open_f}, integrity {open_i}",
                      backlog=backlog, oldest_unreviewed_s=round(age_s), reviewed_through=reviewed,
                      endorsed_through=endorsed, open_findings=open_f, open_integrity=open_i))
    return out


def sweep_check(now: float, path: Path | None = None) -> dict:
    """The integrity sweeps (scripts/integrity_sweep.py): a failure is critical; a full pass of
    either kind older than SWEEP_WARN_DAYS a warning."""
    path = SWEEP_STATE if path is None else path
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return _check("integrity_sweep", "warning", "no integrity sweep has run")
    except (OSError, ValueError) as exc:
        return _error("integrity_sweep", exc)
    failures = {kind: part.get("failures") for kind, part in doc.items()
                if isinstance(part, dict) and part.get("failures")}
    ages = {kind: (now - part["completed_at"]) / 86400 for kind, part in doc.items()
            if isinstance(part, dict) and part.get("completed_at")}
    stale = [k for k in ("metadata", "artifacts") if ages.get(k, 1e9) > SWEEP_WARN_DAYS]
    sev = "critical" if failures else "warning" if stale else "ok"
    return _check("integrity_sweep", sev,
                  f"failures: {sorted(failures)}" if failures else
                  f"stale or never completed: {stale}" if stale else "sweeps current",
                  ages_days={k: round(v, 2) for k, v in ages.items()},
                  failures={k: (v[:10] if isinstance(v, list) else v)
                            for k, v in failures.items()})


def lifecycle_target(root: Path | None = None) -> tuple[str, str]:
    """(DSN, schema) of the staged lifecycle to watch: the one the host authority record makes
    authoritative for `root`, else the shadow schema of this host (pg_backup's defaults). An
    unreadable record raises (reported as a critical check)."""
    import store_authority
    rec = store_authority.record_for(ROOT if root is None else root)
    if rec is not None and rec.mode == "postgres":
        return rec.dsn, rec.schema
    return f"host={pg_backup.SOCKET} dbname={pg_backup.DB}", pg_backup.SCHEMA


def evaluate(*, now: float | None = None, lifecycle_conn=None) -> list[dict]:
    """Every check. `lifecycle_conn`: a connection to the staged schema (or None: found from the
    host authority record, else the shadow schema pg_backup names)."""
    now = time.time() if now is None else now
    checks: list[dict] = []
    try:
        facts = pg_backup.status(retain_days=RETAIN_DAYS)
        checks += metadata_checks(facts, now)
    except Exception as exc:
        checks.append(_error("metadata_recoverability", exc))
    checks.append(data_disk_check())
    checks.append(payload_check(now))
    try:
        if lifecycle_conn is None:
            import psycopg
            from psycopg import sql
            dsn, schema = lifecycle_target()
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                checks += lifecycle_checks(conn, now)
        else:
            checks += lifecycle_checks(lifecycle_conn, now)
    except Exception as exc:
        checks.append(_error("lifecycle", exc))
    try:
        import store_authority
        rec = store_authority.record_for(ROOT)
        staged = rec is not None and rec.mode == "postgres"
    except Exception as exc:   # an unreadable record: judge the sweeps as if staged
        checks.append(_error("authority_record", exc))
        staged = True
    checks.append(sweep_check(now) if staged else _check(
        "integrity_sweep", "ok", "not applicable under file authority (lint_registry and "
        "check_contracts check the whole state every round)"))
    return checks


def record(checks: list[dict], *, state: Path | None = None, alerts: Path | None = None,
           now: float | None = None) -> list[dict]:
    """Replace the state file; append every change of a check's severity to the alert log.
    Returns the changes."""
    import ops
    now = time.time() if now is None else now
    state, alerts = (STATE if state is None else state), (ALERTS if alerts is None else alerts)
    try:
        previous = {c["check"]: c["severity"]
                    for c in json.loads(state.read_text()).get("checks", [])}
    except (FileNotFoundError, ValueError):
        previous = {}
    changes = []
    for c in checks:
        was = previous.get(c["check"], "ok")
        if c["severity"] != was:
            changes.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                            "check": c["check"], "from": was, "to": c["severity"],
                            "summary": c["summary"]})
    worst = max((c["severity"] for c in checks), key=("ok", "warning", "critical").index,
                default="ok")
    ops.atomic_write_text(state, json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "worst": worst,
        "checks": checks}, indent=1, sort_keys=True, default=str) + "\n")
    for change in changes:
        ops.append_jsonl(alerts, change)
    return changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("check",))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    checks = evaluate()
    changes = record(checks)
    if args.json:
        print(json.dumps(checks, indent=1, default=str))
    else:
        for c in checks:
            print(f"{c['severity'].upper():8s} {c['check']:20s} {c['summary']}")
        for ch in changes:
            print(f"ALERT {ch['check']}: {ch['from']} -> {ch['to']}: {ch['summary']}")
    severities = {c["severity"] for c in checks}
    return 2 if "critical" in severities else 1 if "warning" in severities else 0


if __name__ == "__main__":
    sys.exit(main())
