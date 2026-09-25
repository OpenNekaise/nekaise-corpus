"""Discovery writes through ONE store transaction (ADR 0001 stage 3, step 5).

Equivalence: for the same finder outcomes, the store path (run_round.apply_discovery inside the
round broker's local batch) leaves tracked files byte-identical to the legacy path
(tests/legacy_discovery.py) — except the store journal, and exhaustion, which now lands in
registry/backend_state.json (runtime) instead of editing registry/backends.json (config). Also:
failed-round rollback, runtime-state contracts, the representation migration, and (when a test
database is configured) the same discovery against the PostgreSQL store."""
import copy
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import blocklist
import check_contracts
import legacy_discovery
import migrate_backend_state
import registry
import run_round
import store
import store_broker
from runids import rid

FIXTURES = Path(__file__).parent / "fixtures"

BACKENDS = {
    "_readme": "control plane",
    "find_a": {"script": "fake_finder.py", "args": [], "rotation": False, "enabled": True},
    "find_b": {"script": "fake_finder.py", "args": [], "enabled": True},
    "find_week": {"script": "fake_finder.py", "args": []},
    "find_dyn": {"script": "fake_finder.py", "args": [], "enabled": True},
    "find_opt": {"script": "fake_finder.py", "args": [], "required": False},
    "find_gh": {"script": "fake_finder.py", "args": [], "rotation": False},
    "find_paused": {"script": "fake_finder.py", "args": [], "rotation": False,
                    "enabled": False, "reason": "operator pause"},
}
ROTATION = {
    "find_b": {"flag": "--page", "next": 3, "step": 2},
    "find_week": {"flag": "--bucket", "next": "2021-W01", "skip": [["2020-W53", "2019-W52"]]},
    "find_dyn": {"flag": "--token", "next": "START", "dynamic": True},
    "find_opt": {"flag": "--page", "next": 10},
}


def entry(sid, title=None, url=None, **extra):
    return {"id": sid, "title": title or f"Title {sid}", "url": url or f"https://e.org/{sid}.pdf",
            "source": "fixture", "license": "cc-by", "topic": "construction", "format": "pdf",
            **extra}


def build_repo(root: Path) -> Path:
    reg = root / "registry"
    reg.mkdir(parents=True)
    (reg / "backends.json").write_text(json.dumps(BACKENDS, indent=2, ensure_ascii=False) + "\n")
    (reg / "eligibility.json").write_text(json.dumps({"version": 1, "restrictions": {}}) + "\n")
    (reg / "rotation.json").write_text(json.dumps(ROTATION, indent=2, ensure_ascii=False) + "\n")
    (reg / "github_passes.json").write_text(json.dumps({"gh_a": {"tex": "2026-01-01"}}, indent=2,
                                                       sort_keys=True) + "\n")
    (reg / "curated.yaml").write_text("# hand comment\nsources:\n"
                                      + registry.emit_entry(entry("hand-one")))
    (reg / "reports.yaml").write_text(registry.shard_header("reports") + registry.emit_entry(
        entry("ost-existing", "Existing Title", "https://e.org/existing.pdf")))
    man = root / "manifest"
    man.mkdir()
    rows = [{**entry("ost-manifest-only", "Manifest Only", "https://e.org/m.pdf"), "status": "ok"}]
    (man / f"{registry.manifest_shard('ost-manifest-only')}.jsonl").write_text(
        registry.manifest_shard_text(rows))
    (root / "pruned_urls.txt").write_text("https://e.org/blocked\n")
    (root / "README.md").write_text("readme\n")
    return root


def files(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and "workspace" not in p.parts}


def data_files(root: Path) -> dict:
    return {k: v for k, v in files(root).items() if not k.startswith("registry/journal/")}


class Finders:
    """Finder outcomes as run_finders_parallel collects them (proposal + side-channel files)."""

    def __init__(self, tmp: Path):
        self.dir = tmp / "proposals"
        self.dir.mkdir()
        self.results: list[dict] = []

    def add(self, name, entries=(), *, returncode=0, hold=None, next_pointer=None,
            exhausted=None, passes=None):
        index = len(self.results)
        base = self.dir / f"{index:03d}-{name}"
        proposal = base.with_suffix(".json")
        doc = [dict(e) for e in entries]
        proposal.write_text(json.dumps({"entries": doc, "github_passes": passes} if passes
                                       else doc))
        for suffix, value in ((".rotation-next", next_pointer), (".backend-exhausted", exhausted)):
            if value is not None:
                Path(f"{base}{suffix}").write_text(value + "\n")
        self.results.append({
            "index": index, "name": name, "proposal": proposal,
            "rotation_hold": hold is not None, "rotation_hold_detail": hold or "",
            "rotation_next": Path(f"{base}.rotation-next"),
            "backend_exhausted": Path(f"{base}.backend-exhausted"),
            "returncode": returncode, "stdout": "", "stderr": "", "elapsed": 0.0,
        })
        return self


