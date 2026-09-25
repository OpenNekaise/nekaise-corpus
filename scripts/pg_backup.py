#!/usr/bin/env python3
"""pg_backup.py — base backups, retention, restore drills and recoverability facts for the local
PostgreSQL store (ADR 0001, stage 2; recovery points and retention: stage 4 step 5).

WAL is archived continuously to the backup SSD by the server's archive_command
(~/.local/share/nekaise-pg/archive_wal.sh). This adds the other halves of point-in-time recovery:

    python scripts/pg_backup.py base                 # compressed, manifest-checksummed base backup
    python scripts/pg_backup.py restore-test         # the restore drill (below)
    python scripts/pg_backup.py prune --retain-days 35 [--daily-days 7] [--dry-run]
                                                     # 35 days of recoverable coverage (below)
    python scripts/pg_backup.py prune --keep 7       # legacy: the newest N bases, older WAL gone
    python scripts/pg_backup.py status               # recoverability facts as JSON
    python scripts/pg_backup.py rpo-probe [--count N] # how long a write takes to reach the archive
    python scripts/pg_backup.py fingerprint          # the recovery fingerprint of the live database

The restore drill names a recovery point and proves the newest base + the archived WAL recover
exactly it. Under the store's writer lock (so no store writer — a round's batch, a promotion, a
shadow sync — commits in between) it opens a REPEATABLE READ snapshot and creates the named
restore point, then releases the lock at once; the live fingerprint is computed in that snapshot
afterwards. It then waits until the WAL segment holding the restore point is CONFIRMED in the
archive (the file itself, not a fixed sleep; a timeout fails the drill), restores the base into a
scratch instance on a private socket with recovery_target_name, and compares the restored
fingerprint with the live one. The fingerprint (fingerprint()) is, per schema: every base table's
row count and an order-independent digest of its rows (sums of sha256 over each row's text:
projection tables, revisions, batches, runs, generations, configuration sets and blobs, events,
outbox and consumers, review state — everything), plus the full content of the small control
tables (state, dataset, outbox_state, projection_state, review_state, ...): the generation, the
revisions, the configuration, the events and the outbox of the recovery point, digested. The
drill's wall time from the start of the restore to a verified fingerprint is the measured
recovery time (RTO); the archive wait is recorded too. Every drill appends one JSON line to
logs/pg-drills.jsonl (NEKAISE_PG_DRILL_LOG); a drill that does not match, or cannot finish, exits
non-zero and records why.

Retention (--retain-days D): keep every base of the last --daily-days days, the newest base of
each ISO week back to D days, and the anchor — the newest base at least D days old — so that any
point of the last D days is recoverable from a kept base plus the WAL after it; WAL older than
the oldest kept base's start is removed. The newest base is always kept, and nothing is removed
while the WAL chain from the oldest kept base is broken (a gap would already make later points
unrecoverable: that is reported, not "cleaned up").

Paths default to this host's layout and can be overridden with NEKAISE_PG_BIN /
NEKAISE_PG_SOCKET / NEKAISE_PG_DB / NEKAISE_PG_SCHEMA / NEKAISE_PG_BACKUP_DIR / NEKAISE_PG_WAL_DIR
/ NEKAISE_PG_SCRATCH.
"""
from __future__ import annotations

import argparse
import datetime as dt
from contextlib import contextmanager as contextmanager_
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PGBIN = Path(os.environ.get("NEKAISE_PG_BIN", "/home/zengp/miniconda3/envs/nekaise-pg/bin"))
SOCKET = os.environ.get("NEKAISE_PG_SOCKET", "/home/zengp/.local/share/nekaise-pg/run")
DB = os.environ.get("NEKAISE_PG_DB", "nekaise")
SCHEMA = os.environ.get("NEKAISE_PG_SCHEMA", "nekaise")
BASES = Path(os.environ.get("NEKAISE_PG_BACKUP_DIR", "/media/zengp/ssd/nekaise-pg-base"))
WAL = Path(os.environ.get("NEKAISE_PG_WAL_DIR", "/media/zengp/ssd/nekaise-pg-wal"))
SCRATCH = Path(os.environ.get("NEKAISE_PG_SCRATCH", "/home/zengp/.local/share/nekaise-pg"))
DRILL_LOG = Path(os.environ.get("NEKAISE_PG_DRILL_LOG", str(ROOT / "logs" / "pg-drills.jsonl")))

# Recovery objectives (ADR 0001 stage 4 step 5): metadata loss at most 15 minutes; the metadata
# service restored within 60 minutes.
RPO_SECONDS = 15 * 60
RTO_SECONDS = 60 * 60
SEGMENT_BYTES = 16 * 1024 * 1024
SEGMENTS_PER_LOG = 0x100000000 // SEGMENT_BYTES
_SEGMENT = re.compile(r"[0-9A-F]{24}")
_BASE_NAME = re.compile(r"\d{8}T\d{6}Z")
# Tables whose whole content the fingerprint shows (small control tables; the rest are digested).
SMALL_TABLE_ROWS = 64
# Transient bookkeeping that is written WITHOUT the store's writer lock (store_staging.release_pin
# drops a retention pin lock-free), so it can change between the drill's snapshot and its restore
# point: never part of the recovery fingerprint, on either side of the comparison. A pin decides
# only how far the fold may advance; no generation's content depends on it.
VOLATILE_TABLES = frozenset({"generation_retention"})


class DrillError(RuntimeError):
    """The drill could not establish, restore or verify its recovery point."""


def run(*args, **kw) -> str:
    return subprocess.run([str(a) for a in args], check=True, text=True, capture_output=True,
                          **kw).stdout.strip()


def psql(socket: str, query: str, db: str | None = None) -> str:
    db = DB if db is None else db
    return run(PGBIN / "psql", "-h", socket, "-d", db, "-Atc", query)


