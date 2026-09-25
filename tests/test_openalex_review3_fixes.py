"""Regression tests for Codex's FOURTH review: every OpenAlex caller shares one machine-level
pacer and cooldown file, cooldown writes never erase each other, and concurrent families keep
separate resolution records. No network; the machine-level directory is isolated by conftest."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import find_sources
import openalex_families as fam
import openalex_state
import ops
from test_openalex_sim import NOW, POLICY, FakeHttp, good, make_run, page


def throttled(headers):
    return SimpleNamespace(status_code=429, headers=headers, json=lambda: {},
                           raise_for_status=lambda: None)


# --- one machine-level location, isolated in tests ----------------------------------------------

def test_machine_state_is_one_directory_for_every_checkout(monkeypatch, tmp_path):
    here = openalex_state.state_dir()
    assert here == Path(__import__("os").environ[openalex_state.STATE_ENV])
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path / "checkout-a" / "workspace")
    assert openalex_state.state_dir() == here  # not checkout-local
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path / "worktree-b" / "workspace")
    assert openalex_state.state_dir() == here
    assert openalex_state.Cooldowns().path.parent == here


def test_tests_can_never_reach_the_real_machine_directory(monkeypatch):
    monkeypatch.delenv(openalex_state.STATE_ENV)
    with pytest.raises(AssertionError, match="machine-level state"):
        openalex_state.state_dir()


def test_the_real_default_is_per_user_and_identical_across_checkouts(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/someone/.cache")
    assert openalex_state.per_user_cache_dir() == Path("/home/someone/.cache/nekaise")
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert openalex_state.per_user_cache_dir() == Path.home() / ".cache" / "nekaise"


def test_spacing_is_shared_across_checkouts(monkeypatch, tmp_path):
    clock, slept = [100.0], []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock[0] += seconds

    monkeypatch.setattr(ops, "WORKSPACE", tmp_path / "checkout-a")
    a = openalex_state.SharedPacer(clock=lambda: clock[0], sleep=sleep)
    a.wait()
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path / "checkout-b")
    b = openalex_state.SharedPacer(clock=lambda: clock[0], sleep=sleep)
    clock[0] += 0.4
    b.wait()
    assert slept == [0.6]


# --- the legacy OpenAlex path uses the same pacer and throttle classification ------------------

def test_legacy_openalex_is_paced_and_classifies_a_budget_429(monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr(openalex_state.SharedPacer, "wait", lambda self: waits.append(1))

    def get(url, **kw):
        calls.append(url)
        return throttled({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "7200",
                          "Retry-After": "30"})

    monkeypatch.setattr(find_sources.requests, "get", get)
    monkeypatch.setattr(find_sources, "_POLICY", POLICY)
    with pytest.raises(openalex_state.Throttled, match="budget exhausted") as err:
        find_sources.from_openalex("modelica", "building_energy", 100)
    assert waits == [1] and err.value.response.status_code == 429
    until = openalex_state.Cooldowns().active("openalex")
    assert until and until > __import__("time").time() + 7000  # the daily reset, not 30 s
    # the next legacy request is refused WITHOUT a request (persisted cooldown, re-read)
    with pytest.raises(openalex_state.Throttled, match="cooldown active"):
        find_sources.from_openalex("modelica", "building_energy", 100)
    assert len(calls) == 1 and waits == [1]


def test_a_family_cooldown_stops_the_legacy_finder_mid_run(monkeypatch, tmp_path):
    calls = []

    def openalex(term, topic, per, page):
        calls.append(term)
        # meanwhile a family process persists an OpenAlex cooldown
        openalex_state.Cooldowns().raise_to({"openalex": __import__("time").time() + 600})
        return []

    monkeypatch.setattr(find_sources, "LEGACY_COOLDOWN_WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(find_sources, "QUERIES", [("one", "urban"), ("two", "urban")])
    monkeypatch.setattr(find_sources, "BACKENDS", {"openalex": openalex})
    monkeypatch.setattr(find_sources, "load_context", lambda partners=(): (POLICY, {}))
    monkeypatch.setattr(find_sources.dedup, "open_keys",
                        lambda: find_sources.dedup.from_sets(set(), set(), set()))
    hold = tmp_path / "hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_sources.py", "--backends", "openalex"])
    assert find_sources.main() == 0
    assert calls == ["one"] and "openalex" in hold.read_text()


# --- cooldown writes: locked max-merge, never erased -------------------------------------------

def test_a_stale_snapshot_never_erases_another_processes_cooldown():
    family_a, family_b = openalex_state.Cooldowns(), openalex_state.Cooldowns()
    snapshot_b = family_b.load()                           # taken at B's startup: empty
    family_a.raise_to({"openalex": 5e9})                   # A: OpenAlex budget cooldown
    find_sources.save_cooldowns({**snapshot_b, "crossref": 4e9})  # B saves its snapshot
    assert json.loads(family_a.path.read_text()) == {"crossref": 4e9, "openalex": 5e9}
    family_b.raise_to({"openalex": 3e9})                   # an earlier deadline never lowers
    assert family_a.active("openalex") == 5e9


def test_concurrent_writers_keep_every_deadline():
    stores = [openalex_state.Cooldowns() for _ in range(16)]
    threads = [threading.Thread(target=s.raise_to, args=({f"host{i}": 5e9 + i},))
               for i, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert openalex_state.Cooldowns().load() == {f"host{i}": 5e9 + i for i in range(16)}


def test_the_family_api_rereads_cooldowns_before_every_request():
    store = openalex_state.Cooldowns()
    http = FakeHttp(singles={f"{fam.OPENALEX}/W1": {"id": "https://openalex.org/W1"}})
    api = fam.Api(http, lookup_max=5, cooldowns=store, now=lambda: NOW, sleep=lambda s: None,
                  pacer=openalex_state.LocalPacer())
    assert api.lookup("openalex", "W1")
    openalex_state.Cooldowns().raise_to({"openalex": 5e9})  # another process
    with pytest.raises(fam.UpstreamError, match="cooldown active"):
        api.lookup("openalex", "W1")
    assert len(http.calls) == 1


def test_a_family_throttle_is_merged_not_overwritten():
    store = openalex_state.Cooldowns(clock=lambda: NOW)
    store.raise_to({"openalex": NOW + 3600})
    http = FakeHttp()
    http.singles = {}
    api = fam.Api(lambda url, **kw: SimpleNamespace(status_code=503, headers={"Retry-After": "5"},
                                                    json=lambda: {}),
                  lookup_max=5, cooldowns=store, now=lambda: NOW, sleep=lambda s: None,
                  pacer=openalex_state.LocalPacer())
    with pytest.raises(fam.UpstreamError, match="rate limited"):
        api.lookup("crossref", "10.1234/x")
    assert store.load() == {"openalex": NOW + 3600, "crossref": NOW + 5}


def test_legacy_cooldown_file_is_migrated_once(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / openalex_state.LEGACY_COOLDOWN_FILE).write_text(json.dumps({"openalex": 5e9}))
    openalex_state.migrate_legacy_cooldowns(ws)
    assert openalex_state.Cooldowns().active("openalex") == 5e9
    assert not (ws / openalex_state.LEGACY_COOLDOWN_FILE).exists()
    openalex_state.migrate_legacy_cooldowns(ws)  # idempotent


# --- per-family resolution records ---------------------------------------------------------------

def test_each_family_writes_only_its_own_record(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path)
    sim_path, ai_path = fam.default_ledger_path("simulation"), fam.default_ledger_path(
        "building-ai")
    assert sim_path != ai_path
    sim, ai = fam.Ledger(sim_path), fam.Ledger(ai_path)   # both loaded before either saves
    sim.upsert("doi:10.1234/s", status="unresolved", family_name="simulation")
    ai.upsert("doi:10.1234/a", status="unresolved", family_name="building-ai")
    sim.save()
    ai.save()
    assert list(fam.Ledger(sim_path).rows) == ["doi:10.1234/s"]
    assert list(fam.Ledger(ai_path).rows) == ["doi:10.1234/a"]


def test_the_shared_record_is_split_by_family_once(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path)
    old = tmp_path / fam.LEGACY_LEDGER
    old.write_text("".join(json.dumps(r) + "\n" for r in (
        {"key": "doi:10.1234/s", "status": "unresolved"},                    # pre-family rows
        {"key": "doi:10.1234/s2", "status": "unresolved", "family_name": "simulation"},
        {"key": "doi:10.1234/a", "status": "unresolved", "family_name": "building-ai"})))
    existing = fam.Ledger(tmp_path / "openalex-resolution-simulation.jsonl")
    existing.upsert("doi:10.1234/s", status="proposed")  # already per-family: kept as is
    existing.save()
    sim = fam.Ledger(fam.default_ledger_path("simulation"))
    ai = fam.Ledger(fam.default_ledger_path("building-ai"))
    assert sorted(sim.rows) == ["doi:10.1234/s", "doi:10.1234/s2"]
    assert sim.rows["doi:10.1234/s"]["status"] == "proposed"
    assert list(ai.rows) == ["doi:10.1234/a"]
    assert not old.exists() and (tmp_path / (fam.LEGACY_LEDGER + ".migrated")).exists()


def test_main_family_uses_its_familys_record(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path)
    from types import SimpleNamespace as NS
    import dedup
    reported = []
    args = NS(family="building-ai", family_cursor="ai1 t=4 q=0 w=0 p=1 k=0",
              resolution_file=None, append=True, lookup_max=250, per=100, max=25)
    works = [fam_work := good(9)]
    fam_work["title"] = "Large language model agents for HVAC fault diagnosis in buildings"
    fam_work["locations"] = []
    fam_work["doi"] = None
    code = fam.main_family(
        args, policy=POLICY, keys=dedup.from_sets(set(), set(), set()), cooldowns={},
        get=FakeHttp(pages=[page(works)]), openalex_relevant=find_sources.openalex_relevant,
        append_entries=lambda e: None, request_hold=lambda r: None, report_next=reported.append,
        now=NOW, sleep=lambda s: None, clock=lambda: NOW, pacer=openalex_state.LocalPacer())
    assert code == 0 and reported
    assert (tmp_path / "openalex-resolution-building-ai.jsonl").exists()
    assert not (tmp_path / "openalex-resolution-simulation.jsonl").exists()


# --- fifth review: a cooldown persisted while a request queued for its slot ------------------------

class CoolingPacer:
    """A pacer during whose wait another process persists an OpenAlex cooldown."""

    def wait(self):
        openalex_state.Cooldowns().raise_to({"openalex": 5e9})


def test_legacy_request_is_not_sent_after_a_cooldown_set_during_the_wait():
    calls = []
    with pytest.raises(openalex_state.Throttled, match="cooldown active"):
        openalex_state.openalex_get(lambda url, **kw: calls.append(url), fam.OPENALEX,
                                    params={}, cooldowns=openalex_state.Cooldowns(),
                                    pacer=CoolingPacer())
    assert calls == []


def test_family_request_is_not_sent_after_a_cooldown_set_during_the_wait():
    http = FakeHttp(pages=[page([])])
    api = fam.Api(http, lookup_max=5, cooldowns=openalex_state.Cooldowns(), now=lambda: NOW,
                  sleep=lambda s: None, pacer=CoolingPacer())
    with pytest.raises(fam.UpstreamError, match="cooldown active"):
        api.search({}, 100)
    assert http.calls == []


# --- fifth review: tests never touch a checkout's real legacy cooldown file -------------------------

def test_the_migration_source_is_isolated_and_guarded(tmp_path):
    real = Path(find_sources.__file__).resolve().parents[1] / "workspace"
    assert Path(find_sources.LEGACY_COOLDOWN_WORKSPACE).resolve() != real.resolve()
    with pytest.raises(AssertionError, match="real legacy cooldown file"):
        openalex_state.migrate_legacy_cooldowns(real)
    openalex_state.migrate_legacy_cooldowns(tmp_path)  # anything else is allowed