@pytest.fixture
def quiet(monkeypatch):
    events = []
    monkeypatch.setattr(run_round.ops, "run_event",
                        lambda run_id, event, **fields: events.append((event, fields)))
    return events


def store_discovery(st, results, selected, run_id="rnd"):
    """The production path: protocol checks, then apply_discovery in the broker's local batch."""
    backends = {k: v for k, v in BACKENDS.items() if not k.startswith("_")}
    with st.writer(round_id=run_id) as w:
        broker = store_broker.Broker(st, w, run_id)
        with broker.serving():
            with st.read(writer=w) as view:
                rotation_state = view.rotation_get()
            successful = run_round.check_finder_results(results, backends, rotation_state, run_id)
            with broker.local_batch("discover", "merge") as tx:
                return run_round.apply_discovery(tx, successful, selected, backends)


def legacy_discovery_run(root, results, selected, monkeypatch):
    monkeypatch.setattr(registry, "ROOT", root)  # tests/legacy_registry.py follows it
    backends = {k: copy.deepcopy(v) for k, v in BACKENDS.items() if not k.startswith("_")}
    rotation_state = json.loads((root / "registry" / "rotation.json").read_text())
    successful = run_round.check_finder_results(results, backends, rotation_state, "legacy")
    return legacy_discovery.legacy_apply(root, successful, selected, backends, rotation_state)


def both(tmp_path, monkeypatch, finders, selected, *, index=True):
    if not index:
        monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    new = build_repo(tmp_path / "store")
    old = tmp_path / "legacy"
    shutil.copytree(new, old)
    st = store.FileStore(new)
    total, accepted, notes = store_discovery(st, finders.results, selected)
    l_total, l_accepted, l_events = legacy_discovery_run(old, finders.results, selected,
                                                         monkeypatch)
    assert (total, accepted) == (l_total, l_accepted)
    assert [(event, fields) for _, event, fields in notes] == l_events
    return st, new, old


ALL = [k for k in BACKENDS if not k.startswith("_") and k != "find_paused"]


# --- equivalence: store path vs legacy path ----------------------------------------------------------

@pytest.mark.parametrize("index", [True, False], ids=["index", "no-index"])
def test_cross_finder_collisions_and_id_suffixes_match_legacy(tmp_path, monkeypatch, quiet,
                                                               index):
    finders = Finders(tmp_path).add("find_a", [
        entry("ost-new-a", "Shared Title", "https://e.org/a.pdf"),
        entry("ost-existing", "Fresh title for an old id", "https://e.org/fresh.pdf"),  # id -> -2
        entry("oer-blocked", "Blocked", "https://e.org/blocked/"),                     # blocklist
        entry("vnd-hashed", "Vendor sheet", "https://v.example/sheet.pdf"),
    ]).add("find_b", [
        entry("ost-new-a", "Different title", "https://e.org/b.pdf"),       # id repeat -> -2
        entry("ost-dup-url", "Dup url", "https://e.org/a.pdf/"),            # url repeat
        entry("ost-dup-title", "shared   TITLE!", "https://e.org/c.pdf"),   # title repeat
        entry("ost-manifest-only", "Only in the manifest by id", "https://e.org/d.pdf"),  # -> -2
        entry("ost-known-url", "Known url", "https://e.org/m.pdf"),         # manifest url
        entry("kit-new-shard", "Into a new shard", "https://k.example/1"),
        entry("hand-two", "Curated append", "https://e.org/hand-two.pdf"),
    ])
    st, new, old = both(tmp_path, monkeypatch, finders, ["find_a", "find_b"], index=index)
    assert data_files(new) == data_files(old)
    ids = {e["id"] for e in registry.parse_yaml(
        (new / "registry" / "reports.yaml").read_text())["sources"]}
    assert {"ost-existing-2", "ost-new-a", "ost-new-a-2", "ost-manifest-only-2"} <= ids
    assert (new / "registry" / "kitopen.yaml").exists()
    # one transaction: the discovery merge, journaled
    with st.read() as v:
        runs = {e["run_id"] for e in v.scan(store.Table.EVENTS).rows}
    assert runs == {"rnd.discover.merge"}