def connect(socket: str | None = None, db: str | None = None, **kw):
    import psycopg
    socket, db = (SOCKET if socket is None else socket), (DB if db is None else db)
    return psycopg.connect(f"host={socket} dbname={db}", **kw)


def _now() -> float:
    return time.time()


def _utc(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now() if ts is None else ts))


# --- WAL segment arithmetic --------------------------------------------------------------------------

def segment_of(lsn: str, timeline: int = 1) -> str:
    """The WAL segment file name holding `lsn` ("X/Y") on `timeline` (16 MiB segments)."""
    hi, lo = (int(x, 16) for x in lsn.split("/"))
    segno = ((hi << 32) | lo) // SEGMENT_BYTES
    return f"{timeline:08X}{segno // SEGMENTS_PER_LOG:08X}{segno % SEGMENTS_PER_LOG:08X}"


def segment_number(name: str) -> tuple[int, int]:
    """(timeline, absolute segment number) of a WAL segment file name."""
    return int(name[:8], 16), int(name[8:16], 16) * SEGMENTS_PER_LOG + int(name[16:24], 16)


def segment_name(timeline: int, segno: int) -> str:
    return f"{timeline:08X}{segno // SEGMENTS_PER_LOG:08X}{segno % SEGMENTS_PER_LOG:08X}"


def segment_like(name: str) -> bool:
    """Whether `name` is a WAL segment file name (24 hex digits)."""
    return bool(_SEGMENT.fullmatch(name))


def archived(name: str, wal: Path | None = None) -> bool:
    """Whether segment `name` is in the archive (plain, or zstd-compressed by a future
    archive_command)."""
    wal = WAL if wal is None else wal
    return (wal / name).is_file() or (wal / f"{name}.zst").is_file()


def archived_segments(wal: Path | None = None) -> dict[int, set[int]]:
    """{timeline: {segment numbers}} of the archive's segment files."""
    wal = WAL if wal is None else wal
    out: dict[int, set[int]] = {}
    for p in wal.iterdir():
        name = p.name[:-4] if p.name.endswith(".zst") else p.name
        if _SEGMENT.fullmatch(name):
            tli, seg = segment_number(name)
            out.setdefault(tli, set()).add(seg)
    return out


def missing_segments(first: str, last: str, wal: Path | None = None,
                     limit: int = 20) -> list[str]:
    """The segments of the INCLUSIVE range first..last that the archive does not hold (the first
    `limit`), the starting segment and the endpoint included — recovery replays WAL in order,
    so any of them missing makes every later point unrecoverable from a base before it. A range
    that crosses timelines or runs backwards is reported as unrecoverable, never as complete."""
    tli, lo = segment_number(first)
    tli2, hi = segment_number(last)
    if tli != tli2:
        return [f"{first}..{last}: the range crosses timelines {tli} -> {tli2}"]
    if hi < lo:
        return [f"{first}..{last}: the endpoint precedes the start"]
    have = archived_segments(WAL if wal is None else wal).get(tli, set())
    missing = []
    for seg in range(lo, hi + 1):
        if seg not in have:
            missing.append(segment_name(tli, seg))
            if len(missing) >= limit:
                break
    return missing


def base_range(base_dir: Path) -> tuple[str, str]:
    """(start segment, end segment) of the WAL a base backup needs to be consistent: its
    manifest's Start-LSN and End-LSN on its timeline. Recovery from it needs both and everything
    in between, then every segment after it up to the recovery target."""
    wal = json.loads((base_dir / "backup_manifest").read_text())["WAL-Ranges"][0]
    return (segment_of(wal["Start-LSN"], wal["Timeline"]),
            segment_of(wal["End-LSN"], wal["Timeline"]))




# --- bases ---------------------------------------------------------------------------------------

def bases(root: Path | None = None) -> list[Path]:
    """Complete, verified base backups (a base becomes visible only after pg_verifybackup),
    oldest first."""
    root = BASES if root is None else root
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and _BASE_NAME.fullmatch(p.name))


def base_time(base_dir: Path) -> float:
    """The base's start time (its name, UTC)."""
    return dt.datetime.strptime(base_dir.name, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=dt.timezone.utc).timestamp()


def start_segment(base_dir: Path) -> str:
    """WAL segment file holding a base backup's start LSN, per its manifest."""
    return base_range(base_dir)[0]


# --- the backup-retention lock -------------------------------------------------------------------------

@contextmanager_
def retention_lock(*, exclusive: bool, timeout: float, root: Path | None = None):
    """The lock that keeps bases and WAL a drill is using from being pruned: a drill holds it
    shared from choosing its base until its scratch instance is gone; prune holds it exclusive
    (waiting at most `timeout` seconds, then refusing). An flock on a file in the base
    directory: it dies with its holder. Corpus rounds never take it."""
    import fcntl
    root = BASES if root is None else root
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".retention.lock", "a+") as f:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    who = "a drill is using the backups" if exclusive else "a prune is running"
                    raise RetentionBusy(f"{root / '.retention.lock'} is held ({who})") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class RetentionBusy(RuntimeError):
    """The backup-retention lock could not be taken in time."""


def base() -> Path:
    if not BASES.parent.is_mount() and not BASES.exists():
        raise SystemExit(f"backup location {BASES} is unavailable (SSD not mounted?)")
    dest = BASES / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    BASES.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.partial")
    run(PGBIN / "pg_basebackup", "-h", SOCKET, "-D", tmp, "-Ft", "-z", "-X", "none", "-c", "fast",
        "--manifest-checksums=SHA256")
    run(PGBIN / "pg_verifybackup", "-n", "-m", tmp / "backup_manifest", tmp)
    tmp.rename(dest)  # a base only becomes visible once it is complete and verified
    print(f"base backup {dest} ({sum(f.stat().st_size for f in dest.iterdir()) / 1e9:.2f} GB)")
    return dest


# --- retention -------------------------------------------------------------------------------------

