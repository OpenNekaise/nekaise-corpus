#!/usr/bin/env python3
"""integrity_sweep.py — resumable background integrity sweeps under PostgreSQL authority
(ADR 0001 stage 4 step 5). Per-run checks (verify_generation.run_checks, the artifact gate) cover
what each run changed; these sweeps cover everything, a bounded slice per invocation:

    python scripts/integrity_sweep.py metadata [--seconds 900] [--restart]
    python scripts/integrity_sweep.py artifacts [--seconds 900] [--restart]
    python scripts/integrity_sweep.py status

metadata — one pass over the whole dataset in key order, a slice per invocation, each slice
reading the CURRENT committed generation (one REPEATABLE READ snapshot per invocation; nothing is
pinned, so the pass never holds the fold back — with pinned generations every later generation
would stay an overlay, and reads slow down with the number of unfolded generations, see the
basis benchmark): every registry entry and manifest row (the lint checks, manifest/registry
agreement, restricted rows claiming corpus data, every payload claim of a successful row
resolvable on this machine unless its host is fetch-suspended), every blocklist and ledger row,
and the derived lookup/order columns of every physical row (the projection tables, and the
revisions of the promoted runs the current view overlays). A row changed behind the cursor
after the pass started is checked by the next pass (and was checked by its run's gates). The
last step, in ONE snapshot: the current generation's counters recounted by server-side
aggregates and compared with its recorded counters (its contracts receipt — the per-run delta
chain), so the delta arithmetic and the recount check each other; any difference is a failure.

artifacts — periodic re-verification: every registered artifact version with a local locator
re-hashed against its identity and size (read-only; the artifact gate verified it when it was
first referenced — this finds later damage).

State: workspace/integrity-sweep.json (atomic replace after every page, under a named lock so two
invocations never interleave; a busy lock skips the slice): per kind the pass in progress (its
cursor, failures so far) and the last completed pass (when, what it checked, its failures).
Every invocation advances at least one page. ops_health.py alerts on failures and on stale
passes. Under file authority there is nothing to sweep here (lint_registry.py and
check_contracts.py check everything every round); the command says so and exits 0.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import ops
import store
import verify_generation

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "workspace" / "integrity-sweep.json"
PAGE = 2000
MAX_FAILURES = 200


def load_state(path: Path | None = None) -> dict:
    path = STATE if path is None else path
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    path = STATE if path is None else path
    ops.atomic_write_text(path, json.dumps(state, indent=1, sort_keys=True) + "\n")


def _fail(part: dict, text: str) -> None:
    part.setdefault("failures", [])
    if len(part["failures"]) < MAX_FAILURES:
        part["failures"].append(text)
    part["failure_count"] = part.get("failure_count", 0) + 1


# --- metadata ---------------------------------------------------------------------------------------

PHASES = (*(t.value for t in (store.Table.ENTRIES, store.Table.MANIFEST, store.Table.BLOCKLIST,
                              store.Table.LEDGER)), "derived", "counters")


def _new_pass(dataset: str | None) -> dict:
    return {"dataset": dataset, "started_at": time.time(), "phase": PHASES[0], "cursor": None,
            "checked": {}, "failures": [], "failure_count": 0, "generations": []}


def _manifest_page(view, rows: list[dict], part: dict, policy, restricted, access) -> None:
    import artifact_store
    import host_policy
    import lint_registry
    entries = view.get_entries([r["id"] for r in rows])
    corpus_fields = verify_generation._corpus_fields()
    for r in rows:
        sid = r.get("id")
        for e in lint_registry.manifest_errors(r, entries.get(sid)):
            _fail(part, e)
        if store.evaluate(restricted, r) and any(f in r for f in corpus_fields):
            _fail(part, f"manifest {sid}: a restricted row claims corpus data")
        if r.get("status") != "ok" or host_policy.suspended(r.get("url"), policy):
            continue
        for stage in ("raw", "text", "corpus"):
            if artifact_store.claim(r, stage) is not None and access.path(r, stage) is None:
                _fail(part, f"manifest {sid}: its {stage} claim resolves to nothing here")


def _derived_page(view, part: dict, *, page: int) -> bool:
    """One page of derived-column checks over physical rows; returns True when the phase is
    done. Cursor: [kind, key] with kind entries | manifest | revisions."""
    import store_pg
    kind, after = part["cursor"] or ["entries", ""]
    if kind in ("entries", "manifest"):
        extra = ", sha256, shard, topic_key" if kind == "manifest" else ""
        rows = view._q(f"SELECT id, row_text, url_norm, url_key, title_norm, title_key{extra}, pids "
                       f"FROM {kind} WHERE id > %s ORDER BY id LIMIT %s", [after, page]).fetchall()
        for rid, text, *derived in rows:
            got = [bytes(v) if isinstance(v, memoryview) else v for v in derived]
            row = json.loads(text)
            want = list(store_pg.revision_keys(kind, row))
            if kind == "entries":
                want = want[:4]
            want.append(store_pg.pids_for(row))
            if got != want:
                _fail(part, f"{kind} {rid}: projection derived columns differ from its row")
        part["checked"][f"derived_{kind}"] = part["checked"].get(f"derived_{kind}", 0) + len(rows)
        if len(rows) == page:
            part["cursor"] = [kind, rows[-1][0]]
        else:
            part["cursor"] = ["manifest", ""] if kind == "entries" else ["revisions", ""]
        return False
    # the revisions of the runs this view overlays (promoted after the projection's generation)
    vis = view._visibility
    runs = [r for r, _ in vis.runs] if vis is not None else []
    rows = view._q(
        "SELECT rev_id::text, tbl, key, row_text, url_norm, url_key, title_norm, title_key, "
        "sha256, shard, topic_key, pids FROM revisions WHERE run_id = ANY(%s) AND op = 'put' AND "
        "tbl IN ('entries', 'manifest') AND rev_id > %s::bigint ORDER BY rev_id LIMIT %s",
        [runs, after or "0", page]).fetchall() if runs else []
    for rev, tbl, key, text, *derived in rows:
        got = [bytes(v) if isinstance(v, memoryview) else v for v in derived]
        row = json.loads(text)
        if got != [*store_pg.revision_keys(tbl, row), store_pg.pids_for(row)]:
            _fail(part, f"revision {rev} ({tbl} {key}): derived columns differ from its row")
    part["checked"]["derived_revisions"] = part["checked"].get("derived_revisions", 0) + len(rows)
    if len(rows) == page:
        part["cursor"] = ["revisions", rows[-1][0]]
        return False
    return True


def _resume(view, table: store.Table, last_key) -> store.Cursor | None:
    """A keyset cursor of `view` positioned after `last_key` (persisted between invocations) for
    the plain key-order scan of `table` — PgReadView.scan's own cursor identity."""
    if last_key is None:
        return None
    query_id = store._digest([table.value, repr(None), None, "key"])
    return store.Cursor(view._cursor_scope(), query_id, tuple(last_key))