def test_empty_successful_passes_match_legacy_and_write_nothing(tmp_path, monkeypatch, quiet):
    finders = Finders(tmp_path).add("find_a").add("find_gh")
    st, new, old = both(tmp_path, monkeypatch, finders, ["find_a", "find_gh"])
    assert files(new) == files(build_repo(tmp_path / "pristine"))  # not even a journal row
    assert data_files(new) == data_files(old)


def test_rotation_kinds_dynamic_weekly_integer_and_hold_match_legacy(tmp_path, monkeypatch, quiet):
    finders = (Finders(tmp_path)
               .add("find_b", [entry("ost-b1")])
               .add("find_week", [entry("ost-w1")])
               .add("find_dyn", [entry("kit-d1")], next_pointer="  cursor-2  ")
               .add("find_opt", [entry("ost-o1")], hold="capped at 60"))
    st, new, old = both(tmp_path, monkeypatch, finders, ALL)
    assert data_files(new) == data_files(old)
    state = json.loads((new / "registry" / "rotation.json").read_text())
    assert state["find_b"]["next"] == 5
    assert state["find_week"]["next"] == "2019-W51"   # skipped the mined range
    assert state["find_dyn"]["next"] == "cursor-2"
    assert state["find_opt"]["next"] == 10             # held: pointer kept


def test_github_passes_ride_in_the_same_transaction_and_match_legacy(tmp_path, monkeypatch, quiet):
    finders = Finders(tmp_path).add("find_gh", [entry("gh-x-readme")], passes={
        "gh_a": {"tex": "2026-09-24", "man": "2026-09-24"}, "gh_b": {"md": "2026-09-24"}})
    st, new, old = both(tmp_path, monkeypatch, finders, ["find_gh"])
    assert data_files(new) == data_files(old)
    assert json.loads((new / "registry" / "github_passes.json").read_text()) == {
        "gh_a": {"man": "2026-09-24", "tex": "2026-01-01"}, "gh_b": {"md": "2026-09-24"}}
    with st.read() as v:
        assert {e["run_id"] for e in v.scan(store.Table.EVENTS).rows} == {"rnd.discover.merge"}


def test_optional_failure_keeps_its_pointer_and_matches_legacy(tmp_path, monkeypatch, quiet):
    finders = (Finders(tmp_path)
               .add("find_a", [entry("ost-good")])
               .add("find_opt", [entry("ost-bad")], returncode=7))
    st, new, old = both(tmp_path, monkeypatch, finders, ["find_a", "find_opt"])
    assert data_files(new) == data_files(old)
    assert json.loads((new / "registry" / "rotation.json").read_text())["find_opt"]["next"] == 10
    assert "ost-bad" not in (new / "registry" / "reports.yaml").read_text()
    assert ("discovery_degraded", {"failures": {"find_opt": 7}}) in quiet


def test_exhaustion_is_runtime_state_and_otherwise_matches_legacy(tmp_path, monkeypatch, quiet):
    finders = Finders(tmp_path).add("find_dyn", [entry("kit-last")], next_pointer="END",
                                    exhausted="set fully harvested")
    st, new, old = both(tmp_path, monkeypatch, finders, ["find_dyn"])
    reason = "exhausted: set fully harvested"
    got, want = data_files(new), data_files(old)
    # the one intended difference: runtime state instead of a configuration edit
    assert got.pop("registry/backend_state.json") is not None
    assert "registry/backend_state.json" not in want
    assert got.pop("registry/backends.json") == files(build_repo(tmp_path / "p"))[
        "registry/backends.json"]  # configuration untouched
    legacy_config = json.loads(want.pop("registry/backends.json"))
    assert legacy_config["find_dyn"] == {**BACKENDS["find_dyn"], "enabled": False,
                                         "reason": reason}
    assert got == want
    assert json.loads((new / "registry" / "backend_state.json").read_text()) == {
        "find_dyn": {"enabled": False, "reason": reason}}
    with st.read() as v:  # effective enablement is the same as the legacy config edit
        assert v.backend_enabled("find_dyn") is False
        assert v.config_get().backends["find_dyn"]["enabled"] is True
        assert v.rotation_get("find_dyn")["next"] == "END"
    # and migrating the legacy representation lands exactly on the new one
    assert migrate_backend_state.migrate(old, ["find_dyn"], apply=True, log=lambda *_: None) == 0
    assert data_files(old)["registry/backend_state.json"] == \
        data_files(new)["registry/backend_state.json"]
    assert json.loads(data_files(old)["registry/backends.json"])["find_dyn"] == \
        BACKENDS["find_dyn"]