def retention_plan(names: list[str], now: float, *, retain_days: float,
                   daily_days: float) -> tuple[list[str], list[str]]:
    """(keep, remove) among base names for `retain_days` of recoverable coverage: every base of
    the last `daily_days`, the newest base of each ISO week back to `retain_days`, the anchor
    (the newest base at least `retain_days` old: recovery to the oldest covered point starts
    there) and always the newest base. Pure: tested without a filesystem."""
    if daily_days > retain_days:
        raise ValueError("--daily-days cannot exceed --retain-days")
    ordered = sorted(names)
    if not ordered:
        return [], []
    t = {n: dt.datetime.strptime(n, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=dt.timezone.utc).timestamp() for n in ordered}
    horizon, daily = now - retain_days * 86400, now - daily_days * 86400
    keep = {ordered[-1]}
    keep |= {n for n in ordered if t[n] >= daily}
    weekly: dict[tuple[int, int], str] = {}
    for n in ordered:
        if horizon <= t[n] < daily:
            week = dt.datetime.fromtimestamp(t[n], dt.timezone.utc).isocalendar()[:2]
            weekly[week] = max(weekly.get(week, n), n)
    keep |= set(weekly.values())
    older = [n for n in ordered if t[n] < horizon]
    if older:
        keep.add(older[-1])
    return sorted(keep), [n for n in ordered if n not in keep]


def prune(keep: int | None = None, *, retain_days: float | None = None, daily_days: float = 7,
          dry_run: bool = False, now: float | None = None, root: Path | None = None,
          wal: Path | None = None, lock_timeout: float = 3 * 3600, log=print) -> dict:
    """Remove bases outside the plan (`keep`: the legacy newest-N rule; `retain_days`: the
    coverage rule above), then WAL older than the oldest kept base's start segment, plain and
    zstd-compressed alike. Holds the retention lock exclusively (a running drill keeps its base
    and WAL). Refuses to remove anything unless every segment from the oldest kept base's start
    through the newest kept base's end is archived (a missing prefix, interior gap or tail would
    leave a kept base unrecoverable while its fallback is deleted)."""
    root, wal = (BASES if root is None else root), (WAL if wal is None else wal)
    try:
        with retention_lock(exclusive=True, timeout=lock_timeout, root=root):
            return _prune(keep, retain_days=retain_days, daily_days=daily_days, dry_run=dry_run,
                          now=now, root=root, wal=wal, log=log)
    except RetentionBusy as exc:
        raise SystemExit(f"prune refused: {exc}") from None


def _prune(keep, *, retain_days, daily_days, dry_run, now, root, wal, log) -> dict:
    found = bases(root)
    names = [p.name for p in found]
    if retain_days is not None:
        kept, gone = retention_plan(names, _now() if now is None else now,
                                    retain_days=retain_days, daily_days=daily_days)
    else:
        if not keep or keep < 1:
            raise SystemExit("--keep must be at least 1")
        kept, gone = names[-keep:], names[:-keep]
    out = {"kept": kept, "removed": gone, "wal_cleaned_to": None, "dry_run": dry_run}
    if not kept:
        return out
    segment = start_segment(root / kept[0])
    through = base_range(root / kept[-1])[1]
    if gaps := missing_segments(segment, through, wal):
        raise SystemExit(f"WAL chain from {kept[0]} ({segment}) through {kept[-1]}'s end "
                         f"({through}) is incomplete ({', '.join(gaps[:5])}"
                         f"{' ...' if len(gaps) > 5 else ''}): nothing removed — a kept base is "
                         "not recoverable; take a new base and investigate the archive")
    out["wal_cleaned_to"] = segment
    for name in gone:
        log(f"{'would remove' if dry_run else 'removed'} base {name}")
        if not dry_run:
            shutil.rmtree(root / name)
    if dry_run:
        log(f"would clean the WAL archive up to {segment} (the start of base {kept[0]})")
    else:
        # -x .zst: compressed segments are judged by their segment name like plain ones
        run(PGBIN / "pg_archivecleanup", "-x", ".zst", wal, segment)
        log(f"WAL archive cleaned up to {segment} (the start of base {kept[0]})")
    return out


# --- the recovery fingerprint -------------------------------------------------------------------------

def _schemas(conn) -> list[str]:
    return [s for (s,) in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname NOT IN "
        "('information_schema', 'public') ORDER BY nspname").fetchall()]


def fingerprint(conn, schemas: list[str] | None = None) -> dict:
    """The recovery fingerprint of `conn`'s current snapshot (call it inside a REPEATABLE READ
    transaction for one consistent state): for every base table of each schema its row count and
    an order-independent digest — four 64-bit column sums of sha256(row text) — and the full
    content of tables of at most SMALL_TABLE_ROWS rows. Computed in the server (parallel scans);
    nothing but the sums crosses the connection."""
    from psycopg import sql
    out: dict = {}
    conn.execute("SET LOCAL max_parallel_workers_per_gather = 8")
    for schema in schemas if schemas is not None else _schemas(conn):
        tables = [t for (t,) in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
            "ORDER BY c.relname", [schema]).fetchall() if t not in VOLATILE_TABLES]
        digests, small = {}, {}
        for table in tables:
            ident = sql.Identifier(schema, table)
            chunks = sql.SQL(", ").join(
                sql.SQL("COALESCE(sum(('x' || substr(h, {}, 16))::bit(64)::bigint::numeric), 0)")
                .format(sql.Literal(1 + 16 * i)) for i in range(4))
            row = conn.execute(sql.SQL(
                "SELECT count(*), {} FROM (SELECT encode(sha256(convert_to(t::text, 'UTF8')), "
                "'hex') AS h FROM {} t) s").format(chunks, ident)).fetchone()
            digests[table] = f"{row[0]}:" + ":".join(str(v) for v in row[1:])
            if row[0] <= SMALL_TABLE_ROWS:
                small[table] = sorted(r for (r,) in conn.execute(sql.SQL(
                    "SELECT row_to_json(t)::text FROM {} t").format(ident)).fetchall())
        out[schema] = {"tables": digests, "content": small}
    return out