def _slice(st, view, part: dict, *, deadline: float, page: int, save) -> None:
    """Advance the pass `part` over `view` (the current generation, one snapshot) until done or
    out of time, saving after every page."""
    import artifact_store
    import lint_registry
    restrictions, policy = store.pinned_policy(view)
    restricted = store.restriction_where(restrictions)
    access = artifact_store.VersionedAccess(Path(st.root))
    if view.generation not in part["generations"]:
        part["generations"].append(view.generation)
    progressed = False   # every invocation advances at least one page, whatever its budget
    while part["phase"] != "done" and (not progressed or time.monotonic() < deadline):
        progressed = True
        phase = part["phase"]
        if phase in ("entries", "manifest", "blocklist", "ledger"):
            table = store.Table(phase)
            got = view.scan(table, cursor=_resume(view, table, part["cursor"]), limit=page)
            if phase == "entries":
                for e in got.rows:
                    for err in lint_registry.entry_errors(
                            e, store.codec.shard_filename(e["id"])):
                        _fail(part, err)
            elif phase == "manifest":
                _manifest_page(view, got.rows, part, policy, restricted, access)
            elif phase == "ledger":
                for r in got.rows:
                    if missing := [f for f in verify_generation.LEDGER_REQUIRED if not r.get(f)]:
                        _fail(part, f"ledger {r.get('id')}: missing {', '.join(missing)}")
            part["checked"][phase] = part["checked"].get(phase, 0) + len(got.rows)
            if got.next_cursor is None:
                part["phase"], part["cursor"] = PHASES[PHASES.index(phase) + 1], None
            else:
                part["cursor"] = list(got.next_cursor.last_key)
        elif phase == "derived":
            if _derived_page(view, part, page=page):
                part["phase"], part["cursor"] = "counters", None
        else:   # counters, in this one snapshot: the recount vs the recorded delta chain
            full = verify_generation.full_counters(view, restrictions)
            recorded = verify_generation.recorded_counters(view, view.generation)
            if recorded is None:
                part["recorded"] = "none (no passed contracts receipt carries counters)"
            elif recorded != full:
                _fail(part, f"generation {view.generation}'s recorded counters differ from its "
                            f"recount: {_diff(recorded, full)}")
            else:
                part["recorded"] = "equal"
            part["counters_generation"] = view.generation
            part["counters_full"] = full
            part["phase"] = "done"
        save(part)