def test_invalid_proposal_fails_the_transaction_and_writes_nothing(tmp_path, quiet):
    root = build_repo(tmp_path / "repo")
    before = files(root)
    bad = {k: v for k, v in entry("ost-x").items() if k != "license"}
    finders = (Finders(tmp_path).add("find_b", [entry("ost-ok")])
               .add("find_a", [bad]))
    with pytest.raises(RuntimeError, match="find_a proposed ost-x without license"):
        store_discovery(store.FileStore(root), finders.results, ["find_b", "find_a"])
    assert files(root) == before


# --- the round: selection, the whole discovery phase, failed-round rollback -----------------------

def round_env(tmp_path, monkeypatch, root):
    monkeypatch.setattr(run_round, "ROOT", root)
    monkeypatch.setattr(run_round, "SCRIPTS", FIXTURES)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", root / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", root / "workspace")
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))


def configure_round_backends(root):
    """Two runnable fake finders plus a runtime-exhausted one and an operator-paused one."""
    cfg = {
        "_readme": "control plane",
        "find_b": {"script": "fake_finder.py", "enabled": True,
                   "args": ["--id", "ost-r1", "--title", "Round one", "--url", "https://e.org/r1"]},
        "find_dyn": {"script": "fake_finder.py", "enabled": True,
                     "args": ["--id", "kit-r2", "--title", "Round two", "--url", "https://e.org/r2",
                              "--next-pointer", "NEXT", "--exhausted", "walked to the end"]},
        "find_gone": {"script": "fake_finder.py", "rotation": False, "enabled": True,
                      "args": ["--exit-code", "9", "--id", "x", "--title", "x", "--url", "x"]},
        "find_paused": {"script": "fake_finder.py", "rotation": False, "enabled": False,
                        "reason": "operator pause",
                        "args": ["--exit-code", "9", "--id", "x", "--title", "x", "--url", "x"]},
    }
    (root / "registry" / "backends.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (root / "registry" / "rotation.json").write_text(json.dumps(
        {"find_b": ROTATION["find_b"], "find_dyn": ROTATION["find_dyn"]}, indent=2) + "\n")
    (root / "registry" / "backend_state.json").write_text(json.dumps(
        {"find_gone": {"enabled": False, "reason": "exhausted: earlier round"}}, indent=2,
        sort_keys=True) + "\n")


def test_round_runs_only_effectively_enabled_backends_and_rolls_everything_back(
        tmp_path, monkeypatch, capsys):
    root = build_repo(tmp_path / "repo")
    configure_round_backends(root)
    round_env(tmp_path, monkeypatch, root)
    before = files(root)
    events = []
    monkeypatch.setattr(run_round.ops, "run_event",
                        lambda run_id, event, **fields: events.append((event, fields)))
    seen = {}

    def fail_in_prune(step, cmd, env, run_id):
        seen[step] = files(root)  # discovery's transaction has committed by now
        if step == "prune":
            raise RuntimeError("prune exploded")

    monkeypatch.setattr(run_round, "run_command", fail_in_prune)
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-tests", "--run-id", rid("r-fail")])
    assert run_round.main() == 1
    assert "prune exploded" in capsys.readouterr().err
    started = [f["step"] for e, f in events if e == "step_started"]
    assert sorted(started) == ["discover:find_b", "discover:find_dyn"]  # not gone, not paused
    after_discovery = seen["fetch"]
    assert "ost-r1" in after_discovery["registry/reports.yaml"].decode()
    assert json.loads(after_discovery["registry/backend_state.json"])["find_dyn"] == {
        "enabled": False, "reason": "exhausted: walked to the end"}
    assert json.loads(after_discovery["registry/rotation.json"])["find_dyn"]["next"] == "NEXT"
    assert any(k.startswith("registry/journal/") for k in after_discovery)
    # the failed round restored every tracked byte (journal and runtime state included)
    assert files(root) == before
    assert ("state_rolled_back", {}) in events
    assert not run_round.ops.StateSnapshot.pending()


