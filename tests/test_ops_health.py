"""ops_health.py (ADR 0001 stage 4 step 5): alert thresholds over recoverability facts, payload
backups apart from the metadata RPO, sweep freshness, alert transitions, and the recoverability
growth block failing closed. No database (lifecycle checks over a real schema run in
tests/test_rehearsal.py)."""
from __future__ import annotations

import json
import time

import pytest

import ops_health
import pg_backup

NOW = 1_790_000_000.0


def facts(**over):
    base = {
        "archiver": {"archive_mode": "on", "archive_timeout_s": 300, "failing": False,
                     "oldest_ready_s": None, "last_failed_wal": None},
        "exposure_s": 300.0,
        "recoverability": [],
        "bases": {"count": 3, "newest_age_h": 5.0, "oldest": "20260901T033000Z"},
        "wal_chain_gaps": [],
        "last_drill": {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 86400)),
                       "ok": True, "rto_s": 846.2},
        "capacity": {"free_gb": 1600.0, "total_gb": 1900.0, "free_fraction": 0.84,
                     "wal_archive_gb": 10.0, "bases_gb": 20.0, "projected_wal_gb": 105.0},
    }
    base.update(over)
    return base


def sev(checks, name):
    return next(c["severity"] for c in checks if c["check"] == name)


def test_healthy_facts_raise_nothing():
    checks = ops_health.metadata_checks(facts(), NOW)
    assert {c["severity"] for c in checks} == {"ok"}, checks


@pytest.mark.parametrize("over, check, severity", [
    ({"exposure_s": 1000.0, "recoverability": ["committed WAL may be up to 17 min old"]},
     "archive", "critical"),
    ({"exposure_s": 700.0}, "archive", "warning"),
    ({"exposure_s": None, "recoverability": ["archive_mode is off"]}, "archive", "critical"),
    ({"archiver": {"error": "permission denied"},
      "recoverability": ["recoverability unknown"]}, "archive", "critical"),
    ({"recoverability": None}, "archive", "critical"),       # never evaluated: not healthy
    ({"archiver": {"archive_mode": "on", "archive_timeout_s": 300, "failing": True,
                   "oldest_ready_s": None, "last_failed_wal": "x"}}, "archive", "warning"),
    ({"bases": {"count": 0}}, "base_backup", "critical"),
    ({"bases": {"count": 1, "newest_age_h": 30.0, "oldest": "x"}}, "base_backup", "warning"),
    ({"bases": {"count": 1, "newest_age_h": 60.0, "oldest": "x"}}, "base_backup", "critical"),
    ({"wal_chain_gaps": ["000000010000000100000012"]}, "wal_chain", "critical"),
    ({"last_drill": None}, "restore_drill", "warning"),
    ({"last_drill": {"at": "2026-09-01T00:00:00Z", "ok": False, "error": "mismatch"}},
     "restore_drill", "critical"),
    ({"last_drill": {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW)), "ok": True,
                     "rto_s": 4000}}, "restore_drill", "critical"),
    ({"last_drill": {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 10 * 86400)),
                     "ok": True, "rto_s": 100}}, "restore_drill", "warning"),
    ({"capacity": {"free_gb": 50.0, "total_gb": 1900.0, "free_fraction": 0.026,
                   "wal_archive_gb": 10.0, "bases_gb": 20.0, "projected_wal_gb": 105.0}},
     "backup_capacity", "critical"),
    ({"capacity": {"free_gb": 1600.0, "total_gb": 1900.0, "free_fraction": 0.84,
                   "wal_archive_gb": 10.0, "bases_gb": 40.0, "projected_wal_gb": 300.0}},
     "backup_capacity", "warning"),
    ({"capacity": {"error": "unmounted"}}, "backup_capacity", "critical"),
])
def test_each_threshold(over, check, severity):
    assert sev(ops_health.metadata_checks(facts(**over), NOW), check) == severity


