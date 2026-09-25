"""pg_backup.py (ADR 0001 stage 4 step 5): retention coverage, WAL arithmetic and chain gaps,
the exposure bound, fingerprint comparison, the drill's fail-closed paths. No database: the
drill against a real cluster is exercised in tests/test_rehearsal.py and on the live base."""
from __future__ import annotations

import calendar
import json
import time
from pathlib import Path

import pytest

import pg_backup


def ts(text: str) -> float:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def name(text: str) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts(text)))


def test_segment_arithmetic():
    assert pg_backup.segment_of("1E/7B0004A8") == "000000010000001E0000007B"
    assert pg_backup.segment_of("F/10FFDD58") == "000000010000000F00000010"
    assert pg_backup.segment_of("0/0", timeline=2) == "000000020000000000000000"
    tli, seg = pg_backup.segment_number("000000010000001E000000FF")
    assert (tli, pg_backup.segment_name(tli, seg + 1)) == (1, "000000010000001F00000000")


def test_retention_keeps_daily_weekly_and_the_anchor():
    now = ts("2026-11-01T12:00:00Z")
    days = [f"2026-{m:02d}-{d:02d}T03:30:00Z" for m, d in
            [(9, 20), (9, 22), (9, 24), (9, 25)] + [(10, d) for d in range(1, 32)]
            + [(11, 1)]]
    names = [name(d) for d in days]
    keep, remove = pg_backup.retention_plan(names, now, retain_days=35, daily_days=7)
    assert sorted(keep + remove) == sorted(names) and not set(keep) & set(remove)
    # every base of the last 7 days
    assert all(n in keep for n in names if ts(pg_backup.dt.datetime.strptime(
        n, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")) >= now - 7 * 86400)
    # the anchor: the newest base at least 35 days old (2026-09-25 03:30 < 2026-09-27 12:00)
    assert name("2026-09-25T03:30:00Z") in keep
    assert name("2026-09-24T03:30:00Z") not in keep and name("2026-09-20T03:30:00Z") in remove
    # between 35 and 7 days: one per ISO week, the newest of the week
    weekly = [n for n in keep if now - 35 * 86400 <= _t(n) < now - 7 * 86400]
    weeks = {pg_backup.dt.datetime.fromtimestamp(_t(n), pg_backup.dt.timezone.utc)
             .isocalendar()[:2] for n in weekly}
    assert len(weekly) == len(weeks) >= 4
    # coverage: for any point in the last 35 days a kept base starts at or before it
    for hours in range(0, 35 * 24, 7):
        point = now - hours * 3600
        assert any(_t(n) <= point for n in keep), point


def _t(n: str) -> float:
    return pg_backup.base_time(Path(n))


def test_retention_before_35_days_exist_keeps_everything_and_the_newest_always():
    now = ts("2026-09-30T12:00:00Z")
    names = [name(f"2026-09-{d:02d}T03:30:00Z") for d in (10, 20, 29)]
    keep, remove = pg_backup.retention_plan(names, now, retain_days=35, daily_days=7)
    assert keep == sorted(names) and remove == []
    keep, _ = pg_backup.retention_plan([names[0]], now + 400 * 86400, retain_days=35,
                                       daily_days=7)
    assert keep == [names[0]]
    with pytest.raises(ValueError):
        pg_backup.retention_plan(names, now, retain_days=3, daily_days=7)


def _wal(tmp: Path, *segments: str) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    for s in segments:
        (tmp / s).write_bytes(b"")
    return tmp


def test_missing_segments_needs_the_start_and_the_explicit_end(tmp_path):
    wal = _wal(tmp_path / "wal", "000000010000000100000010", "000000010000000100000011",
               "000000010000000100000013.zst", "000000010000000100000013.00000028.backup")
    ms = pg_backup.missing_segments
    assert ms("000000010000000100000010", "000000010000000100000013", wal) == [
        "000000010000000100000012"]                                    # interior gap
    assert pg_backup.archived("000000010000000100000013", wal)
    (wal / "000000010000000100000012").write_bytes(b"")
    assert ms("000000010000000100000010", "000000010000000100000013", wal) == []
    # the start beyond everything archived: the whole required range is missing, not "[]"
    assert ms("000000010000000100000020", "000000010000000100000021", wal) == [
        "000000010000000100000020", "000000010000000100000021"]
    # a missing prefix, and a missing tail
    assert ms("00000001000000010000000E", "000000010000000100000010", wal) == [
        "00000001000000010000000E", "00000001000000010000000F"]
    assert ms("000000010000000100000012", "000000010000000100000015", wal) == [
        "000000010000000100000014", "000000010000000100000015"]
    assert "crosses timelines" in ms("000000010000000100000010", "000000020000000100000010",
                                     wal)[0]
    assert "precedes" in ms("000000010000000100000011", "000000010000000100000010", wal)[0]


def _base(root: Path, when: str, start_lsn: str, end_lsn: str | None = None) -> Path:
    d = root / name(when)
    d.mkdir(parents=True)
    (d / "backup_manifest").write_text(json.dumps(
        {"WAL-Ranges": [{"Timeline": 1, "Start-LSN": start_lsn,
                         "End-LSN": end_lsn or start_lsn}]}))
    return d


def test_prune_refuses_a_broken_chain_and_dry_run_touches_nothing(tmp_path, monkeypatch):
    bases = tmp_path / "bases"
    old = _base(bases, "2026-08-01T03:30:00Z", "1/10000000")
    new = _base(bases, "2026-09-29T03:30:00Z", "1/12000000")
    wal = _wal(tmp_path / "wal", "000000010000000100000010", "000000010000000100000012")
    now = ts("2026-09-30T00:00:00Z")
    with pytest.raises(SystemExit, match="incomplete"):
        pg_backup.prune(retain_days=35, dry_run=True, now=now, root=bases, wal=wal,
                        log=lambda *_: None)
    (wal / "000000010000000100000011").write_bytes(b"")
    calls = []
    monkeypatch.setattr(pg_backup, "run", lambda *a, **k: calls.append(a))
    out = pg_backup.prune(retain_days=35, dry_run=True, now=now, root=bases, wal=wal,
                          log=lambda *_: None)
    assert out["kept"] == [old.name, new.name] and out["removed"] == []
    assert calls == [] and old.exists()
    # legacy --keep 1: the newest base only, WAL (plain and .zst) cleaned to its start
    out = pg_backup.prune(1, root=bases, wal=wal, now=now, log=lambda *_: None)
    assert out["kept"] == [new.name] and not old.exists()
    assert calls and calls[0][-3:] == (".zst", wal, "000000010000000100000012")


@pytest.mark.parametrize("have, why", [
    (["000000010000000100000010", "000000010000000100000012", "000000010000000100000013"],
     "missing prefix (the newer base's start)"),
    (["000000010000000100000010", "000000010000000100000011", "000000010000000100000013"],
     "interior gap"),
    (["000000010000000100000010", "000000010000000100000011", "000000010000000100000012"],
     "missing tail (the newer base's end)"),
    (["000000010000000100000010"], "the whole range of the newer base (the reviewed case)"),
    ([], "nothing archived"),
])
def test_prune_never_drops_the_fallback_for_a_base_the_archive_cannot_serve(
        tmp_path, monkeypatch, have, why):
    """The newer base needs ...11 through its end ...13; the older fallback starts at ...10. If
    any of that range is missing, keeping only the newer base would leave nothing recoverable."""
    bases = tmp_path / "bases"
    old = _base(bases, "2026-09-20T03:30:00Z", "1/10000000")
    new = _base(bases, "2026-09-29T03:30:00Z", "1/11000000", "1/13000100")
    wal = _wal(tmp_path / "wal", *have)
    calls = []
    monkeypatch.setattr(pg_backup, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(SystemExit, match="incomplete"):
        pg_backup.prune(1, root=bases, wal=wal, log=lambda *_: None)
    assert old.exists() and new.exists() and calls == [], why


def test_prune_waits_for_a_running_drill(tmp_path, monkeypatch):
    bases = tmp_path / "bases"
    old = _base(bases, "2026-09-20T03:30:00Z", "1/10000000")
    _base(bases, "2026-09-29T03:30:00Z", "1/11000000")
    wal = _wal(tmp_path / "wal", "000000010000000100000010", "000000010000000100000011")
    monkeypatch.setattr(pg_backup, "run", lambda *a, **k: None)
    with pg_backup.retention_lock(exclusive=False, timeout=1, root=bases):   # a drill
        with pytest.raises(SystemExit, match="a drill is using the backups"):
            pg_backup.prune(1, root=bases, wal=wal, lock_timeout=0.5, log=lambda *_: None)
        assert old.exists()
    pg_backup.prune(1, root=bases, wal=wal, lock_timeout=0.5, log=lambda *_: None)
    assert not old.exists()


def test_archive_cleanup_removes_plain_and_compressed_segments(tmp_path):
    """The real pg_archivecleanup with -x .zst over a mixed archive."""
    binary = pg_backup.PGBIN / "pg_archivecleanup"
    if not binary.exists():
        pytest.skip("pg_archivecleanup not installed here")
    bases = tmp_path / "bases"
    _base(bases, "2026-09-29T03:30:00Z", "1/12000000")
    wal = _wal(tmp_path / "wal", "00000001000000010000000F", "000000010000000100000010.zst",
               "000000010000000100000011", "000000010000000100000012.zst",
               "000000010000000100000013")
    pg_backup.prune(1, root=bases, wal=wal, log=lambda *_: None)
    assert sorted(p.name for p in wal.iterdir()) == [
        "000000010000000100000012.zst", "000000010000000100000013"]


def _fake_postgres(data: Path):
    """A process that looks like data's postmaster: argv[0] 'postgres', cwd = data."""
    import subprocess
    data.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["bash", "-c", "exec -a postgres sleep 600"], cwd=data)
    time.sleep(0.2)
    start = pg_backup._proc(proc.pid)["start"]
    (data / "postmaster.pid").write_text(f"{proc.pid}\n{data}\n{int(start)}\n5432\n/tmp\n")
    return proc


def test_stop_never_signals_an_unrelated_process_with_a_reused_pid(tmp_path, monkeypatch):
    import subprocess
    data = tmp_path / "restore-test-x" / "data"
    data.mkdir(parents=True)
    other = subprocess.Popen(["sleep", "600"])          # not a postgres, not in `data`
    try:
        start = pg_backup._proc(other.pid)["start"]
        (data / "postmaster.pid").write_text(f"{other.pid}\n{data}\n{int(start) - 5000}\n")
        stops = []
        monkeypatch.setattr(pg_backup, "_pg_ctl_stop", lambda d, m: stops.append(m) or 0)
        assert pg_backup.instance_state(data) == "other"
        assert pg_backup.stop_instance(data, wait=0.5) is True    # the postmaster is gone
        assert stops == [] and other.poll() is None               # nothing signalled
    finally:
        other.kill()
        other.wait()
    # an unreadable postmaster.pid is uncertain: kept
    (data / "postmaster.pid").write_text("garbage\n")
    assert pg_backup.instance_state(data) == "uncertain"
    assert pg_backup.stop_instance(data, wait=0.5) is False


def test_a_failed_stop_keeps_the_scratch_directory(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(pg_backup, "SCRATCH", scratch)
    monkeypatch.setattr(pg_backup, "STOP_WAIT_SECONDS", 1.0)
    data = scratch / "restore-test-dead" / "data"
    proc = _fake_postgres(data)
    try:
        assert pg_backup.instance_state(data) == "ours"
        monkeypatch.setattr(pg_backup, "_pg_ctl_stop", lambda d, m: 0)   # "succeeds", does not
        assert pg_backup._sweep_scratch(lambda *_: None) == [str(data.parent)]
        assert data.exists() and proc.poll() is None
        # a stop that really stops it: the directory goes
        monkeypatch.setattr(pg_backup, "_pg_ctl_stop", lambda d, m: proc.terminate() or 0)
        assert pg_backup._sweep_scratch(lambda *_: None) == []
        assert not data.parent.exists()
    finally:
        proc.kill()
        proc.wait()


def test_exposure_bounds_the_unarchived_age():
    base = {"archive_mode": "on", "archive_timeout_s": 300, "oldest_ready_s": None}
    assert pg_backup.exposure(base) == (300, None)
    assert pg_backup.exposure({**base, "oldest_ready_s": 700.0}) == (1000.0, None)
    assert pg_backup.exposure({**base, "archive_mode": "off"})[0] is None
    assert "never forced" in pg_backup.exposure({**base, "archive_timeout_s": 0})[1]
    assert pg_backup._seconds("5min") == 300 and pg_backup._seconds("300") == 300
    with pytest.raises(ValueError):
        pg_backup._seconds("five")


def test_fingerprint_diff_names_every_difference():
    a = {"s": {"tables": {"t": "1:1:2:3:4", "u": "0:0:0:0:0"}, "content": {"u": []}}}
    assert pg_backup.diff(a, json.loads(json.dumps(a))) == []
    b = {"s": {"tables": {"t": "1:9:2:3:4", "u": "0:0:0:0:0"}, "content": {"u": []}},
         "extra": {"tables": {}, "content": {}}}
    got = pg_backup.diff(a, b)
    assert any(line.startswith("s.t (tables)") for line in got)
    assert any(line.startswith("extra:") for line in got)


def test_a_drill_without_a_base_fails_and_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(pg_backup, "BASES", tmp_path / "bases")
    (tmp_path / "bases").mkdir()
    monkeypatch.setattr(pg_backup, "DRILL_LOG", tmp_path / "drills.jsonl")
    rec = pg_backup.restore_test(log=lambda *_: None)
    assert rec["ok"] is False and "no base backup" in rec["error"]
    assert pg_backup.last_drill(tmp_path / "drills.jsonl")["error"] == rec["error"]


def test_waiting_for_archival_times_out_naming_the_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(pg_backup, "psql", lambda *a, **k: "")
    with pytest.raises(pg_backup.DrillError, match="000000010000000100000010"):
        pg_backup.wait_archived("000000010000000100000010", timeout=0.2, wal=tmp_path)
    (tmp_path / "000000010000000100000010").write_bytes(b"")
    assert pg_backup.wait_archived("000000010000000100000010", timeout=1, wal=tmp_path) < 1


def test_one_drill_at_a_time_and_leftovers_are_swept(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(pg_backup, "BASES", tmp_path / "bases")
    (tmp_path / "bases").mkdir()
    monkeypatch.setattr(pg_backup, "DRILL_LOG", tmp_path / "drills.jsonl")
    monkeypatch.setattr(pg_backup, "SCRATCH", tmp_path / "scratch")
    leftover = tmp_path / "scratch" / "restore-test-dead" / "data"
    leftover.mkdir(parents=True)
    rec = pg_backup.restore_test(log=lambda *_: None)
    assert "no base backup" in rec["error"] and not leftover.parent.exists()
    with open(tmp_path / "scratch" / ".restore-test.lock", "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        rec = pg_backup.restore_test(log=lambda *_: None)
    assert rec["error"] == "another restore drill is running" and not rec["ok"]