def test_round_refuses_malformed_runtime_state(tmp_path, monkeypatch, capsys):
    root = build_repo(tmp_path / "repo")
    configure_round_backends(root)
    (root / "registry" / "backend_state.json").write_text(json.dumps(
        {"find_nobody": {"enabled": False, "reason": "exhausted: ?"}}))
    round_env(tmp_path, monkeypatch, root)
    monkeypatch.setattr(run_round.ops, "run_event", lambda *a, **k: None)
    monkeypatch.setattr(run_round, "run_command", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-tests"])
    assert run_round.main() == 1
    assert "find_nobody: runtime backend state names an unknown backend" in capsys.readouterr().err


# --- contracts for runtime state ------------------------------------------------------------------

@pytest.mark.parametrize("runtime, message", [
    ({"find_x": store.BackendState(False, "exhausted: y")}, "find_x: runtime backend state names"),
    ({"find_a": store.BackendState(False, None)}, "find_a: runtime-disabled backend lacks a reason"),
    ({"find_a": store.BackendState("no", "why")}, "find_a: runtime enabled must be true or false"),
    ({"find_a": store.BackendState(False, "  ")}, "find_a: runtime reason must be a non-empty"),
    ({"find_a": store.BackendState(False, "policy-blocked: rights")},
     "find_a: policy blocks belong in registry/backends.json"),
])
def test_runtime_state_contract_errors(runtime, message):
    backends = {"find_a": {"script": "fake_finder.py", "rotation": False}}
    errors = run_round.runtime_state_errors(backends, runtime)
    assert len(errors) == 1 and errors[0].startswith(message)


def test_runtime_state_contract_accepts_defaults_and_exhaustion():
    backends = {"find_a": {"rotation": False}, "find_b": {"rotation": False}}
    assert run_round.runtime_state_errors(backends, {
        "find_a": store.BackendState(), "find_b": store.BackendState(False, "exhausted: done"),
    }) == []


def test_policy_rules_stay_on_configuration_and_runs_use_effective_enablement(tmp_path):
    restrictions = {"translated": {"match": {"id_prefix": "pat-cn"},
                                   "backends": ["find_patents_cn"]}}
    backends = {"find_patents_cn": {"script": "find_patents.py", "enabled": True,
                                    "args": ["--countries", "XX"]}}
    runtime = {"find_patents_cn": store.BackendState(False, "exhausted: all weeks")}
    # runtime exhaustion never satisfies a policy block: it must be disabled in configuration
    errors = check_contracts.eligibility_contract_errors((0, None), backends, restrictions)
    assert any("must be disabled" in e for e in errors)
    # ...but what may RUN is the effective enablement
    assert check_contracts.patent_country_contract_errors(backends) != []
    effective = check_contracts.effective_backends(backends, runtime)
    assert effective["find_patents_cn"]["enabled"] is False
    assert check_contracts.patent_country_contract_errors(effective) == []
    assert backends["find_patents_cn"]["enabled"] is True  # the configuration is not modified


def test_contracts_report_unreadable_runtime_state(tmp_path):
    root = build_repo(tmp_path / "repo")
    (root / "registry" / "backend_state.json").write_text(json.dumps(
        {"find_a": {"enabled": False, "reason": "x", "surprise": 1}}))
    with store.FileStore(root).read() as v:
        runtime, errors = check_contracts.runtime_backend_state(v)
    assert runtime == {} and "unreadable runtime state" in errors[0]


# --- the representation migration --------------------------------------------------------------------

def test_migration_preserves_pauses_and_reasons_and_is_idempotent(tmp_path):
    root = build_repo(tmp_path / "repo")
    raw = json.loads((root / "registry" / "backends.json").read_text())
    raw["find_dyn"].update(enabled=False, reason="exhausted: KIT set fully harvested")
    raw["find_b"].update(enabled=False, reason="exhausted: operator retired this vein")
    (root / "registry" / "backends.json").write_text(json.dumps(raw, indent=2) + "\n")
    log = []
    # dry run changes nothing
    before = files(root)
    assert migrate_backend_state.migrate(root, ["find_dyn"], apply=False, log=log.append) == 0
    assert files(root) == before and log[-1] == "dry run -- pass --apply to migrate"
    # an operator pause without the loop's prefix is refused; so is an enabled backend
    assert migrate_backend_state.migrate(root, ["find_paused", "find_a"], apply=True,
                                         log=log.append) == 1
    assert files(root) == before
    assert migrate_backend_state.migrate(root, ["find_dyn"], apply=True, log=log.append) == 0
    config = json.loads((root / "registry" / "backends.json").read_text())
    assert config["find_dyn"] == {**BACKENDS["find_dyn"], "enabled": True}
    assert config["find_b"]["reason"] == "exhausted: operator retired this vein"  # not named
    assert list(config) == list(raw)  # key order kept
    with store.FileStore(root).read() as v:
        assert v.backend_state_get("find_dyn") == store.BackendState(
            False, "exhausted: KIT set fully harvested")
        assert not v.backend_enabled("find_dyn") and not v.backend_enabled("find_b")
        assert not v.backend_enabled("find_paused")
    after = files(root)
    assert migrate_backend_state.migrate(root, ["find_dyn"], apply=True, log=log.append) == 0
    assert files(root) == after and "find_dyn: already migrated" in log


def test_migration_resumes_after_an_interruption_between_runtime_and_config(tmp_path):
    root = build_repo(tmp_path / "repo")
    raw = json.loads((root / "registry" / "backends.json").read_text())
    raw["find_dyn"].update(enabled=False, reason="exhausted: done")
    (root / "registry" / "backends.json").write_text(json.dumps(raw, indent=2) + "\n")
    (root / "registry" / "backend_state.json").write_text(json.dumps(
        {"find_dyn": {"enabled": False, "reason": "exhausted: done"}}))
    assert migrate_backend_state.migrate(root, ["find_dyn"], apply=True,
                                         log=lambda *_: None) == 0
    with store.FileStore(root).read() as v:
        assert v.config_get().backends["find_dyn"]["enabled"] is True
        assert not v.backend_enabled("find_dyn")


def test_real_runtime_state_is_valid_and_only_ever_pauses():
    """The live data (read as files: the suite may run inside a round that holds the lock)."""
    backends = run_round.load_backends()
    path = Path(run_round.__file__).resolve().parents[1] / "registry" / "backend_state.json"
    runtime = {k: store.BackendState(**v)
               for k, v in (json.loads(path.read_text()) if path.exists() else {}).items()}
    assert run_round.runtime_state_errors(backends, runtime) == []
    effective = check_contracts.effective_backends(backends, runtime)
    for name, cfg in backends.items():  # runtime state can pause, never enable
        assert effective[name]["enabled"] <= bool(cfg.get("enabled", True))
        assert effective[name]["enabled"] == (bool(cfg.get("enabled", True))
                                              and runtime.get(name, store.BackendState()).enabled)


# --- the same discovery against PostgreSQL ------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"), reason="NEKAISE_PG_TEST_DSN not set")
def test_postgres_discovery_matches_the_file_store(tmp_path, quiet):
    import uuid

    import store_pg
    finders = (Finders(tmp_path)
               .add("find_a", [entry("ost-p1", "Shared"), entry("ost-existing", "New old id")])
               .add("find_b", [entry("ost-p2", "shared"), entry("ost-p3")])
               .add("find_dyn", [entry("kit-p4")], next_pointer="C2", exhausted="dry")
               .add("find_gh", [], passes={"gh_z": {"md": "2026-09-24"}})
               .add("find_opt", [entry("ost-p5")], hold="cap"))
    file_root = build_repo(tmp_path / "file")
    pg_root = build_repo(tmp_path / "pg")
    fs = store.FileStore(file_root)
    pg = store_pg.PgStore(pg_root, dsn=os.environ["NEKAISE_PG_TEST_DSN"],
                          schema=f"d_{uuid.uuid4().hex[:12]}")
    try:
        pg.pin_config_from_files()
        # seed the PostgreSQL store with the file store's contents
        with fs.read() as v, pg.writer() as w:
            with pg.transaction("seed", expected_version=pg.version(), writer=w) as tx:
                tx.insert_entries(v.scan(store.Table.ENTRIES, limit=1000).rows)
                tx.upsert_manifest(v.scan(store.Table.MANIFEST, limit=1000).rows)
                tx.blocklist_add(r["url"] for r in v.scan(store.Table.BLOCKLIST).rows)
                for name, value in v.rotation_get().items():
                    tx.rotation_set(name, value)
                tx.control_set("github_passes.json", v.control_get("github_passes.json"))
        outcomes = [store_discovery(s, finders.results, ALL) for s in (fs, pg)]
        assert outcomes[0] == outcomes[1]
        with fs.read() as a, pg.read() as b:
            for getter in ("rotation_get", "backend_state_get"):
                assert getattr(a, getter)() == getattr(b, getter)()
            assert a.control_get("github_passes.json") == b.control_get("github_passes.json")
            assert a.scan(store.Table.ENTRIES, limit=1000).rows == \
                b.scan(store.Table.ENTRIES, limit=1000).rows
            assert not b.backend_enabled("find_dyn")
    finally:
        pg.drop()