def summary(fp: dict) -> dict:
    """The named facts of a fingerprint's schemas: generation, runs, revisions, configuration
    sets, events, outbox (from the digested counts and the small tables' content)."""
    out = {}
    for schema, data in fp.items():
        counts = {t: int(v.split(":", 1)[0]) for t, v in data["tables"].items()}
        facts = {"rows": {t: counts[t] for t in ("entries", "manifest", "blocklist", "ledger",
                                                 "revisions", "batches", "runs", "generations",
                                                 "config_sets", "config_blobs", "events",
                                                 "outbox", "gate_receipts", "review_verdicts")
                          if t in counts}}
        for table, key in (("dataset", "current_generation"), ("state", "schema_version"),
                           ("outbox_state", "allocated"), ("review_state", "reviewed_through"),
                           ("projection_state", "generation")):
            rows = data["content"].get(table)
            if rows:
                facts[f"{table}.{key}"] = json.loads(rows[0]).get(key)
        state = data["content"].get("state")
        if state:
            facts["replication_watermark"] = (json.loads(state[0]).get("replication") or {}
                                              ).get("watermark")
        out[schema] = facts
    return out


def diff(live: dict, restored: dict) -> list[str]:
    """What differs between two fingerprints (empty: identical)."""
    bad = []
    for schema in sorted(set(live) | set(restored)):
        a, b = live.get(schema), restored.get(schema)
        if a is None or b is None:
            bad.append(f"{schema}: {'missing in the restore' if b is None else 'not live'}")
            continue
        for part in ("tables", "content"):
            for t in sorted(set(a[part]) | set(b[part])):
                if a[part].get(t) != b[part].get(t):
                    bad.append(f"{schema}.{t} ({part}): live {str(a[part].get(t))[:60]} != "
                               f"restored {str(b[part].get(t))[:60]}")
    return bad


# --- restore ------------------------------------------------------------------------------------------

RESTORE_SH = """#!/bin/sh
# restore_command of a drill: a segment from the archive, plain or zstd-compressed
f="{wal}/$1"
if [ -f "$f" ]; then exec cp "$f" "$2"; fi
if [ -f "$f.zst" ]; then exec zstd -q -d -f "$f.zst" -o "$2"; fi
exit 1
"""


def extract(base_dir: Path, data: Path) -> None:
    data.mkdir(mode=0o700)
    run("tar", "-xzf", base_dir / "base.tar.gz", "-C", data)
    if (base_dir / "pg_wal.tar.gz").exists():   # a base taken with -X stream carries its WAL
        run("tar", "-xzf", base_dir / "pg_wal.tar.gz", "-C", data / "pg_wal")


def start_instance(work: Path, data: Path, *, conf: str, timeout: float = RTO_SECONDS) -> Path:
    """Start the restored data directory on a private socket (no TCP, no archiving) and wait
    until it has left recovery (promoted). Returns the socket directory. Raises DrillError when
    recovery ends before its target or the instance dies."""
    sock = work / "run"
    sock.mkdir(exist_ok=True)
    # postgresql.auto.conf is read last, so settings appended there win over both files of the
    # base (an ALTER SYSTEM archive_command of the source must never run in a restore: a
    # promoted restore would push its new timeline into the live archive); archiving is also
    # switched off on the command line, which beats every file
    with (data / "postgresql.auto.conf").open("a") as f:
        f.write(f"\n# restore drill (pg_backup.py)\nlisten_addresses = ''\n"
                f"unix_socket_directories = '{sock}'\narchive_mode = off\n"
                f"archive_command = ''\nshared_buffers = 1GB\n{conf}")
    deadline = time.monotonic() + timeout
    started = subprocess.run([str(PGBIN / "pg_ctl"), "-D", str(data), "-l", str(work / "log"),
                              "-o", "-c archive_mode=off", "-w", "-t", str(int(timeout)),
                              "start"], capture_output=True, text=True)
    if started.returncode:
        raise DrillError(f"the restored instance did not start: {_log_tail(work)}")
    while True:
        try:
            if psql(str(sock), "SELECT pg_is_in_recovery()", "postgres") == "f":
                return sock
        except subprocess.CalledProcessError:
            if not (data / "postmaster.pid").exists():
                raise DrillError(f"the restored instance stopped: {_log_tail(work)}") from None
        if time.monotonic() > deadline:
            raise DrillError(f"recovery did not finish within {timeout:.0f} s")
        time.sleep(1)


def _postmaster(data: Path) -> dict | None:
    """postmaster.pid of a data directory: pid, data directory, start time (epoch seconds);
    None when there is no such file. A malformed file raises (uncertain)."""
    try:
        lines = (Path(data) / "postmaster.pid").read_text().splitlines()
    except FileNotFoundError:
        return None
    return {"pid": int(lines[0]), "data": lines[1], "start": int(lines[2])}


def _proc(pid: int) -> dict | None:
    """What /proc says about `pid`: its start time (epoch seconds), command line and working
    directory; None when no such process exists. Any other read failure raises (uncertain)."""
    base = Path("/proc") / str(pid)
    try:
        stat = (base / "stat").read_text()
    except FileNotFoundError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    state, ticks = fields[0], int(fields[19])
    if state in ("Z", "X"):
        return None
    btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                 if line.startswith("btime "))
    try:
        cmdline = (base / "cmdline").read_bytes().split(b"\0")
        cwd = os.readlink(base / "cwd")
    except FileNotFoundError:
        return None
    return {"start": btime + ticks / os.sysconf("SC_CLK_TCK"),
            "cmdline": [c.decode(errors="replace") for c in cmdline if c], "cwd": cwd}