def test_the_metadata_budget_proposes_a_retention_that_fits():
    over = {"capacity": {"free_gb": 1600.0, "total_gb": 1900.0, "free_fraction": 0.84,
                         "wal_archive_gb": 10.0, "bases_gb": 40.0, "projected_wal_gb": 300.0,
                         "wal_rate": {"gb_per_day": 300.0 / 35}}}
    c = next(c for c in ops_health.metadata_checks(facts(**over), NOW)
             if c["check"] == "backup_capacity")
    assert c["severity"] == "warning"
    assert c["facts"]["fitting_retention_days"] == pytest.approx((200 - 40) / (300 / 35), 0.01)


def test_payload_backups_are_reported_apart_from_the_metadata_rpo(tmp_path):
    path = tmp_path / "backup-status.json"
    assert ops_health.payload_check(NOW, path)["severity"] == "warning"   # never ran
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(NOW - 3600))
    path.write_text(json.dumps({"result": "up-to-date", "error": None,
                                "latest_backup": f"/ssd/corpus-{stamp}-f2eb400a"}))
    c = ops_health.payload_check(NOW, path)
    assert c["severity"] == "ok" and c["facts"]["age_h"] == pytest.approx(1.0)
    assert "apart from the metadata RPO" in c["summary"]
    old = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(NOW - 100 * 3600))
    path.write_text(json.dumps({"result": "up-to-date", "latest_backup": f"/x/corpus-{old}-a"}))
    assert ops_health.payload_check(NOW, path)["severity"] == "critical"
    path.write_text("{broken")
    assert ops_health.payload_check(NOW, path)["severity"] == "critical"


def test_sweep_failures_are_critical_and_stale_passes_warn(tmp_path):
    path = tmp_path / "sweep.json"
    assert ops_health.sweep_check(NOW, path)["severity"] == "warning"
    path.write_text(json.dumps({"metadata": {"completed_at": NOW - 86400, "failures": []},
                                "artifacts": {"completed_at": NOW - 86400, "failures": []}}))
    assert ops_health.sweep_check(NOW, path)["severity"] == "ok"
    path.write_text(json.dumps({"metadata": {"completed_at": NOW - 20 * 86400},
                                "artifacts": {"completed_at": NOW - 86400}}))
    assert ops_health.sweep_check(NOW, path)["severity"] == "warning"
    path.write_text(json.dumps({"metadata": {"completed_at": NOW, "failures": []},
                                "artifacts": {"completed_at": NOW, "failures": ["raw x: damaged"]}}))
    assert ops_health.sweep_check(NOW, path)["severity"] == "critical"


def test_record_appends_only_transitions(tmp_path):
    state, alerts = tmp_path / "state.json", tmp_path / "alerts.jsonl"
    ok = [{"check": "archive", "severity": "ok", "summary": "fine", "facts": {}}]
    bad = [{"check": "archive", "severity": "critical", "summary": "lag", "facts": {}}]
    assert ops_health.record(ok, state=state, alerts=alerts, now=NOW) == []
    assert not alerts.exists()
    assert [c["to"] for c in ops_health.record(bad, state=state, alerts=alerts, now=NOW)] == [
        "critical"]
    assert ops_health.record(bad, state=state, alerts=alerts, now=NOW) == []
    assert [c["to"] for c in ops_health.record(ok, state=state, alerts=alerts, now=NOW)] == ["ok"]
    assert [json.loads(line)["to"] for line in alerts.read_text().splitlines()] == [
        "critical", "ok"]
    assert json.loads(state.read_text())["worst"] == "ok"