def metadata(st, *, seconds: float, restart: bool, page: int = PAGE,
             state_path: Path | None = None, log=print) -> dict:
    state = load_state(state_path)

    def save(part):
        state.setdefault("metadata", {})["current"] = part
        save_state(state, state_path)

    deadline = time.monotonic() + seconds
    with st.read() as view:
        if view.generation is None:
            log("no promoted generation yet: nothing to sweep")
            return state
        dataset = (view.provenance() or {}).get("dataset")
        part = (state.get("metadata") or {}).get("current")
        if restart or part is None or part.get("dataset") != dataset:
            part = _new_pass(dataset)
            save(part)
        _slice(st, view, part, deadline=deadline, page=page, save=save)
    if part["phase"] == "done":
        done = {k: part.get(k) for k in ("dataset", "checked", "failures", "failure_count",
                                         "generations", "counters_generation", "counters_full",
                                         "recorded")}
        done.update(started_at=part["started_at"], completed_at=time.time())
        state["metadata"] = {"current": None, **done}
        save_state(state, state_path)
        log(f"metadata sweep complete (generations {part['generations']}): "
            f"{part.get('failure_count', 0)} failure(s); checked {part['checked']}")
    else:
        log(f"metadata sweep: phase {part['phase']} (resumes next invocation); failures so far "
            f"{part.get('failure_count', 0)}")
    return state


def _diff(a: dict, b: dict) -> str:
    keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return ", ".join(f"{k}: {str(a.get(k))[:80]} != {str(b.get(k))[:80]}" for k in keys[:5])


# --- artifacts ----------------------------------------------------------------------------------------

def artifacts(st, *, seconds: float, restart: bool, page: int = 500,
              state_path: Path | None = None, log=print) -> dict:
    """Re-hash registered local artifact versions from the cursor on, until done or out of
    time. Read-only."""
    import artifact_store
    state = load_state(state_path)
    part = (state.get("artifacts") or {}).get("current")
    if restart or part is None:
        part = {"cursor": ["", ""], "started_at": time.time(), "checked": 0, "bytes": 0,
                "failures": [], "failure_count": 0}
    local = artifact_store.LocalArtifacts(Path(st.root))
    deadline = time.monotonic() + seconds
    finished = False
    with st.read() as view:
        while time.monotonic() < deadline:
            rows = view._q(
                "SELECT a.stage, a.sha256, a.size, l.locator FROM artifacts a JOIN "
                "artifact_locators l USING (stage, sha256) WHERE l.kind = 'local' AND "
                "(a.stage, a.sha256) > (%s, %s) ORDER BY a.stage, a.sha256 LIMIT %s",
                [part["cursor"][0], part["cursor"][1], page]).fetchall()
            for stage, sha, size, locator in rows:
                if locator != artifact_store.local_locator(stage, sha):
                    _fail(part, f"{stage} {sha}: unexpected local locator {locator}")
                elif not local.verify(stage, sha, size):
                    _fail(part, f"{stage} {sha}: missing or damaged ({size} bytes expected)")
                else:
                    part["bytes"] += size
                part["checked"] += 1
            if rows:
                part["cursor"] = [rows[-1][0], rows[-1][1]]
            state["artifacts"] = {**(state.get("artifacts") or {}), "current": part}
            save_state(state, state_path)
            if len(rows) < page:
                finished = True
                break
    if finished:
        done = {k: part[k] for k in ("started_at", "checked", "bytes", "failures",
                                     "failure_count")}
        state["artifacts"] = {"current": None, **done, "completed_at": time.time()}
        save_state(state, state_path)
        log(f"artifact re-verification complete: {part['checked']} versions, "
            f"{part['bytes'] / 1e9:.2f} GB, {part['failure_count']} failure(s)")
    else:
        log(f"artifact re-verification: {part['checked']} versions so far (resumes next "
            f"invocation); failures so far {part['failure_count']}")
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=("metadata", "artifacts", "status"))
    ap.add_argument("--seconds", type=float, default=900.0)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    state_path = root / "workspace" / "integrity-sweep.json"
    if args.kind == "status":
        print(json.dumps(load_state(state_path), indent=1, sort_keys=True))
        return 0
    import staged_runs
    st = store.open(root=root)
    if not staged_runs.staged_authority(st):
        print("file authority: lint_registry.py and check_contracts.py check the whole state "
              "every round; the PostgreSQL sweeps have nothing to do here")
        return 0
    from contextlib import ExitStack
    with ExitStack() as stack:
        try:   # one sweep at a time owns the state file; a busy lock is not a failure
            stack.enter_context(ops.named_lock("integrity-sweep", timeout=0,
                                               workspace=root / "workspace"))
        except RuntimeError as exc:
            print(f"another sweep is running ({exc}); this slice is skipped")
            return 0
        if args.kind == "metadata":
            state = metadata(st, seconds=args.seconds, restart=args.restart,
                             state_path=state_path)
            part = state.get("metadata") or {}
        else:
            state = artifacts(st, seconds=args.seconds, restart=args.restart,
                              state_path=state_path)
            part = state.get("artifacts") or {}
    current = part.get("current") or part
    return 1 if current.get("failure_count") else 0


if __name__ == "__main__":
    sys.exit(main())