def instance_state(data: Path) -> str:
    """"absent" (no postmaster.pid, or its pid is not running), "ours" (the running process is
    a postgres whose working directory or -D is this data directory and whose start time is the
    one postmaster.pid records), "other" (the pid now belongs to an unrelated process — the
    recorded postmaster is gone) or "uncertain" (anything could not be read)."""
    data = Path(data).resolve()
    try:
        pm = _postmaster(data)
        if pm is None:
            return "absent"
        proc = _proc(pm["pid"])
    except (OSError, ValueError, IndexError, StopIteration):
        return "uncertain"
    if proc is None:
        return "absent"
    argv = proc["cmdline"]
    named = bool(argv) and Path(argv[0]).name.startswith("postgres")
    in_dir = proc["cwd"] == str(data) or (
        "-D" in argv and argv.index("-D") + 1 < len(argv)
        and Path(argv[argv.index("-D") + 1]).resolve() == data)
    same_start = abs(proc["start"] - pm["start"]) <= 2
    return "ours" if named and in_dir and same_start else "other"


def _pg_ctl_stop(data: Path, mode: str) -> int:
    return subprocess.run([str(PGBIN / "pg_ctl"), "-D", str(data), "-w", "-t", "60", "-m", mode,
                           "stop"], capture_output=True).returncode


STOP_WAIT_SECONDS = 90.0


def stop_instance(data: Path, *, wait: float | None = None) -> bool:
    """Stop a scratch instance and CONFIRM it is gone. Never signals anything that is not
    verifiably this data directory's postmaster (instance_state: pid, start time, command line
    and working directory); pg_ctl's result alone is not trusted — the process must have exited.
    True when no instance of `data` runs any more; False when that could not be established
    (the caller then keeps the scratch state)."""
    wait = STOP_WAIT_SECONDS if wait is None else wait
    for mode in ("fast", "immediate"):
        state = instance_state(data)
        if state in ("absent", "other"):
            return True
        if state == "uncertain":
            return False
        _pg_ctl_stop(data, mode)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            state = instance_state(data)
            if state in ("absent", "other"):
                return True
            if state == "uncertain":
                return False
            time.sleep(0.2)
    return False


def _log_tail(work: Path, lines: int = 12) -> str:
    try:
        return " | ".join((work / "log").read_text().splitlines()[-lines:])
    except OSError:
        return "(no server log)"


# --- the drill ------------------------------------------------------------------------------------------

def pin_recovery_point(socket: str | None = None, db: str | None = None,
                       schema: str | None = None, *,
                       lock_timeout: float = 1800) -> tuple[str, str, str, dict, float]:
    """Create a named restore point at exactly the state a REPEATABLE READ snapshot sees: both
    under the store's writer lock (so no store writer commits between them), which is released
    at once; the fingerprint of the locked schema is then computed in the snapshot. Returns
    (name, restore LSN, the WAL segment holding it — named by the server, so the timeline and a
    record ending exactly on a segment boundary are right — fingerprint, time.monotonic() of the
    WAL switch)."""
    socket, db = (SOCKET if socket is None else socket), (DB if db is None else db)
    schema = SCHEMA if schema is None else schema
    name = "drill-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    with connect(socket, db, autocommit=True) as locker, \
            connect(socket, db, autocommit=True) as reader:
        locker.execute(f"SET lock_timeout = '{int(lock_timeout)}s'")
        locker.execute("SELECT pg_advisory_lock(hashtext(%s))", [f"nekaise-writer:{schema}"])
        try:
            reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            reader.execute("SELECT 1").fetchone()   # the snapshot is taken here
            lsn, segment = locker.execute(
                "SELECT p::text, pg_walfile_name(p) FROM pg_create_restore_point(%s) p",
                [name]).fetchone()
            locker.execute("SELECT pg_switch_wal()")  # make the segment holding it archivable
            switched = time.monotonic()
        finally:
            locker.execute("SELECT pg_advisory_unlock(hashtext(%s))", [f"nekaise-writer:{schema}"])
        live = fingerprint(reader, [schema])
        reader.execute("ROLLBACK")
    return name, lsn, segment, live, switched


def wait_archived(segment: str, *, timeout: float, wal: Path | None = None,
                  socket: str | None = None, db: str | None = None) -> float:
    """Wait until `segment` is in the archive (the file itself); returns the seconds waited.
    Raises DrillError on timeout, naming the archiver's last failure if there is one."""
    wal, socket = (WAL if wal is None else wal), (SOCKET if socket is None else socket)
    t0 = time.monotonic()
    while not archived(segment, wal):
        if time.monotonic() - t0 > timeout:
            try:
                last = psql(socket, "SELECT last_failed_wal || ' at ' || last_failed_time FROM "
                            "pg_stat_archiver", db)
            except subprocess.CalledProcessError:
                last = "unknown"
            raise DrillError(f"segment {segment} was not archived within {timeout:.0f} s "
                             f"(last archiver failure: {last or 'none'})")
        time.sleep(0.5)
    return time.monotonic() - t0


class Terminated(Exception):
    """SIGTERM during a drill (a cron timeout, an operator): unwinds so the scratch instance is
    stopped and the record written."""


def _terminated(signum, frame):
    raise Terminated(f"signal {signum}")


def _sweep_scratch(log) -> list[str]:
    """Stop and remove what an earlier drill killed hard (SIGKILL) left in SCRATCH: its
    restore-test-* directories and any instance still running from one — but only an instance
    verified to be that directory's postmaster is ever signalled, and a directory whose instance
    could not be confirmed gone is KEPT (reported). Called under the drill lock, so no other
    drill is using them. Returns the directories kept."""
    kept = []
    for d in sorted(SCRATCH.glob("restore-test-*")):
        if not stop_instance(d / "data"):
            kept.append(str(d))
            log(f"kept the leftover drill directory {d.name}: its instance could not be "
                "confirmed stopped")
            continue
        shutil.rmtree(d, ignore_errors=True)
        log(f"removed a leftover drill directory {d.name}")
    return kept


