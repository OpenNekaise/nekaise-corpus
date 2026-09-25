#!/usr/bin/env python3
"""staged_runs.py — the PostgreSQL-authoritative run lifecycle shared by rounds, standalone
commands and maintainer repairs (ADR 0001 stage 4, step 4).

Selection. `staged_authority(st)` is true exactly when the host authority record
(scripts/store_authority.py) makes PostgreSQL authoritative for the store's root: the store is a
PgStore bound to a "postgres" record. Then every mutation of tracked state — a round, a standalone
`rotation.py advance` / `blocklist.add` / `migrate_backend_state.py`, a standalone fetch/prune/
clean, a maintainer repair — is a staged run: its batches are staged, the run is frozen, the
required gates run against the frozen state and record their receipts, and ONE promotion makes it
generation G+1. Anywhere else (file authority, unbound roots, tests that address a schema
directly) the legacy paths are unchanged.

Identity of a run (checked again when a run is resumed): the producer commit (git HEAD of a clean
checkout), the exact bytes of its configuration files (a sealed config set), the extractor
version and the cleaning ruleset it inherits from its parent generation (before the first
generation: the legacy corpus/.ruleset stamp — the policy the cutover carries over).

After a promotion the run stands; what follows is completion work that any later recovery repeats
idempotently (`after_promotion`): the incremental refresh of the corpus/ materialization, and
bounded housekeeping — folding promoted generations into the projection and purging aborted runs
after a grace period, each in short transactions under a time budget, so no caller holds the writer
for work that grows with the number of batches.
"""
from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import store

# The gates every standalone mutation passes before its promotion (read-only over the frozen
# state; "artifacts" re-hashes the versions the run introduced, "check" is the versioned claim
# check of clean_corpus.py --check inside a staged view).
# The gates every staged run passes before its promotion — rounds, standalone mutations and
# maintainer repairs alike: read-only over the frozen state ("artifacts" re-hashes the versions the
# run introduced, "check" is the versioned claim check of clean_corpus.py --check inside a staged
# view) plus the test suite of the code that ran.
STANDALONE_GATES = ("artifacts", "check", "contracts", "lint", "tests")
GATE_COMMANDS = {
    "check": ("clean_corpus.py", "--check"),
    "lint": ("lint_registry.py",),
    "contracts": ("check_contracts.py",),
}
# The pipeline stages a standalone step's run completes before it freezes: what a step stages must
# be what the gates accept — a standalone fetch's new rows are pruned and cleaned (their corpus
# claims written) in the same run, exactly as a round's fetch is followed by prune and clean.
DOWNSTREAM = {
    "fetch": (("prune", "prune_corpus.py", ("--apply",)), ("clean", "clean_corpus.py", ())),
    "prune": (("clean", "clean_corpus.py", ()),),
}
# An aborted run's staging stays inspectable this long before housekeeping purges it.
PURGE_GRACE_SECONDS = 24 * 3600
# Housekeeping's time budget per call (each fold or purge step is its own short transaction).
HOUSEKEEPING_SECONDS = 60.0


class StagedRunError(store.StoreError):
    """A staged run could not be opened, gated or promoted."""


class GateFailed(StagedRunError):
    """A required gate failed at the frozen state; the run is not promoted."""


def staged_authority(st) -> bool:
    """True when `st` is the PostgreSQL store the host authority record makes authoritative for
    its root (only then are rounds, standalone commands and the maintainer's repairs staged
    runs). Never inferred from the environment alone."""
    import store_authority
    try:
        import store_pg
    except ImportError:   # no psycopg: nothing can be PostgreSQL-authoritative here
        return False
    rec = getattr(st, "_authority", None)
    return (isinstance(st, store_pg.PgStore) and isinstance(rec, store_authority.Record)
            and rec.mode == "postgres")


# --- a run's identity ---------------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def producer_commit(root: Path) -> str:
    """The commit whose code and configuration a run executes: HEAD of the checkout at `root`.
    Raises unless it resolves to a full object name."""
    got = _git(Path(root), "rev-parse", "--verify", "-q", "HEAD^{commit}")
    sha = got.stdout.strip()
    if got.returncode or len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
        raise StagedRunError(f"cannot establish the producer commit at {root}: "
                             f"{got.stderr.strip() or 'no commit'}")
    return sha


def tree_clean(root: Path) -> bool:
    """No tracked or untracked change in the checkout: the producer commit describes exactly the
    code and configuration that run. A git failure raises (never read as clean)."""
    got = _git(Path(root), "status", "--porcelain")
    if got.returncode:
        raise StagedRunError(f"git status failed at {root}: {got.stderr.strip()}")
    return not got.stdout.strip()