@pytest.mark.recoverability
def test_the_recoverability_block_fails_closed(monkeypatch, tmp_path):
    arch = {"archive_mode": "on", "archive_timeout_s": 300, "oldest_ready_s": None,
            "failing": False, "last_failed_wal": None, "archived": 1,
            "last_archived_wal": "000000010000000100000010",
            "current_wal": "000000010000000100000011"}
    monkeypatch.setattr(pg_backup, "archiver_state", lambda conn: dict(arch))
    bases = tmp_path / "bases"
    base = bases / "20260925T013001Z"
    base.mkdir(parents=True)
    (base / "backup_manifest").write_text(json.dumps(
        {"WAL-Ranges": [{"Timeline": 1, "Start-LSN": "1/10000000", "End-LSN": "1/10000000"}]}))
    wal = tmp_path / "wal"
    wal.mkdir()
    (wal / "000000010000000100000010").write_bytes(b"")
    # monitoring and blocking give the same answer, always
    def same():
        got = ops_health.recoverability_block(None)
        assert (got is None) == (pg_backup.recoverability(dict(arch)) == [])
        return got
    monkeypatch.setattr(pg_backup, "BASES", bases)
    monkeypatch.setattr(pg_backup, "WAL", wal)
    assert same() is None
    arch["oldest_ready_s"] = 700.0          # 700 + 300 > 900
    assert "RPO budget" in same()
    # an archive_timeout above the budget with nothing waiting: a reason, not a crash
    arch.update(oldest_ready_s=None, archive_timeout_s=1000)
    assert "RPO budget" in same()
    arch["archive_timeout_s"] = 300
    arch.update(oldest_ready_s=None, archive_mode="off")
    assert "archive_mode is off" in same()
    arch["archive_mode"] = "on"
    # the server says it archived a segment the archive does not hold (archive_command writing
    # elsewhere): blocked
    arch["last_archived_wal"] = "000000010000000100000099"
    assert "not in" in same()
    arch["last_archived_wal"] = "000000010000000100000010"
    # a timeline switch after the newest base: a new base is needed
    arch["current_wal"] = "000000020000000100000011"
    assert "timeline" in same()
    arch["current_wal"] = "000000010000000100000013"
    # the server archived through ...12, the archive has ...12 but not ...11: an interior gap
    arch["last_archived_wal"] = "000000010000000100000012"
    (wal / "000000010000000100000012").write_bytes(b"")
    assert "incomplete" in same()
    (wal / "000000010000000100000011").write_bytes(b"")
    assert same() is None
    # the base's own start segment missing (a missing prefix)
    (wal / "000000010000000100000010").unlink()
    assert "incomplete" in same()
    (wal / "000000010000000100000010").write_bytes(b"")

    def broken(conn):
        raise PermissionError("pg_ls_archive_statusdir: permission denied")
    monkeypatch.setattr(pg_backup, "archiver_state", broken)
    assert "recoverability unknown" in ops_health.recoverability_block(None)
    monkeypatch.setattr(pg_backup, "archiver_state", lambda conn: dict(arch))
    for p in bases.iterdir():
        for f in p.iterdir():
            f.unlink()
        p.rmdir()
    assert "no base backup" in same()


def test_the_retention_target_warns_once_drills_have_run_long_enough():
    first = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 50 * 86400))
    short = facts(bases={"count": 3, "newest_age_h": 5.0, "oldest": "x", "coverage_days": 20.0},
                  first_drill_at=first)
    assert sev(ops_health.metadata_checks(short, NOW), "retention") == "warning"
    young = facts(bases={"count": 3, "newest_age_h": 5.0, "oldest": "x", "coverage_days": 20.0},
                  first_drill_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(NOW - 10 * 86400)))
    assert sev(ops_health.metadata_checks(young, NOW), "retention") == "ok"
    full = facts(bases={"count": 13, "newest_age_h": 5.0, "oldest": "x", "coverage_days": 36.0},
                 first_drill_at=first)
    assert sev(ops_health.metadata_checks(full, NOW), "retention") == "ok"


def test_the_recheck_interval_can_only_shrink(monkeypatch):
    import run_round
    for raw, want in (("1", 1.0), ("0", 1.0), ("-5", 1.0), ("100000", 300.0), ("x", 300.0),
                      ("", 300.0)):
        monkeypatch.setenv("NEKAISE_RECOVERABILITY_RECHECK_SECONDS", raw)
        assert run_round.recheck_seconds() == want