def restore_test(base_name: str | None = None, *, archive_timeout: float = 900,
                 log=print) -> dict:
    """The drill (module docstring), one at a time (an flock in SCRATCH). SIGTERM unwinds it:
    the scratch instance is stopped and the failure recorded. Returns its record (also appended
    to DRILL_LOG)."""
    import fcntl
    import signal
    record: dict = {"at": _utc(), "ok": False}
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with open(SCRATCH / ".restore-test.lock", "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record["error"] = "another restore drill is running"
            log(f"restore drill: {record['error']}")
            return record
        previous = signal.signal(signal.SIGTERM, _terminated) \
            if threading_main() else None
        try:
            if kept := _sweep_scratch(log):
                record["leftover_scratch"] = kept
            with retention_lock(exclusive=False, timeout=600):
                return _drill(record, base_name, archive_timeout=archive_timeout, log=log)
        except RetentionBusy as exc:
            record["error"] = f"RetentionBusy: {exc}"
            _write_record(record)
            log(f"restore drill: {record['error']}")
            return record
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


def threading_main() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


def _drill(record: dict, base_name: str | None, *, archive_timeout: float, log) -> dict:
    work = None
    data = None
    try:
        found = bases()
        if not found:
            raise DrillError("no base backup to restore")
        chosen = next((b for b in found if b.name == base_name), None) if base_name else found[-1]
        if chosen is None:
            raise DrillError(f"no base backup {base_name}")
        record.update(base=chosen.name, base_age_hours=round((_now() - base_time(chosen)) / 3600, 2))
        target, lsn, segment, live, switched = pin_recovery_point()
        record.update(target=target, lsn=lsn, segment=segment, summary=summary(live))
        # confirmed archival of the recovery point's segment, never a fixed sleep; the time from
        # the WAL switch to the file in the archive is the archive latency of this drill
        record["archive_wait_s"] = round(wait_archived(segment, timeout=archive_timeout), 1)
        record["archived_after_switch_s"] = round(time.monotonic() - switched, 1)
        first = start_segment(chosen)
        record["segments_to_replay"] = segment_number(segment)[1] - segment_number(first)[1] + 1
        if gaps := missing_segments(first, segment):
            raise DrillError(f"WAL chain from {first} through {segment} is incomplete: "
                             f"{gaps[:5]}")
        # the recovery clock: from here to a verified restored state is the measured RTO
        t0 = time.monotonic()
        run(PGBIN / "pg_verifybackup", "-n", "-m", chosen / "backup_manifest", chosen)
        record["verify_s"] = round(time.monotonic() - t0, 1)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="restore-test-", dir=SCRATCH))
        (work / "restore.sh").write_text(RESTORE_SH.format(wal=WAL))
        data = work / "data"
        t1 = time.monotonic()
        extract(chosen, data)
        record["extract_s"] = round(time.monotonic() - t1, 1)
        (data / "recovery.signal").touch()
        t2 = time.monotonic()
        sock = start_instance(work, data, conf=(
            f"restore_command = 'sh {work}/restore.sh %f %p'\n"
            f"recovery_target_name = '{target}'\nrecovery_target_action = 'promote'\n"))
        record["recovery_s"] = round(time.monotonic() - t2, 1)
        t3 = time.monotonic()
        with connect(str(sock), DB, autocommit=True) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            restored = fingerprint(conn, list(live))
            conn.execute("ROLLBACK")
        record["fingerprint_s"] = round(time.monotonic() - t3, 1)
        record["rto_s"] = round(time.monotonic() - t0, 1)
        mismatches = diff(live, restored)
        record["mismatches"] = mismatches[:20]
        record["rto_ok"] = record["rto_s"] <= RTO_SECONDS
        # a drill that recovers the right state too slowly fails as well: the RTO is the claim
        record["ok"] = not mismatches and record["rto_ok"]
    except (DrillError, subprocess.CalledProcessError, OSError, Terminated) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else ""
        record["error"] = f"{type(exc).__name__}: {exc} {detail}".strip()[:1000]
    except Exception as exc:   # a database error: recorded, and the drill fails
        record["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        stopped = data is None or not data.exists() or stop_instance(data)
        if work is not None and stopped:
            shutil.rmtree(work, ignore_errors=True)
        elif work is not None:   # never delete under an instance that may still run
            record["ok"] = False
            record["error"] = (record.get("error") or "") + (
                f" the scratch instance could not be confirmed stopped: kept at {work}").strip()
    _write_record(record)
    log(f"restore drill from {record.get('base')}: {'OK' if record['ok'] else 'FAILED'} "
        f"(target {record.get('target')}, archive wait {record.get('archive_wait_s')} s, "
        f"RTO {record.get('rto_s')} s)")
    for line in record.get("mismatches", []):
        log(f"  MISMATCH {line}")
    if record.get("error"):
        log(f"  ERROR {record['error']}")
    return record


def _write_record(record: dict) -> None:
    try:
        DRILL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DRILL_LOG.open("a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        record["log_error"] = str(exc)
        record["ok"] = False


def last_drill(path: Path | None = None) -> dict | None:
    """The newest drill record, or None when no drill ran."""
    path = DRILL_LOG if path is None else path
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        if line.strip():
            return json.loads(line)
    return None


# --- recoverability facts -------------------------------------------------------------------------------

def archiver_state(conn) -> dict:
    """The live archiver's facts: configuration, counters, and the oldest segment still waiting
    (.ready) with its age. The ready list needs superuser or pg_monitor; an error propagates."""
    mode, timeout = (conn.execute("SELECT current_setting('archive_mode'), "
                                  "current_setting('archive_timeout')").fetchone())
    row = conn.execute(
        "SELECT archived_count, last_archived_wal, extract(epoch FROM now() - last_archived_time),"
        " failed_count, last_failed_wal, extract(epoch FROM now() - last_failed_time), "
        "pg_walfile_name(pg_current_wal_lsn()) FROM pg_stat_archiver").fetchone()
    ready = conn.execute(
        "SELECT count(*), extract(epoch FROM now() - min(modification)) FROM "
        "pg_ls_archive_statusdir() WHERE name LIKE '%.ready'").fetchone()
    last_ok, last_fail = row[2], row[5]
    return {"archive_mode": mode, "archive_timeout_s": _seconds(timeout),
            "archived": row[0], "last_archived_wal": row[1],
            "last_archived_s_ago": None if last_ok is None else round(float(last_ok), 1),
            "failed": row[3], "last_failed_wal": row[4],
            "last_failed_s_ago": None if last_fail is None else round(float(last_fail), 1),
            "failing": last_fail is not None and (last_ok is None or float(last_fail) < float(last_ok)),
            "current_wal": row[6], "ready_segments": int(ready[0]),
            "oldest_ready_s": None if ready[1] is None else round(float(ready[1]), 1)}


def _seconds(setting: str) -> int:
    m = re.fullmatch(r"(\d+)\s*(ms|s|min|h|d)?", setting.strip())
    if not m:
        raise ValueError(f"unparsable duration {setting!r}")
    return int(int(m.group(1)) * {"ms": 0.001, None: 1, "s": 1, "min": 60, "h": 3600,
                                  "d": 86400}[m.group(2)])


def exposure(archiver: dict) -> tuple[float | None, str | None]:
    """(worst-case age in seconds of committed WAL not yet in the archive, why it is unknown or
    unbounded). A write lands in the current segment, which archive_timeout forces out within
    archive_timeout seconds; a completed segment waits as .ready until archived. So the oldest
    unarchived write is at most (oldest .ready age + archive_timeout), or archive_timeout when
    nothing is waiting."""
    if archiver["archive_mode"] not in ("on", "always"):
        return None, f"archive_mode is {archiver['archive_mode']}: WAL is not archived"
    timeout = archiver["archive_timeout_s"]
    if not timeout:
        return None, "archive_timeout is 0: a quiet segment is never forced into the archive"
    return (archiver["oldest_ready_s"] or 0.0) + timeout, None


def recoverability(arch: dict, *, wal: Path | None = None,
                   root: Path | None = None) -> list[str]:
    """THE recoverability judgement — the growth block and every health report use this one
    function: the problems that make committed metadata unrecoverable within the RPO budget
    (empty: recoverable). `arch` is archiver_state() of the store's cluster.

    * WAL is archived at all (archive_mode, archive_timeout) and the exposure (oldest waiting
      segment + archive_timeout) is within RPO_SECONDS;
    * the segment the server reports as last archived is in the archive the restores read;
    * a base exists, and the server's timeline is the newest base's;
    * the WAL chain from the newest base's start through its end and on through the last
      archived segment is complete (prefix, interior and tail)."""
    wal, root = (WAL if wal is None else wal), (BASES if root is None else root)
    problems: list[str] = []
    exp, why = exposure(arch)
    if why:
        problems.append(why)
    elif exp > RPO_SECONDS:
        problems.append(
            f"committed WAL may be up to {exp / 60:.0f} min old without being archived (oldest "
            f"waiting segment {arch.get('oldest_ready_s') or 0:.0f} s + archive_timeout "
            f"{arch['archive_timeout_s']} s) — above the {RPO_SECONDS // 60}-minute RPO budget"
            + (f"; the archiver is failing ({arch.get('last_failed_wal')})"
               if arch.get("failing") else ""))
    last = arch.get("last_archived_wal")
    if arch.get("archived") and last:
        if not segment_like(last):
            problems.append(f"the server's last archived file {last!r} is not a WAL segment")
            last = None
        elif not archived(last, wal):
            problems.append(f"the server reports {last} archived, but it is not in {wal} (the "
                            "archive the restores read)")
    found = bases(root)
    if not found:
        problems.append("no base backup exists — nothing to recover from")
        return problems
    start, end = base_range(found[-1])
    current = arch.get("current_wal")
    if current and segment_like(current) and \
            segment_number(current)[0] != segment_number(start)[0]:
        problems.append(f"the server is on timeline {segment_number(current)[0]} but the newest "
                        f"base starts on timeline {segment_number(start)[0]} — take a new base")
        return problems
    tail = end
    if last and segment_number(last)[0] == segment_number(end)[0] and \
            segment_number(last)[1] > segment_number(end)[1]:
        tail = last
    if gaps := missing_segments(start, tail, wal):
        problems.append(f"the WAL chain from the newest base ({found[-1].name}: {start}) through "
                        f"{tail} is incomplete ({', '.join(gaps[:3])})")
    return problems


def coverage_gaps(*, wal: Path | None = None, root: Path | None = None,
                  through: str | None = None) -> list[str]:
    """The retention check on top of recoverability(): every segment from the OLDEST base's
    start through the newest base's end (or `through`, the last archived segment, when later)
    must be archived, so every kept point in time is recoverable."""
    found = bases(BASES if root is None else root)
    if not found:
        return ["no base backup"]
    start = start_segment(found[0])
    end = base_range(found[-1])[1]
    if through and segment_like(through) and segment_number(through)[0] == \
            segment_number(end)[0] and segment_number(through)[1] > segment_number(end)[1]:
        end = through
    return missing_segments(start, end, WAL if wal is None else wal)


def wal_rate(wal: Path | None = None, *, hours: float = 24.0,
             since: float | None = None) -> dict:
    """Archived WAL volume per day from the archive's own files (their mtimes) over the last
    `hours` (or since `since`)."""
    wal = WAL if wal is None else wal
    now = _now()
    lo = max(now - hours * 3600, since or 0)
    n = size = 0
    for p in wal.iterdir():
        try:
            s = p.stat()
        except FileNotFoundError:
            continue
        if s.st_mtime >= lo and _SEGMENT.fullmatch(p.name.removesuffix(".zst")):
            n += 1
            size += s.st_size
    span = max(now - lo, 1.0)
    return {"segments": n, "bytes": size, "window_h": round(span / 3600, 2),
            "gb_per_day": round(size / span * 86400 / 1e9, 2)}


def status(*, retain_days: float = 35, socket: str | None = None,
           db: str | None = None) -> dict:
    """Recoverability facts as one JSON-able dict (ops_health reads them; `status` prints them).
    Every part that cannot be read is reported as an error, never as healthy."""
    out: dict = {"at": _utc(), "rpo_s": RPO_SECONDS, "rto_s": RTO_SECONDS}
    try:
        with connect(socket, db, autocommit=True) as conn:
            out["archiver"] = archiver_state(conn)
        out["exposure_s"], why = exposure(out["archiver"])
        if why:
            out["exposure_error"] = why
    except Exception as exc:
        out["archiver"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    # the one recoverability judgement (the growth block uses the same function)
    try:
        if "error" in out["archiver"]:
            raise RuntimeError(f"cannot read the archiver: {out['archiver']['error']}")
        out["recoverability"] = recoverability(out["archiver"])
    except Exception as exc:
        out["recoverability"] = [f"recoverability unknown: {type(exc).__name__}: {exc}"[:300]]
    try:
        found = bases()
        out["bases"] = {"count": len(found), "names": [b.name for b in found],
                        "newest_age_h": round((_now() - base_time(found[-1])) / 3600, 2)
                        if found else None,
                        "oldest": found[0].name if found else None}
        if found:
            out["bases"]["recoverable_from"] = _utc(base_time(found[0]))
            out["bases"]["coverage_days"] = round((_now() - base_time(found[0])) / 86400, 2)
            out["wal_chain_gaps"] = coverage_gaps(
                through=(out.get("archiver") or {}).get("last_archived_wal"))
    except Exception as exc:
        out["bases"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        usage = shutil.disk_usage(WAL)
        rate = wal_rate()
        base_gb = sum(f.stat().st_size for b in bases() for f in b.iterdir()) / 1e9 \
            if BASES.exists() else 0.0
        wal_gb = sum(p.stat().st_size for p in WAL.iterdir() if p.is_file()) / 1e9
        out["capacity"] = {"free_gb": round(usage.free / 1e9, 1),
                           "total_gb": round(usage.total / 1e9, 1),
                           "free_fraction": round(usage.free / usage.total, 3),
                           "wal_archive_gb": round(wal_gb, 1), "bases_gb": round(base_gb, 1),
                           "wal_rate": rate,
                           "projected_wal_gb": round(rate["gb_per_day"] * retain_days, 1)}
    except Exception as exc:
        out["capacity"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        drill = last_drill()
        out["last_drill"] = None if drill is None else {
            k: drill.get(k) for k in ("at", "ok", "base", "target", "rto_s", "archive_wait_s",
                                      "error", "segments_to_replay")}
        with DRILL_LOG.open() as f:   # the first drill: how long this retention has run
            first = next((json.loads(line) for line in f if line.strip()), None)
        out["first_drill_at"] = None if first is None else first.get("at")
    except FileNotFoundError:
        out["last_drill"] = out["first_drill_at"] = None
    except (OSError, ValueError) as exc:
        out["last_drill"] = {"at": None, "ok": False, "error": f"unreadable drill log: {exc}"}
        out["first_drill_at"] = None
    return out


def rpo_probe(count: int = 1, *, timeout: float = 1200, socket: str | None = None,
              db: str | None = None,
              log=print) -> list[dict]:
    """Measure the loss window directly: write a WAL record (a non-transactional logical
    message — no table changes), then time until the segment holding it is in the archive."""
    out = []
    for _ in range(count):
        with connect(socket, db, autocommit=True) as conn:
            lsn, segment = conn.execute(
                "SELECT m::text, pg_walfile_name(m) FROM pg_logical_emit_message(false, "
                "'nekaise-rpo-probe', %s) m", [_utc()]).fetchone()
        t0 = _now()
        waited = wait_archived(segment, timeout=timeout, socket=socket, db=db)
        out.append({"at": _utc(t0), "lsn": lsn, "segment": segment,
                    "archived_after_s": round(waited, 1), "within_rpo": waited <= RPO_SECONDS})
        log(json.dumps(out[-1]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("base", "restore-test", "prune", "status", "rpo-probe",
                                        "fingerprint"))
    ap.add_argument("--keep", type=int, default=None,
                    help="prune (legacy): keep the newest N bases")
    ap.add_argument("--retain-days", type=float, default=None,
                    help="prune: keep D days of recoverable coverage")
    ap.add_argument("--daily-days", type=float, default=7.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--base", default=None, help="restore-test: the base to restore")
    ap.add_argument("--archive-timeout", type=float, default=900.0)
    ap.add_argument("--count", type=int, default=1)
    args = ap.parse_args()
    if args.command == "base":
        base()
    elif args.command == "restore-test":
        return 0 if restore_test(args.base, archive_timeout=args.archive_timeout)["ok"] else 1
    elif args.command == "prune":
        if (args.keep is None) == (args.retain_days is None):
            ap.error("prune needs exactly one of --keep N or --retain-days D")
        prune(args.keep, retain_days=args.retain_days, daily_days=args.daily_days,
              dry_run=args.dry_run)
    elif args.command == "status":
        print(json.dumps(status(retain_days=args.retain_days or 35), indent=1))
    elif args.command == "rpo-probe":
        probes = rpo_probe(args.count)
        return 0 if all(p["within_rpo"] for p in probes) else 1
    else:
        with connect(autocommit=True) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            fp = fingerprint(conn, [SCHEMA])
            conn.execute("ROLLBACK")
        print(json.dumps({"summary": summary(fp), "fingerprint": fp}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