def config_documents(root: Path) -> dict[str, bytes]:
    """The exact bytes of the git-owned configuration files at `root` (store.CONFIG_FILES)."""
    docs = {}
    for name in store.CONFIG_FILES:
        path = store.config_path(name, Path(root))
        if path.exists():
            docs[name] = path.read_bytes()
    return docs


def config_digest(documents: Mapping[str, bytes]) -> str:
    """The config set digest a run pinning `documents` gets (store_pg.Contracts.put_config_set)."""
    import hashlib
    return store._digest({name: hashlib.sha256(bytes(data)).hexdigest()
                          for name, data in sorted(documents.items())})


def extractor_version() -> str:
    import build_corpus
    return build_corpus.EXTRACTOR_VERSION


def inherited_ruleset(st, root: Path) -> str:
    """The cleaning ruleset a new run inherits: its parent generation's (policy is pinned with
    the generation), or before the first generation the legacy corpus/.ruleset stamp that the
    file-authoritative corpus was built with ("none" when there is none). Routine runs never
    change it; a ruleset change is a separate, reviewed decision."""
    import clean_corpus
    with st.read() as view:
        prov = view.provenance()
    if prov:
        return prov["cleaning_ruleset"]
    stamp = Path(root) / "corpus" / ".ruleset"
    spec = stamp.read_text().strip() if stamp.exists() else "none"
    if spec.startswith("IN-PROGRESS"):
        spec = spec.split(None, 1)[1].strip() if " " in spec else ""
    rules = clean_corpus.parse_rules(spec or "none")
    return ",".join(rules) if rules else "none"


@dataclass(frozen=True)
class Identity:
    producer_commit: str
    config: dict
    extractor_version: str
    cleaning_ruleset: str

    @property
    def config_digest(self) -> str:
        return config_digest(self.config)


def identity_changed(root: Path, run: Mapping) -> str | None:
    """What of a run's identity (its row: producer commit, config digest) the checkout at `root`
    no longer matches, or None. A git failure raises."""
    root = Path(root)
    if not tree_clean(root):
        return "the checkout (uncommitted changes)"
    if producer_commit(root) != run["producer_commit"]:
        return "the producer commit"
    if config_digest(config_documents(root)) != run["config_digest"]:
        return "the configuration"
    return None


def identity(st, root: Path, *, require_clean: bool = True) -> Identity:
    """The identity a new (or resumed) run at `root` has now; refuses a dirty checkout, whose
    HEAD would not describe the code that runs."""
    root = Path(root)
    if require_clean and not tree_clean(root):
        raise StagedRunError(f"the checkout at {root} has uncommitted changes: a staged run "
                             "records its producer commit, which must be exactly the code that "
                             "runs — commit or stash first")
    return Identity(producer_commit(root), config_documents(root), extractor_version(),
                    inherited_ruleset(st, root))


# --- gates ----------------------------------------------------------------------------------------------

def gate_command(gate: str, root: Path, python: str = sys.executable) -> list[str]:
    """The gate's command in the checkout at `root` (its own scripts judge its own state)."""
    if gate == "tests":
        return [python, "-m", "pytest", "-q", "tests/"]
    return [python, str(Path(root) / "scripts" / GATE_COMMANDS[gate][0]), *GATE_COMMANDS[gate][1:]]


def run_gates(rnd, root: Path, gates: Sequence[str], *, env: Mapping[str, str] | None = None,
              log=print) -> dict[str, dict]:
    """Run `gates` against the frozen run `rnd` (store_broker.StagedRound) and record every
    verdict as a gate receipt bound to the frozen state: "artifacts" in-process
    (StagedRound.verify_artifacts), the others as read-only subprocesses pinned at the frozen
    sequence (rnd.gate_env()), concurrently. Raises GateFailed naming every failed gate, after
    all verdicts are recorded."""
    from concurrent.futures import ThreadPoolExecutor

    import store_broker
    # read-only: no broker capability reaches a gate (a maintenance window exports its own)
    base = {k: v for k, v in (os.environ if env is None else env).items()
            if k not in (store_broker.BROKER_ENV, store_broker.CAP_ENV, store_broker.ROUND_ENV,
                         store_broker.ATTEMPT_ENV)}
    commands = [(g, gate_command(g, root)) for g in gates if g != "artifacts"]
    results: dict[str, dict] = {}

    import store_staging

    def execute(gate: str, cmd: list[str]):
        started = time.monotonic()
        # the test suite builds its own stores: it gets no read pin (like run_round's)
        env = ({k: v for k, v in base.items() if k != store_staging.STAGE_ENV}
               if gate == "tests" else {**base, **rnd.gate_env()})
        got = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
        return gate, got, round(time.monotonic() - started, 3)

    with ThreadPoolExecutor(max_workers=max(1, len(commands))) as pool:
        done = [f.result() for f in [pool.submit(execute, g, c) for g, c in commands]]
    for gate, got, elapsed in done:
        passed = got.returncode == 0
        rnd.record_gate(gate, passed=passed, detail={"exit": got.returncode,
                                                     "seconds": elapsed})
        results[gate] = {"passed": passed, "exit": got.returncode, "seconds": elapsed}
        if not passed:
            log(f"gate {gate} failed (exit {got.returncode}): "
                f"{shlex.join(gate_command(gate, root))}")
            for text in (got.stdout, got.stderr):
                if text.strip():
                    log(text.rstrip()[-4000:])
    if "artifacts" in gates:
        verified = rnd.verify_artifacts()
        results["artifacts"] = {"passed": not verified["failed"],
                                "verified": verified["verified"],
                                "failed": [list(f) for f in verified["failed"][:20]]}
    failed = sorted(g for g, r in results.items() if not r["passed"])
    if failed:
        raise GateFailed(f"run {rnd.run.run_id}: required gate(s) failed at the frozen state: "
                         + ", ".join(failed))
    return results