def test_sweep_failures_of_the_pass_in_progress_are_critical_until_a_clean_pass(tmp_path):
    path = tmp_path / "sweep.json"
    done = {"completed_at": NOW - 3600, "failures": [], "failure_count": 0}
    path.write_text(json.dumps({"metadata": {**done, "current": {
        "failures": ["manifest x: its raw claim resolves to nothing here"],
        "failure_count": 1}}, "artifacts": done}))
    c = ops_health.sweep_check(NOW, path)
    assert c["severity"] == "critical" and "metadata" in c["facts"]["failures"]
    # a completed pass with failures stays critical while the next pass is clean so far
    path.write_text(json.dumps({"metadata": {**done, "failures": ["x"], "failure_count": 1,
                                             "current": {"failures": [], "failure_count": 0}},
                                "artifacts": done}))
    assert ops_health.sweep_check(NOW, path)["severity"] == "critical"
    # counts without the list (truncated) still count
    path.write_text(json.dumps({"metadata": {**done, "current": {"failure_count": 3}},
                                "artifacts": done}))
    assert ops_health.sweep_check(NOW, path)["severity"] == "critical"
    path.write_text(json.dumps({"metadata": done, "artifacts": done}))   # a clean pass
    assert ops_health.sweep_check(NOW, path)["severity"] == "ok"
    path.write_text(json.dumps({"metadata": ["not", "an", "object"]}))
    assert ops_health.sweep_check(NOW, path)["severity"] == "critical"


@pytest.mark.parametrize("doc", [
    [1, 2], "a string", {"latest_backup": 7}, {"latest_backup": "/x/corpus-nodate"},
    {"latest_backup": "/x/corpus-20269999T999999Z-a", "result": "ok"},
    {"latest_backup": "/x/corpus-20260925T032147Z-a", "result": {"nested": 1}},
    {"latest_backup": None},
])
def test_a_malformed_payload_status_is_critical_never_an_exception(tmp_path, doc):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(doc))
    c = ops_health.payload_check(NOW, path)
    assert c["severity"] == "critical" and c["check"] == "payload_backup"


def test_health_is_always_recorded_even_when_evaluation_breaks(tmp_path, monkeypatch):
    state, alerts = tmp_path / "state.json", tmp_path / "alerts.jsonl"
    monkeypatch.setattr(ops_health, "STATE", state)
    monkeypatch.setattr(ops_health, "ALERTS", alerts)
    (tmp_path / "status.json").write_text(json.dumps({"latest_backup": [1]}))
    monkeypatch.setattr(ops_health, "PAYLOAD_STATUS", tmp_path / "status.json")

    def boom(**_):
        raise RuntimeError("the backup SSD vanished mid-read")
    monkeypatch.setattr(pg_backup, "status", boom)
    assert ops_health.main(["check"]) == 2
    checks = {c["check"]: c["severity"]
              for c in json.loads(state.read_text())["checks"]}
    assert checks["metadata_recoverability"] == "critical"
    assert checks["payload_backup"] == "critical"
    monkeypatch.setattr(ops_health, "evaluate", lambda **_: 1 / 0)
    assert ops_health.main(["check"]) == 2
    assert json.loads(state.read_text())["checks"][0]["check"] == "ops_health"


def test_review_evidence_survives_a_malformed_payload_status(tmp_path, monkeypatch):
    import generation_review
    (tmp_path / "status.json").write_text(json.dumps({"latest_backup": {"x": 1}}))
    monkeypatch.setattr(ops_health, "PAYLOAD_STATUS", tmp_path / "status.json")

    class Conn:
        def transaction(self):
            raise RuntimeError("no database here")
    out = generation_review._backup_health(Conn(), tmp_path / "no-bases")
    assert out["payload_backup"]["severity"] == "critical"
    assert "recoverability unknown" in out["recoverability"]["problems"][0]