# --- after a promotion ------------------------------------------------------------------------------------

def housekeeping(st, writer, *, seconds: float = HOUSEKEEPING_SECONDS,
                 purge_grace: float = PURGE_GRACE_SECONDS, fold_limit: int | None = None) -> dict:
    """Bounded background work under the caller's writer: fold promoted generations into the
    projection (store_staging.fold, one short transaction per step, stopping at retention pins)
    and purge aborted runs queued at least `purge_grace` seconds ago (store_staging.purge_run,
    likewise). Stops when done or when `seconds` have passed; whatever is left is picked up by
    the next call (after the next promotion, or by recovery). Returns what it did."""
    import store_staging
    deadline = time.monotonic() + max(0.0, seconds)
    out = {"folded_generations": 0, "folded_rows": 0, "fold_blocked": False,
           "purged_runs": 0, "purged_rows": 0, "budget_exhausted": False}
    limit = fold_limit or store_staging.FOLD_BATCH
    while True:
        if time.monotonic() >= deadline:
            out["budget_exhausted"] = True
            return out
        progress = store_staging.fold(st, writer, limit=limit)
        out["folded_rows"] += progress.rows
        out["folded_generations"] += progress.done
        if progress.generation is None:
            break
        if progress.blocked:
            out["fold_blocked"] = True
            break
    for run_id in store_staging.purge_due(st, writer, grace_seconds=purge_grace):
        while True:
            if time.monotonic() >= deadline:
                out["budget_exhausted"] = True
                return out
            n = store_staging.purge_run(st, writer, run_id, limit=limit)
            out["purged_rows"] += n
            if n == 0:
                out["purged_runs"] += 1
                break
    return out


def after_promotion(st, writer, root: Path, *, seconds: float = HOUSEKEEPING_SECONDS) -> dict:
    """Completion work for the current generation (idempotent; recovery repeats it): refresh the
    corpus/ materialization to the current generation (incremental from its stamp), then bounded
    housekeeping. Raises when the materialization cannot be completed (its stamp then stays
    "refreshing" and consumers are refused)."""
    import artifact_store
    import materialize
    with st.read(writer=writer) as view:
        promoted = view.generation is not None
    out = {"materialized": materialize.refresh(st, Path(root)) if promoted
           else {"mode": None, "generation": None}}
    out["housekeeping"] = housekeeping(st, writer, seconds=seconds)
    out["swept_incoming"] = artifact_store.LocalArtifacts(Path(root)).sweep_incoming()
    return out


def materialization_current(st, root: Path) -> bool:
    """corpus/ is a complete materialization of the current generation (or there is none yet)."""
    import materialize
    with st.read() as view:
        generation, dataset = view.generation, (view.provenance() or {}).get("dataset")
    if generation is None:
        return True
    stamp = materialize.read_stamp(Path(root) / "corpus") or {}
    return (stamp.get("state"), stamp.get("generation"), stamp.get("dataset")) == (
        "complete", generation, dataset)


# --- standalone mutations as staged runs --------------------------------------------------------------------

def refuse_unfinished(st, writer) -> None:
    """A staged run left open or frozen by a crash must be recovered (aborted, or explicitly
    resumed) before anything else stages: its outcome decides what the next run is based on."""
    import store_staging
    if left := store_staging.unfinished_runs(st, writer):
        raise StagedRunError(
            "unfinished staged run(s) " + ", ".join(f"{r['run_id']} ({r['status']})" for r in left)
            + ": recover first (python scripts/run_round.py --recover latest aborts them; "
            "--resume RUN_ID continues one) — nothing else may stage meanwhile")


def standalone_id(step: str) -> str:
    return store._check_run_id(
        f"{step}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}")


@dataclass
class Standalone:
    """One standalone command's staged run (see standalone())."""
    st: object
    writer: object
    rnd: object
    step: str
    view: object = None
    staged: int = 0
    transactions: list = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.rnd.run.run_id

    def submit(self, batch: str, requests: list[dict], expected_version) -> tuple[list, object]:
        """Stage `requests` as batch (run, step, batch); returns (results, the run's version)."""
        got = self.st.stage_batch(self.writer, self.run_id, self.step, batch, requests,
                                  expected_version=expected_version)
        self.staged += 1
        self.transactions.append(f"{self.run_id}.{self.step}.{batch}")
        return got.results, got.version


@contextmanager
def standalone(st, step: str, *, timeout: float = 30.0, writer=None,
               gates: Sequence[str] = STANDALONE_GATES, log=None) -> Iterator[Standalone]:
    """A standalone command's mutations as ONE staged run of kind "standalone": the command's
    own writer (waiting at most `timeout` for a running round) or `writer`; refused while a
    crashed run is unfinished; the run inherits the parent generation's policy and pins this
    checkout's commit and configuration. The body stages batches (Standalone.submit) against the
    run's overlay (Standalone.view is opened by the caller: store_broker.step_session and
    run_batch do). On a clean exit: nothing staged -> the run is aborted as a no-op; otherwise
    frozen, gated (`gates`, receipts bound to the frozen state), promoted, and the promotion
    completed (after_promotion). Before freezing, the pipeline stages after `step` (DOWNSTREAM:
    a fetch's prune and clean) run as the run's children through its broker, so the gates see a
    complete state (a fetch's run shares its loader/pruner handoff through NEKAISE_RUN_ID, as a
    round does). The command's process is tagged with the run id (NEKAISE_RUN_OWNER) and holds
    the run's ownership mark, so recovery finds every process it started — exec'd or forked —
    if it dies. Any failure aborts the run (store_broker.staged_round: owned processes stopped,
    broker drained, durable status decides)."""
    import store_broker
    log = log or (lambda msg: print(msg, file=sys.stderr))
    root = Path(st.root)
    run_id = standalone_id(step)
    downstream = DOWNSTREAM.get(step, ())
    lifecycle = None
    with ExitStack() as stack:
        if writer is None:
            # the lifecycle lock before the writer, until this run's mark is installed; a dead
            # coordinator's forks may hold its writer session: they are stopped first
            import run_ownership
            lifecycle = stack.enter_context(run_ownership.lifecycle(root, timeout=timeout))
            run_ownership.sweep_dead(root)
            writer = stack.enter_context(st.writer(timeout=timeout, round_id=run_id))
        if any(name == "prune" for name, _, _ in downstream):
            # the loader and the pruner of ONE run share its deferral handoff, keyed by
            # NEKAISE_RUN_ID exactly as in a round (a lone standalone prune has none)
            saved = os.environ.get("NEKAISE_RUN_ID")
            os.environ["NEKAISE_RUN_ID"] = run_id
            stack.callback(lambda: os.environ.__setitem__("NEKAISE_RUN_ID", saved)
                           if saved is not None else os.environ.pop("NEKAISE_RUN_ID", None))
        refuse_unfinished(st, writer)
        ident = identity(st, root)
        with store_broker.staged_round(st, writer, run_id, kind="standalone",
                                       producer_commit=ident.producer_commit,
                                       extractor_version=ident.extractor_version,
                                       cleaning_ruleset=ident.cleaning_ruleset,
                                       config_documents=ident.config, tag=True,
                                       lifecycle=lifecycle) as rnd:
            session = Standalone(st, writer, rnd, step)
            yield session
            if not session.staged:
                rnd.broker.drain()
                st.abort_run(writer, run_id, reason="no-op: nothing staged")
                return
            for name, script, args in downstream:
                cmd = [sys.executable, str(root / "scripts" / script), *args]
                log(f"{step}: completing the run with {name}: {shlex.join(cmd)}")
                done = subprocess.run(cmd, cwd=root, env={**os.environ, **rnd.broker.env()})
                if done.returncode:
                    raise StagedRunError(f"standalone run {run_id}: its {name} stage failed "
                                         f"(exit {done.returncode})")
            rnd.freeze(list(gates))
            run_gates(rnd, root, gates, log=log)
            generation = rnd.promote()
        log(f"{step}: promoted generation {generation} (standalone run {run_id})")
        try:   # the promotion stands; recovery and the next promotion repeat this work
            after_promotion(st, writer, root)
        except Exception as exc:
            log(f"WARNING: generation {generation} is promoted but its completion work did not "
                f"finish ({type(exc).__name__}: {exc}); the next round or recovery repeats it")
